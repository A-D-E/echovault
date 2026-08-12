# Cross-Agent Native Integrations for EchoVault

**Status:** Approved in conversation, including the canonical Rust dashboard write-path addendum

**Date:** 2026-07-14

**Repository:** `A-D-E/echovault`

**Baseline:** upstream/fork commit `737c503` (v0.5.0), 272 Python tests passing

## Summary

EchoVault will support Cursor IDE, Cursor CLI, and Gemini CLI through native
integration packages layered over one agent-neutral local memory core.

The selected design is a native hybrid:

- versioned Markdown remains the canonical memory record and SQLite/FTS/vector
  data remains a rebuildable derived index;
- the stdio MCP server remains the portable capability boundary;
- Cursor receives a native plugin plus a project-local fallback;
- Gemini receives a native extension plus a direct-settings fallback;
- Gemini task context is injected deterministically by a hook when supported;
- Cursor task context is policy-guided through an always-applied rule because
  Cursor exposes no reliable mechanism that can force a model tool call;
- durable memories remain curated, explicit saves;
- full conversations and transcripts are never ingested by default;
- provider hooks accelerate supported workflows but are not persistence
  transaction boundaries.

The adapter boundary is intentionally reusable for later integrations such as
Antigravity or additional MCP-capable coding agents.

## Problem

EchoVault currently advertises four agent integrations, but their behavior is
not equivalent.

- Claude Code receives MCP configuration and an EchoVault skill.
- Codex receives MCP configuration, AGENTS.md instructions, and a skill.
- OpenCode receives MCP configuration only.
- Cursor receives MCP configuration only. Its setup function also removes any
  existing EchoVault skill and legacy context hooks.
- Gemini has no setup or uninstall support.

MCP registration makes tools available, but it does not guarantee that an agent
retrieves task context before work or saves durable learning before finishing.
This is the direct cause of EchoVault appearing inactive in Cursor.

The audit also found adjacent issues that materially affect cross-agent use:

- `memory_save` over MCP cannot record the saving agent as `source`;
- the published skill refers to a `memory_details` MCP tool that does not exist;
- project identity falls back to the current directory basename, which is wrong
  when an agent runs from a nested directory;
- simultaneous processes can overwrite one another during the Markdown
  read-modify-write cycle;
- malformed JSON configuration is currently treated as empty configuration and
  can be overwritten;
- built wheels omit the canonical skill and native integration assets;
- the repository has no hosted CI.

## Goals

1. Support local Cursor IDE and Cursor CLI with the same EchoVault behavior.
2. Support Gemini CLI with task-aware context injection.
3. Keep one local vault shared by Claude Code, Codex, Cursor, Gemini, and
   OpenCode.
4. Preserve curated persistence: save durable decisions, bugs, patterns,
   constraints, project state, and learnings, not full transcripts.
5. Provide global and project-local installation modes.
6. Make setup, repeated setup, diagnosis, migration, and uninstall safe and
   observable.
7. Preserve existing Claude Code, Codex, and OpenCode behavior.
8. Prevent lost writes when multiple agents save concurrently.
9. Package every required rule, skill, hook, and manifest in built artifacts.
10. Establish a small adapter contract for future agent integrations.

## Non-goals

- Remote synchronization or access from Cursor Cloud Agents.
- Cursor Tab completion, Cmd-K/Inline Edit, Background Agents, and multi-root
  workspaces. Phase one targets Cursor Agent chat in the IDE plus interactive
  and headless Cursor CLI sessions in one workspace root.
- Automatic storage of full prompts, responses, or transcripts.
- Replacing Cursor Memories, Gemini memory files, Claude auto-memory, or Codex
  native memories.
- A web service, daemon, or always-on background process.
- New Rust dashboard features or a dashboard UI redesign. Its existing mutation
  actions must delegate to the canonical persistence coordinator so they cannot
  bypass schema-v2 Markdown.
- A new embedding provider or embedding-model calibration.
- Publishing a release, tag, marketplace listing, or upstream pull request as
  part of implementation. Those are separate completion decisions.

## Selected Approach

### Native hybrid

EchoVault keeps a portable MCP core and adds native packaging for each client.
This approach was selected over:

1. **Instructions only.** Simpler, but weaker discovery and less reliable
   startup behavior.
2. **Hooks first.** Gemini supports useful prompt-time context injection, but
   Cursor lifecycle hooks still have missing execution paths and cannot be the
   correctness boundary.
3. **Transcript ingestion.** Mechanically automatic, but lower signal, more
   private data, more duplicates, and inconsistent with EchoVault's current
   operating model.

## Architecture

~~~mermaid
flowchart LR
    Task["Current user task"] --> Cursor["Cursor plugin or project adapter"]
    Task --> Gemini["Gemini extension or settings adapter"]
    Task --> Existing["Claude, Codex, and OpenCode adapters"]

    Cursor --> MCP["EchoVault stdio MCP<br/>with default agent identity"]
    Gemini --> MCP
    Existing --> MCP

    MCP --> Service["MemoryService"]
    Service --> Markdown["Canonical versioned Markdown vault"]
    Markdown --> SQLite["Derived SQLite, FTS5, and sqlite-vec index"]
    SQLite --> Service
~~~

### Boundaries

- `MemoryService` owns redaction, deduplication, canonical Markdown persistence,
  derived indexing, and retrieval.
- Every existing mutation surface, including the Rust dashboard, delegates to
  `MemoryService`; the dashboard keeps its current UI and SQLite read model.
- The MCP server owns agent-neutral tool contracts plus configured agent and
  project authority.
- Agent adapters own native paths, manifests, instructions, hooks, setup,
  uninstall, and diagnostics.
- Native hooks never write raw transcripts to the vault.

## Canonical Agent Identities

| Client | Canonical identity |
|---|---|
| Claude Code | `claude-code` |
| Codex | `codex` |
| Cursor IDE and Cursor CLI | `cursor` |
| Gemini CLI | `gemini-cli` |
| OpenCode | `opencode` |

Cursor IDE and Cursor CLI deliberately share one identity so agent-specific
context policy does not fragment by surface. Surface-specific diagnostics may
still report `cursor-ide` or `cursor-cli` without changing stored `source`.

## MCP Contract

### Agent-bound server startup

Add an optional agent identity to the MCP entry point:

~~~text
memory mcp --agent cursor
memory mcp --agent gemini-cli
memory mcp --agent cursor --project-root /absolute/project
~~~

`MEMORY_AGENT` may provide the same value for environments that cannot express
arguments conveniently. Explicit command-line input wins over the environment.
Running `memory mcp` without either remains backward compatible.
`--project-root` is optional and makes project authority explicit for managed,
non-portable installations; portable project files rely on one negotiated MCP
Root.

