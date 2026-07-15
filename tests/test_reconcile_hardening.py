from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

import memory.reconcile as reconcile_module
from memory.cli import main
from memory.core import MemoryService
from memory.health import doctor
from memory.markdown import parse_session_file, write_session_document
from memory.models import Memory, MemoryOperation, RawMemoryInput
from memory.safe_io import LockTimeoutError, ProcessFileLock


def _derived_memory(
    memory_id: str,
    *,
    project: str,
    title: str = "Derived only",
    what: str = "staleprojectionuniqueterm",
    content_fingerprint: str | None = None,
    operations: list[MemoryOperation] | None = None,
) -> Memory:
    timestamp = "2000-01-01T00:00:00+00:00"
    return Memory(
        id=memory_id,
        title=title,
        what=what,
        why=None,
        impact=None,
        tags=["derived"],
        category="context",
        project=project,
        source="cursor",
        related_files=[],
        file_path=f"/missing/{project}/2000-01-01-session.md",
        section_anchor="derived-only",
        created_at=timestamp,
        updated_at=timestamp,
        operations=list(operations or []),
        content_fingerprint=content_fingerprint,
    )


def _ledger_rows(service: MemoryService) -> list[dict[str, object]]:
    return [
        dict(row)
        for row in service.db.conn.execute(
            """
            SELECT project, operation_id, memory_id, request_fingerprint,
                   action, source, timestamp, branch, commit_sha
            FROM save_operations
            ORDER BY project, operation_id
            """
        ).fetchall()
    ]


def _memory_rows(service: MemoryService) -> list[dict[str, object]]:
    return [
        dict(row)
        for row in service.db.conn.execute(
            "SELECT * FROM memories ORDER BY id"
        ).fetchall()
    ]


def _add_v1_blocker(service: MemoryService, project: str) -> Path:
    path = Path(service.vault_dir) / project / "2000-01-01-session.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"project: {project}\n"
        "---\n\n"
        "# Legacy blocker\n\n"
        "### Legacy entry\n"
        "**What:** incomplete canonical scope\n",
        encoding="utf-8",
    )
    return path


def test_complete_scan_removes_all_stale_derived_state(
    service: MemoryService,
) -> None:
    kept = service.save(
        RawMemoryInput(title="Kept", what="canonical"),
        project="p--1",
        authoritative_source="cursor",
        idempotency_key="11111111-1111-4111-8111-111111111111",
    )
    stale_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    stale_operation = MemoryOperation(
        operation_id="22222222-2222-4222-8222-222222222222",
        source="cursor",
        action="save",
        request_fingerprint="sha256:" + "2" * 64,
        timestamp="2000-01-01T00:00:00+00:00",
    )
    stale_fingerprint = "sha256:" + "e" * 64
    stale = _derived_memory(
        stale_id,
        project="p--1",
        content_fingerprint=stale_fingerprint,
        operations=[stale_operation],
    )
    stale_rowid = service.db.insert_memory(stale, details="stale details")
    service.db.upsert_operation("p--1", stale_id, stale_operation)
    assert service.db.upsert_vector_if_current(
        stale_id, stale_fingerprint, [0.0] * 768
    )
    service.db.queue_vector_repair(
        stale_id,
        stale_fingerprint,
        "2000-01-01T00:00:00+00:00",
    )

    assert service.db.has_vector(stale_id) is True
    assert service.db.conn.execute(
        "SELECT body FROM memory_details WHERE memory_id = ?", (stale_id,)
    ).fetchone() is not None
    assert service.db.get_operation("p--1", stale_operation.operation_id) is not None
    assert [
        row["rowid"]
        for row in service.db.conn.execute(
            """
            SELECT rowid FROM memories_fts
            WHERE memories_fts MATCH 'staleprojectionuniqueterm'
            """
        ).fetchall()
    ] == [stale_rowid]

    report = service.import_from_vault(reconcile=True, project="p--1")

    assert report["destructive_cleanup"] is True
    assert report["deleted"] == 1
    assert service.db.get_memory(stale_id) is None
    assert service.db.get_memory(str(kept["id"])) is not None
    assert service.db.conn.execute(
        "SELECT body FROM memory_details WHERE memory_id = ?", (stale_id,)
    ).fetchone() is None
    assert service.db.conn.execute(
        "SELECT rowid FROM memories_vec WHERE rowid = ?", (stale_rowid,)
    ).fetchone() is None
    assert not any(
        repair["memory_id"] == stale_id
        for repair in service.db.list_vector_repairs()
    )
    assert service.db.get_operation("p--1", stale_operation.operation_id) is None
    assert service.db.conn.execute(
        """
        SELECT rowid FROM memories_fts
        WHERE memories_fts MATCH 'staleprojectionuniqueterm'
        """
    ).fetchall() == []


