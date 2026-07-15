import hashlib
import json
from pathlib import Path

import pytest

import memory.reconcile as reconcile_module
from memory.core import MemoryService
from memory.health import doctor, doctor_home
from memory.markdown import parse_session_file
from memory.models import Memory, MemoryOperation, RawMemoryInput
from memory.persistence import content_fingerprint
from memory.projects import ProjectIdentity, ProjectRegistry
from memory.safe_io import ConcurrentModificationError


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


def test_migration_preserves_archived_markdown_status_over_stale_index(
    service: MemoryService,
) -> None:
    path = Path(service.vault_dir) / "legacy" / "2000-01-01-session.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    memory_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    path.write_text(
        "---\nproject: legacy\n---\n\n# Legacy\n\n## Archived\n\n"
        "### Historical entry\n"
        f"<!-- memory-id: {memory_id} -->\n"
        "**What:** Canonically archived\n"
        "**Category:** decision\n",
        encoding="utf-8",
    )
    service.db.insert_memory(
        Memory(
            id=memory_id,
            title="Historical entry",
            what="Canonically archived",
            why=None,
            impact=None,
            tags=[],
            category="context",
            project="legacy",
            source=None,
            related_files=[],
            file_path=str(path),
            section_anchor="historical-entry",
            created_at="2000-01-01T10:00:00+00:00",
            updated_at="2000-01-01T10:00:00+00:00",
            status="active",
        )
    )

    result = service.migrate_vault_metadata(project="legacy")

    assert result["migrated"] == 1
    migrated = parse_session_file(path).entries[0].to_memory(str(path))
    assert migrated.status == "archived"
    assert migrated.category == "decision"
    row = service.db.get_memory(memory_id)
    assert row is not None
    assert row["status"] == "archived"
    assert row["category"] == "decision"


def test_migration_preserves_preexisting_operation_ledger_rows(
    service: MemoryService,
    legacy_file: Path,
) -> None:
    memory_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    historical = MemoryOperation(
        operation_id="11111111-1111-4111-8111-111111111111",
        source="cursor",
        action="created",
        request_fingerprint="historical",
        timestamp="1999-12-31T10:00:00+00:00",
    )
    service.db.upsert_operation("legacy", memory_id, historical)

    service.migrate_vault_metadata(project="legacy")

    canonical = parse_session_file(legacy_file).entries[0].to_memory(str(legacy_file))
    canonical_operations = [
        {
            "operation_id": operation.operation_id,
            "source": operation.source,
            "action": operation.action,
            "request_fingerprint": operation.request_fingerprint,
            "timestamp": operation.timestamp,
            "branch": operation.branch,
            "commit_sha": operation.commit_sha,
        }
        for operation in canonical.operations
    ]
    row = service.db.get_memory(memory_id)
    assert row is not None
    assert json.loads(row["operation_history"]) == canonical_operations
    ledger = service.db.conn.execute(
        """
        SELECT operation_id, source, action, request_fingerprint, timestamp,
               branch, commit_sha
        FROM save_operations
        WHERE project = ? AND memory_id = ?
        ORDER BY timestamp, operation_id
        """,
        ("legacy", memory_id),
    ).fetchall()
    assert [dict(item) for item in ledger] == canonical_operations
    assert [operation.action for operation in canonical.operations] == [
        "created",
        "migrated",
    ]


def test_migration_refuses_operation_ledger_rows_from_another_project(
    service: MemoryService,
    legacy_file: Path,
) -> None:
    memory_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    cross_project = MemoryOperation(
        operation_id="22222222-2222-4222-8222-222222222222",
        source="gemini-cli",
        action="updated",
        request_fingerprint="cross-project",
        timestamp="2000-01-01T09:00:00+00:00",
    )
    service.db.upsert_operation("other-project", memory_id, cross_project)
    original = legacy_file.read_bytes()

    result = service.migrate_vault_metadata(project="legacy")

    assert result["migrated"] == 0
    assert result["unresolved"][0]["reason"] == (
        "operation ledger crosses project scope"
    )
    assert legacy_file.read_bytes() == original
    assert service.db.get_operation("other-project", cross_project.operation_id)