The configured identity becomes:

- the authoritative `agent` for `memory_context`;
- the authoritative `source` for `memory_save`.

On an agent-bound server, an omitted tool field uses that identity and a
different caller-supplied identity is rejected as a validation error. A model
therefore cannot spoof cross-agent provenance. On an unbound legacy
`memory mcp` server, callers may still supply an explicit non-empty identity.
Adapter-generated values use the canonical table; existing custom source values
remain valid for backward compatibility.

### Tools

| Tool | Contract |
|---|---|
| `memory_context` | Retrieve task-aware, token-budgeted context. Uses the bound agent and rejects a conflicting override; unbound servers retain the optional field. |
| `memory_search` | Perform project-scoped keyword or semantic search for a narrower topic on a bound server; legacy unbound use remains available. |
| `memory_details` | Return full details for a memory ID or unique prefix only when it belongs to the effective project on a bound server. |
| `memory_save` | Save one curated memory. Uses the bound source and rejects a conflicting override; unbound servers retain optional `source`. Agent-bound servers require an `idempotency_key`. |

Existing tool names and fields remain compatible. New fields are optional.
The sole conditional exception is `idempotency_key`: it remains optional for an
unbound backward-compatible server and is required for an agent-bound adapter.
Adapter instructions generate one UUID per conceptual save and reuse it for the
single allowed retry. Responses distinguish `created`, `updated`, and
`replayed`.

### Project root and identity

CLI, hooks, and MCP use one shared root resolver. It selects a candidate path in
this order:

1. an authoritative `--project-root PATH` configured by the adapter;
2. the single root advertised through the MCP Roots protocol;
3. a validated `cwd` supplied by a native hook or MCP tool call;
4. the MCP server's startup directory;
5. the current working directory as the final fallback.

From that candidate it walks upward to the nearest `.git`, then
`pyproject.toml` or `package.json` marker. Cursor officially supports MCP Roots,
so both its global plugin and project MCP can identify a single open workspace
without relying only on process cwd or model-supplied text.

The resolver returns a `ProjectIdentity` with separate `root`, `display_name`,
and `key` fields. The key is
`<slug(identity-name)>--<12-char-sha256>`. For Git, both `identity_name` and the
hash seed come from the normalized canonical common-directory owner, so local
worktrees share one key even when their checkout-directory names differ. For a
non-Git project, the identity name is the root name and the hash seed is the
normalized canonical root path. `display_name` remains the current workspace's
human-facing name. This avoids collisions between unrelated projects with the
same directory name without storing the absolute path in vault Markdown or
SQLite.

A local registry at `MEMORY_HOME/projects.json` records key, display name, and
canonical roots for diagnostics and aliases. It is not injected into agent
context or synchronized as vault content. Registry updates use their own
cross-process lock and atomic compare-and-swap. On first use, a sole legacy
project whose key is exactly the display-name basename is adopted as a read
alias for the first matching registered root. A later same-name root receives
only its hashed key. Ambiguous legacy ownership is reported by doctor and
requires an explicit reassignment; it is never silently exposed to both
projects. New saves always use the hashed key, while alias-aware retrieval keeps
adopted legacy memories readable.

The explicit command is
`memory project adopt-legacy LEGACY_KEY --project-root PATH`. It refuses an
already assigned alias unless `--force-reassign` is present and never moves or
rewrites Markdown; it only changes which hashed project key reads the legacy
key. Doctor shows both affected display names before suggesting reassignment.

The MCP server negotiates Roots and all four tools gain optional `cwd` input for
clients that do not advertise one. Search and details are constrained to the
resolved project and adopted aliases just like context and save; a cross-project
details ID is returned as not found. A project-bound server rejects a
conflicting caller-supplied project or path. A project-unbound global adapter
server prefers the single client root, then resolves `cwd`, then falls back to
its startup directory. Existing explicit `project` string input remains
available only on a backward-compatible legacy server started without agent or
project binding. Phase one has no implicit multi-root routing. A project-bound
server treats its configured root as authoritative; advertised roots and caller
cwd may only confirm that boundary. A global agent-bound server may use exactly
one advertised root directly. When more than one root is advertised, the call
must provide cwd and that cwd must be contained by exactly one advertised root;
otherwise the call is rejected as ambiguous. With zero roots, cwd may select a
project only when it is outside a generic home/startup directory; otherwise the
adapter must supply an explicit project root. No basename or first-root
fallback is allowed.

For an agent-bound MCP server, caller-supplied cwd must be inside its configured
project root, the single advertised root, or the exactly one advertised root
that contains it. When the client exposes no Roots capability, cwd must remain
inside the startup-resolved project unless the startup directory is a generic
home and cwd provides the only project boundary. Paths outside that boundary
are rejected. Gemini's hook
cwd is trusted as client protocol input, not copied from model-generated tool
arguments.

Root discovery reads filesystem metadata only and never executes repository
hooks or project code.

## Adapter Contract

Introduce a focused integration layer under `src/memory/integrations/`.

Each adapter exposes:

- canonical identity;
- supported global and project paths;
- setup operation;
- uninstall operation;
- diagnostic checks;
- packaged native assets;
- capability flags for MCP, rules, skills, extensions, and hooks.

Existing public setup functions remain as compatibility wrappers. Claude Code,
Codex, and OpenCode may delegate through the shared helpers, but their observable
behavior must not change in this phase.

The integration registry must not contain storage or retrieval business logic.

### Phase-one command matrix

| Command | Scope and target |
|---|---|
| `memory setup cursor` | User Cursor local plugin |
| `memory setup cursor --project` | Current workspace `.cursor` fallback |
| `memory uninstall cursor` | User Cursor local plugin |
| `memory uninstall cursor --project` | Current workspace `.cursor` fallback |
| `memory setup gemini` | User Gemini native extension |
| `memory setup gemini --direct` | User `.gemini` direct fallback |
| `memory setup gemini --project` | Current workspace `.gemini` direct fallback |
| `memory uninstall gemini` | User Gemini native extension |
| `memory uninstall gemini --direct` | User `.gemini` direct fallback |
| `memory uninstall gemini --project` | Current workspace `.gemini` direct fallback |

All setup variants accept `--command PATH`; all setup/uninstall variants accept
`--force-managed`. Cursor retains `--config-dir DIR` in either scope. Gemini
accepts `--config-dir DIR` only with `--direct` or `--project` because native
extension scope belongs to Gemini's own manager. For Gemini, `--project`
implicitly selects direct mode, so adding `--direct` is accepted but redundant.

## Cursor Design

