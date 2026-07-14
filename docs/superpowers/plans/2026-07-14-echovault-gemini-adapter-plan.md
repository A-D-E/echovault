# EchoVault Gemini Extension and Hooks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver deterministic task-time EchoVault context for Gemini CLI through a native extension, safe direct fallbacks, and one effective injection across valid global/project coexistence states.

**Architecture:** Reuse the bound MCP, project resolver, curated behavior, config ownership, and diagnostics from prior phases. A bounded Gemini BeforeAgent worker retrieves locally without feedback, then an event-claim store permits exactly one successful hook invocation to inject context and record feedback.

**Tech Stack:** Python 3.10–3.14, Click, multiprocessing, JSON, Gemini CLI extension/hook/settings/skill formats, pytest, injected subprocess runners.

## Global Constraints

- Complete the Local Core, Bound MCP, and Cursor Adapter Foundation plans first.
- Native mode uses Gemini's supported extension manager; do not bypass its consent prompt.
- Native extension source is owned under the platform EchoVault config root, then copied by `gemini extensions install <local-path>`.
- User native N and user direct U are mutually exclusive; project direct P may coexist with either.
- Project settings MCP named echovault takes precedence over the same extension/global definition.
- Project direct always carries its own hook when hooks are supported, regardless of global activation.
- Duplicate valid hooks may execute, but one event yields at most one additionalContext response and one feedback record.
- Hook event identity uses only session_id, hook_event_name, and timestamp; never prompt/cwd/transcript/context.
- Claims expire after ten minutes and contain only digest, integration version, and expiry.
- The prompt is a retrieval query only; never persist or log it, and never open transcript_path.
- Automatic remote query embedding remains disabled unless the explicit redacted-query opt-in is set.
- Hook stdout is one valid JSON document; diagnostics go to stderr without prompt/response/token/secret content.
- On recoverable failure or internal deadline, return an empty JSON object; static instructions/MCP remain available.
- Native/user direct global commands use an absolute validated memory executable; project direct defaults to portable memory.
- Full deterministic phase acceptance targets Gemini CLI 0.50.0; older/missing clients report degraded rather than silently changing scope.
- Begin each behavior change with an observed failing test.

---

## File Responsibility Map

- **Create: src/memory/integrations/gemini_state.py** — pure N/U/P state and transition planner.
- **Modify: src/memory/integrations/process.py** — reuse the Phase 3 injectable command runner without changing its contract.
- **Create: src/memory/integrations/gemini.py** — native/direct setup, detection, uninstall, and diagnostics.
- **Create: src/memory/integrations/gemini_hook.py** — payload validation, bounded worker, claim store, and response.
- **Create: src/memory/context_pack.py** — shared context-pack JSON and Gemini additional-context rendering.
- **Create Gemini assets:** gemini-extension.json, GEMINI.md, hooks/hooks.json; reuse canonical common skill.
- **Modify: src/memory/integrations/registry.py** — register Gemini.
- **Modify: src/memory/integrations/diagnostics.py** — Gemini findings.
- **Modify: src/memory/mcp_server.py** — use shared context-pack formatter.
- **Modify: src/memory/cli.py** — Gemini setup/uninstall and hook commands.
- **Modify: src/memory/setup.py** — public Gemini wrappers.
- **Modify: src/memory/health.py** — compose Gemini state/claim/privacy diagnostics.
- **Create tests:** tests/test_gemini_state.py, tests/test_gemini_assets.py, tests/test_gemini_hook.py, tests/test_gemini_integration.py.
- **Modify tests:** tests/test_cli.py, tests/test_setup.py, tests/test_integration_diagnostics.py, tests/test_mcp_server.py.

### Task 1: Normative Gemini N/U/P Transition Planner

**Files:**
- Create: src/memory/integrations/gemini_state.py
- Create: tests/test_gemini_state.py

**Interfaces:**
- Produces: GeminiTarget, ArtifactState, GeminiInstallationState, GeminiTransition
- Produces: InvalidGeminiState and GeminiStateConflict
- Produces: plan_gemini_transition(state, target, operation, force_managed=False)
- Contains no filesystem/subprocess logic

- [ ] **Step 1: Write the complete failing state matrix**

~~~python
N = GeminiTarget.NATIVE
U = GeminiTarget.USER_DIRECT
P = GeminiTarget.PROJECT_DIRECT


def state(*, n: bool = False, u: bool = False, p: bool = False) -> GeminiInstallationState:
    return GeminiInstallationState(
        native=ArtifactState.OWNED if n else ArtifactState.ABSENT,
        user_direct=ArtifactState.OWNED if u else ArtifactState.ABSENT,
        project_direct=ArtifactState.OWNED if p else ArtifactState.ABSENT,
    )


@pytest.mark.parametrize(
    ('state', 'target', 'expected'),
    [
        (state(), N, 'install'),
        (state(), U, 'install'),
        (state(), P, 'install'),
        (state(n=True), N, 'unchanged'),
        (state(n=True), U, 'conflict_uninstall_native'),
        (state(n=True), P, 'install'),
        (state(u=True), N, 'conflict_uninstall_user_direct'),
        (state(u=True), U, 'unchanged'),
        (state(u=True), P, 'install'),
        (state(p=True), N, 'install'),
        (state(p=True), U, 'install'),
        (state(p=True), P, 'unchanged'),
        (state(n=True, p=True), N, 'unchanged'),
        (state(n=True, p=True), U, 'conflict_uninstall_native'),
        (state(n=True, p=True), P, 'unchanged'),
        (state(u=True, p=True), N, 'conflict_uninstall_user_direct'),
        (state(u=True, p=True), U, 'unchanged'),
        (state(u=True, p=True), P, 'unchanged'),
    ],
)
def test_setup_matrix(state, target, expected) -> None:
    transition = plan_gemini_transition(state, target, operation='setup')
    assert transition.code == expected


@pytest.mark.parametrize('with_project', [False, True])
def test_invalid_n_plus_u_blocks_setup(with_project: bool) -> None:
    invalid = state(n=True, u=True, p=with_project)
    for target in (N, U, P):
        with pytest.raises(InvalidGeminiState):
            plan_gemini_transition(invalid, target, operation='setup')


@pytest.mark.parametrize('target', [N, U, P])
def test_owned_target_uninstall_is_scope_exact(target: GeminiTarget) -> None:
    current = state(n=True, p=True) if target is N else (
        state(u=True, p=True) if target is U else state(n=True, p=True)
    )
    transition = plan_gemini_transition(current, target, operation='uninstall')
    assert transition.code == 'remove'
    assert transition.target is target
    assert transition.mutation == 'remove'


@pytest.mark.parametrize(
    'artifact',
    [ArtifactState.MODIFIED, ArtifactState.CUSTOM, ArtifactState.MALFORMED],
)
def test_target_conflict_never_proposes_mutation(artifact: ArtifactState) -> None:
    current = GeminiInstallationState(native=artifact)
    with pytest.raises(GeminiStateConflict):
        plan_gemini_transition(current, N, operation='setup')


def test_outdated_owned_target_plans_update() -> None:
    current = GeminiInstallationState(native=ArtifactState.OWNED_OUTDATED)
    transition = plan_gemini_transition(current, N, operation='setup')
    assert transition.code == 'update'
    assert transition.mutation == 'update'


