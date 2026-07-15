import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

import pytest

from memory.core import MemoryService
from memory.markdown import parse_session_file
from memory.models import Memory, RawMemoryInput
from memory.persistence import (
    CanonicalDriftError,
    LegacyMetadataRequiredError,
    SaveRequest,
    SaveConflict,
    content_fingerprint,
    embedding_text,
    request_fingerprint,
)
from memory.projects import ProjectIdentity, ProjectRegistry, ProjectResolutionError


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


def _adopt_alias(
    service: MemoryService,
    tmp_path: Path,
    *,
    canonical: str = "project--111111111111",
    alias: str = "legacy",
) -> None:
    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    identity = ProjectIdentity(root, "workspace", canonical, None, root)
    ProjectRegistry(Path(service.memory_home)).adopt_legacy(alias, identity)


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
        / f"{datetime.now(timezone.utc).date().isoformat()}-session.md"
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
    (
        "phase",
        "markdown_expected",
        "db_expected",
        "journal_expected",
        "temporary_expected",
    ),
    [
        ("after_all_temps_fsync", False, False, False, True),
        ("after_journal_fsync", False, False, True, True),
        ("after_db_write", False, False, True, True),
        ("after_target_replace:0", True, False, True, False),
        ("after_db_commit", True, True, True, False),
        ("after_journal_remove", True, True, False, False),
        ("before_vector_write", True, True, False, False),
    ],
)
def test_fault_boundaries_leave_recoverable_state(
    service: MemoryService,
    phase: str,
    markdown_expected: bool,
    db_expected: bool,
    journal_expected: bool,
    temporary_expected: bool,
) -> None:
    operation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"fault:{phase}"))

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
    temporaries = list(
        (Path(service.vault_dir) / "fault-project").glob(".*.tmp")
    )
    assert bool(temporaries) is temporary_expected
    journals = list((Path(service.memory_home) / "transactions").glob("*.json"))
    assert bool(journals) is journal_expected

    service.persistence.fault = lambda _phase: None
    recovered = service.save(raw, project="fault-project", idempotency_key=operation_id)
    assert recovered["action"] == (
        "created" if phase == "after_all_temps_fsync" else "replayed"
    )
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


@pytest.mark.parametrize(
    "project",
    ["", "   ", ".", "..", "../outside", "nested/name", "nested\\name", "/absolute"],
)
def test_unsafe_project_key_is_rejected_before_any_write(
    service: MemoryService,
    project: str,
) -> None:
    with pytest.raises(ProjectResolutionError, match="storage key"):
        service.save(
            RawMemoryInput(title="Unsafe", what="must not persist"),
            project=project,
            idempotency_key="70000000-0000-4000-8000-000000000001",
        )

    assert service.db.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert list(Path(service.vault_dir).rglob("*-session.md")) == []
    locks = Path(service.memory_home) / "locks"
    assert not locks.exists() or list(locks.iterdir()) == []


@pytest.mark.parametrize("escape_kind", ["locks", "project", "session"])
def test_symlink_cannot_redirect_persistence_outside_memory_home(
    service: MemoryService,
    tmp_path: Path,
    escape_kind: str,
) -> None:
    memory_home = Path(service.memory_home)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-{escape_kind}"
    outside.mkdir()
    request = SaveRequest(
        raw=RawMemoryInput(title="Contained", what="stay inside"),
        project="safe-project",
        source="codex",
        operation_id="70000000-0000-4000-8000-000000000002",
        timestamp="2026-01-23T00:30:00+00:00",
    )

    try:
        if escape_kind == "locks":
            (memory_home / "locks").symlink_to(outside, target_is_directory=True)
        elif escape_kind == "project":
            (memory_home / "vault" / "safe-project").symlink_to(
                outside,
                target_is_directory=True,
            )
        else:
            project_dir = memory_home / "vault" / "safe-project"
            project_dir.mkdir()
            (project_dir / "2026-01-23-session.md").symlink_to(
                outside / "escaped.md"
            )
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are not supported on this platform")

    with pytest.raises(ProjectResolutionError, match="outside|contain"):
        service.persistence.save(request)

    assert list(outside.iterdir()) == []
    assert service.db.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


