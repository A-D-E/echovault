"""Agent setup — installs hooks, skills, and configuration for supported agents."""

import json
import os
import shutil
import copy
from pathlib import Path
from typing import Any

from memory.integrations.config_io import (
    ConfigMalformedError,
    mutate_json_atomic,
    mutate_toml_atomic,
    read_json_strict,
    read_toml_strict,
)
from memory.integrations.asset_io import read_package_asset


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def _read_json(path: str) -> dict:
    """Read one JSON object without treating malformed input as empty."""
    return copy.deepcopy(read_json_strict(Path(path)).data)


def _write_json(path: str, data: dict) -> None:
    """Atomically replace one JSON object with digest-CAS protection."""
    snapshot = copy.deepcopy(data)
    mutate_json_atomic(Path(path), lambda _current: copy.deepcopy(snapshot))


# ---------------------------------------------------------------------------
# TOML helpers
# ---------------------------------------------------------------------------

def _read_toml(path: str) -> dict:
    """Read one TOML document without malformed-state fallback."""
    document = read_toml_strict(Path(path)).data
    return document.unwrap() if hasattr(document, "unwrap") else dict(document)


def _write_toml(path: str, data: dict) -> None:
    """Atomically replace one TOML document."""
    snapshot = copy.deepcopy(data)
    mutate_toml_atomic(Path(path), lambda _current: copy.deepcopy(snapshot))


def _toml_value(v: object) -> str:
    """Format a Python value as a TOML literal."""
    if isinstance(v, str):
        return f'"{v}"'
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        items = ", ".join(_toml_value(i) for i in v)
        return f"[{items}]"
    return f'"{v}"'


# ---------------------------------------------------------------------------
# Shared MCP install/uninstall helpers
# ---------------------------------------------------------------------------

MCP_CONFIG = {
    "command": "memory",
    "args": ["mcp"],
    "type": "stdio",
}

OPENCODE_MCP_CONFIG = {
    "type": "local",
    "command": ["memory", "mcp"],
}


def _install_mcp_servers(path: str) -> bool:
    """Install echovault into a JSON file under the ``mcpServers`` key.

    Used by Claude Code and Cursor.  Returns True if the entry was added.
    """
    def install(data: dict[str, Any]) -> dict[str, Any]:
        servers = data.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            raise ConfigMalformedError("mcpServers must be a JSON object")
        if "echovault" not in servers:
            servers["echovault"] = copy.deepcopy(MCP_CONFIG)
        return data

    return mutate_json_atomic(Path(path), install).changed


def _uninstall_mcp_servers(path: str) -> bool:
    """Remove echovault from a JSON ``mcpServers`` key.  Returns True if removed."""
    def uninstall(data: dict[str, Any]) -> dict[str, Any]:
        servers = data.get("mcpServers", {})
        if not isinstance(servers, dict):
            raise ConfigMalformedError("mcpServers must be a JSON object")
        servers.pop("echovault", None)
        if not servers:
            data.pop("mcpServers", None)
        return data

    return mutate_json_atomic(
        Path(path),
        uninstall,
        remove_if_empty=True,
    ).changed


def _install_toml_mcp(path: str) -> bool:
    """Install echovault into a TOML ``[mcp_servers.echovault]`` table.

    Used by Codex.  Returns True if the entry was added.
    If the existing file can't be parsed (e.g. Codex writes non-standard
    TOML keys), falls back to appending the section directly.
    """
    def install(data: Any) -> Any:
        servers = data.get("mcp_servers")
        if servers is None:
            data["mcp_servers"] = {}
            servers = data["mcp_servers"]
        if not hasattr(servers, "__setitem__"):
            raise ConfigMalformedError("mcp_servers must be a TOML table")
        if "echovault" not in servers:
            servers["echovault"] = {
                "command": "memory",
                "args": ["mcp"],
            }
        return data

    return mutate_toml_atomic(Path(path), install).changed


