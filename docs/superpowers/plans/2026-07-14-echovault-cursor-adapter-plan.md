# EchoVault Adapter Foundation and Cursor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a reusable safe integration layer and deliver supported Cursor global-plugin and project-fallback installations for IDE and CLI.

**Architecture:** Move client-specific setup behind typed adapters and shared strict config/ownership primitives. Render one canonical curated behavior source into a Cursor plugin or project files while the shared bound MCP core remains the only memory capability implementation.

**Tech Stack:** Python 3.10–3.14, Click, importlib.resources, JSON/TOML/Markdown, pytest, Cursor plugin/rule/skill/MCP formats.

## Global Constraints

- Complete the Local Core and Bound MCP plans before this plan.
- Existing Claude Code, Codex, and OpenCode command names and valid-config behavior remain compatible.
- Missing, empty, valid, and malformed configuration are distinct states; malformed input is never replaced with an empty object.
- Preserve unrelated keys, hooks, MCP servers, rules, environment entries, file modes, and user Markdown.
- Every shared-file mutation uses a per-target process lock, one digest retry, prepared atomic replace, and explicit conflict after the second observed change.
- Every owned integration root contains .echovault-managed.json with hashes for only the claimed files/entries/blocks.
- Manually modified managed content requires --force-managed; that flag never authorizes unrelated replacement.
- Reject implicit user/project config symlinks that escape the chosen root; explicit --config-dir is the opt-in override.
- Global GUI-facing commands use the validated absolute EchoVault executable; project files use portable memory unless --command is explicit.
- Cursor global path is ~/.cursor/plugins/local/echovault or the equivalent selected config root.
- Cursor project fallback consists of .cursor/mcp.json, .cursor/rules/echovault.mdc, and .cursor/skills/echovault/SKILL.md.
- No Cursor lifecycle hook is a correctness dependency.
- Cursor rule compliance remains observational; deterministic gates cover files, manifest, tools, and instruction content.
- Do not print or hash unrelated environment values into diagnostics/ownership metadata.
- Begin each behavior change with an observed failing test.

---

## File Responsibility Map

- **Create: src/memory/integrations/types.py** — scope/mode/options/result/capability and diagnostic contracts.
- **Create: src/memory/integrations/process.py** — shared injectable subprocess contract for client discovery and native managers.
- **Create: src/memory/integrations/registry.py** — adapter lookup only.
- **Create: src/memory/integrations/config_io.py** — strict JSON/TOML/marked-block mutation.
- **Create: src/memory/integrations/ownership.py** — managed artifact manifest and conflict verification.
- **Create: src/memory/integrations/asset_io.py** — canonical package-resource loading, rendering, staging, and validation.
- **Create: src/memory/integrations/cursor.py** — Cursor setup/uninstall/diagnostics.
- **Create: src/memory/integrations/diagnostics.py** — shared read-only findings.
- **Create: src/memory/integrations/assets/common/echovault-skill.md** — one curated behavior source.
- **Create Cursor assets:** plugin manifest, MCP template, rule template.
- **Modify: src/memory/setup.py** — compatibility wrappers and safe legacy helpers.
- **Modify: src/memory/cli.py** — command/force/scope options and agent doctor.
- **Modify: src/memory/health.py** — compose integration findings without writes.
- **Modify: pyproject.toml and uv.lock** — lossless TOML dependency if selected and package-resource declarations prepared for Phase 5.
- **Create tests:** tests/test_integration_registry.py, tests/test_integration_process.py, tests/test_integration_config_io.py, tests/test_integration_ownership.py, tests/test_cursor_integration.py, tests/test_integration_diagnostics.py.
- **Create test support:** tests/integration_helpers.py; shared fake executable fixture in tests/conftest.py.
- **Modify tests:** tests/conftest.py, tests/test_setup.py, tests/test_cli.py.

### Task 1: Typed Adapter Contract and Registry

**Files:**
- Create: src/memory/integrations/__init__.py
- Create: src/memory/integrations/types.py
- Create: src/memory/integrations/process.py
- Create: src/memory/integrations/registry.py
- Create: tests/test_integration_registry.py
- Create: tests/test_integration_process.py

**Interfaces:**
- Produces: InstallScope, InstallMode, AdapterCapabilities, IntegrationOptions, IntegrationResult, DiagnosticFinding
- Produces: CommandResult, CommandRunner, SubprocessRunner
- Produces: IntegrationAdapter protocol
- Produces: register_adapter(adapter), get_adapter(name), and registered_adapters()
- Leaves setup_cursor/uninstall_cursor untouched until CursorAdapter exists in Task 5

- [ ] **Step 1: Write failing registry and compatibility tests**

~~~python
class StubAdapter:
    integration_id = 'stub'
    agent = 'stub-agent'
    capabilities = AdapterCapabilities(
        mcp=True, rules=False, skills=False, extensions=False, hooks=False
    )


def test_registry_registers_adapter_without_storage_logic() -> None:
    register_adapter(StubAdapter())
    adapter = get_adapter('stub')
    assert adapter.integration_id == 'stub'
    assert registered_adapters() == ('stub',)


def test_unknown_adapter_is_explicit() -> None:
    with pytest.raises(UnknownIntegrationError):
        get_adapter('unknown')


def test_subprocess_runner_never_uses_a_shell_and_forwards_context(
    monkeypatch, tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        captured['argv'] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, '2026.07.09-a3815c0\n', '')

    monkeypatch.setattr(subprocess, 'run', fake_run)
    result = SubprocessRunner().run(
        ['agent', '--version'],
        timeout=2.0,
        cwd=tmp_path,
        env={'HOME': str(tmp_path)},
    )
    assert result == CommandResult(0, '2026.07.09-a3815c0\n', '')
    assert captured['argv'] == ['agent', '--version']
    assert captured['shell'] is False
    assert captured['cwd'] == tmp_path.resolve()
    assert captured['env'] == {'HOME': str(tmp_path)}
~~~

- [ ] **Step 2: Run registry tests and observe missing package**