def test_invalid_operation_uuid_is_rejected_before_any_write(
    service: MemoryService,
) -> None:
    with pytest.raises(ValueError, match="UUID"):
        service.save(
            RawMemoryInput(title="Invalid operation", what="no write"),
            project="safe-project",
            idempotency_key="not-a-uuid",
        )

    assert service.db.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert list(Path(service.vault_dir).rglob("*-session.md")) == []
    assert not (Path(service.memory_home) / "locks").exists()


def test_operation_uuid_is_canonicalized_before_fingerprinting_and_storage(
    service: MemoryService,
) -> None:
    uppercase = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
    raw = RawMemoryInput(title="Canonical UUID", what="one operation")

    created = service.save(raw, project="safe-project", idempotency_key=uppercase)
    replayed = service.save(
        raw,
        project="safe-project",
        idempotency_key=uppercase.lower(),
    )

    assert replayed["id"] == created["id"]
    assert replayed["action"] == "replayed"
    operation = service.db.get_operation("safe-project", uppercase.lower())
    assert operation is not None
    assert operation["operation_id"] == uppercase.lower()
    assert _operation(service, str(created["id"]))["operation_id"] == uppercase.lower()


def test_alias_save_uses_canonical_project_for_all_new_state(
    service: MemoryService,
    tmp_path: Path,
) -> None:
    canonical = "project--111111111111"
    _adopt_alias(service, tmp_path, canonical=canonical, alias="legacy")

    saved = service.save(
        RawMemoryInput(title="Canonical scope", what="alias input"),
        project="legacy",
        idempotency_key="70000000-0000-4000-8000-000000000003",
    )
    record = service.get_memory_record(str(saved["id"]))

    assert record is not None
    assert record["project"] == canonical
    assert Path(str(saved["file_path"])).parent.name == canonical
    assert service.db.get_operation(
        canonical,
        "70000000-0000-4000-8000-000000000003",
    ) is not None
    assert service.db.get_operation(
        "legacy",
        "70000000-0000-4000-8000-000000000003",
    ) is None


def test_replay_scans_every_alias_directory_under_one_canonical_scope(
    service: MemoryService,
    tmp_path: Path,
) -> None:
    canonical = "project--111111111111"
    operation_id = "70000000-0000-4000-8000-000000000004"
    raw = RawMemoryInput(title="Alias replay", what="canonical request")
    created = service.save(raw, project=canonical, idempotency_key=operation_id)
    canonical_path = Path(str(created["file_path"]))
    alias_path = canonical_path.parent.parent / "legacy" / canonical_path.name
    alias_path.parent.mkdir()
    canonical_path.replace(alias_path)
    _adopt_alias(service, tmp_path, canonical=canonical, alias="legacy")

    replayed = service.save(raw, project="legacy", idempotency_key=operation_id)

    assert replayed["id"] == created["id"]
    assert replayed["action"] == "replayed"
    assert replayed["file_path"] == str(alias_path)
    record = service.get_memory_record(str(created["id"]))
    assert record is not None
    assert record["project"] == canonical
    assert record["file_path"] == str(alias_path)


def test_duplicate_operation_across_scope_files_is_rejected_as_ambiguous(
    service: MemoryService,
    tmp_path: Path,
) -> None:
    canonical = "project--111111111111"
    operation_id = "70000000-0000-4000-8000-000000000005"
    raw = RawMemoryInput(title="Ambiguous operation", what="same operation twice")
    saved = service.save(raw, project=canonical, idempotency_key=operation_id)
    canonical_path = Path(str(saved["file_path"]))
    alias_path = canonical_path.parent.parent / "legacy" / canonical_path.name
    alias_path.parent.mkdir()
    alias_path.write_bytes(canonical_path.read_bytes())
    _adopt_alias(service, tmp_path, canonical=canonical, alias="legacy")
    before = canonical_path.read_bytes(), alias_path.read_bytes()

    with pytest.raises(CanonicalDriftError, match="ambiguous|multiple"):
        service.save(raw, project=canonical, idempotency_key=operation_id)

    assert (canonical_path.read_bytes(), alias_path.read_bytes()) == before