def _append_toml_mcp_section(path: str) -> bool:
    """Append [mcp_servers.echovault] to a TOML file without parsing it."""
    try:
        with open(path) as f:
            content = f.read()
    except FileNotFoundError:
        content = ""

    if "mcp_servers.echovault" in content:
        return False

    section = '\n[mcp_servers.echovault]\ncommand = "memory"\nargs = ["mcp"]\n'

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(section)
    return True


def _uninstall_toml_mcp(path: str) -> bool:
    """Remove echovault from a TOML ``[mcp_servers]`` table.  Returns True if removed."""
    def uninstall(data: Any) -> Any:
        servers = data.get("mcp_servers")
        if servers is None:
            return data
        if not hasattr(servers, "__delitem__"):
            raise ConfigMalformedError("mcp_servers must be a TOML table")
        if "echovault" in servers:
            del servers["echovault"]
        if not servers:
            del data["mcp_servers"]
        return data

    return mutate_toml_atomic(Path(path), uninstall).changed


def _install_opencode_mcp(path: str) -> bool:
    """Install echovault into a JSON ``mcp`` key with OpenCode schema.

    Returns True if the entry was added.
    """
    def install(data: dict[str, Any]) -> dict[str, Any]:
        mcp = data.setdefault("mcp", {})
        if not isinstance(mcp, dict):
            raise ConfigMalformedError("mcp must be a JSON object")
        if "echovault" not in mcp:
            mcp["echovault"] = copy.deepcopy(OPENCODE_MCP_CONFIG)
        return data

    return mutate_json_atomic(Path(path), install).changed


def _uninstall_opencode_mcp(path: str) -> bool:
    """Remove echovault from a JSON ``mcp`` key.  Returns True if removed."""
    def uninstall(data: dict[str, Any]) -> dict[str, Any]:
        mcp = data.get("mcp", {})
        if not isinstance(mcp, dict):
            raise ConfigMalformedError("mcp must be a JSON object")
        mcp.pop("echovault", None)
        if not mcp:
            data.pop("mcp", None)
        return data

    return mutate_json_atomic(
        Path(path),
        uninstall,
        remove_if_empty=True,
    ).changed


def _remove_old_hooks(settings: dict) -> list[str]:
    """Remove legacy EchoVault hooks from settings. Returns list of removed event names."""
    hooks = settings.get("hooks", {})
    removed = []
    memory_fragments = ("memory context", "memory auto-save")

    for event in list(hooks.keys()):
        event_hooks = hooks[event]
        filtered = [
            group for group in event_hooks
            if not any(
                any(frag in h.get("command", "") for frag in memory_fragments)
                for h in group.get("hooks", [])
            )
        ]
        if len(filtered) != len(event_hooks):
            removed.append(event)
            if filtered:
                hooks[event] = filtered
            else:
                del hooks[event]

    if not hooks and "hooks" in settings:
        del settings["hooks"]

    return removed


def _install_skill(agent_home: str, agent_name: str = "agent") -> bool:
    """Install the echovault SKILL.md into an agent's skills directory.

    Args:
        agent_home: Path to the agent's config directory (e.g. ~/.claude).

    Returns:
        True if skill was installed or refreshed, False if already current.
    """
    skill_dir = os.path.join(agent_home, "skills", "echovault")
    skill_path = os.path.join(skill_dir, "SKILL.md")

    content = read_package_asset(
        "common/echovault-skill.md"
    ).decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    content = (
        content.replace("{{AGENT_NAME}}", agent_name)
        .replace("{{VERSION}}", "0.6.0")
        .replace("<!-- echovault:unbound-compatibility:start -->\n", "")
        .replace("<!-- echovault:unbound-compatibility:end -->\n", "")
    )

    try:
        with open(skill_path) as f:
            if f.read() == content:
                return False
    except FileNotFoundError:
        pass

    os.makedirs(skill_dir, exist_ok=True)
    with open(skill_path, "w") as f:
        f.write(content)

    return True


