import copy
from datetime import date
import hashlib
import json
from pathlib import Path

import pytest

from memory.core import MemoryService
from memory.markdown import parse_session_file
from memory.models import RawMemoryInput
from memory.persistence import (
    CanonicalDriftError,
    LegacyMetadataRequiredError,
    SaveConflict,
    content_fingerprint,
    embedding_text,
    request_fingerprint,
)


def _operation(service: MemoryService, memory_id: str) -> dict[str, object]:
    record = service.get_memory_record(memory_id)
    assert record is not None
    operations = json.loads(record["operation_history"])
    assert operations
    return operations[-1]


def _canonical_memory(saved: dict[str, object]):
    document = parse_session_file(Path(str(saved["file_path"])))
    return next(
        entry.to_memory(str(saved["file_path"]))
        for entry in document.entries
        if entry.id == saved["id"]
    )


def test_same_operation_and_payload_replays_without_second_update(
    service: MemoryService,
) -> None:
    raw = RawMemoryInput(
        title="Stable decision",
        what="Use one canonical writer",
        source="spoofed",
    )
    created = service.save(
        raw,
        project="project--111111111111",
        authoritative_source="cursor",
        idempotency_key="11111111-1111-4111-8111-111111111111",
    )
    replayed = service.save(
        raw,
        project="project--111111111111",
        authoritative_source="cursor",
        idempotency_key="11111111-1111-4111-8111-111111111111",
    )
    record = service.get_memory_record(str(created["id"]))
    assert record is not None
    assert replayed == {**created, "action": "replayed"}
    assert record["updated_count"] == 0
    assert json.loads(record["contributors"]) == ["cursor"]
    assert len(json.loads(record["operation_history"])) == 1


def test_reused_operation_with_different_payload_conflicts(
    service: MemoryService,
) -> None:
    key = "22222222-2222-4222-8222-222222222222"
    service.save(
        RawMemoryInput(title="One", what="first"),
        project="p--1",
        idempotency_key=key,
    )
    with pytest.raises(SaveConflict):
        service.save(
            RawMemoryInput(title="One", what="different"),
            project="p--1",
            idempotency_key=key,
        )


def test_duplicate_update_rewrites_historical_markdown(
    service: MemoryService,
) -> None:
    first = service.save(
        RawMemoryInput(title="Decision", what="old", tags=["one"]),
        project="p--1",
    )
    current_path = Path(str(first["file_path"]))
    historical_path = current_path.with_name("2020-01-02-session.md")
    current_path.replace(historical_path)
    with service.db.transaction():
        service.db.conn.execute(
            "UPDATE memories SET file_path = ? WHERE id = ?",
            (str(historical_path), first["id"]),
        )

    before_record = service.get_memory_record(str(first["id"]))
    assert before_record is not None
    before_fingerprint = before_record["content_fingerprint"]
    second = service.save(
        RawMemoryInput(title="Decision", what="new", tags=["two"]),
        project="p--1",
        authoritative_source="gemini-cli",
        idempotency_key="33333333-3333-4333-8333-333333333333",
    )
    assert second["id"] == first["id"]
    assert second["file_path"] == str(historical_path)
    assert not current_path.exists()
    canonical = _canonical_memory(second)
    assert canonical.what == "new"
    assert canonical.last_updated_by == "gemini-cli"
    record = service.get_memory_record(str(first["id"]))
    assert record is not None
    assert record["content_fingerprint"] != before_fingerprint
    assert canonical.content_fingerprint == record["content_fingerprint"]
    assert record["content_fingerprint"] == content_fingerprint(canonical)


def test_input_object_is_not_mutated_by_deep_redaction(
    service: MemoryService,
) -> None:
    secret = "sk_live_secret123"
    raw = RawMemoryInput(
        title=f"token {secret}",
        what=f"token {secret}",
        why=f"why {secret}",
        impact=f"impact {secret}",
        tags=[f"tag-{secret}"],
        category="context",
        related_files=[f"src/{secret}.py"],
        details=f"details {secret}",
        source=f"spoof-{secret}",
        triggers=[f"trigger-{secret}"],
        prerequisites=[f"prerequisite-{secret}"],
        steps=[f"step-{secret}"],
        verification=[f"verify-{secret}"],
        follow_ups=[f"follow-{secret}"],
        constraints=[f"constraint-{secret}"],
        alternatives_rejected=[f"alternative-{secret}"],
        open_questions=[f"question-{secret}"],
        valid_from=f"2026-{secret}",
        valid_until=f"2027-{secret}",
        commit_sha=f"commit-{secret}",
        branch=f"branch-{secret}",
        links=[f"https://x.test/?key={secret}"],
        last_verified=f"verified-{secret}",
    )
    original = copy.deepcopy(raw)
    saved = service.save(raw, project="p--1", authoritative_source="codex")

    assert raw == original
    markdown = Path(str(saved["file_path"])).read_text(encoding="utf-8")
    record = service.get_memory_record(str(saved["id"]))
    assert record is not None
    assert secret not in markdown
    assert secret not in json.dumps(record, sort_keys=True)
    assert record["source"] == "codex"


