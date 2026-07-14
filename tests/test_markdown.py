"""Tests for markdown session file rendering and writing."""

import os
import tempfile
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memory.markdown import (
    SessionDocument,
    SessionEntry,
    parse_session_file,
    render_section,
    render_session_document,
    write_session_memory,
)
from memory.models import Memory, MemoryOperation


def document_with(memory: Memory) -> SessionDocument:
    """Build a one-entry schema-v2 document for rendering tests."""
    return SessionDocument(
        project=memory.project,
        created=memory.created_at,
        tags=memory.tags,
        sources=[memory.source] if memory.source else [],
        title="2026-07-14 Session",
        entries=[
            SessionEntry(
                id=memory.id,
                title=memory.title,
                what=memory.what,
                why=memory.why,
                impact=memory.impact,
                source=memory.source,
                details=None,
                category=memory.category,
                status=memory.status,
                archived_at=memory.archived_at,
                archive_reason=memory.archive_reason,
                superseded_by=memory.superseded_by,
                section_anchor=memory.section_anchor,
                metadata={
                    "project": memory.project,
                    "tags": memory.tags,
                    "category": memory.category,
                    "related_files": memory.related_files,
                    "section_anchor": memory.section_anchor,
                    "created_at": memory.created_at,
                    "updated_at": memory.updated_at,
                    "status": memory.status,
                    "archived_at": memory.archived_at,
                    "archive_reason": memory.archive_reason,
                    "superseded_by": memory.superseded_by,
                    "structured_data": memory.structured_data,
                    "confidence": memory.confidence,
                    "valid_from": memory.valid_from,
                    "valid_until": memory.valid_until,
                    "commit_sha": memory.commit_sha,
                    "branch": memory.branch,
                    "links": memory.links,
                    "last_verified": memory.last_verified,
                    "creator_source": memory.creator_source,
                    "last_updated_by": memory.last_updated_by,
                    "contributors": memory.contributors,
                    "operations": [asdict(operation) for operation in memory.operations],
                    "content_fingerprint": memory.content_fingerprint,
                    "history_complete": memory.history_complete,
                    "updated_count": memory.updated_count,
                },
                metadata_complete=True,
            )
        ],
        schema_version=2,
    )


@pytest.fixture
def sample_memory() -> Memory:
    """Create a sample memory for testing."""
    return Memory(
        id="test-123",
        title="Use FastAPI for API endpoints",
        what="Implemented REST API using FastAPI framework",
        why="FastAPI provides automatic validation and documentation",
        impact="Reduces boilerplate code by 40%",
        tags=["api", "fastapi"],
        category="decision",
        project="my-project",
        source="claude-code",
        related_files=["/src/api/main.py"],
        file_path="2026-01-22-session.md",
        section_anchor="use-fastapi-for-api-endpoints",
        created_at="2026-01-22T14:30:00Z",
        updated_at="2026-01-22T14:30:00Z",
    )


@pytest.fixture
def minimal_memory() -> Memory:
    """Create a minimal memory without optional fields."""
    return Memory(
        id="test-456",
        title="Basic memory",
        what="Simple memory entry",
        why=None,
        impact=None,
        tags=["test"],
        category="context",
        project="my-project",
        source=None,
        related_files=[],
        file_path="2026-01-22-session.md",
        section_anchor="basic-memory",
        created_at="2026-01-22T14:30:00Z",
        updated_at="2026-01-22T14:30:00Z",
    )


class TestRenderSection:
    """Tests for render_section function."""

    def test_render_section_all_fields(self, sample_memory: Memory) -> None:
        """Test rendering section with all fields populated."""
        result = render_section(sample_memory)

        assert "### Use FastAPI for API endpoints" in result
        assert "**What:** Implemented REST API using FastAPI framework" in result
        assert "**Why:** FastAPI provides automatic validation and documentation" in result
        assert "**Impact:** Reduces boilerplate code by 40%" in result
        assert "**Source:** claude-code" in result
        assert "<details>" not in result

    def test_render_section_with_details(self, sample_memory: Memory) -> None:
        """Test rendering section with details tag."""
        details = "Here is the full implementation:\n\n```python\nfrom fastapi import FastAPI\n```"
        result = render_section(sample_memory, details=details)

        assert "### Use FastAPI for API endpoints" in result
        assert "<details>" in result
        assert details in result
        assert "</details>" in result

    def test_render_section_without_optional_fields(self, minimal_memory: Memory) -> None:
        """Test rendering section without Why, Impact, and Source."""
        result = render_section(minimal_memory)

        assert "### Basic memory" in result
        assert "**What:** Simple memory entry" in result
        assert "**Why:**" not in result
        assert "**Impact:**" not in result
        assert "**Source:**" not in result
        assert "<details>" not in result