def _uninstall_skill(agent_home: str) -> bool:
    """Remove the echovault skill from an agent's skills directory.

    Returns:
        True if skill was removed, False if not found.
    """
    skill_dir = os.path.join(agent_home, "skills", "echovault")
    if os.path.islink(skill_dir):
        os.remove(skill_dir)
        return True
    if os.path.exists(skill_dir):
        shutil.rmtree(skill_dir)
        return True
    return False


def _get_claude_mcp_path(claude_home: str, project: bool) -> str:
    """Return the correct MCP config path for Claude Code.

    Project scope: <project_root>/.mcp.json
    Global scope:  ~/.claude.json
    """
    if project:
        project_root = os.path.dirname(claude_home)
        return os.path.join(project_root, ".mcp.json")
    return os.path.join(os.path.expanduser("~"), ".claude.json")


def setup_claude_code(claude_home: str, *, project: bool = False) -> dict[str, str]:
    """Install EchoVault MCP server into Claude Code."""
    installed = []

    # Clean old hooks from settings.json if present
    settings_path = os.path.join(claude_home, "settings.json")
    if os.path.exists(settings_path):
        settings = _read_json(settings_path)
        removed = _remove_old_hooks(settings)
        if removed:
            installed.append(f"removed old hooks: {', '.join(removed)}")
        # Remove mcpServers from settings.json (moved to dedicated config)
        if "mcpServers" in settings and "echovault" in settings["mcpServers"]:
            del settings["mcpServers"]["echovault"]
            if not settings["mcpServers"]:
                del settings["mcpServers"]
            installed.append("migrated mcpServers from settings.json")
        _write_json(settings_path, settings)

    # Install or refresh behavioral instructions alongside native MCP tools.
    if _install_skill(claude_home, "claude-code"):
        installed.append("skill")

    # Add MCP server config
    mcp_path = _get_claude_mcp_path(claude_home, project)
    if _install_mcp_servers(mcp_path):
        scope = ".mcp.json" if project else "~/.claude.json"
        installed.append(f"mcpServers in {scope}")

    if installed:
        return {"status": "ok", "message": f"Installed: {', '.join(installed)}"}
    return {"status": "ok", "message": "Already installed"}


def setup_cursor(cursor_home: str) -> dict[str, str]:
    """Install the managed EchoVault local plugin for Cursor."""
    from memory.integrations.registry import get_adapter
    from memory.integrations.types import (
        InstallMode,
        InstallScope,
        IntegrationOptions,
    )

    result = get_adapter("cursor").setup(
        IntegrationOptions(
            scope=InstallScope.USER,
            mode=InstallMode.NATIVE,
            config_root=Path(cursor_home),
            project_root=None,
            command=None,
            config_root_explicit=True,
        )
    )
    return {"status": "ok", "message": result.message}


def setup_gemini(
    gemini_home: str,
    *,
    direct: bool = False,
    project_root: str | None = None,
    command: str | None = None,
    force_managed: bool = False,
) -> dict[str, str]:
    """Install one managed Gemini integration through the canonical adapter."""
    from memory.integrations.registry import get_adapter
    from memory.integrations.types import (
        InstallMode,
        InstallScope,
        IntegrationOptions,
    )

    project = Path(project_root).expanduser().resolve() if project_root else None
    result = get_adapter("gemini").setup(
        IntegrationOptions(
            scope=(
                InstallScope.PROJECT if project is not None else InstallScope.USER
            ),
            mode=(
                InstallMode.DIRECT
                if direct or project is not None
                else InstallMode.NATIVE
            ),
            config_root=Path(gemini_home).expanduser().resolve(),
            project_root=project,
            command=command,
            force_managed=force_managed,
            config_root_explicit=True,
        )
    )
    return {"status": "ok", "message": result.message}