### Global installation

`memory setup cursor` installs a packaged local plugin at
`~/.cursor/plugins/local/echovault/` (or the platform/config-directory
equivalent) with a valid `.cursor-plugin/plugin.json` manifest. The plugin
bundles:

- an MCP server definition using `memory mcp --agent cursor`;
- an always-applied EchoVault rule;
- the EchoVault skill.

The local-plugin path is the phase-one distribution mechanism and supports
dogfooding without requiring marketplace publication. Setup reports that an
already-running Cursor window or CLI session must be restarted or reloaded.
Marketplace publication remains a separate release decision.

Cursor discovers that user-level local-plugin directory directly after reload;
EchoVault writes no private Cursor registry. Setup validates the manifest and
all declared component paths before success. Doctor then performs a structural
check and, when the `agent` CLI is installed, a read-only `agent mcp list` plus
`agent mcp list-tools echovault` runtime check. IDE loading remains part of the
authenticated smoke gate because it cannot be proven from files alone.

`memory uninstall cursor` removes only a plugin directory carrying EchoVault's
manifest identity and ownership marker. `memory uninstall cursor --project`
removes only the project MCP entry, marked rule, and EchoVault skill. An
unmarked pre-existing plugin directory named `echovault` is a setup conflict.
The existing `--config-dir DIR` option remains supported and treats `DIR` as
the `.cursor` configuration root for both setup and uninstall.

No Cursor lifecycle hook is required for correctness in phase one. Cursor hooks
may be added later behind capability checks, but task-aware retrieval and
curated saving must work through the rule, skill, and MCP tools.

“Work” here means the integration is installed, discoverable, and gives the
Agent an always-applied instruction to retrieve memory. Cursor still controls
model execution, so no design can guarantee that every model follows the rule
and calls an MCP tool. Deterministic contract tests verify availability and
instruction injection; authenticated client smoke tests measure actual marker
retrieval without treating model compliance as a transactional guarantee.

### Project installation

`memory setup cursor --project` installs version-controllable files:

- `.cursor/mcp.json`;
- `.cursor/rules/echovault.mdc`;
- `.cursor/skills/echovault/SKILL.md`.

The project rule is always applied and instructs Cursor to:

1. call `memory_context` with the current request before substantive planning,
   debugging, architecture, or implementation;
2. use `memory_search` and `memory_details` only when the context pack is
   insufficient;
3. call `memory_save` before the final response when durable learning exists;
4. generate one operation UUID per conceptual save and reuse it for the one
   allowed retry;
5. skip trivial or duplicate memories;
6. never store secrets or entire transcripts.

Cursor IDE and Cursor CLI both consume the same project rules and MCP
configuration.

Global and project setup must each leave only one effective EchoVault MCP
server. A verified native plugin supplies the global entry; the project
fallback supplies the workspace entry.

### Executable paths

- Global, machine-local configuration uses the resolved absolute EchoVault
  executable path for reliable GUI startup.
- Project configuration uses the portable `memory` command by default and
  `memory doctor --agent cursor` verifies that it resolves in the target
  environment.
- Both modes accept `--command PATH`; setup validates the executable and writes
  that exact path for managed environments.

### Migration

The existing exact legacy direct entry:

~~~json
{
  "command": "memory",
  "args": ["mcp"],
  "type": "stdio"
}
~~~

is owned by EchoVault. During global setup it is removed only after the native
plugin has been installed and validated, preventing duplicate global servers.
During project setup it is upgraded in place to include agent identity. A
different user-managed `echovault` entry is a conflict: setup reports it and
does not install a second entry or overwrite the custom one silently.

The new setup installs or refreshes the skill rather than deleting it.

## Gemini CLI Design

### Native extension

Ship a Gemini extension containing:

- `gemini-extension.json`;
- MCP configuration using `memory mcp --agent gemini-cli`;
- a minimal static `GEMINI.md`;
- `skills/echovault/SKILL.md`;
- `hooks/hooks.json` with a `BeforeAgent` command hook.

`memory setup gemini` renders the packaged assets into the owned, stable local
source directory
`~/.config/echovault/integrations/gemini-extension/echovault/` (or the platform
configuration equivalent), then uses
Gemini's supported `gemini extensions install <local-path>` mechanism for the
extension named `echovault`. Setup does not bypass Gemini's consent prompt. A
repeated setup is a no-op when source and installed versions/hashes match; after
an EchoVault upgrade it refreshes the owned source and calls
`gemini extensions update echovault`. `memory uninstall gemini` delegates to
`gemini extensions uninstall echovault`, verifies absence, and then removes an
unmodified owned source directory.

Before invoking Gemini's installer, setup renders every command-bearing
machine-local extension asset with the resolved absolute EchoVault executable
path, including MCP and hook commands. The checked-in and packaged templates
remain portable; the installed copy is machine-specific.
User direct mode also uses the resolved absolute executable; direct project mode
uses the portable `memory` command by default. All Gemini setup modes accept the
same validated `--command PATH` override.

### Prompt-time context hook

The hook invokes a cross-platform Python CLI handler:

~~~text
memory hook gemini before-agent
~~~

The handler:

1. reads one JSON document from stdin;
2. extracts `prompt`, `cwd`, `session_id`, `hook_event_name`, and `timestamp`;
3. ignores `transcript_path` and never opens it;
4. resolves the project root;
5. retrieves task-aware context with agent `gemini-cli` and retrieval-feedback
   recording disabled;
6. claims the hook event atomically and, only for the winning invocation,
   records retrieval feedback;
7. writes one valid JSON response containing
   `hookSpecificOutput.hookEventName: "BeforeAgent"` and
   `hookSpecificOutput.additionalContext`;
8. writes diagnostics only to stderr;
9. returns a harmless empty JSON response on recoverable failure or when
   another EchoVault handler already claimed the same event.

The prompt is used only as a retrieval query. It is not persisted, logged, or
included in local feedback data.

### Hook event idempotency

Gemini can discover the same EchoVault hook from a global extension and a
workspace fallback. Configuration alone cannot guarantee that every Gemini
version executes only the higher-precedence hook, so the handler enforces one
effective context injection at runtime.

The event key is the SHA-256 digest of a canonical tuple containing only
Gemini's base `session_id`, `hook_event_name`, and `timestamp` fields. It never
contains the prompt, cwd, transcript path, or retrieved memory content. Each
handler first performs retrieval with feedback recording disabled, then tries a
process-atomic create-if-absent claim under `MEMORY_HOME/hook-events/`. The
claim contains only the digest, integration version, and expiry time. Claims
expire after ten minutes and stale files are removed under the same short
claim-directory lock.

