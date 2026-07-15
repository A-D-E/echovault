import pytest

from memory.integrations.registry import (
    UnknownIntegrationError,
    get_adapter,
    register_adapter,
    registered_adapters,
)
from memory.integrations.types import AdapterCapabilities


class StubAdapter:
    integration_id = "stub"
    agent = "stub-agent"
    capabilities = AdapterCapabilities(
        mcp=True,
        rules=False,
        skills=False,
        extensions=False,
        hooks=False,
    )


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("memory.integrations.registry._ADAPTERS", {})


def test_registry_registers_adapter_without_storage_logic() -> None:
    register_adapter(StubAdapter())
    adapter = get_adapter("stub")
    assert adapter.integration_id == "stub"
    assert registered_adapters() == ("stub",)


def test_unknown_adapter_is_explicit() -> None:
    with pytest.raises(UnknownIntegrationError):
        get_adapter("unknown")


def test_duplicate_adapter_is_rejected() -> None:
    register_adapter(StubAdapter())
    with pytest.raises(ValueError, match="already registered"):
        register_adapter(StubAdapter())
