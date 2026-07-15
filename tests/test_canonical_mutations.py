from __future__ import annotations

import json
from pathlib import Path
import uuid

import memory.persistence as persistence
import pytest

from memory.core import MemoryService
from memory.health import doctor
from memory.markdown import parse_session_file
from memory.models import RawMemoryInput
from memory.persistence import SaveRequest
from memory.projects import ProjectIdentity, ProjectRegistry
from memory.safe_io import prepare_atomic_text


def test_delete_removes_markdown_and_derived_rows(service: MemoryService) -> None:
    saved = service.save(RawMemoryInput(title="Delete me", what="gone"), project="p--1")

    assert service.delete(str(saved["id"])) is True
    assert str(saved["id"]) not in Path(str(saved["file_path"])).read_text(
        encoding="utf-8"
    )
    assert service.get_memory_record(str(saved["id"])) is None


def test_archive_and_restore_update_both_stores(service: MemoryService) -> None:
    saved = service.save(
        RawMemoryInput(title="Lifecycle", what="state"), project="p--1"
    )

    service.archive_memory(str(saved["id"]), reason="reviewed")
    archived_document = parse_session_file(Path(str(saved["file_path"])))
    assert next(
        entry for entry in archived_document.entries if entry.id == saved["id"]
    ).status == "archived"
    archived = service.get_memory_record(str(saved["id"]))
    assert archived is not None
    assert archived["status"] == "archived"

    service.restore_memory(str(saved["id"]))
    restored = service.get_memory_record(str(saved["id"]))
    assert restored is not None
    assert restored["status"] == "active"
    restored_document = parse_session_file(Path(str(saved["file_path"])))
    assert next(
        entry for entry in restored_document.entries if entry.id == saved["id"]
    ).status == "active"


def test_admin_patch_distinguishes_omitted_clear_and_replace(
    service: MemoryService,
) -> None:
    saved = service.save(
        RawMemoryInput(
            title="Patch",
            what="old",
            why="keep",
            impact="clear",
            tags=["old"],
            source="creator",
        ),
        project="p--1",
    )

    service.update_memory_record(
        str(saved["id"]),
        patch=persistence.MemoryPatch(
            title="Renamed",
            what="new",
            impact=None,
            tags=[],
        ),
        actor="dashboard",
    )

    record = service.get_memory_record(str(saved["id"]))
    assert record is not None
    assert record["title"] == "Renamed"
    assert record["why"] == "keep"
    assert record["impact"] is None
    assert json.loads(record["tags"]) == []
    assert record["source"] == record["creator_source"] == "creator"
    assert record["last_updated_by"] == "dashboard"
    assert json.loads(record["contributors"]) == ["creator", "dashboard"]


def test_admin_update_recomputes_exact_fingerprint_before_both_writes(
    service: MemoryService,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Patch", what="old", tags=["one"]), project="p--1"
    )
    before = service.get_memory_record(str(saved["id"]))
    assert before is not None

    service.update_memory_record(
        str(saved["id"]),
        patch=persistence.MemoryPatch(what="new", tags=["two"]),
        actor="dashboard",
    )

    row = service.get_memory_record(str(saved["id"]))
    assert row is not None
    entry = next(
        entry
        for entry in parse_session_file(Path(str(saved["file_path"]))).entries
        if entry.id == saved["id"]
    )
    rebuilt = entry.to_memory(str(saved["file_path"]))
    assert row["content_fingerprint"] != before["content_fingerprint"]
    assert row["content_fingerprint"] == entry.metadata["content_fingerprint"]
    assert row["content_fingerprint"] == persistence.content_fingerprint(rebuilt)


