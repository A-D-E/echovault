from __future__ import annotations

import importlib.metadata
from importlib.resources import files
import json
from pathlib import Path
import subprocess

import pytest
import yaml

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


def test_ci_matrix_and_pinned_validator_are_explicit() -> None:
    workflow = yaml.safe_load(Path(".github/workflows/ci.yml").read_text())
    matrix = workflow["jobs"]["python-tests"]["strategy"]["matrix"][
        "include"
    ]
    cells = {(item["os"], str(item["python"])) for item in matrix}
    assert cells == {
        ("ubuntu-latest", "3.10"),
        ("ubuntu-latest", "3.11"),
        ("ubuntu-latest", "3.12"),
        ("ubuntu-latest", "3.13"),
        ("ubuntu-latest", "3.14"),
        ("macos-latest", "3.10"),
        ("macos-latest", "3.14"),
        ("windows-latest", "3.10"),
        ("windows-latest", "3.14"),
    }
    package_steps = workflow["jobs"]["package-gate"]["steps"]
    serialized = json.dumps(package_steps)
    assert "@google/gemini-cli@0.50.0" in serialized
    assert "gemini extensions validate" in serialized
    assert "verify_installed_tool.py" in serialized
    assert "cursor-agent" not in serialized


@pytest.mark.parametrize(
    ("path", "phrases"),
    [
        (
            "docs/integrations/cursor.md",
            (
                "memory setup cursor",
                "memory doctor --agent cursor",
                "policy-guided",
            ),
        ),
        (
            "docs/integrations/gemini-cli.md",
            (
                "memory setup gemini",
                "BeforeAgent",
                "N/U/P",
                "not verified",
            ),
        ),
        (
            "docs/migrations/cross-agent-v0.6.md",
            (
                "memory migrate vault-metadata",
                "--force-managed",
                "schema v1",
            ),
        ),
        (
            "docs/security-and-privacy.md",
            (
                "allow_remote_query_embeddings",
                "transcripts",
                "redacted copy",
            ),
        ),
    ],
)
def test_support_docs_cover_required_contracts(
    path: str,
    phrases: tuple[str, ...],
) -> None:
    content = Path(path).read_text()
    for phrase in phrases:
        assert phrase in content


@pytest.mark.parametrize(
    "path",
    [
        "docs/integrations/cursor.md",
        "docs/integrations/gemini-cli.md",
        "docs/migrations/cross-agent-v0.6.md",
        "docs/security-and-privacy.md",
        "docs/dogfood/README.md",
    ],
)
def test_support_docs_are_not_git_ignored(path: str) -> None:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", path],
        check=False,
        shell=False,
    )
    assert result.returncode == 1
