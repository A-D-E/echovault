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
    _entry_from_memory,
    parse_session_file,
    render_entry,
    render_section,
    render_session_document,
    write_session_document,
    write_session_memory,
)
from memory.models import CATEGORY_HEADINGS, Memory, MemoryOperation


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
        assert '**What:** "Implemented REST API using FastAPI framework"' in result
        assert '**Why:** "FastAPI provides automatic validation and documentation"' in result
        assert '**Impact:** "Reduces boilerplate code by 40%"' in result
        assert '**Source:** "claude-code"' in result
        assert "**Details:** null" in result
        assert "<details>" not in result

    def test_render_section_with_details(self, sample_memory: Memory) -> None:
        """Test rendering details as a JSON-encoded readable record."""
        details = "Here is the full implementation:\n\n```python\nfrom fastapi import FastAPI\n```"
        result = render_section(sample_memory, details=details)

        assert "### Use FastAPI for API endpoints" in result
        assert (
            '**Details:** "Here is the full implementation:\\n\\n```python\\n'
            'from fastapi import FastAPI\\n```"'
        ) in result
        assert "<details>" not in result

    def test_render_section_without_optional_fields(self, minimal_memory: Memory) -> None:
        """Test rendering explicit JSON nulls for optional v2 fields."""
        result = render_section(minimal_memory)

        assert "### Basic memory" in result
        assert '**What:** "Simple memory entry"' in result
        assert "**Why:** null" in result
        assert "**Impact:** null" in result
        assert "**Source:** null" in result
        assert "**Details:** null" in result
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
        assert '**What:** "Implemented REST API using FastAPI framework"' in content

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
        """Test writing memory with an encoded details record."""
        details = "Full implementation details here"
        file_path = write_session_memory(temp_vault, sample_memory, "2026-01-22", details=details)

        content = Path(file_path).read_text()

        assert '**Details:** "Full implementation details here"' in content
        assert "<details>" not in content

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

    def test_schema_v1_append_fails_without_changing_bytes(self, temp_vault: str) -> None:
        """A legacy file must be explicitly migrated before appending."""
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
        original_bytes = file_path.read_bytes()

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

        with pytest.raises(ValueError, match=r"memory migrate vault-metadata"):
            write_session_memory(temp_vault, memory, "2026-01-22")

        assert file_path.read_bytes() == original_bytes


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


def test_write_session_document_rejects_schema_v1_without_touching_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.md"
    original_bytes = b"legacy bytes\n"
    path.write_bytes(original_bytes)
    document = SessionDocument(
        project="legacy",
        created=None,
        tags=[],
        sources=[],
        title="Legacy",
        entries=[],
        schema_version=1,
    )

    with pytest.raises(ValueError, match=r"memory migrate vault-metadata"):
        write_session_document(path, document)

    assert path.read_bytes() == original_bytes


def test_schema_v2_multiline_readable_fields_round_trip_exactly(
    tmp_path: Path, sample_memory: Memory
) -> None:
    sample_memory.title = "Title first line\n\n### title-looking heading"
    sample_memory.what = (
        "What first line\n\n## what heading\n### what subheading\n**Why:** still what"
    )
    sample_memory.why = "Why first line\n\n### why heading"
    sample_memory.impact = "Impact first line\n  indented impact\n## impact heading"
    sample_memory.source = "cursor\n\n### source heading with trailing spaces  "
    document = document_with(sample_memory)
    path = tmp_path / "multiline.md"
    path.write_text(render_session_document(document), encoding="utf-8")

    entry = parse_session_file(path).entries[0]

    assert entry.title == sample_memory.title
    assert entry.what == sample_memory.what
    assert entry.why == sample_memory.why
    assert entry.impact == sample_memory.impact
    assert entry.source == sample_memory.source


def test_schema_v2_details_round_trip_headings_and_whitespace_exactly(
    tmp_path: Path, sample_memory: Memory
) -> None:
    details = (
        "\n  meaningful leading whitespace\n\n"
        "## Details heading\n"
        "### Looks like a memory but has no stable ID\n"
        "**What:** remains detail content\n\n"
        "meaningful trailing whitespace  \n"
    )
    document = document_with(sample_memory)
    document.entries[0].details = details
    second_memory = replace(
        sample_memory,
        id="test-456",
        title="Actual second entry",
        section_anchor="actual-second-entry",
    )
    document.entries.append(document_with(second_memory).entries[0])
    path = tmp_path / "details.md"
    path.write_text(render_session_document(document), encoding="utf-8")

    parsed = parse_session_file(path)

    assert [entry.id for entry in parsed.entries] == ["test-123", "test-456"]
    assert parsed.entries[0].details == details
    assert parsed.entries[1].title == "Actual second entry"


