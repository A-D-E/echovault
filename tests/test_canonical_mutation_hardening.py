from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import uuid

from click.testing import CliRunner
import pytest

from memory.cli import main
from memory.core import MemoryService
from memory.health import doctor
from memory.markdown import parse_session_file
from memory.models import RawMemoryInput
import memory.persistence as persistence
from memory.safe_io import PreparedAtomicWrite, prepare_atomic_text


def test_create_uses_durable_journal_and_recovers_after_publication(
    service: MemoryService,
) -> None:
    operation_id = "81000000-0000-4000-8000-000000000001"

    def inject(phase: str) -> None:
        if phase == "after_journal_fsync":
            raise RuntimeError("durable create journal")

    service.persistence.fault = inject
    with pytest.raises(RuntimeError, match="durable create journal"):
        service.save(
            RawMemoryInput(title="Journaled create", what="recover me"),
            project="p--1",
            idempotency_key=operation_id,
        )

    journals = sorted((Path(service.memory_home) / "transactions").glob("*.json"))
    assert [path.stem for path in journals] == [operation_id]
    service.close()

    recovered = MemoryService(service.memory_home)
    try:
        assert recovered.persistence.startup_recoveries == (operation_id,)
        rows = recovered.list_memories(project="p--1")
        assert len(rows) == 1
        document = parse_session_file(rows[0]["file_path"])
        assert document.entries[0].id == rows[0]["id"]
        assert recovered.db.get_operation("p--1", operation_id) is not None
    finally:
        recovered.close()


def test_journal_publication_rejects_symlinked_transactions(
    tmp_path: Path,
) -> None:
    memory_home = tmp_path / ".memory"
    memory_home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (memory_home / "transactions").symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are not supported on this platform")

    operation = persistence.OperationJournal(
        schema_version=1,
        operation_id="82000000-0000-4000-8000-000000000001",
        action="update",
        project_keys=("p--1",),
        affected_memory_ids=("memory-id",),
        canonical_memory_id="memory-id",
        targets=(
            persistence.JournalTarget(
                target="vault/p--1/2026-07-14-session.md",
                temporary="vault/p--1/.2026-07-14-session.md.test.tmp",
                before_sha256=None,
                after_sha256="0" * 64,
            ),
        ),
        created_at="2026-07-14T10:00:00+00:00",
    )

    with pytest.raises(persistence.JournalRecoveryConflict):
        persistence.persist_operation_journal(memory_home, operation)
    assert list(outside.iterdir()) == []


def test_journal_publication_rejects_dangling_destination_symlink(
    tmp_path: Path,
) -> None:
    memory_home = tmp_path / ".memory"
    transactions = memory_home / "transactions"
    transactions.mkdir(parents=True)
    operation_id = "82000000-0000-4000-8000-000000000002"
    journal_path = transactions / f"{operation_id}.json"
    try:
        journal_path.symlink_to(tmp_path / "missing-journal")
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are not supported on this platform")

    operation = persistence.OperationJournal(
        schema_version=1,
        operation_id=operation_id,
        action="update",
        project_keys=("p--1",),
        affected_memory_ids=("memory-id",),
        canonical_memory_id="memory-id",
        targets=(
            persistence.JournalTarget(
                target="vault/p--1/2026-07-14-session.md",
                temporary="vault/p--1/.2026-07-14-session.md.test.tmp",
                before_sha256=None,
                after_sha256="0" * 64,
            ),
        ),
        created_at="2026-07-14T10:00:00+00:00",
    )

    with pytest.raises(persistence.JournalRecoveryConflict):
        persistence.persist_operation_journal(memory_home, operation)
    assert journal_path.is_symlink()


