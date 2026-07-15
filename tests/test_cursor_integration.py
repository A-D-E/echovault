import json
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner

from memory.cli import main
from memory.integrations.asset_io import render_cursor_assets
from memory.integrations.config_io import ConfigBoundaryError, ConfigMalformedError
from memory.integrations.ownership import OwnershipConflict
from memory.integrations.registry import get_adapter
from memory.setup import setup_cursor
from tests.integration_helpers import (
    cursor_adapter,
    project_options,
    seed_project_with_other_mcp,
    snapshot_tree,
    user_options,
)


def test_cursor_assets_have_valid_manifest_and_curated_contract() -> None:
    assets = render_cursor_assets(
        command="/opt/echovault/bin/memory",
        version="0.6.0",
    )
    manifest = json.loads(assets[".cursor-plugin/plugin.json"])
    mcp = json.loads(assets["mcp.json"])
    rule = assets["rules/echovault.mdc"].decode()
    skill = assets["skills/echovault/SKILL.md"].decode()
    assert manifest["name"] == "echovault"
    assert manifest["mcpServers"] == "mcp.json"
    assert mcp["mcpServers"]["echovault"]["args"] == [
        "mcp",
        "--agent",
        "cursor",
    ]
    assert "env" not in mcp["mcpServers"]["echovault"]
    assert "trust" not in mcp["mcpServers"]["echovault"]
    assert "alwaysApply: true" in rule
    for phrase in (
        "memory_context",
        "memory_search",
        "memory_details",
        "memory_save",
        "idempotency",
    ):
        assert phrase in rule
        assert phrase in skill
    assert "transcript" in rule
    assert "context.mode=off" in rule
    for forbidden in (
        "source=cursor",
        "source=gemini",
        "agent=cursor",
        "agent=gemini",
        "project=",
    ):
        assert forbidden not in skill
    assert "0.6.0" in rule
    assert "0.6.0" in skill


def test_project_setup_installs_portable_mcp_rule_skill_and_manifest(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    result = cursor_adapter().setup(project_options(project))
    config = json.loads((project / ".cursor" / "mcp.json").read_text())
    assert config["mcpServers"]["echovault"]["command"] == "memory"
    assert config["mcpServers"]["echovault"]["args"] == [
        "mcp",
        "--agent",
        "cursor",
    ]
    assert (project / ".cursor/rules/echovault.mdc").is_file()
    assert (project / ".cursor/skills/echovault/SKILL.md").is_file()
    assert (project / ".cursor/.echovault-managed.json").is_file()
    assert result.status == "installed"


def test_project_setup_is_byte_stable_and_preserves_other_server(
    tmp_path: Path,
) -> None:
    project = seed_project_with_other_mcp(tmp_path)
    adapter = cursor_adapter()
    adapter.setup(project_options(project))
    first = snapshot_tree(project / ".cursor")
    second = adapter.setup(project_options(project))
    assert snapshot_tree(project / ".cursor") == first
    assert second.status == "unchanged"
    config = json.loads((project / ".cursor/mcp.json").read_text())
    assert config["mcpServers"]["other"]["env"]["TOKEN"] == "unchanged"


def test_project_setup_does_not_mutate_service_context_policy(
    tmp_path: Path,
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
    cursor_adapter().setup(project_options(project))
    assert config.read_bytes() == before


def test_project_setup_upgrades_only_exact_legacy_entry(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    cursor = project / ".cursor"
    cursor.mkdir(parents=True)
    legacy = {"command": "memory", "args": ["mcp"], "type": "stdio"}
    (cursor / "mcp.json").write_text(
        json.dumps({"mcpServers": {"echovault": legacy}})
    )
    result = cursor_adapter().setup(project_options(project))
    entry = json.loads((cursor / "mcp.json").read_text())["mcpServers"][
        "echovault"
    ]
    assert entry == {
        "command": "memory",
        "args": ["mcp", "--agent", "cursor"],
    }
    assert result.status == "updated"


def test_project_setup_rejects_custom_same_named_entry_without_mutation(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    cursor = project / ".cursor"
    cursor.mkdir(parents=True)
    (cursor / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "echovault": {
                        "command": "wrapper",
                        "args": ["memory", "mcp"],
                    }
                }
            }
        )
    )
    before = snapshot_tree(cursor)
    with pytest.raises(OwnershipConflict, match="custom"):
        cursor_adapter().setup(project_options(project))
    assert snapshot_tree(cursor) == before


def test_project_setup_rejects_implicit_cursor_symlink_escape(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / ".cursor").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ConfigBoundaryError):
        cursor_adapter().setup(project_options(project))
    assert snapshot_tree(outside) == {}