def test_custom_other_global_mode_blocks_second_global_mode() -> None:
    current = GeminiInstallationState(native=ArtifactState.CUSTOM)
    with pytest.raises(GeminiStateConflict):
        plan_gemini_transition(current, U, operation='setup')


def state_with_target(target: GeminiTarget, artifact: ArtifactState) -> GeminiInstallationState:
    values = {
        'native': ArtifactState.ABSENT,
        'user_direct': ArtifactState.ABSENT,
        'project_direct': ArtifactState.ABSENT,
    }
    values[{
        N: 'native',
        U: 'user_direct',
        P: 'project_direct',
    }[target]] = artifact
    return GeminiInstallationState(**values)


@pytest.mark.parametrize('target', [N, U, P])
@pytest.mark.parametrize(
    ('operation', 'expected'),
    [('setup', 'update'), ('uninstall', 'remove')],
)
def test_force_managed_allows_only_selected_modified_target(
    target: GeminiTarget, operation: str, expected: str,
) -> None:
    modified = state_with_target(target, ArtifactState.MODIFIED)
    with pytest.raises(GeminiStateConflict):
        plan_gemini_transition(modified, target, operation=operation)
    transition = plan_gemini_transition(
        modified,
        target,
        operation=operation,
        force_managed=True,
    )
    assert transition.code == expected


@pytest.mark.parametrize('artifact', [ArtifactState.CUSTOM, ArtifactState.MALFORMED])
@pytest.mark.parametrize('operation', ['setup', 'uninstall'])
def test_force_never_overrides_custom_or_malformed(
    artifact: ArtifactState, operation: str,
) -> None:
    with pytest.raises(GeminiStateConflict):
        plan_gemini_transition(
            state_with_target(N, artifact),
            N,
            operation=operation,
            force_managed=True,
        )


def test_force_never_overrides_modified_other_global_scope() -> None:
    current = GeminiInstallationState(user_direct=ArtifactState.MODIFIED)
    with pytest.raises(GeminiStateConflict):
        plan_gemini_transition(
            current,
            N,
            operation='setup',
            force_managed=True,
        )
~~~

Filesystem transition tests in Task 7 prove that N/U removal leaves P and P removal leaves the valid global state; this pure task proves the planner mutates only the requested target.

- [ ] **Step 2: Run state tests and observe missing module**

Run: `uv run --extra dev pytest tests/test_gemini_state.py -q`

Expected: collection fails because gemini_state does not exist.

- [ ] **Step 3: Implement pure states and explicit commands**

~~~python
class GeminiTarget(str, Enum):
    NATIVE = 'native'
    USER_DIRECT = 'user-direct'
    PROJECT_DIRECT = 'project-direct'


class ArtifactState(str, Enum):
    ABSENT = 'absent'
    OWNED = 'owned'
    OWNED_OUTDATED = 'owned-outdated'
    MODIFIED = 'modified'
    CUSTOM = 'custom'
    MALFORMED = 'malformed'


@dataclass(frozen=True)
class GeminiInstallationState:
    native: ArtifactState = ArtifactState.ABSENT
    user_direct: ArtifactState = ArtifactState.ABSENT
    project_direct: ArtifactState = ArtifactState.ABSENT
    native_enabled: bool = True
    conflicts: tuple[str, ...] = ()


@dataclass(frozen=True)
class GeminiTransition:
    code: str
    target: GeminiTarget
    mutation: Literal['install', 'update', 'remove', 'none']
    message: str


class InvalidGeminiState(ValueError):
    pass


class GeminiStateConflict(ValueError):
    pass


_PRESENT_MANAGED = {
    ArtifactState.OWNED,
    ArtifactState.OWNED_OUTDATED,
    ArtifactState.MODIFIED,
}


def _target_state(
    state: GeminiInstallationState,
    target: GeminiTarget,
) -> ArtifactState:
    return {
        GeminiTarget.NATIVE: state.native,
        GeminiTarget.USER_DIRECT: state.user_direct,
        GeminiTarget.PROJECT_DIRECT: state.project_direct,
    }[target]


def plan_gemini_transition(
    state: GeminiInstallationState,
    target: GeminiTarget,
    operation: Literal['setup', 'uninstall'],
    force_managed: bool = False,
) -> GeminiTransition:
    if state.native in _PRESENT_MANAGED and state.user_direct in _PRESENT_MANAGED:
        raise InvalidGeminiState('Native and user-direct Gemini modes cannot coexist')

    current = _target_state(state, target)
    if current in {ArtifactState.CUSTOM, ArtifactState.MALFORMED}:
        raise GeminiStateConflict(f'{target.value} is {current.value}')
    if current is ArtifactState.MODIFIED and not force_managed:
        raise GeminiStateConflict(f'{target.value} is modified; use --force-managed')

    if operation == 'uninstall':
        code = 'remove' if current in _PRESENT_MANAGED else 'unchanged'
        mutation = 'remove' if current in _PRESENT_MANAGED else 'none'
        return GeminiTransition(code, target, mutation, code)

    if target is GeminiTarget.NATIVE and state.user_direct is not ArtifactState.ABSENT:
        if state.user_direct in {ArtifactState.OWNED, ArtifactState.OWNED_OUTDATED}:
            return GeminiTransition(
                'conflict_uninstall_user_direct',
                target,
                'none',
                'Run memory uninstall gemini --direct first',
            )
        raise GeminiStateConflict(f'user-direct is {state.user_direct.value}; force applies only to the selected target')
    if target is GeminiTarget.USER_DIRECT and state.native is not ArtifactState.ABSENT:
        if state.native in {ArtifactState.OWNED, ArtifactState.OWNED_OUTDATED}:
            return GeminiTransition(
                'conflict_uninstall_native',
                target,
                'none',
                'Run memory uninstall gemini first',
            )
        raise GeminiStateConflict(f'native is {state.native.value}; force applies only to the selected target')
    if current is ArtifactState.OWNED:
        return GeminiTransition('unchanged', target, 'none', 'unchanged')
    if current is ArtifactState.OWNED_OUTDATED:
        return GeminiTransition('update', target, 'update', 'update')
    if current is ArtifactState.MODIFIED:
        return GeminiTransition('update', target, 'update', 'update')
    return GeminiTransition('install', target, 'install', 'install')
~~~

The adapter passes `options.force_managed` into every setup/uninstall planner call, converts conflict_* transitions into GeminiStateConflict before any filesystem or subprocess mutation, and never removes or force-overwrites a different scope automatically.

- [ ] **Step 4: Run full state matrix**

Run: `uv run --extra dev pytest tests/test_gemini_state.py -q`

Expected: all setup/uninstall/conflict states pass.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/integrations/gemini_state.py tests/test_gemini_state.py
git commit -m "feat: define gemini integration state machine"
~~~

### Task 2: Gemini Extension, Context, Skill, and Hook Assets

**Files:**
- Create: src/memory/integrations/assets/gemini/gemini-extension.json
- Create: src/memory/integrations/assets/gemini/GEMINI.md
- Create: src/memory/integrations/assets/gemini/hooks.json
- Create: tests/test_gemini_assets.py
- Modify: src/memory/integrations/asset_io.py

**Interfaces:**
- Produces: render_gemini_assets(memory_command: str, hook_command: str, version: str) -> dict[str, bytes]
- Reuses: common/echovault-skill.md
- Defines asset metadata for canonical agent `gemini-cli`; Task 5 registers the concrete adapter after it exists

- [ ] **Step 1: Write failing asset tests**

