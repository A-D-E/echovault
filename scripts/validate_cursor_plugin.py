from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml


_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_TOOLS = (
    "memory_context",
    "memory_search",
    "memory_details",
    "memory_save",
)


def _json(path: Path, label: str, errors: list[str]) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        errors.append(f"{label}: invalid or unreadable JSON")
        return None


def _reference(
    root: Path,
    manifest_label: str,
    field: str,
    value: object,
    *,
    directory: bool,
    errors: list[str],
) -> Path | None:
    if not isinstance(value, str) or not value:
        errors.append(f"{manifest_label}: {field} must be a relative path")
        return None
    candidate = (root / value).resolve(strict=False)
    if not candidate.is_relative_to(root):
        errors.append(f"{manifest_label}: {field} escapes plugin root")
        return None
    exists = candidate.is_dir() if directory else candidate.is_file()
    if not exists:
        errors.append(f"{manifest_label}: {field} points to {value}")
        return None
    return candidate


def validate_cursor_plugin(root: Path) -> tuple[str, ...]:
    """Return deterministic structural errors without mutating the bundle."""

    resolved = root.expanduser().resolve()
    errors: list[str] = []
    manifest_label = ".cursor-plugin/plugin.json"
    manifest_path = resolved / manifest_label
    manifest = _json(manifest_path, manifest_label, errors)
    mcp_path: Path | None = None
    if isinstance(manifest, dict):
        if manifest.get("name") != "echovault":
            errors.append(f"{manifest_label}: name must be echovault")
        version = manifest.get("version")
        if not isinstance(version, str) or _SEMVER.fullmatch(version) is None:
            errors.append(f"{manifest_label}: version must be semantic")
        _reference(
            resolved,
            manifest_label,
            "skills",
            manifest.get("skills"),
            directory=True,
            errors=errors,
        )
        _reference(
            resolved,
            manifest_label,
            "rules",
            manifest.get("rules"),
            directory=True,
            errors=errors,
        )
        mcp_path = _reference(
            resolved,
            manifest_label,
            "mcpServers",
            manifest.get("mcpServers"),
            directory=False,
            errors=errors,
        )

    if mcp_path is not None:
        mcp_label = mcp_path.relative_to(resolved).as_posix()
        mcp = _json(mcp_path, mcp_label, errors)
        if isinstance(mcp, dict):
            servers = mcp.get("mcpServers")
            valid_servers = (
                isinstance(servers, dict)
                and set(servers) == {"echovault"}
            )
            if not valid_servers:
                errors.append(
                    f"{mcp_label}: must contain exactly one EchoVault server"
                )
            else:
                entry = servers["echovault"]
                valid_entry = (
                    isinstance(entry, dict)
                    and isinstance(entry.get("command"), str)
                    and entry.get("args") == ["mcp", "--agent", "cursor"]
                    and "env" not in entry
                    and "trust" not in entry
                )
                if not valid_entry:
                    errors.append(
                        f"{mcp_label}: EchoVault server contract is invalid"
                    )

    rule_path = resolved / "rules/echovault.mdc"
    skill_path = resolved / "skills/echovault/SKILL.md"
    try:
        rule = rule_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        rule = ""
        errors.append("rules/echovault.mdc: missing or unreadable")
    try:
        skill = skill_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        skill = ""
        errors.append("skills/echovault/SKILL.md: missing or unreadable")
    if rule:
        parts = rule.split("---", 2)
        try:
            frontmatter = yaml.safe_load(parts[1]) if len(parts) == 3 else None
        except yaml.YAMLError:
            frontmatter = None
        if not isinstance(frontmatter, dict) or frontmatter.get(
            "alwaysApply"
        ) is not True:
            errors.append(
                "rules/echovault.mdc: frontmatter must set alwaysApply true"
            )
    for label, content in (
        ("rules/echovault.mdc", rule),
        ("skills/echovault/SKILL.md", skill),
    ):
        for phrase in (*_TOOLS, "curated", "idempotency", "transcript"):
            if phrase not in content:
                errors.append(f"{label}: missing required guidance {phrase}")
    for path in sorted(resolved.rglob("*")) if resolved.is_dir() else ():
        if not path.is_file():
            continue
        try:
            unresolved = "{{" in path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if unresolved:
            errors.append(
                f"{path.relative_to(resolved).as_posix()}: unresolved template marker"
            )
    return tuple(sorted(set(errors)))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a rendered EchoVault Cursor plugin."
    )
    parser.add_argument("root", type=Path)
    arguments = parser.parse_args()
    errors = validate_cursor_plugin(arguments.root)
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
