"""Core MemoryService orchestrator for the memory system.

This module provides the main MemoryService class that wires together:
- Configuration loading
- Database operations
- Secret redaction
- Markdown file writing
- Embedding generation
- Hybrid search

All CLI commands use this service as the main entry point.
"""

import json
import os
import re
import uuid
from difflib import SequenceMatcher
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from memory.config import get_memory_home, load_config, resolve_context_mode
from memory.db import DimensionMismatchError, MemoryDB
from memory.embeddings.base import EmbeddingProvider
from memory.markdown import (
    SessionDocument,
    SessionEntry,
    assign_entry_anchors,
    make_section_anchor,
    parse_session_file,
    write_session_document,
    write_session_memory,
)
from memory.models import Memory, MemoryDetail, RawMemoryInput
from memory.persistence import (
    UNSET,
    CanonicalPersistence,
    MemoryPatch,
    SaveRequest,
    _Unset,
)
from memory.redaction import load_memoryignore, redact
from memory.search import hybrid_search, tiered_search


class MemoryService:
    """Main orchestrator for memory operations.

    Manages configuration, database, embeddings, redaction, and file writing.
    All operations are coordinated through this service.
    """

    def __init__(
        self,
        memory_home: Optional[str] = None,
        *,
        recover_pending: bool = True,
        read_only: bool = False,
    ):
        """Initialize the memory service.

        Args:
            memory_home: Optional path to memory home directory.
                        If not provided, uses MEMORY_HOME env var or ~/.memory
        """
        self.memory_home = memory_home or get_memory_home()
        self.vault_dir = os.path.join(self.memory_home, "vault")
        self.db_path = os.path.join(self.memory_home, "index.db")
        self.config_path = os.path.join(self.memory_home, "config.yaml")
        self.ignore_path = os.path.join(self.memory_home, ".memoryignore")

        # Read-only inspection must not initialize missing storage.
        if not read_only:
            os.makedirs(self.vault_dir, exist_ok=True)

        # Load configuration and initialize database
        self.config = load_config(self.config_path)
        self.db = MemoryDB(self.db_path, read_only=read_only)

        # Lazy-load embedding provider (expensive operation)
        self._embedding_provider: Optional[EmbeddingProvider] = None
        self._ignore_patterns: Optional[list[str]] = None
        self._vectors_available: Optional[bool] = None
        self.persistence = CanonicalPersistence(
            Path(self.memory_home),
            self.db,
            self.ignore_patterns,
        )
        self.persistence.embed = lambda text: self.embedding_provider.embed(text)
        if recover_pending and not read_only:
            self.persistence.startup_recoveries = tuple(
                self.persistence.recover_pending_operations(())
            )
            self.persistence.startup_vector_repairs = (
                self.persistence.repair_pending_vectors()
            )

    @property
    def embedding_provider(self) -> EmbeddingProvider:
        """Get the embedding provider, lazily initializing if needed.

        Returns:
            Configured embedding provider instance
        """
        if self._embedding_provider is None:
            self._embedding_provider = self._create_embedding_provider()
        return self._embedding_provider

    @property
    def ignore_patterns(self) -> list[str]:
        """Get redaction patterns, lazily loading from .memoryignore if needed.

        Returns:
            List of regex patterns for redaction
        """
        if self._ignore_patterns is None:
            self._ignore_patterns = load_memoryignore(self.ignore_path)
        return self._ignore_patterns

    @property
    def vectors_available(self) -> bool:
        """Check if vector operations are available.

        Returns True if the vec table exists and dimensions match.
        Caches the result after first check.
        """
        if self._vectors_available is None:
            self._vectors_available = self.db.has_vec_table()
        return self._vectors_available

    def _create_embedding_provider(self) -> EmbeddingProvider:
        """Create an embedding provider based on configuration.

        Returns:
            Configured embedding provider instance

        Raises:
            ValueError: If embedding provider is not supported
        """
        provider = self.config.embedding.provider
        if provider == "ollama":
            from memory.embeddings.ollama import OllamaEmbedding
            return OllamaEmbedding(
                model=self.config.embedding.model,
                base_url=self.config.embedding.base_url or "http://localhost:11434",
            )
        elif provider == "openai":
            from memory.embeddings.openai_embed import OpenAIEmbedding
            return OpenAIEmbedding(
                model=self.config.embedding.model,
                api_key=self.config.embedding.api_key,
                base_url=self.config.embedding.base_url,
            )
        raise ValueError(f"Unknown embedding provider: {provider}")

    def _merge_tags(self, existing: list[str], extra: list[str]) -> list[str]:
        combined = existing[:]
        existing_norm = {t.lower() for t in existing}
        for tag in extra:
            if tag.lower() in existing_norm:
                continue
            combined.append(tag)
            existing_norm.add(tag.lower())
        return combined

    def _ensure_vectors(self, embedding: list[float]) -> bool:
        """Ensure the vector table is set up for the given embedding dimension.

        Args:
            embedding: An embedding vector to detect dimension from

        Returns:
            True if vectors are ready, False if dimension mismatch
        """
        dim = len(embedding)
        try:
            self.db.ensure_vec_table(dim)
            self._vectors_available = True
            return True
        except DimensionMismatchError:
            self._vectors_available = False
            return False

    def _details_warnings(self, raw: RawMemoryInput) -> list[str]:
        """Return quality warnings for memory details."""
        warnings: list[str] = []

        details = (raw.details or "").strip()
        category = (raw.category or "").strip().lower()

        if category in {"decision", "bug"} and not details:
            warnings.append(
                f"'{category}' memories should include details. "
                "Capture context, options considered, decision, tradeoffs, and follow-up."
            )
            return warnings

        if not details:
            return warnings

        min_chars = 120
        if len(details) < min_chars:
            warnings.append(
                f"Details are brief ({len(details)} chars). "
                f"Aim for at least {min_chars} chars for future-session context."
            )

        required_sections = [
            "context",
            "options considered",
            "decision",
            "tradeoffs",
            "follow-up",
        ]
        details_lc = details.lower()
        missing = [section for section in required_sections if section not in details_lc]
        if missing:
            warnings.append(
                "Details are missing recommended sections: "
                + ", ".join(missing)
                + "."
            )

        return warnings

    def save(
        self,
        raw: RawMemoryInput,
        project: Optional[str] = None,
        *,
        authoritative_source: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, object]:
        """Save a memory with full pipeline: redact, write markdown, index, embed.

        Args:
            raw: Raw memory input to process and save
            project: Optional project name. If not provided, uses current directory name

        Returns:
            Dictionary with 'id' (memory UUID) and 'file_path' (markdown file path)
        """
        project = os.path.basename(os.getcwd()) if project is None else project
        warnings = self._details_warnings(raw)
        request = SaveRequest(
            raw=raw,
            project=project,
            source=(authoritative_source if authoritative_source is not None else raw.source),
            operation_id=idempotency_key or str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        result = self.persistence.save(request)
        vector_warning = result.get("warning")
        if isinstance(vector_warning, str) and vector_warning not in warnings:
            warnings.append(vector_warning)
        return {**result, "warnings": warnings}

    def search(
        self,
        query: str,
        limit: int = 5,
        project: Optional[str] = None,
        source: Optional[str] = None,
        use_vectors: bool = True,
        include_archived: bool = False,
        record_feedback: bool = True,
    ) -> list[dict]:
        """Search memories using hybrid FTS + vector search.

        Falls back to FTS-only if vectors are unavailable.

        Args:
            query: Search query string
            limit: Maximum number of results to return (default: 5)
            project: Optional project filter
            source: Optional source filter

        Returns:
            List of search results with scores and metadata
        """
        # FTS-only path when semantic search is disabled
        if not use_vectors:
            results = hybrid_search(
                self.db,
                None,
                query,
                limit=limit,
                project=project,
                source=source,
                include_archived=include_archived,
                min_relevance=self.config.context.min_relevance,
            )
            if record_feedback:
                self.db.record_feedback([r["id"] for r in results])
            return results

        # Use tiered search: FTS first, embed only if sparse results
        if self.vectors_available:
            try:
                results = tiered_search(
                    self.db,
                    self.embedding_provider,
                    query,
                    limit=limit,
                    project=project,
                    source=source,
                    include_archived=include_archived,
                    min_relevance=self.config.context.min_relevance,
                    min_vector_similarity=self.config.context.min_vector_similarity,
                )
                if record_feedback:
                    self.db.record_feedback([r["id"] for r in results])
                return results
            except DimensionMismatchError:
                self._vectors_available = False
            except Exception:
                pass

        # Fallback: FTS-only search
        results = tiered_search(
            self.db,
            None,
            query,
            limit=limit,
            project=project,
            source=source,
            include_archived=include_archived,
            min_relevance=self.config.context.min_relevance,
        )
        if record_feedback:
            self.db.record_feedback([r["id"] for r in results])
        return results

    def _ollama_warm(self) -> bool:
        base_url = self.config.embedding.base_url or "http://localhost:11434"
        try:
            from memory.embeddings.ollama import is_model_loaded
        except Exception:
            return False
        return is_model_loaded(self.config.embedding.model, base_url)

    def _should_use_semantic(self, semantic_mode: str) -> bool:
        if semantic_mode == "never":
            return False
        if semantic_mode == "always":
            return True
        provider = self.config.embedding.provider
        if provider == "ollama":
            return self._ollama_warm()
        return True

    def get_context(
        self,
        limit: int = 10,
        project: Optional[str] = None,
        source: Optional[str] = None,
        query: Optional[str] = None,
        semantic_mode: Optional[str] = None,
        topup_recent: Optional[bool] = None,
        agent: Optional[str] = None,
        token_budget: Optional[int] = None,
    ) -> tuple[list[dict], int]:
        """Get memory pointers for context injection.

        Args:
            limit: Maximum number of pointers to return
            project: Optional project filter
            source: Optional source filter
            query: Optional search query for semantic filtering

        Returns:
            Tuple of (list of memory pointer dicts, total count)
        """
        mode, _mode_source = resolve_context_mode(self.config, agent)
        if mode == "off":
            return [], self.db.count_memories(project=project, source=source)
        total = self.db.count_memories(project=project, source=source)

        if semantic_mode is None:
            semantic_mode = self.config.context.semantic
        if isinstance(semantic_mode, bool):
            semantic_mode = "always" if semantic_mode else "never"
        if semantic_mode not in {"auto", "always", "never"}:
            semantic_mode = "auto"

        if topup_recent is None:
            topup_recent = self.config.context.topup_recent

        results: list[dict]
        if query:
            use_vectors = self._should_use_semantic(semantic_mode)
            results = self.search(
                query,
                limit=limit,
                project=project,
                source=source,
                use_vectors=use_vectors,
                include_archived=False,
            )
            if topup_recent and len(results) < limit:
                # Fill unused space with operationally useful living memory before
                # generic recency. Query relevance remains the primary signal.
                candidates = self.db.list_memories(
                    limit=max(limit * 4, 20), project=project, include_archived=False
                )
                if source:
                    candidates = [r for r in candidates if r.get("source") == source]
                category_priority = {
                    "constraint": 0, "project_state": 1, "active_work": 2,
                    "known_fix": 3, "playbook": 4, "decision": 5,
                    "bug": 6, "pattern": 7,
                }
                candidates.sort(key=lambda r: (
                    category_priority.get(r.get("category"), 20),
                    -(r.get("retrieved_count") or 0),
                ))
                seen = {r["id"] for r in results}
                for r in candidates:
                    if r["id"] in seen:
                        continue
                    results.append(r)
                    if len(results) >= limit:
                        break
        else:
            results = self.db.list_memories(
                limit=max(limit * 4, 20), project=project, include_archived=False
            )
            if source:
                results = [r for r in results if r.get("source") == source]
            priority = {"project_state": 0, "constraint": 1, "active_work": 2, "playbook": 3, "known_fix": 4}
            results.sort(key=lambda r: (
                priority.get(r.get("category"), 20),
                -(r.get("retrieved_count") or 0),
            ))

        budget = token_budget or self.config.context.token_budget
        packed: list[dict] = []
        used = 0
        for result in results:
            text = " ".join(str(result.get(k, "") or "") for k in ("title", "what", "why", "impact"))
            estimated = max(12, (len(text) + 3) // 4)
            if packed and used + estimated > budget:
                continue
            result = dict(result)
            result["estimated_tokens"] = estimated
            packed.append(result)
            used += estimated
            if len(packed) >= limit:
                break
        self.db.record_feedback([r["id"] for r in packed])
        return packed, total

    def context_policy(self, agent: Optional[str] = None) -> dict[str, object]:
        mode, source = resolve_context_mode(self.config, agent)
        return {"mode": mode, "source": source, "enabled": mode != "off", "agent": agent}

    def list_memories(
        self,
        *,
        query: Optional[str] = None,
        project: Optional[str] = None,
        category: Optional[str] = None,
        include_archived: bool = False,
        limit: int = 200,
        use_vectors: bool = False,
    ) -> list[dict]:
        """List memories for dashboard and admin flows."""
        if query:
            results = self.search(
                query,
                limit=limit,
                project=project,
                use_vectors=use_vectors,
                include_archived=include_archived,
            )
            if category:
                results = [result for result in results if result.get("category") == category]
            return results
        return self.db.list_memories(
            limit=limit,
            project=project,
            category=category,
            include_archived=include_archived,
        )

    def get_memory_record(self, memory_id: str) -> Optional[dict]:
        """Return a memory with parsed details for dashboard editing."""
        record = self._get_full_memory(memory_id)
        if not record:
            return None
        detail = self.get_details(memory_id)
        record["details"] = detail.body if detail else ""
        return record

    def migrate_vault_metadata(
        self,
        *,
        project: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict[str, object]:
        """Explicitly enrich legacy vault files with per-memory metadata."""
        from memory.reconcile import migrate_vault_metadata

        return migrate_vault_metadata(self, project=project, dry_run=dry_run)

    def get_dashboard_stats(
        self,
        project: Optional[str] = None,
        *,
        include_duplicate_candidates: bool = False,
    ) -> dict[str, object]:
        """Return aggregate dashboard statistics."""
        cursor = self.db.conn.cursor()
        params: list[object] = []
        project_clause = ""
        if project:
            project_clause = "WHERE project = ?"
            params.append(project)

        cursor.execute(
            f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status IS NULL OR status = 'active' THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN status = 'archived' THEN 1 ELSE 0 END) AS archived
            FROM memories
            {project_clause}
            """,
            params,
        )
        totals = dict(cursor.fetchone())

        cursor.execute(
            f"""
            SELECT project, COUNT(*) AS count
            FROM memories
            {project_clause}
            GROUP BY project
            ORDER BY count DESC, project ASC
            """,
            params,
        )
        by_project = [dict(row) for row in cursor.fetchall()]

        category_clause = "WHERE (status IS NULL OR status = 'active')"
        category_params: list[object] = []
        if project:
            category_clause += " AND project = ?"
            category_params.append(project)
        cursor.execute(
            f"""
            SELECT category, COUNT(*) AS count
            FROM memories
            {category_clause}
            GROUP BY category
            ORDER BY count DESC, category ASC
            """,
            category_params,
        )
        by_category = [dict(row) for row in cursor.fetchall()]

        duplicate_count = 0
        if include_duplicate_candidates:
            duplicate_count = len(self.find_duplicate_candidates(project=project, limit=50))

        return {
            "totals": totals,
            "projects": by_project,
            "categories": by_category,
            "duplicate_candidates": duplicate_count,
            "recent": self.db.list_recent(limit=10, project=project, include_archived=True),
        }

    def update_memory_record(
        self,
        memory_id: str,
        *,
        patch: Optional[MemoryPatch] = None,
        actor: Optional[str] = None,
        title: str | _Unset = UNSET,
        what: str | _Unset = UNSET,
        why: Optional[str] | _Unset = UNSET,
        impact: Optional[str] | _Unset = UNSET,
        category: Optional[str] | _Unset = UNSET,
        tags: list[str] | _Unset = UNSET,
        source: Optional[str] = None,
        details: Optional[str] | _Unset = UNSET,
    ) -> dict[str, object]:
        """Update a memory through the canonical mutation coordinator."""
        if patch is None:
            patch = MemoryPatch(
                title=title,
                what=what,
                why=why,
                impact=impact,
                category=category,
                tags=tags,
                details=details,
            )
        return self.persistence.update(
            memory_id,
            patch,
            actor=actor or source or "dashboard",
        )

    def archive_memory(
        self,
        memory_id: str,
        *,
        reason: str = "archived",
        superseded_by: Optional[str] = None,
        actor: str = "dashboard",
    ) -> dict[str, object]:
        return self.persistence.archive(
            memory_id,
            reason=reason,
            superseded_by=superseded_by,
            actor=actor,
        )

    def restore_memory(
        self,
        memory_id: str,
        *,
        actor: str = "dashboard",
    ) -> dict[str, object]:
        """Restore an archived memory."""
        return self.persistence.restore(memory_id, actor=actor)

    def merge_memories(
        self,
        canonical_id: str,
        source_ids: list[str],
        *,
        actor: str = "dashboard",
        operation_id: str | None = None,
    ) -> dict[str, object]:
        """Merge source memories into a canonical memory and archive the sources."""
        return self.persistence.merge(
            canonical_id,
            source_ids,
            actor=actor,
            operation_id=operation_id,
        )

    def find_duplicate_candidates(
        self,
        *,
        project: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Find likely duplicate memories for dashboard review."""
        memories = self.db.list_memories(limit=500, project=project, include_archived=False)
        candidates: list[dict] = []
        for index, left in enumerate(memories):
            for right in memories[index + 1:]:
                if left["project"] != right["project"]:
                    continue
                title_ratio = SequenceMatcher(
                    None,
                    self._normalize_duplicate_text(left["title"]),
                    self._normalize_duplicate_text(right["title"]),
                ).ratio()
                what_ratio = SequenceMatcher(
                    None,
                    self._normalize_duplicate_text(left["what"]),
                    self._normalize_duplicate_text(right["what"]),
                ).ratio()
                if title_ratio < 0.72 and not (
                    self._normalize_duplicate_text(left["title"])
                    == self._normalize_duplicate_text(right["title"])
                ):
                    continue
                score = round(max(title_ratio, (title_ratio + what_ratio) / 2), 3)
                if score < 0.75:
                    continue
                candidates.append(
                    {
                        "left_id": left["id"],
                        "left_title": left["title"],
                        "right_id": right["id"],
                        "right_title": right["title"],
                        "project": left["project"],
                        "score": score,
                    }
                )
        candidates.sort(key=lambda item: item["score"], reverse=True)
        return candidates[:limit]

    def get_details(self, memory_id: str) -> Optional[MemoryDetail]:
        """Get full details for a memory by ID.

        Args:
            memory_id: UUID of the memory to retrieve details for

        Returns:
            MemoryDetail object if details exist, None otherwise
        """
        return self.db.get_details(memory_id)

    def delete(self, memory_id: str, *, actor: str = "cli") -> bool:
        """Delete a memory by ID or prefix.

        Args:
            memory_id: Full UUID or prefix of the memory to delete

        Returns:
            True if deleted, False if not found
        """
        return self.persistence.delete(memory_id, actor=actor)

    def resolve_memory_id(self, memory_id: str) -> str:
        """Resolve one exact memory ID or unique literal prefix."""
        return self.persistence.resolve_memory_id(memory_id)

    def _normalize_duplicate_text(self, value: str) -> str:
        return re.sub(r"\W+", " ", (value or "").lower()).strip()

    def _get_full_memory(self, memory_id: str) -> Optional[dict]:
        cursor = self.db.conn.cursor()
        cursor.execute(
            """
            SELECT m.*,
                   EXISTS(SELECT 1 FROM memory_details WHERE memory_id = m.id) as has_details
            FROM memories m
            WHERE m.id LIKE ?
            ORDER BY m.id
            LIMIT 1
            """,
            (memory_id + "%",),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def _load_document_entry(self, record: dict) -> tuple[SessionDocument, SessionEntry]:
        document = parse_session_file(record["file_path"])
        file_rows = self.db.list_memories(
            limit=500,
            file_path=record["file_path"],
            include_archived=True,
        )
        rows_by_anchor = {row["section_anchor"]: row for row in file_rows}
        rows_by_id = {row["id"]: row for row in file_rows}

        for entry in document.entries:
            if entry.id and entry.id in rows_by_id:
                continue
            row = rows_by_anchor.get(entry.section_anchor)
            if row:
                entry.id = row["id"]
                if row.get("status"):
                    entry.status = row["status"]
                if row.get("archived_at"):
                    entry.archived_at = row["archived_at"]
                if row.get("archive_reason"):
                    entry.archive_reason = row["archive_reason"]
                if row.get("superseded_by"):
                    entry.superseded_by = row["superseded_by"]

        for entry in document.entries:
            if entry.id == record["id"]:
                return document, entry

        for entry in document.entries:
            if entry.section_anchor == record["section_anchor"]:
                entry.id = record["id"]
                return document, entry

        raise ValueError(f"Unable to locate markdown entry for memory {record['id']}")

    def _persist_document(
        self,
        file_path: str,
        document: SessionDocument,
        *,
        tag_overrides: Optional[dict[str, list[str]]] = None,
        source_overrides: Optional[dict[str, Optional[str]]] = None,
    ) -> None:
        assign_entry_anchors(document.entries)
        file_rows = self.db.list_memories(limit=500, file_path=file_path, include_archived=True)
        tags: set[str] = set()
        sources: set[str] = set()
        rows_by_id = {row["id"]: row for row in file_rows}
        for entry in document.entries:
            if entry.id and tag_overrides and entry.id in tag_overrides:
                tags.update(tag_overrides[entry.id])
            elif entry.id and entry.id in rows_by_id:
                row = rows_by_id[entry.id]
                row_tags = row["tags"]
                if isinstance(row_tags, str):
                    try:
                        tags.update(json.loads(row_tags))
                    except (json.JSONDecodeError, TypeError):
                        pass
                elif isinstance(row_tags, list):
                    tags.update(row_tags)
            source = entry.source
            if entry.id and source_overrides and entry.id in source_overrides:
                source = source_overrides[entry.id]
            if source:
                sources.add(source)
        write_session_document(file_path, document, tags=sorted(tags), sources=sorted(sources))
        self._sync_document_rows(document)

    def _replace_details(self, memory_id: str, details: Optional[str]) -> None:
        cursor = self.db.conn.cursor()
        cursor.execute("DELETE FROM memory_details WHERE memory_id = ?", (memory_id,))
        if details:
            cursor.execute(
                "INSERT INTO memory_details (memory_id, body) VALUES (?, ?)",
                (memory_id, details),
            )

    def _sync_document_rows(self, document: SessionDocument) -> None:
        cursor = self.db.conn.cursor()
        for entry in document.entries:
            if not entry.id:
                continue
            cursor.execute(
                """
                UPDATE memories
                SET section_anchor = ?, category = ?, source = ?, status = ?, archived_at = ?, archive_reason = ?, superseded_by = ?
                WHERE id = ?
                """,
                (
                    entry.section_anchor,
                    entry.category,
                    entry.source,
                    entry.status,
                    entry.archived_at,
                    entry.archive_reason,
                    entry.superseded_by,
                    entry.id,
                ),
            )

    def reindex(self, progress_callback=None) -> dict:
        """Rebuild the vector table with current embedding provider.

        Args:
            progress_callback: Optional callable(current, total) for progress reporting

        Returns:
            Dict with 'count' (memories reindexed), 'dim' (new dimension),
            'model' (embedding model name)
        """
        # Detect dimension from provider
        probe = self.embedding_provider.embed("dimension probe")
        dim = len(probe)

        # Drop and recreate vec table
        self.db.drop_vec_table()
        self.db.set_embedding_dim(dim)
        self.db._create_vec_table(dim)

        # Re-embed all memories
        memories = self.db.list_all_for_reindex()
        total = len(memories)

        for i, mem in enumerate(memories):
            tags = ""
            if mem["tags"]:
                try:
                    tags = " ".join(json.loads(mem["tags"]))
                except (json.JSONDecodeError, TypeError):
                    tags = str(mem["tags"])

            embed_text = (
                f"{mem['title']} {mem['what']} "
                f"{mem['why'] or ''} {mem['impact'] or ''} {tags}"
            )
            embedding = self.embedding_provider.embed(embed_text)
            self.db.insert_vector(mem["rowid"], embedding)

            if progress_callback:
                progress_callback(i + 1, total)

        self._vectors_available = True

        return {
            "count": total,
            "dim": dim,
            "model": self.config.embedding.model,
        }

    def import_from_vault(
        self,
        dry_run: bool = False,
        progress_callback=None,
        *,
        reconcile: bool = False,
        project: str | None = None,
    ) -> dict:
        """Scan vault/ markdown files and import memories missing from SQLite.

        This bridges the gap for multi-agent setups where new ``.md``
        files arrive via file-sync (e.g. Syncthing) but are not yet in
        the local ``index.db``.

        Deduplication key: ``(project, file_path, section_anchor)``.

        Args:
            dry_run: If True, only report what *would* be imported.
            progress_callback: Optional ``callable(imported, skipped, project, title)``
                called for every memory encountered.

        Returns:
            Dict with ``imported`` (int), ``skipped`` (int), ``projects``
            (list of project names that had new imports).
        """
        if reconcile:
            if dry_run:
                raise ValueError("Reconciliation cannot be combined with dry-run")
            from memory.reconcile import reconcile_vault

            return reconcile_vault(self, project=project)
        if project is not None:
            raise ValueError("Project selection requires reconciliation")

        if not os.path.isdir(self.vault_dir):
            return {"imported": 0, "skipped": 0, "projects": []}

        # Build set of existing section identities.
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT project, file_path, section_anchor, title FROM memories")
        existing: set[tuple[str, str, str]] = {
            (
                row[0],
                row[1],
                row[2] or make_section_anchor(row[3]),
            )
            for row in cursor.fetchall()
        }

        imported = 0
        skipped = 0
        touched_projects: set[str] = set()

        for project_dir in sorted(Path(self.vault_dir).iterdir()):
            if not project_dir.is_dir() or project_dir.name.startswith("."):
                continue

            project = project_dir.name

            for md_file in sorted(project_dir.glob("*.md")):
                document = parse_session_file(md_file)
                if document.schema_version == 2:
                    active_entries = [
                        entry
                        for entry in document.entries
                        if entry.title and entry.what and entry.status != "archived"
                    ]
                    missing_ids = [
                        entry.id
                        for entry in active_entries
                        if not isinstance(entry.id, str)
                        or self.db.get_memory(entry.id) is None
                    ]
                    if missing_ids:
                        raise ValueError(
                            "Schema-v2 canonical Markdown requires "
                            "import_from_vault(reconcile=True) or "
                            "'memory import --reconcile'"
                        )
                    skipped += len(active_entries)
                    for entry in active_entries:
                        if progress_callback:
                            progress_callback(
                                imported,
                                skipped,
                                project,
                                entry.title,
                            )
                    continue
                parsed = [
                    {
                        "title": entry.title,
                        "what": entry.what,
                        "why": entry.why,
                        "impact": entry.impact,
                        "source": entry.source,
                        "category": entry.category,
                        "project": project,
                        "tags": list(document.tags),
                        "file_path": str(md_file),
                        "section_anchor": (
                            entry.section_anchor
                            or make_section_anchor(entry.title)
                        ),
                        "details": entry.details,
                        "living_data": dict(entry.living_data),
                    }
                    for entry in document.entries
                    if entry.title and entry.what and entry.status != "archived"
                ]

                for mem_data in parsed:
                    key = (
                        mem_data["project"],
                        mem_data["file_path"],
                        mem_data["section_anchor"],
                    )
                    if key in existing:
                        skipped += 1
                        if progress_callback:
                            progress_callback(imported, skipped, project, mem_data["title"])
                        continue

                    if not dry_run:
                        now = datetime.now(timezone.utc).isoformat()
                        mem = Memory(
                            id=str(uuid.uuid4()),
                            title=mem_data["title"],
                            what=mem_data["what"],
                            why=mem_data["why"],
                            impact=mem_data["impact"],
                            tags=mem_data["tags"],
                            category=mem_data["category"],
                            project=mem_data["project"],
                            source=mem_data["source"],
                            related_files=[],
                            file_path=mem_data["file_path"],
                            section_anchor=mem_data["section_anchor"],
                            created_at=now,
                            updated_at=now,
                            structured_data=mem_data.get("living_data", {}).get("structured_data", {}),
                            confidence=mem_data.get("living_data", {}).get("confidence"),
                            valid_from=mem_data.get("living_data", {}).get("valid_from"),
                            valid_until=mem_data.get("living_data", {}).get("valid_until"),
                            commit_sha=mem_data.get("living_data", {}).get("commit_sha"),
                            branch=mem_data.get("living_data", {}).get("branch"),
                            links=mem_data.get("living_data", {}).get("links", []),
                            last_verified=mem_data.get("living_data", {}).get("last_verified"),
                        )
                        self.db.insert_memory(mem, details=mem_data.get("details"))
                        existing.add(key)

                    imported += 1
                    touched_projects.add(project)

                    if progress_callback:
                        progress_callback(imported, skipped, project, mem_data["title"])

        return {
            "imported": imported,
            "skipped": skipped,
            "projects": sorted(touched_projects),
        }

    def doctor(self, project: str | None = None) -> dict:
        """Return read-only health and canonical-drift diagnostics."""
        from memory.health import doctor

        return doctor(self, project)

    def close(self) -> None:
        """Close database connection and clean up resources."""
        self.db.close()