The invocation that acquires the claim records retrieval feedback once and
returns `additionalContext`; every losing invocation returns `{}`. A failure
after a successful claim may therefore omit context, but can never duplicate it
or record feedback twice. If any required base event field is missing, the
handler fails open with `{}` and a stderr diagnostic rather than using prompt
content as a fallback identity. The hook contract probe used by doctor and CI
must exercise that degraded path.

The extension does not use `AfterAgent` or `SessionEnd` to create memories.
Gemini's shutdown hook is best effort and cannot be a persistence boundary.
Its static context and skill use the same curated-save policy as Cursor,
including one operation UUID per conceptual save and reuse of that UUID for the
single allowed retry. The shared skill is agent-neutral: bound tool calls omit
caller-selected `agent`, `source`, and `project`, and the configured MCP binding
assigns those authority fields for Cursor or Gemini.

### Direct-settings fallback

The fallback has an explicit CLI contract:

- `memory setup gemini --project` installs workspace-scoped direct settings and
  instructions;
- `memory setup gemini --direct` installs user-scoped direct settings and
  instructions without the extension;
- `memory setup gemini --project --direct` is accepted as the explicit form of
  the workspace fallback and is equivalent to `--project`.

The symmetric uninstall commands are `memory uninstall gemini --project` for
the workspace fallback and `memory uninstall gemini --direct` for the user
fallback; plain `memory uninstall gemini` targets the native extension.
Direct setup and uninstall also accept `--config-dir DIR`, interpreted as the
target `.gemini` directory. Native extension mode deliberately delegates scope
to Gemini's extension manager and does not accept that override.

The fallback updates only marked EchoVault sections or entries in:

- project `.gemini/settings.json`, root `GEMINI.md`, and
  `.gemini/skills/echovault/SKILL.md`; or
- user `~/.gemini/settings.json`, `~/.gemini/GEMINI.md`, and
  `~/.gemini/skills/echovault/SKILL.md`.

This path installs MCP and static instructions where extension support is
unavailable or where Gemini Code Assist exposes only a subset of Gemini CLI
features. On a supported Gemini CLI it also merges the named EchoVault
`BeforeAgent` hook into `settings.json`; on a client without hook support it
omits that entry and doctor reports static/MCP-only degraded mode. Project
direct setup always installs its hook when the target supports hooks, regardless
of the current global activation state. Full phase-one acceptance targets
Gemini CLI.

Native extension setup and direct settings must not create duplicate effective
MCP definitions. An exact EchoVault-owned legacy settings entry can be removed
after a native extension validates, or upgraded in place for direct mode. A
custom `echovault` settings entry is a conflict and is never overwritten.

The following state machine is normative. `N` means the user-scoped native
extension, `U` the user-scoped direct fallback, and `P` the project-scoped
direct fallback:

| Observed state | `setup gemini` (`N`) | `setup gemini --direct` (`U`) | `setup gemini --project` (`P`) | Effective result |
|---|---|---|---|---|
| None | Install `N` | Install `U` | Install `P` | Requested source only |
| `N` | Byte-stable no-op | Conflict; require uninstall of `N` | Add `P` | Global MCP/hook, or project MCP plus guarded `N` + `P` hooks |
| `U` | Conflict; require uninstall of `U` | Byte-stable no-op | Add `P` | Global MCP/hook, or project MCP plus guarded `U` + `P` hooks |
| `P` | Add `N` | Add `U` | Byte-stable no-op | Project MCP; guarded global + project hooks when global is added |
| `N + P` | Byte-stable no-op | Conflict; require uninstall of `N` | Byte-stable no-op | Project MCP; guarded `N` + `P` hooks |
| `U + P` | Conflict; require uninstall of `U` | Byte-stable no-op | Byte-stable no-op | Project MCP; guarded `U` + `P` hooks |
| `N + U`, with or without `P` | Stop; invalid state | Stop; invalid state | Stop; invalid state | Doctor reports unhealthy until one global mode is uninstalled |

`N` and `U` are mutually exclusive because they occupy the same user scope.
Attempting `N` while `U` exists stops without mutation and names
`memory uninstall gemini --direct`; attempting `U` while `N` exists stops and
names `memory uninstall gemini`. `P` may coexist with either valid global mode.
Gemini's workspace precedence makes the same-named `P` MCP definition effective
over the global definition; without `P`, the sole valid global definition is
effective. Doctor reports the selected definition and the shadowed source.

Both global and project hooks may execute in a coexistence state. The hook-event
claim contract above turns them into at most one context injection and one
feedback record per Gemini event without making project setup dependent on
global activation. Uninstalling `N` or `U` removes only that global target, so
`P` continues standalone. Uninstalling `P` removes only project artifacts, so a
valid global mode continues. An observed custom same-named MCP entry or hook in
the target scope is a conflict; setup performs no mutation and doctor does not
count it as EchoVault-owned.

## Lifecycle and Data Flow

### Task start

1. Resolve the project root.
2. Use the current request as the retrieval query.
3. Retrieve a token-budgeted context pack from the shared vault.
4. Inject or expose summaries, structured constraints, provenance, and memory
   IDs.

Cursor's always-applied rule instructs the Agent to perform step 3; this is
policy-guided and observable, not mechanically forced. Gemini performs step 3
directly through `BeforeAgent`, so context injection is deterministic whenever
the supported hook is active.

The default context budget is approximately 1,200 tokens and remains
configurable. Setup does not overwrite an explicit user policy of `off`; under
the default `auto` or explicit `on` policy, Gemini's task hook and Cursor's
task-start instruction remain enabled for every substantive task. Cursor model
compliance retains the policy-guided limitation above.

### During work

- Use the initial pack when sufficient.
- Use `memory_search` for narrower retrieval.
- Use `memory_details` only when full context is necessary.
- Do not duplicate business logic in agent adapters.

### Task end

Save only when the task produced durable value:

- architectural or design decisions;
- root cause and solution for a bug;
- reusable patterns or gotchas;
- infrastructure or workflow setup;
- project state or active work needed by the next session;
- user corrections or clarified requirements.

Do not save:

- formatting or typo changes;
- obvious facts visible directly in code;
- generic session summaries;
- duplicate memories;
- complete prompts, responses, or transcripts.

The save includes agent source, project, related files, and available
branch/commit provenance. Adapter instructions generate one save operation UUID
and allow one bounded retry with that same idempotency key after an uncertain
failure. If persistence still fails, the agent must report the failure rather
than claiming the memory exists.

Curated end-of-task saving is policy-guided for both clients. EchoVault does not
mine responses or use shutdown hooks to manufacture a memory, so it cannot
mechanically force a model to identify durable learning. Deterministic tests
verify the instruction and save contract; real-client smoke tests observe model
compliance separately.