def test_scope_preflight_includes_existing_v2_memory_ids(
    service: MemoryService,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Canonical v2", what="already authoritative"),
        project="legacy",
        authoritative_source="cursor",
        idempotency_key="55555555-5555-4555-8555-555555555555",
    )
    canonical_path = Path(str(saved["file_path"]))
    canonical_before = canonical_path.read_bytes()
    legacy_path = Path(service.vault_dir) / "legacy" / "2000-01-01-session.md"
    legacy_path.write_text(
        "---\nproject: legacy\n---\n\n# Legacy\n\n### Duplicate v1\n"
        f"<!-- memory-id: {saved['id']} -->\n"
        "**What:** must not become canonical too\n",
        encoding="utf-8",
    )
    legacy_before = legacy_path.read_bytes()

    result = service.migrate_vault_metadata(project="legacy")

    assert result["migrated"] == 0
    assert result["unresolved"] == [
        {
            "file": "legacy/2000-01-01-session.md",
            "anchor": None,
            "reason": "stable memory ID is assigned to multiple files",
        }
    ]
    assert canonical_path.read_bytes() == canonical_before
    assert legacy_path.read_bytes() == legacy_before


@pytest.mark.parametrize(
    "living_json",
    [
        '{"custom":"must-not-disappear"}',
        '{"structured_data":{"kept":1},"structured_data":{"last":2}}',
    ],
)
def test_unknown_or_duplicate_living_memory_data_blocks_migration(
    service: MemoryService,
    living_json: str,
) -> None:
    path = Path(service.vault_dir) / "legacy" / "2000-01-01-session.md"
    path.parent.mkdir(parents=True)
    memory_id = "66666666-6666-4666-8666-666666666666"
    path.write_text(
        "---\nproject: legacy\n---\n\n# Legacy\n\n### Living\n"
        f"<!-- memory-id: {memory_id} -->\n"
        "**What:** keep every field\n"
        f"**Living Memory:** {living_json}\n",
        encoding="utf-8",
    )
    service.db.insert_memory(
        Memory(
            id=memory_id,
            title="Living",
            what="keep every field",
            why=None,
            impact=None,
            tags=[],
            category=None,
            project="legacy",
            source=None,
            related_files=[],
            file_path=str(path),
            section_anchor="living",
            created_at="2000-01-01T10:00:00+00:00",
            updated_at="2000-01-01T10:00:00+00:00",
        )
    )
    original = path.read_bytes()

    result = service.migrate_vault_metadata(project="legacy")

    assert result["migrated"] == 0
    assert result["unresolved"][0]["reason"] == (
        "legacy file contains unsupported content"
    )
    assert path.read_bytes() == original


def test_details_whitespace_that_v1_parser_would_trim_blocks_migration(
    service: MemoryService,
) -> None:
    path = Path(service.vault_dir) / "legacy" / "2000-01-01-session.md"
    path.parent.mkdir(parents=True)
    memory_id = "77777777-7777-4777-8777-777777777777"
    path.write_text(
        "---\nproject: legacy\n---\n\n# Legacy\n\n### Details\n"
        f"<!-- memory-id: {memory_id} -->\n"
        "**What:** preserve whitespace\n\n"
        "<details>\n"
        "  leading and trailing  \n"
        "</details>\n",
        encoding="utf-8",
    )
    service.db.insert_memory(
        Memory(
            id=memory_id,
            title="Details",
            what="preserve whitespace",
            why=None,
            impact=None,
            tags=[],
            category=None,
            project="legacy",
            source=None,
            related_files=[],
            file_path=str(path),
            section_anchor="details",
            created_at="2000-01-01T10:00:00+00:00",
            updated_at="2000-01-01T10:00:00+00:00",
        ),
        "leading and trailing",
    )
    original = path.read_bytes()

    result = service.migrate_vault_metadata(project="legacy")

    assert result["migrated"] == 0
    assert result["unresolved"][0]["reason"] == (
        "legacy file contains unsupported content"
    )
    assert path.read_bytes() == original