def test_operation_journal_is_durable_and_contains_no_memory_text(
    tmp_path: Path,
) -> None:
    memory_home = tmp_path / ".memory"
    targets = []
    for index in range(3):
        target = (
            memory_home
            / "vault"
            / "p--1"
            / f"2026-07-{index + 10}-session.md"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"old-{index}\n", encoding="utf-8")
        prepared = prepare_atomic_text(target, f"new-{index}\n")
        targets.append(persistence.JournalTarget.from_prepared(memory_home, prepared))

    operation = persistence.OperationJournal(
        schema_version=1,
        operation_id="55555555-5555-4555-8555-555555555555",
        action="merge",
        project_keys=("p--1",),
        affected_memory_ids=(
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
        ),
        canonical_memory_id="11111111-1111-4111-8111-111111111111",
        targets=tuple(targets),
        created_at="2026-07-14T10:00:00+00:00",
    )

    journal_path = persistence.persist_operation_journal(memory_home, operation)
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["action"] == "merge"
    assert journal["affected_memory_ids"] == [
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
    ]
    assert len(journal["targets"]) == 3
    assert all(
        set(item) == {"target", "temporary", "before_sha256", "after_sha256"}
        for item in journal["targets"]
    )
    serialized = json.dumps(journal)
    assert "canonical body" not in serialized
    assert "source body" not in serialized
    assert [
        target.read_text(encoding="utf-8")
        for target in sorted((memory_home / "vault" / "p--1").glob("*.md"))
    ] == ["old-0\n", "old-1\n", "old-2\n"]


def test_delete_last_entry_leaves_valid_empty_v2_document(
    service: MemoryService,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Only entry", what="delete safely"),
        project="p--1",
    )

    assert service.delete(str(saved["id"])) is True
    document = parse_session_file(Path(str(saved["file_path"])))
    assert document.schema_version == 2
    assert document.project == "p--1"
    assert document.entries == []


def test_update_preserves_creator_and_redacts_before_fingerprinting(
    service: MemoryService,
) -> None:
    saved = service.save(
        RawMemoryInput(
            title="Immutable provenance",
            what="old",
            source="creator",
        ),
        project="p--1",
    )

    service.update_memory_record(
        str(saved["id"]),
        patch=persistence.MemoryPatch(what="new sk_live_secret123"),
        actor="dashboard",
    )

    record = service.get_memory_record(str(saved["id"]))
    assert record is not None
    assert record["source"] == record["creator_source"] == "creator"
    assert record["last_updated_by"] == "dashboard"
    assert "sk_live_secret123" not in record["what"]
    entry = parse_session_file(Path(str(saved["file_path"]))).entries[0]
    assert entry.to_memory(str(saved["file_path"])).content_fingerprint == record[
        "content_fingerprint"
    ]


def test_category_and_details_only_patch_preserves_current_vector(
    service: MemoryService,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Vector", what="same content"),
        project="p--1",
    )
    assert service.db.has_vector(str(saved["id"])) is True

    def unexpected_embed(_text: str) -> list[float]:
        raise AssertionError("unchanged embedding content must not be embedded")

    service.persistence.embed = unexpected_embed
    result = service.update_memory_record(
        str(saved["id"]),
        patch=persistence.MemoryPatch(category="context", details="metadata only"),
        actor="dashboard",
    )

    assert result["vector_status"] == "ready"
    assert service.db.has_vector(str(saved["id"])) is True


def test_archive_invalidates_and_restore_rebuilds_vector(
    service: MemoryService,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Lifecycle vector", what="semantic body"),
        project="p--1",
    )
    assert service.db.has_vector(str(saved["id"])) is True

    service.archive_memory(str(saved["id"]), actor="dashboard")
    assert service.db.has_vector(str(saved["id"])) is False

    restored = service.restore_memory(str(saved["id"]), actor="dashboard")
    assert restored["vector_status"] == "ready"
    assert service.db.has_vector(str(saved["id"])) is True


