"""Explicit reconciliation and migration helpers for canonical vault state."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from memory.markdown import (
    ARCHIVED_HEADING,
    MEMORY_ID_PREFIX,
    SessionDocument,
    SessionEntry,
    _entry_from_memory,
    assign_entry_anchors,
    normalize_markdown_content,
    parse_session_file,
    read_markdown_text,
    render_session_document,
)
from memory.models import CATEGORY_HEADINGS, Memory, MemoryOperation
from memory.persistence import JournalRecoveryConflict, content_fingerprint
from memory.projects import ProjectResolutionError
from memory.safe_io import (
    ConcurrentModificationError,
    digest_file,
    prepare_atomic_text,
)

if TYPE_CHECKING:
    from memory.core import MemoryService


@dataclass(frozen=True)
class _PreparedVaultMigration:
    path: Path
    relative_path: str
    source_digest: str
    storage_keys: tuple[str, ...]
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


_V1_FRONTMATTER_KEYS = frozenset(
    {"project", "created", "tags", "sources", "schema_version"}
)
_V1_FIELD_PREFIXES = (
    "**What:**",
    "**Why:**",
    "**Impact:**",
    "**Source:**",
    "**Living Memory:**",
    "**Category:**",
    "**Archived:**",
    "**Archive Reason:**",
    "**Superseded By:**",
)
_V1_LIVING_KEYS = frozenset(
    {
        "structured_data",
        "confidence",
        "valid_from",
        "valid_until",
        "commit_sha",
        "branch",
        "links",
        "last_verified",
    }
)
_OPERATION_KEYS = frozenset(
    {
        "operation_id",
        "source",
        "action",
        "request_fingerprint",
        "timestamp",
        "branch",
        "commit_sha",
    }
)


def _strict_json_object(payload: str) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    loaded = json.loads(
        payload,
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_constant,
    )
    if not isinstance(loaded, dict):
        raise ValueError("expected a JSON object")
    return loaded


def _validate_v1_lossless_subset(path: Path) -> None:
    """Refuse a rewrite when permissive v1 parsing would discard content."""
    content = normalize_markdown_content(read_markdown_text(path))
    lines = content.split("\n")
    body_start = 0
    if lines and lines[0] == "---":
        try:
            frontmatter_end = lines.index("---", 1)
        except ValueError as error:
            raise _UnresolvedLegacyMetadata(
                None, "legacy file contains unsupported content"
            ) from error
        seen_frontmatter: set[str] = set()
        for line in lines[1:frontmatter_end]:
            if not line.strip():
                continue
            if ":" not in line:
                raise _UnresolvedLegacyMetadata(
                    None, "legacy file contains unsupported content"
                )
            key = line.split(":", 1)[0].strip()
            if key not in _V1_FRONTMATTER_KEYS or key in seen_frontmatter:
                raise _UnresolvedLegacyMetadata(
                    None, "legacy file contains unsupported content"
                )
            seen_frontmatter.add(key)
        body_start = frontmatter_end + 1

    known_headings = {*(CATEGORY_HEADINGS.values()), ARCHIVED_HEADING}
    in_details = False
    details_lines: list[str] = []
    entry_started = False
    seen_entry_fields: set[str] = set()
    seen_title = False
    for line in lines[body_start:]:
        stripped = line.strip()
        if in_details:
            if line.startswith(("# ", "## ", "### ")):
                raise _UnresolvedLegacyMetadata(
                    None, "legacy file contains unsupported content"
                )
            if stripped == "</details>":
                in_details = False
                details = "\n".join(details_lines)
                if details != details.strip():
                    raise _UnresolvedLegacyMetadata(
                        None, "legacy file contains unsupported content"
                    )
            else:
                details_lines.append(line)
            continue
        if not stripped:
            continue
        if stripped == "<details>":
            if not entry_started or "details" in seen_entry_fields:
                raise _UnresolvedLegacyMetadata(
                    None, "legacy file contains unsupported content"
                )
            seen_entry_fields.add("details")
            details_lines = []
            in_details = True
            continue
        if stripped == "</details>":
            raise _UnresolvedLegacyMetadata(
                None, "legacy file contains unsupported content"
            )
        if line.startswith("# "):
            if (
                seen_title
                or entry_started
                or line != line.rstrip()
                or line[2:] != line[2:].strip()
            ):
                raise _UnresolvedLegacyMetadata(
                    None, "legacy file contains unsupported content"
                )
            seen_title = True
            continue
        if line.startswith("## "):
            if (
                line != line.rstrip()
                or line[3:] != line[3:].strip()
                or line[3:] not in known_headings
            ):
                raise _UnresolvedLegacyMetadata(
                    None, "legacy file contains unsupported content"
                )
            entry_started = False
            seen_entry_fields = set()
            continue
        if line.startswith("### "):
            if line != line.rstrip() or line[4:] != line[4:].strip():
                raise _UnresolvedLegacyMetadata(
                    None, "legacy file contains unsupported content"
                )
            entry_started = True
            seen_entry_fields = set()
            continue
        if not entry_started:
            raise _UnresolvedLegacyMetadata(
                None, "legacy file contains unsupported content"
            )

        field: str | None = None
        if stripped.startswith(MEMORY_ID_PREFIX) and stripped.endswith("-->"):
            field = "memory-id"
        else:
            for prefix in _V1_FIELD_PREFIXES:
                if stripped.startswith(prefix):
                    field = prefix
                    break
        if field is None or field in seen_entry_fields:
            raise _UnresolvedLegacyMetadata(
                None, "legacy file contains unsupported content"
            )
        if line != stripped:
            raise _UnresolvedLegacyMetadata(
                None, "legacy file contains unsupported content"
            )
        if field != "memory-id":
            payload = stripped.removeprefix(field)
            if payload.startswith(" "):
                payload = payload[1:]
            if payload != payload.strip():
                raise _UnresolvedLegacyMetadata(
                    None, "legacy file contains unsupported content"
                )
        if field == "**Living Memory:**":
            try:
                living = _strict_json_object(
                    stripped.removeprefix(field).strip()
                )
            except (json.JSONDecodeError, ValueError) as error:
                raise _UnresolvedLegacyMetadata(
                    None, "legacy file contains unsupported content"
                ) from error
            if not set(living).issubset(_V1_LIVING_KEYS):
                raise _UnresolvedLegacyMetadata(
                    None, "legacy file contains unsupported content"
                )
        seen_entry_fields.add(field)

    if in_details:
        raise _UnresolvedLegacyMetadata(
            None, "legacy file contains unsupported content"
        )


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


def _operation_from_mapping(value: object) -> MemoryOperation:
    if not isinstance(value, dict) or set(value) != _OPERATION_KEYS:
        raise ValueError("invalid operation history")
    for field in ("operation_id", "action", "request_fingerprint", "timestamp"):
        if not isinstance(value[field], str) or not value[field]:
            raise ValueError("invalid operation history")
    for field in ("source", "branch", "commit_sha"):
        if value[field] is not None and not isinstance(value[field], str):
            raise ValueError("invalid operation history")
    return MemoryOperation(
        operation_id=value["operation_id"],
        source=value["source"],
        action=value["action"],
        request_fingerprint=value["request_fingerprint"],
        timestamp=value["timestamp"],
        branch=value["branch"],
        commit_sha=value["commit_sha"],
    )


def _operation_history_from_row(row: dict[str, object]) -> list[MemoryOperation]:
    raw = row.get("operation_history")
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError("invalid operation history") from error
    if not isinstance(raw, list):
        raise ValueError("invalid operation history")
    operations = [_operation_from_mapping(item) for item in raw]
    if len({operation.operation_id for operation in operations}) != len(operations):
        raise ValueError("duplicate operation history")
    return operations


def _historical_operations(
    service: MemoryService,
    row: dict[str, object],
    *,
    storage_keys: tuple[str, ...],
    anchor: str | None,
) -> tuple[MemoryOperation, ...]:
    memory_id = _required_string(row, "id")
    ledger_rows = [
        dict(item)
        for item in service.db.conn.execute(
            """
            SELECT project, operation_id, source, action, request_fingerprint,
                   timestamp, branch, commit_sha
            FROM save_operations
            WHERE memory_id = ?
            ORDER BY timestamp, operation_id, project
            """,
            (memory_id,),
        ).fetchall()
    ]
    if any(item["project"] not in storage_keys for item in ledger_rows):
        raise _UnresolvedLegacyMetadata(
            anchor, "operation ledger crosses project scope"
        )

    ledger_operations: list[MemoryOperation] = []
    ledger_ids: set[str] = set()
    for item in ledger_rows:
        operation_id = item["operation_id"]
        if not isinstance(operation_id, str) or operation_id in ledger_ids:
            raise _UnresolvedLegacyMetadata(
                anchor, "operation ledger is ambiguous"
            )
        ledger_ids.add(operation_id)
        ledger_operations.append(
            _operation_from_mapping(
                {key: item[key] for key in _OPERATION_KEYS}
            )
        )

    try:
        history = _operation_history_from_row(row)
    except ValueError as error:
        raise _UnresolvedLegacyMetadata(
            anchor, "indexed operation history is malformed"
        ) from error
    if history and ledger_operations:
        history_by_id = {
            operation.operation_id: asdict(operation) for operation in history
        }
        ledger_by_id = {
            operation.operation_id: asdict(operation)
            for operation in ledger_operations
        }
        if history_by_id != ledger_by_id:
            raise _UnresolvedLegacyMetadata(
                anchor, "indexed operation history conflicts with the ledger"
            )
    return tuple(history or ledger_operations)


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
    historical_operations: tuple[MemoryOperation, ...],
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

    category = (
        entry.category
        if entry.category is not None
        else _optional_string(row, "category")
    )
    status = entry.status or "active"
    commit_sha = living_optional_string("commit_sha")
    branch = living_optional_string("branch")
    migration_operation = MemoryOperation(
        operation_id=operation_id,
        source=source,
        action="migrated",
        request_fingerprint="migration:" + operation_id,
        timestamp=updated_at,
        branch=branch,
        commit_sha=commit_sha,
    )
    existing_migration = next(
        (
            operation
            for operation in historical_operations
            if operation.operation_id == operation_id
        ),
        None,
    )
    if existing_migration is not None and existing_migration != migration_operation:
        raise _UnresolvedLegacyMetadata(
            entry.section_anchor,
            "migration operation conflicts with indexed history",
        )
    operations = list(historical_operations)
    if existing_migration is None:
        operations.append(migration_operation)
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
        operations=operations,
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
    source_digest = digest_file(resolved_path)
    if source_digest is None:
        raise _UnresolvedLegacyMetadata(None, "file is not a local regular file")
    document = parse_session_file(resolved_path)
    if digest_file(resolved_path) != source_digest:
        raise ConcurrentModificationError(
            f"Legacy file changed while it was parsed: {relative_path}"
        )
    if document.schema_version == 2:
        return None
    _validate_v1_lossless_subset(resolved_path)
    if digest_file(resolved_path) != source_digest:
        raise ConcurrentModificationError(
            f"Legacy file changed while it was validated: {relative_path}"
        )
    if document.project not in {*storage_keys, canonical_project}:
        raise _UnresolvedLegacyMetadata(None, "frontmatter project is outside the scope")
    if (
        not isinstance(document.tags, list)
        or any(not isinstance(item, str) for item in document.tags)
        or not isinstance(document.sources, list)
        or any(not isinstance(item, str) for item in document.sources)
    ):
        raise _UnresolvedLegacyMetadata(
            None, "legacy file contains unsupported content"
        )

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
            historical_operations = _historical_operations(
                service,
                row,
                storage_keys=storage_keys,
                anchor=entry.section_anchor,
            )
            memory = _memory_from_legacy_entry(
                entry,
                row,
                project=canonical_project,
                path=resolved_path,
                relative_path=relative_path,
                historical_operations=historical_operations,
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
        tags=list(document.tags),
        sources=list(document.sources),
        title=document.title,
        entries=entries,
        schema_version=2,
    )
    try:
        aggregate_tags = set(document.tags)
        aggregate_sources = set(document.sources)
        for memory, _details in memories:
            aggregate_tags.update(memory.tags)
            if memory.source:
                aggregate_sources.add(memory.source)
        rendered = render_session_document(
            migrated_document,
            tags=sorted(aggregate_tags),
            sources=sorted(aggregate_sources),
        )
    except (TypeError, ValueError) as error:
        raise _UnresolvedLegacyMetadata(
            None,
            "enriched metadata cannot be represented losslessly",
        ) from error
    return _PreparedVaultMigration(
        path=resolved_path,
        relative_path=relative_path,
        source_digest=source_digest,
        storage_keys=storage_keys,
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
    prepared = prepare_atomic_text(migration.path, migration.rendered)
    embeddings: list[tuple[str, str, bytes]] = []
    try:
        prepared_digest = digest_file(prepared.temporary)
        persistence.fault("after_all_temps_fsync")
        with service.db.transaction():
            memory_ids = tuple(memory.id for memory, _details in migration.memories)
            memory_placeholders = ", ".join("?" for _ in memory_ids)
            project_placeholders = ", ".join("?" for _ in migration.storage_keys)
            service.db.conn.execute(
                f"""
                DELETE FROM save_operations
                WHERE memory_id IN ({memory_placeholders})
                  AND project IN ({project_placeholders})
                """,
                (*memory_ids, *migration.storage_keys),
            )
            for memory, details in migration.memories:
                fingerprint = str(memory.content_fingerprint)
                fingerprint_changed = (
                    migration.previous_fingerprints[memory.id] != fingerprint
                )
                had_vector = service.db.has_vector(memory.id)
                service.db.upsert_memory(memory, details)
                for operation in memory.operations:
                    service.db.upsert_operation(
                        memory.project,
                        memory.id,
                        operation,
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
            prepared.replace_if_digest(migration.source_digest, prepared_digest)
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
    if not os.path.lexists(vault_root):
        return []
    metadata = vault_root.lstat()
    if vault_root.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise OSError("vault root is not a local directory")
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

        def prepare_scope() -> list[_PreparedVaultMigration]:
            files = _scope_files(service, scope)
            migrations: list[_PreparedVaultMigration] = []
            owners: dict[str, set[Path]] = {}
            operation_owners: dict[str, set[str]] = {}
            scanned: dict[Path, tuple[str, SessionDocument, str]] = {}
            scan_failed = False
            rows = _scope_rows(service, scope.storage_keys)

            for row in rows:
                memory_id = row.get("id")
                if not isinstance(memory_id, str):
                    continue
                try:
                    row_operations = _operation_history_from_row(row)
                except ValueError:
                    continue
                for operation in row_operations:
                    operation_owners.setdefault(
                        operation.operation_id, set()
                    ).add(memory_id)

            project_placeholders = ", ".join("?" for _ in scope.storage_keys)
            for ledger_row in service.db.conn.execute(
                f"""
                SELECT operation_id, memory_id
                FROM save_operations
                WHERE project IN ({project_placeholders})
                """,
                scope.storage_keys,
            ).fetchall():
                operation_id = ledger_row["operation_id"]
                memory_id = ledger_row["memory_id"]
                if isinstance(operation_id, str) and isinstance(memory_id, str):
                    operation_owners.setdefault(operation_id, set()).add(memory_id)

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
                    scan_failed = True
                    continue
                try:
                    metadata = path.lstat()
                    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                        raise OSError("session path is not a local regular file")
                    scanned_digest = digest_file(path)
                    if scanned_digest is None:
                        raise OSError("session path disappeared")
                    document = parse_session_file(path)
                    if digest_file(path) != scanned_digest:
                        raise ConcurrentModificationError(
                            f"Session file changed while it was scanned: {relative_path}"
                        )
                    scanned[path] = (relative_path, document, scanned_digest)
                    used_ids: set[str] = set()
                    for entry in document.entries:
                        memory_id = entry.id
                        if memory_id is None and document.schema_version == 1:
                            try:
                                matched = _match_row(
                                    entry,
                                    rows,
                                    relative_path=relative_path,
                                    vault_root=vault_root,
                                    entry_count=len(document.entries),
                                    used_ids=used_ids,
                                )
                            except _UnresolvedLegacyMetadata:
                                matched = None
                            if matched is not None:
                                matched_id = matched.get("id")
                                if isinstance(matched_id, str):
                                    memory_id = matched_id
                        if memory_id is not None:
                            owners.setdefault(memory_id, set()).add(path)
                        if document.schema_version == 2:
                            canonical = entry.to_memory(str(path))
                            for operation in canonical.operations:
                                operation_owners.setdefault(
                                    operation.operation_id, set()
                                ).add(canonical.id)
                except ConcurrentModificationError:
                    raise
                except OSError:
                    unresolved.append(
                        {
                            "file": relative_path,
                            "anchor": None,
                            "reason": "file is not a local regular file",
                        }
                    )
                    scan_failed = True
                    continue
                except (UnicodeError, ValueError):
                    unresolved.append(
                        {
                            "file": relative_path,
                            "anchor": None,
                            "reason": "file is not a readable legacy session",
                        }
                    )
                    scan_failed = True
                    continue

            if scan_failed:
                return []

            for path in files:
                relative_path, _document, _scanned_digest = scanned[path]
                try:
                    migration = _prepare_file(
                        service,
                        path,
                        canonical_project=scope.canonical_key,
                        storage_keys=scope.storage_keys,
                    )
                except _UnresolvedLegacyMetadata as error:
                    unresolved.append(_unresolved_item(relative_path, error))
                    continue
                except (OSError, UnicodeError, ValueError):
                    unresolved.append(
                        {
                            "file": relative_path,
                            "anchor": None,
                            "reason": "file is not a readable legacy session",
                        }
                    )
                    continue
                if migration is None:
                    result["skipped"] = int(result["skipped"]) + 1
                    continue
                if dry_run and digest_file(path) != migration.source_digest:
                    unresolved.append(
                        {
                            "file": relative_path,
                            "anchor": None,
                            "reason": "file changed during dry-run inspection",
                        }
                    )
                    continue
                migrations.append(migration)

            if set(_scope_files(service, scope)) != set(files):
                raise ConcurrentModificationError(
                    f"Session file set changed while project {scope.canonical_key} was scanned"
                )
            for path, (_relative_path, _document, expected_digest) in scanned.items():
                if digest_file(path) != expected_digest:
                    raise ConcurrentModificationError(
                        f"Session file changed while project {scope.canonical_key} was scanned"
                    )

            for migration in migrations:
                for memory, _details in migration.memories:
                    owners.setdefault(memory.id, set()).add(migration.path)
                    for operation in memory.operations:
                        operation_owners.setdefault(
                            operation.operation_id, set()
                        ).add(memory.id)
            duplicate_paths = {
                path
                for paths in owners.values()
                if len(paths) > 1
                for path in paths
            }
            conflicting_operation_ids = {
                operation_id
                for operation_id, memory_ids in operation_owners.items()
                if len(memory_ids) > 1
            }
            retained: list[_PreparedVaultMigration] = []
            for migration in migrations:
                if migration.path in duplicate_paths:
                    unresolved.append(
                        {
                            "file": migration.relative_path,
                            "anchor": None,
                            "reason": (
                                "stable memory ID is assigned to multiple files"
                            ),
                        }
                    )
                    continue
                if any(
                    operation.operation_id in conflicting_operation_ids
                    for memory, _details in migration.memories
                    for operation in memory.operations
                ):
                    unresolved.append(
                        {
                            "file": migration.relative_path,
                            "anchor": None,
                            "reason": (
                                "operation ID is assigned to multiple memories"
                            ),
                        }
                    )
                    continue
                retained.append(migration)
            return retained

        embedding_batches: list[tuple[tuple[str, str, bytes], ...]] = []
        if dry_run:
            migrations = prepare_scope()
            result["would_migrate"] = int(result["would_migrate"]) + len(
                migrations
            )
        else:
            with service.persistence._locked_after_recovery((scope.canonical_key,)):
                migrations = prepare_scope()
                result["would_migrate"] = int(result["would_migrate"]) + len(
                    migrations
                )
                for migration in migrations:
                    embedding_batches.append(_commit_file(service, migration))
                    result["migrated"] = int(result["migrated"]) + 1

        for embeddings in embedding_batches:
            vector_result = service.persistence._finish_mutation({}, embeddings)
            warning = vector_result.get("warning")
            if isinstance(warning, str):
                result.setdefault("warnings", []).append(warning)

    return result


@dataclass
class ReconcileReport:
    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    ledger_rows: int = 0
    rebuilt_vectors: int = 0
    invalid_fingerprints: list[str] | None = None
    blockers: list[dict[str, object]] | None = None
    destructive_cleanup: bool = True

    def __post_init__(self) -> None:
        if self.invalid_fingerprints is None:
            self.invalid_fingerprints = []
        if self.blockers is None:
            self.blockers = []

    def as_dict(self) -> dict[str, object]:
        invalid = sorted(set(self.invalid_fingerprints or []))
        blockers = sorted(
            self.blockers or [],
            key=lambda item: (
                str(item.get("project", "")),
                str(item.get("code", "")),
                str(item.get("file", "")),
                str(item.get("memory_id", "")),
                str(item.get("operation_id", "")),
            ),
        )
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "deleted": self.deleted,
            "ledger_rows": self.ledger_rows,
            "rebuilt_vectors": self.rebuilt_vectors,
            "invalid_fingerprints": invalid,
            "blockers": blockers,
            "destructive_cleanup": self.destructive_cleanup,
        }


@dataclass(frozen=True)
class _CanonicalProjection:
    memory: Memory
    details: str | None
    relative_path: str
    embedding_bytes: bytes


@dataclass
class _ReconcileScan:
    scope: _MigrationScope
    projections: dict[str, _CanonicalProjection]
    blockers: list[dict[str, object]]
    invalid_fingerprints: list[str]
    files: tuple[Path, ...]
    digests: dict[Path, str]
    project_dirs: tuple[Path, ...]
    complete: bool


class _VaultIdentityConflict(RuntimeError):
    def __init__(self, conflicts: list[dict[str, object]]) -> None:
        super().__init__("Canonical memory ID is claimed by multiple projects")
        self.conflicts = conflicts


def _blocker(
    scope: _MigrationScope,
    code: str,
    **fields: object,
) -> dict[str, object]:
    return {"project": scope.canonical_key, "code": code, **fields}


def _canonical_uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        canonical = str(uuid.UUID(value))
    except ValueError:
        return None
    return canonical if canonical == value else None


def _reconcile_scope_paths(
    service: MemoryService,
    scope: _MigrationScope,
) -> tuple[list[Path], list[Path], list[dict[str, object]]]:
    files: list[Path] = []
    project_dirs: list[Path] = []
    blockers: list[dict[str, object]] = []
    vault_root = Path(service.vault_dir)
    if not os.path.lexists(vault_root):
        return files, project_dirs, [_blocker(scope, "missing_vault_root")]
    try:
        vault_metadata = vault_root.lstat()
    except OSError:
        return files, project_dirs, [_blocker(scope, "unreadable_vault_root")]
    if vault_root.is_symlink() or not stat.S_ISDIR(vault_metadata.st_mode):
        return files, project_dirs, [_blocker(scope, "unsafe_vault_root")]

    for storage_key in scope.storage_keys:
        try:
            project_dir = service.persistence._project_dir(storage_key)
        except ProjectResolutionError:
            blockers.append(
                _blocker(scope, "invalid_project_path", storage_key=storage_key)
            )
            continue
        if not os.path.lexists(project_dir):
            continue
        try:
            metadata = project_dir.lstat()
        except OSError:
            blockers.append(
                _blocker(scope, "unreadable_project_directory", file=storage_key)
            )
            continue
        if project_dir.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            blockers.append(
                _blocker(scope, "unsafe_project_directory", file=storage_key)
            )
            continue
        project_dirs.append(project_dir)
        try:
            files.extend(sorted(project_dir.glob("*-session.md")))
        except OSError:
            blockers.append(
                _blocker(scope, "unreadable_project_directory", file=storage_key)
            )
    return (
        sorted(set(files), key=lambda path: str(path)),
        sorted(set(project_dirs), key=lambda path: str(path)),
        blockers,
    )


def _scan_reconcile_scope(
    service: MemoryService,
    scope: _MigrationScope,
) -> _ReconcileScan:
    files, project_dirs, blockers = _reconcile_scope_paths(service, scope)
    vault_root = Path(service.vault_dir).absolute()
    digests: dict[Path, str] = {}
    candidates: dict[str, list[_CanonicalProjection]] = {}

    scoped_rows = _scope_rows(service, scope.storage_keys)
    cross_project_ledger_ids: set[str] = set()
    if scoped_rows and not project_dirs:
        blockers.append(_blocker(scope, "missing_project_directory"))
    scope_placeholders = ", ".join("?" for _ in scope.storage_keys)
    for row in scoped_rows:
        memory_id = row.get("id")
        if not isinstance(memory_id, str):
            continue
        outside_ledger = service.db.conn.execute(
            f"""
            SELECT project, operation_id
            FROM save_operations
            WHERE memory_id = ? AND project NOT IN ({scope_placeholders})
            LIMIT 1
            """,
            (memory_id, *scope.storage_keys),
        ).fetchone()
        if outside_ledger is not None:
            cross_project_ledger_ids.add(memory_id)
            blockers.append(
                _blocker(
                    scope,
                    "cross_project_operation_ledger",
                    memory_id=memory_id,
                    operation_id=outside_ledger["operation_id"],
                )
            )

    for path in files:
        try:
            metadata = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                raise OSError("session path is not a local regular file")
            resolved = path.resolve(strict=False)
            if resolved != path:
                raise OSError("session path changes identity")
            relative_path = resolved.relative_to(vault_root).as_posix()
            before = digest_file(resolved)
            if before is None:
                raise OSError("session path disappeared")
            document = parse_session_file(resolved)
            if digest_file(resolved) != before:
                raise ConcurrentModificationError(
                    f"Session changed while scanning {relative_path}"
                )
            digests[path] = before
        except ConcurrentModificationError:
            blockers.append(
                _blocker(scope, "concurrent_markdown_change", file=path.name)
            )
            continue
        except (OSError, UnicodeError, ValueError):
            blockers.append(
                _blocker(scope, "unreadable_markdown", file=path.name)
            )
            continue

        if document.schema_version != 2:
            blockers.append(
                _blocker(scope, "schema_v1", file=relative_path)
            )
            continue
        if document.project not in {*scope.storage_keys, scope.canonical_key}:
            blockers.append(
                _blocker(scope, "document_project_mismatch", file=relative_path)
            )
            continue

        for entry in document.entries:
            try:
                memory = entry.to_memory(str(path))
            except (TypeError, ValueError):
                blockers.append(
                    _blocker(scope, "invalid_v2_entry", file=relative_path)
                )
                continue
            memory_id = _canonical_uuid(memory.id)
            if memory_id is None:
                blockers.append(
                    _blocker(
                        scope,
                        "invalid_memory_id",
                        file=relative_path,
                        memory_id=memory.id,
                    )
                )
                continue
            if memory.project not in {*scope.storage_keys, scope.canonical_key}:
                blockers.append(
                    _blocker(
                        scope,
                        "memory_project_mismatch",
                        file=relative_path,
                        memory_id=memory_id,
                    )
                )
                continue
            outside = service.db.conn.execute(
                """
                SELECT project FROM memories
                WHERE id = ? AND project NOT IN ({})
                """.format(", ".join("?" for _ in scope.storage_keys)),
                (memory_id, *scope.storage_keys),
            ).fetchone()
            if outside is not None:
                blockers.append(
                    _blocker(
                        scope,
                        "cross_project_memory_id",
                        file=relative_path,
                        memory_id=memory_id,
                    )
                )
                continue
            expected_fingerprint = content_fingerprint(memory)
            if memory.content_fingerprint != expected_fingerprint:
                blockers.append(
                    _blocker(
                        scope,
                        "invalid_content_fingerprint",
                        file=relative_path,
                        memory_id=memory_id,
                    )
                )
                continue
            memory.project = scope.canonical_key
            projection = _CanonicalProjection(
                memory=memory,
                details=entry.details,
                relative_path=relative_path,
                embedding_bytes=service.persistence._embedding_bytes(memory),
            )
            candidates.setdefault(memory_id, []).append(projection)

    invalid_fingerprints = [
        str(item["memory_id"])
        for item in blockers
        if item.get("code") == "invalid_content_fingerprint"
        and isinstance(item.get("memory_id"), str)
    ]
    projections: dict[str, _CanonicalProjection] = {}
    for memory_id, matches in candidates.items():
        if len(matches) != 1:
            blockers.append(
                _blocker(scope, "duplicate_memory_id", memory_id=memory_id)
            )
            continue
        projections[memory_id] = matches[0]

    for memory_id in sorted(set(projections) - cross_project_ledger_ids):
        outside_ledger = service.db.conn.execute(
            f"""
            SELECT project, operation_id
            FROM save_operations
            WHERE memory_id = ? AND project NOT IN ({scope_placeholders})
            LIMIT 1
            """,
            (memory_id, *scope.storage_keys),
        ).fetchone()
        if outside_ledger is None:
            continue
        cross_project_ledger_ids.add(memory_id)
        blockers.append(
            _blocker(
                scope,
                "cross_project_operation_ledger",
                memory_id=memory_id,
                operation_id=outside_ledger["operation_id"],
            )
        )
    for memory_id in cross_project_ledger_ids:
        projections.pop(memory_id, None)

    operation_occurrences: dict[str, list[str]] = {}
    for memory_id, projection in projections.items():
        for operation in projection.memory.operations:
            operation_occurrences.setdefault(operation.operation_id, []).append(
                memory_id
            )
    conflicting_operations = {
        operation_id: memory_ids
        for operation_id, memory_ids in operation_occurrences.items()
        if len(memory_ids) != 1
    }
    blocked_memory_ids = {
        memory_id
        for memory_ids in conflicting_operations.values()
        for memory_id in memory_ids
    }
    for operation_id, memory_ids in conflicting_operations.items():
        blockers.append(
            _blocker(
                scope,
                "duplicate_operation_id",
                operation_id=operation_id,
                memory_ids=sorted(set(memory_ids)),
            )
        )
    for memory_id in blocked_memory_ids:
        projections.pop(memory_id, None)

    complete = not blockers
    return _ReconcileScan(
        scope=scope,
        projections=projections,
        blockers=blockers,
        invalid_fingerprints=invalid_fingerprints,
        files=tuple(files),
        digests=digests,
        project_dirs=tuple(project_dirs),
        complete=complete,
    )


def _verify_reconcile_scan(
    service: MemoryService,
    scan: _ReconcileScan,
) -> None:
    try:
        current_files, project_dirs, blockers = _reconcile_scope_paths(
            service, scan.scope
        )
    except OSError as error:
        raise ConcurrentModificationError(
            "Canonical Markdown scope became unreadable"
        ) from error
    initial_path_blockers = [
        item
        for item in scan.blockers
        if item.get("code")
        in {
            "missing_vault_root",
            "unreadable_vault_root",
            "unsafe_vault_root",
            "invalid_project_path",
            "unreadable_project_directory",
            "unsafe_project_directory",
        }
    ]
    if (
        tuple(current_files) != scan.files
        or tuple(project_dirs) != scan.project_dirs
        or blockers != initial_path_blockers
    ):
        raise ConcurrentModificationError("Canonical Markdown scope changed")
    for path, expected in scan.digests.items():
        try:
            actual = digest_file(path)
        except OSError as error:
            raise ConcurrentModificationError(
                f"Canonical Markdown became unreadable: {path.name}"
            ) from error
        if actual != expected:
            raise ConcurrentModificationError(
                f"Canonical Markdown changed after scan: {path.name}"
            )


def _json_column(row: dict[str, object], field: str) -> object:
    value = row.get(field)
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return object()


def _projection_matches(
    service: MemoryService,
    projection: _CanonicalProjection,
    row: dict[str, object],
) -> bool:
    memory = projection.memory
    scalar_fields = (
        "id",
        "title",
        "what",
        "why",
        "impact",
        "category",
        "project",
        "source",
        "file_path",
        "section_anchor",
        "created_at",
        "updated_at",
        "status",
        "archived_at",
        "archive_reason",
        "superseded_by",
        "confidence",
        "valid_from",
        "valid_until",
        "commit_sha",
        "branch",
        "last_verified",
        "creator_source",
        "last_updated_by",
        "content_fingerprint",
        "updated_count",
    )
    if any(row.get(field) != getattr(memory, field) for field in scalar_fields):
        return False
    if bool(row.get("history_complete")) != memory.history_complete:
        return False
    expected_json = {
        "tags": memory.tags,
        "related_files": memory.related_files,
        "structured_data": memory.structured_data,
        "links": memory.links,
        "contributors": memory.contributors,
        "operation_history": [asdict(operation) for operation in memory.operations],
    }
    if any(
        _json_column(row, field) != expected
        for field, expected in expected_json.items()
    ):
        return False
    detail_row = service.db.conn.execute(
        "SELECT body FROM memory_details WHERE memory_id = ?",
        (memory.id,),
    ).fetchone()
    actual_details = detail_row["body"] if detail_row is not None else None
    return actual_details == projection.details


def _filter_incomplete_ledger_conflicts(
    service: MemoryService,
    scan: _ReconcileScan,
) -> dict[str, _CanonicalProjection]:
    if scan.complete:
        return dict(scan.projections)
    accepted: dict[str, _CanonicalProjection] = {}
    placeholders = ", ".join("?" for _ in scan.scope.storage_keys)
    for memory_id, projection in scan.projections.items():
        conflict = False
        for operation in projection.memory.operations:
            rows = service.db.conn.execute(
                f"""
                SELECT memory_id FROM save_operations
                WHERE operation_id = ? AND project IN ({placeholders})
                """,
                (operation.operation_id, *scan.scope.storage_keys),
            ).fetchall()
            if any(row["memory_id"] != memory_id for row in rows):
                scan.blockers.append(
                    _blocker(
                        scan.scope,
                        "operation_ledger_conflict",
                        memory_id=memory_id,
                        operation_id=operation.operation_id,
                    )
                )
                conflict = True
                break
        if not conflict:
            accepted[memory_id] = projection
    return accepted


def _apply_reconcile_scan(
    service: MemoryService,
    scan: _ReconcileScan,
    report: ReconcileReport,
) -> tuple[tuple[str, str, bytes], ...]:
    scope_placeholders = ", ".join("?" for _ in scan.scope.storage_keys)
    embeddings: list[tuple[str, str, bytes]] = []
    inserted_count = 0
    updated_count = 0
    deleted_count = 0
    ledger_count = 0

    with service.db.transaction():
        conflicts = _project_vault_id_conflicts(
            service,
            scan.scope.canonical_key,
        )
        if conflicts:
            raise _VaultIdentityConflict(conflicts)
        _verify_reconcile_scan(service, scan)
        projections = dict(scan.projections)
        for memory_id in sorted(tuple(projections)):
            outside = service.db.conn.execute(
                f"""
                SELECT project FROM memories
                WHERE id = ? AND project NOT IN ({scope_placeholders})
                """,
                (memory_id, *scan.scope.storage_keys),
            ).fetchone()
            if outside is None:
                continue
            scan.blockers.append(
                _blocker(
                    scan.scope,
                    "cross_project_memory_id",
                    memory_id=memory_id,
                )
            )
            scan.complete = False
            projections.pop(memory_id, None)
        current_rows = {
            str(row["id"]): dict(row)
            for row in service.db.conn.execute(
                f"SELECT * FROM memories WHERE project IN ({scope_placeholders})",
                scan.scope.storage_keys,
            ).fetchall()
        }
        for memory_id in sorted(set(current_rows) | set(projections)):
            outside_ledger = service.db.conn.execute(
                f"""
                SELECT operation_id FROM save_operations
                WHERE memory_id = ?
                  AND project NOT IN ({scope_placeholders})
                LIMIT 1
                """,
                (memory_id, *scan.scope.storage_keys),
            ).fetchone()
            if outside_ledger is None:
                continue
            blocker = _blocker(
                scan.scope,
                "cross_project_operation_ledger",
                memory_id=memory_id,
                operation_id=outside_ledger["operation_id"],
            )
            if blocker not in scan.blockers:
                scan.blockers.append(blocker)
            scan.complete = False
            projections.pop(memory_id, None)
        scan.projections = projections
        projections = _filter_incomplete_ledger_conflicts(service, scan)
        ledger_count = sum(
            len(projection.memory.operations)
            for projection in projections.values()
        )

        if scan.complete:
            service.db.conn.execute(
                f"DELETE FROM save_operations WHERE project IN ({scope_placeholders})",
                scan.scope.storage_keys,
            )

        for memory_id, projection in projections.items():
            memory = projection.memory
            existing = current_rows.get(memory_id)
            inserted = existing is None
            changed = inserted or not _projection_matches(
                service, projection, existing
            )
            old_fingerprint = (
                existing.get("content_fingerprint") if existing is not None else None
            )
            had_vector = service.db.has_vector(memory_id)
            if inserted:
                inserted_count += 1
            elif changed:
                updated_count += 1
            if changed:
                if existing is not None and old_fingerprint != memory.content_fingerprint:
                    service.db.invalidate_vector(memory_id)
                service.db.upsert_memory(memory, projection.details)

            for operation in memory.operations:
                service.db.upsert_operation(
                    scan.scope.canonical_key,
                    memory_id,
                    operation,
                )

            if memory.status != "active":
                service.db.invalidate_vector(memory_id)
                service.db.clear_vector_repair(memory_id)
                continue
            if inserted or changed or not had_vector:
                fingerprint = str(memory.content_fingerprint)
                service.db.queue_vector_repair(
                    memory_id,
                    fingerprint,
                    memory.updated_at,
                )
                embeddings.append(
                    (memory_id, fingerprint, projection.embedding_bytes)
                )
            elif scan.complete:
                service.db.clear_vector_repair(memory_id)

        if scan.complete:
            canonical_ids = set(projections)
            for memory_id in sorted(set(current_rows) - canonical_ids):
                service.db.conn.execute(
                    "DELETE FROM pending_vector_repairs WHERE memory_id = ?",
                    (memory_id,),
                )
                service.db.delete_memory_exact(memory_id)
                deleted_count += 1

        _verify_reconcile_scan(service, scan)
        conflicts = _project_vault_id_conflicts(
            service,
            scan.scope.canonical_key,
        )
        if conflicts:
            raise _VaultIdentityConflict(conflicts)

    report.inserted += inserted_count
    report.updated += updated_count
    report.deleted += deleted_count
    report.ledger_rows += ledger_count
    return tuple(embeddings)


def _finish_reconcile_vectors(
    service: MemoryService,
    embeddings: tuple[tuple[str, str, bytes], ...],
    report: ReconcileReport,
) -> None:
    for memory_id, fingerprint, payload in embeddings:
        try:
            embedding = service.embedding_provider.embed(payload.decode("utf-8"))
            written = service.db.upsert_vector_if_current(
                memory_id,
                fingerprint,
                embedding,
            )
        except Exception as error:
            assert report.blockers is not None
            report.blockers.append(
                {
                    "code": "embedding_failed",
                    "memory_id": memory_id,
                    "error": type(error).__name__,
                }
            )
            continue
        if not written:
            assert report.blockers is not None
            report.blockers.append(
                {"code": "vector_cas_miss", "memory_id": memory_id}
            )
            continue
        service.db.clear_vector_repair(memory_id, fingerprint)
        report.rebuilt_vectors += 1


def _vault_wide_memory_id_conflicts(
    service: MemoryService,
) -> list[dict[str, object]]:
    """Find stable IDs claimed by valid v2 Markdown in multiple projects."""
    owners: dict[str, list[dict[str, str]]] = {}
    try:
        scopes = _discover_scopes(service, None)
    except (OSError, ProjectResolutionError):
        return []
    vault_root = Path(service.vault_dir).absolute()
    for scope in scopes:
        files, _project_dirs, path_blockers = _reconcile_scope_paths(
            service, scope
        )
        if path_blockers:
            continue
        for path in files:
            try:
                metadata = path.lstat()
                if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                    continue
                before = digest_file(path)
                if before is None:
                    continue
                document = parse_session_file(path)
                if digest_file(path) != before or document.schema_version != 2:
                    continue
                if document.project not in {
                    *scope.storage_keys,
                    scope.canonical_key,
                }:
                    continue
                relative = path.relative_to(vault_root).as_posix()
            except (OSError, UnicodeError, ValueError):
                continue
            for entry in document.entries:
                try:
                    memory = entry.to_memory(str(path))
                except (TypeError, ValueError):
                    continue
                memory_id = _canonical_uuid(memory.id)
                if (
                    memory_id is None
                    or memory.project
                    not in {*scope.storage_keys, scope.canonical_key}
                    or memory.content_fingerprint != content_fingerprint(memory)
                ):
                    continue
                owners.setdefault(memory_id, []).append(
                    {
                        "project": scope.canonical_key,
                        "file": relative,
                    }
                )

    conflicts: list[dict[str, object]] = []
    for memory_id, claimed in sorted(owners.items()):
        projects = sorted({owner["project"] for owner in claimed})
        if len(projects) <= 1:
            continue
        conflicts.append(
            {
                "code": "duplicate_memory_id_across_projects",
                "memory_id": memory_id,
                "projects": projects,
                "files": sorted({owner["file"] for owner in claimed}),
            }
        )
    return conflicts


def _project_vault_id_conflicts(
    service: MemoryService,
    project: str,
) -> list[dict[str, object]]:
    return [
        conflict
        for conflict in _vault_wide_memory_id_conflicts(service)
        if isinstance(conflict.get("projects"), list)
        and project in conflict["projects"]
    ]


def reconcile_project(
    service: MemoryService,
    project: str,
) -> ReconcileReport:
    """Rebuild one derived project scope from canonical Markdown."""
    report = ReconcileReport()
    try:
        scope = service.persistence._resolve_scope(project)
    except (OSError, ProjectResolutionError) as error:
        assert report.blockers is not None
        report.blockers.append(
            {
                "project": project,
                "code": "project_resolution_failed",
                "error": type(error).__name__,
            }
        )
        report.destructive_cleanup = False
        return report

    embeddings: tuple[tuple[str, str, bytes], ...] = ()
    try:
        with service.persistence._locked_after_recovery((scope.canonical_key,)):
            conflicts = _project_vault_id_conflicts(
                service,
                scope.canonical_key,
            )
            if conflicts:
                raise _VaultIdentityConflict(conflicts)
            scan = _scan_reconcile_scope(service, scope)
            _verify_reconcile_scan(service, scan)
            assert report.blockers is not None
            assert report.invalid_fingerprints is not None
            report.blockers.extend(scan.blockers)
            report.invalid_fingerprints.extend(scan.invalid_fingerprints)
            embeddings = _apply_reconcile_scan(service, scan, report)
            for blocker in scan.blockers:
                if blocker not in report.blockers:
                    report.blockers.append(blocker)
            report.destructive_cleanup = scan.complete
    except _VaultIdentityConflict as error:
        assert report.blockers is not None
        report.blockers.extend(
            {"project": scope.canonical_key, **conflict}
            for conflict in error.conflicts
        )
        report.destructive_cleanup = False
        return report
    except JournalRecoveryConflict as error:
        assert report.blockers is not None
        report.blockers.append(
            {
                "project": scope.canonical_key,
                "code": "journal_recovery_conflict",
                "error": type(error).__name__,
            }
        )
        report.destructive_cleanup = False
        return report
    except ConcurrentModificationError:
        assert report.blockers is not None
        report.blockers.append(
            {
                "project": scope.canonical_key,
                "code": "concurrent_markdown_change",
            }
        )
        report.destructive_cleanup = False
        return report

    _finish_reconcile_vectors(service, embeddings, report)
    return report


def _reconcile_scopes(
    service: MemoryService,
    project: str | None,
) -> list[_MigrationScope]:
    if project is not None:
        return [service.persistence._resolve_scope(project)]
    scopes = {
        scope.canonical_key: scope for scope in _discover_scopes(service, None)
    }
    for row in service.db.conn.execute(
        """
        SELECT project FROM memories
        UNION
        SELECT project FROM save_operations
        ORDER BY project
        """
    ).fetchall():
        value = row["project"]
        if not isinstance(value, str):
            continue
        try:
            scope = service.persistence._resolve_scope(value)
        except (OSError, ProjectResolutionError):
            continue
        scopes.setdefault(scope.canonical_key, scope)
    return [scopes[key] for key in sorted(scopes)]


def reconcile_vault(
    service: MemoryService,
    project: str | None = None,
) -> dict[str, object]:
    """Reconcile one or every discovered canonical project scope."""
    combined = ReconcileReport()
    try:
        scopes = _reconcile_scopes(service, project)
    except (OSError, ProjectResolutionError) as error:
        assert combined.blockers is not None
        combined.blockers.append(
            {
                "project": project or "",
                "code": "scope_discovery_failed",
                "error": type(error).__name__,
            }
        )
        combined.destructive_cleanup = False
        return combined.as_dict()
    for scope in scopes:
        current = reconcile_project(service, scope.canonical_key)
        combined.inserted += current.inserted
        combined.updated += current.updated
        combined.deleted += current.deleted
        combined.ledger_rows += current.ledger_rows
        combined.rebuilt_vectors += current.rebuilt_vectors
        assert combined.invalid_fingerprints is not None
        assert combined.blockers is not None
        combined.invalid_fingerprints.extend(current.invalid_fingerprints or [])
        combined.blockers.extend(current.blockers or [])
        combined.destructive_cleanup = (
            combined.destructive_cleanup and current.destructive_cleanup
        )
    return combined.as_dict()


def _repair_command(project: str) -> str:
    return f"memory import --reconcile --project {project}"


def _canonical_operation_rows(
    service: MemoryService,
    scan: _ReconcileScan,
) -> list[dict[str, object]]:
    placeholders = ", ".join("?" for _ in scan.scope.storage_keys)
    return [
        dict(row)
        for row in service.db.conn.execute(
            f"""
            SELECT project, operation_id, memory_id, request_fingerprint,
                   action, source, timestamp, branch, commit_sha
            FROM save_operations
            WHERE project IN ({placeholders})
            ORDER BY project, operation_id
            """,
            scan.scope.storage_keys,
        ).fetchall()
    ]


def _operation_drift_findings(
    service: MemoryService,
    scan: _ReconcileScan,
) -> list[dict[str, object]]:
    expected = {
        operation.operation_id: {
            "project": scan.scope.canonical_key,
            "operation_id": operation.operation_id,
            "memory_id": projection.memory.id,
            "request_fingerprint": operation.request_fingerprint,
            "action": operation.action,
            "source": operation.source,
            "timestamp": operation.timestamp,
            "branch": operation.branch,
            "commit_sha": operation.commit_sha,
        }
        for projection in scan.projections.values()
        for operation in projection.memory.operations
    }
    actual_rows = _canonical_operation_rows(service, scan)
    actual: dict[str, list[dict[str, object]]] = {}
    for row in actual_rows:
        actual.setdefault(str(row["operation_id"]), []).append(row)

    operation_ids = set(expected)
    if scan.complete:
        operation_ids.update(actual)
    findings: list[dict[str, object]] = []
    for operation_id in sorted(operation_ids):
        rows = actual.get(operation_id, [])
        matches = len(rows) == 1 and rows[0] == expected.get(operation_id)
        if matches:
            continue
        findings.append(
            {
                "code": "operation_ledger_drift",
                "project": scan.scope.canonical_key,
                "operation_id": operation_id,
                "repair": _repair_command(scan.scope.canonical_key),
            }
        )
    return findings


def _journal_prepared_paths(service: MemoryService) -> set[Path]:
    from memory.persistence import load_operation_journal

    memory_home = Path(service.memory_home).resolve()
    transactions = memory_home / "transactions"
    if not transactions.is_dir() or transactions.is_symlink():
        return set()
    prepared: set[Path] = set()
    for journal_path in sorted(transactions.glob("*.json")):
        try:
            operation = load_operation_journal(memory_home, journal_path)
        except (JournalRecoveryConflict, OSError, UnicodeError, ValueError):
            continue
        prepared.update(
            memory_home / target.temporary for target in operation.targets
        )
    return prepared


def _stale_prepared_findings(
    service: MemoryService,
    scan: _ReconcileScan,
    journal_prepared: set[Path],
) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    vault_root = Path(service.vault_dir).resolve()
    for project_dir in scan.project_dirs:
        try:
            candidates = sorted(project_dir.glob(".*-session.md.*.tmp"))
        except OSError:
            continue
        for path in candidates:
            if path in journal_prepared:
                continue
            try:
                metadata = path.lstat()
                if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                    continue
                relative = path.relative_to(vault_root).as_posix()
            except (OSError, ValueError):
                continue
            findings.append(
                {
                    "code": "stale_prepared_file",
                    "project": scan.scope.canonical_key,
                    "file": relative,
                    "repair": (
                        "Inspect the prepared file and pending journals before "
                        "removing it"
                    ),
                }
            )
    return findings


def canonical_findings(
    service: MemoryService,
    project: str | None = None,
) -> list[dict[str, object]]:
    """Return canonical drift findings without acquiring locks or mutating state."""
    try:
        scopes = _reconcile_scopes(service, project)
    except (OSError, ProjectResolutionError) as error:
        return [
            {
                "code": "project_resolution_failed",
                "project": project or "",
                "error": type(error).__name__,
            }
        ]

    findings: list[dict[str, object]] = []
    selected_projects = {scope.canonical_key for scope in scopes}
    for conflict in _vault_wide_memory_id_conflicts(service):
        projects = conflict.get("projects")
        if project is not None and (
            not isinstance(projects, list)
            or selected_projects.isdisjoint(projects)
        ):
            continue
        repair_project = (
            projects[0]
            if isinstance(projects, list) and projects
            else project or ""
        )
        findings.append(
            {
                **conflict,
                "project": repair_project,
                "repair": (
                    "Assign a unique stable memory ID in canonical Markdown "
                    "before reconciliation"
                ),
            }
        )
    journal_prepared = _journal_prepared_paths(service)
    for scope in scopes:
        scan = _scan_reconcile_scope(service, scope)
        repair = _repair_command(scope.canonical_key)
        for blocker in scan.blockers:
            finding = dict(blocker)
            if blocker.get("code") == "schema_v1":
                finding["repair"] = (
                    f"memory migrate vault-metadata --project {scope.canonical_key}"
                )
            elif blocker.get("code") == "invalid_content_fingerprint":
                finding["repair"] = (
                    "Fix or restore canonical Markdown, then run: " + repair
                )
            else:
                finding["repair"] = repair
            findings.append(finding)

        for memory_id, projection in sorted(scan.projections.items()):
            row = service.db.get_memory(memory_id)
            if row is None or not _projection_matches(service, projection, row):
                findings.append(
                    {
                        "code": "projection_drift",
                        "project": scope.canonical_key,
                        "memory_id": memory_id,
                        "repair": repair,
                    }
                )
        if scan.complete:
            canonical_ids = set(scan.projections)
            for row in _scope_rows(service, scope.storage_keys):
                memory_id = row.get("id")
                if not isinstance(memory_id, str) or memory_id in canonical_ids:
                    continue
                findings.append(
                    {
                        "code": "projection_drift",
                        "project": scope.canonical_key,
                        "memory_id": memory_id,
                        "repair": repair,
                    }
                )
        findings.extend(_operation_drift_findings(service, scan))
        findings.extend(
            _stale_prepared_findings(service, scan, journal_prepared)
        )

    return sorted(
        findings,
        key=lambda item: (
            str(item.get("project", "")),
            str(item.get("code", "")),
            str(item.get("file", "")),
            str(item.get("memory_id", "")),
            str(item.get("operation_id", "")),
        ),
    )
