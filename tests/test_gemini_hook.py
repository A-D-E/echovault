from __future__ import annotations

import multiprocessing
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memory.core import MemoryService
from memory.integrations.gemini_hook import (
    GeminiBeforeAgentInput,
    HookClaimStore,
    handle_before_agent,
    parse_before_agent,
    process_before_agent,
)
from tests.gemini_helpers import (
    VALID_EVENT,
    retrieved_count,
    seeded_service_factory,
)


_REAL_PATH_OPEN = Path.open


def fail_if_transcript_path(path: Path, *args, **kwargs):
    if path == Path(VALID_EVENT["transcript_path"]):
        raise AssertionError("transcript_path was opened")
    return _REAL_PATH_OPEN(path, *args, **kwargs)


class AlwaysWinsClaimStore:
    def try_claim(
        self,
        event: GeminiBeforeAgentInput,
        *,
        now=None,
    ) -> bool:
        _ = event, now
        return True


def always_wins() -> AlwaysWinsClaimStore:
    return AlwaysWinsClaimStore()


class CountingClaimStore:
    def __init__(self) -> None:
        self.attempts = 0

    def try_claim(
        self,
        event: GeminiBeforeAgentInput,
        *,
        now=None,
    ) -> bool:
        _ = event, now
        self.attempts += 1
        return True


def test_before_agent_returns_curated_context_without_reading_transcript(
    monkeypatch: pytest.MonkeyPatch,
    seeded_service_factory,
) -> None:
    monkeypatch.setattr(Path, "open", fail_if_transcript_path)
    result = process_before_agent(
        VALID_EVENT,
        service_factory=seeded_service_factory,
        claim_store=always_wins(),
    )
    assert result["hookSpecificOutput"]["hookEventName"] == "BeforeAgent"
    additional = result["hookSpecificOutput"]["additionalContext"]
    assert "ALPHA-42" in additional
    assert "memory-id:" in additional


@pytest.mark.parametrize(
    "field",
    ["session_id", "cwd", "hook_event_name", "timestamp", "prompt"],
)
def test_missing_required_hook_field_fails_open(
    field: str,
    seeded_service_factory,
) -> None:
    payload = {key: value for key, value in VALID_EVENT.items() if key != field}
    assert process_before_agent(
        payload,
        service_factory=seeded_service_factory,
    ) == {}


def test_context_mode_off_returns_empty_without_retrieval_claim_or_feedback(
    env_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MemoryService(str(env_home))
    service.config.context.agent_modes["gemini-cli"] = "off"
    claims = CountingClaimStore()
    monkeypatch.setattr(service, "get_context", pytest.fail)
    monkeypatch.setattr(service.db, "record_feedback", pytest.fail)
    try:
        assert process_before_agent(
            VALID_EVENT,
            service_factory=lambda: service,
            claim_store=claims,
        ) == {}
        assert claims.attempts == 0
    finally:
        service.close()


def test_before_agent_parser_ignores_transcript_field() -> None:
    event = parse_before_agent(VALID_EVENT)
    assert not hasattr(event, "transcript_path")


def fixed_now() -> datetime:
    return datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


def slow_spawn_worker(request: dict[str, object], result_queue) -> None:
    _ = request
    time.sleep(1.0)
    result_queue.put({"ok": True, "response": {}})


def test_duplicate_event_injects_and_records_feedback_once(
    env_home: Path,
    seeded_service_factory,
) -> None:
    store = HookClaimStore(env_home)
    first = process_before_agent(
        VALID_EVENT,
        service_factory=seeded_service_factory,
        claim_store=store,
    )
    second = process_before_agent(
        VALID_EVENT,
        service_factory=seeded_service_factory,
        claim_store=store,
    )
    assert first["hookSpecificOutput"]["hookEventName"] == "BeforeAgent"
    assert "additionalContext" in first["hookSpecificOutput"]
    assert second == {}
    assert retrieved_count(seeded_service_factory, "ALPHA-42") == 1


def test_claim_contains_no_prompt_cwd_transcript_context_or_session(
    env_home: Path,
) -> None:
    store = HookClaimStore(env_home)
    store.try_claim(parse_before_agent(VALID_EVENT), now=fixed_now())
    raw = next((env_home / "hook-events").glob("*.json")).read_text()
    for forbidden in (
        "ALPHA-42",
        "/workspace/repo",
        "/must/not-be-read.json",
        "session-1",
        "additionalContext",
    ):
        assert forbidden not in raw


def test_expired_claim_can_be_reclaimed(env_home: Path) -> None:
    store = HookClaimStore(env_home, ttl_seconds=600)
    event = parse_before_agent(VALID_EVENT)
    assert store.try_claim(event, now=fixed_now()) is True
    assert store.try_claim(event, now=fixed_now() + timedelta(seconds=599)) is False
    assert store.try_claim(event, now=fixed_now() + timedelta(seconds=601)) is True


def test_internal_deadline_returns_empty_json(env_home: Path) -> None:
    assert handle_before_agent(
        VALID_EVENT,
        memory_home=env_home,
        timeout_seconds=0.05,
        worker_target=slow_spawn_worker,
    ) == {}


def test_spawn_context_accepts_only_serializable_request_and_result(
    env_home: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    response = handle_before_agent(
        VALID_EVENT,
        memory_home=env_home,
        timeout_seconds=2.0,
        multiprocessing_context=context,
    )
    assert isinstance(response, dict)
