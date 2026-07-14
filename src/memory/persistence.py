"""Canonical Markdown persistence with a derived SQLite projection."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Optional

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
        redacted = redact_memory_input(request.raw, self.patterns)
        source = redact(request.source, self.patterns) if request.source is not None else None
        redacted.source = source
        canonical_request = replace(request, raw=redacted, source=source)
        fingerprint = request_fingerprint(redacted, request.project, source)

        lock_path = self.memory_home / "locks" / f"{request.project}.lock"
        with ProcessFileLock(lock_path):
            documents = self._read_project_documents(request.project)
            canonical_operations = self._find_canonical_operations(
                documents,
                request.operation_id,
            )
            if canonical_operations:
                if any(
                    operation.request_fingerprint != fingerprint
                    for _, operation in canonical_operations
                ):
                    raise SaveConflict(
                        f"Operation {request.operation_id!r} was already used with "
                        "a different request"
                    )
                canonical, operation = canonical_operations[0]
                memory = canonical.entry.to_memory(str(canonical.path))
                details = canonical.entry.details
                embedding_bytes = self._embedding_bytes(memory)
                self._validate_content_fingerprint(memory, embedding_bytes)
                with self.db.transaction():
                    self.db.upsert_memory(memory, details)
                    self.db.upsert_operation(request.project, memory.id, operation)
                    self.db.invalidate_vector(memory.id)
                result = {
                    "id": memory.id,
                    "file_path": str(canonical.path),
                    "action": "replayed",
                }
            else:
                ledger = self.db.get_operation(request.project, request.operation_id)
                if ledger is not None:
                    raise CanonicalDriftError(
                        f"Operation {request.operation_id!r} exists only in SQLite"
                    )
                memory, details, document, target, action = self._prepare_change(
                    canonical_request,
                    fingerprint,
                    documents,
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
                            request.project,
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
        self._write_vector(memory.id, memory.content_fingerprint, embedding_bytes)
        return result

    def _read_project_documents(self, project: str) -> dict[Path, SessionDocument]:
        project_dir = self.vault_dir / project
        if not project_dir.exists():
            return {}
        return {
            path: parse_session_file(path)
            for path in sorted(project_dir.glob("*-session.md"))
        }

    def _find_canonical_operations(
        self,
        documents: dict[Path, SessionDocument],
        operation_id: str,
    ) -> list[tuple[_CanonicalEntry, MemoryOperation]]:
        matches: list[tuple[_CanonicalEntry, MemoryOperation]] = []
        for path, document in documents.items():
            if document.schema_version != 2:
                continue
            for entry in document.entries:
                memory = entry.to_memory(str(path))
                for operation in memory.operations:
                    if operation.operation_id == operation_id:
                        matches.append(
                            (_CanonicalEntry(path, document, entry), operation)
                        )
        return matches

    def _prepare_change(
        self,
        request: SaveRequest,
        fingerprint: str,
        documents: dict[Path, SessionDocument],
    ) -> tuple[Memory, Optional[str], SessionDocument, Path, str]:
        query = f"{request.raw.title} {request.raw.what}"
        try:
            candidates = self.db.fts_search(query, limit=5, project=request.project)
        except Exception:
            candidates = []

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
            target = Path(str(duplicate.get("file_path", "")))
            if not target:
                raise CanonicalDriftError(
                    f"Duplicate memory {duplicate['id']} has no canonical file path"
                )
            document = documents.get(target)
            if document is None and target.exists():
                document = parse_session_file(target)
            if document is None:
                raise CanonicalDriftError(
                    f"Duplicate memory {duplicate['id']} is missing canonical Markdown"
                )
            self._require_v2(document)
            entry = next(
                (item for item in document.entries if item.id == duplicate["id"]),
                None,
            )
            if entry is None:
                raise CanonicalDriftError(
                    f"Duplicate memory {duplicate['id']} is absent from canonical Markdown"
                )
            existing = entry.to_memory(str(target))
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
        day = date.today().isoformat()
        return self.vault_dir / request.project / f"{day}-session.md"

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
    ) -> None:
        if self.embed is None or fingerprint is None:
            return
        try:
            embedding = self.embed(embedding_bytes.decode("utf-8"))
            self.db.upsert_vector_if_current(memory_id, fingerprint, embedding)
        except DimensionMismatchError:
            return
        except Exception:
            return