## Canonical Markdown Format

Phase one introduces session Markdown schema v2 so the human-readable vault is
actually lossless. File frontmatter carries `schema_version: 2` and aggregate
tags/sources only for browsing. Each memory section keeps its readable title,
What/Why/Impact/Source/details fields and stable memory-ID comment, plus one
machine-readable metadata JSON line containing every field required to rebuild
the record:

- per-memory tags and related files;
- project key, category, immutable creator `source`, `creator_source`,
  `last_updated_by`, ordered `contributors`, created/updated timestamps, and
  status;
- archive/supersession state;
- structured living-memory data and provenance;
- content fingerprint;
- applied operation IDs, request fingerprints, original actions, authoritative
  agents, timestamps, and per-operation branch/commit provenance.

Aggregate file tags and sources are derived display data and are never applied
back to every memory during import. SQLite stores a derived copy plus operational
retrieval feedback; vectors and feedback counters are not canonical Markdown
fields.

Existing schema-v1 Markdown remains readable. Doctor reports files whose
per-memory metadata is incomplete. `memory migrate vault-metadata` explicitly
enriches them from matching SQLite rows under the project lock. It never guesses
ambiguous per-memory values from aggregate frontmatter. Normal reads do not
rewrite legacy files. A create or update that would mutate a v1 session file
stops with this migration command instead of performing a lossy implicit
rewrite, and reconciliation does not discard DB-only legacy fields before
migration.

For a v1 record with a source, migration maps that value to `source`,
`creator_source`, `last_updated_by`, and the initial contributor. Missing source
data remains explicitly unknown rather than being inferred from the migrating
process. Because prior operations cannot be reconstructed, migrated records use
`history_complete: false` and receive one synthetic migration audit entry; new
operations after migration are complete and individually recorded.

### Duplicate merge and provenance contract

Duplicate matching remains deliberately conservative and project-scoped. A
save is a duplicate only when the highest same-project FTS candidate has a
normalized score of at least `0.7` and its title equals the incoming title after
trim and Unicode case-folding. Otherwise the save creates a new memory.

A duplicate update preserves the existing memory ID, original title,
`created_at`, archive/supersession state, `source`, and `creator_source`.
`source` remains the creator identity for backward compatibility;
`creator_source` is its explicit schema-v2 equivalent. A successful create or
mutation sets `last_updated_by` to the authoritative bound agent and appends
that agent to `contributors` as an ordered unique value. It also appends exactly
one operation-history record containing the operation ID, authoritative agent,
action, request fingerprint, UTC timestamp, and available branch and commit.

Curated duplicate saves merge fields deterministically:

- incoming non-null `what`, `why`, `impact`, `category`, `confidence`,
  `valid_from`, `valid_until`, `commit_sha`, `branch`, and `last_verified`
  replace their current values; omitted or null values preserve the current
  value;
- `tags`, `related_files`, `links`, `triggers`, `prerequisites`, `steps`,
  `verification`, `follow_ups`, `constraints`, `alternatives_rejected`, and
  `open_questions` use a stable ordered union: existing values retain their
  order and only new incoming values are appended;
- tags compare after trim and Unicode case-folding while preserving the first
  spelling; related-file comparisons normalize separators and lexical `.`/`..`
  segments without resolving symlinks; every other list compares trimmed exact
  strings;
- an omitted or empty collection never clears existing values;
- non-empty incoming details append after the existing body under a delimiter
  containing the UTC timestamp, authoritative agent, and operation ID; prior
  details are never replaced by deduplication;
- `updated_at`, `last_updated_by`, `updated_count`, contributors, content
  fingerprint, and operation history advance once for the accepted operation.

Explicit administrative updates are a separate operation and may rename or
clear fields, but their API must distinguish omitted, replacement, and explicit
clear values. They use the same project lock, canonical Markdown write,
contributor tracking, operation history, and derived-index transaction as a
save. A replay of an already applied idempotency key returns the original result
without incrementing counts, appending details, adding a contributor, or adding
a second history record.

For example, if Cursor creates a memory and Gemini later submits a matching
curated update, `source` and `creator_source` remain `cursor`,
`contributors` becomes `[cursor, gemini-cli]`, and `last_updated_by` becomes
`gemini-cli`. Aggregate frontmatter tags and sources are recalculated from the
merged per-memory records and never act as merge input.

## Concurrency and Consistency

Cross-agent support makes concurrent writes a normal condition. Add a
cross-platform, OS-released process lock scoped to the canonical vault project
directory. Writers for one project serialize across all of its historical daily
files, while unrelated projects do not block one another. Lock acquisition and
SQLite busy handling have bounded timeouts and return explicit retryable errors.
A leftover lock path is not itself ownership; a dead process cannot leave a
permanent lock.

The same project lock and ordering contract applies to create, duplicate
update, explicit update, archive, delete, import/reconcile, and vault-metadata
migration. Multi-project operations acquire project locks in sorted key order.
Feedback counters are DB-only operational data. Reindex does not mutate
Markdown and uses the content-fingerprint check described below.

An administrative merge that changes more than one Markdown file uses a
durable operation journal. The journal records only operation ID, target/temp
basenames, before/after digests, and phases; it contains no memory text. Each
file replacement remains individually atomic and parent-fsynced. Before the
next mutator, recovery compares recorded digests, resumes every known phase,
then rebuilds derived rows; an unrecognized external edit stops recovery as a
conflict. The implementation must not claim impossible all-files atomicity.

### Save state machine

For an agent-bound save, `idempotency_key` is required and unique within the
project. Markdown records every applied operation ID; a derived SQLite
`save_operations` table indexes them. The request fingerprint hashes normalized
redacted fields plus authoritative project and source, never the unredacted
input.

1. Validate inputs, resolve project/agent authority, redact persisted fields,
   and acquire the project lock.
2. Re-read canonical Markdown and SQLite under the lock. If the operation ID
   exists in Markdown with the same request fingerprint, repair a missing
   derived ledger row and return the original memory ID with `replayed`. Reuse
   with a different payload is a conflict. An operation found only in SQLite is
   drift, not authority; stop and require reconciliation rather than recreating
   Markdown from the index.
3. Perform duplicate detection and the deterministic merge above inside the
   lock. A duplicate update modifies the matching entry in its actual historical
   session file as well as the index; a create targets today's session file.
4. Render the complete lossless document to a temporary file in the target
   directory, flush it, and fsync the file.
5. Begin an explicit SQLite transaction, upsert the derived memory and operation
   row, and invalidate any previous vector for changed content.
