# EchoVault Packaging, CI, Documentation, and Dogfood Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove that the completed cross-agent implementation ships from an installed wheel, remains backward compatible on every supported platform, and has honest reproducible client-smoke evidence before replacing the local EchoVault installation.

**Architecture:** Package one canonical integration asset tree, validate rendered client bundles before distribution, exercise the installed wheel outside the checkout, and separate deterministic CI evidence from authenticated model-client observations. Documentation and dogfood reports consume the same command matrix and capability boundaries implemented by the adapters.

**Tech Stack:** Python 3.10–3.14, setuptools, uv, pytest, zipfile/tarfile, GitHub Actions, Rust/Cargo, Node.js, Cursor IDE 3.11.19, Cursor Agent CLI build 2026.07.09-a3815c0, Gemini CLI 0.50.0.

## Global Constraints

- Complete the Local Core, Bound MCP, Cursor, and Gemini plans before this plan.
- The installed wheel is the test subject for packaging contracts; importing the source checkout is not acceptable evidence.
- Keep one handwritten curated-behavior source under memory package data. Any repository-facing copy is generated and byte-checked.
- The wheel contains every Cursor, Gemini, common skill, ownership-schema, and hook resource needed at runtime.
- Deterministic CI never requires a model account, GUI session, cloud embedding provider, or trust bypass.
- Authenticated smoke checks are non-blocking evidence. Unavailable binaries, wrong versions, or missing authentication produce not_verified, never a fabricated pass.
- Keep Cursor IDE and Cursor Agent CLI evidence separate: `agent --version` is compared only with build `2026.07.09-a3815c0`; IDE `3.11.19` is recorded only by the manual IDE check.
- Cursor rule compliance remains observational. Gemini hook output, MCP protocol behavior, package structure, and setup state are deterministic.
- Smoke reports never retain prompt bodies, model responses, transcripts, tokens, secrets, or unrelated environment values.
- Native client install commands retain their normal consent and trust prompts. Do not pass Gemini --consent and do not enable Cursor trust automatically.
- The local tool replacement occurs only after source tests, Rust tests, build, installed-wheel contracts, and validators pass.
- Do not push, publish, tag, submit to a marketplace, create a release, or open a pull request in this phase.
- Begin each behavior change with an observed failing regression test.

---

## File Responsibility Map

- **Create: src/memory/integrations/assets/schemas/ownership-manifest.schema.json** — packaged schema for managed artifacts.
- **Modify: src/memory/integrations/asset_io.py** — package-resource enumeration only; no source-tree fallback.
- **Modify: src/memory/setup.py** — remove the handwritten fallback and source-checkout dependency.
- **Modify: pyproject.toml and uv.lock** — explicit package data and release-test dependencies.
- **Create: scripts/sync_echovault_skill.py** — generate and verify the repository skill mirror.
- **Create: scripts/render_integration_fixture.py** — render Cursor or Gemini bundles for validation.
- **Create: scripts/validate_cursor_plugin.py** — checked-in structural validator.
- **Create: scripts/verify_wheel_assets.py** — inspect wheel and sdist contents.
- **Create: scripts/verify_installed_tool.py** — isolated-home setup and MCP contracts against a wheel installation.
- **Create: scripts/smoke_clients.py** — sanitized pinned-client probe and headless evidence runner.
- **Modify: tests/mcp_helpers.py** — extend the Phase 2 real-protocol helper with subprocess transport.
- **Create: tests/test_packaging.py** — canonical resource and archive contracts.
- **Create: tests/test_release_validators.py** — validator success and failure cases.
- **Create: tests/test_backward_compatibility.py** — explicit legacy surface gate.
- **Create: tests/test_smoke_clients.py** — command construction, sanitization, and status semantics.
- **Create: .github/workflows/ci.yml** — Python matrix, package validation, and Rust job.
- **Create: docs/integrations/cursor.md and docs/integrations/gemini-cli.md** — supported setup and diagnostics.
- **Create: docs/migrations/cross-agent-v0.6.md** — legacy migration/coexistence guide.
- **Create: docs/security-and-privacy.md** — local/remote and prompt-retention guarantees.
- **Create: docs/dogfood/README.md and docs/dogfood/report-template.md** — evidence procedure and schema.
- **Modify: .gitignore** — stop broadly ignoring all support documentation before adding the new guides.
- **Modify: README.md and CHANGELOG.md** — discoverability and release notes without publishing.

### Task 1: Canonical Wheel Assets and Generated Skill Mirror

**Files:**
- Create: src/memory/integrations/assets/schemas/ownership-manifest.schema.json
- Create: scripts/sync_echovault_skill.py
- Create: tests/test_packaging.py
- Modify: src/memory/integrations/asset_io.py
- Modify: src/memory/setup.py
- Modify: pyproject.toml
- Modify: uv.lock
- Modify: skills/echovault/SKILL.md

**Interfaces:**
- Produces: REQUIRED_PACKAGE_ASSETS: tuple[str, ...]
- Produces: read_package_asset(relative_path: str) -> bytes
- Produces: sync_skill(destination: Path, check: bool) -> bool
- Removes: _FALLBACK_SKILL_MD and every repository-relative runtime asset lookup
- Sets the fork package version to 0.6.0 without publishing or tagging it

- [ ] **Step 1: Write failing package-resource and single-source tests**

~~~python
import importlib.metadata
from importlib.resources import files
from pathlib import Path

from memory.integrations.asset_io import REQUIRED_PACKAGE_ASSETS, read_package_asset


EXPECTED_ASSETS = {
    'common/echovault-skill.md',
    'cursor/plugin.json',
    'cursor/mcp.json',
    'cursor/echovault.mdc',
    'gemini/gemini-extension.json',
    'gemini/GEMINI.md',
    'gemini/hooks.json',
    'schemas/ownership-manifest.schema.json',
}


def test_required_package_assets_are_explicit_and_readable() -> None:
    assert set(REQUIRED_PACKAGE_ASSETS) == EXPECTED_ASSETS
    for relative_path in REQUIRED_PACKAGE_ASSETS:
        assert read_package_asset(relative_path).strip()


def test_repository_skill_is_generated_from_canonical_resource() -> None:
    repository_skill = Path('skills/echovault/SKILL.md').read_bytes()
    assert repository_skill == read_package_asset('common/echovault-skill.md')


def test_runtime_assets_resolve_inside_memory_package() -> None:
    package_root = files('memory').joinpath('integrations/assets')
    assert package_root.joinpath('cursor/plugin.json').is_file()


def test_cross_agent_build_has_distinct_package_version() -> None:
    assert importlib.metadata.version('echovault') == '0.6.0'
~~~

- [ ] **Step 2: Run packaging tests and observe the missing schema/package declarations**

Run: `uv run --extra dev pytest tests/test_packaging.py -q`

Expected: the schema or REQUIRED_PACKAGE_ASSETS contract is missing, and the repository skill is not yet proven to be generated from package data.

- [ ] **Step 3: Add the ownership schema and exact package-data declaration**

The schema is draft 2020-12 JSON Schema. It requires schema_version, integration_id, asset_version, and managed. Each managed item requires path, kind, and sha256; kind is one of file, json-entry, or marked-block. locator and marker are nullable strings. additionalProperties is false at both object levels.

Add this exact package-data boundary:

~~~toml
[tool.setuptools.package-data]
memory = [
    "integrations/assets/common/*.md",
    "integrations/assets/cursor/*.json",
    "integrations/assets/cursor/*.mdc",
    "integrations/assets/gemini/*.json",
    "integrations/assets/gemini/*.md",
    "integrations/assets/schemas/*.json",
]
~~~

Set project.version to "0.6.0" and refresh uv.lock. Keep the existing packages.find configuration. Do not package docs, tests, client homes, generated reports, or source-tree-only fallback text.

- [ ] **Step 4: Make package resources the only runtime source**

In asset_io.py define the exact tuple from Step 1 and load with importlib.resources.files('memory').joinpath('integrations/assets', relative_path).read_bytes(). Reject paths not present in REQUIRED_PACKAGE_ASSETS.

In setup.py remove _FALLBACK_SKILL_MD and _get_skill_md_path. Compatibility setup functions must call read_package_asset('common/echovault-skill.md'). A missing packaged resource raises IntegrationAssetError naming only the relative resource, not local environment paths.

scripts/sync_echovault_skill.py imports read_package_asset, writes through `prepare_atomic_text` from Phase 1, and supports:

~~~text
python scripts/sync_echovault_skill.py
python scripts/sync_echovault_skill.py --check
~~~

--check exits 0 only when skills/echovault/SKILL.md is byte-identical and makes no write.

- [ ] **Step 5: Regenerate the mirror and run focused tests**

Run: `uv run python scripts/sync_echovault_skill.py`

Expected: the repository skill becomes byte-identical to the canonical package resource.

Run: `uv run --extra dev pytest tests/test_packaging.py tests/test_setup.py -q`

Expected: package-resource and all legacy setup tests pass.

- [ ] **Step 6: Commit**

~~~bash
git add src/memory/integrations/assets/schemas/ownership-manifest.schema.json src/memory/integrations/asset_io.py src/memory/setup.py scripts/sync_echovault_skill.py tests/test_packaging.py skills/echovault/SKILL.md pyproject.toml uv.lock
git commit -m "build: package canonical integration assets"
~~~

### Task 2: Rendered Bundle and Distribution Validators

**Files:**
- Create: scripts/render_integration_fixture.py
- Create: scripts/validate_cursor_plugin.py
- Create: scripts/verify_wheel_assets.py
- Create: tests/test_release_validators.py
- Modify: tests/test_packaging.py

**Interfaces:**
- Produces: render_fixture(client: Literal['cursor', 'gemini'], output: Path, memory_command: str, version: str) -> Path
- Produces: validate_cursor_plugin(root: Path) -> tuple[str, ...]
- Produces: inspect_wheel(path: Path) -> ArchiveInspection
- Produces: inspect_sdist(path: Path) -> ArchiveInspection

- [ ] **Step 1: Write failing validator tests with valid and corrupted fixtures**

~~~python
import json
from pathlib import Path

from scripts.render_integration_fixture import render_fixture
from scripts.validate_cursor_plugin import validate_cursor_plugin


def test_rendered_cursor_bundle_passes_checked_in_validator(tmp_path: Path) -> None:
    root = render_fixture(
        client='cursor',
        output=tmp_path / 'cursor',
        memory_command='/opt/echovault/bin/memory',
        version='0.6.0',
    )
    assert validate_cursor_plugin(root) == ()


def test_cursor_validator_reports_broken_mcp_reference(tmp_path: Path) -> None:
    root = render_fixture(
        client='cursor',
        output=tmp_path / 'cursor',
        memory_command='/opt/echovault/bin/memory',
        version='0.6.0',
    )
    manifest_path = root / '.cursor-plugin/plugin.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['mcpServers'] = 'missing.json'
    manifest_path.write_text(json.dumps(manifest))
    assert validate_cursor_plugin(root) == (
        '.cursor-plugin/plugin.json: mcpServers points to missing.json',
    )


def test_fixture_renderer_refuses_nonempty_output(tmp_path: Path) -> None:
    output = tmp_path / 'bundle'
    output.mkdir()
    (output / 'user.txt').write_text('preserve')
    with pytest.raises(FileExistsError):
        render_fixture('gemini', output, 'memory', '0.6.0')
~~~

- [ ] **Step 2: Run validator tests and observe missing scripts**

Run: `uv run --extra dev pytest tests/test_release_validators.py -q`

Expected: collection fails because the release validator modules do not exist.

- [ ] **Step 3: Implement deterministic fixture rendering**

render_integration_fixture.py accepts exactly:

~~~text
--client cursor|gemini
--output PATH
--memory-command PATH_OR_NAME
--version VERSION
~~~

For Cursor, call render_cursor_assets(command=memory_command, version=version). For Gemini, derive the hook command from the same executable and call render_gemini_assets(memory_command=memory_command, hook_command=quoted_hook_command, version=version). Create the output only when absent or empty, write every returned relative path with prepared atomic writes, then print the absolute root path.

Do not read user configuration or invoke either client.

- [ ] **Step 4: Implement the checked-in Cursor validator**

validate_cursor_plugin(root) returns sorted human-readable errors and never mutates root. It validates:

- .cursor-plugin/plugin.json is valid JSON with name echovault, semantic version, skills ./skills/, rules ./rules/, and mcpServers mcp.json;
- each referenced path remains under root and exists;
- mcp.json contains exactly one EchoVault definition with args ['mcp', '--agent', 'cursor'] and no trust or environment field;
- rules/echovault.mdc contains valid frontmatter with alwaysApply true;
- skills/echovault/SKILL.md and the rule contain all four MCP tool names, curated-save/idempotency guidance, and the no-transcript rule;
- no unresolved double-brace template marker remains.

The CLI exits 0 with no output on success and exits 1 with one stderr line per error.

- [ ] **Step 5: Implement wheel and sdist inspection**

Use only zipfile, tarfile, pathlib, dataclasses, and argparse. ArchiveInspection contains path, members, missing_assets, duplicate_members, and unsafe_members. Reject absolute paths and parent traversal. For wheels, require each asset below memory/integrations/assets. For sdists, find the single top-level distribution directory and require the same asset suffixes plus pyproject.toml.

The CLI accepts one or more archive paths and exits nonzero if any archive is unsafe, missing an asset, or contains a duplicate member.

- [ ] **Step 6: Run validators against source fixtures**

Run:

~~~bash
rm -rf build/validation
uv run python scripts/render_integration_fixture.py --client cursor --output build/validation/cursor --memory-command /opt/echovault/bin/memory --version 0.6.0
uv run python scripts/validate_cursor_plugin.py build/validation/cursor
uv run python scripts/render_integration_fixture.py --client gemini --output build/validation/gemini --memory-command /opt/echovault/bin/memory --version 0.6.0
uv run --extra dev pytest tests/test_release_validators.py tests/test_packaging.py -q
~~~

Expected: both fixture trees render, Cursor validation succeeds, and focused tests pass. Gemini CLI validation is added as a pinned external CI gate in Task 4.

