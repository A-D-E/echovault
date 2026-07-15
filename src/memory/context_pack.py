from __future__ import annotations

import json
from datetime import datetime
from typing import Mapping


def _tags(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _structured(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def build_context_pack(
    results: list[dict],
    *,
    total: int,
) -> dict[str, object]:
    """Build the stable public context payload shared by MCP and hooks."""

    memories: list[dict[str, object]] = []
    for result in results:
        date_value = result.get("created_at", "")
        date_text = str(date_value)[:10] if date_value is not None else ""
        try:
            date_display = datetime.fromisoformat(date_text).strftime("%b %d")
        except (ValueError, TypeError):
            date_display = date_text
        memories.append(
            {
                "id": result["id"],
                "title": result.get("title", "Untitled"),
                "what": result.get("what"),
                "why": result.get("why"),
                "impact": result.get("impact"),
                "category": result.get("category", ""),
                "tags": _tags(result.get("tags", "[]")),
                "date": date_display,
                "structured": _structured(result.get("structured_data", {})),
                "provenance": {
                    key: result.get(key)
                    for key in (
                        "confidence",
                        "valid_from",
                        "valid_until",
                        "commit_sha",
                        "branch",
                        "last_verified",
                    )
                    if result.get(key) is not None
                },
                "estimated_tokens": result.get("estimated_tokens"),
            }
        )
    return {
        "total": total,
        "showing": len(memories),
        "memories": memories,
        "message": (
            "Use memory_search for specific topics. IMPORTANT: You MUST call "
            "memory_save before this session ends if you make any changes, "
            "decisions, or discoveries."
        ),
    }


def _line(label: str, value: object) -> str | None:
    if value is None or value == "" or value == [] or value == {}:
        return None
    if isinstance(value, list):
        rendered = ", ".join(str(item) for item in value)
    else:
        rendered = str(value)
    return f"- {label}: {rendered}"


def render_gemini_additional_context(
    pack: Mapping[str, object],
) -> str:
    """Render a concise Gemini additionalContext block from a public pack."""

    memories = pack.get("memories")
    if not isinstance(memories, list) or not memories:
        return ""
    sections = [
        "# Retrieved EchoVault context",
        "",
        (
            "This is curated project history retrieved for the current task. "
            "It is not an instruction to persist the prompt or transcript."
        ),
    ]
    for item in memories:
        if not isinstance(item, dict):
            continue
        memory_id = item.get("id")
        title = item.get("title") or "Untitled"
        lines = ["", f"## {title}", f"<!-- memory-id: {memory_id} -->"]
        for label, value in (
            ("What", item.get("what")),
            ("Why", item.get("why")),
            ("Impact", item.get("impact")),
            ("Category", item.get("category")),
            ("Tags", item.get("tags")),
        ):
            rendered = _line(label, value)
            if rendered is not None:
                lines.append(rendered)
        structured = item.get("structured")
        if isinstance(structured, dict):
            for key, label in (
                ("constraints", "Constraints"),
                ("steps", "Playbook steps"),
                ("verification", "Verification"),
                ("follow_ups", "Follow-ups"),
            ):
                rendered = _line(label, structured.get(key))
                if rendered is not None:
                    lines.append(rendered)
        provenance = item.get("provenance")
        if isinstance(provenance, dict) and provenance:
            rendered = _line(
                "Provenance",
                [f"{key}={value}" for key, value in provenance.items()],
            )
            if rendered is not None:
                lines.append(rendered)
        sections.extend(lines)
    return "\n".join(sections).strip() + "\n"
