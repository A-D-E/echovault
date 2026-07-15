from __future__ import annotations

import json
from pathlib import Path

import pytest

from memory.integrations.config_io import ConfigBoundaryError
from memory.integrations.gemini import GeminiAdapter
from memory.integrations.ownership import OwnershipConflict
from memory.integrations.registry import get_adapter
from memory.integrations.types import AdapterCapabilities
from tests.gemini_helpers import (
    RecordingRunner,
    gemini_050_runner,
    gemini_adapter,
    named_hook,
    native_options,
    platform_echovault_config,
    project_direct_options,
    seed_gemini_with_other_hook,
    user_direct_options,
)
from tests.integration_helpers import snapshot_tree


def test_project_direct_always_installs_portable_mcp_and_supported_hook(
    tmp_path: Path,
    gemini_050_runner,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    result = gemini_adapter(gemini_050_runner).setup(
        project_direct_options(project)
    )
    settings = json.loads((project / ".gemini/settings.json").read_text())
    assert settings["mcpServers"]["echovault"]["command"] == "memory"
    assert settings["mcpServers"]["echovault"]["args"] == [
        "mcp",
        "--agent",
        "gemini-cli",
    ]
    assert named_hook(settings, "BeforeAgent", "echovault-context") is not None
    assert (project / "GEMINI.md").read_text().count(
        "<!-- echovault:start -->"
    ) == 1
    assert (project / ".gemini/skills/echovault/SKILL.md").is_file()
    assert (project / ".gemini/.echovault-managed.json").is_file()
    assert result.status == "installed"


def test_user_direct_uses_absolute_command_and_preserves_unrelated_hooks(
    tmp_path: Path,
    fake_memory: Path,
    gemini_050_runner,
) -> None:
    root = seed_gemini_with_other_hook(tmp_path)
    gemini_adapter(gemini_050_runner).setup(
        user_direct_options(root, fake_memory)
    )
    settings = json.loads((root / "settings.json").read_text())
    assert settings["mcpServers"]["echovault"]["command"] == str(fake_memory)
    assert named_hook(settings, "BeforeAgent", "other-hook") is not None
    assert named_hook(settings, "BeforeAgent", "echovault-context") is not None


def test_direct_setup_is_byte_stable(
    tmp_path: Path,
    gemini_050_runner,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    adapter = gemini_adapter(gemini_050_runner)
    options = project_direct_options(project)
    adapter.setup(options)
    first = snapshot_tree(project)
    result = adapter.setup(options)
    assert result.status == "unchanged"
    assert snapshot_tree(project) == first


def test_implicit_project_symlink_escape_is_rejected(
    tmp_path: Path,
    gemini_050_runner,
) -> None:
    project = tmp_path / "repo"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / ".gemini").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ConfigBoundaryError):
        gemini_adapter(gemini_050_runner).setup(
            project_direct_options(project)
        )
    assert not list(outside.iterdir())


def test_explicit_project_config_dir_is_the_only_escape_opt_in(
    tmp_path: Path,
    gemini_050_runner,
) -> None:
    project = tmp_path / "repo"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    result = gemini_adapter(gemini_050_runner).setup(
        project_direct_options(project, config_root=outside)
    )
    assert result.status == "installed"
    assert (outside / "settings.json").is_file()


def test_custom_same_named_mcp_is_conflict_without_mutation(
    tmp_path: Path,
    gemini_050_runner,
) -> None:
    project = tmp_path / "repo"
    root = project / ".gemini"
    root.mkdir(parents=True)
    (root / "settings.json").write_text(
        json.dumps({"mcpServers": {"echovault": {"command": "custom"}}})
    )
    before = snapshot_tree(project)
    with pytest.raises(OwnershipConflict, match="custom"):
        gemini_adapter(gemini_050_runner).setup(
            project_direct_options(project)
        )
    assert snapshot_tree(project) == before


def test_registry_exposes_gemini_only_after_adapter_exists() -> None:
    registered = get_adapter("gemini")
    assert isinstance(registered, GeminiAdapter)
    assert registered.integration_id == "gemini"
    assert registered.agent == "gemini-cli"
    assert registered.capabilities == AdapterCapabilities(
        mcp=True,
        rules=False,
        skills=True,
        extensions=True,
        hooks=True,
    )