- [ ] **Step 7: Commit**

~~~bash
git add scripts/render_integration_fixture.py scripts/validate_cursor_plugin.py scripts/verify_wheel_assets.py tests/test_release_validators.py tests/test_packaging.py
git commit -m "build: validate native integration bundles"
~~~

### Task 3: Installed-Wheel and Backward-Compatibility Contracts

**Files:**
- Modify: tests/mcp_helpers.py
- Create: tests/test_backward_compatibility.py
- Create: scripts/verify_installed_tool.py
- Modify: tests/test_mcp_protocol.py
- Modify: tests/test_packaging.py

**Interfaces:**
- Produces: open_stdio_session(memory_executable: Path, args: Sequence[str], env: Mapping[str, str])
- Produces: verify_installed_tool(memory_executable: Path) -> VerificationReport
- Explicitly freezes the unbound MCP and legacy setup surface

- [ ] **Step 1: Extract one real-protocol test helper and add failing compatibility tests**

Extend tests/mcp_helpers.py with a second async context manager that uses mcp.client.stdio.stdio_client, StdioServerParameters, and ClientSession. It starts the supplied executable, initializes the session, yields it, and always closes the process; keep the Phase 2 in-memory helper unchanged.

~~~python
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@asynccontextmanager
async def open_stdio_session(
    memory_executable: Path,
    args: Sequence[str],
    env: Mapping[str, str],
) -> AsyncIterator[ClientSession]:
    parameters = StdioServerParameters(
        command=str(memory_executable),
        args=list(args),
        env=dict(env),
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session
~~~

Define the executable, environment, and schema-v1 fixtures in tests/test_backward_compatibility.py before the tests:

~~~python
@pytest.fixture
def memory_executable() -> Path:
    name = 'memory.exe' if os.name == 'nt' else 'memory'
    executable = Path(sys.executable).resolve().with_name(name)
    assert executable.is_file()
    return executable.resolve()


@pytest.fixture
def isolated_environment(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / 'home'
    memory_home = home / '.memory'
    command_bin = tmp_path / 'command-bin'
    home.mkdir()
    command_bin.mkdir()
    retained = {
        key: os.environ[key]
        for key in ('SystemRoot', 'WINDIR', 'PATHEXT', 'COMSPEC', 'TMP', 'TEMP')
        if key in os.environ
    }
    return {
        **retained,
        'PATH': str(command_bin),
        'HOME': str(home),
        'USERPROFILE': str(home),
        'XDG_CONFIG_HOME': str(home / '.config'),
        'XDG_DATA_HOME': str(home / '.local/share'),
        'MEMORY_HOME': str(memory_home),
        'PYTHONPATH': '',
    }


@pytest.fixture
def v1_service(tmp_path: Path):
    memory_home = tmp_path / 'memory-home'
    session = memory_home / 'vault/legacy/2026-07-14-session.md'
    session.parent.mkdir(parents=True)
    session.write_text(
        '---\nproject: legacy\n---\n\n'
        '# Session\n\n### Legacy memory\n'
        '**What:** legacy marker remains readable\n',
        encoding='utf-8',
    )
    service = MemoryService(str(memory_home))
    service.import_from_vault()
    try:
        yield service
    finally:
        service.db.close()


@pytest.mark.anyio
async def test_unbound_mcp_keeps_legacy_save_schema(
    memory_executable: Path,
    isolated_environment: dict[str, str],
) -> None:
    async with open_stdio_session(
        memory_executable,
        ['mcp'],
        isolated_environment,
    ) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
    save_schema = tools['memory_save'].inputSchema
    assert 'idempotency_key' not in save_schema.get('required', [])
    assert set(tools) == {
        'memory_context', 'memory_search', 'memory_details', 'memory_save'
    }


@pytest.mark.parametrize('client', ['claude', 'codex', 'opencode'])
def test_existing_setup_command_remains_available(client: str) -> None:
    result = CliRunner().invoke(main, ['setup', client, '--help'])
    assert result.exit_code == 0


def test_schema_v1_vault_remains_searchable(v1_service: MemoryService) -> None:
    results = v1_service.search('legacy marker', project='legacy', use_vectors=False)
    assert [result['title'] for result in results] == ['Legacy memory']
~~~

- [ ] **Step 2: Run the compatibility slice and observe missing helper/frozen cases**

Run: `uv run --extra dev pytest tests/test_backward_compatibility.py tests/test_mcp_protocol.py -q`

Expected: new tests fail until the shared protocol helper and explicit legacy fixtures exist.

- [ ] **Step 3: Implement the installed-tool verifier as a black-box harness**

verify_installed_tool.py accepts --memory-executable and optional --keep-home. It creates a temporary HOME, XDG_CONFIG_HOME, XDG_DATA_HOME, USERPROFILE, and a temporary Git project outside the source checkout. It removes PYTHONPATH and sets MEMORY_HOME inside that temporary home.

Resolve `--memory-executable` immediately with `Path.resolve(strict=True)`, require an executable regular file, and use that absolute path for every EchoVault subprocess and MCP session. The harness creates a private `command-bin` containing only explicit non-agent test shims needed by the platform; `PATH` never inherits the developer machine and never exposes a real `agent`, `cursor-agent`, or `gemini` binary. It passes absolute `cwd` and the controlled environment to every subprocess. The harness:

1. runs memory --help;
2. runs global Cursor setup and doctor, validates its installed bundle, reruns setup, and asserts a byte snapshot is unchanged;
3. runs project Cursor setup and uninstall, asserting unrelated JSON/rule content survives;
4. runs Gemini project-direct setup and doctor without requiring a Gemini account, reruns setup, checks byte stability, and invokes the packaged BeforeAgent command once with a nonsecret retrieval-query nonce plus a sentinel transcript path;
5. starts memory mcp unbound and bound for Cursor through the real MCP SDK;
6. lists the four tools, saves a marker with a fixed UUID through the bound server, retries it and requires replayed, then reads the same memory through unbound details;
7. verifies no file under the temporary homes contains the retrieval-query nonce or sentinel transcript path;
8. uninstalls only managed artifacts and requires unrelated sentinel files to remain.

Before Step 1, assert the temporary Git project is outside both `Path.cwd().resolve()` and the installed distribution location. Assert imports resolve from the wheel environment, `PYTHONPATH` is empty, and `shutil.which('agent', path=env['PATH'])`, `shutil.which('cursor-agent', path=env['PATH'])`, and `shutil.which('gemini', path=env['PATH'])` all return `None`. The deterministic wheel verifier must therefore use project-direct Gemini and injected diagnostic runners, never a real client binary.

VerificationReport contains named checks and sanitized diagnostics. The process exits 0 only when every deterministic check passes. --keep-home prints the retained path and is forbidden in CI.

- [ ] **Step 4: Build and test a wheel outside the checkout**

Run:

~~~bash
rm -rf dist build/wheel-venv
uv build
uv run python scripts/verify_wheel_assets.py dist/*.whl dist/*.tar.gz
uv venv build/wheel-venv --python 3.14
uv pip install --python build/wheel-venv/bin/python dist/*.whl
build/wheel-venv/bin/python scripts/verify_installed_tool.py --memory-executable build/wheel-venv/bin/memory
~~~

Expected: archives contain every canonical asset and the verifier passes from temporary directories with PYTHONPATH removed.

- [ ] **Step 5: Run the complete legacy suite**

Run: `uv run --extra dev pytest tests/test_backward_compatibility.py tests/test_setup.py tests/test_cli.py tests/test_mcp_server.py tests/test_mcp_protocol.py tests/test_markdown.py tests/test_import.py -q`

Expected: unbound MCP, Claude Code, Codex, OpenCode, schema-v1 reads, setup, and import compatibility all pass.

- [ ] **Step 6: Commit**

~~~bash
git add tests/mcp_helpers.py tests/test_backward_compatibility.py tests/test_mcp_protocol.py tests/test_packaging.py scripts/verify_installed_tool.py
git commit -m "test: verify installed wheel compatibility"
~~~

### Task 4: Cross-Platform CI and Pinned Native Validators

**Files:**
- Create: .github/workflows/ci.yml
- Modify: README.md

**Interfaces:**
- Runs Python tests on nine operating-system/version cells
- Runs packaging gates on Ubuntu/Python 3.14
- Runs Rust dashboard tests in a separate job
- Uses Gemini CLI 0.50.0 only for extension validation

- [ ] **Step 1: Add a workflow-shape regression test**

Add to tests/test_packaging.py:

~~~python
def test_ci_matrix_and_pinned_validator_are_explicit() -> None:
    workflow = yaml.safe_load(Path('.github/workflows/ci.yml').read_text())
    matrix = workflow['jobs']['python-tests']['strategy']['matrix']['include']
    cells = {(item['os'], str(item['python'])) for item in matrix}
    assert cells == {
        ('ubuntu-latest', '3.10'),
        ('ubuntu-latest', '3.11'),
        ('ubuntu-latest', '3.12'),
        ('ubuntu-latest', '3.13'),
        ('ubuntu-latest', '3.14'),
        ('macos-latest', '3.10'),
        ('macos-latest', '3.14'),
        ('windows-latest', '3.10'),
        ('windows-latest', '3.14'),
    }
    package_steps = workflow['jobs']['package-gate']['steps']
    serialized = json.dumps(package_steps)
    assert '@google/gemini-cli@0.50.0' in serialized
    assert 'gemini extensions validate' in serialized
    assert 'verify_installed_tool.py' in serialized
    assert 'cursor-agent' not in serialized
~~~

- [ ] **Step 2: Run the workflow test and observe the missing file**

Run: `uv run --extra dev pytest tests/test_packaging.py -k ci_matrix -q`

Expected: .github/workflows/ci.yml is missing.

- [ ] **Step 3: Add the Python matrix**

ci.yml uses actions/checkout@v4 and astral-sh/setup-uv@v6. python-tests has fail-fast false and the exact nine include rows from Step 1. Quote every Python value in YAML (`"3.10"`, `"3.11"`, `"3.12"`, `"3.13"`, and `"3.14"`) so YAML cannot coerce `3.10` to `3.1`. Every cell runs these exact workflow commands:

~~~text
uv sync --python "${{ matrix.python }}" --extra dev
uv run --no-sync pytest -q
~~~

GitHub expression interpolation happens before the platform shell, so the quoted command is valid in bash, zsh, and PowerShell and does not use a shell-specific environment expansion. Cache uv by uv.lock. The workflow has read-only contents permission and no credentials.

- [ ] **Step 4: Add package-gate and Rust jobs**

package-gate runs on ubuntu-latest after the complete Python matrix is green. It:

1. syncs Python 3.14;
2. runs uv build;
3. inspects wheel and sdist;
4. creates build/wheel-venv and installs the wheel;
5. invokes verify_installed_tool.py with that venv's Python and memory executable;
6. renders and validates the Cursor bundle;
7. installs Node.js 22 with actions/setup-node@v4;
8. installs exactly @google/gemini-cli@0.50.0;
9. renders the Gemini bundle;
10. runs gemini extensions validate build/validation/gemini.

The validator job must not run gemini extensions install, update, or uninstall and must not receive a Gemini API key.

rust-dashboard runs cargo test --manifest-path dashboard/Cargo.toml on ubuntu-latest. Keep it separate from the Python matrix while making it a required workflow job.

- [ ] **Step 5: Validate YAML and run all locally available deterministic gates**

Run:

~~~bash
uv run --extra dev pytest tests/test_packaging.py tests/test_release_validators.py -q
uv run --extra dev pytest -q
cargo test --manifest-path dashboard/Cargo.toml
uv build
uv run python scripts/verify_wheel_assets.py dist/*.whl dist/*.tar.gz
~~~

Expected: tests, Rust bridge, build, and archive inspection pass. Local Gemini validation may be skipped here if the installed binary is not exactly 0.50.0; CI remains authoritative for that pinned check.

- [ ] **Step 6: Commit**

~~~bash
git add .github/workflows/ci.yml tests/test_packaging.py README.md
git commit -m "ci: gate cross-agent integration releases"
~~~

### Task 5: Support, Migration, Security, and User Documentation

**Files:**
- Modify: .gitignore
- Create: docs/integrations/cursor.md
- Create: docs/integrations/gemini-cli.md
- Create: docs/migrations/cross-agent-v0.6.md
- Create: docs/security-and-privacy.md
- Modify: README.md
- Modify: CHANGELOG.md
- Modify: tests/test_packaging.py

**Interfaces:**
- Documents only commands supported by the adapter CLI
- Separates deterministic guarantees, policy-guided behavior, and degraded states
- Gives safe recovery for legacy/custom/conflicting installations

- [ ] **Step 1: Add failing documentation-contract checks**

~~~python
@pytest.mark.parametrize(
    ('path', 'phrases'),
    [
        (
            'docs/integrations/cursor.md',
            ('memory setup cursor', 'memory doctor --agent cursor', 'policy-guided'),
        ),
        (
            'docs/integrations/gemini-cli.md',
            ('memory setup gemini', 'BeforeAgent', 'N/U/P', 'not verified'),
        ),
        (
            'docs/migrations/cross-agent-v0.6.md',
            ('memory migrate vault-metadata', '--force-managed', 'schema v1'),
        ),
        (
            'docs/security-and-privacy.md',
            ('allow_remote_query_embeddings', 'transcripts', 'redacted copy'),
        ),
    ],
)
def test_support_docs_cover_required_contracts(path: str, phrases: tuple[str, ...]) -> None:
    content = Path(path).read_text()
    for phrase in phrases:
        assert phrase in content


@pytest.mark.parametrize(
    'path',
    [
        'docs/integrations/cursor.md',
        'docs/integrations/gemini-cli.md',
        'docs/migrations/cross-agent-v0.6.md',
        'docs/security-and-privacy.md',
        'docs/dogfood/README.md',
    ],
)
def test_support_docs_are_not_git_ignored(path: str) -> None:
    result = subprocess.run(
        ['git', 'check-ignore', '--no-index', '--quiet', path],
        check=False,
        shell=False,
    )
    assert result.returncode == 1
~~~

- [ ] **Step 2: Remove the broad docs ignore and observe missing guides**

Delete the single broad `docs/` rule from `.gitignore`; keep generated dogfood output under the already ignored `build/` tree. Do not replace it with force-add commands in normal release work. Then run the documentation tests.

Run: `uv run --extra dev pytest tests/test_packaging.py -k support_docs -q`

Expected: the four support documents are missing.

- [ ] **Step 3: Write the Cursor guide**

The guide includes:

- global native plugin setup, project fallback, uninstall, and doctor commands;
- Cursor IDE reload and Cursor CLI MCP discovery with agent mcp list and agent mcp list-tools echovault;
- the exact global/project paths and precedence;
- migration of only the known legacy EchoVault MCP shape;
- conflict and --force-managed recovery without deleting unrelated state;
- deterministic claims limited to installed assets, bound tools, and diagnostics;
- an explicit statement that task-start memory_context and task-end curated memory_save are always-applied policy, but model compliance is observational.

- [ ] **Step 4: Write the Gemini, migration, and security guides**

The Gemini guide documents native extension setup, --project direct fallback, explicit --direct user fallback, N/U/P coexistence, same-scope mutual exclusion, hook deduplication, read-only doctor, normal consent/trust prompts, and fail-open behavior.

The migration guide documents backup, doctor, exact-known Cursor legacy migration, Gemini N/U/P inspection, schema-v1 read compatibility, explicit metadata migration before a v1-targeted write, reconciliation, rollback boundaries, and uninstall preservation.

The security guide documents local canonical storage, no transcript ingestion, no prompt retention, stderr-only sanitized hook logging, local query embeddings by default, explicit remote opt-in with redacted-copy semantics, no API key in native MCP definitions, symlink boundaries, ownership manifests, and trust prompts.

- [ ] **Step 5: Update top-level discovery and changelog**

README links both client guides and gives a compact supported-agent table for Claude Code, Codex, Cursor, Gemini CLI, and OpenCode. It labels Cursor retrieval policy-guided and Gemini BeforeAgent deterministic when the hook succeeds.

CHANGELOG adds an Unreleased section for canonical schema v2, bound MCP/details/idempotency, Cursor/Gemini adapters, migrations, security behavior, and compatibility. Do not claim a published 0.6.0 release or include client-verification results before Task 7.

- [ ] **Step 6: Run docs and full tests**

Run: `uv run --extra dev pytest tests/test_packaging.py -q`

Expected: documentation contracts pass.

Run: `uv run --extra dev pytest -q`

Expected: full suite passes.

- [ ] **Step 7: Commit**

~~~bash
git add .gitignore docs/integrations/cursor.md docs/integrations/gemini-cli.md docs/migrations/cross-agent-v0.6.md docs/security-and-privacy.md README.md CHANGELOG.md tests/test_packaging.py
git commit -m "docs: support cursor and gemini memory"
~~~

### Task 6: Sanitized Authenticated Client Smoke Evidence

**Files:**
- Create: scripts/smoke_clients.py
- Create: tests/test_smoke_clients.py
- Create: docs/dogfood/README.md
- Create: docs/dogfood/report-template.md

**Interfaces:**
- Produces: SmokeStatus = passed | failed | not_verified | observational_miss
- Produces: ReportClaim = not_verified | client_verified
- Produces: SmokeEvidence, ManualEvidence, BoundMarkerEvidence, SmokeReport, and FinalDogfoodReport dataclasses
- Produces: validate_cross_agent_gate(markers: tuple[BoundMarkerEvidence, ...]) -> None
- Produces: strict load_partial_report, load_bound_marker, and load_final_report parsers; every JSON object and nested record rejects unknown or missing fields
- Produces: finalize_report(partial_path, marker_paths, output, claim) and validate_final_report(path, required_claim)
- Produces: append_automated_evidence and append_manual_evidence with duplicate-cell rejection
- Produces executable probe, headless, manual, record-bound-marker, finalize, and validate-final CLI subcommands plus the file entrypoint
- Produces: probe_client(client, workspace, runner, environment) and run_headless_smoke(client, workspace, marker, runner, environment)
- Writes sanitized JSON plus a generated Markdown summary

- [ ] **Step 1: Write failing command, version, and sanitization tests**

In addition to the types imported from `scripts.smoke_clients`, import
`subprocess`, `sys`, and `Path` in `tests/test_smoke_clients.py`; the entrypoint
test below executes only `--help` and never launches a real client.

~~~python
@dataclass
class FakeRunner:
    outcomes: dict[tuple[str, ...], CommandResult] = field(default_factory=dict)
    calls: list[dict[str, object]] = field(default_factory=list)

    def return_for(
        self,
        argv: list[str],
        *,
        returncode: int,
        stdout: str = '',
        stderr: str = '',
    ) -> None:
        self.outcomes[tuple(argv)] = CommandResult(returncode, stdout, stderr)

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        capture_output: bool = True,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        self.calls.append({
            'argv': tuple(argv),
            'cwd': cwd,
            'env': None if env is None else dict(env),
        })
        return self.outcomes[tuple(argv)]


def test_cursor_headless_command_uses_current_agent_entrypoint() -> None:
    command = headless_command(SmokeClient.CURSOR_CLI, 'retrieve the seeded marker')
    assert command == [
        'agent', '-p', '--output-format', 'json',
        'retrieve the seeded marker',
    ]


def test_gemini_headless_command_is_noninteractive_json() -> None:
    command = headless_command(SmokeClient.GEMINI_CLI, 'retrieve the seeded marker')
    assert command == [
        'gemini', '-p', 'retrieve the seeded marker',
        '--output-format', 'json',
    ]


def test_unavailable_or_wrong_version_is_not_verified(tmp_path: Path) -> None:
    fake_runner = FakeRunner()
    fake_runner.return_for(['agent', '--version'], returncode=127, stderr='missing')
    evidence = probe_client(
        SmokeClient.CURSOR_CLI,
        tmp_path.resolve(),
        fake_runner,
        {'PATH': '/controlled'},
    )
    assert evidence.status == SmokeStatus.NOT_VERIFIED
    assert evidence.expected_version == '2026.07.09-a3815c0'
    assert evidence.marker_id is None
    assert evidence.marker_digest is None


def test_report_does_not_retain_prompt_response_or_environment(tmp_path: Path) -> None:
    forbidden_fields = {'prompt', 'response', 'stdout', 'stderr', 'environment'}
    assert forbidden_fields.isdisjoint(
        field.name for field in dataclasses.fields(SmokeEvidence)
    )
    marker_id = '11111111-1111-4111-8111-111111111111'
    private_prompt = 'private prompt secret API_KEY=abc'
    private_response = 'private response secret-value'
    prompt_digest = hashlib.sha256(private_prompt.encode()).hexdigest()
    report = SmokeReport(
        echovault_version='0.6.0',
        commit='abc1234',
        generated_at='2026-07-14T12:00:00Z',
        evidence=(
            SmokeEvidence(
                client=SmokeClient.CURSOR_CLI,
                expected_version='2026.07.09-a3815c0',
                observed_version=None,
                scope=SmokeScope.PROJECT,
                command=SmokeCommand.CURSOR_HEADLESS,
                exit_code=127,
                duration_ms=10,
                marker_id=marker_id,
                marker_digest=hashlib.sha256(marker_id.encode('ascii')).hexdigest(),
                evidence_codes=(EvidenceCode.BINARY_MISSING,),
                status=SmokeStatus.NOT_VERIFIED,
                reason_code=ReasonCode.BINARY_MISSING,
            ),
        ),
    )
    path = write_report(report, tmp_path / 'report.json')
    raw = path.read_text()
    assert private_prompt not in raw
    assert private_response not in raw
    assert prompt_digest not in raw
    assert 'API_KEY' not in raw
    assert 'secret' not in raw


def test_adversarial_client_output_and_its_prompt_digest_are_discarded(tmp_path: Path) -> None:
    prompt = 'do not retain prompt-Q7'
    response = 'model response with SECRET_TOKEN=top-secret'
    fake_runner = FakeRunner()
    fake_runner.return_for(
        ['agent', '--version'],
        returncode=0,
        stdout=f'2026.07.09-a3815c0 {prompt}',
        stderr=response,
    )
    evidence = probe_client(
        SmokeClient.CURSOR_CLI,
        tmp_path.resolve(),
        fake_runner,
        {'PATH': '/controlled', 'API_KEY': 'must-be-filtered-before-run'},
    )
    assert evidence.observed_version is None
    assert evidence.reason_code is ReasonCode.VERSION_MISMATCH
    assert EvidenceCode.VERSION_MISMATCH in evidence.evidence_codes
    report = SmokeReport(
        echovault_version='0.6.0',
        commit='abc1234',
        generated_at='2026-07-14T12:00:00Z',
        evidence=(evidence,),
    )
    raw = write_report(report, tmp_path / 'adversarial.json').read_text()
    for forbidden in (
        prompt,
        response,
        'top-secret',
        hashlib.sha256(prompt.encode()).hexdigest(),
    ):
        assert forbidden not in raw


@pytest.mark.parametrize(
    'forbidden_key',
    [
        'GITHUB_TOKEN',
        'MY_SECRET',
        'DB_CREDENTIAL',
        'PASSWORD',
        'GEMINI_API_KEY',
        'UNRELATED_VALUE',
    ],
)
def test_probe_forwards_only_allowlisted_environment_keys(
    tmp_path: Path, forbidden_key: str,
) -> None:
    runner = FakeRunner()
    runner.return_for(
        ['agent', '--version'],
        returncode=0,
        stdout='2026.07.09-a3815c0\n',
    )
    runner.return_for(['agent', 'mcp', 'list'], returncode=0, stdout='echovault\n')
    runner.return_for(
        ['agent', 'mcp', 'list-tools', 'echovault'],
        returncode=0,
        stdout='memory_context memory_search memory_details memory_save\n',
    )
    probe_client(
        SmokeClient.CURSOR_CLI,
        tmp_path.resolve(),
        runner,
        {
            'PATH': '/controlled',
            'HOME': '/home/test',
            'LANG': 'C.UTF-8',
            'TMP': '/tmp/test',
            forbidden_key: 'private-value',
        },
    )
    assert runner.calls
    for call in runner.calls:
        assert call['cwd'] == tmp_path.resolve()
        assert call['env'] == {
            'PATH': '/controlled',
            'HOME': '/home/test',
            'LANG': 'C.UTF-8',
            'TMP': '/tmp/test',
        }


def test_cursor_ide_and_interactive_cli_evidence_use_distinct_pins_and_commands(
    tmp_path: Path,
) -> None:
    report = SmokeReport(
        echovault_version='0.6.0',
        commit='abc1234',
        generated_at='2026-07-14T12:00:00Z',
        evidence=(),
        manual_evidence=(
            ManualEvidence(
                client=SmokeClient.CURSOR_IDE,
                expected_version='3.11.19',
                observed_version='3.11.19',
                scope=SmokeScope.PROJECT,
                command=SmokeCommand.CURSOR_IDE_MANUAL,
                evidence_codes=(EvidenceCode.MARKER_FOUND,),
                status=SmokeStatus.PASSED,
                reason_code=ReasonCode.VERIFIED,
            ),
            ManualEvidence(
                client=SmokeClient.CURSOR_CLI,
                expected_version='2026.07.09-a3815c0',
                observed_version='2026.07.09-a3815c0',
                scope=SmokeScope.PROJECT,
                command=SmokeCommand.CURSOR_INTERACTIVE,
                evidence_codes=(EvidenceCode.MARKER_FOUND,),
                status=SmokeStatus.PASSED,
                reason_code=ReasonCode.VERIFIED,
            ),
        ),
    )
    payload = json.loads(write_report(report, tmp_path / 'manual.json').read_text())
    assert payload['manual_evidence'][0]['expected_version'] == '3.11.19'
    assert payload['manual_evidence'][1]['expected_version'] == '2026.07.09-a3815c0'


@pytest.mark.parametrize(
    ('field_name', 'invalid_value'),
    [
        ('commit', 'not-a-git-id'),
        ('generated_at', 'not-rfc3339'),
    ],
)
def test_report_rejects_invalid_controlled_metadata(
    field_name: str, invalid_value: str,
) -> None:
    valid = SmokeReport(
        echovault_version='0.6.0',
        commit='abc1234',
        generated_at='2026-07-14T12:00:00Z',
        evidence=(),
    )
    with pytest.raises(EvidenceValidationError):
        replace(valid, **{field_name: invalid_value})


@pytest.mark.parametrize(
    ('field_name', 'invalid_value'),
    [
        ('expected_version', 'private-version-text'),
        ('observed_version', '0.50.0 secret'),
        ('marker_id', 'not-a-uuid'),
        ('marker_digest', 'not-a-sha256'),
    ],
)
def test_evidence_rejects_invalid_version_or_marker_metadata(
    field_name: str, invalid_value: str,
) -> None:
    valid = SmokeEvidence(
        client=SmokeClient.GEMINI_CLI,
        expected_version='0.50.0',
        observed_version='0.50.0',
        scope=SmokeScope.PROJECT,
        command=SmokeCommand.GEMINI_HEADLESS,
        exit_code=0,
        duration_ms=10,
        marker_id='11111111-1111-4111-8111-111111111111',
        marker_digest=hashlib.sha256(
            b'11111111-1111-4111-8111-111111111111'
        ).hexdigest(),
        evidence_codes=(EvidenceCode.MARKER_FOUND,),
        status=SmokeStatus.PASSED,
        reason_code=ReasonCode.VERIFIED,
    )
    with pytest.raises(EvidenceValidationError):
        replace(valid, **{field_name: invalid_value})


def cross_agent_markers() -> tuple[BoundMarkerEvidence, BoundMarkerEvidence]:
    return (
        BoundMarkerEvidence(
            binding_agent=BoundAgent.CURSOR,
            marker_id='11111111-1111-4111-8111-111111111111',
            operation_id='22222222-2222-4222-8222-222222222222',
            memory_id='33333333-3333-4333-8333-333333333333',
            replay_verified=True,
        ),
        BoundMarkerEvidence(
            binding_agent=BoundAgent.GEMINI_CLI,
            marker_id='44444444-4444-4444-8444-444444444444',
            operation_id='55555555-5555-4555-8555-555555555555',
            memory_id='66666666-6666-4666-8666-666666666666',
            replay_verified=True,
        ),
    )


def final_report(
    markers: tuple[BoundMarkerEvidence, ...],
    *,
    claim: ReportClaim = ReportClaim.NOT_VERIFIED,
    evidence: tuple[SmokeEvidence, ...] = (),
    manual_evidence: tuple[ManualEvidence, ...] = (),
) -> FinalDogfoodReport:
    return FinalDogfoodReport(
        echovault_version='0.6.0',
        commit='abc1234',
        generated_at='2026-07-14T12:00:00Z',
        evidence=evidence,
        manual_evidence=manual_evidence,
        report_claim=claim,
        bound_markers=markers,
    )


def verified_partial_report() -> SmokeReport:
    def automated(
        client: SmokeClient,
        command: SmokeCommand,
        version: str,
        marker_id: str | None = None,
    ) -> SmokeEvidence:
        return SmokeEvidence(
            client=client,
            expected_version=version,
            observed_version=version,
            scope=SmokeScope.PROJECT,
            command=command,
            exit_code=0,
            duration_ms=10,
            marker_id=marker_id,
            marker_digest=(
                None if marker_id is None
                else hashlib.sha256(marker_id.encode('ascii')).hexdigest()
            ),
            evidence_codes=(EvidenceCode.MARKER_FOUND,),
            status=SmokeStatus.PASSED,
            reason_code=ReasonCode.VERIFIED,
        )

    def manual(
        client: SmokeClient,
        command: SmokeCommand,
        version: str,
    ) -> ManualEvidence:
        return ManualEvidence(
            client=client,
            expected_version=version,
            observed_version=version,
            scope=SmokeScope.PROJECT,
            command=command,
            evidence_codes=(EvidenceCode.MARKER_FOUND,),
            status=SmokeStatus.PASSED,
            reason_code=ReasonCode.VERIFIED,
        )

    return SmokeReport(
        echovault_version='0.6.0',
        commit='abc1234',
        generated_at='2026-07-14T12:00:00Z',
        evidence=(
            automated(
                SmokeClient.CURSOR_CLI,
                SmokeCommand.CURSOR_PROBE,
                '2026.07.09-a3815c0',
            ),
            automated(
                SmokeClient.CURSOR_CLI,
                SmokeCommand.CURSOR_HEADLESS,
                '2026.07.09-a3815c0',
                '77777777-7777-4777-8777-777777777777',
            ),
            automated(
                SmokeClient.GEMINI_CLI,
                SmokeCommand.GEMINI_PROBE,
                '0.50.0',
            ),
            automated(
                SmokeClient.GEMINI_CLI,
                SmokeCommand.GEMINI_HEADLESS,
                '0.50.0',
                '88888888-8888-4888-8888-888888888888',
            ),
        ),
        manual_evidence=(
            manual(
                SmokeClient.CURSOR_IDE,
                SmokeCommand.CURSOR_IDE_MANUAL,
                '3.11.19',
            ),
            manual(
                SmokeClient.CURSOR_CLI,
                SmokeCommand.CURSOR_INTERACTIVE,
                '2026.07.09-a3815c0',
            ),
        ),
        report_claim=ReportClaim.NOT_VERIFIED,
    )


def test_partial_probe_report_allows_no_bound_markers() -> None:
    partial = SmokeReport(
        echovault_version='0.6.0',
        commit='abc1234',
        generated_at='2026-07-14T12:00:00Z',
        evidence=(),
    )
    assert partial.evidence == ()
    assert partial.report_claim is ReportClaim.NOT_VERIFIED


def test_final_report_requires_exact_cursor_and_gemini_bound_markers(
    tmp_path: Path,
) -> None:
    report = final_report(cross_agent_markers())
    payload = json.loads(write_report(report, tmp_path / 'final.json').read_text())
    assert [item['binding_agent'] for item in payload['bound_markers']] == [
        'cursor', 'gemini-cli',
    ]
    assert all(item['replay_verified'] for item in payload['bound_markers'])


def test_final_report_rejects_missing_duplicate_or_unreplayed_binding() -> None:
    cursor, gemini = cross_agent_markers()
    with pytest.raises(EvidenceValidationError):
        final_report((cursor,))
    with pytest.raises(EvidenceValidationError):
        final_report((cursor, replace(gemini, binding_agent=BoundAgent.CURSOR)))
    with pytest.raises(EvidenceValidationError):
        final_report((cursor, replace(gemini, replay_verified=False)))


@pytest.mark.parametrize('field_name', ['marker_id', 'operation_id', 'memory_id'])
def test_final_report_requires_distinct_cross_agent_ids(field_name: str) -> None:
    cursor, gemini = cross_agent_markers()
    duplicate = replace(gemini, **{field_name: getattr(cursor, field_name)})
    with pytest.raises(EvidenceValidationError):
        final_report((cursor, duplicate))


def test_finalize_and_validate_final_cli_round_trip(tmp_path: Path) -> None:
    partial_path = write_report(
        verified_partial_report(), tmp_path / 'partial-smoke.json'
    )
    marker_paths = []
    for marker in cross_agent_markers():
        marker_path = tmp_path / f'{marker.binding_agent.value}-bound-marker.json'
        write_bound_marker(marker, marker_path)
        marker_paths.append(marker_path)

    runner = CliRunner()
    result = runner.invoke(smoke_cli, [
        'finalize',
        '--partial', str(partial_path),
        '--bound-marker', str(marker_paths[0]),
        '--bound-marker', str(marker_paths[1]),
        '--claim', 'client_verified',
        '--output', str(tmp_path / 'client-verified-final.json'),
    ])
    assert result.exit_code == 0, result.output

    validation = runner.invoke(smoke_cli, [
        'validate-final',
        '--report', str(tmp_path / 'client-verified-final.json'),
        '--require-claim', 'client_verified',
    ])
    assert validation.exit_code == 0, validation.output
    assert validation.output.strip() == 'valid client_verified final report'


def test_smoke_cli_exposes_every_shell_gate_subcommand() -> None:
    assert set(smoke_cli.commands) == {
        'probe',
        'headless',
        'manual',
        'record-bound-marker',
        'finalize',
        'validate-final',
    }


def test_smoke_script_file_entrypoint_is_executable() -> None:
    script = Path(__file__).parents[1] / 'scripts' / 'smoke_clients.py'
    result = subprocess.run(
        [sys.executable, str(script), '--help'],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    for command in ('probe', 'headless', 'manual', 'finalize', 'validate-final'):
        assert command in result.stdout


@pytest.mark.parametrize('marker_count', [0, 1, 3])
def test_finalize_requires_exactly_two_marker_files(
    tmp_path: Path, marker_count: int,
) -> None:
    partial_path = write_report(
        verified_partial_report(), tmp_path / 'partial-smoke.json'
    )
    marker_paths = []
    cursor, gemini = cross_agent_markers()
    for index, marker in enumerate((cursor, gemini, cursor)[:marker_count]):
        path = tmp_path / f'marker-{index}.json'
        write_bound_marker(marker, path)
        marker_paths.append(path)
    with pytest.raises(EvidenceValidationError, match='exactly two'):
        finalize_report(
            partial_path,
            tuple(marker_paths),
            tmp_path / 'final.json',
            ReportClaim.CLIENT_VERIFIED,
        )


def test_client_verified_claim_rejects_not_verified_client_evidence(
    tmp_path: Path,
) -> None:
    partial = replace(
        verified_partial_report(),
        evidence=(
            replace(
                verified_partial_report().evidence[0],
                observed_version=None,
                status=SmokeStatus.NOT_VERIFIED,
                reason_code=ReasonCode.AUTH_MISSING,
            ),
            *verified_partial_report().evidence[1:],
        ),
    )
    partial_path = write_report(partial, tmp_path / 'partial-smoke.json')
    marker_paths = []
    for marker in cross_agent_markers():
        path = tmp_path / f'{marker.binding_agent.value}.json'
        write_bound_marker(marker, path)
        marker_paths.append(path)
    with pytest.raises(EvidenceValidationError, match='client_verified'):
        finalize_report(
            partial_path,
            tuple(marker_paths),
            tmp_path / 'final.json',
            ReportClaim.CLIENT_VERIFIED,
        )


def test_client_verified_claim_rejects_inconsistent_status_reason() -> None:
    partial = verified_partial_report()
    inconsistent = replace(
        partial.manual_evidence[0],
        reason_code=ReasonCode.AUTH_MISSING,
        evidence_codes=(EvidenceCode.AUTH_MISSING,),
    )
    with pytest.raises(EvidenceValidationError, match='client_verified'):
        final_report(
            cross_agent_markers(),
            claim=ReportClaim.CLIENT_VERIFIED,
            evidence=partial.evidence,
            manual_evidence=(inconsistent, *partial.manual_evidence[1:]),
        )


@pytest.mark.parametrize(
    ('target', 'required_field'),
    [
        ('partial', 'manual_evidence'),
        ('evidence', 'status'),
        ('manual', 'status'),
        ('marker', 'memory_id'),
        ('final', 'bound_markers'),
    ],
)
@pytest.mark.parametrize('mutation', ['missing', 'unknown'])
def test_json_loaders_fail_closed_on_missing_or_unknown_fields(
    tmp_path: Path,
    target: str,
    required_field: str,
    mutation: str,
) -> None:
    partial_path = write_report(
        verified_partial_report(), tmp_path / 'partial-smoke.json'
    )
    marker_paths = []
    for marker in cross_agent_markers():
        path = tmp_path / f'{marker.binding_agent.value}.json'
        write_bound_marker(marker, path)
        marker_paths.append(path)
    final_path = finalize_report(
        partial_path,
        tuple(marker_paths),
        tmp_path / 'client-verified-final.json',
        ReportClaim.CLIENT_VERIFIED,
    )

    if target == 'marker':
        path = marker_paths[0]
        payload = json.loads(path.read_text())
        container = payload
        loader = load_bound_marker
    elif target == 'final':
        path = final_path
        payload = json.loads(path.read_text())
        container = payload
        loader = load_final_report
    else:
        path = partial_path
        payload = json.loads(path.read_text())
        container = {
            'partial': payload,
            'evidence': payload['evidence'][0],
            'manual': payload['manual_evidence'][0],
        }[target]
        loader = load_partial_report

    if mutation == 'missing':
        container.pop(required_field)
    else:
        container['SECRET_PROMPT_VALUE'] = 'must never be echoed'
    path.write_text(json.dumps(payload), encoding='utf-8')

    with pytest.raises(EvidenceValidationError) as exc_info:
        loader(path)
    assert 'must never be echoed' not in str(exc_info.value)
    assert 'SECRET_PROMPT_VALUE' not in str(exc_info.value)
~~~

- [ ] **Step 2: Run smoke tests and observe missing evidence module**

Run: `uv run --extra dev pytest tests/test_smoke_clients.py -q`

Expected: collection fails because scripts.smoke_clients does not exist.

- [ ] **Step 3: Implement safe probe and headless modes**

Use these exact serializable types; none has a field capable of retaining raw client output, a prompt, a response, or an environment mapping:

~~~python
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
import dataclasses
import hashlib
import hmac
import importlib.metadata
import json
import os
import re
import subprocess
import time
import uuid

import click

from memory.integrations.process import (
    CommandResult,
    CommandRunner,
    SubprocessRunner,
)
from memory.safe_io import prepare_atomic_text


class SmokeStatus(str, Enum):
    PASSED = 'passed'
    FAILED = 'failed'
    NOT_VERIFIED = 'not_verified'
    OBSERVATIONAL_MISS = 'observational_miss'


class ReportClaim(str, Enum):
    NOT_VERIFIED = 'not_verified'
    CLIENT_VERIFIED = 'client_verified'


class SmokeClient(str, Enum):
    CURSOR_IDE = 'cursor-ide'
    CURSOR_CLI = 'cursor-cli'
    GEMINI_CLI = 'gemini-cli'


class BoundAgent(str, Enum):
    CURSOR = 'cursor'
    GEMINI_CLI = 'gemini-cli'


class SmokeScope(str, Enum):
    USER = 'user'
    PROJECT = 'project'


class SmokeCommand(str, Enum):
    CURSOR_IDE_MANUAL = 'cursor-ide-manual'
    CURSOR_INTERACTIVE = 'cursor-interactive'
    CURSOR_PROBE = 'cursor-probe'
    CURSOR_HEADLESS = 'cursor-headless'
    GEMINI_PROBE = 'gemini-probe'
    GEMINI_HEADLESS = 'gemini-headless'


class EvidenceCode(str, Enum):
    BINARY_MISSING = 'binary_missing'
    VERSION_MISMATCH = 'version_mismatch'
    AUTH_MISSING = 'auth_missing'
    MCP_DISCOVERED = 'mcp_discovered'
    FOUR_TOOLS = 'four_tools'
    EXTENSION_DISCOVERED = 'extension_discovered'
    MARKER_FOUND = 'marker_found'
    MARKER_MISSED = 'marker_missed'
    TIMEOUT = 'timeout'
    CLIENT_ERROR = 'client_error'


class ReasonCode(str, Enum):
    VERIFIED = 'verified'
    BINARY_MISSING = 'binary_missing'
    VERSION_MISMATCH = 'version_mismatch'
    AUTH_MISSING = 'auth_missing'
    MARKER_MISSED = 'marker_missed'
    TIMEOUT = 'timeout'
    CLIENT_ERROR = 'client_error'


class EvidenceValidationError(ValueError):
    pass


@dataclass(frozen=True)
class BoundMarkerEvidence:
    binding_agent: BoundAgent
    marker_id: str
    operation_id: str
    memory_id: str
    replay_verified: bool

    def __post_init__(self) -> None:
        if not isinstance(self.binding_agent, BoundAgent):
            raise EvidenceValidationError('invalid bound agent')
        for field_name in ('marker_id', 'operation_id', 'memory_id'):
            value = getattr(self, field_name)
            try:
                uuid.UUID(value)
            except (AttributeError, TypeError, ValueError) as exc:
                raise EvidenceValidationError(f'invalid {field_name}') from exc
        if self.replay_verified is not True:
            raise EvidenceValidationError('bound marker replay is not verified')


ALLOWED_SMOKE_ENV_KEYS = frozenset({
    'PATH', 'HOME', 'USERPROFILE', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME',
    'LANG', 'LC_ALL', 'TMP', 'TEMP', 'TMPDIR',
    'SystemRoot', 'WINDIR', 'PATHEXT', 'COMSPEC',
})

VERSION_PATTERNS = {
    SmokeClient.CURSOR_IDE: re.compile(r'^\d+\.\d+\.\d+$'),
    SmokeClient.CURSOR_CLI: re.compile(r'^\d{4}\.\d{2}\.\d{2}-[0-9a-f]{7}$'),
    SmokeClient.GEMINI_CLI: re.compile(r'^\d+\.\d+\.\d+$'),
}


def validate_version(client: SmokeClient, value: str | None) -> None:
    if value is not None and VERSION_PATTERNS[client].fullmatch(value) is None:
        raise EvidenceValidationError('invalid version')


@dataclass(frozen=True)
class SmokeEvidence:
    client: SmokeClient
    expected_version: str
    observed_version: str | None
    scope: SmokeScope
    command: SmokeCommand
    exit_code: int | None
    duration_ms: int
    marker_id: str | None
    marker_digest: str | None
    evidence_codes: tuple[EvidenceCode, ...]
    status: SmokeStatus
    reason_code: ReasonCode

    def __post_init__(self) -> None:
        allowed = {
            (SmokeClient.CURSOR_CLI, SmokeCommand.CURSOR_PROBE),
            (SmokeClient.CURSOR_CLI, SmokeCommand.CURSOR_HEADLESS),
            (SmokeClient.GEMINI_CLI, SmokeCommand.GEMINI_PROBE),
            (SmokeClient.GEMINI_CLI, SmokeCommand.GEMINI_HEADLESS),
        }
        if (self.client, self.command) not in allowed:
            raise EvidenceValidationError('invalid automated client/command')
        validate_version(self.client, self.expected_version)
        validate_version(self.client, self.observed_version)
        marker_values = (self.marker_id, self.marker_digest)
        if (marker_values[0] is None) != (marker_values[1] is None):
            raise EvidenceValidationError('marker id and digest must be paired')
        if self.command in {SmokeCommand.CURSOR_PROBE, SmokeCommand.GEMINI_PROBE}:
            if marker_values != (None, None):
                raise EvidenceValidationError('probe evidence has no marker')
        elif None in marker_values:
            raise EvidenceValidationError('headless evidence requires marker')
        if self.marker_id is not None:
            try:
                uuid.UUID(self.marker_id)
            except ValueError as exc:
                raise EvidenceValidationError('invalid marker UUID') from exc
            expected = hashlib.sha256(self.marker_id.encode('ascii')).hexdigest()
            if not hmac.compare_digest(self.marker_digest or '', expected):
                raise EvidenceValidationError('invalid marker digest')


@dataclass(frozen=True)
class ManualEvidence:
    client: SmokeClient
    expected_version: str
    observed_version: str | None
    scope: SmokeScope
    command: SmokeCommand
    evidence_codes: tuple[EvidenceCode, ...]
    status: SmokeStatus
    reason_code: ReasonCode

    def __post_init__(self) -> None:
        allowed = {
            (SmokeClient.CURSOR_IDE, SmokeCommand.CURSOR_IDE_MANUAL),
            (SmokeClient.CURSOR_CLI, SmokeCommand.CURSOR_INTERACTIVE),
        }
        if (self.client, self.command) not in allowed:
            raise EvidenceValidationError('invalid manual client/command')
        validate_version(self.client, self.expected_version)
        validate_version(self.client, self.observed_version)


@dataclass(frozen=True)
class SmokeReport:
    echovault_version: str
    commit: str
    generated_at: str
    evidence: tuple[SmokeEvidence, ...]
    manual_evidence: tuple[ManualEvidence, ...] = ()
    report_claim: ReportClaim = ReportClaim.NOT_VERIFIED

    def __post_init__(self) -> None:
        if not isinstance(self.report_claim, ReportClaim):
            raise EvidenceValidationError('invalid report claim')
        if type(self) is SmokeReport and self.report_claim is not ReportClaim.NOT_VERIFIED:
            raise EvidenceValidationError('partial report claim must be not_verified')
        if re.fullmatch(r'[0-9a-f]{7,40}', self.commit) is None:
            raise EvidenceValidationError('invalid commit')
        try:
            datetime.strptime(self.generated_at, '%Y-%m-%dT%H:%M:%SZ')
        except ValueError as exc:
            raise EvidenceValidationError('invalid generated_at') from exc
        if not self.generated_at.endswith('Z'):
            raise EvidenceValidationError('invalid generated_at')
        if re.fullmatch(r'\d+\.\d+\.\d+', self.echovault_version) is None:
            raise EvidenceValidationError('invalid EchoVault version')


def validate_cross_agent_gate(
    markers: tuple[BoundMarkerEvidence, ...],
) -> None:
    required_agents = {BoundAgent.CURSOR, BoundAgent.GEMINI_CLI}
    if len(markers) != 2 or {item.binding_agent for item in markers} != required_agents:
        raise EvidenceValidationError(
            'final dogfood requires exactly one Cursor-bound and one Gemini-bound marker'
        )
    if any(item.replay_verified is not True for item in markers):
        raise EvidenceValidationError('every bound marker must verify replay')
    for field_name in ('marker_id', 'operation_id', 'memory_id'):
        if len({getattr(item, field_name) for item in markers}) != 2:
            raise EvidenceValidationError(
                f'bound markers require distinct {field_name} values'
            )


EXPECTED_CLIENT_VERSIONS = {
    SmokeClient.CURSOR_IDE: '3.11.19',
    SmokeClient.CURSOR_CLI: '2026.07.09-a3815c0',
    SmokeClient.GEMINI_CLI: '0.50.0',
}
REQUIRED_AUTOMATED_CELLS = {
    (SmokeClient.CURSOR_CLI, SmokeCommand.CURSOR_PROBE),
    (SmokeClient.CURSOR_CLI, SmokeCommand.CURSOR_HEADLESS),
    (SmokeClient.GEMINI_CLI, SmokeCommand.GEMINI_PROBE),
    (SmokeClient.GEMINI_CLI, SmokeCommand.GEMINI_HEADLESS),
}
REQUIRED_MANUAL_CELLS = {
    (SmokeClient.CURSOR_IDE, SmokeCommand.CURSOR_IDE_MANUAL),
    (SmokeClient.CURSOR_CLI, SmokeCommand.CURSOR_INTERACTIVE),
}


def validate_client_verified_gate(report: SmokeReport) -> None:
    automated = {(item.client, item.command): item for item in report.evidence}
    manual = {(item.client, item.command): item for item in report.manual_evidence}
    if len(automated) != len(report.evidence) or set(automated) != REQUIRED_AUTOMATED_CELLS:
        raise EvidenceValidationError('client_verified requires each automated cell once')
    if len(manual) != len(report.manual_evidence) or set(manual) != REQUIRED_MANUAL_CELLS:
        raise EvidenceValidationError('client_verified requires each manual cell once')
    for item in report.evidence:
        expected = EXPECTED_CLIENT_VERSIONS[item.client]
        if (
            item.expected_version != expected
            or item.observed_version != expected
            or item.status is not SmokeStatus.PASSED
            or item.reason_code is not ReasonCode.VERIFIED
            or (
                item.command in {
                    SmokeCommand.CURSOR_HEADLESS,
                    SmokeCommand.GEMINI_HEADLESS,
                }
                and EvidenceCode.MARKER_FOUND not in item.evidence_codes
            )
        ):
            raise EvidenceValidationError(
                'client_verified requires exact pinned automated success'
            )
    for item in report.manual_evidence:
        expected = EXPECTED_CLIENT_VERSIONS[item.client]
        if (
            item.expected_version != expected
            or item.observed_version != expected
            or item.status not in {
                SmokeStatus.PASSED,
                SmokeStatus.OBSERVATIONAL_MISS,
            }
            or (
                item.status is SmokeStatus.PASSED
                and (
                    item.reason_code is not ReasonCode.VERIFIED
                    or EvidenceCode.MARKER_FOUND not in item.evidence_codes
                )
            )
            or (
                item.status is SmokeStatus.OBSERVATIONAL_MISS
                and (
                    item.reason_code is not ReasonCode.MARKER_MISSED
                    or EvidenceCode.MARKER_MISSED not in item.evidence_codes
                )
            )
        ):
            raise EvidenceValidationError(
                'client_verified requires an exact pinned manual observation'
            )


@dataclass(frozen=True)
class FinalDogfoodReport(SmokeReport):
    bound_markers: tuple[BoundMarkerEvidence, ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        validate_cross_agent_gate(self.bound_markers)
        if self.report_claim is ReportClaim.CLIENT_VERIFIED:
            validate_client_verified_gate(self)


PARTIAL_REPORT_FIELDS = frozenset({
    'echovault_version', 'commit', 'generated_at', 'evidence',
    'manual_evidence', 'report_claim',
})
FINAL_REPORT_FIELDS = PARTIAL_REPORT_FIELDS | {'bound_markers'}
SMOKE_EVIDENCE_FIELDS = frozenset({
    'client', 'expected_version', 'observed_version', 'scope', 'command',
    'exit_code', 'duration_ms', 'marker_id', 'marker_digest',
    'evidence_codes', 'status', 'reason_code',
})
MANUAL_EVIDENCE_FIELDS = frozenset({
    'client', 'expected_version', 'observed_version', 'scope', 'command',
    'evidence_codes', 'status', 'reason_code',
})
BOUND_MARKER_FIELDS = frozenset({
    'binding_agent', 'marker_id', 'operation_id', 'memory_id',
    'replay_verified',
})


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise EvidenceValidationError('invalid JSON object')
        result[key] = value
    return result


def _load_json(path: Path) -> object:
    try:
        return json.loads(
            path.read_text(encoding='utf-8'),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except EvidenceValidationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceValidationError('invalid JSON document') from exc


def _exact_object(
    value: object,
    fields: frozenset[str] | set[str],
    label: str,
) -> dict[str, object]:
    if type(value) is not dict or set(value) != set(fields):
        raise EvidenceValidationError(f'invalid {label} schema')
    return value


def _string(value: object, label: str) -> str:
    if type(value) is not str:
        raise EvidenceValidationError(f'invalid {label}')
    return value


def _optional_string(value: object, label: str) -> str | None:
    if value is not None and type(value) is not str:
        raise EvidenceValidationError(f'invalid {label}')
    return value


def _enum(enum_type, value: object, label: str):
    if type(value) is not str:
        raise EvidenceValidationError(f'invalid {label}')
    try:
        return enum_type(value)
    except ValueError as exc:
        raise EvidenceValidationError(f'invalid {label}') from exc


def _evidence_codes(value: object) -> tuple[EvidenceCode, ...]:
    if type(value) is not list:
        raise EvidenceValidationError('invalid evidence codes')
    return tuple(_enum(EvidenceCode, item, 'evidence code') for item in value)


def _smoke_evidence(value: object) -> SmokeEvidence:
    item = _exact_object(value, SMOKE_EVIDENCE_FIELDS, 'smoke evidence')
    exit_code = item['exit_code']
    if exit_code is not None and type(exit_code) is not int:
        raise EvidenceValidationError('invalid exit code')
    duration_ms = item['duration_ms']
    if type(duration_ms) is not int or duration_ms < 0:
        raise EvidenceValidationError('invalid duration')
    return SmokeEvidence(
        client=_enum(SmokeClient, item['client'], 'client'),
        expected_version=_string(item['expected_version'], 'expected version'),
        observed_version=_optional_string(
            item['observed_version'], 'observed version'
        ),
        scope=_enum(SmokeScope, item['scope'], 'scope'),
        command=_enum(SmokeCommand, item['command'], 'command'),
        exit_code=exit_code,
        duration_ms=duration_ms,
        marker_id=_optional_string(item['marker_id'], 'marker id'),
        marker_digest=_optional_string(item['marker_digest'], 'marker digest'),
        evidence_codes=_evidence_codes(item['evidence_codes']),
        status=_enum(SmokeStatus, item['status'], 'status'),
        reason_code=_enum(ReasonCode, item['reason_code'], 'reason code'),
    )


def _manual_evidence(value: object) -> ManualEvidence:
    item = _exact_object(value, MANUAL_EVIDENCE_FIELDS, 'manual evidence')
    return ManualEvidence(
        client=_enum(SmokeClient, item['client'], 'client'),
        expected_version=_string(item['expected_version'], 'expected version'),
        observed_version=_optional_string(
            item['observed_version'], 'observed version'
        ),
        scope=_enum(SmokeScope, item['scope'], 'scope'),
        command=_enum(SmokeCommand, item['command'], 'command'),
        evidence_codes=_evidence_codes(item['evidence_codes']),
        status=_enum(SmokeStatus, item['status'], 'status'),
        reason_code=_enum(ReasonCode, item['reason_code'], 'reason code'),
    )


def _bound_marker(value: object) -> BoundMarkerEvidence:
    item = _exact_object(value, BOUND_MARKER_FIELDS, 'bound marker')
    if type(item['replay_verified']) is not bool:
        raise EvidenceValidationError('invalid replay verification')
    return BoundMarkerEvidence(
        binding_agent=_enum(BoundAgent, item['binding_agent'], 'bound agent'),
        marker_id=_string(item['marker_id'], 'marker id'),
        operation_id=_string(item['operation_id'], 'operation id'),
        memory_id=_string(item['memory_id'], 'memory id'),
        replay_verified=item['replay_verified'],
    )


def _report_values(
    value: object,
    fields: frozenset[str] | set[str],
) -> tuple[dict[str, object], dict[str, object]]:
    item = _exact_object(value, fields, 'report')
    evidence = item['evidence']
    manual_evidence = item['manual_evidence']
    if type(evidence) is not list or type(manual_evidence) is not list:
        raise EvidenceValidationError('invalid report evidence')
    values = {
        'echovault_version': _string(
            item['echovault_version'], 'EchoVault version'
        ),
        'commit': _string(item['commit'], 'commit'),
        'generated_at': _string(item['generated_at'], 'generated timestamp'),
        'evidence': tuple(_smoke_evidence(record) for record in evidence),
        'manual_evidence': tuple(
            _manual_evidence(record) for record in manual_evidence
        ),
        'report_claim': _enum(ReportClaim, item['report_claim'], 'report claim'),
    }
    return item, values


def load_partial_report(path: Path) -> SmokeReport:
    _, values = _report_values(_load_json(path), PARTIAL_REPORT_FIELDS)
    if values['report_claim'] is not ReportClaim.NOT_VERIFIED:
        raise EvidenceValidationError('partial report claim must be not_verified')
    return SmokeReport(**values)


def load_bound_marker(path: Path) -> BoundMarkerEvidence:
    return _bound_marker(_load_json(path))


def load_final_report(path: Path) -> FinalDogfoodReport:
    item, values = _report_values(_load_json(path), FINAL_REPORT_FIELDS)
    markers = item['bound_markers']
    if type(markers) is not list:
        raise EvidenceValidationError('invalid bound marker list')
    return FinalDogfoodReport(
        **values,
        bound_markers=tuple(_bound_marker(marker) for marker in markers),
    )


def write_bound_marker(marker: BoundMarkerEvidence, output: Path) -> Path:
    payload = json.dumps(dataclasses.asdict(marker), indent=2, sort_keys=True) + '\n'
    prepared = prepare_atomic_text(output, payload)
    prepared.replace()
    return output


def finalize_report(
    partial_path: Path,
    marker_paths: tuple[Path, ...],
    output: Path,
    claim: ReportClaim,
) -> Path:
    if len(marker_paths) != 2:
        raise EvidenceValidationError('finalize requires exactly two marker files')
    partial = load_partial_report(partial_path)
    markers = tuple(load_bound_marker(path) for path in marker_paths)
    report = FinalDogfoodReport(
        echovault_version=partial.echovault_version,
        commit=partial.commit,
        generated_at=partial.generated_at,
        evidence=partial.evidence,
        manual_evidence=partial.manual_evidence,
        report_claim=claim,
        bound_markers=markers,
    )
    return write_report(report, output)


def validate_final_report(
    path: Path,
    required_claim: ReportClaim | None = None,
) -> FinalDogfoodReport:
    report = load_final_report(path)
    if required_claim is not None and report.report_claim is not required_claim:
        raise EvidenceValidationError('final report has the wrong claim')
    return report


def build_smoke_environment(environment: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in environment.items()
        if key in ALLOWED_SMOKE_ENV_KEYS
    }


def headless_command(client: SmokeClient, prompt: str) -> list[str]:
    if client is SmokeClient.CURSOR_CLI:
        return ['agent', '-p', '--output-format', 'json', prompt]
    if client is SmokeClient.GEMINI_CLI:
        return ['gemini', '-p', prompt, '--output-format', 'json']
    raise ValueError(f'Unsupported headless smoke client: {client.value}')


def _workspace(path: Path) -> Path:
    if path.is_symlink():
        raise EvidenceValidationError('workspace must not be a symlink')
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise EvidenceValidationError('workspace is unavailable') from exc
    if not resolved.is_dir():
        raise EvidenceValidationError('workspace must be a directory')
    return resolved


def _probe_result(
    *,
    client: SmokeClient,
    observed_version: str | None,
    returncode: int | None,
    duration_ms: int,
    codes: tuple[EvidenceCode, ...],
    status: SmokeStatus,
    reason: ReasonCode,
) -> SmokeEvidence:
    command = (
        SmokeCommand.CURSOR_PROBE
        if client is SmokeClient.CURSOR_CLI
        else SmokeCommand.GEMINI_PROBE
    )
    return SmokeEvidence(
        client=client,
        expected_version=EXPECTED_CLIENT_VERSIONS[client],
        observed_version=observed_version,
        scope=SmokeScope.PROJECT,
        command=command,
        exit_code=returncode,
        duration_ms=duration_ms,
        marker_id=None,
        marker_digest=None,
        evidence_codes=codes,
        status=status,
        reason_code=reason,
    )


def probe_client(
    client: SmokeClient,
    workspace: Path,
    runner: CommandRunner,
    environment: Mapping[str, str],
) -> SmokeEvidence:
    if client not in {SmokeClient.CURSOR_CLI, SmokeClient.GEMINI_CLI}:
        raise EvidenceValidationError('unsupported automated client')
    cwd = _workspace(workspace)
    safe_environment = build_smoke_environment(environment)
    version_command = (
        ['agent', '--version']
        if client is SmokeClient.CURSOR_CLI
        else ['gemini', '--version']
    )
    started = time.monotonic()
    try:
        version = runner.run(
            version_command,
            timeout=10.0,
            cwd=cwd,
            env=safe_environment,
        )
    except FileNotFoundError:
        duration = max(0, round((time.monotonic() - started) * 1000))
        return _probe_result(
            client=client,
            observed_version=None,
            returncode=127,
            duration_ms=duration,
            codes=(EvidenceCode.BINARY_MISSING,),
            status=SmokeStatus.NOT_VERIFIED,
            reason=ReasonCode.BINARY_MISSING,
        )
    except (TimeoutError, subprocess.TimeoutExpired):
        duration = max(0, round((time.monotonic() - started) * 1000))
        return _probe_result(
            client=client,
            observed_version=None,
            returncode=None,
            duration_ms=duration,
            codes=(EvidenceCode.TIMEOUT,),
            status=SmokeStatus.FAILED,
            reason=ReasonCode.TIMEOUT,
        )
    duration = max(0, round((time.monotonic() - started) * 1000))
    if version.returncode == 127:
        return _probe_result(
            client=client,
            observed_version=None,
            returncode=version.returncode,
            duration_ms=duration,
            codes=(EvidenceCode.BINARY_MISSING,),
            status=SmokeStatus.NOT_VERIFIED,
            reason=ReasonCode.BINARY_MISSING,
        )
    if version.returncode != 0:
        return _probe_result(
            client=client,
            observed_version=None,
            returncode=version.returncode,
            duration_ms=duration,
            codes=(EvidenceCode.AUTH_MISSING,),
            status=SmokeStatus.NOT_VERIFIED,
            reason=ReasonCode.AUTH_MISSING,
        )
    expected = EXPECTED_CLIENT_VERSIONS[client]
    observed = version.stdout.strip() if version.stdout.strip() == expected else None
    if observed is None:
        return _probe_result(
            client=client,
            observed_version=None,
            returncode=version.returncode,
            duration_ms=duration,
            codes=(EvidenceCode.VERSION_MISMATCH,),
            status=SmokeStatus.NOT_VERIFIED,
            reason=ReasonCode.VERSION_MISMATCH,
        )

    if client is SmokeClient.CURSOR_CLI:
        discovery_commands = (
            ['agent', 'mcp', 'list'],
            ['agent', 'mcp', 'list-tools', 'echovault'],
        )
    else:
        discovery_commands = (['gemini', 'extensions', 'list'],)
    try:
        discovery = [
            runner.run(command, timeout=15.0, cwd=cwd, env=safe_environment)
            for command in discovery_commands
        ]
    except (TimeoutError, subprocess.TimeoutExpired):
        duration = max(0, round((time.monotonic() - started) * 1000))
        return _probe_result(
            client=client,
            observed_version=observed,
            returncode=None,
            duration_ms=duration,
            codes=(EvidenceCode.TIMEOUT,),
            status=SmokeStatus.FAILED,
            reason=ReasonCode.TIMEOUT,
        )
    except OSError:
        duration = max(0, round((time.monotonic() - started) * 1000))
        return _probe_result(
            client=client,
            observed_version=observed,
            returncode=None,
            duration_ms=duration,
            codes=(EvidenceCode.CLIENT_ERROR,),
            status=SmokeStatus.FAILED,
            reason=ReasonCode.CLIENT_ERROR,
        )
    duration = max(0, round((time.monotonic() - started) * 1000))
    if any(result.returncode != 0 for result in discovery):
        return _probe_result(
            client=client,
            observed_version=observed,
            returncode=next(result.returncode for result in discovery if result.returncode != 0),
            duration_ms=duration,
            codes=(EvidenceCode.CLIENT_ERROR,),
            status=SmokeStatus.FAILED,
            reason=ReasonCode.CLIENT_ERROR,
        )
    if client is SmokeClient.CURSOR_CLI:
        server_found = 'echovault' in discovery[0].stdout.split()
        tools = set(discovery[1].stdout.split())
        complete = {
            'memory_context', 'memory_search', 'memory_details', 'memory_save',
        }.issubset(tools)
        codes = (
            *((EvidenceCode.MCP_DISCOVERED,) if server_found else ()),
            *((EvidenceCode.FOUR_TOOLS,) if complete else ()),
        )
        healthy = server_found and complete
    else:
        healthy = 'echovault' in discovery[0].stdout.split()
        codes = (EvidenceCode.EXTENSION_DISCOVERED,) if healthy else ()
    return _probe_result(
        client=client,
        observed_version=observed,
        returncode=0,
        duration_ms=duration,
        codes=codes or (EvidenceCode.CLIENT_ERROR,),
        status=SmokeStatus.PASSED if healthy else SmokeStatus.FAILED,
        reason=ReasonCode.VERIFIED if healthy else ReasonCode.CLIENT_ERROR,
    )


def run_headless_smoke(
    client: SmokeClient,
    workspace: Path,
    marker_id: str,
    runner: CommandRunner,
    environment: Mapping[str, str],
) -> SmokeEvidence:
    try:
        uuid.UUID(marker_id)
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvidenceValidationError('invalid marker UUID') from exc
    prerequisite = probe_client(client, workspace, runner, environment)
    command = (
        SmokeCommand.CURSOR_HEADLESS
        if client is SmokeClient.CURSOR_CLI
        else SmokeCommand.GEMINI_HEADLESS
    )
    marker_digest = hashlib.sha256(marker_id.encode('ascii')).hexdigest()

    def evidence(
        *,
        returncode: int | None,
        duration_ms: int,
        codes: tuple[EvidenceCode, ...],
        status: SmokeStatus,
        reason: ReasonCode,
    ) -> SmokeEvidence:
        return SmokeEvidence(
            client=client,
            expected_version=prerequisite.expected_version,
            observed_version=prerequisite.observed_version,
            scope=SmokeScope.PROJECT,
            command=command,
            exit_code=returncode,
            duration_ms=duration_ms,
            marker_id=marker_id,
            marker_digest=marker_digest,
            evidence_codes=codes,
            status=status,
            reason_code=reason,
        )

    if prerequisite.status is not SmokeStatus.PASSED:
        return evidence(
            returncode=prerequisite.exit_code,
            duration_ms=prerequisite.duration_ms,
            codes=prerequisite.evidence_codes,
            status=prerequisite.status,
            reason=prerequisite.reason_code,
        )

    cwd = _workspace(workspace)
    safe_environment = build_smoke_environment(environment)
    prompt = f'Retrieve the EchoVault marker {marker_id}; report whether it is available.'
    started = time.monotonic()
    try:
        result = runner.run(
            headless_command(client, prompt),
            timeout=120.0,
            cwd=cwd,
            env=safe_environment,
        )
    except (TimeoutError, subprocess.TimeoutExpired):
        duration = max(0, round((time.monotonic() - started) * 1000))
        return evidence(
            returncode=None,
            duration_ms=duration,
            codes=(EvidenceCode.TIMEOUT,),
            status=SmokeStatus.FAILED,
            reason=ReasonCode.TIMEOUT,
        )
    except OSError:
        duration = max(0, round((time.monotonic() - started) * 1000))
        return evidence(
            returncode=None,
            duration_ms=duration,
            codes=(EvidenceCode.CLIENT_ERROR,),
            status=SmokeStatus.FAILED,
            reason=ReasonCode.CLIENT_ERROR,
        )
    duration = max(0, round((time.monotonic() - started) * 1000))
    if result.returncode != 0:
        return evidence(
            returncode=result.returncode,
            duration_ms=duration,
            codes=(EvidenceCode.CLIENT_ERROR,),
            status=SmokeStatus.FAILED,
            reason=ReasonCode.CLIENT_ERROR,
        )
    if marker_id in result.stdout:
        return evidence(
            returncode=0,
            duration_ms=duration,
            codes=(EvidenceCode.MARKER_FOUND,),
            status=SmokeStatus.PASSED,
            reason=ReasonCode.VERIFIED,
        )
    return evidence(
        returncode=0,
        duration_ms=duration,
        codes=(EvidenceCode.MARKER_MISSED,),
        status=(
            SmokeStatus.OBSERVATIONAL_MISS
            if client is SmokeClient.CURSOR_CLI
            else SmokeStatus.FAILED
        ),
        reason=ReasonCode.MARKER_MISSED,
    )


def write_report(report: SmokeReport, output: Path) -> Path:
    payload = json.dumps(dataclasses.asdict(report), indent=2, sort_keys=True) + '\n'
    prepared = prepare_atomic_text(output, payload)
    prepared.replace()
    rows = [
        '# EchoVault client smoke report',
        '',
        f'- EchoVault: {report.echovault_version}',
        f'- Commit: {report.commit}',
        f'- Generated: {report.generated_at}',
        f'- Claim: {report.report_claim.value}',
        '',
        '| Client | Version | Scope | Status | Evidence |',
        '|---|---|---|---|---|',
    ]
    for item in (*report.evidence, *report.manual_evidence):
        rows.append(
            f'| {item.client.value} | {item.observed_version or "unavailable"} '
            f'| {item.scope.value} | {item.status.value} '
            f'| {", ".join(code.value for code in item.evidence_codes)} |'
        )
    if isinstance(report, FinalDogfoodReport):
        rows.extend([
            '',
            '| Bound agent | Marker ID | Memory ID | Replay |',
            '|---|---|---|---|',
        ])
        for marker in report.bound_markers:
            rows.append(
                f'| {marker.binding_agent.value} | {marker.marker_id} '
                f'| {marker.memory_id} | verified |'
            )
    summary = output.with_suffix('.md')
    prepared_summary = prepare_atomic_text(summary, '\n'.join(rows) + '\n')
    prepared_summary.replace()
    return output


@click.group()
def smoke_cli() -> None:
    """Create sanitized EchoVault client evidence."""


def _partial_report(output: Path, commit: str) -> SmokeReport:
    if output.exists():
        report = load_partial_report(output)
        if report.commit != commit:
            raise EvidenceValidationError('partial report commit mismatch')
        return report
    return SmokeReport(
        echovault_version=importlib.metadata.version('echovault'),
        commit=commit,
        generated_at=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        evidence=(),
    )


def append_automated_evidence(
    output: Path,
    commit: str,
    evidence: SmokeEvidence,
) -> Path:
    report = _partial_report(output, commit)
    cell = (evidence.client, evidence.command)
    if cell in {(item.client, item.command) for item in report.evidence}:
        raise EvidenceValidationError('duplicate automated evidence cell')
    return write_report(
        replace(report, evidence=(*report.evidence, evidence)),
        output,
    )


def append_manual_evidence(
    output: Path,
    commit: str,
    evidence: ManualEvidence,
) -> Path:
    report = _partial_report(output, commit)
    cell = (evidence.client, evidence.command)
    if cell in {(item.client, item.command) for item in report.manual_evidence}:
        raise EvidenceValidationError('duplicate manual evidence cell')
    return write_report(
        replace(report, manual_evidence=(*report.manual_evidence, evidence)),
        output,
    )


def _require_pinned_version(client: SmokeClient, expected_version: str) -> None:
    if expected_version != EXPECTED_CLIENT_VERSIONS[client]:
        raise EvidenceValidationError('expected version is not the pinned client version')


@smoke_cli.command('probe')
@click.option('--client', type=click.Choice(['cursor-cli', 'gemini-cli']), required=True)
@click.option('--workspace', type=click.Path(exists=True, file_okay=False, path_type=Path), required=True)
@click.option('--output', type=click.Path(path_type=Path), required=True)
@click.option('--expected-version', required=True)
@click.option('--commit', required=True)
def probe_command(
    client: str,
    workspace: Path,
    output: Path,
    expected_version: str,
    commit: str,
) -> None:
    try:
        selected = SmokeClient(client)
        _require_pinned_version(selected, expected_version)
        evidence = probe_client(
            selected,
            workspace,
            SubprocessRunner(),
            os.environ,
        )
        append_automated_evidence(output, commit, evidence)
    except EvidenceValidationError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f'appended {client} probe evidence')


@smoke_cli.command('headless')
@click.option('--client', type=click.Choice(['cursor-cli', 'gemini-cli']), required=True)
@click.option('--workspace', type=click.Path(exists=True, file_okay=False, path_type=Path), required=True)
@click.option('--output', type=click.Path(path_type=Path), required=True)
@click.option('--expected-version', required=True)
@click.option('--marker-id', required=True)
@click.option('--commit', required=True)
def headless_command_cli(
    client: str,
    workspace: Path,
    output: Path,
    expected_version: str,
    marker_id: str,
    commit: str,
) -> None:
    try:
        selected = SmokeClient(client)
        _require_pinned_version(selected, expected_version)
        evidence = run_headless_smoke(
            selected,
            workspace,
            marker_id,
            SubprocessRunner(),
            os.environ,
        )
        append_automated_evidence(output, commit, evidence)
    except EvidenceValidationError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f'appended {client} headless evidence')


@smoke_cli.command('manual')
@click.option('--client', type=click.Choice(['cursor-ide', 'cursor-cli']), required=True)
@click.option('--output', type=click.Path(path_type=Path), required=True)
@click.option('--manual-command', type=click.Choice(['cursor-ide-manual', 'cursor-interactive']), required=True)
@click.option('--manual-status', type=click.Choice([item.value for item in SmokeStatus]), required=True)
@click.option('--reason-code', type=click.Choice([item.value for item in ReasonCode]), required=True)
@click.option('--evidence-code', type=click.Choice([item.value for item in EvidenceCode]), multiple=True, required=True)
@click.option('--expected-version', required=True)
@click.option('--observed-version')
@click.option('--commit', required=True)
def manual_command_cli(
    client: str,
    output: Path,
    manual_command: str,
    manual_status: str,
    reason_code: str,
    evidence_code: tuple[str, ...],
    expected_version: str,
    observed_version: str | None,
    commit: str,
) -> None:
    try:
        selected = SmokeClient(client)
        _require_pinned_version(selected, expected_version)
        evidence = ManualEvidence(
            client=selected,
            expected_version=expected_version,
            observed_version=observed_version,
            scope=SmokeScope.PROJECT,
            command=SmokeCommand(manual_command),
            evidence_codes=tuple(EvidenceCode(code) for code in evidence_code),
            status=SmokeStatus(manual_status),
            reason_code=ReasonCode(reason_code),
        )
        append_manual_evidence(output, commit, evidence)
    except (EvidenceValidationError, ValueError) as exc:
        message = str(exc) if isinstance(exc, EvidenceValidationError) else 'invalid manual evidence'
        raise click.ClickException(message) from None
    click.echo(f'appended {client} manual evidence')


@smoke_cli.command('record-bound-marker')
@click.option('--binding-agent', type=click.Choice(['cursor', 'gemini-cli']), required=True)
@click.option('--marker-id', required=True)
@click.option('--operation-id', required=True)
@click.option('--memory-id', required=True)
@click.option('--replay-verified', is_flag=True, required=True)
@click.option('--output', type=click.Path(path_type=Path), required=True)
def record_bound_marker_command(
    binding_agent: str,
    marker_id: str,
    operation_id: str,
    memory_id: str,
    replay_verified: bool,
    output: Path,
) -> None:
    try:
        marker = BoundMarkerEvidence(
            binding_agent=BoundAgent(binding_agent),
            marker_id=marker_id,
            operation_id=operation_id,
            memory_id=memory_id,
            replay_verified=replay_verified,
        )
        write_bound_marker(marker, output)
    except EvidenceValidationError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo('wrote sanitized bound marker')


@smoke_cli.command('finalize')
@click.option(
    '--partial',
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    '--bound-marker',
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    multiple=True,
    required=True,
)
@click.option(
    '--claim',
    type=click.Choice(['not_verified', 'client_verified']),
    required=True,
)
@click.option('--output', type=click.Path(path_type=Path), required=True)
def finalize_command(
    partial: Path,
    bound_marker: tuple[Path, ...],
    claim: str,
    output: Path,
) -> None:
    try:
        finalize_report(
            partial,
            bound_marker,
            output,
            ReportClaim(claim),
        )
    except EvidenceValidationError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f'wrote {claim} final report')


@smoke_cli.command('validate-final')
@click.option(
    '--report',
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    '--require-claim',
    type=click.Choice(['not_verified', 'client_verified']),
)
def validate_final_command(report: Path, require_claim: str | None) -> None:
    required = None if require_claim is None else ReportClaim(require_claim)
    try:
        parsed = validate_final_report(report, required)
        if (
            parsed.report_claim is ReportClaim.CLIENT_VERIFIED
            and required is not ReportClaim.CLIENT_VERIFIED
        ):
            raise EvidenceValidationError(
                'client_verified validation requires --require-claim client_verified'
            )
    except EvidenceValidationError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f'valid {parsed.report_claim.value} final report')


if __name__ == '__main__':
    smoke_cli()
~~~

The CLI accepts:

~~~text
probe --client cursor-cli|gemini-cli --workspace PATH --output PARTIAL.json --expected-version VERSION --commit GIT_ID
headless --client cursor-cli|gemini-cli --workspace PATH --output PARTIAL.json --expected-version VERSION --marker-id UUID --commit GIT_ID
manual --client cursor-ide|cursor-cli --output PARTIAL.json --manual-command cursor-ide-manual|cursor-interactive --manual-status passed|failed|not_verified|observational_miss --reason-code CODE --evidence-code CODE [--evidence-code CODE] --expected-version VERSION [--observed-version VERSION] --commit GIT_ID
record-bound-marker --binding-agent cursor|gemini-cli --marker-id UUID --operation-id UUID --memory-id UUID --replay-verified --output MARKER.json
finalize --partial PARTIAL.json --bound-marker CURSOR.json --bound-marker GEMINI.json --claim not_verified|client_verified --output FINAL.json
validate-final --report FINAL.json [--require-claim not_verified|client_verified]
~~~

`probe` accepts only cursor-cli/gemini-cli and forbids marker/manual flags. `headless` accepts only cursor-cli/gemini-cli and requires `--marker-id`; it computes the paired digest itself. `manual` accepts only the exact pairs cursor-ide/cursor-ide-manual and cursor-cli/cursor-interactive, requires closed status/reason/evidence enums and version fields, executes no client process, and appends one `ManualEvidence`. Each command requires the validated Git commit, creates `--output` when absent, or strictly parses and atomically appends to that same partial report when present; a commit mismatch or duplicate client/command cell fails closed. A partial `SmokeReport` always serializes `report_claim: not_verified`; a missing binary, auth session, or pinned version therefore remains explicitly not_verified and can never be described as client_verified.

`record-bound-marker` accepts only the five closed `BoundMarkerEvidence` fields, requires the positive replay flag, and writes one object rather than a wrapper. `finalize` accepts exactly two `--bound-marker` occurrences, strictly parses the partial report and both marker documents, requires one Cursor and one Gemini binding with distinct marker/operation/memory UUIDs, and atomically writes one `FinalDogfoodReport`. `--claim client_verified` additionally requires all four automated cells exactly once at their pinned observed versions with `passed`, plus both pinned manual cells exactly once with `passed` or the explicitly non-deterministic `observational_miss`; no `failed` or `not_verified` client cell can be promoted. `--claim not_verified` keeps the report honest even if the two bound saves were proven through deterministic diagnostics while an authenticated client was unavailable.

Every loader rejects duplicate JSON keys, non-object roots, unknown fields, missing fields, wrong primitive types, unknown enum values, malformed UUIDs/digests/versions, and extra nested evidence fields. Errors use fixed schema messages and never echo values, paths, prompts, output, or environment data. `validate-final` re-parses an existing final report through that same closed schema. For a client_verified claim it must be called with `--require-claim client_verified`; for a not_verified or merely partial report it is optional and must never be cited as client verification. Only the final dogfood assembly creates `FinalDogfoodReport`, whose constructor calls both marker validation and, conditionally, client-claim validation.

Defaults are Cursor Agent CLI build 2026.07.09-a3815c0 and Gemini CLI 0.50.0. Cursor IDE 3.11.19 is a separate manual-evidence field and is never compared with `agent --version`. probe executes only:

~~~text
agent --version
agent mcp list
agent mcp list-tools echovault
gemini --version
gemini extensions list
~~~

Run only commands belonging to the selected client. Resolve `workspace` before the first command and reject symlinks/non-directories. Every runner call receives `cwd=workspace.resolve()` and exactly `build_smoke_environment(environment)`: only PATH, HOME/USERPROFILE, XDG paths, locale, temporary-directory, and required Windows process variables survive; all other keys—including token, secret, credential, password, API-key, and unrelated variables—are absent. headless first requires the exact expected version and a successful deterministic tool/extension probe, then uses the commands from Step 1 with a fresh nonsecret marker. Apply a bounded timeout and terminate the process group on expiry.

The report records only the closed enums above, validated version strings, scope, timestamp, exit code, duration, and status. Probe evidence has both marker fields `None`; headless evidence has both fields populated. `marker_digest` is SHA-256 of the nonsecret marker UUID itself, never of a prompt or response. Validate commit as a hexadecimal Git ID, timestamp as RFC 3339, UUID with `uuid.UUID`, and expected/observed versions against the selected client's strict grammar before serialization. If parsed external version output does not match, discard it, set `observed_version=None`, and emit only VERSION_MISMATCH or CLIENT_ERROR—never the raw value. Stdout/stderr are parsed in memory for allowlisted facts and discarded. A policy-guided Cursor marker miss is observational_miss. A deterministic setup/tool/hook failure is failed. Missing binary/auth/version is not_verified. `BoundMarkerEvidence` contains only canonical bound identity plus nonsecret UUIDs and the replay boolean; it never contains marker text, prompts, responses, or client output.

- [ ] **Step 4: Document the authenticated/manual portions**

docs/dogfood/README.md gives this order:

1. build and validate the wheel;
2. install the verified wheel locally;
3. run memory setup cursor and memory setup gemini, allowing each client to show its normal trust/consent UI;
4. seed one nonsecret marker through Cursor-bound memory_save and a different marker through Gemini-bound memory_save, each with its own fixed operation UUID and verified replay, then create the two closed marker files with record-bound-marker;
5. run Cursor IDE in a fresh single-root task and record manual tool evidence without copying the prompt or response;
6. run Cursor Agent CLI once interactively in the fresh workspace, then run the separate automated probe/headless commands; record only closed evidence codes from both;
7. run Gemini extension discovery and headless BeforeAgent smoke;
8. retrieve both memory IDs through Cursor, Gemini, Claude Code, and Codex where available;
9. exercise missing executable, missing MCP, embedding failure, and abrupt client exit in isolated homes;
10. mark every unavailable check not_verified; run finalize plus validate-final only for a client_verified claim, otherwise retain the not_verified partial or explicitly not_verified final assembly.

The report template has separate deterministic, observational, failure-mode, cross-agent, and privacy sections. It requires client versions and scope, but prohibits credentials, prompts, responses, transcripts, and environment dumps.

- [ ] **Step 5: Run tests and a read-only local probe**

Run: `uv run --extra dev pytest tests/test_smoke_clients.py -q`

Expected: command/version/status/sanitization tests pass.

Run:

~~~bash
mkdir -p build/dogfood
rm -f build/dogfood/partial-smoke.json
COMMIT="$(git rev-parse --short HEAD)"
uv run python scripts/smoke_clients.py probe --client cursor-cli --workspace . --output build/dogfood/partial-smoke.json --expected-version 2026.07.09-a3815c0 --commit "$COMMIT"
uv run python scripts/smoke_clients.py probe --client gemini-cli --workspace . --output build/dogfood/partial-smoke.json --expected-version 0.50.0 --commit "$COMMIT"
~~~

Expected: each available exact client appends sanitized evidence. A missing, unauthenticated, or differently versioned client exits successfully with not_verified evidence; the partial report remains `report_claim: not_verified` and must not be described as client verified.

- [ ] **Step 6: Commit tooling and template, not generated evidence**

~~~bash
git add scripts/smoke_clients.py tests/test_smoke_clients.py docs/dogfood/README.md docs/dogfood/report-template.md
git commit -m "test: add sanitized client dogfood evidence"
~~~

Do not commit build/dogfood output because it is machine/session-specific evidence. Attach or publish it only after a separate user decision.

### Task 7: Full Gate and Verified Local Dogfood Installation

**Files:**
- Modify: no repository files unless verification reveals a regression
- Generate outside Git: dist archives, build validation trees, temporary isolated homes, sanitized dogfood evidence

**Interfaces:**
- Consumes every preceding phase gate
- Replaces the local EchoVault executable only after deterministic verification
- Produces an honest local dogfood report without publishing it

- [ ] **Step 1: Confirm branch scope and clean tracked state**

Run:

~~~bash
git status --short
git log --oneline --decorate -12
git diff --check
~~~

Expected: only intentional commits from the five phase plans are present and no uncommitted production change is hidden by generated files.

- [ ] **Step 2: Run the complete source and dashboard gates**

Run:

~~~bash
uv sync --extra dev
uv run --extra dev pytest -q
cargo test --manifest-path dashboard/Cargo.toml
~~~

Expected: all Python and Rust tests pass. Stop on any failure; do not install the fork locally.

- [ ] **Step 3: Build and verify distribution artifacts**

Run:

~~~bash
rm -rf dist build/validation build/wheel-venv
uv build
uv run python scripts/verify_wheel_assets.py dist/*.whl dist/*.tar.gz
uv run python scripts/render_integration_fixture.py --client cursor --output build/validation/cursor --memory-command memory --version 0.6.0
uv run python scripts/validate_cursor_plugin.py build/validation/cursor
uv run python scripts/render_integration_fixture.py --client gemini --output build/validation/gemini --memory-command memory --version 0.6.0
uv venv build/wheel-venv --python 3.14
uv pip install --python build/wheel-venv/bin/python dist/*.whl
build/wheel-venv/bin/python scripts/verify_installed_tool.py --memory-executable build/wheel-venv/bin/memory
~~~

Expected: archives, rendered Cursor bundle, and black-box installed-wheel contracts pass. If local Gemini is exactly 0.50.0, also run gemini extensions validate build/validation/gemini; otherwise record that pinned validation is delegated to CI and do not substitute a different version as acceptance evidence.

- [ ] **Step 4: Back up managed local state and install the verified wheel**

Before replacement, record memory --version, command -v memory, memory doctor, and the paths of EchoVault-owned Cursor/Gemini manifests. Copy only EchoVault-owned manifests/config files into a timestamped local backup outside the repository; never copy tokens or unrelated environment data into the report.

Resolve the single wheel filename and install that exact file:

~~~bash
WHEEL_PATH="$(find dist -maxdepth 1 -name 'echovault-*.whl' -print -quit)"
test -n "$WHEEL_PATH"
uv tool install --force "$WHEEL_PATH"
memory --version
memory doctor
~~~

Expected: the installed version matches the built distribution and core doctor is healthy. This local tool replacement is authorized by the approved dogfood step; it does not publish or alter remote state.

- [ ] **Step 5: Install adapters with normal client consent**

Run:

~~~bash
memory setup cursor
memory doctor --agent cursor
memory setup gemini
memory doctor --agent gemini-cli
~~~

Expected: Cursor installs a valid user plugin. Gemini delegates native installation to the client and preserves its normal consent/trust flow. If no interactive terminal or exact client is available, do not bypass it; keep Gemini native setup not_verified and use isolated project-direct setup only for deterministic diagnostics.

- [ ] **Step 6: Run available pinned-client and cross-agent smoke checks**

Use the Task 6 runner for exact Cursor Agent CLI build 2026.07.09-a3815c0 and Gemini CLI 0.50.0. Record Cursor IDE 3.11.19 separately and run it manually in one fresh single-root workspace. Run the Cursor Agent CLI once interactively in that same disposable workspace, then run its automated headless check; neither evidence path substitutes for the other.

Create the machine-specific evidence tree and the four nonsecret UUIDs before invoking either bound save:

~~~bash
DOGFOOD_DIR=build/dogfood
PARTIAL_REPORT="$DOGFOOD_DIR/partial-smoke.json"
CURSOR_MARKER_FILE="$DOGFOOD_DIR/cursor-bound-marker.json"
GEMINI_MARKER_FILE="$DOGFOOD_DIR/gemini-bound-marker.json"
CLIENT_FINAL_REPORT="$DOGFOOD_DIR/client-verified-final.json"
NOT_VERIFIED_FINAL_REPORT="$DOGFOOD_DIR/not-verified-final.json"
rm -rf "$DOGFOOD_DIR"
mkdir -p "$DOGFOOD_DIR"
COMMIT="$(git rev-parse --short HEAD)"
CURSOR_MARKER_ID="$(uv run python -c 'import uuid; print(uuid.uuid4())')"
CURSOR_OPERATION_ID="$(uv run python -c 'import uuid; print(uuid.uuid4())')"
GEMINI_MARKER_ID="$(uv run python -c 'import uuid; print(uuid.uuid4())')"
GEMINI_OPERATION_ID="$(uv run python -c 'import uuid; print(uuid.uuid4())')"
~~~

Save a marker whose prefix is `CURSOR-` followed by its generated UUID through the Cursor-bound `memory_save`, and a different marker whose prefix is `GEMINI-` followed by its generated UUID through the Gemini-bound `memory_save`. Let each bound server assign source, use a distinct fixed operation UUID for each marker, and verify replay. Retrieve both resulting memory IDs through Cursor, Gemini, Claude Code, and Codex where those bound clients are available. A single generic/unbound marker does not satisfy the cross-agent acceptance gate.

After the second identical save for each operation has returned `replayed`, enter only the two returned memory UUIDs. These variables are nonsecret identifiers; abort rather than paste a response, prompt, transcript, or any other text. Produce the two closed marker documents with these exact commands:

~~~bash
IFS= read -r CURSOR_MEMORY_ID
IFS= read -r GEMINI_MEMORY_ID
uv run python scripts/smoke_clients.py record-bound-marker --binding-agent cursor --marker-id "$CURSOR_MARKER_ID" --operation-id "$CURSOR_OPERATION_ID" --memory-id "$CURSOR_MEMORY_ID" --replay-verified --output "$CURSOR_MARKER_FILE"
uv run python scripts/smoke_clients.py record-bound-marker --binding-agent gemini-cli --marker-id "$GEMINI_MARKER_ID" --operation-id "$GEMINI_OPERATION_ID" --memory-id "$GEMINI_MEMORY_ID" --replay-verified --output "$GEMINI_MARKER_FILE"
~~~

The commands fail closed unless each file contains exactly `binding_agent`, `marker_id`, `operation_id`, `memory_id`, and `replay_verified`. Build the partial report at one stable path; every command strictly reads and atomically appends to that report:

~~~bash
uv run python scripts/smoke_clients.py probe --client cursor-cli --workspace . --output "$PARTIAL_REPORT" --expected-version 2026.07.09-a3815c0 --commit "$COMMIT"
uv run python scripts/smoke_clients.py headless --client cursor-cli --workspace . --output "$PARTIAL_REPORT" --expected-version 2026.07.09-a3815c0 --marker-id "$CURSOR_MARKER_ID" --commit "$COMMIT"
uv run python scripts/smoke_clients.py probe --client gemini-cli --workspace . --output "$PARTIAL_REPORT" --expected-version 0.50.0 --commit "$COMMIT"
uv run python scripts/smoke_clients.py headless --client gemini-cli --workspace . --output "$PARTIAL_REPORT" --expected-version 0.50.0 --marker-id "$GEMINI_MARKER_ID" --commit "$COMMIT"
~~~

For successful pinned observations, append the two manual cells with the exact
commands below. For an unavailable client, omit `--observed-version` and use
`--manual-status not_verified --reason-code auth_missing --evidence-code auth_missing`;
for a policy-guided miss use `observational_miss`, `marker_missed`, and
`marker_missed`. No free-form evidence text is accepted.

~~~bash
uv run python scripts/smoke_clients.py manual --client cursor-ide --output "$PARTIAL_REPORT" --manual-command cursor-ide-manual --manual-status passed --reason-code verified --evidence-code marker_found --expected-version 3.11.19 --observed-version 3.11.19 --commit "$COMMIT"
uv run python scripts/smoke_clients.py manual --client cursor-cli --output "$PARTIAL_REPORT" --manual-command cursor-interactive --manual-status passed --reason-code verified --evidence-code marker_found --expected-version 2026.07.09-a3815c0 --observed-version 2026.07.09-a3815c0 --commit "$COMMIT"
~~~

The partial file remains `report_claim: not_verified` regardless of how many
cells passed.

When every required pinned client cell is present and neither client is unavailable, assemble and validate the only artifact that may support a client-verified claim:

~~~bash
uv run python scripts/smoke_clients.py finalize --partial "$PARTIAL_REPORT" --bound-marker "$CURSOR_MARKER_FILE" --bound-marker "$GEMINI_MARKER_FILE" --claim client_verified --output "$CLIENT_FINAL_REPORT"
uv run python scripts/smoke_clients.py validate-final --report "$CLIENT_FINAL_REPORT" --require-claim client_verified
~~~

If any client is missing, unauthenticated, on a different version, failed, or was not observed, do not run those client-verified commands. Keep `partial-smoke.json` as not_verified. If both bound marker files nevertheless exist from deterministic diagnostics, an explicitly non-verified final assembly is allowed but conveys no client-verification claim:

~~~bash
uv run python scripts/smoke_clients.py finalize --partial "$PARTIAL_REPORT" --bound-marker "$CURSOR_MARKER_FILE" --bound-marker "$GEMINI_MARKER_FILE" --claim not_verified --output "$NOT_VERIFIED_FINAL_REPORT"
~~~

A partial `SmokeReport`, a not_verified final report, a generic/unbound marker, duplicate IDs, or a save without verified replay cannot satisfy the client-verified gate. `validate-final --require-claim client_verified` is mandatory only when `client-verified-final.json` is used to make that claim.

Run native Gemini install, update, and uninstall smoke only with normal interactive consent. After uninstall, restore the intended native dogfood state with memory setup gemini and rerun doctor.

Record missing executable, missing MCP, embedding failure, and abrupt exit behavior in isolated temporary homes. Do not damage the user's active configuration to simulate failures.

- [ ] **Step 7: Review evidence and final repository state**

When all clients were available, the final local report must be `build/dogfood/client-verified-final.json`, must pass the exact `validate-final --require-claim client_verified` command above, and must state:

- exact built EchoVault version and commit;
- client expected/observed versions;
- deterministic installation, tool, hook, replay, and cross-agent results;
- Cursor policy-guided observations separately;
- every not_verified item;
- privacy checks and failure-mode results;
- local backup path and resulting managed integration state.

Run:

~~~bash
if test -f build/dogfood/client-verified-final.json; then
  uv run python scripts/smoke_clients.py validate-final --report build/dogfood/client-verified-final.json --require-claim client_verified
fi
git status --short
uv run --extra dev pytest -q
cargo test --manifest-path dashboard/Cargo.toml
memory doctor
memory doctor --agent cursor
memory doctor --agent gemini-cli
~~~

Expected: repository tests remain green, generated build/report files are untracked or ignored, and installed doctor output is healthy or explicitly documented as degraded/not_verified. If the client-verified final path is absent, the evidence remains partial/not_verified and no completion text may call the clients verified.

No commit is expected for machine-specific evidence. If verification required a code or documentation fix, return to RED/GREEN in the owning task, commit the focused fix, and rerun this entire task from Step 1.

## Phase Completion Gate

The delivery is ready for a separate push/release decision only when:

~~~bash
rm -rf dist build/validation build/wheel-venv
uv sync --extra dev
uv run --extra dev pytest -q
cargo test --manifest-path dashboard/Cargo.toml
uv build
uv run python scripts/verify_wheel_assets.py dist/*.whl dist/*.tar.gz
uv run python scripts/render_integration_fixture.py --client cursor --output build/validation/cursor --memory-command memory --version 0.6.0
uv run python scripts/validate_cursor_plugin.py build/validation/cursor
uv run python scripts/render_integration_fixture.py --client gemini --output build/validation/gemini --memory-command memory --version 0.6.0
uv venv build/wheel-venv --python 3.14
uv pip install --python build/wheel-venv/bin/python dist/*.whl
build/wheel-venv/bin/python scripts/verify_installed_tool.py --memory-executable build/wheel-venv/bin/memory
if test -f build/dogfood/client-verified-final.json; then
  uv run python scripts/smoke_clients.py validate-final --report build/dogfood/client-verified-final.json --require-claim client_verified
fi
git diff --check
git status --short
~~~

The cleanup makes this gate independent of distribution artifacts left by Task 7. All deterministic commands must pass; `build/validation/cursor`, `build/validation/gemini`, and `build/wheel-venv` are recreated by the block itself. CI confirms the pinned Gemini 0.50.0 validator and full OS/Python matrix, and the verified wheel is installed locally. The conditional is fail-closed for the canonical client-verified artifact: if that file exists, strict parsing, both bound markers, all pinned client cells, and the `client_verified` claim must validate. If clients were unavailable, the canonical client-verified file must not be created; `partial-smoke.json` or `not-verified-final.json` remains explicitly not_verified and cannot be cited as client verification. Pushing, publishing, tagging, marketplace submission, and upstream contribution remain outside this plan.