Run: `uv run --extra dev pytest tests/test_integration_registry.py -q`

Expected: collection fails because memory.integrations does not exist.

- [ ] **Step 3: Implement the minimal typed boundary**

~~~python
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal, Protocol


class InstallScope(str, Enum):
    USER = 'user'
    PROJECT = 'project'


class InstallMode(str, Enum):
    NATIVE = 'native'
    DIRECT = 'direct'


@dataclass(frozen=True)
class AdapterCapabilities:
    mcp: bool
    rules: bool
    skills: bool
    extensions: bool
    hooks: bool


@dataclass(frozen=True)
class IntegrationOptions:
    scope: InstallScope
    mode: InstallMode
    config_root: Path | None
    project_root: Path | None
    command: str | None
    force_managed: bool = False
    config_root_explicit: bool = False


@dataclass(frozen=True)
class IntegrationResult:
    status: Literal['installed', 'updated', 'unchanged', 'removed']
    message: str
    paths: tuple[Path, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiagnosticFinding:
    code: str
    status: Literal['ok', 'degraded', 'warning', 'unhealthy', 'shadowed']
    message: str
    path: Path | None = None
    command: str | None = None


class IntegrationAdapter(Protocol):
    integration_id: str
    agent: str
    capabilities: AdapterCapabilities

    def setup(self, options: IntegrationOptions) -> IntegrationResult:
        raise NotImplementedError

    def uninstall(self, options: IntegrationOptions) -> IntegrationResult:
        raise NotImplementedError

    def diagnose(self, options: IntegrationOptions) -> tuple['DiagnosticFinding', ...]:
        raise NotImplementedError
~~~

In process.py implement the shared runner without shell expansion:

~~~python
from dataclasses import dataclass
import subprocess
from pathlib import Path
from typing import Mapping, Protocol, Sequence


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        capture_output: bool = True,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        raise NotImplementedError


class SubprocessRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        capture_output: bool = True,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        completed = subprocess.run(
            list(argv),
            timeout=timeout,
            check=False,
            shell=False,
            text=True,
            capture_output=capture_output,
            cwd=None if cwd is None else cwd.resolve(),
            env=None if env is None else dict(env),
        )
        return CommandResult(
            completed.returncode,
            completed.stdout or '',
            completed.stderr or '',
        )
~~~

Keep the registry free of MemoryService imports. `register_adapter` rejects duplicate integration IDs and tests reset the registry through a private pytest fixture, not through a production reset API. Task 5 registers Cursor only after CursorAdapter exists and then translates legacy setup.py wrappers to IntegrationOptions.

- [ ] **Step 4: Run registry and existing setup tests**

Run: `uv run --extra dev pytest tests/test_integration_registry.py tests/test_integration_process.py -q`

Expected: registry and runner tests pass; no Cursor implementation is imported yet.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/integrations/__init__.py src/memory/integrations/types.py src/memory/integrations/process.py src/memory/integrations/registry.py tests/test_integration_registry.py tests/test_integration_process.py
git commit -m "feat: add agent integration registry"
~~~

### Task 2: Strict Config Parsing and Locked Digest-CAS Mutation

**Files:**
- Create: src/memory/integrations/config_io.py
- Create: tests/test_integration_config_io.py
- Modify: src/memory/setup.py
- Modify: pyproject.toml
- Modify: uv.lock
- Modify: tests/test_setup.py

**Interfaces:**
- Produces: ConfigState, ConfigDocument, FileMutationResult
- Produces: read_json_strict, read_toml_strict
- Produces: mutate_json_atomic, mutate_toml_atomic, mutate_marked_block
- Produces: validate_target_root(target: Path, boundary: Path, explicit: bool) -> Path
- Produces: ConfigMalformedError and ConfigConflictError

- [ ] **Step 1: Write failing malformed/preservation/race tests**

~~~python
def add_owned_echovault_entry(data: dict[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(data)
    updated.setdefault('mcpServers', {})['echovault'] = {
        'command': 'memory',
        'args': ['mcp', '--agent', 'cursor'],
    }
    return updated


def test_malformed_json_is_never_mutated(tmp_path: Path) -> None:
    path = tmp_path / 'settings.json'
    original = b'{"mcpServers":'
    path.write_bytes(original)
    with pytest.raises(ConfigMalformedError):
        mutate_json_atomic(path, lambda data: data)
    assert path.read_bytes() == original


def test_json_mutation_preserves_unrelated_nested_values_and_mode(tmp_path: Path) -> None:
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({
        'theme': 'dark',
        'mcpServers': {'other': {'command': 'other', 'env': {'TOKEN': 'unchanged'}}},
    }))
    path.chmod(0o640)
    mutate_json_atomic(path, add_owned_echovault_entry)
    data = json.loads(path.read_text())
    assert data['mcpServers']['other']['env']['TOKEN'] == 'unchanged'
    assert path.stat().st_mode & 0o777 == 0o640


def test_second_external_change_returns_conflict_without_overwrite(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / 'settings.json'
    path.write_text('{}')
    real_digest = config_io.digest_file
    comparisons = 0

    def digest_after_external_change(target: Path) -> str | None:
        nonlocal comparisons
        comparisons += 1
        path.write_text(json.dumps({'external': comparisons}))
        return real_digest(target)

    monkeypatch.setattr(config_io, 'digest_file', digest_after_external_change)
    with pytest.raises(ConfigConflictError):
        mutate_json_atomic(path, add_owned_echovault_entry, max_conflict_retries=1)
    assert json.loads(path.read_text()) == {'external': 2}


def test_implicit_symlink_escape_is_rejected_but_explicit_root_is_allowed(tmp_path: Path) -> None:
    boundary = tmp_path / 'home'
    outside = tmp_path / 'outside'
    boundary.mkdir()
    outside.mkdir()
    target = boundary / '.cursor'
    target.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ConfigBoundaryError):
        validate_target_root(target, boundary, explicit=False)
    assert validate_target_root(target, boundary, explicit=True) == outside.resolve()
~~~

- [ ] **Step 2: Run config tests and observe malformed overwrite behavior**

Run: `uv run --extra dev pytest tests/test_integration_config_io.py tests/test_setup.py -k "malformed or preserves or conflict" -q`

Expected: missing config_io module and current setup malformed-file tests expose replacement/append behavior.

- [ ] **Step 3: Implement strict documents and mutation**

~~~python
class ConfigState(str, Enum):
    MISSING = 'missing'
    EMPTY = 'empty'
    VALID = 'valid'
    MALFORMED = 'malformed'


@dataclass(frozen=True)
class ConfigDocument:
    path: Path
    state: ConfigState
    data: dict[str, Any]
    raw: bytes
    sha256: str | None
    mode: int | None


@dataclass(frozen=True)
class FileMutationResult:
    changed: bool
    before_sha256: str | None
    after_sha256: str | None


class ConfigBoundaryError(ValueError):
    pass


def validate_target_root(target: Path, boundary: Path, explicit: bool) -> Path:
    resolved_target = target.expanduser().resolve()
    resolved_boundary = boundary.expanduser().resolve()
    if explicit:
        return resolved_target
    if not resolved_target.is_relative_to(resolved_boundary):
        raise ConfigBoundaryError(f'Implicit target escapes selected root: {target}')
    return resolved_target
~~~

read_json_strict treats missing and whitespace-only as empty data with distinct states, computes its document digest directly from the bytes it read, and raises ConfigMalformedError carrying the path/line/column for malformed content. The separate digest_file call is reserved for the pre-replace CAS comparison. Use tomlkit for lossless TOML mutation; add `tomlkit>=0.13` to runtime dependencies and lock it. Do not retain the old append/regex fallback for malformed TOML.

mutate functions:

1. acquire `<target>.echovault.lock`;
2. read state/raw/digest/mode;
3. fail on malformed;
4. apply a pure deep-copy mutator;
5. re-read digest immediately before replace;
6. retry once from a fresh read if changed;
7. raise ConfigConflictError on the second change;
8. prepare/replace atomically and preserve mode.

Marked blocks use exact begin/end markers and reject unmatched or duplicated markers. Update Claude/Codex/OpenCode helpers to use these primitives while keeping valid outputs and command names stable.

- [ ] **Step 4: Run config, legacy setup, and CLI suites**

Run: `uv run --extra dev pytest tests/test_integration_config_io.py tests/test_setup.py tests/test_cli.py -k "setup or uninstall or config or toml" -q`

Expected: strict config and legacy valid-state tests pass; malformed state remains byte-identical.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/integrations/config_io.py src/memory/setup.py pyproject.toml uv.lock tests/test_integration_config_io.py tests/test_setup.py
git commit -m "fix: mutate agent configuration safely"
~~~

### Task 3: Managed Ownership and Atomic Integration Trees

**Files:**
- Create: src/memory/integrations/ownership.py
- Create: src/memory/integrations/asset_io.py
- Create: tests/test_integration_ownership.py

**Interfaces:**
- Produces: ManagedArtifact and OwnershipManifest
- Produces: load_manifest, verify_managed_content, write_manifest_atomic
- Produces: stage_managed_tree and replace_managed_tree
- Produces: TreeSwapJournal and recover_managed_tree_swap
- Produces: private TreeFilesystem protocol and LocalTreeFilesystem implementation
- Produces: OwnershipConflict

- [ ] **Step 1: Write failing ownership tests**

~~~python
def rendered_fixture(version: str = '0.5.0') -> dict[str, bytes]:
    return {
        'rules/echovault.mdc': f'policy {version}\n'.encode(),
        'skills/echovault/SKILL.md': f'skill {version}\n'.encode(),
    }


def install_fixture_tree(tmp_path: Path) -> Path:
    root = tmp_path / 'managed-tree'
    replace_managed_tree(root, rendered_fixture(), force_managed=False)
    return root


def upgraded_fixture() -> dict[str, bytes]:
    return rendered_fixture('0.6.0')


class SimulatedTreeSwapCrash(RuntimeError):
    pass


class CrashInjectingTreeFilesystem:
    def __init__(self, crash_after: str) -> None:
        self.delegate = LocalTreeFilesystem()
        self.crash_after = crash_after

    def replace(self, source: Path, target: Path) -> None:
        self.delegate.replace(source, target)

    def unlink(self, path: Path) -> None:
        self.delegate.unlink(path)

    def fsync_directory(self, path: Path) -> None:
        self.delegate.fsync_directory(path)

    def checkpoint(self, phase: str) -> None:
        if phase == self.crash_after:
            raise SimulatedTreeSwapCrash(phase)


def test_modified_managed_file_requires_force(tmp_path: Path) -> None:
    root = install_fixture_tree(tmp_path)
    (root / 'rules' / 'echovault.mdc').write_text('user edit')
    conflicts = verify_managed_content(root, load_manifest(root))
    assert [conflict.path for conflict in conflicts] == ['rules/echovault.mdc']
    with pytest.raises(OwnershipConflict):
        replace_managed_tree(root, rendered_fixture(), force_managed=False)


def test_force_never_claims_unrelated_file(tmp_path: Path) -> None:
    root = install_fixture_tree(tmp_path)
    (root / 'notes.txt').write_text('user data')
    replace_managed_tree(root, upgraded_fixture(), force_managed=True)
    assert (root / 'notes.txt').read_text() == 'user data'


@pytest.mark.parametrize('crash_after', ['journal', 'backup-rename', 'target-rename'])
def test_tree_swap_recovers_after_each_durable_phase(
    tmp_path: Path, crash_after: str,
) -> None:
    root = install_fixture_tree(tmp_path)
    filesystem = CrashInjectingTreeFilesystem(crash_after)
    with pytest.raises(SimulatedTreeSwapCrash):
        replace_managed_tree(
            root,
            upgraded_fixture(),
            force_managed=False,
            _filesystem=filesystem,
        )
    recover_managed_tree_swap(root)
    verify_managed_content(root, load_manifest(root))
    assert not list(root.parent.glob('.echovault-tree-*.json'))
    assert not list(root.parent.glob('.echovault-backup-*'))
~~~

- [ ] **Step 2: Run ownership tests and observe missing module**

Run: `uv run --extra dev pytest tests/test_integration_ownership.py -q`

Expected: collection fails because ownership/assets modules do not exist.

- [ ] **Step 3: Implement artifact-level ownership**

~~~python
@dataclass(frozen=True)
class ManagedArtifact:
    path: str
    kind: Literal['file', 'json-entry', 'marked-block']
    sha256: str
    locator: str | None = None
    marker: str | None = None


@dataclass(frozen=True)
class OwnershipManifest:
    integration_id: str
    schema_version: int
    asset_version: str
    managed: tuple[ManagedArtifact, ...]


class TreeFilesystem(Protocol):
    def replace(self, source: Path, target: Path) -> None: ...
    def unlink(self, path: Path) -> None: ...
    def fsync_directory(self, path: Path) -> None: ...
    def checkpoint(self, phase: str) -> None: ...
~~~

Hash only owned file bytes, canonical JSON entry bytes, or marked-block bytes. Validate relative paths against traversal. Write .echovault-managed.json atomically. For native directories, hold a per-tree process lock and render into a sibling staging directory. Validate every declared path/hash/manifest before the swap.

Do not claim that replacing a non-empty directory is one portable atomic operation. Persist and fsync a parent-directory `TreeSwapJournal` containing operation ID, target/staging/backup basenames, before/after tree digests, and phase only—never file contents. Fsync staging, then write the journal; rename target to backup; rename staging to target; fsync the parent after each rename; verify the after digest; remove backup and journal and fsync again. Before any later setup/uninstall/doctor, recover a journal deterministically by comparing target/staging/backup digests with the recorded before/after values. Resume a known phase, keep the last verified owned tree if both candidates are valid, and raise OwnershipConflict without deletion if any path has an unknown digest. Preserve unrelated shared-root files and remove only manifest-claimed artifacts on uninstall.

`LocalTreeFilesystem` implements the private protocol with `os.replace`, durable directory fsync, unlink, and a no-op checkpoint. `replace_managed_tree` accepts only a private keyword `_filesystem: TreeFilesystem | None = None`; all production callers omit it and receive LocalTreeFilesystem. Invoke checkpoints only after the journal fsync, backup rename plus parent fsync, and target rename plus parent fsync. The test double above delegates real filesystem operations and injects a defined exception at those durable boundaries; there is no string-valued public `fault_injector` API.

- [ ] **Step 4: Run ownership and safe-IO tests**

Run: `uv run --extra dev pytest tests/test_integration_ownership.py tests/test_safe_io.py -q`

Expected: modified, force, traversal, staging, no-op, and unrelated-file tests pass.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/integrations/ownership.py src/memory/integrations/asset_io.py tests/test_integration_ownership.py
git commit -m "feat: track managed integration artifacts"
~~~

### Task 4: Canonical Curated Behavior and Cursor Asset Templates

**Files:**
- Create: src/memory/integrations/assets/__init__.py
- Create: src/memory/integrations/assets/common/echovault-skill.md
- Create: src/memory/integrations/assets/cursor/plugin.json
- Create: src/memory/integrations/assets/cursor/mcp.json
- Create: src/memory/integrations/assets/cursor/echovault.mdc
- Create: tests/test_cursor_integration.py

**Interfaces:**
- Produces: render_cursor_assets(command, version) -> dict[str, bytes]
- Produces one behavior version marker shared by global/project rule and skill

- [ ] **Step 1: Write failing asset-content tests**

~~~python
def test_cursor_assets_have_valid_manifest_and_curated_contract() -> None:
    assets = render_cursor_assets(command='/opt/echovault/bin/memory', version='0.6.0')
    manifest = json.loads(assets['.cursor-plugin/plugin.json'])
    mcp = json.loads(assets['mcp.json'])
    rule = assets['rules/echovault.mdc'].decode()
    skill = assets['skills/echovault/SKILL.md'].decode()
    assert manifest['name'] == 'echovault'
    assert manifest['mcpServers'] == 'mcp.json'
    assert mcp['mcpServers']['echovault']['args'] == ['mcp', '--agent', 'cursor']
    assert 'env' not in mcp['mcpServers']['echovault']
    assert 'trust' not in mcp['mcpServers']['echovault']
    assert 'alwaysApply: true' in rule
    for phrase in ('memory_context', 'memory_search', 'memory_details', 'memory_save', 'idempotency'):
        assert phrase in rule
        assert phrase in skill
    assert 'transcript' in rule
    assert 'context.mode=off' in rule
    for forbidden in ('source=cursor', 'source=gemini', 'agent=cursor', 'agent=gemini', 'project='):
        assert forbidden not in skill
    assert '0.6.0' in rule
    assert '0.6.0' in skill
~~~

- [ ] **Step 2: Run Cursor asset test and observe missing assets**

Run: `uv run --extra dev pytest tests/test_cursor_integration.py -k assets -q`

Expected: render_cursor_assets or canonical resources are missing.

- [ ] **Step 3: Add exact templates and renderer**

plugin.json:

~~~json
{
  "name": "echovault",
  "displayName": "EchoVault",
  "version": "{{VERSION}}",
  "description": "Local-first curated memory for coding agents",
  "license": "MIT",
  "skills": "./skills/",
  "rules": "./rules/",
  "mcpServers": "mcp.json"
}
~~~

mcp.json:

~~~json
{
  "mcpServers": {
    "echovault": {
      "command": "{{MEMORY_COMMAND}}",
      "args": ["mcp", "--agent", "cursor"]
    }
  }
}
~~~

The rule starts:

~~~markdown
---
description: Retrieve EchoVault context before substantive work and save curated durable learnings.
alwaysApply: true
---

# EchoVault curated memory

Before substantive planning, debugging, architecture, or implementation, call memory_context with the current request.
Use memory_search and memory_details only when the context pack is insufficient.
If memory_context reports context.mode=off, continue without automatic memory_search; explicit user-directed retrieval remains available.
Before the final response, call memory_save only for durable decisions, fixes, patterns, project state, or clarified requirements.
Generate one UUID per conceptual save and reuse it for one retry through idempotency_key.
Never store secrets, complete prompts, responses, or transcripts. Skip trivial and duplicate memories.
~~~

The common skill is agent-neutral. It contains the same policy, the 1,200-token default, created/updated/replayed response handling, and explicit reporting after a failed retry. Bound tool calls never pass `agent`, `source`, or `project`; the MCP process started with `--agent cursor` or `--agent gemini-cli` assigns those authoritative values. `asset_io.py` loads resources through importlib.resources and substitutes only validated command/version placeholders. Phase 4 renders this exact common skill for Gemini and adds a regression assertion that neither renderer contains a foreign or caller-selected identity.

- [ ] **Step 4: Run asset and registry tests**

Run: `uv run --extra dev pytest tests/test_cursor_integration.py -k assets tests/test_integration_registry.py -q`

Expected: manifest paths, MCP binding, rule, skill, and version markers pass.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/integrations/assets/__init__.py src/memory/integrations/assets/common/echovault-skill.md src/memory/integrations/assets/cursor/plugin.json src/memory/integrations/assets/cursor/mcp.json src/memory/integrations/assets/cursor/echovault.mdc tests/test_cursor_integration.py
git commit -m "feat: add canonical cursor integration assets"
~~~

### Task 5: Cursor Project Fallback

**Files:**
- Create: src/memory/integrations/cursor.py
- Create: tests/integration_helpers.py
- Modify: tests/test_cursor_integration.py
- Modify: src/memory/integrations/registry.py
- Modify: src/memory/setup.py
- Modify: tests/conftest.py
- Modify: tests/test_setup.py

**Interfaces:**
- Produces: CursorAdapter.setup/uninstall/diagnose
- Project target: PROJECT/.cursor or explicit --config-dir
- Project MCP command defaults to memory; explicit --command is exact

- [ ] **Step 1: Write failing project install tests**

~~~python
def cursor_adapter() -> CursorAdapter:
    adapter = get_adapter('cursor')
    assert isinstance(adapter, CursorAdapter)
    return adapter


def project_options(
    project: Path,
    *,
    command: str | None = None,
    force_managed: bool = False,
) -> IntegrationOptions:
    return IntegrationOptions(
        scope=InstallScope.PROJECT,
        mode=InstallMode.DIRECT,
        config_root=None,
        project_root=project,
        command=command,
        force_managed=force_managed,
    )


def user_options(
    cursor_root: Path,
    *,
    command: str,
    force_managed: bool = False,
) -> IntegrationOptions:
    return IntegrationOptions(
        scope=InstallScope.USER,
        mode=InstallMode.NATIVE,
        config_root=cursor_root,
        project_root=None,
        command=command,
        force_managed=force_managed,
        config_root_explicit=True,
    )


def snapshot_tree(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob('*'))
        if path.is_file()
    }


def seed_project_with_other_mcp(tmp_path: Path) -> Path:
    project = tmp_path / 'repo'
    cursor = project / '.cursor'
    cursor.mkdir(parents=True)
    (cursor / 'mcp.json').write_text(json.dumps({
        'mcpServers': {
            'other': {'command': 'other', 'env': {'TOKEN': 'unchanged'}},
        },
    }))
    return project


@pytest.fixture
def fake_memory(tmp_path: Path) -> Path:
    executable = tmp_path / 'bin' / 'memory'
    executable.parent.mkdir()
    executable.write_text('#!/bin/sh\nexit 0\n')
    executable.chmod(0o755)
    return executable


def test_project_setup_installs_portable_mcp_rule_skill_and_manifest(tmp_path: Path) -> None:
    project = tmp_path / 'repo'
    project.mkdir()
    result = cursor_adapter().setup(project_options(project))
    config = json.loads((project / '.cursor' / 'mcp.json').read_text())
    assert config['mcpServers']['echovault']['command'] == 'memory'
    assert (project / '.cursor/rules/echovault.mdc').is_file()
    assert (project / '.cursor/skills/echovault/SKILL.md').is_file()
    assert (project / '.cursor/.echovault-managed.json').is_file()
    assert result.status == 'installed'


def test_project_setup_is_byte_stable_and_preserves_other_server(tmp_path: Path) -> None:
    project = seed_project_with_other_mcp(tmp_path)
    adapter = cursor_adapter()
    adapter.setup(project_options(project))
    first = snapshot_tree(project / '.cursor')
    second = adapter.setup(project_options(project))
    assert snapshot_tree(project / '.cursor') == first
    assert second.status == 'unchanged'
    assert json.loads((project / '.cursor/mcp.json').read_text())['mcpServers']['other']['env']['TOKEN'] == 'unchanged'


def test_project_setup_does_not_mutate_service_context_policy(
    tmp_path: Path, monkeypatch,
) -> None:
    memory_home = tmp_path / 'memory-home'
    memory_home.mkdir()
    config = memory_home / 'config.yaml'
    config.write_text('context:\n  mode: off\n')
    before = config.read_bytes()
    monkeypatch.setenv('MEMORY_HOME', str(memory_home))
    project = tmp_path / 'repo'
    project.mkdir()
    cursor_adapter().setup(project_options(project))
    assert config.read_bytes() == before


def test_registry_and_legacy_cursor_wrapper_keep_contract(tmp_path: Path) -> None:
    assert get_adapter('cursor').agent == 'cursor'
    result = setup_cursor(str(tmp_path / '.cursor'))
    assert set(result) >= {'status', 'message'}
~~~

Place `cursor_adapter`, both options builders, `snapshot_tree`, and `seed_project_with_other_mcp` in `tests/integration_helpers.py`; every test module imports them explicitly. Place only the `fake_memory` pytest fixture in `tests/conftest.py`, making it available to `test_cursor_integration.py`, `test_cli.py`, and later Gemini tests. This is the sole producer of those names; do not duplicate module-local variants.

- [ ] **Step 2: Run project tests and observe missing adapter**

Run: `uv run --extra dev pytest tests/test_cursor_integration.py -k project -q`

Expected: cursor adapter setup is unimplemented.

- [ ] **Step 3: Implement safe project setup**

Register one CursorAdapter instance in registry.py. Resolve/validate project root with the shared resolver. Refuse an implicit .cursor symlink outside it; allow an explicit config_root and mark that override in diagnostics. Strict-merge the owned mcpServers.echovault entry, write the marked rule and skill, then write one ownership manifest claiming only those components. Never modify the EchoVault service `config.yaml`; `context.mode=off` is consumed at retrieval time, not changed by adapter setup.

If the exact legacy entry is present, upgrade it in place to bound args. If a custom same-named entry exists, return a conflict with no file mutation. Validate the portable memory executable when doctor runs, not by writing an absolute path during portable setup. In setup.py preserve the existing `setup_cursor(cursor_home: str)` and `uninstall_cursor(cursor_home: str)` signatures, translate them to user-scope IntegrationOptions, and translate IntegrationResult back to the established `{'status', 'message'}` shape; new project behavior is exposed through the adapter-backed Click commands, not a new legacy keyword.

- [ ] **Step 4: Run project, config, and ownership tests**

Run: `uv run --extra dev pytest tests/test_cursor_integration.py -k project tests/test_integration_config_io.py tests/test_integration_ownership.py -q`

Expected: portable, idempotent, preserved, legacy, conflict, symlink, and explicit-command tests pass.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/integrations/cursor.py src/memory/integrations/registry.py src/memory/setup.py tests/conftest.py tests/integration_helpers.py tests/test_cursor_integration.py tests/test_setup.py
git commit -m "feat: install cursor project memory"
~~~

### Task 6: Cursor User Local Plugin

**Files:**
- Modify: src/memory/integrations/cursor.py
- Modify: tests/test_cursor_integration.py

**Interfaces:**
- User target: CURSOR_CONFIG/plugins/local/echovault
- Resolves and validates absolute memory executable
- Stages and validates native plugin before replacing current owned tree

- [ ] **Step 1: Write failing global plugin tests**

~~~python
def test_user_setup_installs_valid_local_plugin_with_absolute_command(tmp_path: Path, fake_memory: Path) -> None:
    cursor_root = tmp_path / '.cursor'
    result = cursor_adapter().setup(user_options(cursor_root, command=str(fake_memory)))
    plugin = cursor_root / 'plugins/local/echovault'
    config = json.loads((plugin / 'mcp.json').read_text())
    assert Path(config['mcpServers']['echovault']['command']).is_absolute()
    assert config['mcpServers']['echovault']['command'] == str(fake_memory)
    assert (plugin / '.cursor-plugin/plugin.json').is_file()
    assert (plugin / '.echovault-managed.json').is_file()
    assert 'restart' in result.message.lower() or 'reload' in result.message.lower()


def test_unmarked_existing_plugin_directory_is_a_conflict(tmp_path: Path, fake_memory: Path) -> None:
    plugin = tmp_path / '.cursor/plugins/local/echovault'
    plugin.mkdir(parents=True)
    (plugin / 'user.txt').write_text('mine')
    with pytest.raises(OwnershipConflict):
        cursor_adapter().setup(user_options(tmp_path / '.cursor', command=str(fake_memory)))
    assert (plugin / 'user.txt').read_text() == 'mine'
~~~

- [ ] **Step 2: Run global plugin tests and observe direct-MCP behavior**

Run: `uv run --extra dev pytest tests/test_cursor_integration.py -k "user or plugin" -q`

Expected: setup still creates only mcp.json or removes the skill.

- [ ] **Step 3: Render, validate, and atomically install the plugin**

Resolve command with shutil.which unless --command supplies a path. Require an executable regular file. Render a sibling staging tree, validate JSON, manifest identity/version, every declared rule/skill/MCP path, ownership hashes, and absence of path traversal. Atomically rename only after validation. Never write a private Cursor registry.

After plugin validation, remove an exact owned legacy user mcpServers.echovault entry from CURSOR_CONFIG/mcp.json. If that entry is custom, keep it and fail setup before replacing the active plugin so duplicate effective configuration cannot be reported as success.

- [ ] **Step 4: Run global and legacy Cursor tests**

Run: `uv run --extra dev pytest tests/test_cursor_integration.py tests/test_setup.py -k cursor -q`

Expected: plugin tree, absolute command, staging, validation, legacy cleanup, conflict, and compatibility wrapper tests pass.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/integrations/cursor.py tests/test_cursor_integration.py tests/test_setup.py
git commit -m "feat: install cursor local plugin"
~~~

### Task 7: Cursor Coexistence, Upgrade, and Scope-Exact Uninstall

**Files:**
- Modify: src/memory/integrations/cursor.py
- Modify: tests/test_cursor_integration.py

**Interfaces:**
- Handles no artifacts, exact legacy, current owned, older owned, modified owned, custom conflict, malformed config, and global+project states
- Removes only requested scope and claimed artifacts

- [ ] **Step 1: Parameterize the migration/ownership matrix**

~~~python
def seed_cursor_state(tmp_path: Path, state: str) -> tuple[CursorAdapter, IntegrationOptions]:
    project = tmp_path / state
    project.mkdir()
    adapter = cursor_adapter()
    options = project_options(project)
    cursor = project / '.cursor'
    if state == 'none':
        return adapter, options
    if state == 'legacy_exact':
        cursor.mkdir()
        (cursor / 'mcp.json').write_text(json.dumps({
            'mcpServers': {
                'echovault': {
                    'command': 'memory',
                    'args': ['mcp'],
                    'type': 'stdio',
                },
            },
        }))
        return adapter, options
    if state == 'custom_same_name':
        cursor.mkdir()
        (cursor / 'mcp.json').write_text(json.dumps({
            'mcpServers': {'echovault': {'command': 'user-memory'}},
        }))
        return adapter, options
    if state == 'malformed':
        cursor.mkdir()
        (cursor / 'mcp.json').write_text('{"mcpServers":')
        return adapter, options
    adapter.setup(options)
    if state == 'older_owned':
        manifest_path = cursor / '.echovault-managed.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['asset_version'] = '0.5.0'
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
    elif state == 'modified_owned':
        (cursor / 'rules' / 'echovault.mdc').write_text('user edit\n')
    elif state != 'current_owned':
        raise AssertionError(f'unknown fixture state: {state}')
    return adapter, options


def run_cursor_matrix_case(tmp_path: Path, state: str, force: bool) -> str:
    adapter, options = seed_cursor_state(tmp_path, state)
    try:
        return adapter.setup(replace(options, force_managed=force)).status
    except OwnershipConflict:
        return 'conflict'
    except ConfigMalformedError:
        return 'parse_error'


@pytest.mark.parametrize(
    ('state', 'force', 'expected'),
    [
        ('none', False, 'installed'),
        ('legacy_exact', False, 'updated'),
        ('current_owned', False, 'unchanged'),
        ('older_owned', False, 'updated'),
        ('modified_owned', False, 'conflict'),
        ('modified_owned', True, 'updated'),
        ('custom_same_name', False, 'conflict'),
        ('malformed', False, 'parse_error'),
    ],
)
def test_cursor_setup_matrix(state: str, force: bool, expected: str, tmp_path: Path) -> None:
    outcome = run_cursor_matrix_case(tmp_path, state, force)
    assert outcome == expected
~~~

Add a second parameterized table for uninstall. Seed both user and project scope, select exactly one scope, and assert: global uninstall leaves project files; project uninstall leaves the global plugin; current/older owned are removed; no artifacts returns unchanged; modified owned conflicts without force and is removed with force; custom/malformed targets remain byte-identical even with force. Use `snapshot_tree` before and after to prove all non-selected and unrelated paths survive.

- [ ] **Step 2: Run matrix tests and observe destructive legacy behavior**

Run: `uv run --extra dev pytest tests/test_cursor_integration.py -k "matrix or uninstall or coexist" -q`

Expected: at least modified/custom/scope cases fail.

- [ ] **Step 3: Implement the normative state transitions**

Use ownership hashes and exact structural legacy recognition, not filenames alone. Global+project is healthy: project MCP shadows global by Cursor workspace precedence, while identical versioned rule/skill content is semantically idempotent. Report shadowing; do not delete the other scope. Uninstall reads the selected scope manifest, verifies claims, mutates only those entries/blocks/files, removes empty owned directories, and preserves all unrelated content.

- [ ] **Step 4: Run the full Cursor matrix**

Run: `uv run --extra dev pytest tests/test_cursor_integration.py -q`

Expected: every setup/upgrade/conflict/coexistence/uninstall row passes.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/integrations/cursor.py tests/test_cursor_integration.py
git commit -m "feat: migrate and uninstall cursor safely"
~~~

### Task 8: Cursor CLI Contract and Read-Only Diagnostics

**Files:**
- Create: src/memory/integrations/diagnostics.py
- Modify: src/memory/integrations/cursor.py
- Modify: src/memory/cli.py
- Modify: src/memory/health.py
- Create: tests/test_integration_diagnostics.py
- Modify: tests/test_cli.py
- Modify: tests/test_mcp_protocol.py

**Interfaces:**
- CLI setup cursor supports --project, --config-dir, --command, --force-managed
- CLI uninstall cursor supports --project, --config-dir, --force-managed and rejects --command
- CLI doctor supports --agent cursor and resolves project context from the current working directory
- Consumes: DiagnosticFinding and CommandRunner from Task 1

- [ ] **Step 1: Write failing option and read-only doctor tests**

~~~python
class RecordingRunner:
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.calls: list[tuple[tuple[str, ...], Path | None, dict[str, str] | None]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        capture_output: bool = True,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        self.calls.append((tuple(argv), cwd, None if env is None else dict(env)))
        return self.result


def install_cursor_fixture(tmp_path: Path) -> tuple[MemoryService, Path]:
    memory_home = tmp_path / 'memory-home'
    project = tmp_path / 'repo'
    project.mkdir()
    cursor_adapter().setup(project_options(project))
    return MemoryService(str(memory_home)), project


def test_cursor_cli_option_matrix(tmp_path: Path, fake_memory: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(main, [
        'setup', 'cursor', '--project', '--config-dir', str(tmp_path / '.cursor'),
        '--command', str(fake_memory), '--force-managed',
    ])
    assert result.exit_code == 0


def test_cursor_uninstall_rejects_setup_only_command_option() -> None:
    result = CliRunner().invoke(main, [
        'uninstall', 'cursor', '--project', '--command', '/opt/echovault/bin/memory',
    ])
    assert result.exit_code == 2
    assert 'No such option: --command' in result.output


def test_cursor_doctor_is_read_only(tmp_path: Path) -> None:
    service, project = install_cursor_fixture(tmp_path)
    fake_runner = RecordingRunner(CommandResult(0, 'echovault: connected\n', ''))
    before = snapshot_tree(tmp_path)
    report = doctor(service, agent='cursor', project_root=project, runner=fake_runner)
    assert snapshot_tree(tmp_path) == before
    assert {finding['code'] for finding in report['integration_findings']} >= {
        'cursor.scope', 'cursor.manifest', 'cursor.mcp', 'cursor.rule', 'cursor.skill'
    }
    assert all(call[1] == project.resolve() for call in fake_runner.calls)


def test_cursor_doctor_reports_effective_disabled_policy(tmp_path: Path) -> None:
    service, project = install_cursor_fixture(tmp_path)
    service.config.context.agent_modes['cursor'] = 'off'
    report = doctor(service, agent='cursor', project_root=project, runner=RecordingRunner(
        CommandResult(127, '', 'not installed')
    ))
    policy = next(item for item in report['integration_findings'] if item['code'] == 'cursor.context-policy')
    assert policy['mode'] == 'off'
    assert policy['source'] == 'agent:cursor'
~~~

Append the bound-policy case to the existing Phase-2 real-protocol module
`tests/test_mcp_protocol.py`. Reuse its produced transport instead of adding a
second direct-dispatch test seam:

~~~python
from memory.core import MemoryService
from memory.mcp_authority import MCPServerBinding
from memory.projects import ProjectRegistry
from tests.mcp_helpers import decode_object, make_workspace, open_test_client


@pytest.mark.anyio
async def test_cursor_bound_context_off_does_not_fall_back_to_search(
    tmp_path: Path, monkeypatch,
) -> None:
    root = make_workspace(tmp_path / 'repo')
    memory_home = tmp_path / 'memory-home'
    service = MemoryService(str(memory_home))
    service.config.context.agent_modes['cursor'] = 'off'
    search_calls = 0

    def fail_if_searched(*args, **kwargs):
        nonlocal search_calls
        search_calls += 1
        raise AssertionError('disabled context must not search')

    monkeypatch.setattr(service, 'search', fail_if_searched)
    try:
        async with open_test_client(
            service,
            MCPServerBinding('cursor', root, root),
            ProjectRegistry(memory_home),
        ) as client:
            response = decode_object(await client.call_tool(
                'memory_context',
                {'query': 'task'},
            ))
        assert response['disabled'] is True
        assert response['policy']['mode'] == 'off'
        assert response['policy']['source'] == 'agent:cursor'
        assert response['memories'] == []
        assert search_calls == 0
    finally:
        service.close()
~~~

- [ ] **Step 2: Run CLI/diagnostic tests and observe unknown options**

Run: `uv run --extra dev pytest tests/test_cli.py tests/test_integration_diagnostics.py tests/test_mcp_protocol.py -k cursor -q`

Expected: Click rejects command/force-managed or doctor lacks agent findings;
the Phase-2 disabled-context regression remains green and proves no search
fallback while Task 8 changes only adapter diagnostics and CLI wiring.

- [ ] **Step 3: Wire adapter options and diagnostics**

Use DiagnosticFinding and CommandRunner from Task 1. Diagnostics never instantiate SubprocessRunner internally when a runner argument was supplied by tests or the caller.

Doctor checks EchoVault version/executable, selected scope, manifests/hashes, rule/skill/MCP, portable command resolution, project identity/aliases, four-tool expectations, effective context mode plus its precedence source, client version/capability, global/project shadowing, and Cursor Cloud Agent local-vault unavailability. When the agent CLI exists, run read-only `agent mcp list` and `agent mcp list-tools echovault` through an injected runner with `cwd=project_root.resolve()`. Missing/old CLI is degraded, never a write or deterministic IDE claim. Preserve Phase 2's disabled bound `memory_context` shape (`disabled`, nested `policy`, empty `memories`) and its no-search behavior; the rule/skill tells the model to continue instead of silently falling back to `memory_search`.

- [ ] **Step 4: Run Cursor, CLI, diagnostics, and legacy setup suites**

Run: `uv run --extra dev pytest tests/test_cursor_integration.py tests/test_integration_diagnostics.py tests/test_cli.py tests/test_setup.py tests/test_mcp_server.py tests/test_mcp_protocol.py -q`

Expected: exact CLI matrix, read-only diagnostics, and old agents pass.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/integrations/diagnostics.py src/memory/integrations/cursor.py src/memory/cli.py src/memory/health.py tests/test_integration_diagnostics.py tests/test_cli.py tests/test_mcp_protocol.py
git commit -m "feat: diagnose cursor memory integration"
~~~

### Task 9: Cursor Integration Gate

**Files:**
- Modify: tests/test_cursor_integration.py
- Modify: tests/test_integration_diagnostics.py
- Modify: README.md

**Interfaces:**
- Verifies isolated user/project homes and package-resource rendering
- Documents deterministic files/tools separately from policy-guided model behavior

- [ ] **Step 1: Add isolated-home end-to-end contract**

~~~python
def test_cursor_project_contract_from_cli(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / 'home'
    project = tmp_path / 'repo'
    home.mkdir()
    project.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(project)
    result = CliRunner().invoke(main, ['setup', 'cursor', '--project'])
    assert result.exit_code == 0
    doctor_result = CliRunner().invoke(main, ['doctor', '--agent', 'cursor'])
    assert doctor_result.exit_code == 0
    result = CliRunner().invoke(main, ['uninstall', 'cursor', '--project'])
    assert result.exit_code == 0
    assert not (project / '.cursor/rules/echovault.mdc').exists()
~~~

- [ ] **Step 2: Run the full Cursor/legacy gate**

Run: `uv run --extra dev pytest tests/test_cursor_integration.py tests/test_integration_config_io.py tests/test_integration_ownership.py tests/test_integration_diagnostics.py tests/test_setup.py tests/test_cli.py -q`

Expected: all tests pass. If a fixture fails, capture its exact state and fix only the violated contract.

- [ ] **Step 3: Document current Cursor behavior**

README must give exact global/project setup, uninstall, doctor, reload, and migration commands. State plainly that the rule instructs task-start retrieval but Cursor controls whether a model calls MCP; deterministic evidence is installation/tool availability, while actual marker retrieval belongs to authenticated smoke testing.

- [ ] **Step 4: Run baseline and inspect changes**

Run: `uv run --extra dev pytest -q`

Expected: full Python suite passes.

Run: `git diff --check`

Expected: no whitespace errors.

- [ ] **Step 5: Commit**

~~~bash
git add tests/test_cursor_integration.py tests/test_integration_diagnostics.py README.md
git commit -m "docs: document cursor memory integration"
~~~

## Phase Completion Gate

Run:

~~~bash
uv run --extra dev pytest -q
memory setup cursor --help
memory uninstall cursor --help
memory doctor --agent cursor
git status --short
~~~

Expected: tests pass, help matches the approved matrix, doctor changes no files, and a rendered plugin contains one bound MCP definition plus the same versioned rule/skill policy as the project fallback.
