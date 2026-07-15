import hashlib
import json
from pathlib import Path

import pytest

from memory.core import MemoryService
from memory.health import doctor, doctor_home
from memory.markdown import parse_session_file
from memory.models import Memory
from memory.persistence import content_fingerprint


@pytest.fixture
def legacy_file(service: MemoryService) -> Path:
    path = Path(service.vault_dir) / "legacy" / "2000-01-01-session.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    memory_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    details = "Legacy details preserved exactly."
    path.write_text(
        "---\n"
        "project: legacy\n"
        "created: 2000-01-01T10:00:00+00:00\n"
        "tags: [aggregate-only]\n"
        "sources: [cursor]\n"
        "---\n\n"
        "# Legacy Session\n\n"
        "## Decisions\n\n"
        "### Legacy decision\n"
        f"<!-- memory-id: {memory_id} -->\n"
        "**What:** Legacy canonical text\n"
        "**Why:** Preserve historical metadata\n"
        "**Impact:** Migration remains lossless\n"
        "**Source:** cursor\n\n"
        "<details>\n"
        f"{details}\n"
        "</details>\n",
        encoding="utf-8",
    )
    row = Memory(
        id=memory_id,
        title="Legacy decision",
        what="Legacy canonical text",
        why="Preserve historical metadata",
        impact="Migration remains lossless",
        tags=["per-memory-tag"],
        category="decision",
        project="legacy",
        source="cursor",
        related_files=["src/legacy.py"],
        file_path=str(path),
        section_anchor="legacy-decision",
        created_at="2000-01-01T10:00:00+00:00",
        updated_at="2000-01-02T10:00:00+00:00",
        structured_data={"constraints": ["preserve DB-only metadata"]},
        confidence=0.8,
        branch="legacy-branch",
    )
    service.db.insert_memory(row, details)
    return path


def test_migration_enriches_from_matching_db_row_without_guessing(
    service: MemoryService,
    legacy_file: Path,
) -> None:
    result = service.migrate_vault_metadata(project="legacy")
    assert result["migrated"] == 1
    parsed = parse_session_file(legacy_file)
    assert parsed.schema_version == 2
    metadata = parsed.entries[0].metadata
    assert metadata["history_complete"] is False
    assert metadata["creator_source"] == "cursor"
    assert metadata["operations"][0]["action"] == "migrated"
    rebuilt = parsed.entries[0].to_memory(str(legacy_file))
    assert rebuilt.source == metadata["creator_source"]
    assert metadata["content_fingerprint"] == content_fingerprint(rebuilt)
    row = service.get_memory_record(rebuilt.id)
    assert row is not None
    assert row["content_fingerprint"] == metadata["content_fingerprint"]
    operation = service.db.get_operation(
        "legacy", metadata["operations"][0]["operation_id"]
    )
    assert operation is not None
    assert operation["memory_id"] == rebuilt.id
    assert operation["action"] == "migrated"
    second = service.migrate_vault_metadata(project="legacy")
    assert second["migrated"] == 0


def test_dry_run_reports_migratable_file_without_changing_storage(
    service: MemoryService,
    legacy_file: Path,
) -> None:
    original = legacy_file.read_bytes()

    result = service.migrate_vault_metadata(project="legacy", dry_run=True)

    assert result["migrated"] == 0
    assert result["would_migrate"] == 1
    assert legacy_file.read_bytes() == original
    row = service.get_memory_record("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    assert row is not None
    assert row["content_fingerprint"] is None
    assert service.db.conn.execute("SELECT COUNT(*) FROM save_operations").fetchone()[0] == 0


def test_unmatched_entry_is_reported_and_entire_file_stays_unchanged(
    service: MemoryService,
) -> None:
    path = Path(service.vault_dir) / "legacy" / "2000-01-01-session.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\nproject: legacy\n---\n\n# Legacy Session\n\n"
        "### Orphan\n**What:** No derived row exists\n",
        encoding="utf-8",
    )
    original = path.read_bytes()

    result = service.migrate_vault_metadata(project="legacy")

    assert result["migrated"] == 0
    assert result["unresolved"][0]["anchor"] == "orphan"
    assert result["unresolved"][0]["file"] == "legacy/2000-01-01-session.md"
    assert path.read_bytes() == original