@pytest.mark.parametrize(
    "corruption",
    [
        "duplicate_id",
        "duplicate_metadata",
        "metadata_before_id",
        "metadata_nonadjacent",
        "stray_id",
        "stray_metadata",
    ],
)
def test_schema_v2_rejects_malformed_placement(
    tmp_path: Path, sample_memory: Memory, corruption: str
) -> None:
    lines = render_session_document(document_with(sample_memory)).splitlines()
    id_index = next(i for i, line in enumerate(lines) if line.startswith("<!-- memory-id:"))
    metadata_index = next(
        i for i, line in enumerate(lines) if line.startswith("<!-- echovault-metadata-v2:")
    )
    id_line = lines[id_index]
    metadata_line = lines[metadata_index]

    if corruption == "duplicate_id":
        lines.insert(id_index + 1, id_line)
    elif corruption == "duplicate_metadata":
        lines.insert(metadata_index + 1, metadata_line)
    elif corruption == "metadata_before_id":
        lines[id_index], lines[metadata_index] = metadata_line, id_line
    elif corruption == "metadata_nonadjacent":
        lines.insert(metadata_index, "")
    elif corruption == "stray_id":
        lines.append("<!-- memory-id: stray -->")
    elif corruption == "stray_metadata":
        lines.append(metadata_line)

    path = tmp_path / f"{corruption}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ValueError):
        parse_session_file(path)


@pytest.mark.parametrize(
    "corruption",
    [
        "missing_what",
        "missing_why",
        "missing_impact",
        "missing_source",
        "duplicate_what",
        "duplicate_source",
    ],
)
def test_schema_v2_rejects_missing_or_duplicate_readable_fields(
    tmp_path: Path, sample_memory: Memory, corruption: str
) -> None:
    lines = render_session_document(document_with(sample_memory)).splitlines()
    what_index = next(i for i, line in enumerate(lines) if line.startswith("**What:**"))
    why_index = next(i for i, line in enumerate(lines) if line.startswith("**Why:**"))
    impact_index = next(i for i, line in enumerate(lines) if line.startswith("**Impact:**"))
    source_index = next(i for i, line in enumerate(lines) if line.startswith("**Source:**"))

    if corruption == "missing_what":
        lines.pop(what_index)
    elif corruption == "missing_why":
        lines.pop(why_index)
    elif corruption == "missing_impact":
        lines.pop(impact_index)
    elif corruption == "missing_source":
        lines.pop(source_index)
    elif corruption == "duplicate_what":
        lines.insert(what_index + 1, lines[what_index])
    elif corruption == "duplicate_source":
        lines.insert(source_index + 1, lines[source_index])

    path = tmp_path / f"{corruption}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ValueError):
        parse_session_file(path)


def test_schema_v2_distinguishes_optional_null_from_empty_string(
    tmp_path: Path, minimal_memory: Memory
) -> None:
    minimal_memory.why = None
    minimal_memory.impact = ""
    minimal_memory.source = None
    path = tmp_path / "optional-fields.md"
    path.write_text(
        render_session_document(document_with(minimal_memory)),
        encoding="utf-8",
    )

    entry = parse_session_file(path).entries[0]

    assert entry.why is None
    assert entry.impact == ""
    assert entry.source is None


@pytest.mark.parametrize(
    "structured_data_json",
    [
        '{"nested":{"value":NaN}}',
        '{"nested":[Infinity]}',
        '{"nested":{"values":[0,-Infinity]}}',
    ],
)
def test_schema_v2_rejects_non_finite_json_at_any_depth(
    tmp_path: Path, sample_memory: Memory, structured_data_json: str
) -> None:
    rendered = render_session_document(document_with(sample_memory))
    corrupted = rendered.replace(
        '"structured_data":{}',
        f'"structured_data":{structured_data_json}',
        1,
    )
    assert corrupted != rendered
    path = tmp_path / "non-finite.md"
    path.write_text(corrupted, encoding="utf-8")

    with pytest.raises(ValueError):
        parse_session_file(path)