~~~python
def test_rendered_gemini_extension_has_bound_mcp_hook_context_and_skill() -> None:
    assets = render_gemini_assets(
        memory_command='/opt/bin/memory',
        hook_command='/opt/bin/memory hook gemini before-agent',
        version='0.6.0',
    )
    manifest = json.loads(assets['gemini-extension.json'])
    hooks = json.loads(assets['hooks/hooks.json'])
    assert manifest['name'] == 'echovault'
    assert manifest['mcpServers']['echovault']['args'] == ['mcp', '--agent', 'gemini-cli']
    assert manifest['contextFileName'] == 'GEMINI.md'
    command = hooks['hooks']['BeforeAgent'][0]['hooks'][0]['command']
    assert 'hook gemini before-agent' in command
    assert hooks['hooks']['BeforeAgent'][0]['hooks'][0]['timeout'] == 5000
    assert b'memory_context' in assets['GEMINI.md']
    skill = assets['skills/echovault/SKILL.md'].decode()
    assert 'memory_save' in skill
    for forbidden in ('source=cursor', 'source=gemini', 'agent=cursor', 'agent=gemini', 'project='):
        assert forbidden not in skill
~~~

- [ ] **Step 2: Run asset tests and observe missing renderer**

Run: `uv run --extra dev pytest tests/test_gemini_assets.py -q`

Expected: render_gemini_assets and Gemini resources are missing.

- [ ] **Step 3: Add canonical templates**

gemini-extension.json:

~~~json
{
  "name": "echovault",
  "version": "{{VERSION}}",
  "description": "Local-first curated memory for Gemini CLI",
  "mcpServers": {
    "echovault": {
      "command": "{{MEMORY_COMMAND}}",
      "args": ["mcp", "--agent", "gemini-cli"]
    }
  },
  "contextFileName": "GEMINI.md"
}
~~~

The packaged resource is exactly `src/memory/integrations/assets/gemini/hooks.json`; the renderer deliberately maps that resource to the extension output key `hooks/hooks.json`. Test both the `importlib.resources` package path and the rendered output key so packaging and Gemini layout cannot drift.

Rendered hooks/hooks.json:

~~~json
{
  "hooks": {
    "BeforeAgent": [
      {
        "matcher": "*",
        "hooks": [
          {
            "name": "echovault-context",
            "type": "command",
            "command": "{{HOOK_COMMAND}}",
            "timeout": 5000,
            "description": "Inject curated EchoVault context"
          }
        ]
      }
    ]
  }
}
~~~

GEMINI.md contains a minimal versioned reference to the same curated lifecycle as the agent-neutral common skill. Bound tool calls omit caller-selected `agent`, `source`, and `project`; the MCP binding supplies `gemini-cli`. Render structured MCP command/args separately from the shell command string. Use shlex.join on POSIX and subprocess.list2cmdline on Windows so paths with spaces remain one executable argument.

- [ ] **Step 4: Run Gemini/Cursor asset and registry tests**

Run: `uv run --extra dev pytest tests/test_gemini_assets.py tests/test_cursor_integration.py -k assets tests/test_integration_registry.py -q`

Expected: both clients reuse the same behavior source without path/quoting failures.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/integrations/assets/gemini/gemini-extension.json src/memory/integrations/assets/gemini/GEMINI.md src/memory/integrations/assets/gemini/hooks.json src/memory/integrations/asset_io.py tests/test_gemini_assets.py
git commit -m "feat: add gemini extension assets"
~~~

### Task 3: Shared Context Pack and Gemini BeforeAgent Handler

**Files:**
- Create: src/memory/context_pack.py
- Create: src/memory/integrations/gemini_hook.py
- Create: tests/gemini_helpers.py
- Create: tests/test_gemini_hook.py
- Modify: src/memory/mcp_server.py
- Modify: tests/test_mcp_server.py

**Interfaces:**
- Produces: GeminiBeforeAgentInput
- Produces: build_context_pack and render_gemini_additional_context
- Produces: before_agent_success(additional_context) -> dict[str, object]
- Produces: process_before_agent(payload, service_factory, claim_store)
- Ignores transcript_path without opening it

- [ ] **Step 1: Write failing valid/invalid/transcript tests**

~~~python
VALID_EVENT = {
    'session_id': 'session-1',
    'transcript_path': '/must/not/be-read.json',
    'cwd': '/workspace/repo',
    'hook_event_name': 'BeforeAgent',
    'timestamp': '2026-07-14T12:00:00Z',
    'prompt': 'Find marker ALPHA-42',
}

_REAL_PATH_OPEN = Path.open


def fail_if_transcript_path(path: Path, *args, **kwargs):
    if path == Path(VALID_EVENT['transcript_path']):
        raise AssertionError('transcript_path was opened')
    return _REAL_PATH_OPEN(path, *args, **kwargs)


class AlwaysWinsClaimStore:
    def try_claim(self, event: GeminiBeforeAgentInput, *, now=None) -> bool:
        return True


def always_wins() -> AlwaysWinsClaimStore:
    return AlwaysWinsClaimStore()


class CountingClaimStore:
    def __init__(self) -> None:
        self.attempts = 0

    def try_claim(self, event: GeminiBeforeAgentInput, *, now=None) -> bool:
        self.attempts += 1
        return True


class SeededServiceFactory:
    def __init__(self, service: MemoryService) -> None:
        self.service = service

    def __call__(self) -> MemoryService:
        return self.service


def retrieved_count(factory: SeededServiceFactory, marker: str) -> int:
    row = factory.service.db.conn.execute(
        'SELECT retrieved_count FROM memories WHERE what LIKE ?',
        (f'%{marker}%',),
    ).fetchone()
    assert row is not None
    return int(row['retrieved_count'] or 0)


@pytest.fixture
def seeded_service_factory(env_home):
    service = MemoryService(str(env_home))
    service.save(RawMemoryInput(
        title='Marker ALPHA-42',
        what='Durable marker ALPHA-42',
        category='context',
        source='cursor',
    ), project='workspace-repo')
    yield SeededServiceFactory(service)
    service.db.close()


def test_before_agent_returns_curated_context_without_reading_transcript(monkeypatch, seeded_service_factory) -> None:
    monkeypatch.setattr(Path, 'open', fail_if_transcript_path)
    result = process_before_agent(VALID_EVENT, service_factory=seeded_service_factory, claim_store=always_wins())
    assert result['hookSpecificOutput']['hookEventName'] == 'BeforeAgent'
    additional = result['hookSpecificOutput']['additionalContext']
    assert 'ALPHA-42' in additional
    assert 'memory-id:' in additional


@pytest.mark.parametrize('field', ['session_id', 'cwd', 'hook_event_name', 'timestamp', 'prompt'])
def test_missing_required_hook_field_fails_open(field, seeded_service_factory) -> None:
    payload = {key: value for key, value in VALID_EVENT.items() if key != field}
    assert process_before_agent(payload, service_factory=seeded_service_factory) == {}


def test_context_mode_off_returns_empty_without_retrieval_claim_or_feedback(
    env_home, monkeypatch,
) -> None:
    service = MemoryService(str(env_home))
    service.config.context.agent_modes['gemini-cli'] = 'off'
    claims = CountingClaimStore()
    monkeypatch.setattr(service, 'get_context', pytest.fail)
    monkeypatch.setattr(service.db, 'record_feedback', pytest.fail)
    assert process_before_agent(
        VALID_EVENT,
        service_factory=lambda: service,
        claim_store=claims,
    ) == {}
    assert claims.attempts == 0