6. Atomically replace the Markdown file and fsync its parent directory.
7. Commit SQLite and release the project lock.
8. Compute embeddings outside the lock. Upsert a vector only if a fresh content
   fingerprint still matches the embedded text.

A slower older embedding therefore cannot replace the vector for a newer
update. Embedding failure leaves the memory available through FTS without a
stale vector.

### Crash and recovery contract

Markdown schema v2 is authoritative; SQLite is rebuildable. Recovery is:

| Failure point | Durable state | Required recovery |
|---|---|---|
| Before temporary-file fsync | Old Markdown and old DB | Remove incomplete temp; no logical change |
| After temp fsync, before SQLite transaction | Old Markdown and old DB | Ignore/remove complete but uncommitted temp |
| During uncommitted DB update, before Markdown replace | Old Markdown; DB transaction rolls back | No logical change |
| After Markdown replace, before DB commit | New Markdown; old DB after rollback | Doctor reports drift; `memory import --reconcile` upserts DB and operation ledger from Markdown |
| After DB commit, before MCP response | New Markdown and DB | Retry with same operation ID returns `replayed`, never a second update |
| During or after embedding | Canonical save and FTS are complete | Conditional reindex fills a missing vector; stale vector is never retained |

`memory import --reconcile` is explicit and idempotent. It treats lossless v2
Markdown as canonical, upserts changed as well as missing rows, removes derived
rows and operation records absent from a complete v2 project scan, rebuilds the
operation ledger, and then conditionally rebuilds vectors. Legacy v1 conflicts
remain diagnostic until `memory migrate vault-metadata` can resolve them; a
mixed v1/v2 project never performs destructive derived-row removal. This
guarantees no lost update from normal concurrent writers while acknowledging
that two storage engines cannot share one physical transaction.

## Safe Configuration Mutation

Configuration helpers must:

- distinguish missing, empty, valid, and malformed files;
- abort without mutation on malformed JSON or TOML;
- preserve unrelated keys, MCP servers, hooks, rules, and environment entries;
- preserve file permissions where possible;
- write through a temporary file and atomic replacement;
- update only recognized EchoVault-owned legacy entries;
- preserve user-authored Markdown outside explicit EchoVault marker blocks;
- make repeated setup byte-stable after the first successful setup;
- remove only marked or structurally recognized EchoVault artifacts.

The existing behavior that converts malformed JSON to an empty object is not
allowed for setup or uninstall.

### Ownership and concurrency

Every installed integration root carries an `.echovault-managed.json` sidecar
with the integration ID, schema and asset versions, relative managed paths,
marked-block identifiers, and SHA-256 hashes of the last installed contents.
Shared JSON/TOML/Markdown files remain user-owned; the sidecar claims only the
EchoVault entry or marker block. Native plugin and extension directories are
staged under the same parent only after their manifests and hashes validate.
Because replacing a non-empty directory is not one portable atomic operation,
updates use a fsynced operation journal plus target-to-backup and staging-to-
target renames. Each rename is atomic; recovery resumes or preserves the last
digest-verified owned tree and never deletes an unknown user-modified tree.

Setup and uninstall take a per-target configuration lock, re-read the file
under that lock, and compare its digest again immediately before replacement.
If a non-cooperating client changes the file, the operation retries once from a
fresh read and then exits with a conflict. This lock-and-compare contract covers
all shared JSON, TOML, and marked Markdown read-modify-write operations; atomic
replacement alone is not considered sufficient.

Manually changed managed content is never silently overwritten or deleted.
Setup and uninstall stop without mutation and show the changed managed paths.
An explicit `--force-managed` flag may replace or remove only the previously
claimed EchoVault portions; it never authorizes replacement of unrelated user
configuration.

### Migration and coexistence matrix

| Observed state in the selected scope | Setup | Uninstall |
|---|---|---|
| No EchoVault artifacts | Install and write ownership manifest | No-op |
| Exact recognized pre-manifest legacy entry | Migrate in place, then write ownership manifest | Remove only exact legacy shape |
| Current owned, hashes match | Byte-stable no-op | Remove claimed artifacts and marker blocks |
| Older known owned version, hashes match | Upgrade through the locked journaled swap | Remove claimed artifacts |
| Owned but a managed hash differs | Conflict; require `--force-managed` | Conflict; require `--force-managed` |
| Same name with custom command, args, content, or unmarked directory | Conflict; no mutation | Preserve; report not owned |
| Malformed shared configuration | Parse error; no mutation | Parse error; no mutation |
| File changes during read-modify-write | Re-read once, then conflict if it changes again | Same |
| Valid global and project installs both exist | Project MCP entry shadows global by client precedence; doctor reports `shadowed`, not duplicate-health failure | Remove only requested scope |

When global and project static instructions are both visible, their generated
content and version marker are identical, so coexistence is semantically
idempotent even if a client includes both. A custom higher-precedence project
entry remains a conflict and is never treated as EchoVault-owned. Gemini's
adapter additionally enforces the hook-event claim and state machine described
above, yielding one effective context injection even when valid global and
project hook definitions both execute.

## Failure and Degraded Modes

| Condition | Required behavior |
|---|---|
| EchoVault executable missing | Setup or doctor reports the exact missing dependency; existing config is not damaged. |
| MCP startup fails | Agent continues without automatic memory and receives a concise diagnostic. |
| Embedding provider fails | Retrieval falls back to FTS5; save remains successful without a vector. |
| Context hook times out | Hook fails open and the static Gemini instructions still expose MCP fallback. |
| Curated save fails | Retry once with the same operation ID, then report that persistence failed. |
| Config is malformed | Stop without writing and identify the path and parse error. |
| Existing custom `echovault` entry conflicts | Report conflict; do not replace it silently. |
| Lock times out | Return an explicit retryable save error; do not write an unlocked document. |
| Client lacks native capability | Stop without silent scope change, report the exact explicit fallback command, and report degraded status after that fallback is selected. |
| Cursor Cloud Agent | Report local-vault unavailability; remote memory is out of scope. |

## Diagnostics

Extend the existing doctor flow with:

~~~text
memory doctor --agent cursor
memory doctor --agent gemini-cli
~~~

Checks include:

- EchoVault version and executable path;
- detected Cursor/Gemini version and required native capabilities;
- global or project installation scope;
- native manifest, rule, skill, hook, and MCP configuration;
- configured agent identity, MCP Roots state, and effective project key/aliases;
- MCP tool discovery for context, search, details, and save;
- integration ownership hashes, asset versions, and cross-scope shadowing;
- schema-v1/v2 status, stale temp/lock metadata, and vault/index consistency;
- embedding availability and remote-query privacy mode as healthy or degraded;
- conflicting or malformed configuration.