def test_schema_v2_deep_copies_nested_structured_data(sample_memory: Memory) -> None:
    sample_memory.structured_data = {
        "nested": {"items": [{"value": "original"}]},
    }

    entry = _entry_from_memory(sample_memory)
    entry.metadata["structured_data"]["nested"]["items"][0]["value"] = "entry"
    assert sample_memory.structured_data["nested"]["items"][0]["value"] == "original"

    entry = _entry_from_memory(sample_memory)
    sample_memory.structured_data["nested"]["items"].append({"value": "memory"})
    assert entry.metadata["structured_data"] == {
        "nested": {"items": [{"value": "original"}]},
    }

    rebuilt = entry.to_memory(file_path="session.md")
    rebuilt.structured_data["nested"]["items"][0]["value"] = "rebuilt"
    assert entry.metadata["structured_data"]["nested"]["items"][0]["value"] == "original"


def test_legacy_render_entry_behavior_remains_available() -> None:
    entry = SessionEntry(
        id="legacy-1",
        title="Legacy entry",
        what="Readable legacy value",
        why=None,
        impact=None,
        source=None,
        details=None,
        category="context",
    )

    rendered = render_entry(entry)

    assert rendered.startswith("### Legacy entry\n<!-- memory-id: legacy-1 -->")
    assert "**What:** Readable legacy value" in rendered
    assert "echovault-metadata-v2" not in rendered


def test_schema_v2_rejects_entry_heading_without_id_and_metadata(
    tmp_path: Path, sample_memory: Memory
) -> None:
    lines = render_session_document(document_with(sample_memory)).splitlines()
    id_index = next(i for i, line in enumerate(lines) if line.startswith("<!-- memory-id:"))
    lines.pop(id_index)
    lines.pop(id_index)
    path = tmp_path / "missing-entry-comments.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ValueError):
        parse_session_file(path)


@pytest.mark.parametrize("details", ["", "\r", "\r\n", "\n\n", "  leading\ntrailing  "])
def test_schema_v2_details_json_record_round_trips_exact_values(
    tmp_path: Path, sample_memory: Memory, details: str
) -> None:
    document = document_with(sample_memory)
    document.entries[0].details = details
    path = tmp_path / "details-values.md"
    path.write_text(render_session_document(document), encoding="utf-8")

    assert parse_session_file(path).entries[0].details == details


def test_schema_v2_details_json_record_round_trips_arbitrary_reserved_text(
    tmp_path: Path, sample_memory: Memory
) -> None:
    canonical = render_session_document(document_with(sample_memory))
    metadata_line = next(
        line
        for line in canonical.splitlines()
        if line.startswith("<!-- echovault-metadata-v2:")
    )
    details = (
        "\r\n  leading whitespace\r"
        "<!-- memory-id: literal-detail-id -->\n"
        "<!-- echovault-metadata-v2: {malformed detail json} -->\r\n"
        "### Full canonical-looking entry\n"
        "<!-- memory-id: fake-entry -->\n"
        f"{metadata_line}\n"
        "<details>\n</details>\n"
        "\ntrailing whitespace  \r\n"
    )
    document = document_with(sample_memory)
    document.entries[0].details = details
    path = tmp_path / "arbitrary-details.md"
    path.write_text(render_session_document(document), encoding="utf-8")

    parsed = parse_session_file(path)

    assert len(parsed.entries) == 1
    assert parsed.entries[0].details == details


@pytest.mark.parametrize("corruption", ["duplicate", "malformed"])
def test_schema_v2_rejects_duplicate_or_malformed_details_records(
    tmp_path: Path, sample_memory: Memory, corruption: str
) -> None:
    lines = render_session_document(document_with(sample_memory)).splitlines()
    details_index = next(
        i for i, line in enumerate(lines) if line.startswith("**Details:**")
    )
    if corruption == "duplicate":
        lines.insert(details_index + 1, lines[details_index])
    else:
        lines[details_index] = "**Details:** {not valid json}"
    path = tmp_path / f"details-{corruption}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ValueError):
        parse_session_file(path)


def test_schema_v2_readable_json_records_preserve_control_characters(
    tmp_path: Path, sample_memory: Memory
) -> None:
    sample_memory.title = "title\rwith\r\nlines\\and trailing spaces  "
    sample_memory.what = "what\r\n\rblank\\literal\\n  "
    sample_memory.why = "why\rending"
    sample_memory.impact = "impact\\\r\nnext  "
    sample_memory.source = "cursor\r\nwindows\\path\\"
    document = document_with(sample_memory)
    path = tmp_path / "control-characters.md"
    path.write_text(render_session_document(document), encoding="utf-8")

    entry = parse_session_file(path).entries[0]

    assert entry.title == sample_memory.title
    assert entry.what == sample_memory.what
    assert entry.why == sample_memory.why
    assert entry.impact == sample_memory.impact
    assert entry.source == sample_memory.source