class TestWriteSessionMemory:
    """Tests for write_session_memory function."""

    @pytest.fixture
    def temp_vault(self) -> str:
        """Create temporary vault directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = Path(tmpdir) / "test-vault" / "my-project"
            vault_path.mkdir(parents=True)
            yield str(vault_path)

    def test_write_creates_new_session_file(self, temp_vault: str, sample_memory: Memory) -> None:
        """Test creating a new session file with correct structure."""
        file_path = write_session_memory(temp_vault, sample_memory, "2026-01-22")

        assert os.path.exists(file_path)
        assert file_path.endswith("2026-01-22-session.md")

        content = Path(file_path).read_text()

        # Check frontmatter
        assert "---" in content
        assert "project: my-project" in content
        assert "sources: [claude-code]" in content
        assert "created:" in content
        assert "tags: [api, fastapi]" in content

        # Check H1
        assert "# 2026-01-22 Session" in content

        # Check category H2
        assert "## Decisions" in content

        # Check memory section
        assert "### Use FastAPI for API endpoints" in content
        assert "**What:** Implemented REST API using FastAPI framework" in content

    def test_write_appends_to_existing_same_category(self, temp_vault: str, sample_memory: Memory) -> None:
        """Test appending to existing session under same category."""
        # Create initial file
        file_path = write_session_memory(temp_vault, sample_memory, "2026-01-22")

        # Create second memory in same category
        memory2 = Memory(
            id="test-789",
            title="Another decision",
            what="Made another decision",
            why=None,
            impact=None,
            tags=["decision"],
            category="decision",
            project="my-project",
            source="claude-code",
            related_files=[],
            file_path="2026-01-22-session.md",
            section_anchor="another-decision",
            created_at="2026-01-22T15:00:00Z",
            updated_at="2026-01-22T15:00:00Z",
        )

        file_path2 = write_session_memory(temp_vault, memory2, "2026-01-22")
        assert file_path == file_path2

        content = Path(file_path).read_text()

        # Should have only one "## Decisions" heading
        assert content.count("## Decisions") == 1

        # Should have both memories
        assert "### Use FastAPI for API endpoints" in content
        assert "### Another decision" in content

    def test_write_appends_different_category(self, temp_vault: str, sample_memory: Memory) -> None:
        """Test appending with different category creates new H2."""
        # Create initial file with decision
        file_path = write_session_memory(temp_vault, sample_memory, "2026-01-22")

        # Create memory with different category
        memory2 = Memory(
            id="test-999",
            title="Bug fix description",
            what="Fixed a critical bug",
            why="Bug was causing crashes",
            impact="Improved stability",
            tags=["bugfix"],
            category="bug",
            project="my-project",
            source="claude-code",
            related_files=[],
            file_path="2026-01-22-session.md",
            section_anchor="bug-fix-description",
            created_at="2026-01-22T15:30:00Z",
            updated_at="2026-01-22T15:30:00Z",
        )

        write_session_memory(temp_vault, memory2, "2026-01-22")

        content = Path(file_path).read_text()

        # Should have both category headings
        assert "## Decisions" in content
        assert "## Bugs Fixed" in content

        # Check category order (Decisions before Bugs Fixed)
        decisions_pos = content.index("## Decisions")
        bugs_pos = content.index("## Bugs Fixed")
        assert decisions_pos < bugs_pos

    def test_write_updates_frontmatter_tags(self, temp_vault: str, sample_memory: Memory) -> None:
        """Test that tags are merged, deduplicated, and sorted."""
        # Create initial file
        file_path = write_session_memory(temp_vault, sample_memory, "2026-01-22")

        # Create memory with overlapping and new tags
        memory2 = Memory(
            id="test-111",
            title="New pattern",
            what="Discovered a pattern",
            why=None,
            impact=None,
            tags=["api", "pattern", "architecture"],  # "api" overlaps
            category="pattern",
            project="my-project",
            source="claude-code",
            related_files=[],
            file_path="2026-01-22-session.md",
            section_anchor="new-pattern",
            created_at="2026-01-22T16:00:00Z",
            updated_at="2026-01-22T16:00:00Z",
        )

        write_session_memory(temp_vault, memory2, "2026-01-22")

        content = Path(file_path).read_text()

        # Tags should be merged, deduplicated, and sorted
        assert "tags: [api, architecture, fastapi, pattern]" in content

    def test_write_updates_frontmatter_sources(self, temp_vault: str, sample_memory: Memory) -> None:
        """Test that sources list is updated when multiple agents contribute."""
        # Create initial file
        file_path = write_session_memory(temp_vault, sample_memory, "2026-01-22")

        # Create memory from different source
        memory2 = Memory(
            id="test-222",
            title="Manual entry",
            what="Manually added memory",
            why=None,
            impact=None,
            tags=["manual"],
            category="context",
            project="my-project",
            source="user",
            related_files=[],
            file_path="2026-01-22-session.md",
            section_anchor="manual-entry",
            created_at="2026-01-22T17:00:00Z",
            updated_at="2026-01-22T17:00:00Z",
        )

        write_session_memory(temp_vault, memory2, "2026-01-22")

        content = Path(file_path).read_text()

        # Sources should include both
        assert "sources: [claude-code, user]" in content

    def test_write_with_details(self, temp_vault: str, sample_memory: Memory) -> None:
        """Test writing memory with details section."""
        details = "Full implementation details here"
        file_path = write_session_memory(temp_vault, sample_memory, "2026-01-22", details=details)

        content = Path(file_path).read_text()

        assert "<details>" in content
        assert details in content
        assert "</details>" in content

    def test_write_maintains_category_order(self, temp_vault: str) -> None:
        """Test that categories are inserted in correct order."""
        # Create memories in reverse order
        learning_mem = Memory(
            id="l1", title="L", what="Learning", why=None, impact=None,
            tags=[], category="learning", project="my-project", source="claude-code",
            related_files=[], file_path="", section_anchor="l",
            created_at="2026-01-22T10:00:00Z", updated_at="2026-01-22T10:00:00Z",
        )

        decision_mem = Memory(
            id="d1", title="D", what="Decision", why=None, impact=None,
            tags=[], category="decision", project="my-project", source="claude-code",
            related_files=[], file_path="", section_anchor="d",
            created_at="2026-01-22T10:00:00Z", updated_at="2026-01-22T10:00:00Z",
        )

        pattern_mem = Memory(
            id="p1", title="P", what="Pattern", why=None, impact=None,
            tags=[], category="pattern", project="my-project", source="claude-code",
            related_files=[], file_path="", section_anchor="p",
            created_at="2026-01-22T10:00:00Z", updated_at="2026-01-22T10:00:00Z",
        )

        # Write in non-standard order
        file_path = write_session_memory(temp_vault, learning_mem, "2026-01-22")
        write_session_memory(temp_vault, decision_mem, "2026-01-22")
        write_session_memory(temp_vault, pattern_mem, "2026-01-22")

        content = Path(file_path).read_text()

        # Categories should appear in standard order
        decisions_pos = content.index("## Decisions")
        patterns_pos = content.index("## Patterns")
        learnings_pos = content.index("## Learnings")

        assert decisions_pos < patterns_pos < learnings_pos

    def test_write_uses_utf8_for_unicode_content(
        self, temp_vault: str, sample_memory: Memory
    ) -> None:
        """Test writing Unicode content does not depend on system locale."""
        sample_memory.what = "unicode — кириллица ✓"

        file_path = write_session_memory(temp_vault, sample_memory, "2026-01-22")

        content = Path(file_path).read_text(encoding="utf-8")
        assert "unicode — кириллица ✓" in content

    def test_write_reads_legacy_cp1251_and_rewrites_utf8(self, temp_vault: str) -> None:
        """Test appending to a legacy cp1251 file rewrites it as UTF-8."""
        file_path = Path(temp_vault) / "2026-01-22-session.md"
        legacy_content = """---