def test_merge_archives_sources_and_rejects_cross_project_before_writes(
    service: MemoryService,
) -> None:
    canonical = service.save(
        RawMemoryInput(title="Canonical", what="base", tags=["one"]),
        project="p--1",
    )
    source = service.save(
        RawMemoryInput(title="Source", what="extra", tags=["two"]),
        project="p--1",
    )
    foreign = service.save(
        RawMemoryInput(title="Foreign", what="outside"),
        project="p--2",
    )
    before = Path(str(canonical["file_path"])).read_bytes()
    with pytest.raises(ValueError, match="Cross-project"):
        service.merge_memories(
            str(canonical["id"]),
            [str(foreign["id"])],
            actor="dashboard",
        )
    assert Path(str(canonical["file_path"])).read_bytes() == before
    assert not list((Path(service.memory_home) / "transactions").glob("*.json"))

    merged = service.merge_memories(
        str(canonical["id"]),
        [str(source["id"])],
        actor="dashboard",
    )
    assert merged["id"] == canonical["id"]
    canonical_row = service.get_memory_record(str(canonical["id"]))
    source_row = service.get_memory_record(str(source["id"]))
    assert canonical_row is not None and source_row is not None
    assert json.loads(canonical_row["tags"]) == ["one", "two"]
    assert source_row["status"] == "archived"
    assert source_row["superseded_by"] == canonical["id"]
    assert service.db.has_vector(str(source["id"])) is False


def test_collateral_anchor_projection_is_rebuilt_after_delete(
    service: MemoryService,
) -> None:
    first = service.save(
        RawMemoryInput(title="First title", what="one"),
        project="p--1",
    )
    second = service.save(
        RawMemoryInput(title="Second title", what="two"),
        project="p--1",
    )
    service.update_memory_record(
        str(second["id"]),
        patch=persistence.MemoryPatch(title="First title"),
        actor="dashboard",
    )
    before = service.get_memory_record(str(second["id"]))
    assert before is not None and before["section_anchor"] == "first-title-2"

    service.delete(str(first["id"]))
    after = service.get_memory_record(str(second["id"]))
    assert after is not None and after["section_anchor"] == "first-title"


@pytest.mark.parametrize(
    "phase",
    [
        "after_journal_fsync",
        "after_db_write",
        "after_target_replace:0",
        "after_db_commit",
    ],
)
def test_startup_recovery_rolls_forward_each_durable_crash_boundary(
    service: MemoryService,
    phase: str,
) -> None:
    saved = service.save(
        RawMemoryInput(title=f"Crash {phase}", what="old"),
        project="p--1",
    )

    def inject(current: str) -> None:
        if current == phase:
            raise RuntimeError(f"crash at {phase}")

    service.persistence.fault = inject
    with pytest.raises(RuntimeError, match="crash at"):
        service.update_memory_record(
            str(saved["id"]),
            patch=persistence.MemoryPatch(what="new"),
            actor="dashboard",
        )
    journals = list((Path(service.memory_home) / "transactions").glob("*.json"))
    assert len(journals) == 1

    recovered = MemoryService(str(service.memory_home))
    try:
        assert recovered.persistence.startup_recoveries == (journals[0].stem,)
        row = recovered.get_memory_record(str(saved["id"]))
        assert row is not None and row["what"] == "new"
        assert not journals[0].exists()
        assert recovered.db.has_vector(str(saved["id"])) is True
    finally:
        recovered.close()


def test_recovery_preflights_all_targets_before_replacing_any(
    tmp_path: Path,
) -> None:
    memory_home = tmp_path / ".memory"
    db_service = MemoryService(str(memory_home))
    try:
        first = db_service.persistence.save(
            SaveRequest(
                raw=RawMemoryInput(title="First target", what="canonical"),
                project="p--1",
                source="codex",
                operation_id="11111111-1111-4111-8111-111111111111",
                timestamp="2026-07-10T10:00:00+00:00",
            )
        )
        second = db_service.persistence.save(
            SaveRequest(
                raw=RawMemoryInput(title="Second target", what="source"),
                project="p--1",
                source="cursor",
                operation_id="22222222-2222-4222-8222-222222222222",
                timestamp="2026-07-11T10:00:00+00:00",
            )
        )
        targets = (Path(str(first["file_path"])), Path(str(second["file_path"])))
        before = {path: path.read_bytes() for path in targets}

        def inject(phase: str) -> None:
            if phase == "after_journal_fsync":
                raise RuntimeError("leave multi-target journal")

        db_service.persistence.fault = inject
        with pytest.raises(RuntimeError, match="leave multi-target journal"):
            db_service.merge_memories(
                str(first["id"]),
                [str(second["id"])],
                actor="dashboard",
            )
        db_service.persistence.fault = lambda _phase: None
        journal_path = next((memory_home / "transactions").glob("*.json"))
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        corrupt = memory_home / journal["targets"][1]["temporary"]
        corrupt.write_text("corrupt\n", encoding="utf-8")

        with pytest.raises(persistence.JournalRecoveryConflict):
            db_service.persistence.recover_pending_operations(("p--1",))
        assert journal_path.exists()
        assert {path: path.read_bytes() for path in targets} == before
    finally:
        db_service.close()