def test_duplicate_operation_id_within_one_entry_fails_closed(
    service: MemoryService,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Repeated history", what="canonical"),
        project="p--1",
        authoritative_source="cursor",
        idempotency_key="33333333-3333-4333-8333-333333333333",
    )
    path = Path(str(saved["file_path"]))
    document = parse_session_file(path)
    entry = next(item for item in document.entries if item.id == saved["id"])
    operations = entry.metadata["operations"]
    assert isinstance(operations, list) and len(operations) == 1
    operations.append(copy.deepcopy(operations[0]))
    write_session_document(path, document)
    service.db.conn.execute(
        "UPDATE memories SET what = 'derived drift' WHERE id = ?",
        (saved["id"],),
    )
    service.db.conn.commit()
    before_ledger = _ledger_rows(service)

    report = service.import_from_vault(reconcile=True, project="p--1")

    duplicate = next(
        blocker
        for blocker in report["blockers"]
        if blocker["code"] == "duplicate_operation_id"
    )
    assert duplicate["operation_id"] == operations[0]["operation_id"]
    assert report["destructive_cleanup"] is False
    assert service.db.get_memory(str(saved["id"]))["what"] == "derived drift"
    assert _ledger_rows(service) == before_ledger


def test_duplicate_operation_id_across_entries_fails_closed(
    service: MemoryService,
) -> None:
    first = service.save(
        RawMemoryInput(title="First history", what="first canonical"),
        project="p--1",
        authoritative_source="cursor",
        idempotency_key="44444444-4444-4444-8444-444444444444",
    )
    second = service.save(
        RawMemoryInput(title="Second history", what="second canonical"),
        project="p--1",
        authoritative_source="cursor",
        idempotency_key="55555555-5555-4555-8555-555555555555",
    )
    path = Path(str(first["file_path"]))
    assert path == Path(str(second["file_path"]))
    document = parse_session_file(path)
    by_id = {entry.id: entry for entry in document.entries}
    first_operation = by_id[str(first["id"])].metadata["operations"][0]
    second_operation = by_id[str(second["id"])].metadata["operations"][0]
    second_operation["operation_id"] = first_operation["operation_id"]
    write_session_document(path, document)
    service.db.conn.execute(
        "UPDATE memories SET what = 'first drift' WHERE id = ?", (first["id"],)
    )
    service.db.conn.execute(
        "UPDATE memories SET what = 'second drift' WHERE id = ?", (second["id"],)
    )
    service.db.conn.commit()
    before_ledger = _ledger_rows(service)

    report = service.import_from_vault(reconcile=True, project="p--1")

    duplicate = next(
        blocker
        for blocker in report["blockers"]
        if blocker["code"] == "duplicate_operation_id"
    )
    assert duplicate["operation_id"] == first_operation["operation_id"]
    assert set(duplicate["memory_ids"]) == {str(first["id"]), str(second["id"])}
    assert report["destructive_cleanup"] is False
    assert service.db.get_memory(str(first["id"]))["what"] == "first drift"
    assert service.db.get_memory(str(second["id"]))["what"] == "second drift"
    assert _ledger_rows(service) == before_ledger


def test_cross_scope_memory_id_collision_fails_closed(
    service: MemoryService,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Scoped", what="canonical"),
        project="p--1",
        authoritative_source="cursor",
        idempotency_key="66666666-6666-4666-8666-666666666666",
    )
    service.db.conn.execute(
        "UPDATE memories SET project = 'foreign-project' WHERE id = ?",
        (saved["id"],),
    )
    service.db.conn.commit()
    before_memories = _memory_rows(service)
    before_ledger = _ledger_rows(service)

    report = service.import_from_vault(reconcile=True, project="p--1")

    blocker = next(
        item
        for item in report["blockers"]
        if item["code"] == "cross_project_memory_id"
    )
    assert blocker["memory_id"] == saved["id"]
    assert report["destructive_cleanup"] is False
    assert _memory_rows(service) == before_memories
    assert _ledger_rows(service) == before_ledger


