from __future__ import annotations

import copy
import math
from dataclasses import FrozenInstanceError

import pytest

from memory.merge import (
    MergeContext,
    _ordered_union_with_projection,
    _path_key,
    is_duplicate,
    merge_duplicate,
)
from memory.models import Memory, RawMemoryInput


def test_cross_agent_merge_preserves_creator_and_unions_fields(
    sample_memory: Memory,
) -> None:
    sample_memory.source = "cursor"
    sample_memory.creator_source = "cursor"
    sample_memory.last_updated_by = "cursor"
    sample_memory.contributors = ["cursor"]
    incoming = RawMemoryInput(
        title="Use FastAPI for API endpoints",
        what="Added explicit response models",
        why=None,
        tags=["API", "validation"],
        related_files=["src/api/../api/routes.py"],
        constraints=["Never expose internal models"],
        details="Response models now validate outbound data.",
        source="spoofed",
    )
    context = MergeContext(
        operation_id="op-gemini",
        source="gemini-cli",
        timestamp="2026-07-14T11:00:00+00:00",
        request_fingerprint="req-gemini",
        branch="feature",
        commit_sha="def456",
    )

    merged, details = merge_duplicate(
        sample_memory,
        "Original details",
        incoming,
        context,
    )

    assert merged.id == sample_memory.id
    assert merged.title == sample_memory.title
    assert merged.created_at == sample_memory.created_at
    assert merged.source == merged.creator_source == "cursor"
    assert merged.last_updated_by == "gemini-cli"
    assert merged.contributors == ["cursor", "gemini-cli"]
    assert merged.tags == ["api", "fastapi", "validation"]
    assert merged.related_files == ["/src/api/main.py", "src/api/routes.py"]
    assert merged.structured_data["constraints"] == [
        "Never expose internal models",
    ]
    assert merged.why == sample_memory.why
    assert merged.what == "Added explicit response models"
    assert details is not None
    assert "op-gemini" in details
    assert "Original details" in details
    assert len(merged.operations) == len(sample_memory.operations) + 1


def test_empty_collections_and_null_scalars_do_not_clear(
    sample_memory: Memory,
) -> None:
    incoming = RawMemoryInput(
        title=sample_memory.title,
        what="new",
        tags=[],
        related_files=[],
    )
    context = MergeContext(
        "op-2",
        "codex",
        "2026-07-14T12:00:00+00:00",
        "req-2",
    )

    merged, _ = merge_duplicate(sample_memory, None, incoming, context)

    assert merged.tags == sample_memory.tags
    assert merged.related_files == sample_memory.related_files
    assert merged.why == sample_memory.why


@pytest.mark.parametrize(
    ("top_score", "expected_id"),
    [(7.0, "same-project"), (6.999, None)],
)
def test_duplicate_threshold_and_unicode_casefold_contract(
    top_score: float,
    expected_id: str | None,
) -> None:
    incoming = RawMemoryInput(title=" Straße ", what="routing")
    same_project = [
        {
            "id": "same-project",
            "project": "p--1",
            "title": "STRASSE",
            "score": top_score,
        },
    ]
    normalization_pool = [
        *same_project,
        {
            "id": "foreign",
            "project": "other--2",
            "title": "Straße",
            "score": 10.0,
        },
    ]

    result = is_duplicate(
        incoming,
        "p--1",
        same_project,
        normalization_pool,
    )

    assert (result or {}).get("id") == expected_id


def test_projection_precedes_normalization_and_preserves_first_value() -> None:
    assert _ordered_union_with_projection(
        [" src\\api\\../api/routes.py "],
        ["src/api/routes.py", "src/api/models.py"],
        lambda value: value.casefold(),
        _path_key,
    ) == ["src/api/routes.py", "src/api/models.py"]


def test_only_highest_same_project_fts_row_can_match() -> None:
    incoming = RawMemoryInput(title="Exact title", what="body")
    candidates = [
        {
            "id": "first",
            "project": "p--1",
            "title": "Different title",
            "score": 10.0,
        },
        {
            "id": "second",
            "project": "p--1",
            "title": "Exact title",
            "score": 9.0,
        },
    ]

    assert is_duplicate(incoming, "p--1", candidates, candidates) is None