~~~

- [ ] **Step 2: Run hook/MCP formatter tests and observe missing handler**

Run: `uv run --extra dev pytest tests/test_gemini_hook.py tests/test_mcp_server.py -k "before_agent or context_pack" -q`

Expected: context_pack/gemini_hook imports fail.

- [ ] **Step 3: Implement validated prompt-only retrieval**

~~~python
@dataclass(frozen=True)
class GeminiBeforeAgentInput:
    session_id: str
    cwd: Path
    hook_event_name: str
    timestamp: str
    prompt: str


def parse_before_agent(payload: Mapping[str, Any]) -> GeminiBeforeAgentInput:
    required = ('session_id', 'cwd', 'hook_event_name', 'timestamp', 'prompt')
    values = {name: payload.get(name) for name in required}
    if any(not isinstance(value, str) or not value.strip() for value in values.values()):
        raise HookInputError('Missing required BeforeAgent field')
    if values['hook_event_name'] != 'BeforeAgent':
        raise HookInputError('Unexpected hook event')
    return GeminiBeforeAgentInput(
        session_id=values['session_id'],
        cwd=Path(values['cwd']),
        hook_event_name=values['hook_event_name'],
        timestamp=values['timestamp'],
        prompt=values['prompt'],
    )


def before_agent_success(additional_context: str) -> dict[str, object]:
    if not additional_context.strip():
        return {}
    return {
        'hookSpecificOutput': {
            'hookEventName': 'BeforeAgent',
            'additionalContext': additional_context,
        },
    }
~~~

Place `VALID_EVENT`, `SeededServiceFactory`, `retrieved_count`, and the seeded factory fixture in `tests/gemini_helpers.py`, then import them explicitly in hook and integration tests. Keep transcript-open and claim-store doubles local to the hook test module. Resolve the project from trusted protocol cwd and ProjectRegistry. Before retrieval, call `resolve_context_mode(service.config, 'gemini-cli')`; if it is `off`, return `{}` without retrieval, claim, or feedback. Otherwise call get_context with query=prompt, agent=gemini-cli, token_budget=1200, and record_feedback=false. Do not access transcript_path. build_context_pack returns stable dictionaries also used by MCP; render_gemini_additional_context emits concise Markdown with memory ID, title, what/why/impact, constraints/playbooks, provenance, and a note that it is retrieved context, not an instruction to persist the prompt. Successful non-empty output must go through `before_agent_success`, including the exact `hookEventName` envelope required by Gemini.

All recoverable validation/retrieval exceptions return {} and log only an error code to stderr.

- [ ] **Step 4: Run hook and MCP formatter tests**

Run: `uv run --extra dev pytest tests/test_gemini_hook.py tests/test_mcp_server.py -q`

Expected: valid event, missing field, transcript non-read, project resolution, and shared formatting pass.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/context_pack.py src/memory/integrations/gemini_hook.py src/memory/mcp_server.py tests/gemini_helpers.py tests/test_gemini_hook.py tests/test_mcp_server.py
git commit -m "feat: inject gemini task context"
~~~

### Task 4: Hook Event Claims, Feedback Exactly Once, and Deadline

**Files:**
- Modify: src/memory/integrations/gemini_hook.py
- Modify: tests/test_gemini_hook.py

**Interfaces:**
- Produces: HookClaimStore(memory_home, ttl_seconds=600)
- Produces: handle_before_agent(payload, timeout_seconds=4.0)
- Guarantees at most one additionalContext and feedback record for an event

- [ ] **Step 1: Add failing duplicate/expiry/privacy/deadline tests**

~~~python
def fixed_now() -> datetime:
    return datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


def slow_spawn_worker(request: dict[str, object], result_queue) -> None:
    time.sleep(1.0)
    result_queue.put({'ok': True, 'response': {}})


def test_duplicate_event_injects_and_records_feedback_once(env_home: Path, seeded_service_factory) -> None:
    store = HookClaimStore(env_home)
    first = process_before_agent(VALID_EVENT, service_factory=seeded_service_factory, claim_store=store)
    second = process_before_agent(VALID_EVENT, service_factory=seeded_service_factory, claim_store=store)
    assert first['hookSpecificOutput']['hookEventName'] == 'BeforeAgent'
    assert 'additionalContext' in first['hookSpecificOutput']
    assert second == {}
    assert retrieved_count(seeded_service_factory, 'ALPHA-42') == 1


def test_claim_contains_no_prompt_cwd_transcript_or_context(env_home: Path) -> None:
    store = HookClaimStore(env_home)
    store.try_claim(parse_before_agent(VALID_EVENT), now=fixed_now())
    raw = next((env_home / 'hook-events').glob('*.json')).read_text()
    for forbidden in ('ALPHA-42', '/workspace/repo', '/must/not-be-read.json', 'session-1'):
        assert forbidden not in raw


def test_internal_deadline_returns_empty_json(env_home: Path) -> None:
    assert handle_before_agent(
        VALID_EVENT,
        memory_home=env_home,
        timeout_seconds=0.05,
        worker_target=slow_spawn_worker,
    ) == {}


def test_spawn_context_accepts_only_serializable_request_and_result(env_home: Path) -> None:
    context = multiprocessing.get_context('spawn')
    response = handle_before_agent(
        VALID_EVENT,
        memory_home=env_home,
        timeout_seconds=2.0,
        multiprocessing_context=context,
    )
    assert isinstance(response, dict)
~~~

- [ ] **Step 2: Run hook claim tests and observe duplicate context/feedback**

Run: `uv run --extra dev pytest tests/test_gemini_hook.py -k "duplicate or claim or deadline" -q`

Expected: both invocations inject/record, or claim/deadline APIs are missing.

- [ ] **Step 3: Implement digest-only claims and bounded worker**

Event digest:

~~~python
def hook_event_digest(event: GeminiBeforeAgentInput) -> str:
    identity = {
        'session_id': event.session_id,
        'hook_event_name': event.hook_event_name,
        'timestamp': event.timestamp,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()
~~~

HookClaimStore cleans expired claims under hook-events/.lock, then uses O_CREAT|O_EXCL for the digest JSON file. Store only digest, integration_version, and expires_at. Retrieval happens before claim with feedback disabled. Only the claimant calls db.record_feedback(ids) and returns additionalContext; losers return {}.

Define `_spawn_before_agent_worker(request: dict[str, object], result_queue) -> None` at module scope so it is importable and picklable on Windows. The parent validates input and converts it to a JSON-compatible request containing only the validated event fields, `memory_home`, integration version, and timeout; no service, closure, open handle, prompt derivative, or exception object crosses the process boundary. The worker creates/closes its own MemoryService and puts exactly one JSON-compatible `{'ok': bool, 'response': dict, 'error_code': str | None}` result.

`handle_before_agent` defaults to `multiprocessing.get_context('spawn')` on every platform, runs that module-level worker, waits four seconds by default, terminates and joins on deadline, closes its queue, and returns `{}`. Tests may inject only a module-level `worker_target` and an explicit multiprocessing context; production does not accept arbitrary callables from hook input. The extension's outer timeout remains 5000 ms. Run the forced-spawn test on Linux, macOS, and Windows rather than skipping it outside Windows.

- [ ] **Step 4: Run all hook tests**

Run: `uv run --extra dev pytest tests/test_gemini_hook.py -q`

Expected: duplicate, expiry, privacy, fail-open, timeout, and feedback assertions pass on the current OS.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/integrations/gemini_hook.py tests/test_gemini_hook.py
git commit -m "feat: deduplicate gemini hook events"
~~~

### Task 5: User-Direct and Project-Direct Fallbacks

**Files:**
- Create: src/memory/integrations/gemini.py
- Create: tests/test_gemini_integration.py
- Modify: tests/gemini_helpers.py
- Modify: src/memory/integrations/registry.py

**Interfaces:**
- User direct target: ~/.gemini or explicit --config-dir
- Project direct target: PROJECT/.gemini or explicit --config-dir
- Direct artifacts: settings.json MCP/hook, marked GEMINI.md, skill, ownership manifest

- [ ] **Step 1: Write failing direct-mode tests**

~~~python
class RecordingRunner:
    def __init__(
        self,
        *,
        installed: bool = False,
        installed_version: str = '0.6.0',
        client_version: str = '0.50.0',
    ) -> None:
        self.installed = installed
        self.installed_version = installed_version
        self.client_version = client_version
        self.argv: list[list[str]] = []
        self.calls: list[dict[str, object]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        capture_output: bool = True,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        command = list(argv)
        self.argv.append(command)
        self.calls.append({
            'argv': command,
            'capture_output': capture_output,
            'cwd': cwd,
            'env': None if env is None else dict(env),
        })
        if command == ['gemini', '--version']:
            return CommandResult(0, f'{self.client_version}\n', '')
        if command == ['gemini', 'extensions', 'list']:
            line = f'echovault {self.installed_version} local\n' if self.installed else ''
            return CommandResult(0, line, '')
        if command[1:3] == ['extensions', 'install']:
            self.installed = True
        elif command[1:3] == ['extensions', 'uninstall']:
            self.installed = False
        elif command[1:3] == ['extensions', 'update']:
            self.installed_version = '0.6.0'
        return CommandResult(0, '', '')


@pytest.fixture
def gemini_050_runner() -> RecordingRunner:
    return RecordingRunner(client_version='0.50.0')


def gemini_adapter(runner: RecordingRunner) -> GeminiAdapter:
    return GeminiAdapter(runner=runner)


def project_direct_options(
    project: Path,
    *,
    config_root: Path | None = None,
    force_managed: bool = False,
) -> IntegrationOptions:
    return IntegrationOptions(
        scope=InstallScope.PROJECT,
        mode=InstallMode.DIRECT,
        config_root=config_root,
        project_root=project,
        command=None,
        force_managed=force_managed,
        config_root_explicit=config_root is not None,
    )


def user_direct_options(root: Path, memory: Path) -> IntegrationOptions:
    return IntegrationOptions(
        scope=InstallScope.USER,
        mode=InstallMode.DIRECT,
        config_root=root,
        project_root=None,
        command=str(memory),
        config_root_explicit=True,
    )


def native_options(
    home: Path,
    memory: Path | None = None,
    *,
    force_managed: bool = False,
) -> IntegrationOptions:
    return IntegrationOptions(
        scope=InstallScope.USER,
        mode=InstallMode.NATIVE,
        config_root=home / '.gemini',
        project_root=None,
        command=str(memory or Path(sys.executable)),
        force_managed=force_managed,
        config_root_explicit=False,
    )


def platform_echovault_config(home: Path) -> Path:
    if sys.platform == 'darwin':
        return home / 'Library' / 'Application Support' / 'echovault'
    if os.name == 'nt':
        return home / 'AppData' / 'Roaming' / 'echovault'
    return home / '.config' / 'echovault'


def seed_gemini_with_other_hook(tmp_path: Path) -> Path:
    root = tmp_path / '.gemini'
    root.mkdir()
    (root / 'settings.json').write_text(json.dumps({
        'hooks': {
            'BeforeAgent': [{
                'matcher': '*',
                'hooks': [{'name': 'other-hook', 'type': 'command', 'command': 'other'}],
            }],
        },
    }))
    return root


def named_hook(settings: dict[str, object], event: str, name: str) -> dict[str, object] | None:
    hooks = settings.get('hooks', {})
    if not isinstance(hooks, dict):
        return None
    groups = hooks.get(event, [])
    if not isinstance(groups, list):
        return None
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get('hooks'), list):
            continue
        for hook in group['hooks']:
            if isinstance(hook, dict) and hook.get('name') == name:
                return hook
    return None


def test_project_direct_always_installs_portable_mcp_and_supported_hook(tmp_path: Path, gemini_050_runner) -> None:
    project = tmp_path / 'repo'
    project.mkdir()
    result = gemini_adapter(gemini_050_runner).setup(project_direct_options(project))
    settings = json.loads((project / '.gemini/settings.json').read_text())
    assert settings['mcpServers']['echovault']['command'] == 'memory'
    assert settings['mcpServers']['echovault']['args'] == ['mcp', '--agent', 'gemini-cli']
    assert named_hook(settings, 'BeforeAgent', 'echovault-context') is not None
    assert (project / 'GEMINI.md').read_text().count('<!-- echovault:start -->') == 1
    assert (project / '.gemini/skills/echovault/SKILL.md').is_file()
    assert result.status == 'installed'


def test_user_direct_uses_absolute_command_and_preserves_unrelated_hooks(tmp_path: Path, fake_memory: Path, gemini_050_runner) -> None:
    root = seed_gemini_with_other_hook(tmp_path)
    gemini_adapter(gemini_050_runner).setup(user_direct_options(root, fake_memory))
    settings = json.loads((root / 'settings.json').read_text())
    assert settings['mcpServers']['echovault']['command'] == str(fake_memory)
    assert named_hook(settings, 'BeforeAgent', 'other-hook') is not None


def test_implicit_project_symlink_escape_is_rejected(tmp_path: Path, gemini_050_runner) -> None:
    project = tmp_path / 'repo'
    outside = tmp_path / 'outside'
    project.mkdir()
    outside.mkdir()
    (project / '.gemini').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ConfigBoundaryError):
        gemini_adapter(gemini_050_runner).setup(project_direct_options(project))
    assert not list(outside.iterdir())


def test_explicit_project_config_dir_is_the_only_escape_opt_in(
    tmp_path: Path, gemini_050_runner,
) -> None:
    project = tmp_path / 'repo'
    outside = tmp_path / 'outside'
    project.mkdir()
    outside.mkdir()
    result = gemini_adapter(gemini_050_runner).setup(
        project_direct_options(project, config_root=outside)
    )
    assert result.status == 'installed'
    assert (outside / 'settings.json').is_file()


def test_registry_exposes_gemini_only_after_adapter_exists(
) -> None:
    registered = get_adapter('gemini')
    assert isinstance(registered, GeminiAdapter)
    assert registered.integration_id == 'gemini'
    assert registered.agent == 'gemini-cli'
    assert registered.capabilities == AdapterCapabilities(
        mcp=True, rules=False, skills=True, extensions=True, hooks=True
    )
~~~

- [ ] **Step 2: Run direct tests and observe missing Gemini adapter**

Run: `uv run --extra dev pytest tests/test_gemini_integration.py -k direct -q`

Expected: GeminiAdapter is missing.

- [ ] **Step 3: Implement owned direct settings**

