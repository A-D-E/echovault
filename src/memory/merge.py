from __future__ import annotations

import copy
import math
import posixpath
from dataclasses import dataclass
from typing import Callable

from memory.models import Memory, MemoryOperation, RawMemoryInput


@dataclass(frozen=True)
class MergeContext:
    operation_id: str
    source: str | None
    timestamp: str
    request_fingerprint: str
    branch: str | None = None
    commit_sha: str | None = None


def _ordered_union_with_projection(
    existing: list[str],
    incoming: list[str],
    normalizer: Callable[[str], str],
    projector: Callable[[str], str],
) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw_value in (*existing, *incoming):
        projected = projector(raw_value)
        if not projected:
            continue
        key = normalizer(projected)
        if key in seen:
            continue
        result.append(projected)
        seen.add(key)
    return result


def _ordered_union(
    existing: list[str],
    incoming: list[str],
    normalizer: Callable[[str], str],
) -> list[str]:
    return _ordered_union_with_projection(
        existing,
        incoming,
        normalizer,
        lambda value: value.strip(),
    )


def _path_key(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        return ""
    return posixpath.normpath(stripped.replace("\\", "/"))


def _is_valid_fts_row(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    if not all(
        isinstance(item.get(field), str)
        for field in ("id", "project", "title")
    ):
        return False
    score = item.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return False
    try:
        return math.isfinite(score)
    except OverflowError:
        return False


def _require_string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise ValueError(f"structured_data.{field} must be a list[str]")
    return value


def is_duplicate(
    incoming: RawMemoryInput,
    project: str,
    candidates: list[dict],
    normalization_pool: list[dict],
) -> dict | None:
    if not all(
        _is_valid_fts_row(item)
        for item in (*candidates, *normalization_pool)
    ):
        return None

    same_project = [item for item in candidates if item["project"] == project]
    if not same_project:
        return None

    top = same_project[0]
    raw_scores = [float(item["score"]) for item in normalization_pool]
    if not raw_scores or any(not math.isfinite(score) for score in raw_scores):
        return None

    top_score = float(top["score"])
    if not math.isfinite(top_score):
        return None

    scores = [max(0.0, score) for score in raw_scores]
    maximum = max(scores, default=0.0)
    normalized = max(0.0, top_score) / maximum if maximum > 0.0 else 0.0
    title_matches = (
        incoming.title.strip().casefold()
        == top["title"].strip().casefold()
    )
    return top if normalized >= 0.7 and title_matches else None


def merge_duplicate(
    existing: Memory,
    existing_details: str | None,
    incoming: RawMemoryInput,
    context: MergeContext,
) -> tuple[Memory, str | None]:
    merged = copy.deepcopy(existing)
    scalar_fields = (
        "what",
        "why",
        "impact",
        "category",
        "confidence",
        "valid_from",
        "valid_until",
        "commit_sha",
        "branch",
        "last_verified",
    )
    for name in scalar_fields:
        value = getattr(incoming, name)
        if value is not None:
            setattr(merged, name, value)

    merged.tags = _ordered_union(
        merged.tags,
        incoming.tags,
        lambda value: value.casefold(),
    )
    merged.related_files = _ordered_union_with_projection(
        merged.related_files,
        incoming.related_files,
        _path_key,
        _path_key,
    )
    merged.links = _ordered_union(
        merged.links,
        incoming.links,
        lambda value: value,
    )

    structured = copy.deepcopy(merged.structured_data)
    structured_fields = (
        "triggers",
        "prerequisites",
        "steps",
        "verification",
        "follow_ups",
        "constraints",
        "alternatives_rejected",
        "open_questions",
    )
    for name in structured_fields:
        existing_values = _require_string_list(
            structured.get(name, []),
            name,
        )
        structured[name] = _ordered_union(
            existing_values,
            getattr(incoming, name),
            lambda value: value,
        )
    merged.structured_data = structured

    merged.updated_at = context.timestamp
    merged.last_updated_by = context.source
    contributors = [
        *merged.contributors,
        *([context.source] if context.source else []),
    ]
    merged.contributors = list(dict.fromkeys(contributors))
    merged.updated_count += 1
    merged.operations.append(
        MemoryOperation(
            operation_id=context.operation_id,
            source=context.source,
            action="updated",
            request_fingerprint=context.request_fingerprint,
            timestamp=context.timestamp,
            branch=context.branch,
            commit_sha=context.commit_sha,
        )
    )

    details = existing_details
    if incoming.details:
        header = (
            f"--- update {context.timestamp} by "
            f'{context.source or "unknown"} op {context.operation_id} ---'
        )
        details = (
            f"{existing_details}\n\n{header}\n\n{incoming.details}"
            if existing_details
            else f"{header}\n\n{incoming.details}"
        )
    return merged, details
