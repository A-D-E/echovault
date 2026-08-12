from __future__ import annotations

import re
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from memory.models import Memory, MemoryDetail, MemoryOperation, RawMemoryInput, SearchResult


def test_raw_memory_input_with_required_fields_only():
    """Test that RawMemoryInput works with only required fields (title, what)."""
    raw = RawMemoryInput(title="Test Title", what="Test what")

    assert raw.title == "Test Title"
    assert raw.what == "Test what"
    assert raw.why is None
    assert raw.impact is None
    assert raw.tags == []
    assert raw.category is None
    assert raw.related_files == []
    assert raw.details is None
    assert raw.source is None


def test_memory_from_raw_generates_uuid():
    """Test that Memory.from_raw generates a UUID id (36 chars)."""
    raw = RawMemoryInput(title="Test Title", what="Test what")
    memory = Memory.from_raw(raw, project="test-project")

    # UUID format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx (36 chars)
    assert len(memory.id) == 36
    assert memory.id.count("-") == 4
    # Verify it's a valid UUID format
    uuid_pattern = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    assert re.match(uuid_pattern, memory.id)


def test_memory_from_raw_generates_section_anchor():
    """Test that Memory.from_raw generates correct section_anchor from title."""
    test_cases = [
        ("Simple Title", "simple-title"),
        ("Title With  Multiple   Spaces", "title-with-multiple-spaces"),
        ("Title-With-Dashes", "title-with-dashes"),
        ("Title_With_Underscores", "title-with-underscores"),
        ("Title!@#$%^&*()Special", "title-special"),
        ("UPPERCASE TITLE", "uppercase-title"),
        ("  Leading and Trailing  ", "leading-and-trailing"),
    ]

    for title, expected_anchor in test_cases:
        raw = RawMemoryInput(title=title, what="Test what")
        memory = Memory.from_raw(raw, project="test-project")
        assert memory.section_anchor == expected_anchor, f"Failed for title: {title}"


def test_search_result_has_details_flag():
    """Test that SearchResult has_details flag works."""
    result = SearchResult(
        id="test-id",
        title="Test Title",
        what="Test what",
        why=None,
        impact=None,
        category="decision",
        tags=["test"],
        project="test-project",
        source=None,
        score=0.95,
        has_details=True,
        file_path="/path/to/file.md",
        created_at="2025-01-30T12:00:00+00:00",
    )

    assert result.has_details is True

    result_no_details = SearchResult(
        id="test-id-2",
        title="Test Title 2",
        what="Test what 2",
        why=None,
        impact=None,
        category="pattern",
        tags=[],
        project="test-project",
        source=None,
        score=0.85,
        has_details=False,
        file_path="/path/to/file2.md",
        created_at="2025-01-30T12:00:00+00:00",
    )

    assert result_no_details.has_details is False


def test_memory_from_raw_sets_project_and_created_at():
    """Test that Memory.from_raw sets project and created_at correctly."""
    before = datetime.now(timezone.utc)
    raw = RawMemoryInput(title="Test Title", what="Test what")
    memory = Memory.from_raw(raw, project="my-awesome-project")
    after = datetime.now(timezone.utc)

    assert memory.project == "my-awesome-project"
    assert memory.created_at is not None
    assert memory.updated_at is not None
    assert memory.created_at == memory.updated_at

    # Verify created_at is a valid ISO format timestamp
    created_dt = datetime.fromisoformat(memory.created_at)
    assert before <= created_dt <= after

    # Verify timezone is UTC
    assert created_dt.tzinfo is not None


def test_memory_from_raw_preserves_all_fields():
    """Test that Memory.from_raw preserves all fields from RawMemoryInput."""
    raw = RawMemoryInput(
        title="Complete Memory",
        what="What happened",
        why="Why it matters",
        impact="Impact details",
        tags=["tag1", "tag2", "tag3"],
        category="decision",
        related_files=["file1.py", "file2.py"],
        details="Full details here",
        source="user",
    )

    memory = Memory.from_raw(raw, project="test-project", file_path="/path/to/session.md")

    assert memory.title == "Complete Memory"
    assert memory.what == "What happened"
    assert memory.why == "Why it matters"
    assert memory.impact == "Impact details"
    assert memory.tags == ["tag1", "tag2", "tag3"]
    assert memory.category == "decision"
    assert memory.related_files == ["file1.py", "file2.py"]
    assert memory.source == "user"
    assert memory.project == "test-project"
    assert memory.file_path == "/path/to/session.md"


def test_memory_from_raw_initializes_provenance_defaults():
    raw = RawMemoryInput(title="Provenance", what="Track the creator", source="cursor")

    memory = Memory.from_raw(raw, project="test-project")

    assert memory.creator_source == "cursor"
    assert memory.last_updated_by == "cursor"
    assert memory.contributors == ["cursor"]
    assert memory.operations == []
    assert memory.content_fingerprint is None
    assert memory.history_complete is True
    assert memory.updated_count == 0


def test_memory_operation_is_immutable():
    operation = MemoryOperation(
        operation_id="op-1",
        source="cursor",
        action="created",
        request_fingerprint="request-1",
        timestamp="2026-07-14T10:00:00+00:00",
    )

    with pytest.raises(FrozenInstanceError):
        operation.action = "updated"


def test_memory_detail():
    """Test MemoryDetail dataclass."""
    detail = MemoryDetail(
        memory_id="test-memory-id",
        body="Detailed information about the memory",
    )

    assert detail.memory_id == "test-memory-id"
    assert detail.body == "Detailed information about the memory"