def test_project_setup_honors_exact_command_and_explicit_config_root(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    target = tmp_path / "portable-cursor"
    project.mkdir()
    cursor_adapter().setup(
        project_options(
            project,
            command="/workspace/.venv/bin/memory",
            config_root=target,
            config_root_explicit=True,
        )
    )
    entry = json.loads((target / "mcp.json").read_text())["mcpServers"][
        "echovault"
    ]
    assert entry["command"] == "/workspace/.venv/bin/memory"


def test_registry_and_legacy_cursor_wrapper_keep_contract(tmp_path: Path) -> None:
    assert get_adapter("cursor").agent == "cursor"
    result = setup_cursor(str(tmp_path / ".cursor"))
    assert set(result) >= {"status", "message"}


def test_user_setup_installs_valid_local_plugin_with_absolute_command(
    tmp_path: Path,
    fake_memory: Path,
) -> None:
    cursor_root = tmp_path / ".cursor"
    result = cursor_adapter().setup(
        user_options(cursor_root, command=str(fake_memory))
    )
    plugin = cursor_root / "plugins/local/echovault"
    config = json.loads((plugin / "mcp.json").read_text())
    configured = Path(config["mcpServers"]["echovault"]["command"])
    assert configured.is_absolute()
    assert configured == fake_memory
    assert (plugin / ".cursor-plugin/plugin.json").is_file()
    assert (plugin / ".echovault-managed.json").is_file()
    assert "restart" in result.message.lower() or "reload" in result.message.lower()


def test_user_setup_is_byte_stable(tmp_path: Path, fake_memory: Path) -> None:
    cursor_root = tmp_path / ".cursor"
    adapter = cursor_adapter()
    options = user_options(cursor_root, command=str(fake_memory))
    adapter.setup(options)
    first = snapshot_tree(cursor_root)
    result = adapter.setup(options)
    assert result.status == "unchanged"
    assert snapshot_tree(cursor_root) == first


def test_unmarked_existing_plugin_directory_is_a_conflict(
    tmp_path: Path,
    fake_memory: Path,
) -> None:
    plugin = tmp_path / ".cursor/plugins/local/echovault"
    plugin.mkdir(parents=True)
    (plugin / "user.txt").write_text("mine")
    before = snapshot_tree(tmp_path / ".cursor")
    with pytest.raises(OwnershipConflict):
        cursor_adapter().setup(
            user_options(tmp_path / ".cursor", command=str(fake_memory))
        )
    assert snapshot_tree(tmp_path / ".cursor") == before


def test_user_setup_cleans_exact_legacy_mcp_and_preserves_other_server(
    tmp_path: Path,
    fake_memory: Path,
) -> None:
    cursor_root = tmp_path / ".cursor"
    cursor_root.mkdir()
    (cursor_root / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "echovault": {
                        "command": "memory",
                        "args": ["mcp"],
                        "type": "stdio",
                    },
                    "other": {"command": "other"},
                }
            }
        )
    )
    result = cursor_adapter().setup(
        user_options(cursor_root, command=str(fake_memory))
    )
    servers = json.loads((cursor_root / "mcp.json").read_text())["mcpServers"]
    assert "echovault" not in servers
    assert servers["other"] == {"command": "other"}
    assert result.status == "updated"


