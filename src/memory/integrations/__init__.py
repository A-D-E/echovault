"""Typed integration adapters for supported coding agents."""

from memory.integrations.cursor import CursorAdapter
from memory.integrations.registry import (
    get_adapter,
    register_adapter,
    registered_adapters,
)


if "cursor" not in registered_adapters():
    register_adapter(CursorAdapter())

__all__ = ["get_adapter", "registered_adapters"]
