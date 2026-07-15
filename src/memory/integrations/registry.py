from __future__ import annotations

from memory.integrations.types import IntegrationAdapter


class UnknownIntegrationError(KeyError):
    pass


_ADAPTERS: dict[str, IntegrationAdapter] = {}


def register_adapter(adapter: IntegrationAdapter) -> None:
    integration_id = adapter.integration_id
    if integration_id in _ADAPTERS:
        raise ValueError(f"Integration already registered: {integration_id}")
    _ADAPTERS[integration_id] = adapter


def get_adapter(name: str) -> IntegrationAdapter:
    try:
        return _ADAPTERS[name]
    except KeyError as error:
        raise UnknownIntegrationError(name) from error


def registered_adapters() -> tuple[str, ...]:
    return tuple(sorted(_ADAPTERS))