def test_recovery_rechecks_digest_immediately_before_replace(
    service: MemoryService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Recovery CAS", what="before"),
        project="p--1",
    )

    def inject(phase: str) -> None:
        if phase == "after_journal_fsync":
            raise RuntimeError("leave journal")

    service.persistence.fault = inject
    with pytest.raises(RuntimeError, match="leave journal"):
        service.update_memory_record(
            str(saved["id"]),
            patch=persistence.MemoryPatch(what="after"),
            actor="dashboard",
        )
    service.persistence.fault = lambda _phase: None
    target = Path(str(saved["file_path"]))
    journal = next((Path(service.memory_home) / "transactions").glob("*.json"))
    original_replace_if_digest = PreparedAtomicWrite.replace_if_digest

    def concurrent_edit(
        prepared: PreparedAtomicWrite,
        expected_digest: str | None,
    ) -> None:
        target.write_text("external edit\n", encoding="utf-8")
        original_replace_if_digest(prepared, expected_digest)

    monkeypatch.setattr(
        PreparedAtomicWrite,
        "replace_if_digest",
        concurrent_edit,
    )
    with pytest.raises(persistence.JournalRecoveryConflict):
        service.persistence.recover_pending_operations(("p--1",))
    assert target.read_text(encoding="utf-8") == "external edit\n"
    assert journal.exists()


def test_recovered_vector_is_rebuilt_when_following_mutation_fails(
    service: MemoryService,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Recovered vector", what="before"),
        project="p--1",
    )

    def inject(phase: str) -> None:
        if phase == "after_journal_fsync":
            raise RuntimeError("leave journal")

    service.persistence.fault = inject
    with pytest.raises(RuntimeError, match="leave journal"):
        service.update_memory_record(
            str(saved["id"]),
            patch=persistence.MemoryPatch(what="after"),
            actor="dashboard",
        )
    service.persistence.fault = lambda _phase: None
    service.db.invalidate_vector(str(saved["id"]))

    with pytest.raises(TypeError, match="title"):
        service.update_memory_record(
            str(saved["id"]),
            patch=persistence.MemoryPatch(title=object()),
            actor="dashboard",
        )
    assert service.db.has_vector(str(saved["id"])) is True


def test_startup_repairs_vector_after_journal_removal_crash(
    service: MemoryService,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Repair queue", what="before"),
        project="p--1",
    )

    def inject(phase: str) -> None:
        if phase == "after_journal_remove":
            raise RuntimeError("crash after remove")

    service.persistence.fault = inject
    with pytest.raises(RuntimeError, match="crash after remove"):
        service.update_memory_record(
            str(saved["id"]),
            patch=persistence.MemoryPatch(what="after"),
            actor="dashboard",
        )
    assert service.db.has_vector(str(saved["id"])) is False
    assert list((Path(service.memory_home) / "transactions").glob("*.json")) == []
    service.close()

    recovered = MemoryService(service.memory_home)
    try:
        assert recovered.db.has_vector(str(saved["id"])) is True
    finally:
        recovered.close()


def test_delete_journal_rejects_final_document_containing_deleted_id(
    service: MemoryService,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Invalid delete", what="must disappear"),
        project="p--1",
    )
    target = Path(str(saved["file_path"]))
    prepared = prepare_atomic_text(target, target.read_text(encoding="utf-8"))
    operation = persistence.OperationJournal(
        schema_version=1,
        operation_id="83000000-0000-4000-8000-000000000001",
        action="delete",
        project_keys=("p--1",),
        affected_memory_ids=(str(saved["id"]),),
        canonical_memory_id=None,
        targets=(persistence.JournalTarget.from_prepared(Path(service.memory_home), prepared),),
        created_at="2026-07-14T10:00:00+00:00",
        actor="dashboard",
    )
    journal = persistence.persist_operation_journal(Path(service.memory_home), operation)

    with pytest.raises(persistence.JournalRecoveryConflict):
        service.persistence.recover_pending_operations(("p--1",))
    assert journal.exists()
    assert service.get_memory_record(str(saved["id"])) is not None


def test_doctor_on_missing_home_is_read_only(tmp_path: Path) -> None:
    memory_home = tmp_path / "missing-home"
    result = CliRunner().invoke(
        main,
        ["doctor"],
        env={"MEMORY_HOME": str(memory_home)},
    )
    assert result.exit_code == 0
    assert memory_home.exists() is False


def test_journal_loader_rejects_invalid_utf8(tmp_path: Path) -> None:
    memory_home = tmp_path / ".memory"
    transactions = memory_home / "transactions"
    transactions.mkdir(parents=True)
    journal = transactions / "84000000-0000-4000-8000-000000000001.json"
    journal.write_bytes(b"\xff\xfe")
    with pytest.raises(persistence.JournalRecoveryConflict):
        persistence.load_operation_journal(memory_home, journal)