@pytest.mark.parametrize(
    "structured_data",
    [
        {"tuple": (1, 2)},
        {"set": {1, 2}},
        {1: "non-string key"},
        {"nested": [None, True, {"value": float("inf")}]},
    ],
)
def test_schema_v2_rejects_non_json_native_structured_data_before_render(
    sample_memory: Memory, structured_data: dict
) -> None:
    sample_memory.structured_data = structured_data

    with pytest.raises(ValueError):
        render_session_document(document_with(sample_memory))


def test_schema_v2_to_memory_rejects_non_json_native_structured_data(
    sample_memory: Memory,
) -> None:
    entry = _entry_from_memory(sample_memory)
    entry.metadata["structured_data"] = {"tuple": (1, 2)}

    with pytest.raises(ValueError):
        entry.to_memory(file_path="session.md")


@pytest.mark.parametrize("duplicate", ["top_level", "nested"])
def test_schema_v2_rejects_duplicate_json_object_keys(
    tmp_path: Path, sample_memory: Memory, duplicate: str
) -> None:
    sample_memory.structured_data = {"nested": {"value": 1}}
    rendered = render_session_document(document_with(sample_memory))
    if duplicate == "top_level":
        corrupted = rendered.replace(
            '"project":"my-project"',
            '"project":"my-project","project":"other-project"',
            1,
        )
    else:
        corrupted = rendered.replace(
            '"nested":{"value":1}',
            '"nested":{"value":1,"value":2}',
            1,
        )
    assert corrupted != rendered
    path = tmp_path / f"duplicate-{duplicate}.md"
    path.write_text(corrupted, encoding="utf-8")

    with pytest.raises(ValueError):
        parse_session_file(path)


def test_schema_v2_entry_deep_copies_mutable_living_data_links(
    sample_memory: Memory,
) -> None:
    sample_memory.links = ["https://example.test/original"]
    entry = _entry_from_memory(sample_memory)

    sample_memory.links.append("https://example.test/source-mutation")

    assert entry.metadata["links"] == ["https://example.test/original"]
    assert entry.living_data["links"] == ["https://example.test/original"]


def test_schema_v1_details_keep_reserved_v2_comments_literal(tmp_path: Path) -> None:
    path = tmp_path / "legacy-details.md"
    details = (
        "before\n"
        "<!-- memory-id: literal-id -->\n"
        "<!-- echovault-metadata-v2: {not valid json} -->\n"
        '<!-- echovault-metadata-v2: {"valid":"but literal"} -->\n'
        "after"
    )
    path.write_text(
        "---\nproject: legacy\n---\n\n# Legacy\n\n"
        "### Entry\n**What:** readable\n\n<details>\n"
        f"{details}\n"
        "</details>\n",
        encoding="utf-8",
    )

    entry = parse_session_file(path).entries[0]

    assert entry.id is None
    assert entry.details == details


def test_schema_v2_archived_diagnostics_round_trip_only_through_metadata(
    tmp_path: Path, sample_memory: Memory
) -> None:
    sample_memory.status = "archived"
    sample_memory.category = "decision\r\n### category heading"
    sample_memory.archived_at = (
        "2026-07-14T11:30:00+00:00\r\n<!-- memory-id: archived-at -->"
    )
    sample_memory.archive_reason = (
        "superseded  \\ path\n"
        "<!-- echovault-metadata-v2: {not structure} -->"
    )
    sample_memory.superseded_by = "memory-2\r### superseded heading  "
    document = document_with(sample_memory)
    path = tmp_path / "archived-diagnostics.md"
    rendered = render_session_document(document)
    path.write_text(rendered, encoding="utf-8")

    entry = parse_session_file(path).entries[0]
    rebuilt = entry.to_memory(file_path=str(path))

    assert rebuilt.category == sample_memory.category
    assert rebuilt.archived_at == sample_memory.archived_at
    assert rebuilt.archive_reason == sample_memory.archive_reason
    assert rebuilt.superseded_by == sample_memory.superseded_by
    assert "**Category:**" not in rendered
    assert "**Archived:**" not in rendered
    assert "**Archive Reason:**" not in rendered
    assert "**Superseded By:**" not in rendered