def test_duplicate_memory_id_across_scope_files_is_rejected_as_ambiguous(
    service: MemoryService,
    tmp_path: Path,
) -> None:
    canonical = "project--111111111111"
    saved = service.save(
        RawMemoryInput(title="Ambiguous memory", what="old"),
        project=canonical,
        idempotency_key="70000000-0000-4000-8000-000000000006",
    )
    canonical_path = Path(str(saved["file_path"]))
    alias_path = canonical_path.parent.parent / "legacy" / canonical_path.name
    alias_path.parent.mkdir()
    alias_path.write_bytes(canonical_path.read_bytes())
    _adopt_alias(service, tmp_path, canonical=canonical, alias="legacy")
    before = canonical_path.read_bytes(), alias_path.read_bytes()

    with pytest.raises(CanonicalDriftError, match="ambiguous|multiple"):
        service.save(
            RawMemoryInput(title="Ambiguous memory", what="new"),
            project=canonical,
            idempotency_key="70000000-0000-4000-8000-000000000007",
        )

    assert (canonical_path.read_bytes(), alias_path.read_bytes()) == before


@pytest.mark.parametrize("indexed_path", ["", "outside"])
def test_duplicate_rewrite_uses_canonical_scan_not_indexed_file_path(
    service: MemoryService,
    tmp_path: Path,
    indexed_path: str,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Indexed path", what="old"),
        project="safe-project",
        idempotency_key="70000000-0000-4000-8000-000000000008",
    )
    canonical_path = Path(str(saved["file_path"]))
    outside = tmp_path / "outside.md"
    outside.write_text("do not rewrite\n", encoding="utf-8")
    untrusted = "" if indexed_path == "" else str(outside)
    with service.db.transaction():
        service.db.conn.execute(
            "UPDATE memories SET file_path = ? WHERE id = ?",
            (untrusted, saved["id"]),
        )
    before_outside = outside.read_bytes()

    updated = service.save(
        RawMemoryInput(title="Indexed path", what="new"),
        project="safe-project",
        idempotency_key="70000000-0000-4000-8000-000000000009",
    )

    assert updated["id"] == saved["id"]
    assert updated["file_path"] == str(canonical_path)
    assert _canonical_memory(updated).what == "new"
    assert outside.read_bytes() == before_outside


def test_embedding_failure_returns_redacted_degraded_status(
    service: MemoryService,
) -> None:
    secret = "provider-secret-token"

    def fail(_text: str) -> list[float]:
        raise RuntimeError(secret)

    service.persistence.embed = fail
    saved = service.save(
        RawMemoryInput(title="Degraded vector", what="canonical save succeeds"),
        project="safe-project",
        idempotency_key="70000000-0000-4000-8000-000000000010",
    )

    assert saved["vector_status"] == "degraded"
    assert saved["warning"] == "Memory saved, but semantic indexing is temporarily unavailable."
    assert saved["warning"] in saved["warnings"]
    assert secret not in json.dumps(saved, sort_keys=True)
    assert service.get_memory_record(str(saved["id"])) is not None
    assert service.db.has_vector(str(saved["id"])) is False


def test_failed_replay_embedding_preserves_existing_current_vector(
    service: MemoryService,
) -> None:
    operation_id = "70000000-0000-4000-8000-000000000011"
    raw = RawMemoryInput(title="Preserve vector", what="unchanged replay")
    created = service.save(raw, project="safe-project", idempotency_key=operation_id)
    assert created["vector_status"] == "ready"
    assert service.db.has_vector(str(created["id"])) is True

    service.persistence.embed = lambda _text: (_ for _ in ()).throw(
        RuntimeError("provider unavailable")
    )
    replayed = service.save(raw, project="safe-project", idempotency_key=operation_id)

    assert replayed["action"] == "replayed"
    assert replayed["vector_status"] == "degraded"
    assert service.db.has_vector(str(created["id"])) is True


