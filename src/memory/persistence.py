"""Canonical Markdown persistence with a derived SQLite projection."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Optional
import uuid

from memory.db import DimensionMismatchError, MemoryDB
from memory.markdown import (
    LegacySchemaWriteError,
    SessionDocument,
    SessionEntry,
    parse_session_file,
    render_session_document,
    upsert_session_memory_entry,
)
from memory.merge import MergeContext, is_duplicate, merge_duplicate
from memory.models import Memory, MemoryOperation, RawMemoryInput
from memory.projects import ProjectRegistry, ProjectResolutionError, validate_storage_key
from memory.redaction import redact, redact_memory_input
from memory.safe_io import ProcessFileLock, prepare_atomic_text


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
        fingerprint = request_fingerprint(redacted, scope.canonical_key, source)

        lock_path = self._contained_path(
            self.memory_home,
            self.memory_home / "locks" / f"{scope.canonical_key}.lock",
            "project lock",
        )
        with ProcessFileLock(lock_path):
            documents = self._read_project_documents(scope.storage_keys)
            canonical_operation = self._find_canonical_operation(
                documents,
                operation_id,
            )
            if canonical_operation is not None:
                canonical, operation = canonical_operation
                if operation.request_fingerprint != fingerprint:
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
                details = canonical.entry.details
                embedding_bytes = self._embedding_bytes(memory)
                self._validate_content_fingerprint(memory, embedding_bytes)
                operation = replace(operation, operation_id=operation_id)
                projection = self.db.get_memory(memory.id)
                vector_is_current = (
                    projection is not None
                    and projection.get("content_fingerprint")
                    == memory.content_fingerprint
                )
                with self.db.transaction():
                    self.db.upsert_memory(memory, details)
                    self.db.upsert_operation(scope.canonical_key, memory.id, operation)
                    if not vector_is_current:
                        self.db.invalidate_vector(memory.id)
                result = {
                    "id": memory.id,
                    "file_path": str(canonical.path),
                    "action": "replayed",
                }
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
                rendered = self._render_document(document)
                prepared = prepare_atomic_text(target, rendered)
                try:
                    self.fault("after_temp_fsync")
                    with self.db.transaction():
                        self.db.upsert_memory(memory, details)
                        self.db.upsert_operation(
                            scope.canonical_key,
                            memory.id,
                            memory.operations[-1],
                        )
                        self.db.invalidate_vector(memory.id)
                        self.fault("after_db_write")
                        prepared.replace()
                        self.fault("after_markdown_replace")
                    self.fault("after_db_commit")
                finally:
                    prepared.discard()
                result = {
                    "id": memory.id,
                    "file_path": str(target),
                    "action": action,
                }

        self.fault("before_vector_write")
        vector_status, warning = self._write_vector(
            memory.id,
            memory.content_fingerprint,
            embedding_bytes,
        )
        result["vector_status"] = vector_status
        if warning is not None:
            result["warning"] = warning
        return result

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

    def _project_dir(self, storage_key: str) -> Path:
        storage_key = validate_storage_key(storage_key)
        return self._contained_path(
            self.memory_home,
            self.vault_dir / storage_key,
            "vault project directory",
        )

    def _read_project_documents(
        self,
        storage_keys: Sequence[str],
    ) -> dict[Path, SessionDocument]:
        documents: dict[Path, SessionDocument] = {}
        for storage_key in storage_keys:
            project_dir = self._project_dir(storage_key)
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
