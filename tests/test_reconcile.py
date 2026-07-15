from pathlib import Path

import pytest

from memory.core import MemoryService
from memory.health import doctor
from memory.models import Memory, RawMemoryInput


@pytest.fixture
def legacy_file(service: MemoryService) -> Path:
    path = Path(service.vault_dir) / "legacy" / "2000-01-01-session.md"
    path.parent.mkdir(parents=True)
    memory_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    path.write_text(
        "---\nproject: legacy\n---\n\n# Legacy\n\n### Entry\n"
        f"<!-- memory-id: {memory_id} -->\n"
        "**What:** canonical legacy text\n"
        "**Source:** cursor\n",
        encoding="utf-8",
    )
    service.db.insert_memory(
        Memory(
            id=memory_id,
            title="Entry",
            what="canonical legacy text",
            why=None,
            impact=None,
            tags=["legacy"],
            category="context",
            project="legacy",
            source="cursor",
            related_files=[],
            file_path=str(path),
            section_anchor="entry",
            created_at="2000-01-01T10:00:00+00:00",
            updated_at="2000-01-01T10:00:00+00:00",
        )
    )
    return path


@pytest.fixture
def mixed_project(service: MemoryService) -> Path:
    service.save(
        RawMemoryInput(title="Complete v2 entry", what="safe canonical row"),
        project="mixed",
        authoritative_source="cursor",
        idempotency_key="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    )
    legacy_path = Path(service.vault_dir) / "mixed" / "2000-01-01-session.md"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    legacy_path.write_text(
        "---\n"
        "project: mixed\n"
        "created: 2000-01-01T10:00:00+00:00\n"
        "tags: [legacy]\n"
        "sources: [cursor]\n"
        "---\n\n"
        "# Mixed Legacy Session\n\n"
        "### Incomplete v1 entry\n"
        f"<!-- memory-id: {legacy_id} -->\n"
        "**What:** must remain derived while v1 blocks cleanup\n"
        "**Source:** cursor\n",
        encoding="utf-8",
    )
    service.db.insert_memory(
        Memory(
            id=legacy_id,
            title="Incomplete v1 entry",
            what="must remain derived while v1 blocks cleanup",
            why=None,
            impact=None,
            tags=["legacy"],
            category="context",
            project="mixed",
            source="cursor",
            related_files=[],
            file_path=str(legacy_path),
            section_anchor="incomplete-v1-entry",
            created_at="2000-01-01T10:00:00+00:00",
            updated_at="2000-01-01T10:00:00+00:00",
        ),
        None,
    )
    return legacy_path.parent


def test_reconcile_repairs_changed_row_and_missing_ledger(
    service: MemoryService,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Canonical", what="markdown wins"),
        project="p--1",
        authoritative_source="cursor",
        idempotency_key="44444444-4444-4444-8444-444444444444",
    )
    service.db.conn.execute(
        "UPDATE memories SET what = 'drift' WHERE id = ?", (saved["id"],)
    )
    service.db.conn.execute("DELETE FROM save_operations")
    service.db.conn.commit()

    report = service.import_from_vault(reconcile=True, project="p--1")

    assert report["updated"] == 1
    assert service.get_memory_record(saved["id"])["what"] == "markdown wins"
    assert (
        service.db.get_operation(
            "p--1", "44444444-4444-4444-8444-444444444444"
        )
        is not None
    )


def test_mixed_v1_v2_scan_never_deletes_derived_rows(
    service: MemoryService,
    mixed_project: Path,
) -> None:
    before = service.db.count_memories(project="mixed")
    legacy_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    assert service.get_memory_record(legacy_id) is not None

    report = service.import_from_vault(reconcile=True, project="mixed")

    assert report["destructive_cleanup"] is False
    assert service.db.count_memories(project="mixed") == before
    assert service.get_memory_record(legacy_id) is not None


def test_reconcile_rejects_corrupt_canonical_fingerprint_without_mutation(
    service: MemoryService,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Integrity", what="canonical"), project="p--1"
    )
    path = Path(saved["file_path"])
    row_before = dict(service.get_memory_record(saved["id"]))
    expected = row_before["content_fingerprint"]
    content = path.read_text(encoding="utf-8")
    corrupted = content.replace(
        f'"content_fingerprint":"{expected}"',
        '"content_fingerprint":"sha256:corrupt"',
    )
    assert corrupted != content
    path.write_text(corrupted, encoding="utf-8")

    report = service.import_from_vault(reconcile=True, project="p--1")

    assert report["invalid_fingerprints"] == [saved["id"]]
    assert report["destructive_cleanup"] is False
    assert dict(service.get_memory_record(saved["id"])) == row_before
    finding = next(
        item
        for item in doctor(service, project="p--1")["findings"]
        if item["code"] == "invalid_content_fingerprint"
    )
    assert finding["memory_id"] == saved["id"]
    assert finding["repair"] == (
        "Fix or restore canonical Markdown, then run: "
        "memory import --reconcile --project p--1"
    )


def test_reconcile_rebuilds_missing_vector_through_fingerprint_cas(
    service: MemoryService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Vector", what="rebuild me"), project="p--1"
    )
    expected = service.get_memory_record(saved["id"])["content_fingerprint"]
    service.db.invalidate_vector(saved["id"])
    calls: list[tuple[str, str]] = []
    real_upsert = service.db.upsert_vector_if_current

    def recording_upsert(
        memory_id: str,
        fingerprint: str,
        embedding: list[float],
    ) -> bool:
        calls.append((memory_id, fingerprint))
        return real_upsert(memory_id, fingerprint, embedding)

    monkeypatch.setattr(
        service.db, "upsert_vector_if_current", recording_upsert
    )

    report = service.import_from_vault(reconcile=True, project="p--1")

    assert report["rebuilt_vectors"] == 1
    assert calls == [(saved["id"], expected)]
    assert service.db.has_vector(saved["id"]) is True


def test_reconcile_repairs_post_replace_migration_projection(
    service: MemoryService,
    legacy_file: Path,
) -> None:
    def fail_after_replace(phase: str) -> None:
        if phase == "after_target_replace:0":
            raise RuntimeError("simulated post-replace crash")

    service.persistence.fault = fail_after_replace
    with pytest.raises(RuntimeError, match="post-replace"):
        service.migrate_vault_metadata(project="legacy")
    service.persistence.fault = lambda _phase: None
    row_before = service.db.get_memory("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    assert row_before is not None
    assert row_before["content_fingerprint"] is None

    report = service.import_from_vault(reconcile=True, project="legacy")

    assert report["updated"] == 1
    repaired = service.db.get_memory("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    assert repaired is not None
    assert repaired["content_fingerprint"] is not None
    assert service.db.conn.execute("SELECT COUNT(*) FROM save_operations").fetchone()[0] == 1


def test_complete_v2_scan_removes_projection_absent_from_markdown(
    service: MemoryService,
) -> None:
    kept = service.save(
        RawMemoryInput(title="Kept", what="canonical"), project="p--1"
    )
    stale = Memory(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
        title="Stale",
        what="derived only",
        why=None,
        impact=None,
        tags=[],
        category="context",
        project="p--1",
        source=None,
        related_files=[],
        file_path=str(Path(service.vault_dir) / "p--1" / "missing-session.md"),
        section_anchor="stale",
        created_at="2000-01-01T00:00:00+00:00",
        updated_at="2000-01-01T00:00:00+00:00",
    )
    service.db.insert_memory(stale)

    report = service.import_from_vault(reconcile=True, project="p--1")

    assert report["deleted"] == 1
    assert report["destructive_cleanup"] is True
    assert service.db.get_memory(stale.id) is None
    assert service.db.get_memory(kept["id"]) is not None


def test_second_reconcile_is_idempotent(service: MemoryService) -> None:
    service.save(
        RawMemoryInput(title="Stable", what="canonical"), project="p--1"
    )

    first = service.import_from_vault(reconcile=True, project="p--1")
    second = service.import_from_vault(reconcile=True, project="p--1")

    assert first["blockers"] == []
    assert second == {
        "inserted": 0,
        "updated": 0,
        "deleted": 0,
        "ledger_rows": first["ledger_rows"],
        "rebuilt_vectors": 0,
        "invalid_fingerprints": [],
        "blockers": [],
        "destructive_cleanup": True,
    }
