"""Package-resource rendering and managed-tree staging helpers."""

from __future__ import annotations

import re
import json
import os
import shlex
import subprocess
from importlib.resources import files
from collections.abc import Sequence

from memory.integrations.ownership import (
    replace_managed_tree,
    stage_managed_tree,
)

_VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]*$")
_UNBOUND_COMPATIBILITY = re.compile(
    r"\n?<!-- echovault:unbound-compatibility:start -->.*?"
    r"<!-- echovault:unbound-compatibility:end -->\n?",
    re.DOTALL,
)
REQUIRED_PACKAGE_ASSETS = (
    "common/echovault-skill.md",
    "cursor/plugin.json",
    "cursor/mcp.json",
    "cursor/echovault.mdc",
    "gemini/gemini-extension.json",
    "gemini/GEMINI.md",
    "gemini/hooks.json",
    "schemas/ownership-manifest.schema.json",
)
_CURSOR_RESOURCES = {
    ".cursor-plugin/plugin.json": "cursor/plugin.json",
    "mcp.json": "cursor/mcp.json",
    "rules/echovault.mdc": "cursor/echovault.mdc",
    "skills/echovault/SKILL.md": "common/echovault-skill.md",
}
_GEMINI_RESOURCES = {
    "gemini-extension.json": "gemini/gemini-extension.json",
    "GEMINI.md": "gemini/GEMINI.md",
    "hooks/hooks.json": "gemini/hooks.json",
    "skills/echovault/SKILL.md": "common/echovault-skill.md",
}


class IntegrationAssetError(RuntimeError):
    """Raised when one declared runtime package asset cannot be loaded."""


def read_package_asset(relative_path: str) -> bytes:
    """Read one allowlisted runtime asset from the installed memory package."""

    if relative_path not in REQUIRED_PACKAGE_ASSETS:
        raise ValueError(
            f"{relative_path!r} is not a required package asset"
        )
    try:
        return (
            files("memory")
            .joinpath("integrations", "assets", relative_path)
            .read_bytes()
        )
    except (FileNotFoundError, OSError, TypeError) as error:
        raise IntegrationAssetError(
            f"Missing packaged integration asset: {relative_path}"
        ) from error


def _client_skill(template: str, version: str) -> str:
    return _UNBOUND_COMPATIBILITY.sub("\n", template).replace(
        "{{VERSION}}",
        version,
    )


def _package_text(relative_path: str) -> str:
    return (
        read_package_asset(relative_path)
        .decode("utf-8")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )


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

    assets: dict[str, bytes] = {}
    for destination, resource in _CURSOR_RESOURCES.items():
        template = _package_text(resource)
        if resource == "common/echovault-skill.md":
            template = _client_skill(template, rendered_version)
        is_json = destination.endswith(".json")
        rendered = template.replace(
            "{{MEMORY_COMMAND}}",
            _json_placeholder(rendered_command) if is_json else rendered_command,
        ).replace(
            "{{VERSION}}",
            _json_placeholder(rendered_version) if is_json else rendered_version,
        )
        if "{{MEMORY_COMMAND}}" in rendered or "{{VERSION}}" in rendered:
            raise ValueError(f"unresolved placeholder in {resource}")
        if is_json:
            json.loads(rendered)
        assets[destination] = rendered.encode("utf-8")
    return assets


def shell_join_command(argv: Sequence[str]) -> str:
    """Render one argv vector for Gemini's command-hook string field."""

    if not argv:
        raise ValueError("command argv must not be empty")
    values = [
        _validated_placeholder(value, name="command argument") for value in argv
    ]
    if os.name == "nt":
        return subprocess.list2cmdline(values)
    return shlex.join(values)


def _json_placeholder(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)[1:-1]


def render_gemini_assets(
    memory_command: str,
    hook_command: str,
    version: str,
) -> dict[str, bytes]:
    """Render the canonical Gemini extension assets from package resources."""

    rendered_memory = _validated_placeholder(
        memory_command,
        name="memory_command",
    )
    rendered_hook = _validated_placeholder(hook_command, name="hook_command")
    rendered_version = _validated_placeholder(version, name="version")
    if _VERSION_PATTERN.fullmatch(rendered_version) is None:
        raise ValueError("version contains unsupported characters")

    assets: dict[str, bytes] = {}
    for destination, resource in _GEMINI_RESOURCES.items():
        template = _package_text(resource)
        if resource == "common/echovault-skill.md":
            template = _client_skill(template, rendered_version)
        is_json = destination.endswith(".json")
        replacements = {
            "{{MEMORY_COMMAND}}": (
                _json_placeholder(rendered_memory)
                if is_json
                else rendered_memory
            ),
            "{{HOOK_COMMAND}}": (
                _json_placeholder(rendered_hook) if is_json else rendered_hook
            ),
            "{{VERSION}}": (
                _json_placeholder(rendered_version)
                if is_json
                else rendered_version
            ),
        }
        rendered = template
        for placeholder, value in replacements.items():
            rendered = rendered.replace(placeholder, value)
        if any(placeholder in rendered for placeholder in replacements):
            raise ValueError(f"unresolved placeholder in {resource}")
        if is_json:
            json.loads(rendered)
        assets[destination] = rendered.encode("utf-8")
    return assets


__all__ = [
    "IntegrationAssetError",
    "REQUIRED_PACKAGE_ASSETS",
    "read_package_asset",
    "render_cursor_assets",
    "render_gemini_assets",
    "replace_managed_tree",
    "shell_join_command",
    "stage_managed_tree",
]
