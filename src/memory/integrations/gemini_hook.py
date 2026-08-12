from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import queue
import stat
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from memory.config import get_memory_home, resolve_context_mode
from memory.context_pack import build_context_pack, render_gemini_additional_context
from memory.core import MemoryService
from memory.projects import (
    ProjectRegistry,
    ProjectScope,
    build_project_identity,
    discover_project_root,
)
from memory.safe_io import ProcessFileLock, fsync_directory


INTEGRATION_VERSION = "0.6.0"


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


def hook_event_digest(event: GeminiBeforeAgentInput) -> str:
    identity = {
        "session_id": event.session_id,
        "hook_event_name": event.hook_event_name,
        "timestamp": event.timestamp,
    }
    encoded = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class HookClaimStore:
    def __init__(self, memory_home: Path, ttl_seconds: int = 600) -> None:
        if ttl_seconds <= 0:
            raise ValueError("claim TTL must be positive")
        self.memory_home = memory_home.expanduser().resolve()
        self.root = self.memory_home / "hook-events"
        self.lock_path = self.root / ".lock"
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def _utc(value: datetime | None) -> datetime:
        selected = value or datetime.now(timezone.utc)
        if selected.tzinfo is None:
            return selected.replace(tzinfo=timezone.utc)
        return selected.astimezone(timezone.utc)

    def _prepare_root(self) -> None:
        if os.path.lexists(self.root) and self.root.is_symlink():
            raise OSError("hook claim directory cannot be a symlink")
        self.root.mkdir(parents=True, exist_ok=True)
        metadata = self.root.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise OSError("hook claim path must be a directory")

    def _clean_expired(self, now: datetime) -> None:
        for path in self.root.glob("*.json"):
            try:
                metadata = path.lstat()
                if not stat.S_ISREG(metadata.st_mode):
                    continue
                payload = json.loads(path.read_text(encoding="utf-8"))
                expires_at = datetime.fromisoformat(payload["expires_at"])
                if self._utc(expires_at) <= now:
                    path.unlink()
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue

    def try_claim(
        self,
        event: GeminiBeforeAgentInput,
        *,
        now: datetime | None = None,
    ) -> bool:
        selected_now = self._utc(now)
        digest = hook_event_digest(event)
        self._prepare_root()
        with ProcessFileLock(self.lock_path):
            self._clean_expired(selected_now)
            path = self.root / f"{digest}.json"
            payload = (
                json.dumps(
                    {
                        "digest": digest,
                        "integration_version": INTEGRATION_VERSION,
                        "expires_at": (
                            selected_now + timedelta(seconds=self.ttl_seconds)
                        ).isoformat(),
                    },
                    sort_keys=True,
                    indent=2,
                )
                + "\n"
            ).encode("utf-8")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(path, flags, 0o600)
            except FileExistsError:
                return False
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            fsync_directory(self.root)
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


def _spawn_before_agent_worker(
    request: dict[str, object],
    result_queue,
) -> None:
    response: dict[str, object] = {}
    error_code: str | None = None
    service: MemoryService | None = None
    try:
        memory_home = request.get("memory_home")
        payload = request.get("event")
        if not isinstance(memory_home, str) or not isinstance(payload, dict):
            raise HookInputError("Invalid worker request")
        service = MemoryService(memory_home)
        response = process_before_agent(
            payload,
            service_factory=lambda: service,
            claim_store=HookClaimStore(Path(memory_home)),
        )
    except Exception:
        error_code = "worker_failed"
    finally:
        if service is not None:
            service.close()
    result_queue.put(
        {
            "ok": error_code is None,
            "response": response,
            "error_code": error_code,
        }
    )


def handle_before_agent(
    payload: Mapping[str, object],
    *,
    memory_home: Path | None = None,
    timeout_seconds: float = 4.0,
    worker_target: Callable[[dict[str, object], object], None] = (
        _spawn_before_agent_worker
    ),
    multiprocessing_context=None,
) -> dict[str, object]:
    """Run the hook in a spawn worker bounded below Gemini's outer timeout."""

    if timeout_seconds <= 0:
        return {}
    try:
        event = parse_before_agent(payload)
    except HookInputError:
        _error("invalid_input")
        return {}
    selected_home = (memory_home or Path(get_memory_home())).expanduser().resolve()
    request: dict[str, object] = {
        "event": {
            "session_id": event.session_id,
            "cwd": str(event.cwd),
            "hook_event_name": event.hook_event_name,
            "timestamp": event.timestamp,
            "prompt": event.prompt,
        },
        "memory_home": str(selected_home),
        "integration_version": INTEGRATION_VERSION,
        "timeout_seconds": timeout_seconds,
    }
    context = multiprocessing_context or multiprocessing.get_context("spawn")
    result_queue = context.Queue(maxsize=1)
    process = context.Process(
        target=worker_target,
        args=(request, result_queue),
    )
    try:
        process.start()
        process.join(timeout_seconds)
        if process.is_alive():
            process.terminate()
            process.join()
            _error("deadline")
            return {}
        try:
            result = result_queue.get(timeout=0.2)
        except queue.Empty:
            _error("missing_result")
            return {}
        if not isinstance(result, dict) or result.get("ok") is not True:
            _error("worker_failed")
            return {}
        response = result.get("response")
        return response if isinstance(response, dict) else {}
    except Exception:
        if process.is_alive():
            process.terminate()
            process.join()
        _error("spawn_failed")
        return {}
    finally:
        result_queue.close()
        result_queue.join_thread()
