import json

from memory.integrations.asset_io import render_cursor_assets


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
