from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path

import pytest

from memory.core import MemoryService
from memory.embeddings.base import EmbeddingProvider
from memory.health import doctor
from memory.models import RawMemoryInput


class RecordingRemoteProvider(EmbeddingProvider):
    is_remote = True

    def __init__(self) -> None:
        self.inputs: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.inputs.append(text)
        return [1.0, 0.0, 0.0]


class FTSQuerySpy:
    def __init__(self, wrapped: Callable[..., list[dict]]) -> None:
        self.wrapped = wrapped
        self.queries: list[str] = []

    def __call__(self, query: str, *args, **kwargs) -> list[dict]:
        self.queries.append(query)
        return self.wrapped(query, *args, **kwargs)


@dataclass
class PrivacyFixture:
    service: MemoryService
    provider: RecordingRemoteProvider
    fts_spy: FTSQuerySpy


@pytest.fixture
def service_with_remote_provider(
    env_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    service = MemoryService(str(env_home))
    provider = RecordingRemoteProvider()
    service.config.embedding.provider = "openai"
    service.config.context.semantic = "always"
    service._embedding_provider = provider
    service.db.ensure_vec_table(3)
    service._vectors_available = True
    fts_spy = FTSQuerySpy(service.db.fts_search)
    monkeypatch.setattr(service.db, "fts_search", fts_spy)
    try:
        yield PrivacyFixture(service, provider, fts_spy)
    finally:
        service.close()


@pytest.fixture
def local_service(env_home: Path):
    service = MemoryService(str(env_home))
    try:
        yield service
    finally:
        service.close()


def test_context_default_never_embeds_query_remotely(
    service_with_remote_provider: PrivacyFixture,
) -> None:
    fixture = service_with_remote_provider
    fixture.service.get_context(project="p", query="secret sk_live_value")
    assert fixture.provider.inputs == []
    assert fixture.fts_spy.queries == ["secret sk_live_value"]


def test_remote_opt_in_sends_only_redacted_query(
    service_with_remote_provider: PrivacyFixture,
) -> None:
    fixture = service_with_remote_provider
    fixture.service.config.context.allow_remote_query_embeddings = True
    fixture.service.get_context(project="p", query="secret sk_live_value")
    assert fixture.provider.inputs == ["secret [REDACTED]"]
    assert fixture.fts_spy.queries == ["secret sk_live_value"]


def test_remote_embedding_override_is_redacted_at_service_boundary(
    service_with_remote_provider: PrivacyFixture,
) -> None:
    fixture = service_with_remote_provider
    fixture.service.config.context.allow_remote_query_embeddings = True
    fixture.service.search(
        "lexical marker",
        project="p",
        embedding_query="semantic sk_live_override",
    )
    assert fixture.provider.inputs == ["semantic [REDACTED]"]
    assert fixture.fts_spy.queries == ["lexical marker"]


def test_query_text_is_not_persisted_or_logged(
    service_with_remote_provider: PrivacyFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fixture = service_with_remote_provider
    fixture.service.config.context.allow_remote_query_embeddings = True
    fixture.service.get_context(project="p", query="secret sk_live_value")
    assert "sk_live_value" not in caplog.text
    for path in Path(fixture.service.vault_dir).rglob("*.md"):
        assert "sk_live_value" not in path.read_text(encoding="utf-8")


def test_feedback_can_be_suppressed_for_duplicate_hooks(
    local_service: MemoryService,
) -> None:
    saved = local_service.save(
        RawMemoryInput(title="Marker", what="ALPHA-42"),
        project="p",
    )
    local_service.get_context(
        project="p",
        query="ALPHA-42",
        record_feedback=False,
    )
    assert local_service.get_memory_record(saved["id"])[
        "retrieved_count"
    ] == 0


def test_doctor_reports_only_effective_query_privacy_mode(
    service_with_remote_provider: PrivacyFixture,
) -> None:
    fixture = service_with_remote_provider
    fixture.service.config.context.allow_remote_query_embeddings = True
    fixture.service.get_context(project="p", query="secret sk_live_value")
    report = doctor(fixture.service, "p")
    assert report["query_privacy"] == {
        "provider_scope": "remote",
        "allow_remote_query_embeddings": True,
        "automatic_query_embedding": "remote_redacted",
    }
    assert "sk_live_value" not in json.dumps(report)
