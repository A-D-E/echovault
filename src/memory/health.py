"""Living-memory lifecycle review and health diagnostics."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
import shutil
import stat
import tempfile
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path


def _query_privacy_report(
    memory_home: Path,
    config: object | None = None,
) -> dict[str, object]:
    if config is None:
        from memory.config import load_config

        try:
            config = load_config(str(memory_home / "config.yaml"))
        except Exception:
            config = None

    embedding = getattr(config, "embedding", None)
    context = getattr(config, "context", None)
    provider_scope = (
        "remote"
        if getattr(embedding, "provider", None) == "openai"
        else "local"
    )
    allowed = bool(
        getattr(context, "allow_remote_query_embeddings", False)
    )
    if provider_scope == "local":
        effective = "local"
    elif allowed:
        effective = "remote_redacted"
    else:
        effective = "fts_only"
    return {
        "provider_scope": provider_scope,
        "allow_remote_query_embeddings": allowed,
        "automatic_query_embedding": effective,
    }


def _vault_metadata_diagnostics(
    memory_home: Path,
    project: str | None,
) -> dict[str, object]:
    """Inspect session schema versions without mutating vault or index state."""
    from memory.markdown import parse_session_file
    from memory.projects import (
        ProjectRegistry,
        ProjectResolutionError,
        validate_storage_key,
    )
    from memory.safe_io import digest_file

    schema_v1_files: list[str] = []
    schema_v2_files: list[str] = []
    unreadable_files: list[str] = []
    vault_root = memory_home.resolve() / "vault"
    if not os.path.lexists(vault_root):
        return {
            "schema_v1_files": schema_v1_files,
            "schema_v2_files": schema_v2_files,
            "unreadable_files": unreadable_files,
            "migration_command": None,
        }
    try:
        vault_metadata = vault_root.lstat()
    except OSError:
        vault_metadata = None
    if (
        vault_metadata is None
        or vault_root.is_symlink()
        or not stat.S_ISDIR(vault_metadata.st_mode)
    ):
        unreadable_files.append("<vault-root>")
        return {
            "schema_v1_files": schema_v1_files,
            "schema_v2_files": schema_v2_files,
            "unreadable_files": unreadable_files,
            "migration_command": None,
        }

    if project is None:
        project_dirs = sorted(vault_root.iterdir(), key=lambda path: path.name)
    else:
        try:
            storage_key = validate_storage_key(project)
            scope = ProjectRegistry(memory_home.resolve()).resolve(storage_key)
            storage_keys = scope.storage_keys if scope is not None else (storage_key,)
        except (OSError, ProjectResolutionError):
            unreadable_files.append("<invalid-project>")
            return {
                "schema_v1_files": schema_v1_files,
                "schema_v2_files": schema_v2_files,
                "unreadable_files": unreadable_files,
                "migration_command": None,
            }
        project_dirs = [vault_root / storage_key for storage_key in storage_keys]
    for project_dir in project_dirs:
        if not os.path.lexists(project_dir):
            continue
        relative_project = project_dir.name
        try:
            metadata = project_dir.lstat()
            if project_dir.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
                unreadable_files.append(relative_project)
                continue
            candidates = sorted(project_dir.glob("*-session.md"))
        except OSError:
            unreadable_files.append(relative_project)
            continue
        for path in candidates:
            relative = f"{relative_project}/{path.name}"
            try:
                metadata = path.lstat()
                if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                    raise OSError("session path is not a regular file")
                digest_file(path)
                document = parse_session_file(path)
            except (OSError, UnicodeError, ValueError):
                unreadable_files.append(relative)
                continue
            if document.schema_version == 1:
                schema_v1_files.append(relative)
            else:
                schema_v2_files.append(relative)

    return {
        "schema_v1_files": schema_v1_files,
        "schema_v2_files": schema_v2_files,
        "unreadable_files": unreadable_files,
        "migration_command": (
            "memory migrate vault-metadata" if schema_v1_files else None
        ),
    }


def _operation_journal_diagnostics(
    memory_home: Path,
    project: str | None,
) -> list[dict[str, str]]:
    from memory.persistence import (
        JournalRecoveryConflict,
        inspect_operation_journal_state,
        load_operation_journal,
    )

    transactions = memory_home.resolve() / "transactions"
    if not os.path.lexists(transactions):
        return []
    try:
        metadata = transactions.lstat()
    except OSError:
        return [{"type": "journal_recovery_conflict", "operation_id": "transactions"}]
    if transactions.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        return [{"type": "journal_recovery_conflict", "operation_id": "transactions"}]

    diagnostics: list[dict[str, str]] = []
    try:
        journal_paths = sorted(
            path for path in transactions.iterdir() if path.suffix == ".json"
        )
    except OSError:
        return [{"type": "journal_recovery_conflict", "operation_id": "transactions"}]
    for journal_path in journal_paths:
        try:
            operation = load_operation_journal(memory_home, journal_path)
            inspect_operation_journal_state(memory_home, operation)
        except JournalRecoveryConflict:
            diagnostics.append(
                {
                    "type": "journal_recovery_conflict",
                    "operation_id": journal_path.stem,
                }
            )
            continue
        if project is None or project in operation.project_keys:
            diagnostics.append(
                {
                    "type": "pending_operation_journal",
                    "operation_id": operation.operation_id,
                }
            )
    return diagnostics


def _empty_doctor_report(
    memory_home: Path,
    project: str | None,
    *,
    database_error: str | None = None,
) -> dict:
    operation_journals = _operation_journal_diagnostics(memory_home, project)
    vault_metadata = _vault_metadata_diagnostics(memory_home, project)
    vault_warnings = bool(
        vault_metadata["schema_v1_files"] or vault_metadata["unreadable_files"]
    )
    report = {
        "status": (
            "warning"
            if operation_journals or database_error or vault_warnings
            else "ok"
        ),
        "memories": 0,
        "active": 0,
        "missing_markdown_files": 0,
        "orphaned_details": 0,
        "broken_absolute_related_files": 0,
        "vectors": {"available": False, "rows": 0, "missing": 0},
        "embedding_dimension": None,
        "query_privacy": _query_privacy_report(memory_home),
        "lifecycle_counts": {
            key: 0
            for key in (
                "duplicates",
                "contradictions",
                "stale",
                "superseded",
                "completed_followups",
                "broad",
            )
        },
        "operation_journals": operation_journals,
        "vault_metadata": vault_metadata,
        "findings": [],
    }
    if database_error is not None:
        report["database_error"] = database_error
    return report


def lifecycle_review(db, project: str | None = None) -> dict[str, list]:
    memories = db.list_memories(limit=10000, project=project, include_archived=True)
    now = datetime.now(timezone.utc)
    report: dict[str, list] = {k: [] for k in ("duplicates", "contradictions", "stale", "superseded", "completed_followups", "broad")}
    active = [m for m in memories if (m.get("status") or "active") == "active"]
    titled = [
        (memory, memory["title"].lower(), Counter(memory["title"].lower()))
        for memory in active
    ]
    for i, (left, left_title, left_counts) in enumerate(titled):
        if left.get("valid_until"):
            try:
                if datetime.fromisoformat(left["valid_until"].replace("Z", "+00:00")) < now:
                    report["stale"].append(left["id"])
            except ValueError:
                pass
        if left.get("superseded_by"):
            report["superseded"].append({"id": left["id"], "by": left["superseded_by"]})
        try:
            structured = json.loads(left.get("structured_data") or "{}")
        except (TypeError, json.JSONDecodeError):
            structured = {}
        followups = structured.get("follow_ups", [])
        if followups and all(str(v).strip().lower().startswith(("done", "[x]", "complete")) for v in followups):
            report["completed_followups"].append(left["id"])
        if len(left.get("what", "")) > 800 or len(structured.get("steps", [])) > 20:
            report["broad"].append(left["id"])
        for right, right_title, right_counts in titled[i + 1:]:
            combined_length = len(left_title) + len(right_title)
            if (
                20 * min(len(left_title), len(right_title))
                < 9 * combined_length
            ):
                continue
            shared_characters = sum(
                (left_counts & right_counts).values()
            )
            if 20 * shared_characters < 9 * combined_length:
                continue
            title_ratio = SequenceMatcher(
                None,
                left_title,
                right_title,
            ).ratio()
            what_ratio = SequenceMatcher(None, left["what"].lower(), right["what"].lower()).ratio()
            if title_ratio >= 0.9 and what_ratio >= 0.75:
                report["duplicates"].append([left["id"], right["id"]])
            elif title_ratio >= 0.9 and what_ratio < 0.35:
                report["contradictions"].append([left["id"], right["id"]])
    return report


def doctor(
    service,
    project: str | None = None,
    *,
    agent: str | None = None,
    project_root: Path | None = None,
    runner=None,
) -> dict:
    from memory.reconcile import canonical_findings

    db = service.db
    memories = db.list_memories(limit=100000, project=project, include_archived=True)
    cursor = db.conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM memory_details d LEFT JOIN memories m ON m.id=d.memory_id WHERE m.id IS NULL")
    orphaned_details = cursor.fetchone()[0]
    missing_files = sum(1 for m in memories if not os.path.exists(m["file_path"]))
    broken_related = 0
    for m in memories:
        try:
            paths = json.loads(m.get("related_files") or "[]")
        except (TypeError, json.JSONDecodeError):
            paths = []
        broken_related += sum(1 for path in paths if os.path.isabs(path) and not os.path.exists(path))
    vector_rows = 0
    if db.has_vec_table():
        cursor.execute("SELECT COUNT(*) FROM memories_vec")
        vector_rows = cursor.fetchone()[0]
    active_count = sum(1 for m in memories if (m.get("status") or "active") == "active")
    lifecycle = lifecycle_review(db, project)
    operation_journals = _operation_journal_diagnostics(
        Path(service.memory_home),
        project,
    )
    vault_metadata = _vault_metadata_diagnostics(
        Path(service.memory_home),
        project,
    )
    vault_warnings = bool(
        vault_metadata["schema_v1_files"] or vault_metadata["unreadable_files"]
    )
    findings = canonical_findings(service, project)
    report = {
        "status": (
            "ok"
            if not (
                orphaned_details
                or missing_files
                or operation_journals
                or vault_warnings
                or findings
            )
            else "warning"
        ),
        "memories": len(memories), "active": active_count,
        "missing_markdown_files": missing_files, "orphaned_details": orphaned_details,
        "broken_absolute_related_files": broken_related,
        "vectors": {"available": db.has_vec_table(), "rows": vector_rows, "missing": max(0, len(memories) - vector_rows)},
        "embedding_dimension": db.get_embedding_dim(),
        "query_privacy": _query_privacy_report(
            Path(service.memory_home),
            getattr(service, "config", None),
        ),
        "lifecycle_counts": {key: len(value) for key, value in lifecycle.items()},
        "operation_journals": operation_journals,
        "vault_metadata": vault_metadata,
        "findings": findings,
    }
    if agent is not None:
        from memory.integrations.diagnostics import integration_diagnostics

        selected_root = (project_root or Path.cwd()).expanduser().resolve()
        report["integration_findings"] = list(
            integration_diagnostics(
                service,
                agent=agent,
                project_root=selected_root,
                runner=runner,
            )
        )
    return report


def _doctor_home_base(memory_home: Path, project: str | None = None) -> dict:
    """Inspect one memory home without creating or modifying any storage."""
    from memory.db import MemoryDB

    database_path = memory_home / "index.db"
    if not os.path.lexists(database_path):
        return _empty_doctor_report(memory_home, project)
    try:
        metadata = database_path.lstat()
    except OSError:
        return _empty_doctor_report(
            memory_home,
            project,
            database_error="index_unreadable",
        )
    if database_path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        return _empty_doctor_report(
            memory_home,
            project,
            database_error="index_not_regular",
        )

    def inspect_database(path: Path, *, immutable: bool) -> dict:
        from memory.persistence import CanonicalPersistence

        try:
            database = MemoryDB(
                str(path),
                read_only=True,
                immutable=immutable,
            )
        except Exception:
            return _empty_doctor_report(
                memory_home,
                project,
                database_error="index_read_failed",
            )
        persistence = CanonicalPersistence(memory_home.resolve(), database, [])
        service = type(
            "ReadOnlyDoctorService",
            (),
            {
                "db": database,
                "memory_home": str(memory_home),
                "vault_dir": str(memory_home.resolve() / "vault"),
                "persistence": persistence,
            },
        )()
        try:
            return doctor(service, project)
        except Exception:
            return _empty_doctor_report(
                memory_home,
                project,
                database_error="index_schema_unreadable",
            )
        finally:
            database.close()

    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(database_path) + suffix)
        if os.path.lexists(sidecar):
            try:
                sidecar_metadata = sidecar.lstat()
            except OSError:
                return _empty_doctor_report(
                    memory_home,
                    project,
                    database_error="index_sidecar_unreadable",
                )
            if sidecar.is_symlink() or not stat.S_ISREG(sidecar_metadata.st_mode):
                return _empty_doctor_report(
                    memory_home,
                    project,
                    database_error="index_sidecar_not_regular",
                )

    wal_path = Path(str(database_path) + "-wal")
    if not wal_path.exists() or wal_path.stat().st_size == 0:
        return inspect_database(database_path, immutable=True)

    # A live WAL must not be ignored.  Copy only across a verified stable
    # interval; a checkpoint between the database and WAL copies otherwise
    # produces a private database that never represented a real SQLite state.
    # The SHM wal-index is derived; omitting it makes SQLite rebuild a private
    # index instead of combining a transient shared-memory view with the copy.
    snapshot_paths = (
        database_path,
        Path(str(database_path) + "-wal"),
    )

    def snapshot_signature() -> tuple[tuple[str, int, int, str] | None, ...]:
        signatures: list[tuple[str, int, int, str] | None] = []
        for path in snapshot_paths:
            if not os.path.lexists(path):
                signatures.append(None)
                continue
            metadata = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                raise OSError("snapshot path is not a regular file")
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            final = path.lstat()
            if (
                (metadata.st_dev, metadata.st_ino)
                != (final.st_dev, final.st_ino)
                or metadata.st_size != final.st_size
                or metadata.st_mtime_ns != final.st_mtime_ns
            ):
                raise OSError("snapshot path changed while it was read")
            signatures.append(
                (str(path), final.st_size, final.st_mtime_ns, digest.hexdigest())
            )
        return tuple(signatures)

    for _attempt in range(3):
        try:
            before = snapshot_signature()
            with tempfile.TemporaryDirectory(prefix="echovault-doctor-") as temp_dir:
                copied_database = Path(temp_dir) / database_path.name
                for source in snapshot_paths:
                    if os.path.lexists(source):
                        suffix = str(source)[len(str(database_path)) :]
                        shutil.copy2(source, Path(str(copied_database) + suffix))
                after = snapshot_signature()
                if before != after:
                    continue
                return inspect_database(copied_database, immutable=False)
        except OSError:
            continue
    return _empty_doctor_report(
        memory_home,
        project,
        database_error="index_snapshot_unstable",
    )


def doctor_home(
    memory_home: Path,
    project: str | None = None,
    *,
    agent: str | None = None,
    project_root: Path | None = None,
    runner=None,
) -> dict:
    """Inspect storage and optional agent integration without writing state."""

    report = _doctor_home_base(memory_home, project)
    if agent is None:
        return report
    from types import SimpleNamespace

    from memory.config import load_config
    from memory.integrations.diagnostics import integration_diagnostics

    service = SimpleNamespace(
        memory_home=str(memory_home),
        config=load_config(str(memory_home / "config.yaml")),
    )
    selected_root = (project_root or Path.cwd()).expanduser().resolve()
    report["integration_findings"] = list(
        integration_diagnostics(
            service,
            agent=agent,
            project_root=selected_root,
            runner=runner,
        )
    )
    return report