def test_request_fingerprint_hashes_exact_redacted_authoritative_request() -> None:
    secret = "sk_live_fingerprintsecret"
    raw = RawMemoryInput(
        title=f"Title {secret}",
        what=f"What {secret}",
        tags=[f"tag-{secret}"],
        source="spoofed",
        branch=f"feat/{secret}",
    )
    fingerprint = request_fingerprint(raw, "project", "codex")
    redacted = copy.deepcopy(raw)
    redacted.title = "Title [REDACTED]"
    redacted.what = "What [REDACTED]"
    redacted.tags = ["tag-[REDACTED]"]
    redacted.branch = "feat/[REDACTED]"
    redacted.source = "codex"
    payload = {
        "project": "project",
        "source": "codex",
        "raw": redacted.__dict__,
    }
    expected = "sha256:" + hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    assert secret not in json.dumps(payload, sort_keys=True)
    assert fingerprint == expected


def test_authoritative_source_cannot_be_spoofed(
    service: MemoryService,
) -> None:
    raw = RawMemoryInput(title="Source", what="Authoritative", source="spoofed")
    saved = service.save(
        raw,
        project="p--1",
        authoritative_source="codex",
        idempotency_key="44444444-4444-4444-8444-444444444444",
    )
    record = service.get_memory_record(str(saved["id"]))
    canonical = _canonical_memory(saved)
    assert record is not None
    assert record["source"] == "codex"
    assert record["creator_source"] == "codex"
    assert record["last_updated_by"] == "codex"
    assert json.loads(record["contributors"]) == ["codex"]
    assert _operation(service, str(saved["id"]))["source"] == "codex"
    assert canonical.source == "codex"


def test_write_targeting_v1_file_requires_explicit_migration(
    service: MemoryService,
) -> None:
    legacy_file = (
        Path(service.vault_dir)
        / "legacy"
        / f"{date.today().isoformat()}-session.md"
    )
    legacy_file.parent.mkdir(parents=True)
    legacy_file.write_text(
        "---\nproject: legacy\n---\n\n"
        "# Session\n\n### Legacy\n**What:** old\n",
        encoding="utf-8",
    )
    before = legacy_file.read_bytes()
    with pytest.raises(
        LegacyMetadataRequiredError,
        match="memory migrate vault-metadata",
    ):
        service.save(
            RawMemoryInput(title="New", what="must not rewrite v1"),
            project="legacy",
        )
    assert legacy_file.read_bytes() == before


def test_content_fingerprint_hashes_exact_embedding_bytes(
    service: MemoryService,
) -> None:
    saved = service.save(
        RawMemoryInput(
            title="Vector contract",
            what="stable bytes",
            why="recovery",
            impact="no stale vector",
            tags=["one", "two"],
        ),
        project="p--1",
    )
    memory = service.get_memory_record(str(saved["id"]))
    assert memory is not None
    text = embedding_text(
        title=memory["title"],
        what=memory["what"],
        why=memory["why"],
        impact=memory["impact"],
        tags=json.loads(memory["tags"]),
    )
    expected = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert memory["content_fingerprint"] == expected


def test_markdown_replay_repairs_missing_projection_and_ledger(
    service: MemoryService,
) -> None:
    operation_id = "55555555-5555-4555-8555-555555555555"
    raw = RawMemoryInput(title="Repair", what="Canonical markdown", details="body")
    created = service.save(raw, project="p--1", idempotency_key=operation_id)
    service.db.delete_memory(str(created["id"]))
    with service.db.transaction():
        service.db.conn.execute(
            "DELETE FROM save_operations WHERE project = ? AND operation_id = ?",
            ("p--1", operation_id),
        )

    replayed = service.save(raw, project="p--1", idempotency_key=operation_id)
    record = service.get_memory_record(str(created["id"]))
    assert replayed == {**created, "action": "replayed"}
    assert record is not None
    assert record["details"] == "body"
    assert service.db.get_operation("p--1", operation_id) is not None


def test_operation_existing_only_in_sqlite_is_canonical_drift(
    service: MemoryService,
) -> None:
    operation_id = "66666666-6666-4666-8666-666666666666"
    raw = RawMemoryInput(title="Drift", what="SQLite is derived")
    created = service.save(raw, project="p--1", idempotency_key=operation_id)
    Path(str(created["file_path"])).unlink()

    with pytest.raises(CanonicalDriftError):
        service.save(raw, project="p--1", idempotency_key=operation_id)


