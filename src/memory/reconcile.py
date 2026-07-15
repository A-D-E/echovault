"""Explicit reconciliation and migration helpers for canonical vault state."""

from __future__ import annotations

import hashlib
import json
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from memory.markdown import (
    SessionDocument,
    SessionEntry,
    _entry_from_memory,
    assign_entry_anchors,
    parse_session_file,
)
from memory.models import Memory, MemoryOperation
from memory.persistence import JournalRecoveryConflict, content_fingerprint
from memory.projects import ProjectResolutionError
from memory.safe_io import digest_file, prepare_atomic_text

if TYPE_CHECKING:
    from memory.core import MemoryService


@dataclass(frozen=True)
class _PreparedVaultMigration:
    path: Path
    relative_path: str
    document: SessionDocument
    rendered: str
    memories: tuple[tuple[Memory, str | None], ...]
    previous_fingerprints: dict[str, str | None]


class _UnresolvedLegacyMetadata(ValueError):
    def __init__(self, anchor: str | None, reason: str) -> None:
        super().__init__(reason)
        self.anchor = anchor
        self.reason = reason


class _MigrationScope(Protocol):
    canonical_key: str
    storage_keys: tuple[str, ...]


def _json_list(row: dict[str, object], field: str) -> list[str]:
    raw = row.get(field)
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid {field}") from error
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise ValueError(f"invalid {field}")
    return list(raw)


def _json_object(row: dict[str, object], field: str) -> dict[str, object]:
    raw = row.get(field)
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid {field}") from error
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise ValueError(f"invalid {field}")
    return dict(raw)


def _required_string(row: dict[str, object], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid {field}")
    return value


def _optional_string(row: dict[str, object], field: str) -> str | None:
    value = row.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"invalid {field}")
    return value


def _normalized_row_path(
    row: dict[str, object],
    vault_root: Path,
) -> str | None:
    raw_path = row.get("file_path")
    row_project = row.get("project")
    if (
        not isinstance(raw_path, str)
        or not raw_path
        or not isinstance(row_project, str)
    ):
        return None
    path = Path(raw_path)
    if not path.is_absolute():
        if len(path.parts) > 1 and path.parts[0] == row_project:
            path = vault_root / path
        else:
            path = vault_root / row_project / path
    resolved = path.resolve(strict=False)
    try:
        return resolved.relative_to(vault_root).as_posix()
    except ValueError:
        return None


def _match_row(
    entry: SessionEntry,
    rows: list[dict[str, object]],
    *,
    relative_path: str,
    vault_root: Path,
    entry_count: int,
    used_ids: set[str],
) -> dict[str, object]:
    anchor = entry.section_anchor
    if entry.id:
        matches = [row for row in rows if row.get("id") == entry.id]
        if len(matches) != 1:
            reason = (
                "no exact stable-ID match"
                if not matches
                else "ambiguous stable-ID match"
            )
            raise _UnresolvedLegacyMetadata(anchor, reason)
    else:
        path_matches = [
            row
            for row in rows
            if _normalized_row_path(row, vault_root) == relative_path
        ]
        anchored_path_matches = [
            row for row in path_matches if row.get("section_anchor") == anchor
        ]
        if len(anchored_path_matches) == 1:
            matches = anchored_path_matches
        elif len(anchored_path_matches) > 1:
            raise _UnresolvedLegacyMetadata(anchor, "ambiguous file-and-anchor match")
        elif len(path_matches) == 1 and entry_count == 1:
            matches = path_matches
        else:
            anchor_matches = [row for row in rows if row.get("section_anchor") == anchor]
            if len(anchor_matches) != 1:
                reason = (
                    "no matching SQLite row"
                    if not anchor_matches
                    else "ambiguous project anchor"
                )
                raise _UnresolvedLegacyMetadata(anchor, reason)
            matches = anchor_matches

    row = matches[0]
    row_id = row.get("id")
    if not isinstance(row_id, str) or row_id in used_ids:
        raise _UnresolvedLegacyMetadata(anchor, "SQLite row is not uniquely assignable")
    try:
        canonical_id = str(uuid.UUID(row_id))
    except ValueError as error:
        raise _UnresolvedLegacyMetadata(anchor, "SQLite memory ID is not a UUID") from error
    if canonical_id != row_id:
        raise _UnresolvedLegacyMetadata(anchor, "SQLite memory ID is not canonical")
    used_ids.add(row_id)
    return row