def test_journal_loader_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFOs are not supported on this platform")
    memory_home = tmp_path / ".memory"
    transactions = memory_home / "transactions"
    transactions.mkdir(parents=True)
    journal = transactions / "84000000-0000-4000-8000-000000000002.json"
    os.mkfifo(journal)
    script = (
        "from pathlib import Path; "
        "from memory.persistence import load_operation_journal, JournalRecoveryConflict; "
        f"home=Path({str(memory_home)!r}); path=Path({str(journal)!r}); "
        "\ntry: load_operation_journal(home, path)\n"
        "except JournalRecoveryConflict: raise SystemExit(0)\n"
        "raise SystemExit(2)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        text=True,
        capture_output=True,
        timeout=2,
        check=False,
    )
    assert completed.returncode == 0


@pytest.mark.parametrize("literal_prefix", ["%", "_"])
def test_mutation_prefix_treats_sql_wildcards_literally(
    service: MemoryService,
    literal_prefix: str,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Literal prefix", what="must remain"),
        project="p--1",
    )
    assert service.delete(literal_prefix) is False
    assert service.get_memory_record(str(saved["id"])) is not None


def test_admin_delete_returns_canonical_id_for_unique_prefix(
    service: MemoryService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Canonical delete", what="return full id"),
        project="p--1",
    )
    monkeypatch.setenv("MEMORY_HOME", service.memory_home)
    prefix = str(saved["id"])[:12]
    result = CliRunner().invoke(
        main,
        ["admin", "apply", "--json-stdin"],
        input=json.dumps(
            {"action": "delete", "memory_id": prefix, "actor": "dashboard"}
        ),
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "status": "deleted",
        "memory_id": saved["id"],
    }


def test_duplicate_save_update_uses_journal_and_recovers(
    service: MemoryService,
) -> None:
    first = service.save(
        RawMemoryInput(title="Duplicate journal", what="before"),
        project="p--1",
    )
    operation_id = "85000000-0000-4000-8000-000000000001"

    def inject(phase: str) -> None:
        if phase == "after_journal_fsync":
            raise RuntimeError("durable duplicate journal")

    service.persistence.fault = inject
    with pytest.raises(RuntimeError, match="durable duplicate journal"):
        service.save(
            RawMemoryInput(title="Duplicate journal", what="after"),
            project="p--1",
            authoritative_source="cursor",
            idempotency_key=operation_id,
        )
    service.close()

    recovered = MemoryService(service.memory_home)
    try:
        row = recovered.get_memory_record(str(first["id"]))
        assert row is not None and row["what"] == "after"
        operation = recovered.db.get_operation("p--1", operation_id)
        assert operation is not None and operation["memory_id"] == first["id"]
    finally:
        recovered.close()


def test_journal_publication_collision_race_preserves_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("dir-fd race seam is POSIX-specific")
    memory_home = tmp_path / ".memory"
    memory_home.mkdir()
    operation_id = "85000000-0000-4000-8000-000000000002"
    operation = persistence.OperationJournal(
        schema_version=1,
        operation_id=operation_id,
        action="update",
        project_keys=("p--1",),
        affected_memory_ids=("memory-id",),
        canonical_memory_id="memory-id",
        targets=(
            persistence.JournalTarget(
                target="vault/p--1/2026-07-14-session.md",
                temporary="vault/p--1/.2026-07-14-session.md.test.tmp",
                before_sha256=None,
                after_sha256="0" * 64,
            ),
        ),
        created_at="2026-07-14T10:00:00+00:00",
    )
    original_link = os.link
    injected = False

    def racing_link(src, dst, *args, **kwargs):
        nonlocal injected
        if not injected and dst == f"{operation_id}.json":
            injected = True
            descriptor = os.open(
                dst,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=kwargs["dst_dir_fd"],
            )
            try:
                os.write(descriptor, b"winner")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return original_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", racing_link)
    with pytest.raises(persistence.JournalRecoveryConflict):
        persistence.persist_operation_journal(memory_home, operation)
    journal = memory_home / "transactions" / f"{operation_id}.json"
    assert journal.read_bytes() == b"winner"


