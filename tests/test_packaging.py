from __future__ import annotations

import importlib.metadata
from importlib.resources import files
from pathlib import Path

from memory.integrations.asset_io import (
    REQUIRED_PACKAGE_ASSETS,
    read_package_asset,
)


EXPECTED_ASSETS = {
    "common/echovault-skill.md",
    "cursor/plugin.json",
    "cursor/mcp.json",
    "cursor/echovault.mdc",
    "gemini/gemini-extension.json",
    "gemini/GEMINI.md",
    "gemini/hooks.json",
    "schemas/ownership-manifest.schema.json",
}


def test_required_package_assets_are_explicit_and_readable() -> None:
    assert set(REQUIRED_PACKAGE_ASSETS) == EXPECTED_ASSETS
    for relative_path in REQUIRED_PACKAGE_ASSETS:
        assert read_package_asset(relative_path).strip()


def test_repository_skill_is_generated_from_canonical_resource() -> None:
    repository_skill = Path("skills/echovault/SKILL.md").read_bytes()
    assert repository_skill == read_package_asset(
        "common/echovault-skill.md"
    )


def test_runtime_assets_resolve_inside_memory_package() -> None:
    package_root = files("memory").joinpath("integrations/assets")
    assert package_root.joinpath("cursor/plugin.json").is_file()


def test_cross_agent_build_has_distinct_package_version() -> None:
    assert importlib.metadata.version("echovault") == "0.6.0"


def test_read_package_asset_rejects_unlisted_paths() -> None:
    try:
        read_package_asset("../../pyproject.toml")
    except ValueError as error:
        assert "not a required package asset" in str(error)
    else:  # pragma: no cover - required security boundary
        raise AssertionError("unlisted package resource was accepted")
