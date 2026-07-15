from __future__ import annotations

from pathlib import Path

import pytest

from memory.core import MemoryService
from memory.integrations.gemini_hook import (
    GeminiBeforeAgentInput,
    parse_before_agent,
    process_before_agent,
)
from tests.gemini_helpers import VALID_EVENT, seeded_service_factory


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