def test_replay_repairs_vector_after_provider_recovers(
    service: MemoryService,
) -> None:
    operation_id = "70000000-0000-4000-8000-000000000012"
    raw = RawMemoryInput(title="Recover vector", what="retry same operation")
    healthy_embed = service.persistence.embed
    service.persistence.embed = lambda _text: (_ for _ in ()).throw(
        RuntimeError("provider unavailable")
    )
    created = service.save(raw, project="safe-project", idempotency_key=operation_id)
    assert created["vector_status"] == "degraded"
    assert service.db.has_vector(str(created["id"])) is False

    service.persistence.embed = healthy_embed
    replayed = service.save(raw, project="safe-project", idempotency_key=operation_id)

    assert replayed["action"] == "replayed"
    assert replayed["vector_status"] == "ready"
    assert service.db.has_vector(str(created["id"])) is True
    assert len(json.loads(service.get_memory_record(str(created["id"]))["operation_history"])) == 1


def test_session_filename_uses_captured_request_timestamp(
    service: MemoryService,
) -> None:
    saved = service.persistence.save(
        SaveRequest(
            raw=RawMemoryInput(title="Captured date", what="stable across midnight"),
            project="safe-project",
            source="codex",
            operation_id="70000000-0000-4000-8000-000000000013",
            timestamp="2025-12-31T23:59:59.999999+00:00",
        )
    )

    assert Path(str(saved["file_path"])).name == "2025-12-31-session.md"


def test_same_home_project_symlink_cannot_alias_another_storage_key(
    service: MemoryService,
) -> None:
    memory_home = Path(service.memory_home)
    target = memory_home / "vault" / "other-project"
    target.mkdir()
    sentinel = target / "sentinel.txt"
    sentinel.write_text("unchanged\n", encoding="utf-8")
    try:
        (memory_home / "vault" / "safe-project").symlink_to(
            target,
            target_is_directory=True,
        )
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are not supported on this platform")

    with pytest.raises(ProjectResolutionError, match="identity|exact"):
        service.save(
            RawMemoryInput(title="Cross project", what="must not write"),
            project="safe-project",
            idempotency_key="71000000-0000-4000-8000-000000000001",
        )

    assert sentinel.read_text(encoding="utf-8") == "unchanged\n"
    assert list(target.glob("*-session.md")) == []
    assert not (memory_home / "locks").exists()


def test_alias_directory_symlink_to_canonical_directory_is_rejected_before_lock(
    service: MemoryService,
    tmp_path: Path,
) -> None:
    memory_home = Path(service.memory_home)
    canonical = "project--111111111111"
    canonical_dir = memory_home / "vault" / canonical
    canonical_dir.mkdir()
    sentinel = canonical_dir / "sentinel.txt"
    sentinel.write_text("unchanged\n", encoding="utf-8")
    _adopt_alias(service, tmp_path, canonical=canonical, alias="legacy")
    try:
        (memory_home / "vault" / "legacy").symlink_to(
            canonical_dir,
            target_is_directory=True,
        )
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are not supported on this platform")

    with pytest.raises(ProjectResolutionError, match="identity|exact"):
        service.save(
            RawMemoryInput(title="Alias path", what="must not write"),
            project=canonical,
            idempotency_key="71000000-0000-4000-8000-000000000002",
        )

    assert sentinel.read_text(encoding="utf-8") == "unchanged\n"
    assert list(canonical_dir.glob("*-session.md")) == []
    assert not (memory_home / "locks").exists()


