from __future__ import annotations

import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from memory.integrations.asset_io import REQUIRED_PACKAGE_ASSETS
from scripts.render_integration_fixture import render_fixture
from scripts.validate_cursor_plugin import validate_cursor_plugin
from scripts.verify_wheel_assets import inspect_sdist, inspect_wheel


def test_rendered_cursor_bundle_passes_checked_in_validator(
    tmp_path: Path,
) -> None:
    root = render_fixture(
        client="cursor",
        output=tmp_path / "cursor",
        memory_command="/opt/echovault/bin/memory",
        version="0.6.0",
    )
    assert validate_cursor_plugin(root) == ()


def test_cursor_validator_reports_broken_mcp_reference(
    tmp_path: Path,
) -> None:
    root = render_fixture(
        client="cursor",
        output=tmp_path / "cursor",
        memory_command="/opt/echovault/bin/memory",
        version="0.6.0",
    )
    manifest_path = root / ".cursor-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["mcpServers"] = "missing.json"
    manifest_path.write_text(json.dumps(manifest))
    assert validate_cursor_plugin(root) == (
        ".cursor-plugin/plugin.json: mcpServers points to missing.json",
    )


def test_fixture_renderer_refuses_nonempty_output(tmp_path: Path) -> None:
    output = tmp_path / "bundle"
    output.mkdir()
    (output / "user.txt").write_text("preserve")
    with pytest.raises(FileExistsError):
        render_fixture("gemini", output, "memory", "0.6.0")
    assert (output / "user.txt").read_text() == "preserve"


def test_rendered_gemini_bundle_has_no_unresolved_templates(
    tmp_path: Path,
) -> None:
    root = render_fixture(
        "gemini",
        tmp_path / "gemini",
        "/opt/Echo Vault/bin/memory",
        "0.6.0",
    )
    assert (root / "gemini-extension.json").is_file()
    assert (root / "hooks/hooks.json").is_file()
    assert all(
        b"{{" not in path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    )


def _wheel(path: Path, *, unsafe: bool = False) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for asset in REQUIRED_PACKAGE_ASSETS:
            archive.writestr(
                f"memory/integrations/assets/{asset}",
                b"asset",
            )
        if unsafe:
            archive.writestr("../escape", b"unsafe")


def _sdist(path: Path) -> None:
    with tarfile.open(path, "w:gz") as archive:
        members = {
            "echovault-0.6.0/pyproject.toml": b"[project]\n",
            **{
                "echovault-0.6.0/src/memory/integrations/assets/"
                f"{asset}": b"asset"
                for asset in REQUIRED_PACKAGE_ASSETS
            },
        }
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def test_wheel_and_sdist_inspection_require_all_safe_assets(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "echovault-0.6.0-py3-none-any.whl"
    sdist = tmp_path / "echovault-0.6.0.tar.gz"
    _wheel(wheel)
    _sdist(sdist)
    wheel_result = inspect_wheel(wheel)
    sdist_result = inspect_sdist(sdist)
    assert wheel_result.missing_assets == ()
    assert wheel_result.unsafe_members == ()
    assert wheel_result.duplicate_members == ()
    assert sdist_result.missing_assets == ()
    assert sdist_result.unsafe_members == ()


def test_archive_inspection_reports_unsafe_and_missing_members(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "unsafe.whl"
    _wheel(wheel, unsafe=True)
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr(
            "memory/integrations/assets/common/echovault-skill.md",
            b"duplicate",
        )
    result = inspect_wheel(wheel)
    assert result.unsafe_members == ("../escape",)
    assert result.duplicate_members == (
        "memory/integrations/assets/common/echovault-skill.md",
    )