def test_incomplete_scan_does_not_overwrite_conflicting_ledger_owner(
    service: MemoryService,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Canonical owner", what="canonical"),
        project="p--1",
        authoritative_source="cursor",
        idempotency_key="77777777-7777-4777-8777-777777777777",
    )
    operation_id = "77777777-7777-4777-8777-777777777777"
    conflicting_owner = _derived_memory(
        "88888888-8888-4888-8888-888888888888",
        project="p--1",
        title="Conflicting owner",
    )
    service.db.insert_memory(conflicting_owner)
    _add_v1_blocker(service, "p--1")
    service.db.conn.execute(
        """
        UPDATE save_operations SET memory_id = ?
        WHERE project = ? AND operation_id = ?
        """,
        (conflicting_owner.id, "p--1", operation_id),
    )
    service.db.conn.execute(
        "UPDATE memories SET what = 'derived drift' WHERE id = ?",
        (saved["id"],),
    )
    service.db.conn.commit()
    before_memories = _memory_rows(service)
    before_ledger = _ledger_rows(service)

    report = service.import_from_vault(reconcile=True, project="p--1")

    conflict = next(
        blocker
        for blocker in report["blockers"]
        if blocker["code"] == "operation_ledger_conflict"
    )
    assert conflict["memory_id"] == saved["id"]
    assert conflict["operation_id"] == operation_id
    assert report["destructive_cleanup"] is False
    assert _memory_rows(service) == before_memories
    assert _ledger_rows(service) == before_ledger


@pytest.mark.parametrize(
    ("failure_mode", "expected_blocker"),
    [
        ("cas_miss", "vector_cas_miss"),
        ("provider_failure", "embedding_failed"),
    ],
)
def test_vector_rebuild_failure_keeps_qualified_pending_repair(
    service: MemoryService,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
    expected_blocker: str,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Vector repair", what="canonical"),
        project="p--1",
    )
    memory_id = str(saved["id"])
    expected_fingerprint = service.db.get_memory(memory_id)["content_fingerprint"]
    service.db.invalidate_vector(memory_id)

    if failure_mode == "cas_miss":
        monkeypatch.setattr(
            service.db,
            "upsert_vector_if_current",
            lambda _memory_id, _fingerprint, _embedding: False,
        )
    else:
        def fail_embedding(_text: str) -> list[float]:
            raise RuntimeError("provider unavailable")

        monkeypatch.setattr(service.embedding_provider, "embed", fail_embedding)

    report = service.import_from_vault(reconcile=True, project="p--1")

    assert report["rebuilt_vectors"] == 0
    assert any(
        blocker["code"] == expected_blocker
        and blocker["memory_id"] == memory_id
        for blocker in report["blockers"]
    )
    assert service.db.has_vector(memory_id) is False
    repair = next(
        item
        for item in service.db.list_vector_repairs()
        if item["memory_id"] == memory_id
    )
    assert repair["content_fingerprint"] == expected_fingerprint
    assert service.db.get_memory(memory_id)["content_fingerprint"] == expected_fingerprint
    assert service.db.get_memory(memory_id)["status"] == "active"


def test_archived_projection_never_retains_or_rebuilds_vector(
    service: MemoryService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Archived vector", what="do not embed"),
        project="p--1",
    )
    memory_id = str(saved["id"])
    service.archive_memory(memory_id, reason="no longer current")
    archived = service.db.get_memory(memory_id)
    assert archived is not None
    service.db.insert_vector(int(archived["rowid"]), [0.0] * 768)
    service.db.queue_vector_repair(
        memory_id,
        str(archived["content_fingerprint"]),
        str(archived["updated_at"]),
    )
    embed_calls: list[str] = []

    def record_embed(text: str) -> list[float]:
        embed_calls.append(text)
        return [0.0] * 768

    monkeypatch.setattr(service.embedding_provider, "embed", record_embed)

    report = service.import_from_vault(reconcile=True, project="p--1")

    assert report["rebuilt_vectors"] == 0
    assert embed_calls == []
    assert service.db.has_vector(memory_id) is False
    assert not any(
        repair["memory_id"] == memory_id
        for repair in service.db.list_vector_repairs()
    )
    assert service.db.get_memory(memory_id)["status"] == "archived"


def test_doctor_is_pure_and_reports_stale_temp_and_ledger_drift(
    service: MemoryService,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Doctor source", what="canonical"),
        project="p--1",
        authoritative_source="cursor",
        idempotency_key="99999999-9999-4999-8999-999999999999",
    )
    canonical_path = Path(str(saved["file_path"]))
    stale_temp = canonical_path.parent / f".{canonical_path.name}.orphan.tmp"
    stale_temp.write_text("prepared but unjournaled\n", encoding="utf-8")
    service.db.conn.execute(
        "DELETE FROM save_operations WHERE project = ?", ("p--1",)
    )
    service.db.conn.commit()
    before_changes = service.db.conn.total_changes
    before_memories = _memory_rows(service)
    before_ledger = _ledger_rows(service)
    before_canonical = canonical_path.read_bytes()
    before_canonical_mtime = canonical_path.stat().st_mtime_ns
    before_temp = stale_temp.read_bytes()

    report = doctor(service, project="p--1")

    codes = {finding["code"] for finding in report["findings"]}
    assert "stale_prepared_file" in codes
    assert "operation_ledger_drift" in codes
    assert report["status"] == "warning"
    assert service.db.conn.total_changes == before_changes
    assert _memory_rows(service) == before_memories
    assert _ledger_rows(service) == before_ledger
    assert canonical_path.read_bytes() == before_canonical
    assert canonical_path.stat().st_mtime_ns == before_canonical_mtime
    assert stale_temp.read_bytes() == before_temp


