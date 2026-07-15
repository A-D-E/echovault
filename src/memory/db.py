"""SQLite database layer with FTS5 and sqlite-vec for memory storage."""

from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import re
import struct
from typing import Callable, Iterator, Optional

# Try pysqlite3-binary first (has extension support), fall back to sqlite3
try:
    import pysqlite3.dbapi2 as sqlite3
except ImportError:
    import sqlite3

import sqlite_vec

from memory.models import Memory, MemoryDetail, MemoryOperation


_FTS_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "with",
}


def _build_fts_query(query: str) -> str:
    """Build a prefix FTS query while dropping obvious lexical noise.

    FTS is only used as a lexical signal. Filtering short/common stop-words keeps
    multi-word natural-language queries from matching nearly every memory.
    """
    raw_terms = re.findall(r"\w+", query.lower(), flags=re.UNICODE)
    filtered_terms = [
        term for term in raw_terms if len(term) > 1 and term not in _FTS_STOPWORDS
    ]

    # Fall back gracefully for short or stop-word-only queries such as "AI" or "in".
    terms = filtered_terms or raw_terms or [query.strip()]

    unique_terms: list[str] = []
    seen: set[str] = set()
    for term in terms:
        if term and term not in seen:
            unique_terms.append(term)
            seen.add(term)

    return " OR ".join(f'"{term}"*' for term in unique_terms)