Doctor is always read-only. It reports separate explicit commands such as
`memory import --reconcile`, `memory migrate vault-metadata`, or
`memory project adopt-legacy`; it never runs them automatically.

## Security and Privacy

- All persisted text passes through existing EchoVault redaction.
- Hook prompts are retrieval inputs only and are not stored.
- Automatic task retrieval uses FTS plus a local embedding provider by default.
  If the configured provider is remote, query embedding is disabled unless the
  user explicitly sets `context.allow_remote_query_embeddings: true`.
- With that opt-in enabled, EchoVault applies sensitive-value redaction to a
  copy before sending the query to the remote provider. Local FTS still uses the
  original query. Doctor reports the effective local-only or remote-opt-in
  state.
- Raw transcripts are never parsed as a required integration API.
- Hook stdout contains only the final protocol JSON.
- Hook logs go to stderr and exclude prompt bodies, responses, tokens, and
  secrets.
- Native MCP definitions require no API key.
- Setup preserves unrelated environment fields without displaying their values.
- Setup refuses an implicit target symlink that escapes the selected user or
  project root; an explicit `--config-dir` remains the opt-in escape hatch.
- Project hooks and extensions retain the client platform's normal trust
  prompts; setup does not bypass trust.
- MCP `trust: true` is never enabled automatically.

## Packaging

Canonical native integration templates live under package data reachable
through `importlib.resources`. Setup cannot depend on files outside the
installed wheel. Phase one keeps no second handwritten mirror: setup renders
the Cursor plugin and Gemini extension directly from these canonical assets.
Any later repository- or marketplace-facing mirror must be generated and
verified byte-for-byte against them.

The wheel must contain:

- EchoVault skill template;
- Cursor manifest, MCP definition, rule, and skill assets;
- Gemini manifest, context, skill, and hook assets;
- ownership-manifest schema and any hook entry-point metadata.

The embedded fallback skill should be removed or generated from the canonical
asset so there is one behavioral source of truth.

## Backward Compatibility

- `memory mcp` without `--agent` continues to work.
- Existing MCP tool inputs continue to validate on unbound `memory mcp`;
  stricter identity and idempotency requirements apply only to newly bound
  adapter servers.
- Existing Claude Code, Codex, and OpenCode setup commands keep their public CLI
  names and behavior.
- Existing vault Markdown and SQLite schemas remain readable.
- No migration is required to read or search stored memories. A write targeting
  a schema-v1 session file requires the explicit lossless metadata migration.
- The additive derived-index migration creates project-alias and save-operation
  tables automatically; optional schema-v1 Markdown enrichment remains explicit.
- Existing remote embedding configurations continue to embed curated saves, but
  automatic task-query embedding requires the new explicit privacy opt-in.
- Re-running Cursor setup migrates only the exact known legacy EchoVault MCP
  shape and restores current instructions.

## Test Strategy

All production changes follow red-green-refactor. Every new behavior starts with
a focused failing regression test.

### Unit and contract tests

- shared project-root resolution, MCP Roots, nested cwd, same-name collision,
  and local-worktree identity;
- canonical agent identity and MCP defaults;
- rejection of project, agent, and source spoofing on bound servers;
- project isolation for context, search, details, and save;
- `memory_details` handler and schema;
- `memory_save` source attribution and idempotent replay;
- adapter registry and capability metadata;
- safe JSON/TOML parsing and atomic writes;
- schema-v2 lossless Markdown round trips and schema-v1 compatibility;
- lossless v1 enrichment plus refusal to mutate unresolved legacy metadata;
- deterministic cross-agent duplicate merging, immutable creator provenance,
  ordered contributors, and replay without a second history record;
- vault/index drift detection and Markdown-to-SQLite reconciliation;
- idempotent setup and uninstall;
- the full ownership/migration matrix, modified assets, cross-scope shadowing,
  and concurrent config compare-and-swap;
- Gemini hook JSON input/output, timeout, and fail-open behavior;
- Gemini duplicate-hook event claims, claim expiry, one feedback record, and no
  prompt or transcript content in claim artifacts;
- empty-prompt/no-op hook behavior with no save;
- rule and skill content requirements;
- doctor health and degraded findings;
- prompt non-retention, remote-query opt-in, and secret redaction, including the
  query passed to a remote embedding provider.

### Concurrency tests

Use separate processes, not only threads, to save into the same project/day.
Assert:

- every unique memory exists in Markdown;
- every memory exists once in SQLite;
- duplicate updates are not lost;
- delayed embedding from an older update cannot overwrite a newer vector;
- lock timeout does not produce an unlocked write;
- stale lock metadata does not block after its owning process exits;
- doctor reports no drift after successful concurrent saves;
- reconciliation repairs a simulated failure between Markdown replacement and
  database commit without duplicating a memory.

Fault injection covers every row in the crash/recovery table, including a lost
MCP response after commit. Eight separate writer processes each create ten
unique operation IDs against one project; the test passes only when all 80
operations occur exactly once in lossless Markdown and SQLite and doctor reports
no drift.

### MCP integration tests

- initialize and list tools;
- call context, search, details, and save;
- negotiate zero, one, and multiple MCP roots;
- verify bound identities, rejected overrides, and unbound compatibility;
- retry a committed save with the same operation ID and receive `replayed`;
- exercise malformed input, cancellation, and large output;
- run multiple MCP server processes against one vault.

### Setup integration tests

Use isolated temporary home directories for:

- Cursor global plugin setup;
- Cursor project fallback;
- Gemini extension setup;
- Gemini direct-settings fallback;
- every row and transition in the Gemini `N`/`U`/`P` state matrix, including
  same-scope mutual exclusion and uninstall survival;
- one Gemini context injection when global and project hooks both execute for
  the same event;
- simultaneous global and project installations with documented precedence;
- repeated setup with no second diff;
- uninstall preserving unrelated configuration;
- modified managed assets requiring `--force-managed`;
- malformed configuration with no mutation;
- concurrent non-EchoVault config edits detected by digest comparison;
- package-installed assets rather than source-tree assets.

### Blocking pull-request gates

The CI matrix is:

- Ubuntu with Python 3.10, 3.11, 3.12, 3.13, and 3.14;
- macOS with Python 3.10 and 3.14;
- Windows with Python 3.10 and 3.14.

Every cell runs the Python suite. Ubuntu 3.14 also builds the sdist and wheel,
installs the wheel into a clean environment, runs the isolated-home setup/MCP
contracts against installed assets, and inspects wheel contents. A rendered
Cursor plugin must pass the checked-in manifest/schema validator. A rendered
Gemini extension must pass `gemini extensions validate` with Gemini CLI 0.50.0.
None of these gates requires a model account or network embedding provider.

