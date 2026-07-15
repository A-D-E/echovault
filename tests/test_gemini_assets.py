import json
from importlib.resources import files

from memory.integrations.asset_io import (
    render_gemini_assets,
    shell_join_command,
)


def test_rendered_gemini_extension_has_bound_mcp_hook_context_and_skill() -> None:
    assets = render_gemini_assets(
        memory_command="/opt/bin/memory",
        hook_command="/opt/bin/memory hook gemini before-agent",
        version="0.6.0",
    )
    manifest = json.loads(assets["gemini-extension.json"])
    hooks = json.loads(assets["hooks/hooks.json"])
    assert manifest["name"] == "echovault"
    assert manifest["mcpServers"]["echovault"]["args"] == [
        "mcp",
        "--agent",
        "gemini-cli",
    ]
    assert manifest["contextFileName"] == "GEMINI.md"
    command = hooks["hooks"]["BeforeAgent"][0]["hooks"][0]["command"]
    assert "hook gemini before-agent" in command
    assert hooks["hooks"]["BeforeAgent"][0]["hooks"][0]["timeout"] == 5000
    assert b"memory_context" in assets["GEMINI.md"]
    skill = assets["skills/echovault/SKILL.md"].decode()
    assert "memory_save" in skill
    for forbidden in (
        "source=cursor",
        "source=gemini",
        "agent=cursor",
        "agent=gemini",
        "project=",
    ):
        assert forbidden not in skill


def test_gemini_hook_resource_maps_to_native_extension_layout() -> None:
    resource = files("memory.integrations.assets").joinpath("gemini/hooks.json")
    assert resource.is_file()
    assets = render_gemini_assets(
        memory_command="memory",
        hook_command="memory hook gemini before-agent",
        version="0.6.0",
    )
    assert "hooks/hooks.json" in assets
    assert "gemini/hooks.json" not in assets


def test_hook_command_join_keeps_spaced_executable_as_one_argument() -> None:
    command = shell_join_command(
        ["/opt/Echo Vault/bin/memory", "hook", "gemini", "before-agent"]
    )
    assert "Echo Vault" in command
    assert command != "/opt/Echo Vault/bin/memory hook gemini before-agent"