def test_lock_file_symlink_cannot_alias_another_same_home_file(
    service: MemoryService,
) -> None:
    memory_home = Path(service.memory_home)
    locks = memory_home / "locks"
    locks.mkdir()
    target = memory_home / "other-project.lock"
    target.write_bytes(b"do-not-open")
    try:
        (locks / "safe-project.lock").symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are not supported on this platform")

    with pytest.raises(ProjectResolutionError, match="identity|exact"):
        service.save(
            RawMemoryInput(title="Lock alias", what="must not open target"),
            project="safe-project",
            idempotency_key="71000000-0000-4000-8000-000000000003",
        )

    assert target.read_bytes() == b"do-not-open"
    assert list(Path(service.vault_dir).rglob("*-session.md")) == []
    assert service.db.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


def test_alias_authored_v2_replay_canonicalizes_project_once(
    service: MemoryService,
    tmp_path: Path,
) -> None:
    alias = "legacy"
    canonical = "project--111111111111"
    operation_id = "71000000-0000-4000-8000-000000000004"
    raw = RawMemoryInput(title="Alias authored", what="before adoption")
    created = service.save(raw, project=alias, idempotency_key=operation_id)
    path = Path(str(created["file_path"]))
    before_memory = _canonical_memory(created)
    before_operations = list(before_memory.operations)
    before_fingerprint = before_operations[0].request_fingerprint
    assert before_memory.project == alias
    assert service.db.has_vector(str(created["id"])) is True
    _adopt_alias(service, tmp_path, canonical=canonical, alias=alias)

    replayed_alias = service.save(raw, project=alias, idempotency_key=operation_id)
    after_alias = _canonical_memory(replayed_alias)
    canonical_ledger = service.db.get_operation(canonical, operation_id)
    record = service.get_memory_record(str(created["id"]))

    assert replayed_alias["action"] == "replayed"
    assert after_alias.project == canonical
    assert after_alias.operations == before_operations
    assert after_alias.operations[0].request_fingerprint == before_fingerprint
    assert record is not None
    assert record["project"] == canonical
    assert canonical_ledger is not None
    assert canonical_ledger["request_fingerprint"] == before_fingerprint
    assert service.db.get_operation(alias, operation_id) is None
    assert service.db.has_vector(str(created["id"])) is True
    canonicalized_bytes = path.read_bytes()

    replayed_canonical = service.save(
        raw,
        project=canonical,
        idempotency_key=operation_id,
    )
    assert replayed_canonical["action"] == "replayed"
    assert replayed_canonical["id"] == created["id"]
    assert path.read_bytes() == canonicalized_bytes
    assert len(_canonical_memory(replayed_canonical).operations) == 1

    with pytest.raises(SaveConflict):
        service.save(
            RawMemoryInput(title="Alias authored", what="different payload"),
            project=alias,
            idempotency_key=operation_id,
        )


def test_indexed_historical_v1_duplicate_requires_metadata_migration(
    service: MemoryService,
) -> None:
    project = "safe-project"
    legacy_path = Path(service.vault_dir) / project / "2020-01-02-session.md"
    legacy_path.parent.mkdir()
    legacy_path.write_text(
        "---\nproject: safe-project\n---\n\n"
        "# Historical Session\n\n"
        "### Indexed legacy\n"
        "**What:** old value\n",
        encoding="utf-8",
    )
    parsed = parse_session_file(legacy_path)
    entry = parsed.entries[0]
    indexed = Memory.from_raw(
        RawMemoryInput(title=entry.title, what=entry.what),
        project=project,
        file_path=str(legacy_path),
    )
    indexed.section_anchor = str(entry.section_anchor)
    service.db.insert_memory(indexed)
    before = legacy_path.read_bytes()

    with pytest.raises(
        LegacyMetadataRequiredError,
        match="memory migrate vault-metadata",
    ):
        service.save(
            RawMemoryInput(title="Indexed legacy", what="new value"),
            project=project,
            idempotency_key="71000000-0000-4000-8000-000000000005",
        )

    assert legacy_path.read_bytes() == before