def test_old_client_gets_static_and_mcp_with_degraded_warning(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    result = gemini_adapter(RecordingRunner(client_version="0.49.0")).setup(
        project_direct_options(project)
    )
    settings = json.loads((project / ".gemini/settings.json").read_text())
    assert "echovault" in settings["mcpServers"]
    assert named_hook(settings, "BeforeAgent", "echovault-context") is None
    assert any("lacks BeforeAgent" in warning for warning in result.warnings)


def test_project_direct_honors_exact_command(
    tmp_path: Path,
    gemini_050_runner,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    gemini_adapter(gemini_050_runner).setup(
        project_direct_options(
            project,
            command="/workspace/.venv/bin/memory",
        )
    )
    settings = json.loads((project / ".gemini/settings.json").read_text())
    assert settings["mcpServers"]["echovault"]["command"] == (
        "/workspace/.venv/bin/memory"
    )


def test_setup_does_not_mutate_context_policy(
    tmp_path: Path,
    gemini_050_runner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_home = tmp_path / "memory-home"
    memory_home.mkdir()
    config = memory_home / "config.yaml"
    config.write_text("context:\n  mode: off\n")
    before = config.read_bytes()
    monkeypatch.setenv("MEMORY_HOME", str(memory_home))
    project = tmp_path / "repo"
    project.mkdir()
    gemini_adapter(gemini_050_runner).setup(project_direct_options(project))
    assert config.read_bytes() == before


def test_custom_same_named_hook_is_conflict_without_mutation(
    tmp_path: Path,
    gemini_050_runner,
) -> None:
    project = tmp_path / "repo"
    root = project / ".gemini"
    root.mkdir(parents=True)
    (root / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "BeforeAgent": [
                        {
                            "matcher": "custom",
                            "hooks": [
                                {
                                    "name": "echovault-context",
                                    "type": "command",
                                    "command": "custom",
                                }
                            ],
                        }
                    ]
                }
            }
        )
    )
    before = snapshot_tree(project)
    with pytest.raises(OwnershipConflict, match="custom Gemini hook"):
        gemini_adapter(gemini_050_runner).setup(project_direct_options(project))
    assert snapshot_tree(project) == before


def test_native_install_validates_then_invokes_consent_prompt(
    tmp_path: Path,
    fake_memory: Path,
) -> None:
    runner = RecordingRunner(installed=False)
    adapter = gemini_adapter(runner)
    result = adapter.setup(native_options(tmp_path, fake_memory))
    source = (
        platform_echovault_config(tmp_path)
        / "integrations/gemini-extension/echovault"
    )
    assert runner.argv[0] == [
        "gemini",
        "extensions",
        "validate",
        str(source),
    ]
    assert ["gemini", "extensions", "install", str(source)] in runner.argv
    install_index = runner.argv.index(
        ["gemini", "extensions", "install", str(source)]
    )
    assert "--consent" not in runner.argv[install_index]
    assert runner.calls[0]["capture_output"] is True
    assert runner.calls[install_index]["capture_output"] is False
    assert runner.calls[0]["cwd"] == source.parent.resolve()
    assert runner.calls[install_index]["cwd"] == source.parent.resolve()
    assert result.status == "installed"


def test_native_owned_upgrade_uses_update(
    tmp_path: Path,
    fake_memory: Path,
) -> None:
    runner = RecordingRunner(installed=True, installed_version="0.5.0")
    result = gemini_adapter(runner).setup(native_options(tmp_path, fake_memory))
    assert ["gemini", "extensions", "update", "echovault"] in runner.argv
    assert result.status == "updated"


def test_native_owned_source_symlink_escape_is_rejected(
    tmp_path: Path,
    fake_memory: Path,
) -> None:
    source = (
        platform_echovault_config(tmp_path)
        / "integrations/gemini-extension/echovault"
    )
    outside = tmp_path / "outside"
    source.parent.mkdir(parents=True)
    outside.mkdir()
    source.symlink_to(outside, target_is_directory=True)
    runner = RecordingRunner(installed=False)
    with pytest.raises(ConfigBoundaryError):
        gemini_adapter(runner).setup(native_options(tmp_path, fake_memory))
    assert runner.argv == []
    assert not list(outside.iterdir())


def test_unmarked_native_source_is_conflict_before_manager_call(
    tmp_path: Path,
    fake_memory: Path,
) -> None:
    source = (
        platform_echovault_config(tmp_path)
        / "integrations/gemini-extension/echovault"
    )
    source.mkdir(parents=True)
    (source / "mine.txt").write_text("mine")
    runner = RecordingRunner(installed=False)
    with pytest.raises(OwnershipConflict):
        gemini_adapter(runner).setup(native_options(tmp_path, fake_memory))
    assert runner.argv == []
    assert (source / "mine.txt").read_text() == "mine"


def test_native_current_install_is_byte_stable_and_does_not_update(
    tmp_path: Path,
    fake_memory: Path,
) -> None:
    runner = RecordingRunner(installed=False)
    adapter = gemini_adapter(runner)
    options = native_options(tmp_path, fake_memory)
    adapter.setup(options)
    source = (
        platform_echovault_config(tmp_path)
        / "integrations/gemini-extension/echovault"
    )
    before = snapshot_tree(source)
    runner.argv.clear()
    runner.calls.clear()
    result = adapter.setup(options)
    assert result.status == "unchanged"
    assert ["gemini", "extensions", "update", "echovault"] not in runner.argv
    assert snapshot_tree(source) == before
