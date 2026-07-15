from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from memory.config import resolve_context_mode
from memory.context_pack import build_context_pack, render_gemini_additional_context
from memory.core import MemoryService
from memory.projects import (
    ProjectRegistry,
    ProjectScope,
    build_project_identity,
    discover_project_root,
)


class HookInputError(ValueError):
    pass


@dataclass(frozen=True)
class GeminiBeforeAgentInput:
    session_id: str
    cwd: Path
    hook_event_name: str
    timestamp: str
    prompt: str


class ClaimStore(Protocol):
    def try_claim(
        self,
        event: GeminiBeforeAgentInput,
        *,
        now=None,
    ) -> bool: ...


class _AlwaysClaim:
    def try_claim(
        self,
        event: GeminiBeforeAgentInput,
        *,
        now=None,
    ) -> bool:
        _ = event, now
        return True


def parse_before_agent(payload: Mapping[str, object]) -> GeminiBeforeAgentInput:
    required = ("session_id", "cwd", "hook_event_name", "timestamp", "prompt")
    values = {name: payload.get(name) for name in required}
    if any(
        not isinstance(value, str) or not value.strip()
        for value in values.values()
    ):
        raise HookInputError("Missing required BeforeAgent field")
    if values["hook_event_name"] != "BeforeAgent":
        raise HookInputError("Unexpected hook event")
    session_id = values["session_id"]
    cwd = values["cwd"]
    hook_event_name = values["hook_event_name"]
    timestamp = values["timestamp"]
    prompt = values["prompt"]
    assert isinstance(session_id, str)
    assert isinstance(cwd, str)
    assert isinstance(hook_event_name, str)
    assert isinstance(timestamp, str)
    assert isinstance(prompt, str)
    return GeminiBeforeAgentInput(
        session_id=session_id,
        cwd=Path(cwd),
        hook_event_name=hook_event_name,
        timestamp=timestamp,
        prompt=prompt,
    )


def before_agent_success(additional_context: str) -> dict[str, object]:
    if not additional_context.strip():
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "BeforeAgent",
            "additionalContext": additional_context,
        }
    }


def _project_scope(service: MemoryService, cwd: Path) -> ProjectScope | str:
    root, marker = discover_project_root(cwd)
    identity = build_project_identity(root, marker)
    registry = ProjectRegistry(Path(service.memory_home))
    resolved = registry.resolve(identity.key)
    if resolved is not None:
        return resolved
    vault = Path(service.memory_home) / "vault"
    legacy_candidates = (
        f"{root.parent.name}-{root.name}",
        root.name,
    )
    for candidate in legacy_candidates:
        if (vault / candidate).is_dir():
            return candidate
    return ProjectScope(identity=identity)


def _error(code: str) -> None:
    sys.stderr.write(f"echovault_gemini_hook:{code}\n")


def process_before_agent(
    payload: Mapping[str, object],
    *,
    service_factory: Callable[[], MemoryService] | None = None,
    claim_store: ClaimStore | None = None,
) -> dict[str, object]:
    """Retrieve prompt-relevant context and fail open on recoverable errors."""

    try:
        event = parse_before_agent(payload)
    except HookInputError:
        _error("invalid_input")
        return {}
    factory = service_factory or MemoryService
    service = factory()
    owns_service = service_factory is None
    try:
        mode, _source = resolve_context_mode(service.config, "gemini-cli")
        if mode == "off":
            return {}
        project = _project_scope(service, event.cwd)
        results, total = service.get_context(
            project=project,
            query=event.prompt,
            agent="gemini-cli",
            token_budget=1200,
            record_feedback=False,
        )
        pack = build_context_pack(results, total=total)
        additional_context = render_gemini_additional_context(pack)
        if not additional_context:
            return {}
        selected_store = claim_store or _AlwaysClaim()
        if not selected_store.try_claim(event):
            return {}
        packed_memories = pack.get("memories")
        memory_ids = (
            [
                str(item["id"])
                for item in packed_memories
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ]
            if isinstance(packed_memories, list)
            else []
        )
        if memory_ids:
            service.db.record_feedback(memory_ids)
        return before_agent_success(additional_context)
    except Exception:
        _error("retrieval_failed")
        return {}
    finally:
        if owns_service:
            service.close()