def test_acquire_project_locks_sorts_deduplicates_and_releases_reverse(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class FakeLock:
        def __init__(self, path: Path):
            self.path = path

        def __enter__(self):
            events.append("enter:" + self.path.name)
            return self

        def __exit__(self, *_args):
            events.append("exit:" + self.path.name)

    with persistence.acquire_project_locks(
        tmp_path,
        ("zeta", "alpha", "zeta"),
        lock_factory=FakeLock,
    ):
        events.append("body")

    assert events == [
        "enter:alpha.lock",
        "enter:zeta.lock",
        "body",
        "exit:zeta.lock",
        "exit:alpha.lock",
    ]


@pytest.mark.parametrize(
    "bad_target",
    ["index.db", "/tmp/outside", "vault/../index.db", "vault/p--2/x-session.md"],
)
def test_journal_loader_rejects_non_project_targets(
    tmp_path: Path,
    bad_target: str,
) -> None:
    memory_home = tmp_path / ".memory"
    transactions = memory_home / "transactions"
    transactions.mkdir(parents=True)
    operation_id = str(uuid.uuid4())
    journal_path = transactions / f"{operation_id}.json"
    journal_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": operation_id,
                "action": "update",
                "project_keys": ["p--1"],
                "affected_memory_ids": ["memory-id"],
                "canonical_memory_id": "memory-id",
                "targets": [
                    {
                        "target": bad_target,
                        "temporary": "vault/p--1/.x-session.md.abc.tmp",
                        "before_sha256": None,
                        "after_sha256": "0" * 64,
                    }
                ],
                "created_at": "2026-07-14T10:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(persistence.JournalRecoveryConflict):
        persistence.load_operation_journal(memory_home, journal_path)


def test_doctor_reports_unrecoverable_journal_without_recovery_or_writes(
    tmp_path: Path,
) -> None:
    memory_home = tmp_path / ".memory"
    target = memory_home / "vault" / "p--1" / "2026-07-14-session.md"
    target.parent.mkdir(parents=True)
    target.write_text("old\n", encoding="utf-8")
    prepared = prepare_atomic_text(target, "new\n")
    operation = persistence.OperationJournal(
        schema_version=1,
        operation_id=str(uuid.uuid4()),
        action="update",
        project_keys=("p--1",),
        affected_memory_ids=("44444444-4444-4444-8444-444444444444",),
        canonical_memory_id="44444444-4444-4444-8444-444444444444",
        targets=(persistence.JournalTarget.from_prepared(memory_home, prepared),),
        created_at="2026-07-14T10:00:00+00:00",
    )
    journal_path = persistence.persist_operation_journal(memory_home, operation)
    service = MemoryService(str(memory_home), recover_pending=False)
    try:
        before_target = target.read_bytes()
        before_journal = journal_path.read_bytes()
        before_changes = service.db.conn.total_changes
        report = doctor(service)
        assert report["operation_journals"] == [
            {
                "type": "journal_recovery_conflict",
                "operation_id": operation.operation_id,
            }
        ]
        assert target.read_bytes() == before_target
        assert journal_path.read_bytes() == before_journal
        assert service.db.conn.total_changes == before_changes
    finally:
        service.close()


def test_doctor_reports_journal_conflict_without_leaking_payload(
    tmp_path: Path,
) -> None:
    memory_home = tmp_path / ".memory"
    transactions = memory_home / "transactions"
    transactions.mkdir(parents=True)
    journal_path = transactions / f"{uuid.uuid4()}.json"
    journal_path.write_text('{"secret":"sk_live_do_not_report"}', encoding="utf-8")
    service = MemoryService(str(memory_home), recover_pending=False)
    try:
        report = doctor(service)
        assert report["operation_journals"] == [
            {
                "type": "journal_recovery_conflict",
                "operation_id": journal_path.stem,
            }
        ]
        assert "sk_live_do_not_report" not in json.dumps(report)
        assert journal_path.exists()
    finally:
        service.close()


def test_save_recovers_intersecting_mutation_journal_before_writing(
    service: MemoryService,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Pending update", what="old"),
        project="p--1",
    )

    def inject(phase: str) -> None:
        if phase == "after_journal_fsync":
            raise RuntimeError("durable pending journal")

    service.persistence.fault = inject
    with pytest.raises(RuntimeError, match="durable pending"):
        service.update_memory_record(
            str(saved["id"]),
            patch=persistence.MemoryPatch(what="recovered"),
            actor="dashboard",
        )
    service.persistence.fault = lambda _phase: None

    service.save(
        RawMemoryInput(title="Following save", what="must serialize"),
        project="p--1",
    )
    row = service.get_memory_record(str(saved["id"]))
    assert row is not None and row["what"] == "recovered"
    assert not list((Path(service.memory_home) / "transactions").glob("*.json"))


def test_journal_loader_rejects_boolean_schema_and_symlinked_journal(
    tmp_path: Path,
) -> None:
    memory_home = tmp_path / ".memory"
    transactions = memory_home / "transactions"
    transactions.mkdir(parents=True)
    operation_id = str(uuid.uuid4())
    payload = {
        "schema_version": True,
        "operation_id": operation_id,
        "action": "update",
        "project_keys": ["p--1"],
        "affected_memory_ids": ["memory-id"],
        "canonical_memory_id": "memory-id",
        "targets": [
            {
                "target": "vault/p--1/2026-07-14-session.md",
                "temporary": "vault/p--1/.2026-07-14-session.md.x.tmp",
                "before_sha256": None,
                "after_sha256": "0" * 64,
            }
        ],
        "created_at": "2026-07-14T10:00:00+00:00",
    }
    journal_path = transactions / f"{operation_id}.json"
    journal_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(persistence.JournalRecoveryConflict):
        persistence.load_operation_journal(memory_home, journal_path)

    payload["schema_version"] = 1
    external = tmp_path / "external.json"
    external.write_text(json.dumps(payload), encoding="utf-8")
    journal_path.unlink()
    try:
        journal_path.symlink_to(external)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(persistence.JournalRecoveryConflict):
        persistence.load_operation_journal(memory_home, journal_path)


def test_mutation_of_adopted_alias_document_uses_canonical_lock_and_journal_scope(
    service: MemoryService,
    tmp_path: Path,
) -> None:
    alias = "legacy"
    canonical = "project--111111111111"
    saved = service.save(
        RawMemoryInput(title="Alias mutation", what="old"),
        project=alias,
    )
    identity = ProjectIdentity(
        root=tmp_path.resolve(),
        display_name="Project",
        key=canonical,
        marker=None,
        canonical_owner=tmp_path.resolve(),
    )
    ProjectRegistry(Path(service.memory_home)).adopt_legacy(alias, identity)

    result = service.update_memory_record(
        str(saved["id"]),
        patch=persistence.MemoryPatch(what="new"),
        actor="dashboard",
    )

    row = service.get_memory_record(str(saved["id"]))
    assert row is not None
    assert row["project"] == canonical
    assert row["what"] == "new"
    assert Path(str(result["file_path"])).parent.name == alias
    assert not list((Path(service.memory_home) / "transactions").glob("*.json"))