def test_user_setup_refuses_custom_direct_mcp_before_plugin_mutation(
    tmp_path: Path,
    fake_memory: Path,
) -> None:
    cursor_root = tmp_path / ".cursor"
    cursor_root.mkdir()
    (cursor_root / "mcp.json").write_text(
        json.dumps({"mcpServers": {"echovault": {"command": "custom"}}})
    )
    before = snapshot_tree(cursor_root)
    with pytest.raises(OwnershipConflict, match="custom"):
        cursor_adapter().setup(
            user_options(cursor_root, command=str(fake_memory))
        )
    assert snapshot_tree(cursor_root) == before


def test_user_setup_rejects_non_executable_command_without_mutation(
    tmp_path: Path,
) -> None:
    command = tmp_path / "memory"
    command.write_text("not executable")
    cursor_root = tmp_path / ".cursor"
    with pytest.raises(ValueError, match="executable"):
        cursor_adapter().setup(user_options(cursor_root, command=str(command)))
    assert snapshot_tree(cursor_root) == {}


def seed_cursor_state(tmp_path: Path, state: str):
    project = tmp_path / state
    project.mkdir()
    adapter = cursor_adapter()
    options = project_options(project)
    cursor = project / ".cursor"
    if state == "none":
        return adapter, options
    if state == "legacy_exact":
        cursor.mkdir()
        (cursor / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "echovault": {
                            "command": "memory",
                            "args": ["mcp"],
                            "type": "stdio",
                        }
                    }
                }
            )
        )
        return adapter, options
    if state == "custom_same_name":
        cursor.mkdir()
        (cursor / "mcp.json").write_text(
            json.dumps(
                {"mcpServers": {"echovault": {"command": "user-memory"}}}
            )
        )
        return adapter, options
    if state == "malformed":
        cursor.mkdir()
        (cursor / "mcp.json").write_text('{"mcpServers":')
        return adapter, options
    adapter.setup(options)
    if state == "older_owned":
        manifest_path = cursor / ".echovault-managed.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["asset_version"] = "0.5.0"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    elif state == "modified_owned":
        (cursor / "rules" / "echovault.mdc").write_text("user edit\n")
    elif state != "current_owned":
        raise AssertionError(f"unknown fixture state: {state}")
    return adapter, options


def run_cursor_matrix_case(tmp_path: Path, state: str, force: bool) -> str:
    adapter, options = seed_cursor_state(tmp_path, state)
    try:
        return adapter.setup(replace(options, force_managed=force)).status
    except OwnershipConflict:
        return "conflict"
    except ConfigMalformedError:
        return "parse_error"


@pytest.mark.parametrize(
    ("state", "force", "expected"),
    [
        ("none", False, "installed"),
        ("legacy_exact", False, "updated"),
        ("current_owned", False, "unchanged"),
        ("older_owned", False, "updated"),
        ("modified_owned", False, "conflict"),
        ("modified_owned", True, "updated"),
        ("custom_same_name", False, "conflict"),
        ("malformed", False, "parse_error"),
    ],
)
def test_cursor_setup_matrix(
    state: str,
    force: bool,
    expected: str,
    tmp_path: Path,
) -> None:
    assert run_cursor_matrix_case(tmp_path, state, force) == expected


def test_project_uninstall_removes_only_managed_scope_and_preserves_user_plugin(
    tmp_path: Path,
    fake_memory: Path,
) -> None:
    project = tmp_path / "repo"
    cursor_root = tmp_path / ".cursor"
    project.mkdir()
    adapter = cursor_adapter()
    project_config = project / ".cursor"
    (project_config / "unrelated").mkdir(parents=True)
    (project_config / "unrelated/mine.txt").write_text("mine")
    adapter.setup(project_options(project))
    adapter.setup(user_options(cursor_root, command=str(fake_memory)))
    user_before = snapshot_tree(cursor_root)
    result = adapter.uninstall(project_options(project))
    assert result.status == "removed"
    assert snapshot_tree(cursor_root) == user_before
    assert (project_config / "unrelated/mine.txt").read_text() == "mine"
    assert not (project_config / "rules/echovault.mdc").exists()
    assert "echovault" not in json.loads(
        (project_config / "mcp.json").read_text()
    ).get("mcpServers", {})