def test_one_unmatched_entry_blocks_every_entry_in_the_same_file(
    service: MemoryService,
) -> None:
    path = Path(service.vault_dir) / "legacy" / "2000-01-01-session.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\nproject: legacy\n---\n\n# Legacy Session\n\n"
        "### Matched\n**What:** Has a row\n\n"
        "### Unmatched\n**What:** Must block the file\n",
        encoding="utf-8",
    )
    matched = Memory(
        id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        title="Matched",
        what="Has a row",
        why=None,
        impact=None,
        tags=["row-tag"],
        category=None,
        project="legacy",
        source="cursor",
        related_files=[],
        file_path=str(path),
        section_anchor="matched",
        created_at="2000-01-01T10:00:00+00:00",
        updated_at="2000-01-01T10:00:00+00:00",
    )
    service.db.insert_memory(matched)
    original = path.read_bytes()

    result = service.migrate_vault_metadata(project="legacy")

    assert result["migrated"] == 0
    assert result["unresolved"][0]["anchor"] == "unmatched"
    assert path.read_bytes() == original
    row = service.db.get_memory(matched.id)
    assert row is not None
    assert row["content_fingerprint"] is None


def test_doctor_reports_v1_files_until_explicit_migration(
    service: MemoryService,
    legacy_file: Path,
) -> None:
    before = doctor(service)
    assert before["status"] == "warning"
    assert before["vault_metadata"]["schema_v1_files"] == [
        "legacy/2000-01-01-session.md"
    ]

    service.migrate_vault_metadata(project="legacy")

    after = doctor(service)
    assert after["vault_metadata"]["schema_v1_files"] == []


def test_aggregate_frontmatter_is_not_guessed_for_unknown_entry_metadata(
    service: MemoryService,
) -> None:
    path = Path(service.vault_dir) / "legacy" / "2000-01-01-session.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\nproject: legacy\ntags: [aggregate-only]\nsources: [cursor]\n---\n\n"
        "# Legacy Session\n\n### Unknown creator\n**What:** Keep source unknown\n",
        encoding="utf-8",
    )
    row = Memory(
        id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        title="Unknown creator",
        what="Keep source unknown",
        why=None,
        impact=None,
        tags=["per-memory-only"],
        category="context",
        project="legacy",
        source=None,
        related_files=[],
        file_path=str(path),
        section_anchor="unknown-creator",
        created_at="2000-01-01T10:00:00+00:00",
        updated_at="2000-01-01T10:00:00+00:00",
    )
    service.db.insert_memory(row)

    service.migrate_vault_metadata(project="legacy")

    memory = parse_session_file(path).entries[0].to_memory(str(path))
    assert memory.tags == ["per-memory-only"]
    assert memory.source is None
    assert memory.creator_source is None
    assert memory.last_updated_by is None
    assert memory.contributors == []
    assert memory.operations[0].source is None


def test_explicit_migration_decodes_legacy_cp1251_then_writes_utf8(
    service: MemoryService,
) -> None:
    path = Path(service.vault_dir) / "legacy" / "2000-01-01-session.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\nproject: legacy\n---\n\n# Legacy\n\n"
        "### Решение\n**What:** старый текст\n**Source:** cursor\n",
        encoding="cp1251",
    )
    row = Memory(
        id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        title="Решение",
        what="старый текст",
        why=None,
        impact=None,
        tags=["unicode"],
        category="decision",
        project="legacy",
        source="cursor",
        related_files=[],
        file_path=str(path),
        section_anchor="memory",
        created_at="2000-01-01T10:00:00+00:00",
        updated_at="2000-01-01T10:00:00+00:00",
    )
    service.db.insert_memory(row)

    result = service.migrate_vault_metadata(project="legacy")

    assert result["migrated"] == 1
    rendered = path.read_text(encoding="utf-8")
    assert "Решение" in rendered
    assert "старый текст" in rendered
    assert parse_session_file(path).schema_version == 2