The exact blocking commands include:

~~~text
uv run --extra dev pytest -q
uv build
~~~

The baseline Claude Code, Codex, OpenCode, CLI, MCP, and storage tests remain in
the same blocking suite. Rust dashboard verification remains a separate job
because the compatibility bridge changes its write path but not its UI.

### Authenticated client smoke gates

Pin three independent client observations for the phase-one dogfood report:

- Cursor IDE 3.11.19 for manual IDE evidence;
- Cursor Agent CLI build 2026.07.09-a3815c0 for `agent --version`, interactive,
  and headless CLI evidence;
- Gemini CLI 0.50.0 for extension, hook, and headless evidence.

Never compare the Agent CLI build with the Cursor IDE semantic version. The
report covers:

- Cursor IDE new task and known marker retrieval;
- Cursor CLI interactive and headless task;
- Gemini CLI extension discovery and `BeforeAgent` marker injection;
- Gemini CLI native install, update, and uninstall from packaged assets;
- cross-agent save and retrieval through the same vault;
- missing executable, missing MCP, embedding failure, and abrupt client exit.

Each retrieval smoke seeds a unique marker, starts a fresh supported client in a
single-root workspace, submits a matching task, and records whether the marker
was injected or the expected MCP call occurred. Cursor model compliance results
are reported separately from deterministic installation/tool availability; they
cannot be promoted to an exactly-once guarantee. The cross-agent gate saves one
marker through the Cursor-bound server and a second marker through the
Gemini-bound server, with a distinct fixed operation ID for each, then retrieves
both memory IDs from every available bound client. A single generic or unbound
marker is insufficient. If a binary or authenticated account is unavailable,
the report says `not verified` and the phase is not described as
client-verified.

The pre-change baseline is 272 passing Python tests.

## Acceptance Criteria

### Deterministic acceptance

- `memory setup cursor` and `memory setup gemini` each complete a valid global
  native installation with one command; `--project` produces the documented
  version-controllable fallback.
- Cursor IDE and CLI artifacts expose the same bound MCP tools and same versioned
  rule/skill policy; Cursor model compliance remains observational.
- Gemini's tested `BeforeAgent` handler injects a seeded marker into
  `additionalContext` for a matching prompt and emits harmless valid JSON on
  timeout or empty input.
- A memory saved by Cursor is retrievable from Gemini, Claude Code, and Codex,
  and vice versa.
- Cross-agent duplicate updates preserve immutable creator provenance and record
  the authoritative last updater, ordered contributors, and one operation
  history entry per accepted idempotency key.
- Two same-named project roots receive different keys, nested cwd resolves to
  the same key, and local worktrees share a key.
- A bound server rejects source/agent/project spoofing and a repeated operation
  ID cannot create or apply a second save.
- No prompt or transcript is persisted by context retrieval.
- Remote semantic retrieval receives only a redacted query copy.
- Automatic task retrieval never calls a remote embedding provider without the
  explicit configuration opt-in.
- Simultaneous process saves lose no entries.
- Every injected crash point recovers according to the documented state table,
  and schema-v2 Markdown can rebuild SQLite without losing per-memory metadata.
- Repeated setup produces no file change.
- Uninstall removes only EchoVault-owned artifacts.
- Malformed or custom conflicting configuration is never overwritten.
- Missing MCP, embeddings, or native capabilities produce explicit degraded
  diagnostics.
- Existing Claude Code, Codex, and OpenCode tests remain green.
- The full Python suite and wheel build pass in CI.

### Observational client acceptance

- Fresh Cursor IDE and Cursor CLI sessions can retrieve the seeded marker under
  the documented always-applied policy.
- A fresh Gemini CLI session loads the extension and receives the seeded marker
  through `BeforeAgent`.
- The dogfood report records client version, scope, tool/hook evidence, and any
  policy-guided model miss without misreporting it as deterministic success.

## High-Level Delivery Sequence

1. Add project identity/Roots resolution, agent-bound MCP authority,
   `memory_details`, provenance, idempotency contracts, and failing tests.
2. Make schema-v2 Markdown lossless and add explicit legacy enrichment and
   reconciliation tests.
3. Add project process locking, atomic/fsynced Markdown writes, derived-index
   transactions, crash recovery, concurrency tests, and the minimal Rust
   dashboard-to-canonical-persistence bridge.
4. Add safe configuration lock/CAS primitives, ownership manifests, and the
   adapter registry.
5. Implement Cursor plugin assets, project fallback, migration, diagnostics,
   and tests.
6. Implement Gemini extension assets, hook handler, direct fallbacks,
   diagnostics, and tests.
7. Consolidate packaged assets, add wheel/manifest validation, CI, and support
   documentation.
8. Run isolated-home tests, the full matrix, package build, and available
   pinned-client smoke tests.
9. Install the verified fork locally for dogfooding.
10. Decide separately whether to push, release, publish plugins, or submit an
    upstream pull request.

## Primary References

- Cursor rules: https://docs.cursor.com/context/rules
- Cursor CLI rules and MCP: https://docs.cursor.com/en/cli/using
- Cursor plugins: https://cursor.com/docs/plugins
- Cursor current changelog: https://cursor.com/changelog
- Gemini hooks: https://geminicli.com/docs/hooks/reference/
- Gemini extensions: https://geminicli.com/docs/extensions/reference/
- Gemini MCP: https://geminicli.com/docs/tools/mcp-server/
- Gemini context files: https://geminicli.com/docs/cli/gemini-md/
- Gemini skills: https://geminicli.com/docs/cli/skills/

## Resolved Decisions

- Phase one covers Cursor IDE, Cursor CLI, and Gemini CLI.
- The architecture is native hybrid over one MCP core.
- Persistence remains curated.
- Full transcript ingestion is excluded.
- Cursor hooks are not a correctness dependency.
- Cursor task-start retrieval and curated saving are policy-guided, not forced.
- Gemini `BeforeAgent` performs task-time injection.
- Explicit MCP save is the authoritative persistence trigger; schema-v2
  Markdown is the authoritative stored record and SQLite is derived.
- Existing dashboard mutations cross the same canonical persistence boundary;
  no dashboard feature or UI redesign is included.
- Project keys separate display names from collision-resistant local identity;
  automatic multi-root routing is excluded.
- Bound MCP identities and project roots cannot be spoofed by tool arguments.
- Automatic task queries remain local unless remote embeddings are explicitly
  enabled.
- Local cloud-agent access is excluded until a remote design exists.