def _migration_operation_id(
    project: str,
    relative_path: str,
    identity: str,
) -> str:
    payload = json.dumps(
        [project, relative_path, identity, 2],
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _living_or_row(
    entry: SessionEntry,
    living_field: str,
    row: dict[str, object],
    row_field: str,
) -> object:
    if living_field in entry.living_data:
        return entry.living_data[living_field]
    return row.get(row_field)


def _memory_from_legacy_entry(
    entry: SessionEntry,
    row: dict[str, object],
    *,
    project: str,
    path: Path,
    relative_path: str,
) -> Memory:
    row_source = _optional_string(row, "source") or None
    entry_source = entry.source if entry.source else None
    if entry_source is not None and row_source is not None and entry_source != row_source:
        raise _UnresolvedLegacyMetadata(
            entry.section_anchor,
            "canonical and indexed source identities conflict",
        )
    source = entry_source if entry_source is not None else row_source
    if source == "":
        source = None

    memory_id = _required_string(row, "id")
    created_at = _required_string(row, "created_at")
    updated_at = _required_string(row, "updated_at")
    identity = entry.id or entry.section_anchor
    if not isinstance(identity, str) or not identity:
        raise _UnresolvedLegacyMetadata(entry.section_anchor, "entry has no stable identity")
    operation_id = _migration_operation_id(project, relative_path, identity)

    updated_count = row.get("updated_count")
    if updated_count is None:
        updated_count = 0
    if isinstance(updated_count, bool) or not isinstance(updated_count, int):
        raise _UnresolvedLegacyMetadata(entry.section_anchor, "invalid updated_count")

    if "structured_data" in entry.living_data:
        structured_data = entry.living_data["structured_data"]
    else:
        structured_data = _json_object(row, "structured_data")
    if not isinstance(structured_data, dict):
        raise _UnresolvedLegacyMetadata(entry.section_anchor, "invalid structured_data")

    if "links" in entry.living_data:
        links_value = entry.living_data["links"]
    else:
        links_value = None
    if links_value is None:
        links = _json_list(row, "links")
    elif isinstance(links_value, list) and all(
        isinstance(item, str) for item in links_value
    ):
        links = list(links_value)
    else:
        raise _UnresolvedLegacyMetadata(entry.section_anchor, "invalid links")

    def living_optional_string(name: str) -> str | None:
        value = _living_or_row(entry, name, row, name)
        if value is None or isinstance(value, str):
            return value
        raise _UnresolvedLegacyMetadata(entry.section_anchor, f"invalid {name}")

    confidence = _living_or_row(entry, "confidence", row, "confidence")
    if confidence is not None and (
        isinstance(confidence, bool) or not isinstance(confidence, (int, float))
    ):
        raise _UnresolvedLegacyMetadata(entry.section_anchor, "invalid confidence")

    category = _optional_string(row, "category") or entry.category
    status = _optional_string(row, "status") or entry.status or "active"
    commit_sha = living_optional_string("commit_sha")
    branch = living_optional_string("branch")
    memory = Memory(
        id=memory_id,
        title=entry.title,
        what=entry.what,
        why=entry.why,
        impact=entry.impact,
        tags=_json_list(row, "tags"),
        category=category,
        project=project,
        source=source,
        related_files=_json_list(row, "related_files"),
        file_path=str(path),
        section_anchor=entry.section_anchor or "",
        created_at=created_at,
        updated_at=updated_at,
        status=status,
        archived_at=entry.archived_at or _optional_string(row, "archived_at"),
        archive_reason=entry.archive_reason or _optional_string(row, "archive_reason"),
        superseded_by=entry.superseded_by or _optional_string(row, "superseded_by"),
        structured_data=dict(structured_data),
        confidence=confidence,
        valid_from=living_optional_string("valid_from"),
        valid_until=living_optional_string("valid_until"),
        commit_sha=commit_sha,
        branch=branch,
        links=links,
        last_verified=living_optional_string("last_verified"),
        creator_source=source,
        last_updated_by=source,
        contributors=[source] if source is not None else [],
        operations=[
            MemoryOperation(
                operation_id=operation_id,
                source=source,
                action="migrated",
                request_fingerprint="migration:" + operation_id,
                timestamp=updated_at,
                branch=branch,
                commit_sha=commit_sha,
            )
        ],
        content_fingerprint=None,
        history_complete=False,
        updated_count=updated_count,
    )
    memory.content_fingerprint = content_fingerprint(memory)
    return memory


def _scope_rows(
    service: MemoryService,
    storage_keys: tuple[str, ...],
) -> list[dict[str, object]]:
    placeholders = ", ".join("?" for _ in storage_keys)
    cursor = service.db.conn.execute(
        f"SELECT * FROM memories WHERE project IN ({placeholders}) ORDER BY id",
        storage_keys,
    )
    return [dict(row) for row in cursor.fetchall()]


def _prepare_file(
    service: MemoryService,
    path: Path,
    *,
    canonical_project: str,
    storage_keys: tuple[str, ...],
) -> _PreparedVaultMigration | None:
    vault_root = Path(service.vault_dir).resolve()
    try:
        path_metadata = path.lstat()
    except OSError as error:
        raise _UnresolvedLegacyMetadata(None, "file is not a local regular file") from error
    if path.is_symlink() or not stat.S_ISREG(path_metadata.st_mode):
        raise _UnresolvedLegacyMetadata(None, "file is not a local regular file")
    resolved_path = path.resolve(strict=False)
    try:
        relative_path = resolved_path.relative_to(vault_root).as_posix()
    except ValueError as error:
        raise _UnresolvedLegacyMetadata(None, "file escapes the vault") from error
    if resolved_path != path:
        raise _UnresolvedLegacyMetadata(None, "file changes identity through its path")
    digest_file(resolved_path)
    document = parse_session_file(resolved_path)
    if document.schema_version == 2:
        return None
    if document.project not in {*storage_keys, canonical_project}:
        raise _UnresolvedLegacyMetadata(None, "frontmatter project is outside the scope")

    rows = _scope_rows(service, storage_keys)
    used_ids: set[str] = set()
    memories: list[tuple[Memory, str | None]] = []
    entries: list[SessionEntry] = []
    previous_fingerprints: dict[str, str | None] = {}
    for entry in document.entries:
        row = _match_row(
            entry,
            rows,
            relative_path=relative_path,
            vault_root=vault_root,
            entry_count=len(document.entries),
            used_ids=used_ids,
        )
        try:
            memory = _memory_from_legacy_entry(
                entry,
                row,
                project=canonical_project,
                path=resolved_path,
                relative_path=relative_path,
            )
        except _UnresolvedLegacyMetadata:
            raise
        except (TypeError, ValueError) as error:
            raise _UnresolvedLegacyMetadata(
                entry.section_anchor,
                "indexed metadata is malformed",
            ) from error
        memories.append((memory, entry.details))
        entries.append(_entry_from_memory(memory, entry.details))
        previous = row.get("content_fingerprint")
        previous_fingerprints[memory.id] = previous if isinstance(previous, str) else None

    if not entries:
        raise _UnresolvedLegacyMetadata(None, "legacy file contains no memory entries")
    assign_entry_anchors(entries)
    for (memory, _details), entry in zip(memories, entries):
        if entry.section_anchor is None:
            raise _UnresolvedLegacyMetadata(None, "unable to assign a stable anchor")
        memory.section_anchor = entry.section_anchor
        entry.metadata["section_anchor"] = entry.section_anchor

    migrated_document = SessionDocument(
        project=canonical_project,
        created=document.created,
        tags=[],
        sources=[],
        title=document.title,
        entries=entries,
        schema_version=2,
    )
    try:
        rendered = service.persistence._render_document(migrated_document)
    except (TypeError, ValueError) as error:
        raise _UnresolvedLegacyMetadata(
            None,
            "enriched metadata cannot be represented losslessly",
        ) from error
    return _PreparedVaultMigration(
        path=resolved_path,
        relative_path=relative_path,
        document=migrated_document,
        rendered=rendered,
        memories=tuple(memories),
        previous_fingerprints=previous_fingerprints,
    )


def _commit_file(
    service: MemoryService,
    migration: _PreparedVaultMigration,
) -> tuple[tuple[str, str, bytes], ...]:
    persistence = service.persistence
    before_digest = digest_file(migration.path)
    prepared = prepare_atomic_text(migration.path, migration.rendered)
    embeddings: list[tuple[str, str, bytes]] = []
    try:
        prepared_digest = digest_file(prepared.temporary)
        persistence.fault("after_all_temps_fsync")
        with service.db.transaction():
            memory_ids = tuple(memory.id for memory, _details in migration.memories)
            placeholders = ", ".join("?" for _ in memory_ids)
            service.db.conn.execute(
                f"DELETE FROM save_operations WHERE memory_id IN ({placeholders})",
                memory_ids,
            )
            for memory, details in migration.memories:
                fingerprint = str(memory.content_fingerprint)
                fingerprint_changed = (
                    migration.previous_fingerprints[memory.id] != fingerprint
                )
                had_vector = service.db.has_vector(memory.id)
                service.db.upsert_memory(memory, details)
                service.db.upsert_operation(
                    memory.project,
                    memory.id,
                    memory.operations[0],
                )
                if fingerprint_changed or memory.status != "active":
                    service.db.invalidate_vector(memory.id)
                    service.db.clear_vector_repair(memory.id)
                if memory.status == "active" and (fingerprint_changed or not had_vector):
                    service.db.queue_vector_repair(
                        memory.id,
                        fingerprint,
                        memory.updated_at,
                    )
                    embeddings.append(
                        (memory.id, fingerprint, persistence._embedding_bytes(memory))
                    )
            persistence.fault("after_db_write")
            prepared.replace_if_digest(before_digest, prepared_digest)
            persistence.fault("after_target_replace:0")
    except BaseException:
        prepared.discard()
        raise
    persistence.fault("after_db_commit")
    return tuple(embeddings)


def _discover_scopes(
    service: MemoryService,
    project: str | None,
) -> list[_MigrationScope]:
    persistence = service.persistence
    if project is not None:
        return [persistence._resolve_scope(project)]
    vault_root = Path(service.vault_dir)
    if not vault_root.exists():
        return []
    scopes: dict[str, _MigrationScope] = {}
    for candidate in sorted(vault_root.iterdir(), key=lambda item: item.name):
        if candidate.name.startswith(".") or not candidate.is_dir():
            continue
        scope = persistence._resolve_scope(candidate.name)
        scopes.setdefault(scope.canonical_key, scope)
    return [scopes[key] for key in sorted(scopes)]


def _scope_files(service: MemoryService, scope: _MigrationScope) -> list[Path]:
    files: list[Path] = []
    for storage_key in scope.storage_keys:
        project_dir = service.persistence._project_dir(storage_key)
        if not project_dir.exists():
            continue
        metadata = project_dir.lstat()
        if project_dir.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise OSError("vault project directory is not a local directory")
        files.extend(sorted(project_dir.glob("*-session.md")))
    return sorted(set(files), key=lambda item: str(item))


def _unresolved_item(
    relative_path: str,
    error: _UnresolvedLegacyMetadata,
) -> dict[str, str | None]:
    return {
        "file": relative_path,
        "anchor": error.anchor,
        "reason": error.reason,
    }


def migrate_vault_metadata(
    service: MemoryService,
    project: str | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    """Explicitly enrich schema-v1 files from unambiguous derived rows."""
    result: dict[str, object] = {
        "migrated": 0,
        "would_migrate": 0,
        "skipped": 0,
        "unresolved": [],
        "dry_run": dry_run,
    }
    unresolved = result["unresolved"]
    assert isinstance(unresolved, list)
    vault_root = Path(service.vault_dir).resolve()

    for scope in _discover_scopes(service, project):
        files = _scope_files(service, scope)
        if dry_run:
            try:
                _closure, journals = service.persistence._journal_lock_closure(
                    (scope.canonical_key,)
                )
            except (JournalRecoveryConflict, ProjectResolutionError, OSError):
                unresolved.append(
                    {
                        "file": "",
                        "anchor": None,
                        "reason": "pending operation state is unreadable",
                    }
                )
                continue
            if journals:
                unresolved.append(
                    {
                        "file": "",
                        "anchor": None,
                        "reason": "pending operation recovery required before migration",
                    }
                )
                continue

        for path in files:
            try:
                relative_path = path.relative_to(vault_root).as_posix()
            except ValueError:
                unresolved.append(
                    {
                        "file": path.name,
                        "anchor": None,
                        "reason": "file escapes the vault",
                    }
                )
                continue

            def inspect_current() -> _PreparedVaultMigration | None:
                try:
                    before = digest_file(path)
                except OSError:
                    unresolved.append(
                        {
                            "file": relative_path,
                            "anchor": None,
                            "reason": "file is not a local regular file",
                        }
                    )
                    return None
                try:
                    prepared_migration = _prepare_file(
                        service,
                        path,
                        canonical_project=scope.canonical_key,
                        storage_keys=scope.storage_keys,
                    )
                except _UnresolvedLegacyMetadata as error:
                    unresolved.append(_unresolved_item(relative_path, error))
                    return None
                if dry_run and digest_file(path) != before:
                    unresolved.append(
                        {
                            "file": relative_path,
                            "anchor": None,
                            "reason": "file changed during dry-run inspection",
                        }
                    )
                    return None
                return prepared_migration

            if dry_run:
                unresolved_before = len(unresolved)
                migration = inspect_current()
                if migration is None:
                    if len(unresolved) == unresolved_before:
                        result["skipped"] = int(result["skipped"]) + 1
                    continue
                result["would_migrate"] = int(result["would_migrate"]) + 1
                continue

            embeddings: tuple[tuple[str, str, bytes], ...] = ()
            with service.persistence._locked_after_recovery((scope.canonical_key,)):
                unresolved_before = len(unresolved)
                migration = inspect_current()
                if migration is None:
                    if len(unresolved) == unresolved_before:
                        result["skipped"] = int(result["skipped"]) + 1
                    continue
                result["would_migrate"] = int(result["would_migrate"]) + 1
                embeddings = _commit_file(service, migration)
            vector_result = service.persistence._finish_mutation({}, embeddings)
            warning = vector_result.get("warning")
            if isinstance(warning, str):
                result.setdefault("warnings", []).append(warning)
            result["migrated"] = int(result["migrated"]) + 1

    return result