def test_doctor_refuses_symlinked_vault_root(tmp_path: Path) -> None:
    memory_home = tmp_path / "memory-home"
    memory_home.mkdir()
    outside_vault = tmp_path / "outside-vault"
    outside_file = outside_vault / "legacy" / "2000-01-01-session.md"
    outside_file.parent.mkdir(parents=True)
    outside_file.write_text(
        "---\nproject: secret\n---\n\n# Secret\n\n### Entry\n**What:** outside\n",
        encoding="utf-8",
    )
    try:
        (memory_home / "vault").symlink_to(outside_vault, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    report = doctor_home(memory_home)

    assert report["vault_metadata"]["schema_v1_files"] == []
    assert report["vault_metadata"]["schema_v2_files"] == []
    assert report["vault_metadata"]["unreadable_files"] == ["<vault-root>"]
    assert report["status"] == "warning"


def test_unresolved_v1_stable_ids_still_block_other_migrations(
    service: MemoryService,
) -> None:
    project_dir = Path(service.vault_dir) / "legacy"
    project_dir.mkdir(parents=True)
    memory_id = "88888888-8888-4888-8888-888888888888"
    valid = project_dir / "2000-01-01-session.md"
    unsupported = project_dir / "2000-01-02-session.md"
    valid.write_text(
        "---\nproject: legacy\n---\n\n# Valid\n\n### Entry\n"
        f"<!-- memory-id: {memory_id} -->\n**What:** valid\n",
        encoding="utf-8",
    )
    unsupported.write_text(
        "---\nproject: legacy\ncustom: keep\n---\n\n# Unsupported\n\n"
        "### Entry\n"
        f"<!-- memory-id: {memory_id} -->\n**What:** unsupported\n",
        encoding="utf-8",
    )
    service.db.insert_memory(
        Memory(
            id=memory_id,
            title="Entry",
            what="valid",
            why=None,
            impact=None,
            tags=[],
            category=None,
            project="legacy",
            source=None,
            related_files=[],
            file_path=str(valid),
            section_anchor="entry",
            created_at="2000-01-01T10:00:00+00:00",
            updated_at="2000-01-01T10:00:00+00:00",
        )
    )
    originals = {path: path.read_bytes() for path in (valid, unsupported)}

    result = service.migrate_vault_metadata(project="legacy")

    assert result["migrated"] == 0
    assert any(
        item["file"] == "legacy/2000-01-01-session.md"
        and "multiple files" in item["reason"]
        for item in result["unresolved"]
    )
    assert all(path.read_bytes() == content for path, content in originals.items())


def test_markdown_headings_inside_details_block_lossy_v1_parsing(
    service: MemoryService,
) -> None:
    path = Path(service.vault_dir) / "legacy" / "2000-01-01-session.md"
    path.parent.mkdir(parents=True)
    first_id = "99999999-9999-4999-8999-999999999999"
    second_id = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
    path.write_text(
        "---\nproject: legacy\n---\n\n# Legacy\n\n### One\n"
        f"<!-- memory-id: {first_id} -->\n**What:** first\n\n<details>\n"
        "### Two\n"
        f"<!-- memory-id: {second_id} -->\n**What:** must remain detail text\n"
        "</details>\n",
        encoding="utf-8",
    )
    for memory_id, title, what in (
        (first_id, "One", "first"),
        (second_id, "Two", "must remain detail text"),
    ):
        service.db.insert_memory(
            Memory(
                id=memory_id,
                title=title,
                what=what,
                why=None,
                impact=None,
                tags=[],
                category=None,
                project="legacy",
                source=None,
                related_files=[],
                file_path=str(path),
                section_anchor=title.lower(),
                created_at="2000-01-01T10:00:00+00:00",
                updated_at="2000-01-01T10:00:00+00:00",
            )
        )
    original = path.read_bytes()

    result = service.migrate_vault_metadata(project="legacy")

    assert result["migrated"] == 0
    assert result["unresolved"][0]["reason"] == (
        "legacy file contains unsupported content"
    )
    assert path.read_bytes() == original


def test_readable_field_whitespace_that_v1_parser_would_trim_blocks_migration(
    service: MemoryService,
) -> None:
    path = Path(service.vault_dir) / "legacy" / "2000-01-01-session.md"
    path.parent.mkdir(parents=True)
    memory_id = "dddddddd-1111-4111-8111-dddddddddddd"
    path.write_text(
        "---\nproject: legacy\n---\n\n# Legacy\n\n### Field\n"
        f"<!-- memory-id: {memory_id} -->\n"
        "**What:**  meaningful surrounding whitespace  \n",
        encoding="utf-8",
    )
    service.db.insert_memory(
        Memory(
            id=memory_id,
            title="Field",
            what="meaningful surrounding whitespace",
            why=None,
            impact=None,
            tags=[],
            category=None,
            project="legacy",
            source=None,
            related_files=[],
            file_path=str(path),
            section_anchor="field",
            created_at="2000-01-01T10:00:00+00:00",
            updated_at="2000-01-01T10:00:00+00:00",
        )
    )
    original = path.read_bytes()

    result = service.migrate_vault_metadata(project="legacy")

    assert result["migrated"] == 0
    assert result["unresolved"][0]["reason"] == (
        "legacy file contains unsupported content"
    )
    assert path.read_bytes() == original


def test_operation_ids_must_be_unique_across_every_memory_in_scope(
    service: MemoryService,
) -> None:
    path = Path(service.vault_dir) / "legacy" / "2000-01-01-session.md"
    path.parent.mkdir(parents=True)
    ids = (
        "bbbbbbbb-1111-4111-8111-bbbbbbbbbbbb",
        "cccccccc-1111-4111-8111-cccccccccccc",
    )
    path.write_text(
        "---\nproject: legacy\n---\n\n# Legacy\n\n"
        f"### One\n<!-- memory-id: {ids[0]} -->\n**What:** one\n\n"
        f"### Two\n<!-- memory-id: {ids[1]} -->\n**What:** two\n",
        encoding="utf-8",
    )
    shared_operation = MemoryOperation(
        operation_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        source="cursor",
        action="created",
        request_fingerprint="shared",
        timestamp="1999-01-01T00:00:00+00:00",
    )
    for memory_id, title in zip(ids, ("One", "Two"), strict=True):
        service.db.insert_memory(
            Memory(
                id=memory_id,
                title=title,
                what=title.lower(),
                why=None,
                impact=None,
                tags=[],
                category=None,
                project="legacy",
                source=None,
                related_files=[],
                file_path=str(path),
                section_anchor=title.lower(),
                created_at="2000-01-01T10:00:00+00:00",
                updated_at="2000-01-01T10:00:00+00:00",
                operations=[shared_operation],
            )
        )
    original = path.read_bytes()

    result = service.migrate_vault_metadata(project="legacy")

    assert result["migrated"] == 0
    assert result["unresolved"][0]["reason"] == (
        "operation ID is assigned to multiple memories"
    )
    assert path.read_bytes() == original
    assert service.db.conn.execute("SELECT COUNT(*) FROM save_operations").fetchone()[0] == 0


def test_scope_preflight_refuses_one_stable_id_owned_by_two_v1_files(
    service: MemoryService,
) -> None:
    project_dir = Path(service.vault_dir) / "legacy"
    project_dir.mkdir(parents=True)
    memory_id = "33333333-3333-4333-8333-333333333333"
    paths = [
        project_dir / "2000-01-01-session.md",
        project_dir / "2000-01-02-session.md",
    ]
    for index, path in enumerate(paths, start=1):
        path.write_text(
            "---\nproject: legacy\n---\n\n# Legacy\n\n"
            f"### Duplicate owner {index}\n"
            f"<!-- memory-id: {memory_id} -->\n"
            f"**What:** body {index}\n",
            encoding="utf-8",
        )
    service.db.insert_memory(
        Memory(
            id=memory_id,
            title="Duplicate owner 1",
            what="body 1",
            why=None,
            impact=None,
            tags=[],
            category=None,
            project="legacy",
            source=None,
            related_files=[],
            file_path=str(paths[0]),
            section_anchor="duplicate-owner-1",
            created_at="2000-01-01T10:00:00+00:00",
            updated_at="2000-01-01T10:00:00+00:00",
        )
    )
    originals = [path.read_bytes() for path in paths]

    result = service.migrate_vault_metadata(project="legacy")

    assert result["migrated"] == 0
    assert len(result["unresolved"]) == 2
    assert all("multiple files" in item["reason"] for item in result["unresolved"])
    assert [path.read_bytes() for path in paths] == originals
    assert service.db.conn.execute("SELECT COUNT(*) FROM save_operations").fetchone()[0] == 0


def test_doctor_resolves_canonical_project_to_adopted_alias_directory(
    service: MemoryService,
    tmp_path: Path,
) -> None:
    canonical = "canonical--111111111111"
    root = tmp_path / "repository"
    root.mkdir()
    identity = ProjectIdentity(
        root=root,
        display_name="Canonical",
        key=canonical,
        marker=None,
        canonical_owner=root,
    )
    ProjectRegistry(Path(service.memory_home)).adopt_legacy("legacy", identity)
    path = Path(service.vault_dir) / "legacy" / "2000-01-01-session.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nproject: legacy\n---\n\n# Legacy\n\n### Entry\n**What:** old\n",
        encoding="utf-8",
    )

    report = doctor(service, canonical)

    assert report["vault_metadata"]["schema_v1_files"] == [
        "legacy/2000-01-01-session.md"
    ]
    assert report["vault_metadata"]["migration_command"] == (
        "memory migrate vault-metadata"
    )