CODEX_AGENTS_MD_SECTION = """\

## EchoVault — Persistent Memory

You have persistent memory across sessions. Use it.

### Session start — MANDATORY

Before planning or implementing a feature, retrieve task-relevant context (unless
`memory config context` reports `off`):

```bash
memory context --project --agent codex --query "<current task or feature request>"
```

Search for relevant memories:

```bash
memory search "<relevant terms>"
```

When results show "Details: available", fetch them:

```bash
memory details <memory-id>
```

### Session end — MANDATORY

Before finishing any task that involved changes, debugging, decisions, or learning, save a memory:

```bash
memory save \\
  --title "Short descriptive title" \\
  --what "What happened or was decided" \\
  --why "Reasoning behind it" \\
  --impact "What changed as a result" \\
  --tags "tag1,tag2,tag3" \\
  --category "decision" \\
  --related-files "path/to/file1,path/to/file2" \\
  --source "codex" \\
  --details "Context:

             Options considered:
             - Option A
             - Option B

             Decision:
             Tradeoffs:
             Follow-up:"
```

Categories: `decision`, `bug`, `pattern`, `learning`, `context`.

### Rules

- Retrieve before working. Save before finishing. No exceptions.
- Never include API keys, secrets, or credentials.
- Search before saving to avoid duplicates.
"""


def setup_codex(codex_home: str) -> dict[str, str]:
    """Install EchoVault into Codex (AGENTS.md + MCP config).

    Writes memory instructions to AGENTS.md as a fallback and installs
    ``[mcp_servers.echovault]`` into config.toml for native MCP support.

    Args:
        codex_home: Path to the .codex directory (e.g. ~/.codex).

    Returns:
        Dict with 'status' and 'message' keys.
    """
    installed = []

    # AGENTS.md (fallback for agents that don't use MCP tools)
    agents_path = os.path.join(codex_home, "AGENTS.md")
    existing = ""
    try:
        with open(agents_path) as f:
            existing = f.read()
    except FileNotFoundError:
        pass

    if "## EchoVault" not in existing:
        os.makedirs(os.path.dirname(agents_path), exist_ok=True)
        with open(agents_path, "w") as f:
            f.write(existing.rstrip("\n") + "\n" + CODEX_AGENTS_MD_SECTION)
        installed.append("AGENTS.md")

    # MCP config in config.toml
    toml_path = os.path.join(codex_home, "config.toml")
    if _install_toml_mcp(toml_path):
        installed.append("config.toml")

    # Skill (legacy)
    if _install_skill(codex_home, "codex"):
        installed.append("skill")

    if not installed:
        return {"status": "ok", "message": "Already installed"}

    msg = f"Installed: {', '.join(installed)}"
    msg += "\nNote: Auto-persist (Stop hook) is only available for Claude Code. Codex relies on AGENTS.md instructions for saving."
    return {"status": "ok", "message": msg}


def uninstall_claude_code(claude_home: str, *, project: bool = False) -> dict[str, str]:
    """Remove EchoVault from Claude Code."""
    removed = []

    # Remove from the target scope
    mcp_path = _get_claude_mcp_path(claude_home, project)
    if _uninstall_mcp_servers(mcp_path):
        removed.append(f"mcpServers from {os.path.basename(mcp_path)}")

    # Also clean legacy locations
    settings_path = os.path.join(claude_home, "settings.json")
    if os.path.exists(settings_path):
        settings = _read_json(settings_path)
        if "mcpServers" in settings and "echovault" in settings["mcpServers"]:
            del settings["mcpServers"]["echovault"]
            if not settings["mcpServers"]:
                del settings["mcpServers"]
            removed.append("legacy mcpServers from settings.json")
        old_removed = _remove_old_hooks(settings)
        removed.extend(old_removed)
        _write_json(settings_path, settings)

    # Remove old skill
    if _uninstall_skill(claude_home):
        removed.append("skill")

    if removed:
        return {"status": "ok", "message": f"Removed: {', '.join(removed)}"}
    return {"status": "ok", "message": "Nothing to remove"}