@pytest.mark.parametrize(
    ("phase", "markdown_expected", "db_expected"),
    [
        ("after_temp_fsync", False, False),
        ("after_db_write", False, False),
        ("after_markdown_replace", True, False),
        ("after_db_commit", True, True),
        ("before_vector_write", True, True),
    ],
)
def test_fault_boundaries_leave_recoverable_state(
    service: MemoryService,
    phase: str,
    markdown_expected: bool,
    db_expected: bool,
) -> None:
    operation_id = f"fault-{phase}"

    def inject(current: str) -> None:
        if current == phase:
            raise RuntimeError(f"fault at {phase}")

    service.persistence.fault = inject
    raw = RawMemoryInput(title=f"Fault {phase}", what="recoverable")
    with pytest.raises(RuntimeError, match=f"fault at {phase}"):
        service.save(raw, project="fault-project", idempotency_key=operation_id)

    files = list((Path(service.vault_dir) / "fault-project").glob("*-session.md"))
    assert bool(files) is markdown_expected
    operation = service.db.get_operation("fault-project", operation_id)
    assert (operation is not None) is db_expected
    assert list((Path(service.vault_dir) / "fault-project").glob("*.tmp")) == []

    service.persistence.fault = lambda _phase: None
    recovered = service.save(raw, project="fault-project", idempotency_key=operation_id)
    assert recovered["action"] == ("replayed" if markdown_expected else "created")
    assert service.db.get_operation("fault-project", operation_id) is not None


def test_duplicate_normalization_broadens_only_for_one_same_project_row(
    service: MemoryService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = service.save(RawMemoryInput(title="Conditional", what="first"), project="p--1")
    calls: list[str | None] = []
    original = service.db.fts_search

    def tracked(query: str, limit: int = 10, project: str | None = None, **kwargs):
        calls.append(project)
        rows = original(query, limit=limit, project=project, **kwargs)
        if project is None:
            return [
                {
                    "id": "foreign",
                    "project": "foreign",
                    "title": "Conditional",
                    "score": 0.0,
                },
                *rows,
            ]
        return rows

    monkeypatch.setattr(service.db, "fts_search", tracked)
    updated = service.save(
        RawMemoryInput(title="Conditional", what="third"),
        project="p--1",
    )
    assert updated["id"] == first["id"]
    assert calls[0] == "p--1"
    assert calls.count(None) == 1

    calls.clear()
    monkeypatch.setattr(service.db, "fts_search", original)
    service.save(
        RawMemoryInput(title="Conditional extra", what="second"),
        project="p--1",
    )
    monkeypatch.setattr(
        service.db,
        "fts_search",
        lambda query, limit=10, project=None, **kwargs: (
            calls.append(project)
            or [
                {"id": "one", "project": "p--1", "title": "Other", "score": 1.0},
                {"id": "two", "project": "p--1", "title": "Other", "score": 0.9},
            ]
        ),
    )
    service.save(RawMemoryInput(title="Conditional", what="fourth"), project="p--1")
    assert calls == ["p--1"]


def test_foreign_rows_only_influence_normalization_denominator(
    service: MemoryService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = service.save(
        RawMemoryInput(title="Foreign denominator", what="first"),
        project="local",
    )
    local = {
        "id": existing["id"],
        "project": "local",
        "title": "Foreign denominator",
        "file_path": existing["file_path"],
        "score": 1.0,
    }
    foreign = {
        "id": "foreign-id",
        "project": "foreign",
        "title": "Foreign denominator",
        "file_path": "/foreign.md",
        "score": 10.0,
    }
    calls = 0

    def search(*args, project=None, **kwargs):
        nonlocal calls
        calls += 1
        return [local] if project == "local" else [foreign, local]

    monkeypatch.setattr(service.db, "fts_search", search)
    created = service.save(
        RawMemoryInput(title="Foreign denominator", what="second"),
        project="local",
    )
    assert calls == 2
    assert created["action"] == "created"
    assert created["id"] != existing["id"]


def test_stale_embedding_cannot_overwrite_newer_vector(
    service: MemoryService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Vector race", what="old"),
        project="p--1",
    )
    old_fingerprint = service.get_memory_record(str(saved["id"]))["content_fingerprint"]
    service.save(RawMemoryInput(title="Vector race", what="new"), project="p--1")
    current = service.get_memory_record(str(saved["id"]))
    assert current is not None
    assert current["content_fingerprint"] != old_fingerprint
    assert service.db.upsert_vector_if_current(
        str(saved["id"]), str(old_fingerprint), [0.1, 0.2]
    ) is False