def test_read_only_service_dry_run_performs_no_projection_writes(
    service: MemoryService,
    legacy_file: Path,
) -> None:
    original = legacy_file.read_bytes()
    read_only = MemoryService(
        service.memory_home,
        recover_pending=False,
        read_only=True,
    )
    try:
        result = read_only.migrate_vault_metadata(project="legacy", dry_run=True)
        assert result["would_migrate"] == 1
        assert read_only.db.conn.total_changes == 0
    finally:
        read_only.close()

    assert legacy_file.read_bytes() == original
    assert not (Path(service.memory_home) / "locks").exists()


def test_migration_operation_id_uses_canonical_deterministic_hash(
    service: MemoryService,
    legacy_file: Path,
) -> None:
    service.migrate_vault_metadata(project="legacy")
    operation_id = parse_session_file(legacy_file).entries[0].metadata["operations"][
        0
    ]["operation_id"]
    expected = hashlib.sha256(
        json.dumps(
            [
                "legacy",
                "legacy/2000-01-01-session.md",
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                2,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    assert operation_id == expected


def test_failure_before_markdown_replace_rolls_back_projection_and_v1_bytes(
    service: MemoryService,
    legacy_file: Path,
) -> None:
    original = legacy_file.read_bytes()

    def fail(phase: str) -> None:
        if phase == "after_db_write":
            raise RuntimeError("simulated pre-replace failure")

    service.persistence.fault = fail
    with pytest.raises(RuntimeError, match="pre-replace"):
        service.migrate_vault_metadata(project="legacy")

    assert legacy_file.read_bytes() == original
    row = service.db.get_memory("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    assert row is not None
    assert row["content_fingerprint"] is None
    assert service.db.conn.execute("SELECT COUNT(*) FROM save_operations").fetchone()[0] == 0


def test_failure_after_temp_fsync_discards_uncommitted_temporary(
    service: MemoryService,
    legacy_file: Path,
) -> None:
    original = legacy_file.read_bytes()

    def fail(phase: str) -> None:
        if phase == "after_all_temps_fsync":
            raise RuntimeError("simulated prepared-file failure")

    service.persistence.fault = fail
    with pytest.raises(RuntimeError, match="prepared-file"):
        service.migrate_vault_metadata(project="legacy")

    assert legacy_file.read_bytes() == original
    assert list(legacy_file.parent.glob(f".{legacy_file.name}.*.tmp")) == []


def test_failure_after_markdown_replace_leaves_v2_authoritative_and_db_old(
    service: MemoryService,
    legacy_file: Path,
) -> None:
    def fail(phase: str) -> None:
        if phase == "after_target_replace:0":
            raise RuntimeError("simulated post-replace failure")

    service.persistence.fault = fail
    with pytest.raises(RuntimeError, match="post-replace"):
        service.migrate_vault_metadata(project="legacy")

    assert parse_session_file(legacy_file).schema_version == 2
    row = service.db.get_memory("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    assert row is not None
    assert row["content_fingerprint"] is None
    assert service.db.conn.execute("SELECT COUNT(*) FROM save_operations").fetchone()[0] == 0


def test_doctor_home_reports_v1_without_creating_a_database(tmp_path: Path) -> None:
    path = tmp_path / "vault" / "legacy" / "2000-01-01-session.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nproject: legacy\n---\n\n# Legacy\n\n### Entry\n**What:** old\n",
        encoding="utf-8",
    )

    report = doctor_home(tmp_path)

    assert report["status"] == "warning"
    assert report["vault_metadata"]["schema_v1_files"] == [
        "legacy/2000-01-01-session.md"
    ]
    assert not (tmp_path / "index.db").exists()
    assert not (tmp_path / "locks").exists()


def test_symlinked_session_is_reported_without_following_or_replacing_target(
    service: MemoryService,
    tmp_path: Path,
) -> None:
    project_dir = Path(service.vault_dir) / "legacy"
    project_dir.mkdir(parents=True)
    outside = tmp_path / "outside-session.md"
    outside.write_text(
        "---\nproject: legacy\n---\n\n# Legacy\n\n### Entry\n**What:** old\n",
        encoding="utf-8",
    )
    link = project_dir / "2000-01-01-session.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    original = outside.read_bytes()

    result = service.migrate_vault_metadata(project="legacy")

    assert result["migrated"] == 0
    assert result["unresolved"][0]["reason"] == "file is not a local regular file"
    assert link.is_symlink()
    assert outside.read_bytes() == original