def test_cli_reconcile_can_target_one_project(env_home: Path) -> None:
    service = MemoryService(str(env_home))
    try:
        saved = service.save(
            RawMemoryInput(title="CLI reconcile", what="canonical"),
            project="p--1",
            authoritative_source="cursor",
            idempotency_key="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )
        service.db.conn.execute(
            "UPDATE memories SET what = 'cli drift' WHERE id = ?", (saved["id"],)
        )
        service.db.conn.commit()
    finally:
        service.close()

    result = CliRunner().invoke(
        main,
        ["import", "--reconcile", "--project", "p--1"],
    )

    assert result.exit_code == 0, result.output
    payload = yaml.safe_load(result.output)
    assert payload["updated"] == 1
    assert payload["destructive_cleanup"] is True
    assert payload["blockers"] == []


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            ["import", "--dry-run", "--reconcile"],
            "--dry-run cannot be combined with --reconcile",
        ),
        (
            ["import", "--reindex", "--reconcile"],
            "--reindex cannot be combined with --reconcile",
        ),
        (
            ["import", "--project", "p--1"],
            "--project requires --reconcile",
        ),
    ],
)
def test_cli_reconcile_rejects_invalid_option_combinations(
    arguments: list[str],
    message: str,
) -> None:
    result = CliRunner().invoke(main, arguments)

    assert result.exit_code == 2
    assert message in result.output


def test_plain_import_refuses_missing_schema_v2_identity(
    service: MemoryService,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Canonical only", what="must keep its UUID"),
        project="p--1",
    )
    service.db.conn.execute("DELETE FROM save_operations")
    service.db.delete_memory_exact(str(saved["id"]))

    with pytest.raises(ValueError, match="Schema-v2 canonical Markdown"):
        service.import_from_vault()

    assert service.db.count_memories() == 0


def test_cross_project_canonical_id_collision_blocks_both_scopes(
    service: MemoryService,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Shared UUID", what="first owner"),
        project="p--1",
    )
    source = Path(str(saved["file_path"]))
    document = parse_session_file(source)
    document.project = "p--2"
    for entry in document.entries:
        entry.metadata["project"] = "p--2"
    target = Path(service.vault_dir) / "p--2" / source.name
    target.parent.mkdir(parents=True)
    write_session_document(target, document)
    before = _memory_rows(service)

    report = service.import_from_vault(reconcile=True)

    conflicts = [
        item
        for item in report["blockers"]
        if item["code"] == "duplicate_memory_id_across_projects"
    ]
    assert {item["project"] for item in conflicts} == {"p--1", "p--2"}
    assert all(item["memory_id"] == saved["id"] for item in conflicts)
    assert report["destructive_cleanup"] is False
    assert report["inserted"] == 0
    assert _memory_rows(service) == before


def test_cross_project_id_appearing_after_preflight_blocks_reconcile(
    service: MemoryService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Racing UUID", what="first owner"),
        project="p--1",
    )
    source = Path(str(saved["file_path"]))
    service.db.delete_memory_exact(str(saved["id"]))
    real_preflight = reconcile_module._vault_wide_memory_id_conflicts
    injected = False

    def inject_foreign_owner(
        current_service: MemoryService,
    ) -> list[dict[str, object]]:
        nonlocal injected
        conflicts = real_preflight(current_service)
        if not injected:
            injected = True
            document = parse_session_file(source)
            document.project = "p--2"
            for entry in document.entries:
                entry.metadata["project"] = "p--2"
            target = Path(service.vault_dir) / "p--2" / source.name
            target.parent.mkdir(parents=True)
            write_session_document(target, document)
        return conflicts

    monkeypatch.setattr(
        reconcile_module,
        "_vault_wide_memory_id_conflicts",
        inject_foreign_owner,
    )

    report = service.import_from_vault(reconcile=True, project="p--1")

    assert report["inserted"] == 0
    assert report["destructive_cleanup"] is False
    assert any(
        blocker["code"] == "duplicate_memory_id_across_projects"
        and blocker["memory_id"] == saved["id"]
        for blocker in report["blockers"]
    )
    assert service.db.get_memory(str(saved["id"])) is None