Place the shared helpers above in `tests/gemini_helpers.py` and import them before their first use in every Gemini test module. Reuse the sole `fake_memory` fixture produced by Phase 3 in `tests/conftest.py`. Register the production GeminiAdapter in `src/memory/integrations/registry.py` only in this task, after the class exists; the production-registration test reads that entry with `get_adapter` and never attempts a duplicate registration. Strict-merge only mcpServers.echovault and the named echovault-context BeforeAgent hook. Use exact marked blocks in GEMINI.md and the canonical skill file. Project direct always installs its hook when the detected client supports hooks, independent of N/U state. A client without hook capability gets static/MCP-only artifacts and a degraded warning; never select another scope silently.

Use absolute validated commands for U, portable memory for P, and exact --command overrides. Apply the same `validate_target_root` boundary rules as Cursor: implicit user/project symlinks may not escape the selected root, while an explicit `--config-dir` is the only opt-in and is reported by doctor. Repeated setup is byte-stable. Custom same-name MCP/hook or malformed settings is a conflict with no mutation. Setup never changes `MEMORY_HOME/config.yaml`, including an explicit `context.mode=off` policy.

- [ ] **Step 4: Run direct, config, and ownership tests**

Run: `uv run --extra dev pytest tests/test_gemini_integration.py -k direct tests/test_integration_config_io.py tests/test_integration_ownership.py -q`

Expected: U/P path, command, hook, preservation, degraded, conflict, and idempotency tests pass.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/integrations/gemini.py src/memory/integrations/registry.py tests/gemini_helpers.py tests/test_gemini_integration.py
git commit -m "feat: install gemini direct memory"
~~~

### Task 6: Native Extension Manager Integration

**Files:**
- Modify: src/memory/integrations/process.py
- Modify: src/memory/integrations/gemini.py
- Modify: tests/test_gemini_integration.py

**Interfaces:**
- Consumes: CommandResult, CommandRunner, SubprocessRunner from Phase 3
- Uses exact commands: validate PATH, install PATH, list, update echovault, uninstall echovault
- Verifies local installation metadata under ~/.gemini/extensions/echovault

- [ ] **Step 1: Write failing native install/update/uninstall runner tests**

~~~python
def test_native_install_validates_then_invokes_consent_prompt(tmp_path: Path, fake_memory: Path) -> None:
    runner = RecordingRunner(installed=False)
    adapter = gemini_adapter(runner)
    result = adapter.setup(native_options(tmp_path, fake_memory))
    source = platform_echovault_config(tmp_path) / 'integrations/gemini-extension/echovault'
    assert runner.argv[0] == ['gemini', 'extensions', 'validate', str(source)]
    assert runner.argv[1] == ['gemini', 'extensions', 'install', str(source)]
    assert '--consent' not in runner.argv[1]
    assert runner.calls[0]['capture_output'] is True
    assert runner.calls[1]['capture_output'] is False
    assert runner.calls[0]['cwd'] == source.parent.resolve()
    assert runner.calls[1]['cwd'] == source.parent.resolve()
    assert result.status == 'installed'


def test_native_owned_upgrade_uses_update(tmp_path: Path, fake_memory: Path) -> None:
    runner = RecordingRunner(installed=True, installed_version='0.5.0')
    result = gemini_adapter(runner).setup(native_options(tmp_path, fake_memory))
    assert ['gemini', 'extensions', 'update', 'echovault'] in runner.argv
    assert result.status == 'updated'


def test_native_owned_source_symlink_escape_is_rejected(
    tmp_path: Path, fake_memory: Path,
) -> None:
    source = platform_echovault_config(tmp_path) / 'integrations/gemini-extension/echovault'
    outside = tmp_path / 'outside'
    source.parent.mkdir(parents=True)
    outside.mkdir()
    source.symlink_to(outside, target_is_directory=True)
    runner = RecordingRunner(installed=False)
    with pytest.raises(ConfigBoundaryError):
        gemini_adapter(runner).setup(native_options(tmp_path, fake_memory))
    assert runner.argv == []
    assert not list(outside.iterdir())
~~~

- [ ] **Step 2: Run native tests and observe missing runner/manager**

Run: `uv run --extra dev pytest tests/test_gemini_integration.py -k native -q`

Expected: process abstraction/native mode is missing.

- [ ] **Step 3: Implement supported extension-manager calls**

Use the Phase 3 CommandRunner exactly, including `cwd` and `env`. Native validation/list/update calls use capture_output=True. The consent-bearing install call uses capture_output=False so stdin/stdout/stderr remain attached to Gemini. Execute manager calls with `cwd=source.parent.resolve()` and forward a copied caller environment; never construct a shell command.

Render machine-local source to the platform config root:

- Linux: XDG_CONFIG_HOME/echovault or ~/.config/echovault
- macOS: ~/Library/Application Support/echovault
- Windows: APPDATA/echovault

Validate every implicit target against its selected platform root, including symlinked intermediate directories. Stage and validate ownership through the journaled managed-tree primitive from Phase 3, then run `gemini extensions validate <source>`. Fresh install runs `gemini extensions install <source>` with inherited stdin/stdout so Gemini controls consent. Upgrade refreshes owned source and runs `gemini extensions update echovault`. Verify with `gemini extensions list` plus ~/.gemini/extensions/echovault/.gemini-extension-install.json; local installations must report type local and matching source/version. Never pass --consent or --skip-settings.

Uninstall runs `gemini extensions uninstall echovault`, verifies absence, then removes only an unmodified owned source tree.

- [ ] **Step 4: Run native manager tests**

Run: `uv run --extra dev pytest tests/test_gemini_integration.py -k native -q`

Expected: install/update/uninstall, consent, validation, version, command failure, and source ownership cases pass.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/integrations/process.py src/memory/integrations/gemini.py tests/test_gemini_integration.py
git commit -m "feat: manage gemini native extension"
~~~

### Task 7: Detect and Enforce Complete N/U/P Coexistence

**Files:**
- Modify: src/memory/integrations/gemini.py
- Modify: tests/test_gemini_integration.py
- Modify: tests/test_gemini_state.py

**Interfaces:**
- Produces: detect_gemini_state(options, runner) -> GeminiInstallationState
- Applies plan_gemini_transition before any mutation
- Reports project MCP shadowing and guarded duplicate hooks

- [ ] **Step 1: Parameterize filesystem/manager state transitions**

For every normative state row, build real isolated user/project config fixtures and a fake extension manager, invoke setup/uninstall, and assert the resulting N/U/P state. Include:

~~~python
from memory.projects import (
    ProjectRegistry,
    build_project_identity,
    discover_project_root,
)


@dataclass
class NPlusPFixture:
    runner: RecordingRunner
    home: Path
    project: Path
    memory_home: Path
    service_factory: SeededServiceFactory


@pytest.fixture
def n_plus_p_fixture(tmp_path: Path, fake_memory: Path) -> Iterator[NPlusPFixture]:
    home = tmp_path / 'home'
    project = tmp_path / 'repo'
    memory_home = tmp_path / 'memory-home'
    home.mkdir()
    project.mkdir()
    (project / 'package.json').write_text('{}', encoding='utf-8')
    project_root, marker = discover_project_root(project)
    scope = ProjectRegistry(memory_home).register(
        build_project_identity(project_root, marker)
    )
    service = MemoryService(str(memory_home))
    service.save(RawMemoryInput(
        title='Marker ALPHA-42',
        what='Durable marker ALPHA-42',
        category='context',
        source='cursor',
    ), project=scope.identity.key)
    runner = RecordingRunner()
    adapter = gemini_adapter(runner)
    adapter.setup(native_options(home, fake_memory))
    adapter.setup(project_direct_options(project))
    yield NPlusPFixture(
        runner=runner,
        home=home,
        project=project,
        memory_home=memory_home,
        service_factory=SeededServiceFactory(service),
    )
    service.db.close()