@pytest.mark.parametrize("project", ["../outside", "/tmp/outside"])
def test_doctor_rejects_project_paths_that_escape_the_vault(
    tmp_path: Path,
    project: str,
) -> None:
    memory_home = tmp_path / "memory-home"
    (memory_home / "vault").mkdir(parents=True)
    outside = memory_home / "outside" / "2000-01-01-session.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text(
        "---\nproject: secret\n---\n\n# Secret\n\n### Entry\n**What:** outside\n",
        encoding="utf-8",
    )

    report = doctor_home(memory_home, project=project)

    assert report["vault_metadata"]["schema_v1_files"] == []
    assert report["vault_metadata"]["schema_v2_files"] == []
    assert report["vault_metadata"]["unreadable_files"] == ["<invalid-project>"]


def test_unmodelled_legacy_content_blocks_lossy_rewrite(
    service: MemoryService,
) -> None:
    path = Path(service.vault_dir) / "legacy" / "2000-01-01-session.md"
    path.parent.mkdir(parents=True)
    memory_id = "44444444-4444-4444-8444-444444444444"
    path.write_text(
        "---\nproject: legacy\ncustom-frontmatter: keep-me\n---\n\n"
        "# Legacy\n\nPreamble that cannot be represented.\n\n"
        "### Entry\n"
        f"<!-- memory-id: {memory_id} -->\n"
        "**What:** old\n"
        "**Custom:** keep-me-too\n",
        encoding="utf-8",
    )
    service.db.insert_memory(
        Memory(
            id=memory_id,
            title="Entry",
            what="old",
            why=None,
            impact=None,
            tags=[],
            category=None,
            project="legacy",
            source=None,
            related_files=[],
            file_path=str(path),
            section_anchor="entry",
            created_at="2000-01-01T10:00:00+00:00",
            updated_at="2000-01-01T10:00:00+00:00",
        )
    )
    original = path.read_bytes()

    result = service.migrate_vault_metadata(project="legacy")

    assert result["migrated"] == 0
    assert result["unresolved"] == [
        {
            "file": "legacy/2000-01-01-session.md",
            "anchor": None,
            "reason": "legacy file contains unsupported content",
        }
    ]
    assert path.read_bytes() == original


def test_migration_cas_uses_digest_of_the_parsed_source(
    service: MemoryService,
    legacy_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_commit = reconcile_module._commit_file
    concurrent = legacy_file.read_text(encoding="utf-8").replace(
        "Legacy canonical text",
        "Concurrent external text",
    )

    def concurrent_commit(service_arg, migration):
        legacy_file.write_text(concurrent, encoding="utf-8")
        return original_commit(service_arg, migration)

    monkeypatch.setattr(reconcile_module, "_commit_file", concurrent_commit)

    with pytest.raises(ConcurrentModificationError):
        service.migrate_vault_metadata(project="legacy")

    assert legacy_file.read_text(encoding="utf-8") == concurrent
    row = service.db.get_memory("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    assert row is not None
    assert row["content_fingerprint"] is None
    assert service.db.conn.execute("SELECT COUNT(*) FROM save_operations").fetchone()[0] == 0
