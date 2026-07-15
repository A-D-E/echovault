"""Package-resource rendering and managed-tree staging helpers."""

from __future__ import annotations

import re
from importlib.resources import files

from memory.integrations.ownership import (
    replace_managed_tree,
    stage_managed_tree,
)

_VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]*$")
_CURSOR_RESOURCES = {
    ".cursor-plugin/plugin.json": "cursor/plugin.json",
    "mcp.json": "cursor/mcp.json",
    "rules/echovault.mdc": "cursor/echovault.mdc",
    "skills/echovault/SKILL.md": "common/echovault-skill.md",
}


def _validated_placeholder(value: str, *, name: str) -> str:
    if not value or any(character in value for character in ("\x00", "\r", "\n")):
        raise ValueError(f"{name} must be a non-empty single-line value")
    return value


def render_cursor_assets(command: str, version: str) -> dict[str, bytes]:
    """Render the canonical Cursor plugin assets from package resources."""

    rendered_command = _validated_placeholder(command, name="command")
    rendered_version = _validated_placeholder(version, name="version")
    if _VERSION_PATTERN.fullmatch(rendered_version) is None:
        raise ValueError("version contains unsupported characters")

    root = files("memory.integrations.assets")
    assets: dict[str, bytes] = {}
    for destination, resource in _CURSOR_RESOURCES.items():
        template = root.joinpath(resource).read_text(encoding="utf-8")
        rendered = template.replace("{{MEMORY_COMMAND}}", rendered_command).replace(
            "{{VERSION}}", rendered_version
        )
        if "{{MEMORY_COMMAND}}" in rendered or "{{VERSION}}" in rendered:
            raise ValueError(f"unresolved placeholder in {resource}")
        assets[destination] = rendered.encode("utf-8")
    return assets


__all__ = ["render_cursor_assets", "replace_managed_tree", "stage_managed_tree"]