def project_has_hook_and_mcp(project: Path) -> bool:
    settings = json.loads((project / '.gemini/settings.json').read_text())
    return (
        'echovault' in settings.get('mcpServers', {})
        and named_hook(settings, 'BeforeAgent', 'echovault-context') is not None
    )


def finding(items: Sequence[DiagnosticFinding], code: str) -> DiagnosticFinding:
    return next(item for item in items if item.code == code)


def test_uninstall_native_leaves_project_direct_standalone(n_plus_p_fixture) -> None:
    adapter = gemini_adapter(n_plus_p_fixture.runner)
    adapter.uninstall(native_options(n_plus_p_fixture.home))
    state = adapter.detect(project_root=n_plus_p_fixture.project)
    assert state.native is ArtifactState.ABSENT
    assert state.project_direct is ArtifactState.OWNED
    assert project_has_hook_and_mcp(n_plus_p_fixture.project)


def test_project_mcp_shadows_native_but_duplicate_hooks_inject_once(n_plus_p_fixture) -> None:
    report = gemini_adapter(n_plus_p_fixture.runner).diagnose(project_direct_options(n_plus_p_fixture.project))
    assert finding(report, 'gemini.mcp.precedence').status == 'shadowed'
    event = {**VALID_EVENT, 'cwd': str(n_plus_p_fixture.project)}
    store = HookClaimStore(n_plus_p_fixture.memory_home)
    first = process_before_agent(
        event,
        service_factory=n_plus_p_fixture.service_factory,
        claim_store=store,
    )
    second = process_before_agent(
        event,
        service_factory=n_plus_p_fixture.service_factory,
        claim_store=store,
    )
    assert sum(response == {} for response in (first, second)) == 1
    contexts = [
        response['hookSpecificOutput']['additionalContext']
        for response in (first, second)
        if response
    ]
    assert len(contexts) == 1
    assert 'ALPHA-42' in contexts[0]


@pytest.mark.parametrize('target', [N, U, P])
@pytest.mark.parametrize('operation', ['setup', 'uninstall'])
def test_filesystem_force_matrix_is_scope_exact(
    tmp_path: Path,
    fake_memory: Path,
    target: GeminiTarget,
    operation: str,
) -> None:
    fixture = seed_modified_target(tmp_path, fake_memory, target)
    before_other_scopes = snapshot_other_gemini_scopes(fixture, target)
    invoke_target(fixture, target, operation, force_managed=True)
    assert snapshot_other_gemini_scopes(fixture, target) == before_other_scopes
~~~

Place `NPlusPFixture`, its fixture, `project_has_hook_and_mcp`, `seed_modified_target`, `snapshot_other_gemini_scopes`, and `invoke_target` in `tests/gemini_helpers.py`; test modules import them explicitly. The seed helper performs a normal owned install, edits exactly one manifest-claimed artifact, and returns its adapter/options/roots. The snapshot helper returns relative-path-to-bytes maps for N/U/P excluding the selected target. The invoke helper calls setup or uninstall with `dataclasses.replace(options, force_managed=...)`. Add adjacent rows proving no-force conflicts and that CUSTOM/MALFORMED targets and a modified other global scope remain byte-identical even with force.

- [ ] **Step 2: Run full transition tests and observe duplicate/conflict gaps**

Run: `uv run --extra dev pytest tests/test_gemini_state.py tests/test_gemini_integration.py -k "state or coexist or uninstall or shadow" -q`

Expected: at least one coexistence/uninstall/conflict assertion fails.

- [ ] **Step 3: Enforce transition-before-mutation**

Detect N from extension metadata/list plus owned source manifest, U/P from ownership/config state, and custom/modified/malformed artifacts explicitly. Call `plan_gemini_transition(..., force_managed=options.force_managed)` before staging or invoking a subprocess. N+U is unhealthy and blocks every setup until one global mode is uninstalled. P may coexist and its same-named settings MCP is effective by Gemini precedence. Both valid hooks remain installed and rely on event claims; setup never toggles them based on current global activation.

- [ ] **Step 4: Run state, integration, and hook suites**

Run: `uv run --extra dev pytest tests/test_gemini_state.py tests/test_gemini_integration.py tests/test_gemini_hook.py -q`

Expected: all N/U/P filesystem and runtime coexistence cases pass.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/integrations/gemini.py tests/test_gemini_integration.py tests/test_gemini_state.py
git commit -m "feat: enforce gemini integration coexistence"
~~~

### Task 8: Gemini CLI Commands and Read-Only Doctor

**Files:**
- Modify: src/memory/cli.py
- Modify: src/memory/setup.py
- Modify: src/memory/integrations/gemini.py
- Modify: src/memory/integrations/diagnostics.py
- Modify: src/memory/health.py
- Modify: tests/test_cli.py
- Modify: tests/test_setup.py
- Modify: tests/test_integration_diagnostics.py

**Interfaces:**
- setup gemini supports native, --direct, --project, --config-dir restrictions, --command, --force-managed
- uninstall gemini supports native, --direct, --project, --config-dir restrictions, --force-managed and rejects --command
- Produces: memory hook gemini before-agent
- doctor supports --agent gemini-cli

- [ ] **Step 1: Write failing exact CLI matrix**

~~~python
@dataclass
class CapturedAdapter:
    options: IntegrationOptions | None = None

    def setup(self, options: IntegrationOptions) -> IntegrationResult:
        self.options = options
        return IntegrationResult('installed', 'installed')

    def uninstall(self, options: IntegrationOptions) -> IntegrationResult:
        self.options = options
        return IntegrationResult('removed', 'removed')


def install_fake_adapter(monkeypatch) -> CapturedAdapter:
    captured = CapturedAdapter()
    monkeypatch.setattr('memory.cli.get_adapter', lambda name: captured)
    return captured


@pytest.mark.parametrize(
    ('argv', 'expected_mode', 'expected_scope'),
    [
        (['setup', 'gemini'], 'native', 'user'),
        (['setup', 'gemini', '--direct'], 'direct', 'user'),
        (['setup', 'gemini', '--project'], 'direct', 'project'),
        (['setup', 'gemini', '--project', '--direct'], 'direct', 'project'),
    ],
)
def test_gemini_setup_cli_matrix(argv, expected_mode, expected_scope, monkeypatch) -> None:
    captured = install_fake_adapter(monkeypatch)
    result = CliRunner().invoke(main, argv)
    assert result.exit_code == 0
    assert captured.options.mode.value == expected_mode
    assert captured.options.scope.value == expected_scope


@pytest.mark.parametrize(
    'argv',
    [
        ['uninstall', 'gemini'],
        ['uninstall', 'gemini', '--direct'],
        ['uninstall', 'gemini', '--project'],
        ['uninstall', 'gemini', '--project', '--direct'],
    ],
)
def test_gemini_uninstall_cli_matrix(argv: list[str], monkeypatch) -> None:
    captured = install_fake_adapter(monkeypatch)
    result = CliRunner().invoke(main, argv)
    assert result.exit_code == 0
    assert captured.options is not None
    assert captured.options.scope is (
        InstallScope.PROJECT if '--project' in argv else InstallScope.USER
    )
    assert captured.options.mode is (
        InstallMode.DIRECT if '--direct' in argv or '--project' in argv else InstallMode.NATIVE
    )


