"""Canonical Markdown persistence with a derived SQLite projection."""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Optional
import uuid

from memory.db import DimensionMismatchError, MemoryDB
from memory.markdown import (
    LegacySchemaWriteError,
    SessionDocument,
    SessionEntry,
    assign_entry_anchors,
    parse_session_file,
    render_session_document,
    upsert_session_memory_entry,
)
from memory.merge import MergeContext, is_duplicate, merge_duplicate
from memory.models import Memory, MemoryOperation, RawMemoryInput
from memory.projects import ProjectRegistry, ProjectResolutionError, validate_storage_key
from memory.redaction import redact, redact_memory_input
from memory.safe_io import (
    ConcurrentModificationError,
    PreparedAtomicWrite,
    ProcessFileLock,
    digest_file,
    fsync_directory,
    publish_text_exclusive,
    prepare_atomic_text,
    read_regular_text_bounded,
)


class _Unset:
    """Sentinel type used to distinguish an omitted patch field from a clear."""


UNSET = _Unset()


@dataclass(frozen=True)
class MemoryPatch:
    title: str | _Unset = UNSET
    what: str | _Unset = UNSET
    why: str | None | _Unset = UNSET
    impact: str | None | _Unset = UNSET
    category: str | None | _Unset = UNSET
    tags: list[str] | _Unset = UNSET
    details: str | None | _Unset = UNSET


@dataclass(frozen=True)
class JournalTarget:
    target: str
    temporary: str
    before_sha256: str | None
    after_sha256: str

    @classmethod
    def from_prepared(
        cls,
        memory_home: Path,
        prepared: PreparedAtomicWrite,
    ) -> "JournalTarget":
        root = memory_home.resolve()
        if (
            prepared.target.resolve(strict=False) != prepared.target
            or prepared.temporary.resolve(strict=False) != prepared.temporary
        ):
            raise ValueError("Journal targets must preserve exact local path identity")
        target = prepared.target.resolve(strict=False)
        temporary = prepared.temporary.resolve(strict=False)
        try:
            target_relative = target.relative_to(root).as_posix()
            temporary_relative = temporary.relative_to(root).as_posix()
        except ValueError as error:
            raise ValueError("Journal targets must remain inside MEMORY_HOME") from error
        after_sha256 = digest_file(prepared.temporary)
        if after_sha256 is None:
            raise FileNotFoundError(prepared.temporary)
        return cls(
            target=target_relative,
            temporary=temporary_relative,
            before_sha256=digest_file(prepared.target),
            after_sha256=after_sha256,
        )


@dataclass(frozen=True)
class OperationJournal:
    schema_version: int
    operation_id: str
    action: str
    project_keys: tuple[str, ...]
    affected_memory_ids: tuple[str, ...]
    canonical_memory_id: str | None
    targets: tuple[JournalTarget, ...]
    created_at: str
    actor: str | None = None


class JournalRecoveryConflict(RuntimeError):
    """Raised when journal recovery cannot safely choose a known file state."""


_JOURNAL_ACTIONS = frozenset(
    {"create", "update", "archive", "restore", "merge", "delete"}
)
_JOURNAL_FIELDS = frozenset(
    {
        "schema_version",
        "operation_id",
        "action",
        "project_keys",
        "affected_memory_ids",
        "canonical_memory_id",
        "targets",
        "created_at",
        "actor",
    }
)
_JOURNAL_TARGET_FIELDS = frozenset(
    {"target", "temporary", "before_sha256", "after_sha256"}
)


def _canonical_journal_uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise JournalRecoveryConflict(f"{label} must be a UUID")
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, AttributeError) as error:
        raise JournalRecoveryConflict(f"{label} must be a UUID") from error
    if value != canonical:
        raise JournalRecoveryConflict(f"{label} must use canonical UUID spelling")
    return canonical


def _validate_digest(value: object, *, nullable: bool, label: str) -> str | None:
    if value is None and nullable:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise JournalRecoveryConflict(f"{label} must be a lowercase SHA-256 digest")
    return value