project: my-project
sources: [claude-code]
created: 2026-01-22T14:30:00Z
tags: [legacy]
---

# 2026-01-22 Session

## Context

### Legacy entry
**What:** старый текст
**Source:** claude-code
"""
        file_path.write_text(legacy_content, encoding="cp1251")

        memory = Memory(
            id="test-unicode",
            title="Unicode append",
            what="новый текст — ✓",
            why=None,
            impact=None,
            tags=["unicode"],
            category="context",
            project="my-project",
            source="claude-code",
            related_files=[],
            file_path="2026-01-22-session.md",
            section_anchor="unicode-append",
            created_at="2026-01-22T18:00:00Z",
            updated_at="2026-01-22T18:00:00Z",
        )

        write_session_memory(temp_vault, memory, "2026-01-22")

        content = file_path.read_text(encoding="utf-8")
        assert "старый текст" in content
        assert "новый текст — ✓" in content


def test_schema_v2_round_trip_preserves_every_memory_field(
    tmp_path: Path, sample_memory: Memory
) -> None:
    details = "Context:\n\nExact details body."
    sample_memory.project = "api--111111111111"
    sample_memory.source = "cursor"
    sample_memory.category = "decision"
    sample_memory.tags = ["API", "validation"]
    sample_memory.related_files = ["src/api/routes.py"]
    sample_memory.created_at = "2026-07-14T10:00:00+00:00"
    sample_memory.updated_at = "2026-07-14T11:00:00+00:00"
    sample_memory.status = "archived"
    sample_memory.archived_at = "2026-07-14T11:30:00+00:00"
    sample_memory.archive_reason = "superseded"
    sample_memory.superseded_by = "memory-2"
    sample_memory.structured_data = {
        "constraints": ["Never expose internal models"],
        "steps": ["Validate output"],
    }
    sample_memory.confidence = 0.95
    sample_memory.valid_from = "2026-07-14"
    sample_memory.valid_until = "2027-07-14"
    sample_memory.commit_sha = "abc123"
    sample_memory.branch = "main"
    sample_memory.links = ["https://example.test/decision"]
    sample_memory.last_verified = "2026-07-14T10:30:00+00:00"
    sample_memory.creator_source = "cursor"
    sample_memory.last_updated_by = "gemini-cli"
    sample_memory.contributors = ["cursor", "gemini-cli"]
    sample_memory.content_fingerprint = "sha256:abc"
    sample_memory.history_complete = False
    sample_memory.updated_count = 1
    sample_memory.operations = [
        MemoryOperation(
            operation_id="op-1",
            source="cursor",
            action="created",
            request_fingerprint="req-1",
            timestamp="2026-07-14T10:00:00+00:00",
            branch="main",
            commit_sha="abc123",
        )
    ]

    path = Path(
        write_session_memory(str(tmp_path), sample_memory, "2026-07-14", details=details)
    )
    parsed = parse_session_file(path)

    assert parsed.schema_version == 2
    entry = parsed.entries[0]
    assert entry.metadata_complete is True
    assert set(entry.metadata) == {
        "project",
        "tags",
        "category",
        "related_files",
        "section_anchor",
        "created_at",
        "updated_at",
        "status",
        "archived_at",
        "archive_reason",
        "superseded_by",
        "structured_data",
        "confidence",
        "valid_from",
        "valid_until",
        "commit_sha",
        "branch",
        "links",
        "last_verified",
        "creator_source",
        "last_updated_by",
        "contributors",
        "operations",
        "content_fingerprint",
        "history_complete",
        "updated_count",
    }
    expected = replace(
        sample_memory,
        file_path=str(path),
        section_anchor=entry.metadata["section_anchor"],
    )
    assert asdict(entry.to_memory(file_path=str(path))) == asdict(expected)
    assert entry.details == details


def test_schema_v2_metadata_comment_escapes_comment_terminators(
    sample_memory: Memory,
) -> None:
    sample_memory.structured_data = {"constraint": "never emit -- inside comments"}

    rendered = render_session_document(document_with(sample_memory))
    metadata_line = next(
        line
        for line in rendered.splitlines()
        if line.startswith("<!-- echovault-metadata-v2:")
    )

    assert "-- inside" not in metadata_line
    assert "\\u002d\\u002d inside" in metadata_line


def test_schema_v1_remains_readable_but_incomplete(tmp_path: Path) -> None:
    path = tmp_path / "2026-07-14-session.md"
    path.write_text(
        "---\nproject: legacy\ntags: [one]\n---\n\n"
        "# Session\n\n### Old\n**What:** readable\n",
        encoding="utf-8",
    )

    parsed = parse_session_file(path)

    assert parsed.schema_version == 1
    assert parsed.entries[0].what == "readable"
    assert parsed.entries[0].metadata_complete is False