def test_user_uninstall_removes_only_plugin_and_preserves_project(
    tmp_path: Path,
    fake_memory: Path,
) -> None:
    project = tmp_path / "repo"
    cursor_root = tmp_path / ".cursor"
    project.mkdir()
    adapter = cursor_adapter()
    adapter.setup(project_options(project))
    project_before = snapshot_tree(project / ".cursor")
    adapter.setup(user_options(cursor_root, command=str(fake_memory)))
    result = adapter.uninstall(
        user_options(cursor_root, command=str(fake_memory))
    )
    assert result.status == "removed"
    assert snapshot_tree(project / ".cursor") == project_before
    assert not (cursor_root / "plugins/local/echovault").exists()


def test_project_uninstall_modified_owned_requires_force(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    adapter = cursor_adapter()
    options = project_options(project)
    adapter.setup(options)
    rule = project / ".cursor/rules/echovault.mdc"
    rule.write_text("user edit\n")
    before = snapshot_tree(project / ".cursor")
    with pytest.raises(OwnershipConflict):
        adapter.uninstall(options)
    assert snapshot_tree(project / ".cursor") == before
    assert adapter.uninstall(replace(options, force_managed=True)).status == "removed"


def test_project_uninstall_never_removes_custom_entry_even_with_force(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    cursor = project / ".cursor"
    cursor.mkdir(parents=True)
    (cursor / "mcp.json").write_text(
        json.dumps({"mcpServers": {"echovault": {"command": "custom"}}})
    )
    before = snapshot_tree(cursor)
    result = cursor_adapter().uninstall(
        project_options(project, force_managed=True)
    )
    assert result.status == "unchanged"
    assert snapshot_tree(cursor) == before


def test_project_uninstall_malformed_config_is_read_only_even_with_force(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    cursor = project / ".cursor"
    cursor.mkdir(parents=True)
    (cursor / "mcp.json").write_text('{"mcpServers":')
    before = snapshot_tree(cursor)
    with pytest.raises(ConfigMalformedError):
        cursor_adapter().uninstall(
            project_options(project, force_managed=True)
        )
    assert snapshot_tree(cursor) == before


def test_user_uninstall_force_removes_modified_owned_files_but_keeps_unrelated(
    tmp_path: Path,
    fake_memory: Path,
) -> None:
    cursor_root = tmp_path / ".cursor"
    adapter = cursor_adapter()
    options = user_options(cursor_root, command=str(fake_memory))
    adapter.setup(options)
    plugin = cursor_root / "plugins/local/echovault"
    (plugin / "rules/echovault.mdc").write_text("user edit\n")
    (plugin / "mine.txt").write_text("mine")
    with pytest.raises(OwnershipConflict):
        adapter.uninstall(options)
    result = adapter.uninstall(replace(options, force_managed=True))
    assert result.status == "removed"
    assert (plugin / "mine.txt").read_text() == "mine"
    assert not (plugin / "rules/echovault.mdc").exists()


def test_uninstall_without_artifacts_is_unchanged(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    result = cursor_adapter().uninstall(project_options(project))
    assert result.status == "unchanged"


def test_cursor_project_contract_from_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from memory.integrations.process import CommandResult, SubprocessRunner

    home = tmp_path / "home"
    project = tmp_path / "repo"
    home.mkdir()
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)
    monkeypatch.setattr(
        SubprocessRunner,
        "run",
        lambda self, argv, **kwargs: CommandResult(
            127,
            "",
            "agent unavailable",
        ),
    )
    result = CliRunner().invoke(main, ["setup", "cursor", "--project"])
    assert result.exit_code == 0, result.output
    doctor_result = CliRunner().invoke(main, ["doctor", "--agent", "cursor"])
    assert doctor_result.exit_code == 0, doctor_result.output
    assert "cursor.cloud-boundary" in doctor_result.output
    result = CliRunner().invoke(main, ["uninstall", "cursor", "--project"])
    assert result.exit_code == 0, result.output
    assert not (project / ".cursor/rules/echovault.mdc").exists()
