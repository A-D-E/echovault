# Gemini CLI integration

EchoVault supports three Gemini CLI targets. The compact **N/U/P** state names
mean native user extension (`N`), user-direct configuration (`U`), and
project-direct configuration (`P`). Native is recommended; direct modes are
explicit fallbacks.

## Install and inspect

```bash
# N: managed native extension, with Gemini's normal consent prompt
memory setup gemini

# U: user-direct fallback in ~/.gemini
memory setup gemini --direct

# P: portable fallback in the current repository
memory setup gemini --project

memory doctor --agent gemini-cli
memory doctor --agent gemini-cli --project-root /absolute/project
```

`N` and `U` are mutually exclusive in the same user scope. `P` can coexist
with either and takes MCP precedence in its project. Normal Gemini extension
install and trust/consent prompts remain visible; EchoVault does not bypass
them. Restart the Gemini session after changing a target.

User-direct writes only owned entries/assets under `~/.gemini`. Project-direct
uses `.gemini/settings.json`, `.gemini/skills/echovault/SKILL.md`, its ownership
manifest, and a marked block in `GEMINI.md`. On Gemini CLI older than 0.50.0,
direct MCP and static context still work but the named hook is reported as
degraded.

## BeforeAgent retrieval

On supported clients, the named `BeforeAgent` hook reads one validated event,
uses only the prompt and project root for task-aware retrieval, and emits one
JSON response. It never opens the supplied transcript path. A ten-minute claim
stores only a digest, behavior version, and expiry so valid native and project
hooks inject and record feedback at most once for the same event.

The hook is deterministic when it succeeds: context is injected before the
model turn. Invalid input, timeout, disabled context, or unavailable memory
fails open with `{}` and sanitized stderr diagnostics so Gemini can continue.
The model may independently choose whether to call an MCP tool later.

Repository tests and extension validation use no Gemini account. Until an
authenticated turn is observed, model-session behavior is **not verified**;
installed assets and a green doctor are deterministic evidence only.

## Conflicts, force, and removal

Malformed configuration, custom same-named entries, unowned extension trees,
and symlink boundaries are read-only conflicts. `--force-managed` may update or
remove only content named and hashed by EchoVault's ownership manifest; it does
not take ownership of custom state.

```bash
memory setup gemini --force-managed
memory setup gemini --direct --force-managed
memory setup gemini --project --force-managed

memory uninstall gemini
memory uninstall gemini --direct
memory uninstall gemini --project
```

Select the same target flags for setup and uninstall. Removing `N` or `U` does
not remove `P`, and removing `P` leaves the global target intact. Unrelated
settings and context outside EchoVault's marked block are preserved.
