# Cursor integration

EchoVault supports the Cursor IDE agent, Cursor Agent CLI, and local sessions
started from the Agents Window. They share Cursor's local MCP configuration and
the same bound `cursor` identity. A true Cursor Cloud Agent is a separate
machine: the project integration, package, and vault must be provisioned there;
EchoVault never copies the local vault to the cloud.

## Install and inspect

The recommended user-wide setup is a managed local plugin:

```bash
memory setup cursor
memory doctor --agent cursor
```

It lives at `~/.cursor/plugins/local/echovault/`. Reload or restart Cursor after
setup. To verify MCP discovery with a compatible Cursor Agent CLI, run:

```bash
agent mcp list
agent mcp list-tools echovault
```

For a portable repository fallback, run from the project root:

```bash
memory setup cursor --project
memory doctor --agent cursor --project-root "$PWD"
```

Project setup owns only its entries and marked assets in `.cursor/mcp.json`,
`.cursor/rules/echovault.mdc`, `.cursor/skills/echovault/SKILL.md`, and
`.cursor/.echovault-managed.json`. Project MCP configuration takes precedence
inside that repository; it may coexist with the global plugin.

Use an exact path only when automatic discovery is unsuitable:

```bash
memory setup cursor --config-dir /absolute/path/to/.cursor \
  --command /absolute/path/to/memory
memory setup cursor --project --config-dir /repo/.cursor \
  --command memory
```

The global command must resolve to an executable. A project command may stay
portable when the target environment provides `memory` on `PATH`.

## Memory behavior and evidence

The installed always-applied rule requires task-start `memory_context` and a
task-end `memory_save` only for curated durable decisions, fixes, patterns,
constraints, or project state. The four MCP tools are project-bound; agent,
source, and project identity cannot be spoofed by model arguments, and save
retries use one UUID idempotency key.

This retrieval is **policy-guided**. EchoVault deterministically verifies the
plugin assets, ownership hashes, executable, bound MCP definition, four-tool
inventory, and effective context policy. Cursor's model still decides whether
to issue the instructed MCP call, so an authenticated observed turn is required
to claim model compliance. Installation or `memory doctor` alone is not that
evidence.

## Upgrade, conflicts, and removal

`memory setup cursor` automatically migrates only the exact known legacy
EchoVault `mcpServers.echovault` shape. Custom same-named entries, malformed
JSON, unowned plugin trees, and symlink boundaries are reported without being
changed. Back up the affected Cursor files, inspect them, then use
`--force-managed` only when `memory doctor --agent cursor` reports modified
content already claimed by EchoVault:

```bash
memory setup cursor --force-managed
memory setup cursor --project --force-managed
```

Force never authorizes replacement of custom or unowned state. Uninstall is
scope-exact and preserves unrelated JSON entries, rules, skills, and files:

```bash
memory uninstall cursor
memory uninstall cursor --project
```

Add `--force-managed` to uninstall only when deliberately removing modified
EchoVault-owned content. Removing one scope does not remove the other.