def test_canonical_mutation_lock_holds_vault_identity_lock(
    service: MemoryService,
) -> None:
    identity_lock = (
        Path(service.memory_home) / "locks" / ".vault-identity.lock"
    )

    with service.persistence._locked_after_recovery(("p--1",)):
        with pytest.raises(LockTimeoutError):
            with ProcessFileLock(identity_lock, timeout=0.01):
                pass


def test_cross_project_id_appearing_during_transaction_rolls_back(
    service: MemoryService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Late racing UUID", what="first owner"),
        project="p--1",
    )
    source = Path(str(saved["file_path"]))
    service.db.delete_memory_exact(str(saved["id"]))
    real_check = reconcile_module._project_vault_id_conflicts
    calls = 0

    def inject_after_transaction_check(
        current_service: MemoryService,
        project: str,
    ) -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        conflicts = real_check(current_service, project)
        if calls == 2:
            document = parse_session_file(source)
            document.project = "p--2"
            for entry in document.entries:
                entry.metadata["project"] = "p--2"
            target = Path(service.vault_dir) / "p--2" / source.name
            target.parent.mkdir(parents=True)
            write_session_document(target, document)
        return conflicts

    monkeypatch.setattr(
        reconcile_module,
        "_project_vault_id_conflicts",
        inject_after_transaction_check,
    )

    report = service.import_from_vault(reconcile=True, project="p--1")

    assert calls >= 3
    assert report["inserted"] == 0
    assert report["destructive_cleanup"] is False
    assert any(
        blocker["code"] == "duplicate_memory_id_across_projects"
        and blocker["memory_id"] == saved["id"]
        for blocker in report["blockers"]
    )
    assert service.db.get_memory(str(saved["id"])) is None


def test_cross_project_ledger_for_missing_row_blocks_insert(
    service: MemoryService,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Ledger owner", what="canonical"),
        project="p--1",
        idempotency_key="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    )
    source = Path(str(saved["file_path"]))
    document = parse_session_file(source)
    document.project = "p--2"
    for entry in document.entries:
        entry.metadata["project"] = "p--2"
    target = Path(service.vault_dir) / "p--2" / source.name
    target.parent.mkdir(parents=True)
    write_session_document(target, document)
    source.unlink()
    service.db.delete_memory_exact(str(saved["id"]))
    assert service.db.conn.execute(
        "SELECT 1 FROM save_operations WHERE project = 'p--1'"
    ).fetchone() is not None

    report = service.import_from_vault(reconcile=True, project="p--2")

    blocker = next(
        item
        for item in report["blockers"]
        if item["code"] == "cross_project_operation_ledger"
    )
    assert blocker["memory_id"] == saved["id"]
    assert report["inserted"] == 0
    assert report["destructive_cleanup"] is False
    assert service.db.get_memory(str(saved["id"])) is None


def test_final_scan_race_rolls_back_projection_and_report_counters(
    service: MemoryService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Race", what="canonical"),
        project="p--1",
    )
    service.db.conn.execute(
        "UPDATE memories SET what = 'derived drift' WHERE id = ?",
        (saved["id"],),
    )
    service.db.conn.commit()
    real_verify = reconcile_module._verify_reconcile_scan
    calls = 0

    def fail_final_verify(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise reconcile_module.ConcurrentModificationError("simulated race")
        real_verify(*args, **kwargs)

    monkeypatch.setattr(
        reconcile_module,
        "_verify_reconcile_scan",
        fail_final_verify,
    )

    report = service.import_from_vault(reconcile=True, project="p--1")

    assert report["inserted"] == 0
    assert report["updated"] == 0
    assert report["deleted"] == 0
    assert report["ledger_rows"] == 0
    assert report["destructive_cleanup"] is False
    assert service.db.get_memory(str(saved["id"]))["what"] == "derived drift"


def test_all_project_reconcile_rejects_symlinked_vault_root(
    service: MemoryService,
    tmp_path: Path,
) -> None:
    vault_root = Path(service.vault_dir)
    vault_root.rmdir()
    outside = tmp_path / "outside-vault"
    outside.mkdir()
    (outside / "foreign").mkdir()
    vault_root.symlink_to(outside, target_is_directory=True)

    report = service.import_from_vault(reconcile=True)

    assert report["destructive_cleanup"] is False
    assert report["inserted"] == 0
    assert report["blockers"][0]["code"] == "scope_discovery_failed"