def _journal_relative_path(memory_home: Path, value: object, label: str) -> tuple[str, Path]:
    if not isinstance(value, str) or not value or "\\" in value:
        raise JournalRecoveryConflict(f"{label} must be a normalized relative path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise JournalRecoveryConflict(f"{label} must be a normalized relative path")
    if relative.as_posix() != value:
        raise JournalRecoveryConflict(f"{label} must be a normalized relative path")
    root = memory_home.resolve()
    candidate = root.joinpath(*relative.parts)
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise JournalRecoveryConflict(f"{label} escapes MEMORY_HOME") from error
    if resolved != candidate:
        raise JournalRecoveryConflict(f"{label} changes identity through a symlink")
    return value, candidate


def _strict_json_object(payload: str, label: str) -> dict[str, object]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant {value}")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key}")
            result[key] = value
        return result

    try:
        decoded = json.loads(
            payload,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise JournalRecoveryConflict(f"Invalid {label}") from error
    if not isinstance(decoded, dict):
        raise JournalRecoveryConflict(f"{label} must be one JSON object")
    return decoded


def _operation_from_payload(
    memory_home: Path,
    journal_path: Path,
    payload: dict[str, object],
) -> OperationJournal:
    unknown_fields = set(payload) - _JOURNAL_FIELDS
    missing_fields = (_JOURNAL_FIELDS - {"actor"}) - set(payload)
    if unknown_fields or missing_fields:
        raise JournalRecoveryConflict("Operation journal has unknown or missing fields")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise JournalRecoveryConflict("Unsupported operation journal schema")
    operation_id = _canonical_journal_uuid(payload["operation_id"], "Operation ID")
    if journal_path.name != f"{operation_id}.json":
        raise JournalRecoveryConflict("Operation ID does not match journal filename")
    action = payload["action"]
    if not isinstance(action, str) or action not in _JOURNAL_ACTIONS:
        raise JournalRecoveryConflict("Operation journal action is invalid")

    raw_projects = payload["project_keys"]
    if not isinstance(raw_projects, list) or not raw_projects:
        raise JournalRecoveryConflict("Operation journal project_keys must be a list")
    if not all(isinstance(key, str) for key in raw_projects):
        raise JournalRecoveryConflict("Operation journal project key is invalid")
    try:
        project_keys = tuple(validate_storage_key(key) for key in raw_projects)
    except ProjectResolutionError as error:
        raise JournalRecoveryConflict("Operation journal project key is invalid") from error
    if project_keys != tuple(sorted(set(project_keys))):
        raise JournalRecoveryConflict(
            "Operation journal project_keys must be sorted and unique"
        )

    raw_ids = payload["affected_memory_ids"]
    if (
        not isinstance(raw_ids, list)
        or not raw_ids
        or not all(isinstance(memory_id, str) and memory_id for memory_id in raw_ids)
        or len(set(raw_ids)) != len(raw_ids)
    ):
        raise JournalRecoveryConflict(
            "Operation journal affected_memory_ids must be unique strings"
        )
    canonical_memory_id = payload["canonical_memory_id"]
    if canonical_memory_id is not None and not (
        isinstance(canonical_memory_id, str) and canonical_memory_id
    ):
        raise JournalRecoveryConflict("Operation journal canonical_memory_id is invalid")
    if canonical_memory_id is not None and canonical_memory_id not in raw_ids:
        raise JournalRecoveryConflict(
            "Operation journal canonical_memory_id must be affected"
        )
    if action == "delete" and canonical_memory_id is not None:
        raise JournalRecoveryConflict(
            "Delete journals must not name a canonical memory"
        )
    if action != "delete" and canonical_memory_id is None:
        raise JournalRecoveryConflict(
            "Non-delete journals require a canonical memory"
        )

    raw_targets = payload["targets"]
    if not isinstance(raw_targets, list) or not raw_targets:
        raise JournalRecoveryConflict("Operation journal targets must be a non-empty list")
    targets: list[JournalTarget] = []
    seen_targets: set[str] = set()
    seen_temporaries: set[str] = set()
    for index, item in enumerate(raw_targets):
        if not isinstance(item, dict) or set(item) != _JOURNAL_TARGET_FIELDS:
            raise JournalRecoveryConflict(
                f"Operation journal target {index} has unknown or missing fields"
            )
        target, _ = _journal_relative_path(
            memory_home, item["target"], f"target {index}"
        )
        temporary, _ = _journal_relative_path(
            memory_home, item["temporary"], f"temporary {index}"
        )
        target_parts = PurePosixPath(target).parts
        temporary_parts = PurePosixPath(temporary).parts
        if (
            len(target_parts) != 3
            or target_parts[0] != "vault"
            or not target_parts[2].endswith("-session.md")
        ):
            raise JournalRecoveryConflict(
                "Operation journal target must be a project session document"
            )
        try:
            registered_scope = ProjectRegistry(memory_home).resolve(target_parts[1])
        except ProjectResolutionError as error:
            raise JournalRecoveryConflict(
                "Operation journal target project cannot be resolved"
            ) from error
        target_project = (
            registered_scope.identity.key
            if registered_scope is not None
            else target_parts[1]
        )
        if target_project not in project_keys:
            raise JournalRecoveryConflict(
                "Operation journal target must be a project session document"
            )
        expected_temp_prefix = f".{target_parts[2]}."
        if (
            temporary_parts[:-1] != target_parts[:-1]
            or not temporary_parts[-1].startswith(expected_temp_prefix)
            or not temporary_parts[-1].endswith(".tmp")
        ):
            raise JournalRecoveryConflict(
                "Operation journal temporary must be an exact sibling prepared write"
            )
        if target in seen_targets:
            raise JournalRecoveryConflict("Operation journal has duplicate targets")
        if temporary in seen_temporaries or target == temporary:
            raise JournalRecoveryConflict("Operation journal has duplicate temporary paths")
        seen_targets.add(target)
        seen_temporaries.add(temporary)
        targets.append(
            JournalTarget(
                target=target,
                temporary=temporary,
                before_sha256=_validate_digest(
                    item["before_sha256"],
                    nullable=True,
                    label=f"target {index} before digest",
                ),
                after_sha256=str(
                    _validate_digest(
                        item["after_sha256"],
                        nullable=False,
                        label=f"target {index} after digest",
                    )
                ),
            )
        )

    created_at = payload["created_at"]
    if not isinstance(created_at, str) or not created_at:
        raise JournalRecoveryConflict("Operation journal created_at is invalid")
    try:
        datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise JournalRecoveryConflict(
            "Operation journal created_at must be ISO 8601"
        ) from error
    actor = payload.get("actor")
    if actor is not None and (not isinstance(actor, str) or not actor.strip()):
        raise JournalRecoveryConflict("Operation journal actor is invalid")
    if action == "delete" and actor is None:
        raise JournalRecoveryConflict("Delete journals require actor provenance")
    return OperationJournal(
        schema_version=1,
        operation_id=operation_id,
        action=action,
        project_keys=project_keys,
        affected_memory_ids=tuple(raw_ids),
        canonical_memory_id=canonical_memory_id,
        targets=tuple(targets),
        created_at=created_at,
        actor=actor,
    )


def load_operation_journal(memory_home: Path, journal_path: Path) -> OperationJournal:
    """Strictly load a metadata-only operation journal without changing state."""
    root = memory_home.resolve()
    transactions = root / "transactions"
    resolved_path = journal_path.resolve(strict=False)
    if resolved_path.parent != transactions:
        raise JournalRecoveryConflict("Operation journal is outside transactions")
    try:
        payload = _strict_json_object(
            read_regular_text_bounded(journal_path),
            "operation journal",
        )
    except (OSError, UnicodeError) as error:
        raise JournalRecoveryConflict("Operation journal cannot be read") from error
    return _operation_from_payload(memory_home, journal_path, payload)


def persist_operation_journal(
    memory_home: Path,
    operation: OperationJournal,
) -> Path:
    """Atomically persist one complete metadata-only recovery journal."""
    root = memory_home.resolve()
    transactions = root / "transactions"
    journal_path = transactions / f"{operation.operation_id}.json"
    payload = json.loads(json.dumps(asdict(operation)))
    validated = _operation_from_payload(memory_home, journal_path, payload)
    serialized = (
        json.dumps(
            asdict(validated),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    )
    try:
        publish_text_exclusive(transactions, journal_path.name, serialized)
    except (FileExistsError, OSError, ValueError) as error:
        raise JournalRecoveryConflict(
            f"Unable to publish operation journal: {operation.operation_id}"
        ) from error
    return journal_path


def inspect_operation_journal_state(
    memory_home: Path,
    operation: OperationJournal,
) -> None:
    """Validate whether a journal can roll forward without changing state."""
    final_ids: set[str] = set()
    for target_info in sorted(operation.targets, key=lambda item: item.target):
        _target_name, target = _journal_relative_path(
            memory_home,
            target_info.target,
            "journal target",
        )
        _temporary_name, temporary = _journal_relative_path(
            memory_home,
            target_info.temporary,
            "journal temporary",
        )
        current_digest = digest_file(target)
        if current_digest == target_info.after_sha256:
            final_source = target
        elif current_digest == target_info.before_sha256:
            if digest_file(temporary) != target_info.after_sha256:
                raise JournalRecoveryConflict(
                    f"Required prepared temporary is missing or corrupt: {target_info.target}"
                )
            final_source = temporary
        else:
            raise JournalRecoveryConflict(
                f"Canonical target digest is neither before nor after: {target_info.target}"
            )
        try:
            document = parse_session_file(final_source)
        except Exception as error:
            raise JournalRecoveryConflict(
                f"Prepared canonical document is invalid: {target_info.target}"
            ) from error
        if document.schema_version != 2:
            raise JournalRecoveryConflict("Recovery requires schema-v2 documents")
        storage_key = PurePosixPath(target_info.target).parts[1]
        try:
            registered = ProjectRegistry(memory_home).resolve(storage_key)
        except ProjectResolutionError as error:
            raise JournalRecoveryConflict(
                "Journal target project cannot be resolved"
            ) from error
        canonical_key = (
            registered.identity.key if registered is not None else storage_key
        )
        storage_keys = (
            registered.storage_keys if registered is not None else (storage_key,)
        )
        if canonical_key not in operation.project_keys:
            raise JournalRecoveryConflict(
                "Journal target belongs to a different project scope"
            )
        if document.project not in storage_keys and document.project != canonical_key:
            raise JournalRecoveryConflict(
                "Recovered document has a wrong project scope"
            )
        for entry in document.entries:
            memory = entry.to_memory(str(target))
            if memory.project not in storage_keys and memory.project != canonical_key:
                raise JournalRecoveryConflict(
                    f"Memory {memory.id} has a wrong project in recovered Markdown"
                )
            if memory.content_fingerprint != content_fingerprint(memory):
                raise JournalRecoveryConflict(
                    f"Memory {memory.id} has an invalid recovered fingerprint"
                )
            if memory.id in final_ids:
                raise JournalRecoveryConflict(
                    f"Memory {memory.id} appears in multiple recovered documents"
                )
            final_ids.add(memory.id)

    affected = set(operation.affected_memory_ids)
    present = affected.intersection(final_ids)
    if operation.action == "delete" and present:
        raise JournalRecoveryConflict(
            "Deleted memories remain in recovered Markdown: "
            + ", ".join(sorted(present))
        )
    if operation.action != "delete" and present != affected:
        raise JournalRecoveryConflict(
            "Affected memories are missing from recovered Markdown: "
            + ", ".join(sorted(affected - present))
        )


@contextmanager
def acquire_project_locks(
    memory_home: Path,
    project_keys: Sequence[str],
    lock_factory=ProcessFileLock,
) -> Iterator[None]:
    """Acquire unique project locks in lexical order and release in reverse."""
    keys = sorted(set(validate_storage_key(key) for key in project_keys))
    if not keys:
        raise ProjectResolutionError("At least one project storage key is required")
    root = memory_home.resolve()
    locks_root = root / "locks"
    if locks_root.resolve(strict=False) != locks_root:
        raise ProjectResolutionError(
            "Resolved project lock root escapes containment and changes identity"
        )
    paths: list[Path] = []
    for key in keys:
        path = locks_root / f"{key}.lock"
        if path.resolve(strict=False) != path:
            raise ProjectResolutionError(
                "Resolved project lock escapes containment and changes identity"
            )
        paths.append(path)
    with ExitStack() as stack:
        for path in paths:
            stack.enter_context(lock_factory(path))
        yield


@dataclass(frozen=True)
class SaveRequest:
    raw: RawMemoryInput
    project: str
    source: str | None
    operation_id: str
    timestamp: str


class SaveConflict(ValueError):
    """Raised when an operation ID is reused with a different request."""


class CanonicalDriftError(RuntimeError):
    """Raised when derived SQLite state has no canonical Markdown source."""


class LegacyMetadataRequiredError(RuntimeError):
    """Raised when a canonical save would rewrite schema-v1 Markdown."""


FaultCallback = Callable[[str], None]
EmbedCallback = Callable[[str], list[float]]


@dataclass(frozen=True)
class _CanonicalEntry:
    path: Path
    document: SessionDocument
    entry: SessionEntry


@dataclass(frozen=True)
class _PersistenceScope:
    canonical_key: str
    storage_keys: tuple[str, ...]


@dataclass
class _MutationPlan:
    action: str
    actor: str | None
    project_key: str
    documents: dict[Path, SessionDocument]
    affected_memory_ids: tuple[str, ...]
    canonical_memory_id: str | None
    deleted_memory_ids: tuple[str, ...]
    deleted_operations: tuple[tuple[str, MemoryOperation], ...]
    invalidate_memory_ids: tuple[str, ...]
    embed_memories: tuple[Memory, ...]
    operation_alias_keys: tuple[str, ...]
    result: dict[str, object]


_VECTOR_WARNING = "Memory saved, but semantic indexing is temporarily unavailable."


def embedding_text(
    *,
    title: str,
    what: str,
    why: str | None,
    impact: str | None,
    tags: Sequence[str],
) -> str:
    fields = (title, what, why or "", impact or "", " ".join(tags))
    return " ".join(value.strip() for value in fields if value.strip())


def content_fingerprint(memory: Memory) -> str:
    payload = embedding_text(
        title=memory.title,
        what=memory.what,
        why=memory.why,
        impact=memory.impact,
        tags=memory.tags,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def request_fingerprint(
    raw: RawMemoryInput,
    project: str,
    source: str | None,
) -> str:
    """Hash the normalized, redacted request and authoritative identity."""
    normalized = redact_memory_input(raw, [])
    normalized.source = redact(source, []) if source is not None else None
    payload = {
        "project": project,
        "source": normalized.source,
        "raw": asdict(normalized),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class CanonicalPersistence:
    def __init__(
        self,
        memory_home: Path,
        db: MemoryDB,
        patterns: list[str],
        fault: FaultCallback | None = None,
    ):
        self.memory_home = memory_home
        self.vault_dir = memory_home / "vault"
        self.db = db
        self.patterns = patterns
        self.fault = fault or (lambda phase: None)
        self.embed: EmbedCallback | None = None
        self.startup_recoveries: tuple[str, ...] = ()
        self.startup_vector_repairs: tuple[str, ...] = ()

    def save(self, request: SaveRequest) -> dict[str, object]:
        """Persist one operation with Markdown as the canonical source."""
        scope = self._resolve_scope(request.project)
        operation_id = self._canonical_uuid(request.operation_id, "Operation ID")
        validated_request = replace(
            request,
            project=scope.canonical_key,
            operation_id=operation_id,
        )
        redacted = redact_memory_input(validated_request.raw, self.patterns)
        source = (
            redact(validated_request.source, self.patterns)
            if validated_request.source is not None
            else None
        )
        redacted.source = source
        canonical_request = replace(validated_request, raw=redacted, source=source)
        fingerprints = {
            storage_key: request_fingerprint(redacted, storage_key, source)
            for storage_key in scope.storage_keys
        }
        fingerprint = fingerprints[scope.canonical_key]

        project_dirs = tuple(
            (storage_key, self._project_dir(storage_key))
            for storage_key in scope.storage_keys
        )
        with self._locked_after_recovery((scope.canonical_key,)):
            documents = self._read_project_documents(project_dirs)
            canonical_operation = self._find_canonical_operation(
                documents,
                operation_id,
            )
            if canonical_operation is not None:
                canonical, operation = canonical_operation
                if operation.request_fingerprint not in fingerprints.values():
                    raise SaveConflict(
                        f"Operation {operation_id!r} was already used with "
                        "a different request"
                    )
                memory = canonical.entry.to_memory(str(canonical.path))
                memory.id = self._canonical_uuid(
                    memory.id,
                    "Canonical memory ID",
                    CanonicalDriftError,
                )
                if memory.project not in scope.storage_keys:
                    raise CanonicalDriftError(
                        f"Canonical memory {memory.id} is outside the resolved project scope"
                    )
                details = canonical.entry.details
                embedding_bytes = self._embedding_bytes(memory)
                self._validate_content_fingerprint(memory, embedding_bytes)
                operation = replace(operation, operation_id=operation_id)
                projection = self.db.get_memory(memory.id)
                vector_is_current = (
                    projection is not None
                    and projection.get("content_fingerprint")
                    == memory.content_fingerprint
                    and self.db.has_vector(memory.id)
                )
                memory.project = scope.canonical_key
                canonical.document.project = scope.canonical_key
                upsert_session_memory_entry(canonical.document, memory, details)
                plan = _MutationPlan(
                    action=(
                        "create" if operation.action == "created" else "update"
                    ),
                    actor=operation.source,
                    project_key=scope.canonical_key,
                    documents={canonical.path: canonical.document},
                    affected_memory_ids=(memory.id,),
                    canonical_memory_id=memory.id,
                    deleted_memory_ids=(),
                    deleted_operations=(),
                    invalidate_memory_ids=(memory.id,) if not vector_is_current else (),
                    embed_memories=(copy.deepcopy(memory),),
                    operation_alias_keys=tuple(
                        key for key in scope.storage_keys if key != scope.canonical_key
                    ),
                    result={
                        "id": memory.id,
                        "file_path": str(canonical.path),
                        "action": "replayed",
                    },
                )
                result, embeddings = self._commit_mutation_locked(
                    plan,
                    operation_id,
                    validated_request.timestamp,
                )
            else:
                ledgers = [
                    self.db.get_operation(storage_key, operation_id)
                    for storage_key in scope.storage_keys
                ]
                if any(ledger is not None for ledger in ledgers):
                    raise CanonicalDriftError(
                        f"Operation {operation_id!r} exists only in SQLite"
                    )
                memory, details, document, target, action = self._prepare_change(
                    canonical_request,
                    fingerprint,
                    documents,
                    scope.storage_keys,
                )
                embedding_bytes = self._embedding_bytes(memory)
                memory.content_fingerprint = self._fingerprint_bytes(embedding_bytes)
                upsert_session_memory_entry(document, memory, details)
                projection = self.db.get_memory(memory.id)
                requires_vector = (
                    projection is None
                    or projection.get("content_fingerprint")
                    != memory.content_fingerprint
                    or not self.db.has_vector(memory.id)
                )
                plan = _MutationPlan(
                    action="create" if action == "created" else "update",
                    actor=source,
                    project_key=scope.canonical_key,
                    documents={target: document},
                    affected_memory_ids=(memory.id,),
                    canonical_memory_id=memory.id,
                    deleted_memory_ids=(),
                    deleted_operations=(),
                    invalidate_memory_ids=(memory.id,) if requires_vector else (),
                    embed_memories=(copy.deepcopy(memory),) if requires_vector else (),
                    operation_alias_keys=tuple(
                        key for key in scope.storage_keys if key != scope.canonical_key
                    ),
                    result={
                        "id": memory.id,
                        "file_path": str(target),
                        "action": action,
                    },
                )
                result, embeddings = self._commit_mutation_locked(
                    plan,
                    operation_id,
                    validated_request.timestamp,
                )

        self.fault("before_vector_write")
        return self._finish_mutation(result, embeddings)

    def update(
        self,
        memory_id: str,
        patch: MemoryPatch,
        *,
        actor: str,
    ) -> dict[str, object]:
        """Apply an explicit patch through the canonical mutation coordinator."""
        actor = self._validate_actor(actor)
        full_id, scope = self._scope_for_memory(memory_id)
        operation_id = str(uuid.uuid4())
        timestamp = datetime.now().astimezone().isoformat()
        with self._locked_after_recovery((scope.canonical_key,)):
            documents = self._scope_documents(scope)
            canonical = self._find_canonical_memory(documents, full_id)
            self._require_v2(canonical.document)
            memory = canonical.entry.to_memory(str(canonical.path))
            self._require_memory_scope(memory, scope)
            details = canonical.entry.details
            previous_fingerprint = memory.content_fingerprint

            if not isinstance(patch, MemoryPatch):
                raise TypeError("patch must be a MemoryPatch")
            if patch.title is not UNSET:
                if not isinstance(patch.title, str):
                    raise TypeError("title must be a string")
                memory.title = redact(patch.title, self.patterns)
            if patch.what is not UNSET:
                if not isinstance(patch.what, str):
                    raise TypeError("what must be a string")
                memory.what = redact(patch.what, self.patterns)
            for name in ("why", "impact", "category"):
                value = getattr(patch, name)
                if value is UNSET:
                    continue
                if value is not None and not isinstance(value, str):
                    raise TypeError(f"{name} must be a string or null")
                setattr(
                    memory,
                    name,
                    redact(value, self.patterns) if value is not None else None,
                )
            if patch.tags is not UNSET:
                if not isinstance(patch.tags, list) or any(
                    not isinstance(item, str) for item in patch.tags
                ):
                    raise TypeError("tags must be a list of strings")
                memory.tags = [redact(item, self.patterns) for item in patch.tags]
            if patch.details is not UNSET:
                if patch.details is not None and not isinstance(patch.details, str):
                    raise TypeError("details must be a string or null")
                details = (
                    redact(patch.details, self.patterns)
                    if patch.details is not None
                    else None
                )

            operation = self._append_operation(
                memory,
                operation_id=operation_id,
                action="updated",
                actor=actor,
                timestamp=timestamp,
            )
            memory.content_fingerprint = content_fingerprint(memory)
            memory.project = scope.canonical_key
            canonical.document.project = scope.canonical_key
            upsert_session_memory_entry(canonical.document, memory, details)
            changed_content = memory.content_fingerprint != previous_fingerprint
            plan = _MutationPlan(
                action="update",
                actor=actor,
                project_key=scope.canonical_key,
                documents={canonical.path: canonical.document},
                affected_memory_ids=(memory.id,),
                canonical_memory_id=memory.id,
                deleted_memory_ids=(),
                deleted_operations=(),
                invalidate_memory_ids=(memory.id,) if changed_content else (),
                embed_memories=(copy.deepcopy(memory),) if changed_content else (),
                operation_alias_keys=(),
                result={
                    "id": memory.id,
                    "file_path": str(canonical.path),
                    "action": "updated",
                },
            )
            result, embeddings = self._commit_mutation_locked(
                plan,
                operation_id,
                timestamp,
            )
        return self._finish_mutation(result, embeddings)

    def archive(
        self,
        memory_id: str,
        *,
        reason: str,
        superseded_by: str | None = None,
        actor: str,
    ) -> dict[str, object]:
        actor = self._validate_actor(actor)
        if not isinstance(reason, str):
            raise TypeError("reason must be a string")
        full_id, scope = self._scope_for_memory(memory_id)
        canonical_superseder: str | None = None
        if superseded_by is not None:
            canonical_superseder, superseder_scope = self._scope_for_memory(superseded_by)
            if superseder_scope.canonical_key != scope.canonical_key:
                raise ValueError("Superseding memory must belong to the same project")
        operation_id = str(uuid.uuid4())
        timestamp = datetime.now().astimezone().isoformat()
        with self._locked_after_recovery((scope.canonical_key,)):
            documents = self._scope_documents(scope)
            canonical = self._find_canonical_memory(documents, full_id)
            memory = canonical.entry.to_memory(str(canonical.path))
            self._require_memory_scope(memory, scope)
            memory.status = "archived"
            memory.archived_at = timestamp
            memory.archive_reason = redact(reason, self.patterns)
            memory.superseded_by = canonical_superseder
            self._append_operation(
                memory,
                operation_id=operation_id,
                action="archived",
                actor=actor,
                timestamp=timestamp,
            )
            memory.content_fingerprint = content_fingerprint(memory)
            memory.project = scope.canonical_key
            canonical.document.project = scope.canonical_key
            upsert_session_memory_entry(canonical.document, memory, canonical.entry.details)
            plan = _MutationPlan(
                action="archive",
                actor=actor,
                project_key=scope.canonical_key,
                documents={canonical.path: canonical.document},
                affected_memory_ids=(memory.id,),
                canonical_memory_id=memory.id,
                deleted_memory_ids=(),
                deleted_operations=(),
                invalidate_memory_ids=(memory.id,),
                embed_memories=(),
                operation_alias_keys=(),
                result={"id": memory.id, "file_path": str(canonical.path), "action": "archived"},
            )
            result, embeddings = self._commit_mutation_locked(plan, operation_id, timestamp)
        return self._finish_mutation(result, embeddings)

    def restore(self, memory_id: str, *, actor: str) -> dict[str, object]:
        actor = self._validate_actor(actor)
        full_id, scope = self._scope_for_memory(memory_id)
        operation_id = str(uuid.uuid4())
        timestamp = datetime.now().astimezone().isoformat()
        with self._locked_after_recovery((scope.canonical_key,)):
            documents = self._scope_documents(scope)
            canonical = self._find_canonical_memory(documents, full_id)
            memory = canonical.entry.to_memory(str(canonical.path))
            self._require_memory_scope(memory, scope)
            memory.status = "active"
            memory.archived_at = None
            memory.archive_reason = None
            memory.superseded_by = None
            self._append_operation(
                memory,
                operation_id=operation_id,
                action="restored",
                actor=actor,
                timestamp=timestamp,
            )
            memory.content_fingerprint = content_fingerprint(memory)
            memory.project = scope.canonical_key
            canonical.document.project = scope.canonical_key
            upsert_session_memory_entry(canonical.document, memory, canonical.entry.details)
            plan = _MutationPlan(
                action="restore",
                actor=actor,
                project_key=scope.canonical_key,
                documents={canonical.path: canonical.document},
                affected_memory_ids=(memory.id,),
                canonical_memory_id=memory.id,
                deleted_memory_ids=(),
                deleted_operations=(),
                invalidate_memory_ids=(memory.id,),
                embed_memories=(copy.deepcopy(memory),),
                operation_alias_keys=(),
                result={"id": memory.id, "file_path": str(canonical.path), "action": "restored"},
            )
            result, embeddings = self._commit_mutation_locked(plan, operation_id, timestamp)
        return self._finish_mutation(result, embeddings)

    def merge(
        self,
        canonical_id: str,
        source_ids: Sequence[str],
        *,
        actor: str,
    ) -> dict[str, object]:
        actor = self._validate_actor(actor)
        if not source_ids:
            raise ValueError("At least one source memory is required")
        canonical_full, scope = self._scope_for_memory(canonical_id)
        source_full: list[str] = []
        for source_id in source_ids:
            full_id, source_scope = self._scope_for_memory(source_id)
            if source_scope.canonical_key != scope.canonical_key:
                raise ValueError("Cross-project merges are not allowed")
            source_full.append(full_id)
        if len(set(source_full)) != len(source_full):
            raise ValueError("Source memory IDs must be unique")
        if canonical_full in source_full:
            raise ValueError("Canonical memory cannot also be a source memory")

        operation_id = str(uuid.uuid4())
        timestamp = datetime.now().astimezone().isoformat()
        with self._locked_after_recovery((scope.canonical_key,)):
            documents = self._scope_documents(scope)
            canonical_entry = self._find_canonical_memory(documents, canonical_full)
            source_entries = [
                self._find_canonical_memory(documents, source_id)
                for source_id in source_full
            ]
            canonical_memory = canonical_entry.entry.to_memory(str(canonical_entry.path))
            self._require_memory_scope(canonical_memory, scope)
            canonical_details = canonical_entry.entry.details
            previous_fingerprint = canonical_memory.content_fingerprint
            seen_tags = {item.casefold() for item in canonical_memory.tags}
            detail_parts: list[str] = []
            if canonical_details is not None:
                detail_parts.append(canonical_details)
            source_memories: list[Memory] = []
            changed_documents: dict[Path, SessionDocument] = {
                canonical_entry.path: canonical_entry.document
            }
            for source_entry in source_entries:
                source_memory = source_entry.entry.to_memory(str(source_entry.path))
                self._require_memory_scope(source_memory, scope)
                source_memories.append(source_memory)
                changed_documents[source_entry.path] = source_entry.document
                for tag in source_memory.tags:
                    key = tag.casefold()
                    if key not in seen_tags:
                        canonical_memory.tags.append(tag)
                        seen_tags.add(key)
                if canonical_memory.why is None and source_memory.why is not None:
                    canonical_memory.why = source_memory.why
                if canonical_memory.impact is None and source_memory.impact is not None:
                    canonical_memory.impact = source_memory.impact
                fragment = (
                    f"Merged from: {source_memory.title} ({source_memory.id[:12]})\n"
                    f"{source_memory.what}"
                )
                if source_entry.entry.details is not None:
                    fragment += "\n" + source_entry.entry.details
                detail_parts.append(fragment)

            canonical_details = "\n\n".join(detail_parts) if detail_parts else None
            canonical_operation_id = self._derived_operation_id(
                operation_id, canonical_memory.id
            )
            self._append_operation(
                canonical_memory,
                operation_id=canonical_operation_id,
                action="merged",
                actor=actor,
                timestamp=timestamp,
            )
            canonical_memory.content_fingerprint = content_fingerprint(canonical_memory)
            canonical_memory.project = scope.canonical_key
            canonical_entry.document.project = scope.canonical_key
            upsert_session_memory_entry(
                canonical_entry.document,
                canonical_memory,
                canonical_details,
            )
            for source_memory, source_entry in zip(source_memories, source_entries):
                source_memory.status = "archived"
                source_memory.archived_at = timestamp
                source_memory.archive_reason = "merged"
                source_memory.superseded_by = canonical_memory.id
                self._append_operation(
                    source_memory,
                    operation_id=self._derived_operation_id(
                        operation_id, source_memory.id
                    ),
                    action="archived",
                    actor=actor,
                    timestamp=timestamp,
                )
                source_memory.content_fingerprint = content_fingerprint(source_memory)
                source_memory.project = scope.canonical_key
                source_entry.document.project = scope.canonical_key
                upsert_session_memory_entry(
                    source_entry.document,
                    source_memory,
                    source_entry.entry.details,
                )
            canonical_changed = (
                canonical_memory.content_fingerprint != previous_fingerprint
            )
            affected = (canonical_memory.id, *(memory.id for memory in source_memories))
            invalidated = tuple(
                [*(memory.id for memory in source_memories)]
                + ([canonical_memory.id] if canonical_changed else [])
            )
            plan = _MutationPlan(
                action="merge",
                actor=actor,
                project_key=scope.canonical_key,
                documents=changed_documents,
                affected_memory_ids=affected,
                canonical_memory_id=canonical_memory.id,
                deleted_memory_ids=(),
                deleted_operations=(),
                invalidate_memory_ids=invalidated,
                embed_memories=(copy.deepcopy(canonical_memory),) if canonical_changed else (),
                operation_alias_keys=(),
                result={"id": canonical_memory.id, "merged": len(source_memories), "action": "merged"},
            )
            result, embeddings = self._commit_mutation_locked(plan, operation_id, timestamp)
        return self._finish_mutation(result, embeddings)

    def delete(self, memory_id: str, *, actor: str) -> bool:
        actor = self._validate_actor(actor)
        try:
            full_id, scope = self._scope_for_memory(memory_id)
        except ValueError:
            return False
        operation_id = str(uuid.uuid4())
        timestamp = datetime.now().astimezone().isoformat()
        with self._locked_after_recovery((scope.canonical_key,)):
            documents = self._scope_documents(scope)
            canonical = self._find_canonical_memory(documents, full_id)
            memory = canonical.entry.to_memory(str(canonical.path))
            self._require_memory_scope(memory, scope)
            delete_operation = MemoryOperation(
                operation_id=operation_id,
                source=actor,
                action="deleted",
                request_fingerprint="mutation:" + operation_id,
                timestamp=timestamp,
                branch=None,
                commit_sha=None,
            )
            canonical.document.entries = [
                entry for entry in canonical.document.entries if entry.id != memory.id
            ]
            canonical.document.project = scope.canonical_key
            assign_entry_anchors(canonical.document.entries)
            plan = _MutationPlan(
                action="delete",
                actor=actor,
                project_key=scope.canonical_key,
                documents={canonical.path: canonical.document},
                affected_memory_ids=(memory.id,),
                canonical_memory_id=None,
                deleted_memory_ids=(memory.id,),
                deleted_operations=((memory.id, delete_operation),),
                invalidate_memory_ids=(memory.id,),
                embed_memories=(),
                operation_alias_keys=(),
                result={"id": memory.id, "action": "deleted"},
            )
            self._commit_mutation_locked(plan, operation_id, timestamp)
        return True

    def resolve_memory_id(self, memory_id: str) -> str:
        """Resolve an exact ID or one unique literal prefix to its full ID."""
        full_id, _scope = self._scope_for_memory(memory_id)
        return full_id

    def _validate_actor(self, actor: object) -> str:
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("actor must be a non-empty string")
        return redact(actor, self.patterns)

    def _scope_for_memory(self, memory_id: str) -> tuple[str, _PersistenceScope]:
        if not isinstance(memory_id, str) or not memory_id:
            raise ValueError("Memory ID must be a non-empty string")
        rows = self.db.conn.execute(
            "SELECT id, project FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchall()
        if not rows:
            rows = self.db.conn.execute(
                "SELECT id, project FROM memories "
                "WHERE substr(id, 1, length(?)) = ? ORDER BY id",
                (memory_id, memory_id),
            ).fetchall()
        if len(rows) != 1:
            if not rows:
                raise ValueError(f"Unknown memory: {memory_id}")
            raise ValueError(f"Ambiguous memory ID prefix: {memory_id}")
        return str(rows[0]["id"]), self._resolve_scope(str(rows[0]["project"]))

    def _scope_documents(
        self,
        scope: _PersistenceScope,
    ) -> dict[Path, SessionDocument]:
        return self._read_project_documents(
            tuple(
                (storage_key, self._project_dir(storage_key))
                for storage_key in scope.storage_keys
            )
        )

    @contextmanager
    def _locked_after_recovery(
        self,
        project_keys: Sequence[str],
    ) -> Iterator[None]:
        closure, _journals = self._journal_lock_closure(project_keys)
        recovered_embeddings: list[tuple[str, str, bytes]] = []
        try:
            with acquire_project_locks(self.memory_home, closure):
                current_closure, current_journals = self._journal_lock_closure(project_keys)
                if tuple(current_closure) != tuple(closure):
                    raise JournalRecoveryConflict(
                        "Pending operation lock closure changed during acquisition"
                    )
                for journal_path, operation in current_journals:
                    if set(operation.project_keys).intersection(closure):
                        recovered_embeddings.extend(
                            self._recover_journal_locked(journal_path, operation)
                        )
                yield
        finally:
            for memory_id, fingerprint, payload in recovered_embeddings:
                status, _warning = self._write_vector(
                    memory_id,
                    fingerprint,
                    payload,
                )
                if status == "ready":
                    self.db.clear_vector_repair(memory_id, fingerprint)

    def _append_operation(
        self,
        memory: Memory,
        *,
        operation_id: str,
        action: str,
        actor: str,
        timestamp: str,
    ) -> MemoryOperation:
        memory.updated_at = timestamp
        memory.last_updated_by = actor
        if actor not in memory.contributors:
            memory.contributors.append(actor)
        memory.updated_count += 1
        operation = MemoryOperation(
            operation_id=operation_id,
            source=actor,
            action=action,
            request_fingerprint="mutation:" + operation_id,
            timestamp=timestamp,
            branch=None,
            commit_sha=None,
        )
        memory.operations.append(operation)
        return operation

    @staticmethod
    def _derived_operation_id(operation_id: str, memory_id: str) -> str:
        return str(uuid.uuid5(uuid.UUID(operation_id), memory_id))

    @staticmethod
    def _require_memory_scope(memory: Memory, scope: _PersistenceScope) -> None:
        if memory.project not in scope.storage_keys:
            raise CanonicalDriftError(
                f"Canonical memory {memory.id} is outside the resolved project scope"
            )

    def _commit_mutation_locked(
        self,
        plan: _MutationPlan,
        operation_id: str,
        timestamp: str,
    ) -> tuple[dict[str, object], tuple[tuple[str, str, bytes], ...]]:
        prepared_by_path: list[tuple[Path, PreparedAtomicWrite]] = []
        for path, document in sorted(plan.documents.items(), key=lambda item: str(item[0])):
            self._require_v2(document)
            assign_entry_anchors(document.entries)
            for entry in document.entries:
                if entry.metadata_complete and entry.section_anchor is not None:
                    entry.metadata["section_anchor"] = entry.section_anchor
            prepared_by_path.append(
                (path, prepare_atomic_text(path, self._render_document(document)))
            )
        self.fault("after_all_temps_fsync")
        journal = OperationJournal(
            schema_version=1,
            operation_id=operation_id,
            action=plan.action,
            project_keys=(plan.project_key,),
            affected_memory_ids=plan.affected_memory_ids,
            canonical_memory_id=plan.canonical_memory_id,
            targets=tuple(
                JournalTarget.from_prepared(self.memory_home, prepared)
                for _, prepared in prepared_by_path
            ),
            created_at=timestamp,
            actor=plan.actor,
        )
        journal_path = persist_operation_journal(self.memory_home, journal)
        self.fault("after_journal_fsync")

        projected: dict[str, tuple[Memory, str | None]] = {}
        for path, document in plan.documents.items():
            for entry in document.entries:
                memory = entry.to_memory(str(path))
                existing = projected.get(memory.id)
                if existing is not None:
                    raise CanonicalDriftError(
                        f"Memory {memory.id!r} appears in multiple final documents"
                    )
                projected[memory.id] = (memory, entry.details)

        with self.db.transaction():
            for memory, details in projected.values():
                self.db.upsert_memory(memory, details)
            for memory_id in plan.affected_memory_ids:
                if memory_id in projected:
                    memory = projected[memory_id][0]
                    if memory.operations:
                        self.db.upsert_operation(
                            plan.project_key,
                            memory.id,
                            memory.operations[-1],
                        )
                        if plan.operation_alias_keys:
                            placeholders = ", ".join(
                                "?" for _ in plan.operation_alias_keys
                            )
                            self.db.conn.execute(
                                f"DELETE FROM save_operations "
                                f"WHERE operation_id = ? "
                                f"AND project IN ({placeholders})",
                                (
                                    memory.operations[-1].operation_id,
                                    *plan.operation_alias_keys,
                                ),
                            )
            for memory_id, operation in plan.deleted_operations:
                self.db.upsert_operation(plan.project_key, memory_id, operation)
            for memory_id in plan.deleted_memory_ids:
                self.db.delete_memory(memory_id)
            for memory_id in plan.invalidate_memory_ids:
                self.db.invalidate_vector(memory_id)
                self.db.clear_vector_repair(memory_id)
            for memory in plan.embed_memories:
                if memory.status == "active" and memory.content_fingerprint is not None:
                    self.db.queue_vector_repair(
                        memory.id,
                        memory.content_fingerprint,
                        timestamp,
                    )
            self.fault("after_db_write")
            for index, (_path, prepared) in enumerate(
                sorted(prepared_by_path, key=lambda item: str(item[0]))
            ):
                prepared.replace()
                self.fault(f"after_target_replace:{index}")
        self.fault("after_db_commit")
        journal_path.unlink()
        fsync_directory(journal_path.parent)
        self.fault("after_journal_remove")

        embeddings = tuple(
            (
                memory.id,
                str(memory.content_fingerprint),
                self._embedding_bytes(memory),
            )
            for memory in plan.embed_memories
            if memory.status == "active" and memory.content_fingerprint is not None
        )
        return dict(plan.result), embeddings

    def _finish_mutation(
        self,
        result: dict[str, object],
        embeddings: Sequence[tuple[str, str, bytes]],
    ) -> dict[str, object]:
        statuses: list[str] = []
        warning: str | None = None
        for memory_id, fingerprint, payload in embeddings:
            status, current_warning = self._write_vector(
                memory_id,
                fingerprint,
                payload,
            )
            if status == "ready":
                self.db.clear_vector_repair(memory_id, fingerprint)
            statuses.append(status)
            warning = warning or current_warning
        result["vector_status"] = (
            "ready" if not statuses or all(status == "ready" for status in statuses)
            else "degraded"
        )
        if warning is not None:
            result["warning"] = warning
        return result

    def recover_pending_operations(
        self,
        project_keys: Sequence[str],
    ) -> list[str]:
        """Recover every pending journal intersecting the complete lock closure."""
        closure, _journals = self._journal_lock_closure(project_keys)
        if not closure:
            return []
        recovered: list[str] = []
        embeddings: list[tuple[str, str, bytes]] = []
        with acquire_project_locks(self.memory_home, closure):
            current_closure, current_journals = self._journal_lock_closure(project_keys)
            if tuple(current_closure) != tuple(closure):
                raise JournalRecoveryConflict(
                    "Pending operation lock closure changed during acquisition"
                )
            for journal_path, operation in current_journals:
                if not set(operation.project_keys).intersection(closure):
                    continue
                embeddings.extend(self._recover_journal_locked(journal_path, operation))
                recovered.append(operation.operation_id)
        for memory_id, fingerprint, payload in embeddings:
            status, _warning = self._write_vector(memory_id, fingerprint, payload)
            if status == "ready":
                self.db.clear_vector_repair(memory_id, fingerprint)
        return recovered

    def repair_pending_vectors(self) -> tuple[str, ...]:
        """Drain durable vector work with fingerprint compare-and-swap safety."""
        repaired: list[str] = []
        for queued in self.db.list_vector_repairs():
            memory_id = str(queued["memory_id"])
            fingerprint = str(queued["content_fingerprint"])
            row = self.db.get_memory(memory_id)
            if (
                row is None
                or (row.get("status") or "active") != "active"
                or row.get("content_fingerprint") != fingerprint
            ):
                self.db.clear_vector_repair(memory_id, fingerprint)
                continue
            try:
                raw_tags = json.loads(row.get("tags") or "[]")
            except (TypeError, json.JSONDecodeError):
                raw_tags = []
            tags = [item for item in raw_tags if isinstance(item, str)]
            payload = embedding_text(
                title=str(row.get("title") or ""),
                what=str(row.get("what") or ""),
                why=row.get("why") if isinstance(row.get("why"), str) else None,
                impact=(
                    row.get("impact")
                    if isinstance(row.get("impact"), str)
                    else None
                ),
                tags=tags,
            ).encode("utf-8")
            status, _warning = self._write_vector(memory_id, fingerprint, payload)
            if status == "ready":
                self.db.clear_vector_repair(memory_id, fingerprint)
                repaired.append(memory_id)
        return tuple(repaired)

    def _journal_lock_closure(
        self,
        project_keys: Sequence[str],
    ) -> tuple[tuple[str, ...], list[tuple[Path, OperationJournal]]]:
        requested = {validate_storage_key(key) for key in project_keys}
        transactions = self.memory_home.resolve() / "transactions"
        if os.path.lexists(transactions):
            try:
                transactions_metadata = transactions.lstat()
            except OSError as error:
                raise JournalRecoveryConflict(
                    "transactions cannot be inspected"
                ) from error
            if transactions.is_symlink() or not stat.S_ISDIR(
                transactions_metadata.st_mode
            ):
                raise JournalRecoveryConflict(
                    "transactions must be a local directory"
                )
        journals: list[tuple[Path, OperationJournal]] = []
        if transactions.is_dir():
            for journal_path in sorted(transactions.glob("*.json")):
                journals.append(
                    (
                        journal_path,
                        load_operation_journal(self.memory_home, journal_path),
                    )
                )
        if not requested:
            requested.update(
                key for _path, operation in journals for key in operation.project_keys
            )
        changed = True
        while changed:
            changed = False
            for _path, operation in journals:
                keys = set(operation.project_keys)
                if keys.intersection(requested) and not keys.issubset(requested):
                    requested.update(keys)
                    changed = True
        selected = [
            item
            for item in journals
            if set(item[1].project_keys).intersection(requested)
        ]
        return tuple(sorted(requested)), selected

    def _recover_journal_locked(
        self,
        journal_path: Path,
        operation: OperationJournal,
    ) -> list[tuple[str, str, bytes]]:
        """Preflight and roll one already-locked journal forward idempotently."""
        root = self.memory_home.resolve()
        preflight: list[
            tuple[JournalTarget, Path, Path, bool, SessionDocument]
        ] = []
        final_ids: dict[str, tuple[Memory, str | None]] = {}
        for target_info in sorted(operation.targets, key=lambda item: item.target):
            _target_name, target = _journal_relative_path(
                self.memory_home,
                target_info.target,
                "journal target",
            )
            _temporary_name, temporary = _journal_relative_path(
                self.memory_home,
                target_info.temporary,
                "journal temporary",
            )
            if target.resolve(strict=False) != target or temporary.resolve(strict=False) != temporary:
                raise JournalRecoveryConflict("Journal target path changed identity")
            current_digest = digest_file(target)
            if current_digest == target_info.after_sha256:
                final_source = target
                needs_replace = False
            elif current_digest == target_info.before_sha256:
                if digest_file(temporary) != target_info.after_sha256:
                    raise JournalRecoveryConflict(
                        f"Required prepared temporary is missing or corrupt: {target_info.target}"
                    )
                final_source = temporary
                needs_replace = True
            else:
                raise JournalRecoveryConflict(
                    f"Canonical target digest is neither before nor after: {target_info.target}"
                )
            try:
                document = parse_session_file(final_source)
            except Exception as error:
                raise JournalRecoveryConflict(
                    f"Prepared canonical document is invalid: {target_info.target}"
                ) from error
            if document.schema_version != 2:
                raise JournalRecoveryConflict("Recovery requires schema-v2 documents")
            target_parts = PurePosixPath(target_info.target).parts
            storage_key = target_parts[1]
            resolved_scope = self._resolve_scope(storage_key)
            if resolved_scope.canonical_key not in operation.project_keys:
                raise JournalRecoveryConflict(
                    "Journal target belongs to a different project scope"
                )
            if document.project not in resolved_scope.storage_keys:
                raise JournalRecoveryConflict(
                    "Recovered document has a wrong project scope"
                )
            for entry in document.entries:
                memory = entry.to_memory(str(target))
                if memory.project not in resolved_scope.storage_keys and (
                    memory.project != resolved_scope.canonical_key
                ):
                    raise JournalRecoveryConflict(
                        f"Memory {memory.id} has a wrong project in recovered Markdown"
                    )
                if memory.content_fingerprint != content_fingerprint(memory):
                    raise JournalRecoveryConflict(
                        f"Memory {memory.id} has an invalid recovered fingerprint"
                    )
                if memory.id in final_ids:
                    raise JournalRecoveryConflict(
                        f"Memory {memory.id} appears in multiple recovered documents"
                    )
                memory.project = resolved_scope.canonical_key
                memory.file_path = str(target)
                final_ids[memory.id] = (memory, entry.details)
            preflight.append(
                (target_info, target, temporary, needs_replace, document)
            )

        affected = set(operation.affected_memory_ids)
        present = affected.intersection(final_ids)
        if operation.action == "delete" and present:
            raise JournalRecoveryConflict(
                "Deleted memories remain in recovered Markdown: "
                + ", ".join(sorted(present))
            )
        if operation.action != "delete" and present != affected:
            missing = sorted(affected - present)
            raise JournalRecoveryConflict(
                "Affected memories are missing from recovered Markdown: "
                + ", ".join(missing)
            )

        for target_info, target, temporary, needs_replace, _document in preflight:
            if not needs_replace:
                continue
            original_mode = (
                stat.S_IMODE(target.stat().st_mode) if target.exists() else None
            )
            try:
                PreparedAtomicWrite(
                    target,
                    temporary,
                    original_mode,
                ).replace_if_digest(target_info.before_sha256)
            except ConcurrentModificationError as error:
                raise JournalRecoveryConflict(
                    f"Canonical target changed during recovery: {target_info.target}"
                ) from error
            if digest_file(target) != target_info.after_sha256:
                raise JournalRecoveryConflict(
                    f"Recovered target digest is invalid: {target_info.target}"
                )

        with self.db.transaction():
            for memory, details in final_ids.values():
                self.db.upsert_memory(memory, details)
            for memory_id in operation.affected_memory_ids:
                projected = final_ids.get(memory_id)
                if projected is not None:
                    for memory_operation in projected[0].operations:
                        self.db.upsert_operation(
                            projected[0].project,
                            memory_id,
                            memory_operation,
                        )
                else:
                    existing = self.db.get_memory(memory_id)
                    if existing is not None:
                        delete_operation = MemoryOperation(
                            operation_id=operation.operation_id,
                            source=operation.actor,
                            action="deleted",
                            request_fingerprint="mutation:" + operation.operation_id,
                            timestamp=operation.created_at,
                        )
                        self.db.upsert_operation(
                            operation.project_keys[0],
                            memory_id,
                            delete_operation,
                        )
                    self.db.delete_memory(memory_id)
                self.db.invalidate_vector(memory_id)
                projected_after = final_ids.get(memory_id)
                if (
                    projected_after is not None
                    and projected_after[0].status == "active"
                    and projected_after[0].content_fingerprint is not None
                ):
                    self.db.queue_vector_repair(
                        memory_id,
                        projected_after[0].content_fingerprint,
                        operation.created_at,
                    )
                else:
                    self.db.clear_vector_repair(memory_id)

        for target_info, _target, temporary, _needs_replace, _document in preflight:
            if temporary.exists() and digest_file(temporary) == target_info.after_sha256:
                temporary.unlink()
                fsync_directory(temporary.parent)
        journal_path.unlink()
        fsync_directory(journal_path.parent)

        embeddings: list[tuple[str, str, bytes]] = []
        for memory_id in operation.affected_memory_ids:
            projected = final_ids.get(memory_id)
            if projected is None:
                continue
            memory = projected[0]
            if memory.status == "active" and memory.content_fingerprint is not None:
                embeddings.append(
                    (
                        memory.id,
                        memory.content_fingerprint,
                        self._embedding_bytes(memory),
                    )
                )
        return embeddings

    def _resolve_scope(self, project: str) -> _PersistenceScope:
        project = validate_storage_key(project)
        registered = ProjectRegistry(self.memory_home).resolve(project)
        if registered is None:
            return _PersistenceScope(project, (project,))
        return _PersistenceScope(
            registered.identity.key,
            registered.storage_keys,
        )

    @staticmethod
    def _canonical_uuid(
        value: object,
        label: str,
        error_type: type[Exception] = ValueError,
    ) -> str:
        if not isinstance(value, str):
            raise error_type(f"{label} must be a UUID")
        try:
            return str(uuid.UUID(value))
        except (ValueError, AttributeError) as error:
            raise error_type(f"{label} must be a UUID") from error

    @staticmethod
    def _contained_path(base: Path, candidate: Path, label: str) -> Path:
        resolved_base = base.resolve()
        resolved_candidate = candidate.resolve(strict=False)
        try:
            resolved_candidate.relative_to(resolved_base)
        except ValueError as error:
            raise ProjectResolutionError(
                f"Resolved {label} is outside its containment root"
            ) from error
        return resolved_candidate

    def _exact_child_path(
        self,
        root: Path,
        candidate: Path,
        child_name: str,
        label: str,
    ) -> Path:
        resolved_root = self._contained_path(
            self.memory_home,
            root,
            f"{label} root",
        )
        expected = resolved_root / child_name
        resolved_candidate = candidate.resolve(strict=False)
        if resolved_candidate != expected:
            raise ProjectResolutionError(
                f"Resolved {label} does not preserve its exact storage identity "
                "containment"
            )
        return resolved_candidate

    def _lock_path(self, canonical_key: str) -> Path:
        canonical_key = validate_storage_key(canonical_key)
        filename = f"{canonical_key}.lock"
        locks_root = self.memory_home / "locks"
        return self._exact_child_path(
            locks_root,
            locks_root / filename,
            filename,
            "project lock",
        )

    def _project_dir(self, storage_key: str) -> Path:
        storage_key = validate_storage_key(storage_key)
        return self._exact_child_path(
            self.vault_dir,
            self.vault_dir / storage_key,
            storage_key,
            "vault project directory",
        )

    def _read_project_documents(
        self,
        project_dirs: Sequence[tuple[str, Path]],
    ) -> dict[Path, SessionDocument]:
        documents: dict[Path, SessionDocument] = {}
        for _storage_key, project_dir in project_dirs:
            if not project_dir.exists():
                continue
            if not project_dir.is_dir():
                raise ProjectResolutionError(
                    "Resolved vault project path is not a directory"
                )
            for candidate in sorted(project_dir.glob("*-session.md")):
                path = self._contained_path(
                    project_dir,
                    candidate,
                    "canonical Markdown file",
                )
                documents[path] = parse_session_file(path)
        return documents

    def _find_canonical_operation(
        self,
        documents: dict[Path, SessionDocument],
        operation_id: str,
    ) -> tuple[_CanonicalEntry, MemoryOperation] | None:
        matches: list[tuple[_CanonicalEntry, MemoryOperation]] = []
        for path, document in documents.items():
            if document.schema_version != 2:
                continue
            for entry in document.entries:
                memory = entry.to_memory(str(path))
                for operation in memory.operations:
                    try:
                        stored_operation_id = self._canonical_uuid(
                            operation.operation_id,
                            "Canonical operation ID",
                        )
                    except ValueError:
                        stored_operation_id = operation.operation_id
                    if stored_operation_id == operation_id:
                        matches.append(
                            (_CanonicalEntry(path, document, entry), operation)
                        )
        if len(matches) > 1:
            raise CanonicalDriftError(
                f"Operation {operation_id!r} is ambiguous across multiple canonical memories"
            )
        return matches[0] if matches else None

    def _find_canonical_memory(
        self,
        documents: dict[Path, SessionDocument],
        memory_id: object,
    ) -> _CanonicalEntry:
        canonical_id = self._canonical_uuid(
            memory_id,
            "Indexed memory ID",
            CanonicalDriftError,
        )
        matches: list[_CanonicalEntry] = []
        for path, document in documents.items():
            if document.schema_version != 2:
                continue
            for entry in document.entries:
                entry_id = self._canonical_uuid(
                    entry.id,
                    "Canonical memory ID",
                    CanonicalDriftError,
                )
                if entry_id == canonical_id:
                    matches.append(_CanonicalEntry(path, document, entry))
        if len(matches) > 1:
            raise CanonicalDriftError(
                f"Memory {canonical_id!r} is ambiguous across multiple canonical files"
            )
        if not matches:
            raise CanonicalDriftError(
                f"Memory {canonical_id!r} is absent from canonical Markdown"
            )
        return matches[0]

    @staticmethod
    def _find_legacy_duplicate(
        documents: dict[Path, SessionDocument],
        duplicate: dict,
    ) -> _CanonicalEntry | None:
        indexed_path = duplicate.get("file_path")
        indexed_title = duplicate.get("title")
        indexed_anchor = duplicate.get("section_anchor")
        if (
            not isinstance(indexed_path, str)
            or not indexed_path
            or not isinstance(indexed_title, str)
            or not isinstance(indexed_anchor, str)
            or not indexed_anchor
        ):
            return None
        resolved_path = Path(indexed_path).resolve(strict=False)
        document = documents.get(resolved_path)
        if document is None or document.schema_version != 1:
            return None
        matches = [
            entry
            for entry in document.entries
            if entry.section_anchor == indexed_anchor
            and entry.title.strip().casefold() == indexed_title.strip().casefold()
        ]
        if len(matches) != 1:
            return None
        return _CanonicalEntry(resolved_path, document, matches[0])

    def _prepare_change(
        self,
        request: SaveRequest,
        fingerprint: str,
        documents: dict[Path, SessionDocument],
        storage_keys: Sequence[str],
    ) -> tuple[Memory, Optional[str], SessionDocument, Path, str]:
        query = f"{request.raw.title} {request.raw.what}"
        candidates: list[dict] = []
        for storage_key in storage_keys:
            try:
                candidates.extend(
                    self.db.fts_search(query, limit=5, project=storage_key)
                )
            except Exception:
                continue
        candidates.sort(
            key=lambda item: (
                float(item.get("score", 0.0))
                if isinstance(item, dict)
                and isinstance(item.get("score"), (int, float))
                and not isinstance(item.get("score"), bool)
                else float("-inf")
            ),
            reverse=True,
        )

        same_project_count = sum(
            1 for row in candidates if row.get("project") == request.project
        )
        normalization_pool = candidates
        if same_project_count == 1:
            try:
                normalization_pool = self.db.fts_search(query, limit=5) or candidates
            except Exception:
                normalization_pool = candidates

        duplicate = is_duplicate(
            request.raw,
            request.project,
            candidates,
            normalization_pool,
        )
        context = MergeContext(
            operation_id=request.operation_id,
            source=request.source,
            timestamp=request.timestamp,
            request_fingerprint=fingerprint,
            branch=request.raw.branch,
            commit_sha=request.raw.commit_sha,
        )
        if duplicate is not None:
            legacy = self._find_legacy_duplicate(documents, duplicate)
            if legacy is not None:
                self._require_v2(legacy.document)
            canonical = self._find_canonical_memory(documents, duplicate.get("id"))
            target = canonical.path
            document = canonical.document
            self._require_v2(document)
            entry = canonical.entry
            existing = entry.to_memory(str(target))
            existing.id = self._canonical_uuid(
                existing.id,
                "Canonical memory ID",
                CanonicalDriftError,
            )
            if existing.project != request.project:
                raise CanonicalDriftError(
                    f"Duplicate memory {existing.id} belongs to read-only alias storage"
                )
            merged, details = merge_duplicate(
                existing,
                entry.details,
                request.raw,
                context,
            )
            return merged, details, document, target, "updated"

        target = self._target_path(request)
        document = documents.get(target)
        if document is None and target.exists():
            document = parse_session_file(target)
        if document is None:
            document = SessionDocument(
                project=request.project,
                created=request.timestamp,
                tags=[],
                sources=[],
                title=f"{target.name[:10]} Session",
                entries=[],
                schema_version=2,
            )
        else:
            self._require_v2(document)

        memory = Memory.from_raw(request.raw, project=request.project, file_path=str(target))
        memory.created_at = request.timestamp
        memory.updated_at = request.timestamp
        memory.operations.append(
            MemoryOperation(
                operation_id=request.operation_id,
                source=request.source,
                action="created",
                request_fingerprint=fingerprint,
                timestamp=request.timestamp,
                branch=request.raw.branch,
                commit_sha=request.raw.commit_sha,
            )
        )
        return memory, request.raw.details, document, target, "created"

    def _target_path(self, request: SaveRequest) -> Path:
        timestamp = request.timestamp
        if timestamp.endswith("Z"):
            timestamp = timestamp[:-1] + "+00:00"
        try:
            day = datetime.fromisoformat(timestamp).date().isoformat()
        except ValueError as error:
            raise ValueError("Save request timestamp must be ISO 8601") from error
        project_dir = self._project_dir(request.project)
        return self._contained_path(
            project_dir,
            project_dir / f"{day}-session.md",
            "canonical Markdown file",
        )

    def _require_v2(self, document: SessionDocument) -> None:
        if document.schema_version == 1:
            error = LegacySchemaWriteError()
            raise LegacyMetadataRequiredError(str(error)) from error

    def _render_document(self, document: SessionDocument) -> str:
        tags: set[str] = set()
        sources: set[str] = set()
        for entry in document.entries:
            entry_tags = entry.metadata.get("tags", []) if entry.metadata_complete else []
            tags.update(item for item in entry_tags if isinstance(item, str))
            if entry.source:
                sources.add(entry.source)
        return render_session_document(
            document,
            tags=sorted(tags),
            sources=sorted(sources),
        )

    def _embedding_bytes(self, memory: Memory) -> bytes:
        return embedding_text(
            title=memory.title,
            what=memory.what,
            why=memory.why,
            impact=memory.impact,
            tags=memory.tags,
        ).encode("utf-8")

    @staticmethod
    def _fingerprint_bytes(payload: bytes) -> str:
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def _validate_content_fingerprint(self, memory: Memory, payload: bytes) -> None:
        expected = self._fingerprint_bytes(payload)
        if memory.content_fingerprint != expected:
            raise CanonicalDriftError(
                f"Canonical memory {memory.id} has an invalid content fingerprint"
            )

    def _write_vector(
        self,
        memory_id: str,
        fingerprint: str | None,
        embedding_bytes: bytes,
    ) -> tuple[str, str | None]:
        if self.embed is None or fingerprint is None:
            return "degraded", _VECTOR_WARNING
        try:
            embedding = self.embed(embedding_bytes.decode("utf-8"))
            written = self.db.upsert_vector_if_current(
                memory_id,
                fingerprint,
                embedding,
            )
        except DimensionMismatchError:
            return "degraded", _VECTOR_WARNING
        except Exception:
            return "degraded", _VECTOR_WARNING
        return ("ready", None) if written else ("superseded", None)
