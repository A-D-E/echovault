from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from memory.integrations import config_io
from memory.integrations.config_io import (
    ConfigBoundaryError,
    ConfigConflictError,
    ConfigMalformedError,
    mutate_json_atomic,
    mutate_marked_block,
    mutate_toml_atomic,
    read_json_strict,
    validate_target_root,
)


def add_owned_echovault_entry(data: dict[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(data)
    updated.setdefault("mcpServers", {})["echovault"] = {
        "command": "memory",
        "args": ["mcp", "--agent", "cursor"],
    }
    return updated


def test_malformed_json_is_never_mutated(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    original = b'{"mcpServers":'
    path.write_bytes(original)
    with pytest.raises(ConfigMalformedError):
        mutate_json_atomic(path, lambda data: data)
    assert path.read_bytes() == original


def test_json_mutation_preserves_unrelated_nested_values_and_mode(
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "theme": "dark",
                "mcpServers": {
                    "other": {
                        "command": "other",
                        "env": {"TOKEN": "unchanged"},
                    }
                },
            }
        )
    )
    path.chmod(0o640)
    mutate_json_atomic(path, add_owned_echovault_entry)
    data = json.loads(path.read_text())
    assert data["mcpServers"]["other"]["env"]["TOKEN"] == "unchanged"
    assert path.stat().st_mode & 0o777 == 0o640


def test_second_external_change_returns_conflict_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{}")
    real_digest = config_io.digest_file
    comparisons = 0

    def digest_after_external_change(target: Path) -> str | None:
        nonlocal comparisons
        comparisons += 1
        path.write_text(json.dumps({"external": comparisons}))
        return real_digest(target)

    monkeypatch.setattr(config_io, "digest_file", digest_after_external_change)
    with pytest.raises(ConfigConflictError):
        mutate_json_atomic(
            path,
            add_owned_echovault_entry,
            max_conflict_retries=1,
        )
    assert json.loads(path.read_text()) == {"external": 2}


def test_implicit_symlink_escape_is_rejected_but_explicit_root_is_allowed(
    tmp_path: Path,
) -> None:
    boundary = tmp_path / "home"
    outside = tmp_path / "outside"
    boundary.mkdir()
    outside.mkdir()
    target = boundary / ".cursor"
    target.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ConfigBoundaryError):
        validate_target_root(target, boundary, explicit=False)
    assert validate_target_root(target, boundary, explicit=True) == outside.resolve()


def test_strict_reader_distinguishes_missing_empty_and_valid(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.json"
    assert read_json_strict(path).state.value == "missing"
    path.write_text("  \n")
    assert read_json_strict(path).state.value == "empty"
    path.write_text("{}\n")
    assert read_json_strict(path).state.value == "valid"


def test_toml_mutation_preserves_comments_and_unrelated_tables(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "# keep this comment\nmodel = \"gpt-5\"\n\n[other]\nvalue = 7\n"
    )

    def mutate(document):
        document.setdefault("mcp_servers", {})["echovault"] = {
            "command": "memory",
            "args": ["mcp"],
        }
        return document

    mutate_toml_atomic(path, mutate)
    rendered = path.read_text()
    assert "# keep this comment" in rendered
    assert "[other]" in rendered
    assert "[mcp_servers.echovault]" in rendered


def test_malformed_toml_is_never_replaced(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = b"[broken\n"
    path.write_bytes(original)
    with pytest.raises(ConfigMalformedError):
        mutate_toml_atomic(path, lambda data: data)
    assert path.read_bytes() == original


def test_marked_block_rejects_unmatched_marker(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text("user content\n<!-- echovault:start -->\nbroken\n")
    before = path.read_bytes()
    with pytest.raises(ConfigMalformedError):
        mutate_marked_block(
            path,
            marker="echovault",
            block="managed\n",
        )
    assert path.read_bytes() == before