def test_journal_loader_rejects_oversized_regular_file(tmp_path: Path) -> None:
    memory_home = tmp_path / ".memory"
    transactions = memory_home / "transactions"
    transactions.mkdir(parents=True)
    journal = transactions / "85000000-0000-4000-8000-000000000003.json"
    journal.write_bytes(b"{" + b" " * 1_048_576 + b"}")
    with pytest.raises(persistence.JournalRecoveryConflict):
        persistence.load_operation_journal(memory_home, journal)


def test_doctor_reports_valid_recoverable_journal_as_pending(
    service: MemoryService,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Doctor pending", what="before"),
        project="p--1",
    )

    def inject(phase: str) -> None:
        if phase == "after_journal_fsync":
            raise RuntimeError("leave pending")

    service.persistence.fault = inject
    with pytest.raises(RuntimeError, match="leave pending"):
        service.update_memory_record(
            str(saved["id"]),
            patch=persistence.MemoryPatch(what="after"),
            actor="dashboard",
        )
    report = doctor(service)
    journal = next((Path(service.memory_home) / "transactions").glob("*.json"))
    assert report["operation_journals"] == [
        {"type": "pending_operation_journal", "operation_id": journal.stem}
    ]


def test_doctor_existing_home_changes_no_files(
    service: MemoryService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.save(
        RawMemoryInput(title="Read only doctor", what="inspect only"),
        project="p--1",
    )
    memory_home = Path(service.memory_home)
    service.close()

    def snapshot() -> dict[str, tuple[int, int, int, str | None]]:
        result: dict[str, tuple[int, int, int, str | None]] = {}
        for path in sorted(memory_home.rglob("*")):
            metadata = path.lstat()
            digest = (
                hashlib.sha256(path.read_bytes()).hexdigest()
                if path.is_file() and not path.is_symlink()
                else None
            )
            result[path.relative_to(memory_home).as_posix()] = (
                metadata.st_mode,
                metadata.st_mtime_ns,
                metadata.st_size,
                digest,
            )
        return result

    before = snapshot()
    monkeypatch.setenv("MEMORY_HOME", str(memory_home))
    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 0
    assert snapshot() == before


def test_doctor_reports_symlinked_transactions_without_following(
    tmp_path: Path,
) -> None:
    memory_home = tmp_path / ".memory"
    memory_home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("unchanged", encoding="utf-8")
    try:
        (memory_home / "transactions").symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are not supported on this platform")
    result = CliRunner().invoke(
        main,
        ["doctor"],
        env={"MEMORY_HOME": str(memory_home)},
    )
    assert result.exit_code == 0
    assert "journal_recovery_conflict" in result.stdout
    assert sentinel.read_text(encoding="utf-8") == "unchanged"


def test_delete_recovery_preserves_actor_in_operation_ledger(
    service: MemoryService,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Delete actor", what="preserve provenance"),
        project="p--1",
    )

    def inject(phase: str) -> None:
        if phase == "after_journal_fsync":
            raise RuntimeError("leave delete")

    service.persistence.fault = inject
    with pytest.raises(RuntimeError, match="leave delete"):
        service.delete(str(saved["id"]), actor="dashboard")
    journal = next((Path(service.memory_home) / "transactions").glob("*.json"))
    operation_id = journal.stem
    service.close()

    recovered = MemoryService(service.memory_home)
    try:
        assert recovered.get_memory_record(str(saved["id"])) is None
        operation = recovered.db.get_operation("p--1", operation_id)
        assert operation is not None
        assert operation["action"] == "deleted"
        assert operation["source"] == "dashboard"
    finally:
        recovered.close()


def test_pending_vector_repair_survives_provider_failure(
    service: MemoryService,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Durable vector retry", what="before"),
        project="p--1",
    )

    def inject(phase: str) -> None:
        if phase == "after_journal_remove":
            raise RuntimeError("leave repair")

    service.persistence.fault = inject
    with pytest.raises(RuntimeError, match="leave repair"):
        service.update_memory_record(
            str(saved["id"]),
            patch=persistence.MemoryPatch(what="after"),
            actor="dashboard",
        )
    service.persistence.fault = lambda _phase: None
    assert service.db.list_vector_repairs()[0]["memory_id"] == saved["id"]
    service.persistence.embed = lambda _text: (_ for _ in ()).throw(
        RuntimeError("provider unavailable")
    )
    assert service.persistence.repair_pending_vectors() == ()
    assert len(service.db.list_vector_repairs()) == 1

    service.persistence.embed = lambda _text: [0.0] * 768
    assert service.persistence.repair_pending_vectors() == (saved["id"],)
    assert service.db.list_vector_repairs() == []


def test_vector_repair_survives_journal_directory_fsync_failure(
    service: MemoryService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = service.save(
        RawMemoryInput(title="Fsync repair", what="before"),
        project="p--1",
    )
    original_fsync = persistence.fsync_directory

    def fail_transactions(path: Path) -> None:
        if path.name == "transactions":
            raise OSError("transactions fsync failed")
        original_fsync(path)

    monkeypatch.setattr(persistence, "fsync_directory", fail_transactions)
    with pytest.raises(OSError, match="transactions fsync failed"):
        service.update_memory_record(
            str(saved["id"]),
            patch=persistence.MemoryPatch(what="after"),
            actor="dashboard",
        )
    assert list((Path(service.memory_home) / "transactions").glob("*.json")) == []
    assert service.db.list_vector_repairs()[0]["memory_id"] == saved["id"]


def test_hard_process_termination_after_target_replace_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_home = tmp_path / ".memory"
    operation_id = "86000000-0000-4000-8000-000000000001"
    script = (
        "import os\n"
        "from memory.core import MemoryService\n"
        "from memory.models import RawMemoryInput\n"
        f"service=MemoryService({str(memory_home)!r})\n"
        "service.persistence.fault=lambda phase: "
        "os._exit(73) if phase == 'after_target_replace:0' else None\n"
        "service.save(RawMemoryInput(title='Hard crash', what='recover'), "
        f"project='p--1', idempotency_key={operation_id!r})\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "MEMORY_HOME": str(memory_home)},
    )
    assert completed.returncode == 73
    assert len(list((memory_home / "transactions").glob("*.json"))) == 1

    class TinyEmbedding:
        def embed(self, _text: str) -> list[float]:
            return [0.0, 0.0, 0.0, 0.0]

    monkeypatch.setattr(
        MemoryService,
        "_create_embedding_provider",
        lambda _self: TinyEmbedding(),
    )
    recovered = MemoryService(str(memory_home))
    try:
        rows = recovered.list_memories(project="p--1")
        assert len(rows) == 1 and rows[0]["what"] == "recover"
        assert recovered.db.get_operation("p--1", operation_id) is not None
        assert list((memory_home / "transactions").glob("*.json")) == []
    finally:
        recovered.close()


def test_doctor_does_not_migrate_an_old_database(tmp_path: Path) -> None:
    memory_home = tmp_path / ".memory"
    memory_home.mkdir()
    database_path = memory_home / "index.db"
    connection = sqlite3.connect(database_path)
    connection.execute("CREATE TABLE legacy_only (value TEXT)")
    connection.execute("INSERT INTO legacy_only VALUES ('sentinel')")
    connection.commit()
    connection.close()
    before = database_path.read_bytes()

    result = CliRunner().invoke(
        main,
        ["doctor"],
        env={"MEMORY_HOME": str(memory_home)},
    )
    assert result.exit_code == 0
    assert "database_error" in result.stdout
    assert database_path.read_bytes() == before
    assert sorted(path.name for path in memory_home.iterdir()) == ["index.db"]


def test_doctor_reads_live_wal_via_private_snapshot_without_writes(
    service: MemoryService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.save(
        RawMemoryInput(title="Live WAL", what="visible read only"),
        project="p--1",
    )
    memory_home = Path(service.memory_home)
    wal_path = Path(str(memory_home / "index.db") + "-wal")
    assert wal_path.exists() and wal_path.stat().st_size > 0

    def file_snapshot() -> dict[str, tuple[int, str]]:
        result: dict[str, tuple[int, str]] = {}
        for path in sorted(memory_home.rglob("*")):
            if path.is_file() and not path.is_symlink():
                payload = path.read_bytes()
                result[path.relative_to(memory_home).as_posix()] = (
                    path.stat().st_mtime_ns,
                    hashlib.sha256(payload).hexdigest(),
                )
        return result

    before = file_snapshot()
    monkeypatch.setenv("MEMORY_HOME", str(memory_home))
    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 0
    assert "memories: 1" in result.stdout
    assert file_snapshot() == before