class MemoryDB:
    """SQLite database for storing and searching memories."""

    def __init__(
        self,
        db_path: str,
        *,
        read_only: bool = False,
        immutable: bool = False,
    ) -> None:
        """Initialize database connection and create schema.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        if read_only:
            query = "mode=ro&immutable=1" if immutable else "mode=ro"
            database_uri = Path(db_path).resolve().as_uri() + "?" + query
            self.conn = sqlite3.connect(database_uri, uri=True)
        else:
            self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA foreign_keys = ON")
        if read_only:
            self.conn.execute("PRAGMA query_only = ON")
        else:
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._vector_cas_test_barrier: Optional[Callable[[], object]] = None

        # Enable extension loading and load sqlite-vec extension
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.conn.enable_load_extension(False)

        # Create schema (vec table is deferred until dimension is known)
        if not read_only:
            self._create_schema()

    def _create_schema(self) -> None:
        """Create database tables and indexes (excluding vec table)."""
        cursor = self.conn.cursor()

        # Main memories table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                what TEXT NOT NULL,
                why TEXT,
                impact TEXT,
                tags TEXT,
                category TEXT,
                project TEXT NOT NULL,
                source TEXT,
                related_files TEXT,
                file_path TEXT NOT NULL,
                section_anchor TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT DEFAULT 'active',
                archived_at TEXT,
                archive_reason TEXT,
                superseded_by TEXT
            )
        """)

        # Memory details table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memory_details (
                memory_id TEXT PRIMARY KEY REFERENCES memories(id),
                body TEXT NOT NULL
            )
        """)

        # Metadata table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # Project-scoped idempotency ledger for canonical save operations.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS save_operations (
                project TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                memory_id TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                action TEXT NOT NULL,
                source TEXT,
                timestamp TEXT NOT NULL,
                branch TEXT,
                commit_sha TEXT,
                PRIMARY KEY (project, operation_id)
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS save_operations_memory_id
            ON save_operations(memory_id)
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pending_vector_repairs (
                memory_id TEXT PRIMARY KEY,
                content_fingerprint TEXT NOT NULL,
                queued_at TEXT NOT NULL
            )
        """)

        # FTS5 virtual table
        cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                title, what, why, impact, tags, category, project, source,
                content='memories', content_rowid='rowid',
                tokenize='porter unicode61'
            )
        """)

        # FTS5 auto-sync trigger for INSERT
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(rowid, title, what, why, impact, tags, category, project, source)
                VALUES (new.rowid, new.title, new.what, new.why, new.impact, new.tags, new.category, new.project, new.source);
            END
        """)

        # FTS5 auto-sync trigger for UPDATE
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, title, what, why, impact, tags, category, project, source)
                VALUES ('delete', old.rowid, old.title, old.what, old.why, old.impact, old.tags, old.category, old.project, old.source);
                INSERT INTO memories_fts(rowid, title, what, why, impact, tags, category, project, source)
                VALUES (new.rowid, new.title, new.what, new.why, new.impact, new.tags, new.category, new.project, new.source);
            END
        """)

        # Migration: add updated_count column if missing
        cursor.execute("PRAGMA table_info(memories)")
        columns = {row[1] for row in cursor.fetchall()}
        if "updated_count" not in columns:
            cursor.execute("ALTER TABLE memories ADD COLUMN updated_count INTEGER DEFAULT 0")
        if "status" not in columns:
            cursor.execute("ALTER TABLE memories ADD COLUMN status TEXT DEFAULT 'active'")
        if "archived_at" not in columns:
            cursor.execute("ALTER TABLE memories ADD COLUMN archived_at TEXT")
        if "archive_reason" not in columns:
            cursor.execute("ALTER TABLE memories ADD COLUMN archive_reason TEXT")
        if "superseded_by" not in columns:
            cursor.execute("ALTER TABLE memories ADD COLUMN superseded_by TEXT")
        additive_columns = {
            "structured_data": "TEXT DEFAULT '{}'", "confidence": "REAL",
            "valid_from": "TEXT", "valid_until": "TEXT", "commit_sha": "TEXT",
            "branch": "TEXT", "links": "TEXT DEFAULT '[]'", "last_verified": "TEXT",
            "retrieved_count": "INTEGER DEFAULT 0", "details_opened_count": "INTEGER DEFAULT 0",
            "dismissed_count": "INTEGER DEFAULT 0", "last_used_at": "TEXT",
            "creator_source": "TEXT", "last_updated_by": "TEXT",
            "contributors": "TEXT DEFAULT '[]'",
            "operation_history": "TEXT DEFAULT '[]'",
            "content_fingerprint": "TEXT",
            "history_complete": "INTEGER DEFAULT 1",
        }
        for name, sql_type in additive_columns.items():
            if name not in columns:
                cursor.execute(f"ALTER TABLE memories ADD COLUMN {name} {sql_type}")

        # Create vec table if dimension is already known (e.g. reopening existing DB)
        dim = self.get_embedding_dim()
        if dim is not None:
            self._create_vec_table(dim)

        self.conn.commit()

    @contextmanager
    def transaction(self) -> Iterator["MemoryDB"]:
        """Own one immediate write transaction for a group of projection writes."""
        if self.conn.in_transaction:
            raise RuntimeError("Nested MemoryDB transactions are not supported")

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    @contextmanager
    def _write_scope(self) -> Iterator["MemoryDB"]:
        """Join an active owner transaction or create one for a public write."""
        if self.conn.in_transaction:
            yield self
            return

        with self.transaction():
            yield self

    def _create_vec_table(self, dim: int) -> None:
        """Create the vector table with the given dimension.

        Args:
            dim: Embedding vector dimension
        """
        cursor = self.conn.cursor()
        cursor.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(
                rowid INTEGER PRIMARY KEY,
                embedding float[{dim}]
            )
        """)

    def has_vec_table(self) -> bool:
        """Check if the vector table exists."""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='memories_vec'
        """)
        return cursor.fetchone() is not None

    def drop_vec_table(self) -> None:
        """Drop the vector table."""
        with self._write_scope():
            self._drop_vec_table()

    def _drop_vec_table(self) -> None:
        """Drop the vector table without committing the owner transaction."""
        cursor = self.conn.cursor()
        cursor.execute("DROP TABLE IF EXISTS memories_vec")

    def get_embedding_dim(self) -> Optional[int]:
        """Get the stored embedding dimension from meta table.

        Returns:
            The embedding dimension, or None if not set
        """
        val = self.get_meta("embedding_dim")
        return int(val) if val is not None else None

    def set_embedding_dim(self, dim: int) -> None:
        """Store the embedding dimension in meta table.

        Args:
            dim: Embedding vector dimension
        """
        self.set_meta("embedding_dim", str(dim))

    def ensure_vec_table(self, dim: int) -> None:
        """Ensure the vector table exists with the correct dimension.

        Stores dimension in meta and creates the table if needed.

        Args:
            dim: Embedding vector dimension
        """
        with self._write_scope():
            self._ensure_vec_table(dim)

    def _ensure_vec_table(self, dim: int) -> None:
        """Ensure vector metadata and schema without committing."""
        stored_dim = self.get_embedding_dim()
        if stored_dim is None:
            self._set_meta("embedding_dim", str(dim))
            self._create_vec_table(dim)
        elif stored_dim != dim:
            # Dimension mismatch — caller should handle this
            raise DimensionMismatchError(stored_dim, dim)
        elif not self.has_vec_table():
            self._create_vec_table(dim)

    def insert_memory(self, mem: Memory, details: Optional[str] = None) -> int:
        """Insert a memory into the database.

        Args:
            mem: Memory object to insert
            details: Optional full details/body text

        Returns:
            The rowid of the inserted memory
        """
        with self._write_scope():
            return self._insert_memory(mem, details)

    def _insert_memory(self, mem: Memory, details: Optional[str] = None) -> int:
        """Insert a memory projection without committing."""
        cursor = self.conn.cursor()

        # Serialize lists as JSON
        tags_json = json.dumps(mem.tags)
        related_files_json = json.dumps(mem.related_files)
        contributors_json = json.dumps(mem.contributors)
        operations_json = json.dumps([asdict(operation) for operation in mem.operations])

        cursor.execute("""
            INSERT INTO memories (
                id, title, what, why, impact, tags, category, project,
                source, related_files, file_path, section_anchor,
                created_at, updated_at, status, archived_at, archive_reason, superseded_by,
                structured_data, confidence, valid_from, valid_until, commit_sha, branch,
                links, last_verified, creator_source, last_updated_by, contributors,
                operation_history, content_fingerprint, history_complete, updated_count
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
        """, (
            mem.id, mem.title, mem.what, mem.why, mem.impact,
            tags_json, mem.category, mem.project, mem.source,
            related_files_json, mem.file_path, mem.section_anchor,
            mem.created_at, mem.updated_at, mem.status, mem.archived_at, mem.archive_reason, mem.superseded_by,
            json.dumps(mem.structured_data), mem.confidence, mem.valid_from, mem.valid_until,
            mem.commit_sha, mem.branch, json.dumps(mem.links), mem.last_verified,
            mem.creator_source, mem.last_updated_by, contributors_json, operations_json,
            mem.content_fingerprint, mem.history_complete, mem.updated_count,
        ))

        rowid = int(cursor.lastrowid)

        # Insert details if provided
        if details:
            self._replace_details(mem.id, details)

        return rowid

    def upsert_memory(self, mem: Memory, details: Optional[str]) -> int:
        """Insert or replace a canonical memory projection and its details."""
        with self._write_scope():
            return self._upsert_memory(mem, details)

    def _upsert_memory(self, mem: Memory, details: Optional[str]) -> int:
        """Upsert a memory projection without committing."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT rowid FROM memories WHERE id = ?", (mem.id,))
        existing = cursor.fetchone()

        if existing is None:
            rowid = self._insert_memory(mem)
        else:
            rowid = int(existing["rowid"])
            cursor.execute("""
                UPDATE memories SET
                    title = ?, what = ?, why = ?, impact = ?, tags = ?, category = ?,
                    project = ?, source = ?, related_files = ?, file_path = ?,
                    section_anchor = ?, created_at = ?, updated_at = ?, status = ?,
                    archived_at = ?, archive_reason = ?, superseded_by = ?,
                    structured_data = ?, confidence = ?, valid_from = ?, valid_until = ?,
                    commit_sha = ?, branch = ?, links = ?, last_verified = ?,
                    creator_source = ?, last_updated_by = ?, contributors = ?,
                    operation_history = ?, content_fingerprint = ?, history_complete = ?,
                    updated_count = ?
                WHERE id = ?
            """, (
                mem.title, mem.what, mem.why, mem.impact, json.dumps(mem.tags),
                mem.category, mem.project, mem.source, json.dumps(mem.related_files),
                mem.file_path, mem.section_anchor, mem.created_at, mem.updated_at,
                mem.status, mem.archived_at, mem.archive_reason, mem.superseded_by,
                json.dumps(mem.structured_data), mem.confidence, mem.valid_from,
                mem.valid_until, mem.commit_sha, mem.branch, json.dumps(mem.links),
                mem.last_verified, mem.creator_source, mem.last_updated_by,
                json.dumps(mem.contributors),
                json.dumps([asdict(operation) for operation in mem.operations]),
                mem.content_fingerprint, mem.history_complete, mem.updated_count, mem.id,
            ))

        self._replace_details(mem.id, details)
        return rowid

    def replace_details(self, memory_id: str, details: Optional[str]) -> None:
        """Replace or remove the derived detail body for a memory."""
        with self._write_scope():
            self._replace_details(memory_id, details)

    def _replace_details(self, memory_id: str, details: Optional[str]) -> None:
        """Replace projected details without committing."""
        cursor = self.conn.cursor()
        if details is None:
            cursor.execute("DELETE FROM memory_details WHERE memory_id = ?", (memory_id,))
            return

        cursor.execute("""
            INSERT INTO memory_details (memory_id, body)
            VALUES (?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET body = excluded.body
        """, (memory_id, details))

    def insert_vector(self, rowid: int, embedding: list[float]) -> None:
        """Insert an embedding vector for a memory.

        Args:
            rowid: The rowid of the memory
            embedding: The embedding vector
        """
        if not self.has_vec_table():
            return

        with self._write_scope():
            self._insert_vector(rowid, embedding)

    def _insert_vector(self, rowid: int, embedding: list[float]) -> None:
        """Insert a vector without committing."""

        vec_bytes = struct.pack(f"{len(embedding)}f", *embedding)

        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO memories_vec (rowid, embedding)
            VALUES (?, ?)
        """, (rowid, vec_bytes))

    def invalidate_vector(self, memory_id: str) -> None:
        """Remove a derived vector for a memory if one exists."""
        if not self.has_vec_table():
            return

        with self._write_scope():
            self._invalidate_vector(memory_id)

    def _invalidate_vector(self, memory_id: str) -> None:
        """Remove a vector without committing."""
        if not self.has_vec_table():
            return

        cursor = self.conn.cursor()
        cursor.execute("SELECT rowid FROM memories WHERE id = ?", (memory_id,))
        memory_row = cursor.fetchone()
        if memory_row is not None:
            cursor.execute(
                "DELETE FROM memories_vec WHERE rowid = ?", (memory_row["rowid"],)
            )

    def has_vector(self, memory_id: str) -> bool:
        """Return whether a memory currently has a derived vector."""
        if not self.has_vec_table():
            return False

        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT 1
            FROM memories_vec v
            JOIN memories m ON m.rowid = v.rowid
            WHERE m.id = ?
            LIMIT 1
        """, (memory_id,))
        return cursor.fetchone() is not None

    def upsert_vector_if_current(
        self,
        memory_id: str,
        expected_fingerprint: str,
        embedding: list[float],
    ) -> bool:
        """Replace a vector only while its canonical fingerprint is current."""
        with self._write_scope():
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT rowid, content_fingerprint, status FROM memories WHERE id = ?",
                (memory_id,),
            )
            memory_row = cursor.fetchone()

            barrier = self._vector_cas_test_barrier
            if barrier is not None:
                barrier()

            if (
                memory_row is None
                or memory_row["content_fingerprint"] != expected_fingerprint
                or (memory_row["status"] or "active") != "active"
            ):
                return False

            self._ensure_vec_table(len(embedding))
            self._invalidate_vector(memory_id)
            self._insert_vector(int(memory_row["rowid"]), embedding)
            return True

    def queue_vector_repair(
        self,
        memory_id: str,
        content_fingerprint: str,
        queued_at: str,
    ) -> None:
        """Durably queue vector work in the caller's projection transaction."""
        with self._write_scope():
            self.conn.execute(
                """
                INSERT INTO pending_vector_repairs (
                    memory_id, content_fingerprint, queued_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    content_fingerprint = excluded.content_fingerprint,
                    queued_at = excluded.queued_at
                """,
                (memory_id, content_fingerprint, queued_at),
            )

    def clear_vector_repair(
        self,
        memory_id: str,
        expected_fingerprint: str | None = None,
    ) -> bool:
        """Clear only the repair generation the caller actually completed."""
        with self._write_scope():
            if expected_fingerprint is None:
                cursor = self.conn.execute(
                    "DELETE FROM pending_vector_repairs WHERE memory_id = ?",
                    (memory_id,),
                )
            else:
                cursor = self.conn.execute(
                    """
                    DELETE FROM pending_vector_repairs
                    WHERE memory_id = ? AND content_fingerprint = ?
                    """,
                    (memory_id, expected_fingerprint),
                )
            return cursor.rowcount > 0

    def list_vector_repairs(self) -> list[dict]:
        """List queued vector work deterministically without mutating it."""
        cursor = self.conn.execute(
            """
            SELECT memory_id, content_fingerprint, queued_at
            FROM pending_vector_repairs
            ORDER BY queued_at, memory_id
            """
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_operation(self, project: str, operation_id: str) -> Optional[dict]:
        """Get one project-scoped idempotency ledger record."""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT project, operation_id, memory_id, request_fingerprint, action,
                   source, timestamp, branch, commit_sha
            FROM save_operations
            WHERE project = ? AND operation_id = ?
        """, (project, operation_id))
        row = cursor.fetchone()
        return dict(row) if row is not None else None

    def upsert_operation(
        self, project: str, memory_id: str, operation: MemoryOperation
    ) -> None:
        """Upsert one project-scoped operation ledger record."""
        with self._write_scope():
            self._upsert_operation(project, memory_id, operation)

    def _upsert_operation(
        self, project: str, memory_id: str, operation: MemoryOperation
    ) -> None:
        """Upsert an operation ledger record without committing."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT 1 FROM memories WHERE id = ?", (memory_id,))
        if cursor.fetchone() is None:
            raise ValueError(
                f"Cannot record operation for missing memory: {memory_id}"
            )

        cursor.execute("""
            INSERT INTO save_operations (
                project, operation_id, memory_id, request_fingerprint, action,
                source, timestamp, branch, commit_sha
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project, operation_id) DO UPDATE SET
                memory_id = excluded.memory_id,
                request_fingerprint = excluded.request_fingerprint,
                action = excluded.action,
                source = excluded.source,
                timestamp = excluded.timestamp,
                branch = excluded.branch,
                commit_sha = excluded.commit_sha
        """, (
            project,
            operation.operation_id,
            memory_id,
            operation.request_fingerprint,
            operation.action,
            operation.source,
            operation.timestamp,
            operation.branch,
            operation.commit_sha,
        ))

    def get_memory(self, memory_id: str) -> Optional[dict]:
        """Get a memory by ID.

        Args:
            memory_id: The memory ID to retrieve

        Returns:
            Dictionary with memory data and has_details flag, or None if not found
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT m.*,
                   EXISTS(SELECT 1 FROM memory_details WHERE memory_id = m.id) as has_details
            FROM memories m
            WHERE m.id = ?
        """, (memory_id,))

        row = cursor.fetchone()
        if row:
            return dict(row)
        return None

    def get_details(self, memory_id: str) -> Optional[MemoryDetail]:
        """Get full details for a memory.

        Args:
            memory_id: The memory ID

        Returns:
            MemoryDetail object or None if no details exist
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT memory_id, body
            FROM memory_details
            WHERE memory_id LIKE ?
        """, (memory_id + "%",))

        row = cursor.fetchone()
        if row:
            self.record_feedback([row["memory_id"]], "details_opened")
            return MemoryDetail(memory_id=row["memory_id"], body=row["body"])
        return None

    def update_memory(
        self,
        memory_id: str,
        what: str | None = None,
        why: str | None = None,
        impact: str | None = None,
        tags: list[str] | None = None,
        details_append: str | None = None,
        structured_data: dict | None = None,
        provenance: dict | None = None,
    ) -> bool:
        """Update a memory, owning a transaction only when called standalone."""
        with self._write_scope():
            return self._update_memory(
                memory_id,
                what=what,
                why=why,
                impact=impact,
                tags=tags,
                details_append=details_append,
                structured_data=structured_data,
                provenance=provenance,
            )

    def _update_memory(
        self,
        memory_id: str,
        what: str | None = None,
        why: str | None = None,
        impact: str | None = None,
        tags: list[str] | None = None,
        details_append: str | None = None,
        structured_data: dict | None = None,
        provenance: dict | None = None,
    ) -> bool:
        """Update an existing memory's fields and increment updated_count.

        Args:
            memory_id: Full UUID or prefix of the memory to update
            what: New what text (replaces existing)
            why: New why text (replaces existing)
            impact: New impact text (replaces existing)
            tags: New tag list (replaces existing)
            details_append: Text to append to existing details

        Returns:
            True if updated, False if not found
        """
        cursor = self.conn.cursor()

        # Resolve full ID from prefix
        cursor.execute("SELECT id, rowid FROM memories WHERE id LIKE ?", (memory_id + "%",))
        row = cursor.fetchone()
        if not row:
            return False

        full_id = row["id"]

        # Build SET clauses dynamically
        from datetime import datetime, timezone
        sets = ["updated_count = updated_count + 1", "updated_at = ?"]
        params: list = [datetime.now(timezone.utc).isoformat()]

        if what is not None:
            sets.append("what = ?")
            params.append(what)
        if why is not None:
            sets.append("why = ?")
            params.append(why)
        if impact is not None:
            sets.append("impact = ?")
            params.append(impact)
        if tags is not None:
            sets.append("tags = ?")
            params.append(json.dumps(tags))
        if structured_data is not None:
            sets.append("structured_data = ?")
            params.append(json.dumps(structured_data))
        for key in ("confidence", "valid_from", "valid_until", "commit_sha", "branch", "last_verified"):
            if provenance and key in provenance:
                sets.append(f"{key} = ?")
                params.append(provenance[key])
        if provenance and "links" in provenance:
            sets.append("links = ?")
            params.append(json.dumps(provenance["links"]))

        params.append(full_id)
        cursor.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id = ?", params)

        # Handle details append
        if details_append:
            cursor.execute("SELECT body FROM memory_details WHERE memory_id = ?", (full_id,))
            existing = cursor.fetchone()
            if existing:
                new_body = existing["body"] + "\n\n" + details_append
                cursor.execute("UPDATE memory_details SET body = ? WHERE memory_id = ?", (new_body, full_id))
            else:
                cursor.execute("INSERT INTO memory_details (memory_id, body) VALUES (?, ?)", (full_id, details_append))

        return True

    def delete_memory(self, memory_id: str) -> bool:
        """Delete a memory by ID or prefix.

        Removes the memory from the memories table, memory_details, and FTS index.

        Args:
            memory_id: Full UUID or prefix to match

        Returns:
            True if a memory was deleted, False if no match found
        """
        with self._write_scope():
            return self._delete_memory(memory_id)

    def delete_memory_exact(self, memory_id: str) -> bool:
        """Delete exactly one projected canonical UUID, never a prefix."""
        with self._write_scope():
            cursor = self.conn.cursor()
            row = cursor.execute(
                "SELECT id FROM memories WHERE id = ?",
                (memory_id,),
            ).fetchone()
            if row is None:
                return False
            self._delete_full_memory(str(row["id"]))
            return True

    def _delete_memory(self, memory_id: str) -> bool:
        """Delete a projected memory without committing."""
        if not memory_id:
            return False
        cursor = self.conn.cursor()

        rows = cursor.execute(
            "SELECT id FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchall()
        if not rows:
            rows = cursor.execute(
                "SELECT id FROM memories "
                "WHERE substr(id, 1, length(?)) = ? ORDER BY id",
                (memory_id, memory_id),
            ).fetchall()
        if len(rows) != 1:
            return False

        self._delete_full_memory(str(rows[0]["id"]))
        return True

    def _delete_full_memory(self, full_id: str) -> None:
        """Delete a previously resolved full projection ID."""
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM memory_details WHERE memory_id = ?", (full_id,))
        self._invalidate_vector(full_id)
        cursor.execute("DELETE FROM memories WHERE id = ?", (full_id,))

    def fts_search(
        self,
        query: str,
        limit: int = 10,
        project: Optional[str] = None,
        source: Optional[str] = None,
        include_archived: bool = False,
    ) -> list[dict]:
        """Search memories using FTS5 full-text search.

        Args:
            query: Search query string
            limit: Maximum number of results
            project: Optional project filter
            source: Optional source filter

        Returns:
            List of memory dictionaries with BM25 scores
        """
        # Build prefix matching query while filtering obvious stop-word noise.
        fts_query = _build_fts_query(query)

        # Build WHERE clause for filters
        where_clauses = []
        params = [fts_query]

        if project:
            where_clauses.append("m.project = ?")
            params.append(project)

        if source:
            where_clauses.append("m.source = ?")
            params.append(source)
        if not include_archived:
            where_clauses.append("(m.status IS NULL OR m.status = 'active')")

        where_clause = ""
        if where_clauses:
            where_clause = "AND " + " AND ".join(where_clauses)

        params.append(limit)

        cursor = self.conn.cursor()
        cursor.execute(f"""
            SELECT m.*, -fts.rank as score,
                   EXISTS(SELECT 1 FROM memory_details WHERE memory_id = m.id) as has_details
            FROM memories_fts fts
            JOIN memories m ON m.rowid = fts.rowid
            WHERE fts.memories_fts MATCH ?
            {where_clause}
            ORDER BY fts.rank
            LIMIT ?
        """, params)

        return [dict(row) for row in cursor.fetchall()]

    def record_feedback(self, memory_ids: list[str], event: str = "retrieved") -> int:
        """Record local, aggregate retrieval feedback without storing prompts."""
        if not memory_ids:
            return 0

        with self._write_scope():
            return self._record_feedback(memory_ids, event)

    def _record_feedback(self, memory_ids: list[str], event: str = "retrieved") -> int:
        """Record feedback counters without committing."""
        columns = {
            "retrieved": "retrieved_count", "details_opened": "details_opened_count",
            "dismissed": "dismissed_count", "referenced": "retrieved_count",
        }
        column = columns.get(event)
        if not column or not memory_ids:
            return 0
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.cursor()
        count = 0
        for memory_id in memory_ids:
            cursor.execute(
                f"UPDATE memories SET {column} = COALESCE({column}, 0) + 1, last_used_at = ? WHERE id LIKE ?",
                (now, memory_id + "%"),
            )
            count += cursor.rowcount
        return count

    def vector_search(
        self,
        query_embedding: list[float],
        limit: int = 10,
        project: Optional[str] = None,
        source: Optional[str] = None,
        include_archived: bool = False,
    ) -> list[dict]:
        """Search memories using vector similarity.

        Args:
            query_embedding: Query embedding vector
            limit: Maximum number of results
            project: Optional project filter
            source: Optional source filter

        Returns:
            List of memory dictionaries with similarity scores
        """
        if not self.has_vec_table():
            return []

        vec_bytes = struct.pack(f"{len(query_embedding)}f", *query_embedding)

        # sqlite-vec applies k before SQL post-filters. When filtering by project or
        # source, over-fetch candidate vectors so the final filtered set still has
        # relevant rows from the desired slice.
        fetch_k = limit
        if project or source:
            fetch_k = max(limit * 20, 100)

        where_clauses = ["v.embedding MATCH ?", "k = ?"]
        params: list = [vec_bytes, fetch_k]

        if project:
            where_clauses.append("m.project = ?")
            params.append(project)

        if source:
            where_clauses.append("m.source = ?")
            params.append(source)
        if not include_archived:
            where_clauses.append("(m.status IS NULL OR m.status = 'active')")

        where_clause = " AND ".join(where_clauses)

        cursor = self.conn.cursor()
        cursor.execute(f"""
            SELECT m.*, v.distance,
                   EXISTS(SELECT 1 FROM memory_details WHERE memory_id = m.id) as has_details
            FROM memories_vec v
            JOIN memories m ON m.rowid = v.rowid
            WHERE {where_clause}
            ORDER BY v.distance
        """, params)

        results = []
        for row in cursor.fetchall():
            result = dict(row)
            # sqlite-vec returns distance where smaller is better. Convert it to a
            # bounded positive similarity score so hybrid ranking can merge it.
            distance = float(result["distance"])
            result["score"] = 1.0 / (1.0 + max(distance, 0.0))
            del result["distance"]
            results.append(result)

        return results

    def list_recent(
        self,
        limit: int = 10,
        project: Optional[str] = None,
        source: Optional[str] = None,
        include_archived: bool = False,
    ) -> list[dict]:
        """List recent memories ordered by creation date descending.

        Args:
            limit: Maximum number of results
            project: Optional project filter
            source: Optional source filter

        Returns:
            List of memory dictionaries with metadata
        """
        where_clauses = []
        params: list = []

        if project:
            where_clauses.append("m.project = ?")
            params.append(project)

        if source:
            where_clauses.append("m.source = ?")
            params.append(source)
        if not include_archived:
            where_clauses.append("(m.status IS NULL OR m.status = 'active')")

        where_clause = ""
        if where_clauses:
            where_clause = "WHERE " + " AND ".join(where_clauses)

        params.append(limit)

        cursor = self.conn.cursor()
        cursor.execute(f"""
            SELECT m.id, m.title, m.category, m.tags, m.project, m.source, m.created_at,
                   EXISTS(SELECT 1 FROM memory_details WHERE memory_id = m.id) as has_details
            FROM memories m
            {where_clause}
            ORDER BY m.created_at DESC
            LIMIT ?
        """, params)

        return [dict(row) for row in cursor.fetchall()]

    def list_all_for_reindex(self) -> list[dict]:
        """List all memories with fields needed for re-embedding.

        Returns:
            List of dicts with rowid, title, what, why, impact, tags
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT rowid, title, what, why, impact, tags
            FROM memories
            ORDER BY rowid
        """)
        return [dict(row) for row in cursor.fetchall()]

    def count_memories(
        self,
        project: Optional[str] = None,
        source: Optional[str] = None,
        include_archived: bool = False,
    ) -> int:
        """Count total memories with optional filters.

        Args:
            project: Optional project filter
            source: Optional source filter

        Returns:
            Total count of matching memories
        """
        where_clauses = []
        params: list = []

        if project:
            where_clauses.append("project = ?")
            params.append(project)

        if source:
            where_clauses.append("source = ?")
            params.append(source)
        if not include_archived:
            where_clauses.append("(status IS NULL OR status = 'active')")

        where_clause = ""
        if where_clauses:
            where_clause = "WHERE " + " AND ".join(where_clauses)

        cursor = self.conn.cursor()
        cursor.execute(f"""
            SELECT COUNT(*) FROM memories {where_clause}
        """, params)

        return cursor.fetchone()[0]

    def list_memories(
        self,
        limit: int = 200,
        project: Optional[str] = None,
        category: Optional[str] = None,
        file_path: Optional[str] = None,
        include_archived: bool = False,
    ) -> list[dict]:
        """List memories with dashboard-friendly filters."""
        where_clauses = []
        params: list = []

        if project:
            where_clauses.append("m.project = ?")
            params.append(project)
        if category:
            where_clauses.append("m.category = ?")
            params.append(category)
        if file_path:
            where_clauses.append("m.file_path = ?")
            params.append(file_path)
        if not include_archived:
            where_clauses.append("(m.status IS NULL OR m.status = 'active')")

        where_clause = ""
        if where_clauses:
            where_clause = "WHERE " + " AND ".join(where_clauses)

        params.append(limit)
        cursor = self.conn.cursor()
        cursor.execute(f"""
            SELECT m.*,
                   EXISTS(SELECT 1 FROM memory_details WHERE memory_id = m.id) as has_details
            FROM memories m
            {where_clause}
            ORDER BY m.updated_at DESC, m.created_at DESC
            LIMIT ?
        """, params)
        return [dict(row) for row in cursor.fetchall()]

    def set_meta(self, key: str, value: str) -> None:
        """Set a metadata key-value pair.

        Args:
            key: Metadata key
            value: Metadata value
        """
        with self._write_scope():
            self._set_meta(key, value)

    def _set_meta(self, key: str, value: str) -> None:
        """Set metadata without committing."""
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO meta (key, value)
            VALUES (?, ?)
        """, (key, value))

    def get_meta(self, key: str) -> Optional[str]:
        """Get a metadata value by key.

        Args:
            key: Metadata key

        Returns:
            Metadata value or None if key doesn't exist
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT value FROM meta WHERE key = ?
        """, (key,))

        row = cursor.fetchone()
        if row:
            return row["value"]
        return None

    def close(self) -> None:
        """Close the database connection."""
        self.conn.close()


class DimensionMismatchError(Exception):
    """Raised when embedding dimension doesn't match stored dimension."""

    def __init__(self, stored_dim: int, new_dim: int):
        self.stored_dim = stored_dim
        self.new_dim = new_dim
        super().__init__(
            f"Embedding dimension mismatch: database has {stored_dim}, "
            f"provider returned {new_dim}. Run 'memory reindex' to rebuild."
        )