def test_equal_score_tie_preserves_fts_order() -> None:
    incoming = RawMemoryInput(title="Exact title", what="body")
    candidates = [
        {
            "id": "first",
            "project": "p--1",
            "title": "Exact title",
            "score": 10.0,
        },
        {
            "id": "second",
            "project": "p--1",
            "title": "Exact title",
            "score": 10.0,
        },
    ]

    result = is_duplicate(incoming, "p--1", candidates, candidates)

    assert result is not None
    assert result["id"] == "first"


@pytest.mark.parametrize("score", [math.nan, math.inf, -math.inf])
def test_non_finite_normalization_scores_are_rejected(score: float) -> None:
    incoming = RawMemoryInput(title="Exact title", what="body")
    candidates = [
        {
            "id": "same-project",
            "project": "p--1",
            "title": "Exact title",
            "score": 10.0,
        },
    ]
    normalization_pool = [
        *candidates,
        {
            "id": "foreign",
            "project": "other--2",
            "title": "Exact title",
            "score": score,
        },
    ]

    assert is_duplicate(
        incoming,
        "p--1",
        candidates,
        normalization_pool,
    ) is None


@pytest.mark.parametrize(
    ("top_score", "pool_score"),
    [(0.0, 0.0), (-1.0, -2.0), (-1.0, 1.0)],
)
def test_zero_and_negative_scores_cannot_match(
    top_score: float,
    pool_score: float,
) -> None:
    incoming = RawMemoryInput(title="Exact title", what="body")
    candidates = [
        {
            "id": "same-project",
            "project": "p--1",
            "title": "Exact title",
            "score": top_score,
        },
    ]
    normalization_pool = [
        *candidates,
        {
            "id": "foreign",
            "project": "other--2",
            "title": "Exact title",
            "score": pool_score,
        },
    ]

    assert is_duplicate(
        incoming,
        "p--1",
        candidates,
        normalization_pool,
    ) is None


def test_foreign_project_row_is_never_selectable() -> None:
    incoming = RawMemoryInput(title="Exact title", what="body")
    candidates = [
        {
            "id": "foreign",
            "project": "other--2",
            "title": "Exact title",
            "score": 10.0,
        },
    ]

    assert is_duplicate(incoming, "p--1", candidates, candidates) is None


def test_merge_does_not_mutate_either_input(sample_memory: Memory) -> None:
    sample_memory.structured_data = {
        "constraints": ["Keep the original"],
    }
    incoming = RawMemoryInput(
        title=sample_memory.title,
        what="Updated body",
        tags=["new"],
        constraints=["Add the new constraint"],
        source="spoofed",
    )
    original_memory = copy.deepcopy(sample_memory)
    original_incoming = copy.deepcopy(incoming)
    context = MergeContext(
        "op-immutable",
        "codex",
        "2026-07-14T12:30:00+00:00",
        "req-immutable",
    )

    merged, _ = merge_duplicate(sample_memory, "details", incoming, context)

    assert sample_memory == original_memory
    assert incoming == original_incoming
    assert merged is not sample_memory
    assert merged.tags is not sample_memory.tags
    assert merged.structured_data is not sample_memory.structured_data


def test_context_is_authoritative_for_contributor_and_operation(
    sample_memory: Memory,
) -> None:
    sample_memory.contributors = ["cursor", "codex"]
    incoming = RawMemoryInput(
        title=sample_memory.title,
        what="Updated body",
        source="spoofed",
    )
    context = MergeContext(
        operation_id="op-authoritative",
        source="codex",
        timestamp="2026-07-14T13:00:00+00:00",
        request_fingerprint="req-authoritative",
        branch="feat/merge",
        commit_sha="abc123",
    )

    merged, _ = merge_duplicate(sample_memory, None, incoming, context)

    assert merged.source == "cursor"
    assert merged.creator_source == "cursor"
    assert merged.last_updated_by == "codex"
    assert merged.contributors == ["cursor", "codex"]
    assert merged.updated_at == context.timestamp
    assert merged.updated_count == sample_memory.updated_count + 1
    assert len(merged.operations) == len(sample_memory.operations) + 1
    operation = merged.operations[-1]
    assert operation.operation_id == "op-authoritative"
    assert operation.source == "codex"
    assert operation.action == "updated"
    assert operation.request_fingerprint == "req-authoritative"
    assert operation.timestamp == "2026-07-14T13:00:00+00:00"
    assert operation.branch == "feat/merge"
    assert operation.commit_sha == "abc123"

    with pytest.raises(FrozenInstanceError):
        operation.action = "created"
