from __future__ import annotations

import json
from multiprocessing import get_context
from pathlib import Path

import pytest

from memory.health import doctor_home
from memory.markdown import parse_session_file
from memory.models import RawMemoryInput
from memory.persistence import JournalRecoveryConflict, content_fingerprint
from tests.persistence_helpers import (
    seed_single,
    seed_three_historical_memories,
    snapshot_project,
)
from tests.worker_helpers import (
    PROJECT,
    configured_service,
    crash_merge_worker,
    crash_save_worker,
)


SAVE_OPERATION_ID = "66666666-6666-4666-8666-666666666666"
MERGE_OPERATION_ID = "88888888-8888-4888-8888-888888888888"


@pytest.mark.parametrize(
    (
        "phase",
        "expected_markdown",
        "expected_db",
        "has_pending_journal",
    ),
    [
        ("after_all_temps_fsync", "old", "old", False),
        ("after_journal_fsync", "old", "old", True),
        ("after_db_write", "old", "old", True),
        ("after_target_replace:0", "new", "old", True),
        ("after_db_commit", "new", "new", True),
        ("after_journal_remove", "new", "new", False),
        ("before_vector_write", "new", "new", False),
    ],
)
def test_save_failure_boundary_recovers_by_contract(
    tmp_path: Path,
    phase: str,
    expected_markdown: str,
    expected_db: str,
    has_pending_journal: bool,
) -> None:
    memory_home = tmp_path / ".memory"
    memory_id, _ = seed_single(memory_home)
    process = get_context("spawn").Process(
        target=crash_save_worker,
        args=(str(memory_home), phase),
    )
    process.start()
    process.join(20)
    assert process.exitcode == 91

    crashed = snapshot_project(memory_home)
    assert crashed.markdown[memory_id][0] == expected_markdown
    assert crashed.database[memory_id][0] == expected_db
    assert bool(crashed.journal_paths) is has_pending_journal

    if phase == "after_all_temps_fsync":
        findings = doctor_home(memory_home, project=PROJECT)["findings"]
        assert any(
            finding["code"] == "stale_prepared_file"
            for finding in findings
        )
        return

    service = configured_service(str(memory_home))
    try:
        expected_recoveries = (
            (SAVE_OPERATION_ID,) if has_pending_journal else ()
        )
        assert service.persistence.startup_recoveries == expected_recoveries
    finally:
        service.close()

    recovered = snapshot_project(memory_home)
    assert recovered.journal_paths == ()
    assert recovered.markdown == recovered.database
    assert recovered.markdown[memory_id][0] == "new"
    assert doctor_home(memory_home, project=PROJECT)["status"] == "ok"


def test_lost_response_after_commit_replays_exact_operation(
    tmp_path: Path,
) -> None:
    memory_home = tmp_path / ".memory"
    memory_id, _ = seed_single(memory_home)
    process = get_context("spawn").Process(
        target=crash_save_worker,
        args=(str(memory_home), "after_db_commit"),
    )
    process.start()
    process.join(20)
    assert process.exitcode == 91

    service = configured_service(str(memory_home))
    try:
        replayed = service.save(
            RawMemoryInput(title="crash-boundary", what="new"),
            project=PROJECT,
            authoritative_source="gemini-cli",
            idempotency_key=SAVE_OPERATION_ID,
        )
        assert replayed["id"] == memory_id
        assert replayed["action"] == "replayed"
        row = service.get_memory_record(memory_id)
        history = json.loads(row["operation_history"])
        assert sum(
            item["operation_id"] == SAVE_OPERATION_ID
            for item in history
        ) == 1
        assert row["updated_count"] == 1
    finally:
        service.close()

    replayed_snapshot = snapshot_project(memory_home)
    assert replayed_snapshot.markdown == replayed_snapshot.database
    assert (
        replayed_snapshot.markdown[memory_id][2].count(SAVE_OPERATION_ID)
        == 1
    )


def test_multi_document_crash_before_journal_keeps_old_state(
    tmp_path: Path,
) -> None:
    memory_home = tmp_path / ".memory"
    canonical_id, source_ids = seed_three_historical_memories(memory_home)
    before = snapshot_project(memory_home)
    process = get_context("spawn").Process(
        target=crash_merge_worker,
        args=(
            str(memory_home),
            canonical_id,
            source_ids,
            "after_all_temps_fsync",
        ),
    )
    process.start()
    process.join(20)
    assert process.exitcode == 92

    after = snapshot_project(memory_home)
    assert after.markdown == before.markdown
    assert after.database == before.database
    assert after.journal_paths == ()
    assert any(
        item["code"] == "stale_prepared_file"
        for item in doctor_home(memory_home, project=PROJECT)["findings"]
    )


@pytest.mark.parametrize(
    ("phase", "has_pending_journal"),
    [
        ("after_journal_fsync", True),
        ("after_db_write", True),
        ("after_target_replace:0", True),
        ("after_target_replace:1", True),
        ("after_target_replace:2", True),
        ("after_db_commit", True),
        ("after_journal_remove", False),
    ],
)
def test_multi_document_merge_rolls_forward_after_every_replacement(
    tmp_path: Path,
    phase: str,
    has_pending_journal: bool,
) -> None:
    memory_home = tmp_path / ".memory"
    canonical_id, source_ids = seed_three_historical_memories(memory_home)
    process = get_context("spawn").Process(
        target=crash_merge_worker,
        args=(str(memory_home), canonical_id, source_ids, phase),
    )
    process.start()
    process.join(20)
    assert process.exitcode == 92
    assert bool(snapshot_project(memory_home).journal_paths) is has_pending_journal

    service = configured_service(str(memory_home))
    try:
        expected_recoveries = (
            (MERGE_OPERATION_ID,) if has_pending_journal else ()
        )
        assert service.persistence.startup_recoveries == expected_recoveries
    finally:
        service.close()

    snapshot = snapshot_project(memory_home)
    assert snapshot.journal_paths == ()
    assert snapshot.markdown == snapshot.database
    assert snapshot.markdown[canonical_id][0] == "canonical body"
    assert all(source_id in snapshot.markdown for source_id in source_ids)
    for path in sorted(
        (memory_home / "vault" / PROJECT).glob("*-session.md")
    ):
        for entry in parse_session_file(path).entries:
            memory = entry.to_memory(str(path))
            assert memory.content_fingerprint == content_fingerprint(memory)
    assert doctor_home(memory_home, project=PROJECT)["status"] == "ok"


def test_recovery_stops_when_a_target_matches_neither_digest(
    tmp_path: Path,
) -> None:
    memory_home = tmp_path / ".memory"
    canonical_id, source_ids = seed_three_historical_memories(memory_home)
    process = get_context("spawn").Process(
        target=crash_merge_worker,
        args=(
            str(memory_home),
            canonical_id,
            source_ids,
            "after_target_replace:0",
        ),
    )
    process.start()
    process.join(20)
    assert process.exitcode == 92

    journal_path = snapshot_project(memory_home).journal_paths[0]
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    conflicted = memory_home / journal["targets"][1]["target"]
    conflicted.write_text("externally changed\n", encoding="utf-8")
    before_db = snapshot_project(memory_home).database

    with pytest.raises(JournalRecoveryConflict):
        configured_service(str(memory_home))

    assert journal_path.exists()
    assert snapshot_project(memory_home).database == before_db