def uninstall_cursor(cursor_home: str) -> dict[str, str]:
    """Remove the managed EchoVault local plugin from Cursor."""
    from memory.integrations.registry import get_adapter
    from memory.integrations.types import (
        InstallMode,
        InstallScope,
        IntegrationOptions,
    )

    result = get_adapter("cursor").uninstall(
        IntegrationOptions(
            scope=InstallScope.USER,
            mode=InstallMode.NATIVE,
            config_root=Path(cursor_home),
            project_root=None,
            command=None,
            config_root_explicit=True,
        )
    )
    return {"status": "ok", "message": result.message}


def uninstall_gemini(
    gemini_home: str,
    *,
    direct: bool = False,
    project_root: str | None = None,
    force_managed: bool = False,
) -> dict[str, str]:
    """Remove one managed Gemini integration through the canonical adapter."""
    from memory.integrations.registry import get_adapter
    from memory.integrations.types import (
        InstallMode,
        InstallScope,
        IntegrationOptions,
    )

    project = Path(project_root).expanduser().resolve() if project_root else None
    result = get_adapter("gemini").uninstall(
        IntegrationOptions(
            scope=(
                InstallScope.PROJECT if project is not None else InstallScope.USER
            ),
            mode=(
                InstallMode.DIRECT
                if direct or project is not None
                else InstallMode.NATIVE
            ),
            config_root=Path(gemini_home).expanduser().resolve(),
            project_root=project,
            command=None,
            force_managed=force_managed,
            config_root_explicit=True,
        )
    )
    return {"status": "ok", "message": result.message}


def uninstall_codex(codex_home: str) -> dict[str, str]:
    """Remove EchoVault from Codex (AGENTS.md + config.toml)."""
    import re

    removed = []

    # Remove AGENTS.md section
    agents_path = os.path.join(codex_home, "AGENTS.md")
    try:
        with open(agents_path) as f:
            content = f.read()
    except FileNotFoundError:
        content = ""

    if "## EchoVault" in content:
        cleaned = re.sub(
            r"\n*## EchoVault[^\n]*\n.*?(?=\n## |\Z)",
            "",
            content,
            flags=re.DOTALL,
        )
        with open(agents_path, "w") as f:
            f.write(cleaned.strip() + "\n")
        removed.append("AGENTS.md")

    # Remove config.toml MCP entry
    toml_path = os.path.join(codex_home, "config.toml")
    if _uninstall_toml_mcp(toml_path):
        removed.append("config.toml")

    if _uninstall_skill(codex_home):
        removed.append("skill")

    if removed:
        return {"status": "ok", "message": f"Removed: {', '.join(removed)}"}
    return {"status": "ok", "message": "Nothing to remove"}


# ---------------------------------------------------------------------------
# OpenCode
# ---------------------------------------------------------------------------

def _get_opencode_mcp_path(project: bool) -> str:
    """Return the correct MCP config path for OpenCode.

    Project scope: <cwd>/opencode.json
    Global scope:  ~/.config/opencode/opencode.json
    """
    if project:
        return os.path.join(os.getcwd(), "opencode.json")
    return os.path.join(os.path.expanduser("~"), ".config", "opencode", "opencode.json")


def setup_opencode(*, project: bool = False) -> dict[str, str]:
    """Install EchoVault MCP server into OpenCode."""
    mcp_path = _get_opencode_mcp_path(project)
    if _install_opencode_mcp(mcp_path):
        scope = "opencode.json" if project else "~/.config/opencode/opencode.json"
        return {"status": "ok", "message": f"Installed: mcp in {scope}"}
    return {"status": "ok", "message": "Already installed"}


def uninstall_opencode(*, project: bool = False) -> dict[str, str]:
    """Remove EchoVault from OpenCode."""
    mcp_path = _get_opencode_mcp_path(project)
    if _uninstall_opencode_mcp(mcp_path):
        scope = "opencode.json" if project else "~/.config/opencode/opencode.json"
        return {"status": "ok", "message": f"Removed: mcp from {scope}"}
    return {"status": "ok", "message": "Nothing to remove"}