@pytest.mark.parametrize(
    "noncanonical_line",
    [
        "unexpected body text",
        "  **Details:** null",
        " **Title:** \"indented lookalike\"",
        "<!-- echovault-null-v2: Why -->",
        "**What+:** \"obsolete continuation\"",
        "<details>",
        "</details>",
        "**Category:** decision",
    ],
)
def test_schema_v2_rejects_noncanonical_body_lines(
    tmp_path: Path, sample_memory: Memory, noncanonical_line: str
) -> None:
    lines = render_session_document(document_with(sample_memory)).splitlines()
    details_index = next(
        i for i, line in enumerate(lines) if line.startswith("**Details:**")
    )
    lines.insert(details_index + 1, noncanonical_line)
    path = tmp_path / "noncanonical.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ValueError):
        parse_session_file(path)


def test_schema_v2_rejects_duplicate_living_memory_records(
    tmp_path: Path, sample_memory: Memory
) -> None:
    lines = render_session_document(document_with(sample_memory)).splitlines()
    details_index = next(
        i for i, line in enumerate(lines) if line.startswith("**Details:**")
    )
    lines[details_index + 1:details_index + 1] = [
        "**Living Memory:** {}",
        "**Living Memory:** {}",
    ]
    path = tmp_path / "duplicate-living.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ValueError):
        parse_session_file(path)


def test_schema_v2_rejects_duplicate_schema_version_frontmatter(
    tmp_path: Path, sample_memory: Memory
) -> None:
    rendered = render_session_document(document_with(sample_memory))
    corrupted = rendered.replace(
        "schema_version: 2",
        "schema_version: 2\nschema_version: 1",
        1,
    )
    path = tmp_path / "duplicate-schema-version.md"
    path.write_text(corrupted, encoding="utf-8")

    with pytest.raises(ValueError):
        parse_session_file(path)


@pytest.mark.parametrize(
    "unexpected_prefix_line",
    [
        "arbitrary prelude text",
        "<!-- echovault-null-v2: Why -->",
        '  **Title:** "indented lookalike"',
        "<details>",
        "</details>",
        '**What:** "readable record before entry"',
        "<!-- memory-id: prefix-id -->",
        "<!-- echovault-metadata-v2: {} -->",
        " ",
    ],
)
def test_schema_v2_rejects_noncanonical_lines_before_first_entry(
    tmp_path: Path, sample_memory: Memory, unexpected_prefix_line: str
) -> None:
    lines = render_session_document(document_with(sample_memory)).splitlines()
    first_entry_index = next(i for i, line in enumerate(lines) if line.startswith("### "))
    lines.insert(first_entry_index, unexpected_prefix_line)
    path = tmp_path / "invalid-prefix.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ValueError):
        parse_session_file(path)


@pytest.mark.parametrize(
    "boundary_heading",
    [*(f"## {heading}" for heading in CATEGORY_HEADINGS.values()), "## Archived"],
)
def test_schema_v2_rejects_boundary_heading_before_records_are_complete(
    tmp_path: Path, sample_memory: Memory, boundary_heading: str
) -> None:
    lines = render_session_document(document_with(sample_memory)).splitlines()
    what_index = next(i for i, line in enumerate(lines) if line.startswith("**What:**"))
    lines.insert(what_index + 1, boundary_heading)
    path = tmp_path / "premature-boundary.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ValueError):
        parse_session_file(path)


def test_schema_v2_accepts_rendered_category_and_archive_transitions(
    tmp_path: Path, sample_memory: Memory
) -> None:
    context_memory = replace(
        sample_memory,
        id="test-context",
        title="Context entry",
        category="context",
        section_anchor="context-entry",
    )
    archived_memory = replace(
        sample_memory,
        id="test-archived",
        title="Archived entry",
        status="archived",
        section_anchor="archived-entry",
    )
    document = document_with(sample_memory)
    document.entries.extend(
        [
            document_with(context_memory).entries[0],
            document_with(archived_memory).entries[0],
        ]
    )
    path = tmp_path / "valid-transitions.md"
    path.write_text(render_session_document(document), encoding="utf-8")

    parsed = parse_session_file(path)

    assert [entry.id for entry in parsed.entries] == [
        sample_memory.id,
        context_memory.id,
        archived_memory.id,
    ]