@pytest.mark.parametrize('verb', ['setup', 'uninstall'])
def test_native_config_dir_is_usage_error(verb: str) -> None:
    result = CliRunner().invoke(main, [verb, 'gemini', '--config-dir', '/tmp/.gemini'])
    assert result.exit_code == 2
    assert '--config-dir requires --direct or --project' in result.output


@pytest.mark.parametrize('verb', ['setup', 'uninstall'])
@pytest.mark.parametrize('scope_args', [[], ['--direct'], ['--project']])
def test_force_managed_reaches_every_gemini_variant(
    verb: str, scope_args: list[str], monkeypatch,
) -> None:
    captured = install_fake_adapter(monkeypatch)
    result = CliRunner().invoke(main, [verb, 'gemini', *scope_args, '--force-managed'])
    assert result.exit_code == 0
    assert captured.options is not None
    assert captured.options.force_managed is True


@pytest.mark.parametrize('scope_args', [[], ['--direct'], ['--project']])
def test_command_is_setup_only(scope_args: list[str], monkeypatch) -> None:
    captured = install_fake_adapter(monkeypatch)
    setup = CliRunner().invoke(
        main,
        ['setup', 'gemini', *scope_args, '--command', '/opt/echovault/bin/memory'],
    )
    assert setup.exit_code == 0
    assert captured.options is not None
    assert captured.options.command == '/opt/echovault/bin/memory'
    uninstall = CliRunner().invoke(
        main,
        ['uninstall', 'gemini', *scope_args, '--command', '/opt/echovault/bin/memory'],
    )
    assert uninstall.exit_code == 2
    assert 'No such option: --command' in uninstall.output


@pytest.mark.parametrize('verb', ['setup', 'uninstall'])
@pytest.mark.parametrize('scope_args', [['--direct'], ['--project']])
def test_config_dir_is_valid_only_for_direct_targets(
    verb: str, scope_args: list[str], tmp_path: Path, monkeypatch,
) -> None:
    captured = install_fake_adapter(monkeypatch)
    target = tmp_path / '.gemini'
    result = CliRunner().invoke(
        main,
        [verb, 'gemini', *scope_args, '--config-dir', str(target)],
    )
    assert result.exit_code == 0
    assert captured.options is not None
    assert captured.options.config_root == target.resolve()
    assert captured.options.config_root_explicit is True


def test_hook_command_always_writes_one_json_document(monkeypatch) -> None:
    monkeypatch.setattr('memory.integrations.gemini_hook.handle_before_agent', lambda payload: {})
    result = CliRunner().invoke(main, ['hook', 'gemini', 'before-agent'], input=json.dumps(VALID_EVENT))
    assert result.exit_code == 0
    assert json.loads(result.output) == {}
~~~

- [ ] **Step 2: Run CLI/doctor tests and observe absent Gemini commands**

Run: `uv run --extra dev pytest tests/test_cli.py tests/test_setup.py tests/test_integration_diagnostics.py -k gemini -q`

Expected: Click reports no such command or missing options.

- [ ] **Step 3: Wire exact commands and diagnostic statuses**

Native setup and uninstall reject --config-dir. Project implies direct. Direct config-dir is interpreted as the target .gemini directory. All setup variants accept --command and --force-managed; every uninstall variant accepts --force-managed and rejects --command. Pass `force_managed` unchanged to the adapter/planner.

Doctor reports version/capability, N/U/P state, enabled/disabled native state, selected/shadowed MCP, static context/skill/hook, ownership hashes, four MCP tools, project key/aliases, claim directory health, hook support, local/remote query mode, and the effective context mode plus precedence source. It remains read-only and returns ok/degraded/warning/unhealthy/shadowed findings without running manager updates or repairs. With an effective `gemini-cli: off`, doctor reports disabled while the BeforeAgent hook returns `{}` without claim or feedback.

- [ ] **Step 4: Run Gemini CLI, setup, doctor, and hook tests**

Run: `uv run --extra dev pytest tests/test_cli.py tests/test_setup.py tests/test_integration_diagnostics.py tests/test_gemini_hook.py tests/test_gemini_integration.py -q`

Expected: command matrix, native restrictions, JSON stdout, diagnostics, and wrappers pass.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/cli.py src/memory/setup.py src/memory/integrations/gemini.py src/memory/integrations/diagnostics.py src/memory/health.py tests/test_cli.py tests/test_setup.py tests/test_integration_diagnostics.py
git commit -m "feat: expose gemini memory commands"
~~~

### Task 9: Gemini Deterministic Contract Gate

**Files:**
- Modify: tests/test_gemini_integration.py
- Modify: tests/test_gemini_hook.py
- Modify: README.md
- Modify: CHANGELOG.md

**Interfaces:**
- Verifies native/direct isolated homes without a real account
- Verifies packaged source shape before Phase 5 wheel tests
- Documents exact deterministic and degraded boundaries

- [ ] **Step 1: Add isolated-home lifecycle test**

~~~python
def test_gemini_project_lifecycle_is_standalone_after_global_uninstall(
    n_plus_p_fixture: NPlusPFixture,
) -> None:
    adapter = gemini_adapter(n_plus_p_fixture.runner)
    adapter.uninstall(native_options(n_plus_p_fixture.home))
    event = {**VALID_EVENT, 'cwd': str(n_plus_p_fixture.project)}
    response = process_before_agent(
        event,
        service_factory=n_plus_p_fixture.service_factory,
        claim_store=HookClaimStore(n_plus_p_fixture.memory_home),
    )
    assert response['hookSpecificOutput']['hookEventName'] == 'BeforeAgent'
    assert 'additionalContext' in response['hookSpecificOutput']
    assert project_has_hook_and_mcp(n_plus_p_fixture.project)
~~~

- [ ] **Step 2: Run the full Gemini phase gate**

Run: `uv run --extra dev pytest tests/test_gemini_state.py tests/test_gemini_assets.py tests/test_gemini_hook.py tests/test_gemini_integration.py tests/test_integration_diagnostics.py -q`

Expected: all deterministic Gemini contracts pass without network/model credentials.

- [ ] **Step 3: Document exact Gemini operations**

README covers native, user-direct, project-direct, update/no-op behavior, uninstalls, N/U conflicts, P coexistence, restarts, doctor, hook privacy, remote embedding opt-in, and degraded clients. CHANGELOG records the new integration without claiming authenticated client verification.

- [ ] **Step 4: Run baseline and inspect changes**

Run: `uv run --extra dev pytest -q`

Expected: full Python suite passes.

Run: `git diff --check`

Expected: no whitespace errors.

- [ ] **Step 5: Commit**

~~~bash
git add tests/test_gemini_integration.py tests/test_gemini_hook.py README.md CHANGELOG.md
git commit -m "docs: document gemini memory integration"
~~~

## Phase Completion Gate

Run:

~~~bash
uv run --extra dev pytest -q
memory setup gemini --help
memory uninstall gemini --help
memory hook gemini before-agent --help
memory doctor --agent gemini-cli
git status --short
~~~

Expected: all tests pass, help matches the approved command/state matrix, doctor writes nothing, duplicate hook execution yields one injection, and native install/update/uninstall commands match the official Gemini extension manager contract.
