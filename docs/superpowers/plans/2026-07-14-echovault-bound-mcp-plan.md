# EchoVault Bound MCP and Retrieval Privacy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose four project-isolated EchoVault tools through agent/project-bound MCP servers while preserving unbound legacy behavior and local-first query privacy.

**Architecture:** Resolve one ProjectScope per tool call from an authoritative server binding, current MCP Roots, and a validated cwd. Keep handler formatting thin; MemoryService owns alias-aware retrieval and canonical persistence from Phase 1.

**Tech Stack:** Python 3.10–3.14, MCP Python SDK 1.x, Click, anyio in-memory MCP streams, SQLite/FTS5/sqlite-vec, pytest.

## Global Constraints

- Complete the Local Core and Canonical Storage plan before this plan.
- Preserve `memory mcp` with no flags and all existing unbound tool inputs.
- Bound identity always wins; conflicting agent, source, project, or path input is rejected, not silently replaced.
- All four tools are constrained to the effective hashed project key plus adopted aliases.
- Cross-project details must look identical to not found and must record no feedback.
- Resolve MCP Roots per request; zero or multiple roots degrade explicitly unless an authoritative root or valid cwd resolves the scope.
- Caller cwd must remain inside the negotiated/startup boundary; hook cwd is handled separately by Gemini.
- The bound agent selects context policy but never filters memories by creator source.
- Agent-bound save requires one UUID idempotency key; unbound save keeps it optional.
- Prompt/query text is never stored or logged.
- Remote query embeddings are opt-in and receive only a redacted copy; local FTS still receives the original query.
- Keep Cursor/Gemini source values canonical: `cursor` and `gemini-cli`.
- Begin each behavior change with an observed failing test.

---

## File Responsibility Map

- **Create: src/memory/mcp_authority.py** — binding, identity validation, URI/root conversion, boundary enforcement, and scope resolution.
- **Create: tests/mcp_helpers.py** — reusable in-memory real-protocol client and result decoder.
- **Modify: src/memory/projects.py** — make multiple MCP Roots disambiguatable only by a cwd inside exactly one root.
- **Modify: src/memory/mcp_server.py** — dynamic four-tool schemas, per-request Roots, thin bound handlers.
- **Modify: src/memory/cli.py** — mcp agent/project-root flags and environment support.
- **Modify: src/memory/config.py** — remote query embedding opt-in.
- **Modify: src/memory/embeddings/base.py** — local/remote provider capability.
- **Modify: src/memory/embeddings/ollama.py** and **openai_embed.py** — capability values.
- **Modify: src/memory/core.py** — ProjectScope-aware retrieval, feedback flag, and split FTS/embedding queries.
- **Modify: src/memory/db.py** — alias-aware filters and authorized unique-prefix details.
- **Modify: src/memory/health.py** — binding/root/privacy diagnostics.
- **Create tests:** tests/test_mcp_authority.py, tests/test_mcp_protocol.py, tests/test_embedding_privacy.py.
- **Modify tests:** tests/test_mcp_server.py, tests/test_cli.py, tests/test_core.py, tests/test_db.py, tests/test_remote_embeddings.py.

### Task 1: Alias-Aware Query Scope and Authorized Details

**Files:**
- Modify: src/memory/db.py
- Modify: src/memory/core.py
- Modify: tests/test_db.py
- Modify: tests/test_core.py

**Interfaces:**
- Changes project filters to accept ProjectScope or a legacy string
- Produces: AmbiguousMemoryIdError and InvalidMemoryIdPrefix
- Produces: MemoryDB.get_details(memory_id, *, projects=None, record_feedback=True)
- Produces: MemoryService.get_details(memory_id, *, project=None, record_feedback=True)
- Changes: MemoryService.close() is idempotent and closes MemoryDB exactly once

- [ ] **Step 1: Write failing alias and negative-isolation tests**

Add `memory_service` and `project_scope` before the new tests in `tests/test_core.py`, and add `insert_detail_row` before the new tests in `tests/test_db.py`; include the imports shown by the referenced types and do not rely on fixtures introduced in a later phase:

~~~python
from pathlib import Path

import pytest

from memory.core import MemoryService
from memory.db import AmbiguousMemoryIdError, InvalidMemoryIdPrefix, MemoryDB
from memory.models import RawMemoryInput
from memory.projects import ProjectIdentity, ProjectScope


@pytest.fixture
def memory_service(env_home: Path):
    instance = MemoryService(str(env_home))
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def project_scope(tmp_path: Path) -> ProjectScope:
    root = tmp_path / 'workspace'
    root.mkdir()
    identity = ProjectIdentity(
        root=root.resolve(),
        display_name='workspace',
        key='workspace--111111111111',
        marker=None,
        canonical_owner=root.resolve(),
    )
    return ProjectScope(identity=identity, aliases=('workspace',))


def insert_detail_row(db: MemoryDB, *, memory_id: str, project: str = 'p') -> None:
    db.conn.execute(
        '''
        INSERT INTO memories
            (id, title, what, project, file_path, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ''',
        (memory_id, memory_id, 'memory', project, 'fixture.md', '2026-07-14', '2026-07-14'),
    )
    db.conn.execute(
        'INSERT INTO memory_details (memory_id, body) VALUES (?, ?)',
        (memory_id, 'details'),
    )
    db.conn.commit()


def test_alias_scope_reads_hashed_and_adopted_legacy_rows(memory_service: MemoryService, project_scope: ProjectScope) -> None:
    memory_service.save(
        RawMemoryInput(title='Hashed', what='alias hashed marker'),
        project=project_scope.identity.key,
    )
    memory_service.save(
        RawMemoryInput(title='Legacy', what='alias legacy marker'),
        project=project_scope.aliases[0],
    )
    titles = {row['title'] for row in memory_service.search('alias marker', project=project_scope)}
    assert titles == {'Hashed', 'Legacy'}


def test_details_outside_scope_is_not_found_without_feedback(
    memory_service: MemoryService,
    project_scope: ProjectScope,
) -> None:
    saved = memory_service.save(RawMemoryInput(title='Private', what='other project', details='secret detail'), project='other--1')
    before = memory_service.get_memory_record(saved['id'])['details_opened_count']
    detail = memory_service.get_details(saved['id'], project=project_scope)
    after = memory_service.get_memory_record(saved['id'])['details_opened_count']
    assert detail is None
    assert after == before


def test_ambiguous_prefix_is_rejected(db: MemoryDB) -> None:
    for memory_id in ('abc111', 'abc222'):
        insert_detail_row(db, memory_id=memory_id)
    with pytest.raises(AmbiguousMemoryIdError):
        db.get_details('abc', projects=('p',))


@pytest.mark.parametrize('prefix, expected_id', [('abc%', 'abc%111'), ('abc_', 'abc_111')])
def test_details_prefix_treats_like_metacharacters_literally(
    db: MemoryDB,
    prefix: str,
    expected_id: str,
) -> None:
    for memory_id in ('abc%111', 'abcx222', 'abc_111', 'abcy222'):
        insert_detail_row(db, memory_id=memory_id)
    detail = db.get_details(prefix, projects=('p',))
    assert detail is not None
    assert detail.memory_id == expected_id


def test_details_rejects_empty_prefix_before_querying(db: MemoryDB) -> None:
    insert_detail_row(db, memory_id='abc111')
    with pytest.raises(InvalidMemoryIdPrefix, match='must not be empty'):
        db.get_details('', projects=('p',))


def test_memory_service_close_closes_database_exactly_once(
    env_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MemoryService(str(env_home))
    original_close = service.db.close
    close_calls = 0

    def recording_close() -> None:
        nonlocal close_calls
        close_calls += 1
        original_close()

    monkeypatch.setattr(service.db, 'close', recording_close)
    try:
        service.close()
        service.close()
        assert close_calls == 1
    finally:
        service.close()
~~~

- [ ] **Step 2: Run focused DB/core tests**

Run: `uv run --extra dev pytest tests/test_db.py tests/test_core.py -k "alias_scope or outside_scope or ambiguous_prefix or prefix_treats or empty_prefix or close_closes_database" -q`

Expected: ProjectScope filters are unsupported, wildcard prefixes match more than one row, the empty prefix is accepted, and the close regression observes two `MemoryDB.close()` calls instead of one after calling `service.close()` twice.

- [ ] **Step 3: Centralize project-key SQL and feedback timing**

Add one helper that turns None, a string, or ProjectScope.storage_keys into a parameterized SQL `IN` clause. Apply it to FTS search, vector search, recent/list/count, memory lookup, and details. Never interpolate project values.

~~~python
class AmbiguousMemoryIdError(ValueError):
    pass


class InvalidMemoryIdPrefix(ValueError):
    pass


def _literal_like_prefix(memory_id: str) -> str:
    if not memory_id:
        raise InvalidMemoryIdPrefix('Memory ID or prefix must not be empty')
    escaped = memory_id.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return escaped + '%'


def get_details(
    self,
    memory_id: str,
    *,
    projects: tuple[str, ...] | None = None,
    record_feedback: bool = True,
) -> MemoryDetail | None:
    sql = (
        'SELECT m.id, d.body FROM memories m '
        "JOIN memory_details d ON d.memory_id = m.id WHERE m.id LIKE ? ESCAPE '\\'"
    )
    params: list[object] = [_literal_like_prefix(memory_id)]
    if projects:
        placeholders = ','.join('?' for _ in projects)
        sql += f' AND m.project IN ({placeholders})'
        params.extend(projects)
    rows = self.conn.execute(sql + ' ORDER BY m.id LIMIT 2', params).fetchall()
    if len(rows) > 1:
        raise AmbiguousMemoryIdError(f'Ambiguous memory prefix: {memory_id}')
    if not rows:
        return None
    if record_feedback:
        self.record_feedback([rows[0]['id']], 'details_opened')
    return MemoryDetail(memory_id=rows[0]['id'], body=rows[0]['body'])


# In MemoryService.__init__, immediately after creating self.db:
self._closed = False


def close(self) -> None:
    """Close owned resources once; safe from every server cleanup path."""
    if self._closed:
        return
    self.db.close()
    self._closed = True
~~~

Keep unscoped CLI/dashboard reads compatible by passing `projects=None`. The project-key helper must map a `ProjectScope` to `scope.storage_keys`, a legacy string to a one-element tuple, and `None` to no filter. It is a read-only helper: no mutation path may write to an alias. Record details feedback only after the scoped query returns exactly one row; ambiguous, invalid, cross-project, and missing lookups record nothing. Set `_closed` only after `MemoryDB.close()` succeeds, so a failed close remains retryable; all fixture, worker, and server-finally callers may then call `service.close()` safely.

- [ ] **Step 4: Run targeted and full suites**

Run: `uv run --extra dev pytest tests/test_db.py tests/test_core.py tests/test_search.py -q`

Expected: legacy string filters and new alias scopes both pass.

Run: `uv run --extra dev pytest -q`

Expected: full suite passes.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/db.py src/memory/core.py tests/test_db.py tests/test_core.py
git commit -m "feat: scope memory queries to project aliases"
~~~

### Task 2: Pure MCP Binding and Authority Rules

**Files:**
- Modify: src/memory/projects.py
- Create: src/memory/mcp_authority.py
- Modify: tests/test_projects.py
- Create: tests/test_mcp_authority.py

**Interfaces:**
- Produces: MCPServerBinding
- Produces: resolve_bound_identity
- Produces: public_project_error(error) and fixed ROOTS_ERROR/BOUNDARY_ERROR/PROJECT_ERROR/AUTHORITY_ERROR wire constants
- Produces: file_uri_to_path and ensure_path_within
- Changes: select_project_candidate(ProjectCandidates) disambiguates many Roots only with cwd inside exactly one
- Produces: resolve_project_scope(binding, registry, *, client_roots, cwd, requested_project) -> ProjectScope | str | None

- [ ] **Step 1: Write failing candidate-selection and authority tests**

First extend `tests/test_projects.py` with the corrected multi-root contract. Define the helper before its tests:

~~~python
def marked_workspace(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / 'package.json').write_text('{}', encoding='utf-8')
    return path


def test_multiple_mcp_roots_accept_cwd_inside_exactly_one(tmp_path: Path) -> None:
    left = marked_workspace(tmp_path / 'left')
    right = marked_workspace(tmp_path / 'right')
    nested = right / 'src'
    nested.mkdir()
    selected = select_project_candidate(
        ProjectCandidates(mcp_roots=(left, right), protocol_cwd=nested)
    )
    assert selected == nested.resolve()


@pytest.mark.parametrize('cwd_kind', ['missing', 'outside', 'overlap'])
def test_multiple_mcp_roots_require_one_unambiguous_cwd(tmp_path: Path, cwd_kind: str) -> None:
    outer = marked_workspace(tmp_path / 'outer')
    nested_root = marked_workspace(outer / 'nested')
    other = marked_workspace(tmp_path / 'other')
    cwd = {
        'missing': None,
        'outside': tmp_path / 'outside',
        'overlap': nested_root / 'src',
    }[cwd_kind]
    if cwd is not None:
        cwd.mkdir(parents=True, exist_ok=True)
    roots = (outer, other) if cwd_kind != 'overlap' else (outer, nested_root)
    with pytest.raises(MultiRootError, match='exactly one'):
        select_project_candidate(ProjectCandidates(mcp_roots=roots, protocol_cwd=cwd))
~~~

Then add `tests/test_mcp_authority.py`. The tests use real `ProjectRegistry` state; no undefined registry fixture is assumed:

~~~python
from pathlib import Path

import pytest

from memory.mcp_authority import (
    AUTHORITY_ERROR,
    AuthorityConflict,
    BOUNDARY_ERROR,
    MCPServerBinding,
    PROJECT_ERROR,
    ROOTS_ERROR,
    ensure_path_within,
    file_uri_to_path,
    public_project_error,
    resolve_bound_identity,
    resolve_project_scope,
)
from memory.projects import (
    MultiRootError,
    ProjectIdentity,
    ProjectRegistry,
    ProjectResolutionError,
    ProjectScope,
    build_project_identity,
    discover_project_root,
)


def marked_workspace(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / 'package.json').write_text('{}', encoding='utf-8')
    return path


def new_registry(tmp_path: Path) -> ProjectRegistry:
    return ProjectRegistry(tmp_path / 'memory-home')


def test_bound_identity_accepts_omitted_or_matching_and_rejects_spoof() -> None:
    assert resolve_bound_identity('cursor', None, field_name='source') == 'cursor'
    assert resolve_bound_identity('cursor', 'cursor', field_name='source') == 'cursor'
    with pytest.raises(AuthorityConflict) as raised:
        resolve_bound_identity('cursor', 'gemini-cli', field_name='source')
    assert public_project_error(raised.value) == AUTHORITY_ERROR


def test_public_project_error_ignores_low_level_text_and_paths(tmp_path: Path) -> None:
    private = tmp_path / 'private-repository'
    cases = (
        (AuthorityConflict('boundary', f'escaped through {private}'), BOUNDARY_ERROR),
        (AuthorityConflict('project', f'alias registry at {private}'), PROJECT_ERROR),
        (AuthorityConflict('authority', f'bound source details at {private}'), AUTHORITY_ERROR),
        (MultiRootError(f'candidate roots include {private}'), ROOTS_ERROR),
        (ProjectResolutionError(f'invalid gitdir pointer {private}'), ROOTS_ERROR),
    )
    for error, expected in cases:
        public = public_project_error(error)
        assert public == expected
        assert str(private) not in public
        assert 'gitdir' not in public
        assert 'candidate roots' not in public


def test_cwd_must_remain_inside_client_root(tmp_path: Path) -> None:
    root = tmp_path / 'repo'
    root.mkdir()
    assert ensure_path_within(root / 'src', root) == (root / 'src').resolve()
    with pytest.raises(AuthorityConflict) as raised:
        ensure_path_within(tmp_path / 'other', root)
    public = public_project_error(raised.value)
    assert public == BOUNDARY_ERROR
    assert str(root) not in public
    assert str(tmp_path / 'other') not in public


def test_file_uri_decodes_spaces_and_rejects_non_file_scheme(tmp_path: Path) -> None:
    assert file_uri_to_path((tmp_path / 'my repo').as_uri()) == (tmp_path / 'my repo').resolve()
    with pytest.raises(ProjectResolutionError) as raised:
        file_uri_to_path('https://example.test/repo')
    assert public_project_error(raised.value) == ROOTS_ERROR


def test_legacy_unbound_scope_returns_original_project_without_registry_write(tmp_path: Path) -> None:
    class RegistryMustNotBeCalled:
        def register(self, identity: ProjectIdentity) -> ProjectScope:
            raise AssertionError('legacy unbound resolution must not fabricate a project path')

    binding = MCPServerBinding(agent=None, project_root=None, startup_cwd=tmp_path)
    registry = RegistryMustNotBeCalled()
    assert resolve_project_scope(
        binding,
        registry,
        client_roots=(tmp_path / 'ignored',),
        cwd=tmp_path / 'ignored',
        requested_project='legacy-project',
    ) == 'legacy-project'
    assert resolve_project_scope(
        binding,
        registry,
        client_roots=(),
        cwd=None,
        requested_project=None,
    ) is None


def test_project_bound_root_is_authoritative_and_accepts_key_or_alias(tmp_path: Path) -> None:
    root = marked_workspace(tmp_path / 'repo')
    nested = root / 'src'
    nested.mkdir()
    registry = new_registry(tmp_path)
    identity = build_project_identity(*discover_project_root(root))
    registry.register(identity)
    scope_with_alias = registry.adopt_legacy('legacy-repo', identity)
    binding = MCPServerBinding(agent='cursor', project_root=root, startup_cwd=tmp_path)

    for requested in (None, scope_with_alias.identity.key, 'legacy-repo'):
        scope = resolve_project_scope(
            binding,
            registry,
            client_roots=(root,),
            cwd=nested,
            requested_project=requested,
        )
        assert isinstance(scope, ProjectScope)
        assert scope.identity.key == scope_with_alias.identity.key
        assert 'legacy-repo' in scope.storage_keys

    with pytest.raises(AuthorityConflict) as raised:
        resolve_project_scope(
            binding,
            registry,
            client_roots=(root,),
            cwd=nested,
            requested_project='other-project',
        )
    assert public_project_error(raised.value) == PROJECT_ERROR


def test_project_bound_server_rejects_conflicting_root_without_disclosing_paths(tmp_path: Path) -> None:
    root = marked_workspace(tmp_path / 'repo')
    other = marked_workspace(tmp_path / 'private-other')
    binding = MCPServerBinding(agent='cursor', project_root=root, startup_cwd=tmp_path)
    with pytest.raises(AuthorityConflict) as raised:
        resolve_project_scope(
            binding,
            new_registry(tmp_path),
            client_roots=(other,),
            cwd=root,
            requested_project=None,
        )
    message = public_project_error(raised.value)
    assert message == BOUNDARY_ERROR
    assert 'private-other' not in message
    assert str(root) not in message


def test_global_bound_server_uses_cwd_to_select_one_of_many_roots(tmp_path: Path) -> None:
    left = marked_workspace(tmp_path / 'left')
    right = marked_workspace(tmp_path / 'right')
    nested = right / 'src'
    nested.mkdir()
    binding = MCPServerBinding(agent='gemini-cli', project_root=None, startup_cwd=tmp_path)
    scope = resolve_project_scope(
        binding,
        new_registry(tmp_path),
        client_roots=(left, right),
        cwd=nested,
        requested_project=None,
    )
    assert isinstance(scope, ProjectScope)
    assert scope.identity.key == build_project_identity(*discover_project_root(right)).key


def test_global_bound_server_rejects_many_roots_without_unique_cwd(tmp_path: Path) -> None:
    left = marked_workspace(tmp_path / 'left')
    right = marked_workspace(tmp_path / 'right')
    binding = MCPServerBinding(agent='cursor', project_root=None, startup_cwd=tmp_path)
    with pytest.raises(MultiRootError) as raised:
        resolve_project_scope(
            binding,
            new_registry(tmp_path),
            client_roots=(left, right),
            cwd=None,
            requested_project=None,
        )
    assert public_project_error(raised.value) == ROOTS_ERROR


def test_global_bound_server_without_roots_uses_marked_startup_boundary(tmp_path: Path) -> None:
    root = marked_workspace(tmp_path / 'repo')
    startup = root / 'tools'
    startup.mkdir()
    cwd = root / 'src'
    cwd.mkdir()
    scope = resolve_project_scope(
        MCPServerBinding(agent='cursor', project_root=None, startup_cwd=startup),
        new_registry(tmp_path),
        client_roots=(),
        cwd=cwd,
        requested_project=None,
    )
    assert isinstance(scope, ProjectScope)
    assert scope.identity.key == build_project_identity(*discover_project_root(root)).key


def test_global_bound_server_without_roots_uses_marked_protocol_cwd_from_generic_startup(
    tmp_path: Path,
) -> None:
    generic = tmp_path / 'generic-home'
    project = marked_workspace(tmp_path / 'workspace')
    cwd = project / 'src'
    generic.mkdir()
    cwd.mkdir()
    binding = MCPServerBinding(agent='cursor', project_root=None, startup_cwd=generic)
    scope = resolve_project_scope(
        binding,
        new_registry(tmp_path),
        client_roots=(),
        cwd=cwd,
        requested_project=None,
    )
    assert isinstance(scope, ProjectScope)
    assert scope.identity.key == build_project_identity(*discover_project_root(project)).key
    with pytest.raises(ProjectResolutionError) as raised:
        resolve_project_scope(
            binding,
            new_registry(tmp_path),
            client_roots=(),
            cwd=generic,
            requested_project=None,
        )
    assert public_project_error(raised.value) == ROOTS_ERROR


def test_symlink_cwd_cannot_escape_authorized_root(tmp_path: Path) -> None:
    root = marked_workspace(tmp_path / 'repo')
    outside = tmp_path / 'secret-location'
    outside.mkdir()
    link = root / 'linked'
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('host does not permit test symlink creation')
    binding = MCPServerBinding(agent='cursor', project_root=root, startup_cwd=root)
    with pytest.raises(AuthorityConflict) as raised:
        resolve_project_scope(
            binding,
            new_registry(tmp_path),
            client_roots=(root,),
            cwd=link,
            requested_project=None,
        )
    public = public_project_error(raised.value)
    assert public == BOUNDARY_ERROR
    assert 'secret-location' not in public
    assert str(root) not in public
~~~

- [ ] **Step 2: Run tests and observe the old multi-root contract and missing authority module**

Run: `uv run --extra dev pytest tests/test_projects.py tests/test_mcp_authority.py -k "multiple_mcp_roots or bound or symlink or legacy_unbound or public_project_error" -q`

Expected: `tests/test_mcp_authority.py` cannot import the new module or fixed mapper; if run independently after scaffolding, the core selector rejects every many-root input before considering cwd.

- [ ] **Step 3: Correct candidate selection, then implement binding and complete scope resolution**

Replace the Phase-1 selector body with this contract. `_canonical` resolves symlinks, and `_is_within` compares canonical paths:

~~~python
def _is_within(path: Path, boundary: Path) -> bool:
    try:
        _canonical(path).relative_to(_canonical(boundary))
    except ValueError:
        return False
    return True


def select_project_candidate(candidates: ProjectCandidates) -> Path:
    if candidates.explicit_root is not None:
        return _canonical(candidates.explicit_root)

    roots = tuple(_canonical(root) for root in candidates.mcp_roots)
    if len(roots) == 1:
        return roots[0]
    if len(roots) > 1:
        if candidates.protocol_cwd is None:
            raise MultiRootError('Multiple MCP roots require cwd inside exactly one root')
        cwd = _canonical(candidates.protocol_cwd)
        containing = tuple(root for root in roots if _is_within(cwd, root))
        if len(containing) != 1:
            raise MultiRootError('Multiple MCP roots require cwd inside exactly one root')
        return cwd

    if candidates.protocol_cwd is not None:
        return _canonical(candidates.protocol_cwd)
    if candidates.startup_cwd is not None:
        return _canonical(candidates.startup_cwd)
    return _canonical(Path.cwd())
~~~

Implement `mcp_authority.py` with path-free public errors and this exact return boundary:

~~~python
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence
from urllib.parse import unquote, urlparse

from memory.projects import (
    MultiRootError,
    ProjectCandidates,
    ProjectRegistry,
    ProjectResolutionError,
    ProjectScope,
    _is_within,
    build_project_identity,
    discover_project_root,
    select_project_candidate,
)


BOUNDARY_ERROR = 'Request path is outside the authorized project boundary'
ROOTS_ERROR = 'MCP roots do not resolve to one authorized project'
PROJECT_ERROR = 'Requested project does not match the authorized project'
AUTHORITY_ERROR = 'Request conflicts with the bound MCP authority'

AuthorityKind = Literal['boundary', 'project', 'authority']


class AuthorityConflict(ProjectResolutionError):
    def __init__(self, kind: AuthorityKind, internal_message: str) -> None:
        self.kind = kind
        super().__init__(internal_message)


def public_project_error(error: ProjectResolutionError) -> str:
    """Map internal resolution failures to a fixed path-free wire message."""
    if isinstance(error, AuthorityConflict):
        if error.kind == 'boundary':
            return BOUNDARY_ERROR
        if error.kind == 'project':
            return PROJECT_ERROR
        return AUTHORITY_ERROR
    return ROOTS_ERROR


@dataclass(frozen=True)
class MCPServerBinding:
    agent: str | None
    project_root: Path | None
    startup_cwd: Path

    @property
    def is_bound(self) -> bool:
        return self.agent is not None or self.project_root is not None


def resolve_bound_identity(
    bound_agent: str | None,
    requested: str | None,
    *,
    field_name: Literal['agent', 'source'],
) -> str | None:
    if bound_agent is None:
        return requested
    if requested is not None and requested != bound_agent:
        raise AuthorityConflict(
            'authority',
            f'Conflicting {field_name}: server is bound to {bound_agent}',
        )
    return bound_agent


def file_uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != 'file' or parsed.netloc not in ('', 'localhost'):
        raise ProjectResolutionError('MCP root URI must be a local file URI')
    path = unquote(parsed.path)
    if os.name == 'nt' and path.startswith('/') and len(path) > 2 and path[2] == ':':
        path = path[1:]
    return Path(path).resolve()


def ensure_path_within(path: Path, boundary: Path) -> Path:
    resolved = path.resolve()
    root = boundary.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise AuthorityConflict('boundary', 'Resolved path escapes authority boundary') from error
    return resolved


def _register_candidate(registry: ProjectRegistry, candidate: Path) -> ProjectScope:
    root, marker = discover_project_root(candidate)
    return registry.register(build_project_identity(root, marker))


def _validate_requested_project(scope: ProjectScope, requested_project: str | None) -> None:
    if requested_project is not None and requested_project not in scope.storage_keys:
        raise AuthorityConflict('project', 'Requested project is outside the resolved scope')


def resolve_project_scope(
    binding: MCPServerBinding,
    registry: ProjectRegistry,
    *,
    client_roots: Sequence[Path],
    cwd: Path | None,
    requested_project: str | None,
) -> ProjectScope | str | None:
    # Exact backward-compatibility boundary: legacy unbound MCP neither
    # interprets Roots/cwd nor registers a path that the caller did not request.
    if not binding.is_bound:
        return requested_project

    roots = tuple(root.resolve() for root in client_roots)

    if binding.project_root is not None:
        explicit_candidate = select_project_candidate(
            ProjectCandidates(explicit_root=binding.project_root)
        )
        authorized_root, _marker = discover_project_root(explicit_candidate)
        for root in roots:
            ensure_path_within(root, authorized_root)
        if cwd is not None:
            ensure_path_within(cwd, authorized_root)
        scope = _register_candidate(registry, explicit_candidate)
        _validate_requested_project(scope, requested_project)
        return scope

    # A global agent-bound server uses one negotiated Root. With many Roots,
    # cwd must be inside exactly one and is the candidate. With no Roots, the
    # marked project discovered from startup_cwd is the authority boundary;
    # cwd may refine a call only by remaining inside it.
    if roots:
        candidate = select_project_candidate(
            ProjectCandidates(mcp_roots=roots, protocol_cwd=cwd)
        )
        if len(roots) == 1:
            selected_boundary = roots[0]
        else:
            assert cwd is not None
            containing = tuple(root for root in roots if _is_within(cwd, root))
            if len(containing) != 1:
                raise MultiRootError('Multiple MCP roots require cwd inside exactly one root')
            selected_boundary = containing[0]
        if cwd is not None:
            ensure_path_within(cwd, selected_boundary)
        discovered_root, _marker = discover_project_root(candidate)
        ensure_path_within(discovered_root, selected_boundary)
        scope = _register_candidate(registry, candidate)
    else:
        startup_root, startup_marker = discover_project_root(binding.startup_cwd)
        if startup_marker is not None:
            if cwd is not None:
                ensure_path_within(cwd, startup_root)
            scope = registry.register(build_project_identity(startup_root, startup_marker))
        else:
            if cwd is None:
                raise ProjectResolutionError('No marked startup or protocol cwd candidate')
            cwd_root, cwd_marker = discover_project_root(cwd)
            if cwd_marker is None:
                raise ProjectResolutionError('Protocol cwd has no project marker')
            scope = registry.register(build_project_identity(cwd_root, cwd_marker))

    _validate_requested_project(scope, requested_project)
    return scope
~~~

Import `_is_within` from `memory.projects` rather than implementing a lexical duplicate. URI decoding and all containment decisions must canonicalize with `realpath`/`Path.resolve`, so a symlink lexically below the root cannot escape it. Internal exceptions may retain a diagnostic reason for local tests, but neither their text nor arguments are a wire contract. Every MCP result must call `public_project_error`; it is the only public mapping and never interpolates the root, cwd, registry path, exception text, or another local value. On Windows, retain the existing drive-letter URI normalization and add parametrized URI tests that do not require a Windows host.

- [ ] **Step 4: Run authority tests**

Run: `uv run --extra dev pytest tests/test_mcp_authority.py tests/test_projects.py -q`

Expected: explicit, one-root, many-root, startup fallback, alias/key, legacy-unbound, URI, and symlink-boundary cases pass; every `public_project_error` result is one fixed constant and contains no local path or internal diagnostic text.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/projects.py src/memory/mcp_authority.py tests/test_projects.py tests/test_mcp_authority.py
git commit -m "feat: bind mcp agent and project authority"
~~~

### Task 3: MCP CLI Binding and Dynamic Tool Schemas

**Files:**
- Modify: src/memory/cli.py
- Modify: src/memory/mcp_server.py
- Modify: tests/test_cli.py
- Modify: tests/test_mcp_server.py

**Interfaces:**
- Changes: run_server(*, agent=None, project_root=None, startup_cwd=None)
- Changes: _create_server(service, binding, registry, *, worker_service_factory=None)
- Produces: legacy_tool_definitions() -> tuple[Tool, ...]
- Produces: tool_definitions(binding: MCPServerBinding) -> tuple[Tool, ...]
- Produces CLI: memory mcp [--agent ID] [--project-root PATH]
- Reads: MEMORY_AGENT only when --agent is omitted

- [ ] **Step 1: Add failing CLI and schema tests**

~~~python
from pathlib import Path

from click.testing import CliRunner
import pytest

from memory.cli import main
from memory.mcp_authority import MCPServerBinding
from memory.mcp_server import tool_definitions


def test_mcp_cli_passes_explicit_binding(monkeypatch) -> None:
    captured = {}
    async def fake_run_server(**kwargs):
        captured.update(kwargs)
    monkeypatch.setattr('memory.mcp_server.run_server', fake_run_server)
    project_root = Path('/tmp/repo')
    result = CliRunner().invoke(
        main,
        ['mcp', '--agent', 'cursor', '--project-root', str(project_root)],
    )
    assert result.exit_code == 0
    assert captured['agent'] == 'cursor'
    # click.Path(path_type=Path) passes a Path, not a string, and does not
    # implicitly resolve it.
    assert captured['project_root'] == project_root
    assert isinstance(captured['startup_cwd'], Path)


def test_explicit_agent_overrides_memory_agent(monkeypatch) -> None:
    captured = {}
    async def fake_run_server(**kwargs):
        captured.update(kwargs)
    monkeypatch.setenv('MEMORY_AGENT', 'gemini-cli')
    monkeypatch.setattr('memory.mcp_server.run_server', fake_run_server)
    result = CliRunner().invoke(main, ['mcp', '--agent', 'cursor'])
    assert result.exit_code == 0
    assert captured['agent'] == 'cursor'


@pytest.mark.anyio
async def test_bound_save_schema_requires_idempotency(tmp_path: Path) -> None:
    binding = MCPServerBinding('cursor', tmp_path, tmp_path)
    tools = tool_definitions(binding)
    save = next(tool for tool in tools if tool.name == 'memory_save')
    details = next(tool for tool in tools if tool.name == 'memory_details')
    assert 'idempotency_key' in save.inputSchema['required']
    assert 'cwd' in save.inputSchema['properties']
    assert details.inputSchema['properties']['memory_id']['minLength'] == 1
~~~

- [ ] **Step 2: Run CLI/MCP tests and observe unsupported flags**

Run: `uv run --extra dev pytest tests/test_cli.py tests/test_mcp_server.py -k "binding or idempotency or mcp_cli" -q`

Expected: Click reports unknown options or _create_server rejects binding arguments.

- [ ] **Step 3: Add binding flags and generate schemas from binding**

~~~python
@main.command()
@click.option('--agent', envvar='MEMORY_AGENT', default=None)
@click.option('--project-root', type=click.Path(file_okay=False, path_type=Path), default=None)
def mcp(agent: str | None, project_root: Path | None) -> None:
    import asyncio
    from memory.mcp_server import run_server
    asyncio.run(run_server(agent=agent, project_root=project_root, startup_cwd=Path.cwd()))
~~~

The test expectation above is normative: do not convert Click's `Path` back to `str` in the CLI. `run_server` resolves it at the authority boundary. Explicit `--agent` wins over `MEMORY_AGENT`; Click's `envvar` behavior supplies the environment only when the option is omitted.

Move the current literal three-tool definitions unchanged into legacy_tool_definitions, add memory_details, and derive the bound copy without mutating the legacy tuple:

~~~python
def tool_definitions(binding: MCPServerBinding) -> tuple[Tool, ...]:
    tools = copy.deepcopy(legacy_tool_definitions())
    by_name = {tool.name: tool for tool in tools}
    for tool in tools:
        tool.inputSchema.setdefault('properties', {})['cwd'] = {
            'type': 'string',
            'description': 'Caller cwd; must remain inside the negotiated project root.',
        }
    if binding.agent is not None:
        save_schema = by_name['memory_save'].inputSchema
        save_schema['properties']['idempotency_key'] = {
            'type': 'string',
            'format': 'uuid',
        }
        save_schema['required'] = [
            *save_schema.get('required', []),
            'idempotency_key',
        ]
    return tools
~~~

The tuple contains memory_context, memory_search, memory_details, and memory_save. Define `memory_details.memory_id` as a required string with `minLength: 1`. Bound schemas keep matching project/agent/source fields for compatibility but document them as authoritative validation fields. Validate UUID syntax in Python as well as JSON schema. Unbound schemas retain only title/what as required and do not require idempotency_key.

- [ ] **Step 4: Run CLI and handler tests**

Run: `uv run --extra dev pytest tests/test_cli.py tests/test_mcp_server.py -q`

Expected: existing unbound tests and new bound-schema tests pass.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/cli.py src/memory/mcp_server.py tests/test_cli.py tests/test_mcp_server.py
git commit -m "feat: expose bound mcp server options"
~~~

### Task 4: Per-Request MCP Roots and Cwd Boundary

**Files:**
- Modify: src/memory/mcp_server.py
- Modify: src/memory/mcp_authority.py
- Create: tests/mcp_helpers.py
- Create: tests/test_mcp_protocol.py

**Interfaces:**
- Consumes: ServerSession.list_roots() from MCP SDK
- Produces: resolve_call_scope(server, binding, registry, arguments) -> ProjectScope | str | None
- Produces: ScopedDispatch and make_legacy_scoped_dispatch(service) -> ScopedDispatch
- Changes: _create_server(..., scoped_dispatch: ScopedDispatch | None = None) selects the concrete legacy bridge by default
- Produces: open_test_client(service, binding, registry, list_roots_callback=None)
- Produces: open_stdio_client(memory_home, agent, project_root)
- Produces: decode_result, decode_object, decode_rows, result_text, assert_public_payload, roots_callback, save_with_details, and call_and_store
- Supports zero, one, and multiple root responses without caching across calls

- [ ] **Step 1: Write failing real-protocol root tests**

First add this complete reusable transport to `tests/mcp_helpers.py`. Every helper used by this task or Task 7 is defined here before any test imports it:

~~~python
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager
from io import StringIO
import json
import os
from pathlib import Path
import sys
from typing import Any
import uuid

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import CallToolResult, ListRootsResult, Root, TextContent

from memory.core import MemoryService
from memory.mcp_authority import MCPServerBinding
from memory.mcp_server import _create_server
from memory.projects import ProjectRegistry


RootsCallback = Callable[[Any], Awaitable[ListRootsResult]]
ServiceFactory = Callable[[], MemoryService]


@asynccontextmanager
async def open_test_client(
    service: MemoryService,
    binding: MCPServerBinding,
    registry: ProjectRegistry,
    list_roots_callback: RootsCallback | None = None,
    worker_service_factory: ServiceFactory | None = None,
) -> AsyncIterator[ClientSession]:
    server = _create_server(
        service,
        binding,
        registry,
        worker_service_factory=worker_service_factory,
    )
    async with create_client_server_memory_streams() as (
        client_streams,
        server_streams,
    ):
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(
                server.run,
                server_streams[0],
                server_streams[1],
                server.create_initialization_options(),
            )
            async with ClientSession(
                *client_streams,
                list_roots_callback=list_roots_callback,
            ) as client:
                await client.initialize()
                yield client
            task_group.cancel_scope.cancel()


def result_text(result: CallToolResult) -> str:
    assert len(result.content) == 1
    content = result.content[0]
    assert isinstance(content, TextContent)
    return content.text


def decode_result(result: CallToolResult) -> object:
    assert result.isError is False
    return json.loads(result_text(result))


def decode_object(result: CallToolResult) -> dict[str, object]:
    payload = decode_result(result)
    assert isinstance(payload, dict)
    return payload


def decode_rows(result: CallToolResult) -> list[dict[str, object]]:
    payload = decode_result(result)
    assert isinstance(payload, list)
    assert all(isinstance(row, dict) for row in payload)
    return payload


def assert_public_payload(result: CallToolResult, *private_paths: Path) -> None:
    payload = decode_result(result)

    def visit(value: object) -> None:
        if isinstance(value, dict):
            assert {'file_path', 'root', 'canonical_owner'}.isdisjoint(value)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(payload)
    serialized = result_text(result)
    for path in private_paths:
        assert str(path.resolve()) not in serialized


def roots_callback(*roots: Path) -> RootsCallback:
    async def callback(_context: Any) -> ListRootsResult:
        return ListRootsResult(
            roots=[Root(uri=root.resolve().as_uri(), name=root.name) for root in roots]
        )
    return callback


def make_workspace(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / 'package.json').write_text('{}', encoding='utf-8')
    return path


@asynccontextmanager
async def open_stdio_client(
    memory_home: Path,
    agent: str,
    project_root: Path,
) -> AsyncIterator[tuple[ClientSession, StringIO]]:
    environment = dict(os.environ)
    environment['MEMORY_HOME'] = str(memory_home)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            '-m',
            'memory.cli',
            'mcp',
            '--agent',
            agent,
            '--project-root',
            str(project_root),
        ],
        cwd=project_root,
        env=environment,
    )
    stderr = StringIO()
    async with stdio_client(parameters, errlog=stderr) as streams:
        async with ClientSession(*streams) as client:
            await client.initialize()
            yield client, stderr


async def save_with_details(client: ClientSession, body: str) -> dict[str, object]:
    return decode_object(
        await client.call_tool(
            'memory_save',
            {
                'title': 'Large details fixture',
                'what': 'protocol fixture',
                'details': body,
                'idempotency_key': str(uuid.uuid4()),
            },
        )
    )


async def call_and_store(
    output: MutableMapping[str, CallToolResult],
    key: str,
    client: ClientSession,
    tool: str,
    arguments: dict[str, object],
) -> None:
    output[key] = await client.call_tool(tool, arguments)
~~~

Keeping subprocess creation here makes the later stdio/concurrency tests use the installed Python interpreter and real JSON-RPC transport instead of invoking handlers directly.

Then use it for the real protocol tests:

~~~python
from pathlib import Path
import uuid

from mcp.types import CallToolResult, ListRootsResult, Root
import pytest

from memory.core import MemoryService
from memory.mcp_authority import (
    AUTHORITY_ERROR,
    AuthorityConflict,
    BOUNDARY_ERROR,
    MCPServerBinding,
    PROJECT_ERROR,
    ROOTS_ERROR,
)
from memory.mcp_server import project_safe_dispatch
from memory.models import RawMemoryInput
from memory.projects import (
    MultiRootError,
    ProjectRegistry,
    ProjectResolutionError,
    build_project_identity,
    discover_project_root,
)
from tests.mcp_helpers import (
    assert_public_payload,
    decode_object,
    decode_rows,
    make_workspace,
    open_test_client,
    result_text,
    roots_callback,
)


@pytest.mark.anyio
async def test_protocol_uses_single_client_root_without_leaking_paths(tmp_path: Path) -> None:
    root = make_workspace(tmp_path / 'encoded workspace')
    memory_home = tmp_path / 'memory-home'
    service = MemoryService(str(memory_home))
    binding = MCPServerBinding('cursor', None, tmp_path)
    registry = ProjectRegistry(memory_home)
    try:
        async with open_test_client(
            service,
            binding,
            registry,
            roots_callback(root),
        ) as client:
            result = await client.call_tool(
                'memory_save',
                {
                    'title': 'Root marker',
                    'what': 'single root',
                    'idempotency_key': str(uuid.uuid4()),
                },
            )
            saved = decode_object(result)
            record = service.get_memory_record(str(saved['id']))
            expected = build_project_identity(*discover_project_root(root))
            assert record['project'] == expected.key
            assert_public_payload(result, root, memory_home)
    finally:
        service.close()


@pytest.mark.anyio
async def test_protocol_rejects_cwd_outside_root_without_disclosing_it(tmp_path: Path) -> None:
    root = make_workspace(tmp_path / 'workspace')
    outside = tmp_path / 'private-outside'
    outside.mkdir()
    memory_home = tmp_path / 'memory-home'
    service = MemoryService(str(memory_home))
    binding = MCPServerBinding('cursor', None, tmp_path)
    try:
        async with open_test_client(
            service,
            binding,
            ProjectRegistry(memory_home),
            roots_callback(root),
        ) as client:
            result = await client.call_tool(
                'memory_search',
                {'query': 'x', 'cwd': str(outside)},
            )
            assert result.isError is True
            assert result_text(result) == BOUNDARY_ERROR
            assert 'private-outside' not in result_text(result)
            assert str(root) not in result_text(result)
            assert 'Resolved path escapes authority boundary' not in result_text(result)
    finally:
        service.close()


@pytest.mark.anyio
async def test_protocol_requests_roots_again_for_each_call(tmp_path: Path) -> None:
    left = make_workspace(tmp_path / 'left')
    right = make_workspace(tmp_path / 'right')
    advertised = [left]
    calls = 0

    async def changing_roots(_context: object) -> ListRootsResult:
        nonlocal calls
        calls += 1
        return ListRootsResult(
            roots=[Root(uri=path.resolve().as_uri(), name=path.name) for path in advertised]
        )

    memory_home = tmp_path / 'memory-home'
    service = MemoryService(str(memory_home))
    try:
        async with open_test_client(
            service,
            MCPServerBinding('cursor', None, tmp_path),
            ProjectRegistry(memory_home),
            changing_roots,
        ) as client:
            first = decode_object(await client.call_tool(
                'memory_save',
                {'title': 'Left', 'what': 'left', 'idempotency_key': str(uuid.uuid4())},
            ))
            advertised[:] = [right]
            second = decode_object(await client.call_tool(
                'memory_save',
                {'title': 'Right', 'what': 'right', 'idempotency_key': str(uuid.uuid4())},
            ))
        left_record = service.get_memory_record(str(first['id']))
        right_record = service.get_memory_record(str(second['id']))
        assert left_record['project'] != right_record['project']
        assert calls == 2
    finally:
        service.close()


@pytest.mark.anyio
async def test_protocol_many_roots_require_cwd_inside_exactly_one(tmp_path: Path) -> None:
    left = make_workspace(tmp_path / 'left')
    right = make_workspace(tmp_path / 'right')
    nested = right / 'src'
    nested.mkdir()
    memory_home = tmp_path / 'memory-home'
    service = MemoryService(str(memory_home))
    try:
        async with open_test_client(
            service,
            MCPServerBinding('cursor', None, tmp_path),
            ProjectRegistry(memory_home),
            roots_callback(left, right),
        ) as client:
            accepted = await client.call_tool(
                'memory_save',
                {
                    'title': 'Selected right',
                    'what': 'many roots',
                    'cwd': str(nested),
                    'idempotency_key': str(uuid.uuid4()),
                },
            )
            saved = decode_object(accepted)
            expected = build_project_identity(*discover_project_root(right))
            assert service.get_memory_record(str(saved['id']))['project'] == expected.key

            rejected = await client.call_tool('memory_search', {'query': 'x'})
            assert rejected.isError is True
            assert result_text(rejected) == ROOTS_ERROR
            assert str(left) not in result_text(rejected)
            assert str(right) not in result_text(rejected)
            assert 'exactly one root' not in result_text(rejected)
    finally:
        service.close()


@pytest.mark.anyio
async def test_protocol_zero_roots_uses_only_marked_startup(tmp_path: Path) -> None:
    marked = make_workspace(tmp_path / 'marked workspace')
    startup = marked / 'tools'
    startup.mkdir()
    generic = tmp_path / 'generic-home'
    generic.mkdir()

    for startup_cwd, should_succeed in ((startup, True), (generic, False)):
        memory_home = tmp_path / f'memory-{should_succeed}'
        service = MemoryService(str(memory_home))
        try:
            async with open_test_client(
                service,
                MCPServerBinding('cursor', None, startup_cwd),
                ProjectRegistry(memory_home),
                roots_callback(),
            ) as client:
                result = await client.call_tool('memory_search', {'query': 'x'})
                assert result.isError is (not should_succeed)
                if should_succeed:
                    assert_public_payload(result, marked, memory_home)
                else:
                    assert result_text(result) == ROOTS_ERROR
                    assert str(generic) not in result_text(result)
        finally:
            service.close()


@pytest.mark.parametrize('advertises_roots_capability', [False, True])
@pytest.mark.anyio
async def test_protocol_generic_startup_accepts_explicit_marked_cwd_without_roots(
    tmp_path: Path, advertises_roots_capability: bool,
) -> None:
    generic = tmp_path / 'generic-home'
    project = make_workspace(tmp_path / 'workspace')
    cwd = project / 'src'
    generic.mkdir()
    cwd.mkdir()
    memory_home = tmp_path / 'memory-home'
    service = MemoryService(str(memory_home))
    roots = roots_callback() if advertises_roots_capability else None
    try:
        async with open_test_client(
            service,
            MCPServerBinding('cursor', None, generic),
            ProjectRegistry(memory_home),
            roots,
        ) as client:
            accepted = await client.call_tool(
                'memory_search',
                {'query': 'x', 'cwd': str(cwd)},
            )
            assert accepted.isError is False
            assert_public_payload(accepted, project, memory_home)
            rejected = await client.call_tool(
                'memory_search',
                {'query': 'x', 'cwd': str(generic)},
            )
            assert rejected.isError is True
            assert result_text(rejected) == ROOTS_ERROR
            assert str(generic) not in result_text(rejected)
    finally:
        service.close()


@pytest.mark.anyio
async def test_protocol_without_roots_capability_uses_marked_startup(tmp_path: Path) -> None:
    root = make_workspace(tmp_path / 'startup')
    startup = root / 'tools'
    startup.mkdir()
    memory_home = tmp_path / 'memory-home'
    service = MemoryService(str(memory_home))
    try:
        async with open_test_client(
            service,
            MCPServerBinding('cursor', None, startup),
            ProjectRegistry(memory_home),
        ) as client:
            result = await client.call_tool('memory_search', {'query': 'x'})
            assert result.isError is False
            assert_public_payload(result, root, memory_home)
    finally:
        service.close()


@pytest.mark.parametrize('corruption', ['git-pointer', 'registry-json'])
@pytest.mark.anyio
async def test_protocol_maps_low_level_project_errors_without_path_leak(
    tmp_path: Path, corruption: str,
) -> None:
    root = make_workspace(tmp_path / 'workspace')
    memory_home = tmp_path / 'memory-home'
    registry = ProjectRegistry(memory_home)
    private_marker = tmp_path / 'private-location'
    if corruption == 'git-pointer':
        (root / '.git').write_text(
            f'gitdir: {private_marker / "missing-git-dir"}\n',
            encoding='utf-8',
        )
    else:
        memory_home.mkdir(parents=True, exist_ok=True)
        (memory_home / 'projects.json').write_text(
            f'{{"private":"{private_marker}"',
            encoding='utf-8',
        )
    service = MemoryService(str(memory_home))
    try:
        async with open_test_client(
            service,
            MCPServerBinding('cursor', root, root),
            registry,
            roots_callback(root),
        ) as client:
            result = await client.call_tool('memory_search', {'query': 'x'})
            assert result.isError is True
            assert result_text(result) == ROOTS_ERROR
            payload = result_text(result)
            for forbidden in (str(root), str(private_marker), str(memory_home)):
                assert forbidden not in payload
            for low_level in ('gitdir', 'projects.json', 'missing-git-dir', 'private-location'):
                assert low_level not in payload
    finally:
        service.close()


@pytest.mark.anyio
async def test_project_safe_dispatch_never_forwards_exception_text(tmp_path: Path) -> None:
    private = tmp_path / 'private-workspace'
    cases = (
        (AuthorityConflict('boundary', f'escaped {private}'), BOUNDARY_ERROR),
        (AuthorityConflict('project', f'alias data {private}'), PROJECT_ERROR),
        (AuthorityConflict('authority', f'bound agent data {private}'), AUTHORITY_ERROR),
        (MultiRootError(f'roots were {private}'), ROOTS_ERROR),
        (ProjectResolutionError(f'bad registry at {private}'), ROOTS_ERROR),
    )
    for error, expected in cases:
        async def fail(error: ProjectResolutionError = error) -> CallToolResult:
            raise error

        result = await project_safe_dispatch(fail)
        assert result.isError is True
        assert result_text(result) == expected
        assert str(private) not in result_text(result)
        assert str(error) not in result_text(result)
~~~

The first single-root case exercises a percent-encoded file URI through `roots_callback(encoded workspace)`. Every failure asserts the complete generic error string and also asserts that neither the selected root, rejected cwd, nor `MEMORY_HOME` occurs in `result_text(result)`. Every successful tool result uses `assert_public_payload`.

- [ ] **Step 2: Run protocol tests and observe cwd-based/basename behavior**

Run: `uv run --extra dev pytest tests/test_mcp_protocol.py -k "roots or project_safe_dispatch or low_level_project_errors" -q`

Expected: the server never requests Roots or still uses its process basename, and the safe dispatch symbol/mapping is absent or leaks the low-level exception text.

- [ ] **Step 3: Resolve roots inside the call handler**

Inside each tool call, inspect `server.request_context.session.client_params.capabilities.roots`; when present, call `list_roots()` and convert every returned URI with `file_uri_to_path`, exactly as shown in `resolve_call_scope` below.

Do not cache roots between calls. Convert authority/project errors into MCP tool errors with concise messages and no path outside the selected boundary. For a project-bound server, reject a root that conflicts with `--project-root`. For a global bound adapter, accept one root directly, accept many roots only when cwd lies inside exactly one, and with no roots use the marked startup workspace while validating cwd inside it. A generic unmarked startup directory is degraded and requests explicit cwd/project setup.

Implement the call boundary explicitly:

~~~python
async def resolve_call_scope(
    server: Server,
    binding: MCPServerBinding,
    registry: ProjectRegistry,
    arguments: Mapping[str, object],
) -> ProjectScope | str | None:
    capabilities = server.request_context.session.client_params.capabilities
    client_roots: tuple[Path, ...] = ()
    if capabilities.roots is not None:
        roots_result = await server.request_context.session.list_roots()
        client_roots = tuple(
            file_uri_to_path(str(root.uri)) for root in roots_result.roots
        )

    raw_cwd = arguments.get('cwd')
    if raw_cwd is not None and not isinstance(raw_cwd, str):
        raise AuthorityConflict('authority', 'cwd must be a string path')
    raw_project = arguments.get('project')
    if raw_project is not None and not isinstance(raw_project, str):
        raise AuthorityConflict('authority', 'project must be a string')
    return resolve_project_scope(
        binding,
        registry,
        client_roots=client_roots,
        cwd=Path(raw_cwd) if raw_cwd else None,
        requested_project=raw_project,
    )
~~~

Catch the project/authority exception family only through the fixed public mapping below. Never pass `str(error)`, `error.args`, a registry/parser message, or another unchanged exception value to `tool_error`. Strip internal `file_path`, `root`, `canonical_owner`, registry paths, and exception representations from every successful payload as well. The legacy unbound payload shapes stay unchanged apart from removing the already-internal save `file_path`; the CLI still exposes local file paths where its existing contract requires them.

Return errors deliberately rather than relying on the SDK's exception wrapper. Wrap `resolve_call_scope` plus every operation performed by the selected concrete callback with `project_safe_dispatch`; in Task 4 that callback is the legacy scope bridge, and Task 5 later replaces only the callback selection for bound servers:

~~~python
from collections.abc import Awaitable, Callable, Mapping
import logging

from memory.mcp_authority import MCPServerBinding, public_project_error
from memory.projects import ProjectResolutionError, ProjectScope


logger = logging.getLogger(__name__)
INTERNAL_ERROR = 'EchoVault could not complete the tool request'
ResolvedScope = ProjectScope | str | None
ScopedDispatch = Callable[
    [str, Mapping[str, object], ResolvedScope],
    Awaitable[CallToolResult],
]


def tool_error(message: str) -> CallToolResult:
    return CallToolResult(
        isError=True,
        content=[TextContent(type='text', text=message)],
    )


def success_text(text: str) -> CallToolResult:
    payload = json.loads(text)
    if isinstance(payload, dict):
        payload.pop('file_path', None)
    return CallToolResult(
        isError=False,
        content=[TextContent(type='text', text=json.dumps(payload))],
    )


def legacy_project(scope: ResolvedScope) -> str:
    if isinstance(scope, ProjectScope):
        return scope.identity.key
    if isinstance(scope, str):
        return scope
    return os.path.basename(os.getcwd())


def make_legacy_scoped_dispatch(service: MemoryService) -> ScopedDispatch:
    """Bridge Task-4 scope resolution to the already-existing handlers."""
    async def dispatch(
        name: str,
        arguments: Mapping[str, object],
        scope: ResolvedScope,
    ) -> CallToolResult:
        project = legacy_project(scope)
        call_arguments = dict(arguments)
        call_arguments.pop('cwd', None)
        call_arguments.pop('project', None)

        if name == 'memory_save':
            # Task 4 proves scope routing only. Task 5 consumes these fields
            # authoritatively when it replaces this legacy bridge.
            call_arguments.pop('idempotency_key', None)
            call_arguments.pop('source', None)
            return success_text(handle_memory_save(
                service,
                project=project,
                **call_arguments,
            ))
        if name == 'memory_search':
            return success_text(handle_memory_search(
                service,
                project=project,
                **call_arguments,
            ))
        if name == 'memory_context':
            return success_text(handle_memory_context(
                service,
                project=project,
                **call_arguments,
            ))
        if name == 'memory_details':
            memory_id = call_arguments.get('memory_id')
            if not isinstance(memory_id, str) or not memory_id:
                return tool_error('memory_id must be a non-empty string')
            detail = service.get_details(memory_id, project=project)
            payload = (
                {'status': 'not_found'}
                if detail is None
                else {'status': 'ok', 'memory_id': detail.memory_id, 'body': detail.body}
            )
            return success_text(json.dumps(payload))
        return tool_error('Unknown EchoVault tool')

    return dispatch


async def project_safe_dispatch(
    operation: Callable[[], Awaitable[CallToolResult]],
) -> CallToolResult:
    try:
        return await operation()
    except ProjectResolutionError as error:
        # AuthorityConflict is a ProjectResolutionError subclass. The mapper
        # selects one fixed constant and never reads the exception text.
        return tool_error(public_project_error(error))
    except Exception as error:
        # Log only the class name. Exception text/args may contain local paths.
        logger.error('Unhandled MCP tool failure (%s)', type(error).__name__)
        return tool_error(INTERNAL_ERROR)


def install_scoped_call_handler(
    server: Server,
    binding: MCPServerBinding,
    registry: ProjectRegistry,
    selected_dispatch: ScopedDispatch,
) -> None:
    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, object]) -> CallToolResult:
        async def resolve_and_dispatch() -> CallToolResult:
            scope = await resolve_call_scope(server, binding, registry, arguments)
            return await selected_dispatch(name, arguments, scope)

        return await project_safe_dispatch(resolve_and_dispatch)


def configure_task4_dispatch(
    server: Server,
    service: MemoryService,
    binding: MCPServerBinding,
    registry: ProjectRegistry,
    scoped_dispatch: ScopedDispatch | None,
) -> None:
    selected_dispatch = scoped_dispatch or make_legacy_scoped_dispatch(service)
    install_scoped_call_handler(
        server,
        binding,
        registry,
        selected_dispatch,
    )
~~~

Add `scoped_dispatch: ScopedDispatch | None = None` to `_create_server` and call `configure_task4_dispatch(server, service, binding, registry, scoped_dispatch)` after registering `list_tools`; remove the old `call_tool` decorator so there is exactly one protocol handler. Every symbol consumed by that call is now produced in Task 4. `make_legacy_scoped_dispatch` is what all Task-4 real-protocol Roots tests exercise. It deliberately projects a bound scope to the canonical key only; alias-aware reads, bound identity checks, and canonical provenance are added by Task 5's replacement callback. There must be no catch block in an individual handler that serializes exception text. AnyIO cancellation inherits outside the caught `Exception` path on supported runtimes and must continue to propagate; add an explicit cancellation regression in Task 7.

- [ ] **Step 4: Run all authority/protocol tests**

Run: `uv run --extra dev pytest tests/test_mcp_authority.py tests/test_mcp_protocol.py -q`

Expected: zero/one/many roots and boundary cases pass.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/mcp_server.py src/memory/mcp_authority.py tests/mcp_helpers.py tests/test_mcp_protocol.py
git commit -m "feat: negotiate mcp workspace roots"
~~~

### Task 5: Four Bound Project-Scoped Tool Handlers

**Files:**
- Modify: src/memory/mcp_server.py
- Modify: tests/test_mcp_server.py
- Modify: tests/test_mcp_protocol.py

**Interfaces:**
- Produces: handle_memory_details
- Produces: make_bound_scoped_dispatch(service, binding) -> ScopedDispatch
- Changes: _create_server selects the bound callback only after this Task-5 producer exists
- Changes all handlers to consume `ProjectScope | str | None` rather than basenames
- Enforces bound agent/source/project identity and save idempotency

- [ ] **Step 1: Add failing spoofing, details, and cross-agent tests**

Use the Task-4 helper module and construct all services/registries in the tests themselves:

~~~python
@pytest.mark.anyio
async def test_bound_server_rejects_source_agent_and_project_spoof(tmp_path: Path) -> None:
    root = make_workspace(tmp_path / 'repo')
    memory_home = tmp_path / 'memory-home'
    service = MemoryService(str(memory_home))
    try:
        async with open_test_client(
            service,
            MCPServerBinding('cursor', root, root),
            ProjectRegistry(memory_home),
        ) as client:
            for tool, arguments, expected, low_level in (
                (
                    'memory_context',
                    {'query': 'x', 'agent': 'gemini-cli'},
                    AUTHORITY_ERROR,
                    'Conflicting agent',
                ),
                (
                    'memory_save',
                    {
                        'title': 'x',
                        'what': 'x',
                        'source': 'gemini-cli',
                        'idempotency_key': str(uuid.uuid4()),
                    },
                    AUTHORITY_ERROR,
                    'Conflicting source',
                ),
                (
                    'memory_search',
                    {'query': 'x', 'project': 'other'},
                    PROJECT_ERROR,
                    'outside the resolved scope',
                ),
            ):
                result = await client.call_tool(tool, arguments)
                assert result.isError is True
                assert result_text(result) == expected
                assert low_level not in result_text(result)
                assert str(root) not in result_text(result)
                assert str(memory_home) not in result_text(result)
    finally:
        service.close()


@pytest.mark.anyio
async def test_bound_details_hides_other_project_without_feedback(tmp_path: Path) -> None:
    root = make_workspace(tmp_path / 'repo')
    memory_home = tmp_path / 'memory-home'
    service = MemoryService(str(memory_home))
    seeded = service.save(
        RawMemoryInput(title='Private', what='other project', details='secret'),
        project='other--111111111111',
    )
    before = service.get_memory_record(str(seeded['id']))['details_opened_count']
    try:
        async with open_test_client(
            service,
            MCPServerBinding('cursor', root, root),
            ProjectRegistry(memory_home),
        ) as client:
            result = await client.call_tool(
                'memory_details',
                {'memory_id': str(seeded['id'])},
            )
            assert decode_object(result) == {'status': 'not_found'}
        after = service.get_memory_record(str(seeded['id']))['details_opened_count']
        assert after == before
    finally:
        service.close()


@pytest.mark.anyio
async def test_reads_use_aliases_but_save_writes_only_hashed_key(tmp_path: Path) -> None:
    root = make_workspace(tmp_path / 'repo')
    memory_home = tmp_path / 'memory-home'
    service = MemoryService(str(memory_home))
    registry = ProjectRegistry(memory_home)
    identity = build_project_identity(*discover_project_root(root))
    registry.register(identity)
    registry.adopt_legacy('legacy-repo', identity)
    legacy = service.save(
        RawMemoryInput(title='Legacy marker', what='ALIAS-READ-42'),
        project='legacy-repo',
    )
    try:
        async with open_test_client(
            service,
            MCPServerBinding('cursor', root, root),
            registry,
        ) as client:
            found = decode_rows(await client.call_tool(
                'memory_search',
                {'query': 'ALIAS-READ-42', 'project': 'legacy-repo'},
            ))
            assert str(legacy['id']) in {str(row['id']) for row in found}
            saved = decode_object(await client.call_tool(
                'memory_save',
                {
                    'title': 'Canonical marker',
                    'what': 'HASHED-WRITE-42',
                    'project': 'legacy-repo',
                    'idempotency_key': str(uuid.uuid4()),
                },
            ))
        assert service.get_memory_record(str(saved['id']))['project'] == identity.key
    finally:
        service.close()


@pytest.mark.anyio
async def test_cursor_save_is_retrievable_by_gemini(tmp_path: Path) -> None:
    root = make_workspace(tmp_path / 'repo')
    memory_home = tmp_path / 'memory-home'
    service = MemoryService(str(memory_home))
    registry = ProjectRegistry(memory_home)
    try:
        async with open_test_client(
            service,
            MCPServerBinding('cursor', root, root),
            registry,
        ) as cursor_client:
            async with open_test_client(
                service,
                MCPServerBinding('gemini-cli', root, root),
                registry,
            ) as gemini_client:
                saved = decode_object(await cursor_client.call_tool(
                    'memory_save',
                    {
                        'title': 'Cross agent',
                        'what': 'shared marker',
                        'idempotency_key': str(uuid.uuid4()),
                    },
                ))
                found = decode_rows(await gemini_client.call_tool(
                    'memory_search',
                    {'query': 'Cross agent'},
                ))
                assert saved['id'] in {row['id'] for row in found}
    finally:
        service.close()


@pytest.mark.anyio
async def test_bound_tool_results_never_expose_local_paths(tmp_path: Path) -> None:
    root = make_workspace(tmp_path / 'repo')
    memory_home = tmp_path / 'memory-home'
    service = MemoryService(str(memory_home))
    try:
        async with open_test_client(
            service,
            MCPServerBinding('cursor', root, root),
            ProjectRegistry(memory_home),
        ) as client:
            save_result = await client.call_tool(
                'memory_save',
                {
                    'title': 'Public payload marker',
                    'what': 'PUBLIC-PAYLOAD-42',
                    'details': 'full body',
                    'idempotency_key': str(uuid.uuid4()),
                },
            )
            saved = decode_object(save_result)
            results = (
                save_result,
                await client.call_tool('memory_context', {'query': 'PUBLIC-PAYLOAD-42'}),
                await client.call_tool('memory_search', {'query': 'PUBLIC-PAYLOAD-42'}),
                await client.call_tool('memory_details', {'memory_id': saved['id']}),
            )
            for result in results:
                assert_public_payload(result, root, memory_home)
    finally:
        service.close()
~~~

- [ ] **Step 2: Run tool tests and observe missing details/authority**

Run: `uv run --extra dev pytest tests/test_mcp_server.py tests/test_mcp_protocol.py -k "spoof or details or cross_agent" -q`

Expected: Task 4's concrete legacy bridge still accepts bound agent/source overrides and reads only the canonical key, so spoofing and alias-read assertions fail before the bound callback is introduced.

- [ ] **Step 3: Make handlers thin and authoritative**

Each call follows:

1. Resolve ProjectScope from current Roots/binding/cwd.
2. Resolve the bound agent or source and reject a mismatch.
3. Validate bound-save UUID before entering MemoryService.
4. Call read methods with the full `ProjectScope`, but call save with only `scope.identity.key` plus `authoritative_source`/`idempotency_key`.
5. Return only project-authorized fields.

~~~python
ResolvedScope = ProjectScope | str | None


def read_project(scope: ResolvedScope) -> ProjectScope | str | None:
    # MemoryService expands ProjectScope.storage_keys for reads.
    # None is the exact legacy-unbound signal and retains the historical cwd
    # basename default without registering a fabricated canonical path.
    return os.path.basename(os.getcwd()) if scope is None else scope


def write_project(scope: ResolvedScope) -> str | None:
    # Adopted aliases are read compatibility only. New canonical writes always
    # use the collision-safe hashed identity key.
    if isinstance(scope, ProjectScope):
        return scope.identity.key
    return os.path.basename(os.getcwd()) if scope is None else scope


def handle_memory_details(
    service: MemoryService,
    memory_id: str,
    *,
    scope: ResolvedScope,
) -> str:
    detail = service.get_details(memory_id, project=read_project(scope))
    if detail is None:
        return json.dumps({'status': 'not_found'})
    return json.dumps({'status': 'ok', 'memory_id': detail.memory_id, 'body': detail.body})


def make_bound_scoped_dispatch(
    service: MemoryService,
    binding: MCPServerBinding,
) -> ScopedDispatch:
    """Create the authoritative four-tool callback consumed by _create_server."""
    async def dispatch(
        name: str,
        arguments: Mapping[str, object],
        scope: ResolvedScope,
    ) -> CallToolResult:
        if not isinstance(scope, ProjectScope):
            raise ProjectResolutionError('Bound dispatch requires a canonical scope')

        call_arguments = dict(arguments)
        call_arguments.pop('cwd', None)
        call_arguments.pop('project', None)

        if name == 'memory_context':
            requested_agent = call_arguments.pop('agent', None)
            if requested_agent is not None and not isinstance(requested_agent, str):
                raise AuthorityConflict('authority', 'agent must be a string')
            agent = resolve_bound_identity(
                binding.agent,
                requested_agent,
                field_name='agent',
            )
            return success_text(handle_memory_context(
                service,
                project=read_project(scope),
                agent=agent,
                **call_arguments,
            ))

        if name == 'memory_search':
            return success_text(handle_memory_search(
                service,
                project=read_project(scope),
                **call_arguments,
            ))

        if name == 'memory_details':
            memory_id = call_arguments.get('memory_id')
            if not isinstance(memory_id, str) or not memory_id:
                raise AuthorityConflict('authority', 'memory_id must be non-empty')
            return success_text(handle_memory_details(
                service,
                memory_id,
                scope=scope,
            ))

        if name == 'memory_save':
            requested_source = call_arguments.pop('source', None)
            if requested_source is not None and not isinstance(requested_source, str):
                raise AuthorityConflict('authority', 'source must be a string')
            source = resolve_bound_identity(
                binding.agent,
                requested_source,
                field_name='source',
            )
            operation_id = call_arguments.pop('idempotency_key', None)
            if binding.agent is not None:
                if not isinstance(operation_id, str):
                    raise AuthorityConflict('authority', 'idempotency key is required')
                try:
                    UUID(operation_id)
                except ValueError as error:
                    raise AuthorityConflict(
                        'authority',
                        'idempotency key is not a UUID',
                    ) from error
            return success_text(handle_memory_save(
                service,
                project=write_project(scope),
                authoritative_source=source,
                idempotency_key=operation_id,
                **call_arguments,
            ))

        return tool_error('Unknown EchoVault tool')

    return dispatch
~~~

`memory_context`, `memory_search`, and `memory_details` pass `read_project(scope)`, which expands a bound scope to `scope.storage_keys` in `MemoryService`/`MemoryDB` and preserves the historical cwd basename for an unbound omitted project. `memory_save` passes `write_project(scope)` and therefore never writes an adopted alias while retaining the same legacy default. `memory_context` uses the bound agent for `resolve_context_mode` but never as a creator-source filter. `memory_save` uses `resolve_bound_identity(binding.agent, requested_source, field_name='source')`; context uses the same function with `field_name='agent'`. An agent-bound save validates `UUID(arguments['idempotency_key'])` before entering `MemoryService`, while legacy unbound save keeps the field optional.

The only authority objects are `MCPServerBinding` (server installation authority) and the per-call `ProjectScope` (resolved project read/write authority). Preserve direct legacy handler tests by accepting `scope: str | None`; do not add a third authority abstraction. Extend `handle_memory_save` in this task to accept `authoritative_source` and `idempotency_key` and pass both to the Phase-1 `MemoryService.save` contract.

Place `make_bound_scoped_dispatch` above `configure_task4_dispatch` in `mcp_server.py`. Only after that producer exists, replace Task 4's one selection line inside `configure_task4_dispatch` with:

~~~python
selected_dispatch = scoped_dispatch or (
    make_bound_scoped_dispatch(service, binding)
    if binding.is_bound
    else make_legacy_scoped_dispatch(service)
)
~~~

This is the first point at which `_create_server` references the Task-5 producer. The callback resolves bound `agent`/`source`, validates the operation UUID, invokes the matching thin handler, and returns a success `CallToolResult`. It does not catch `AuthorityConflict`, `ProjectResolutionError`, or `MultiRootError`; those reach Task 4's already-produced `project_safe_dispatch`, which maps them without reading their text. Unknown tool names return a fixed error without an exception or path.

Before JSON encoding, project all rows onto an explicit allowlist. Save exposes `id`, `action`, `warnings`, and replay/update metadata; it never exposes `file_path`. Search/context expose the existing public memory fields but omit `file_path`, `section_anchor`, registry paths, `ProjectIdentity.root`, and `canonical_owner`. Details exposes only status, ID, and body. The four-tool regression above recursively proves that none of those internal keys or local paths reaches a result.

- [ ] **Step 4: Run all MCP handler/protocol tests**

Run: `uv run --extra dev pytest tests/test_mcp_server.py tests/test_mcp_protocol.py -q`

Expected: four tools, spoof rejection, cross-project hiding, replay, and cross-agent retrieval pass.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/mcp_server.py tests/test_mcp_server.py tests/test_mcp_protocol.py
git commit -m "feat: scope all mcp memory tools"
~~~

### Task 6: Remote Query Privacy and Feedback-Suppressed Retrieval

**Files:**
- Modify: src/memory/config.py
- Modify: src/memory/embeddings/base.py
- Modify: src/memory/embeddings/ollama.py
- Modify: src/memory/embeddings/openai_embed.py
- Modify: src/memory/core.py
- Modify: src/memory/health.py
- Create: tests/test_embedding_privacy.py
- Modify: tests/test_remote_embeddings.py

**Interfaces:**
- Produces: ContextConfig.allow_remote_query_embeddings: bool = False
- Produces: EmbeddingProvider.is_remote: bool
- Changes: MemoryService.get_context(..., record_feedback=True)
- Changes: MemoryService.search(..., embedding_query=None, record_feedback=True)

- [ ] **Step 1: Write failing remote-provider privacy tests**

~~~python
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from memory.core import MemoryService
from memory.embeddings.base import EmbeddingProvider
from memory.models import RawMemoryInput


class RecordingRemoteProvider(EmbeddingProvider):
    is_remote = True

    def __init__(self) -> None:
        self.inputs: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.inputs.append(text)
        return [1.0, 0.0, 0.0]


class FTSQuerySpy:
    def __init__(self, wrapped: Callable[..., list[dict]]) -> None:
        self.wrapped = wrapped
        self.queries: list[str] = []

    def __call__(self, query: str, *args, **kwargs) -> list[dict]:
        self.queries.append(query)
        return self.wrapped(query, *args, **kwargs)


@dataclass
class PrivacyFixture:
    service: MemoryService
    provider: RecordingRemoteProvider
    fts_spy: FTSQuerySpy


@pytest.fixture
def service_with_remote_provider(env_home: Path, monkeypatch):
    service = MemoryService(str(env_home))
    provider = RecordingRemoteProvider()
    service.config.embedding.provider = 'openai'
    service.config.context.semantic = 'always'
    service._embedding_provider = provider
    service.db.ensure_vec_table(3)
    service._vectors_available = True
    fts_spy = FTSQuerySpy(service.db.fts_search)
    monkeypatch.setattr(service.db, 'fts_search', fts_spy)
    try:
        yield PrivacyFixture(service, provider, fts_spy)
    finally:
        service.close()


@pytest.fixture
def local_service(env_home: Path):
    service = MemoryService(str(env_home))
    try:
        yield service
    finally:
        service.close()


def test_context_default_never_embeds_query_remotely(service_with_remote_provider: PrivacyFixture) -> None:
    fixture = service_with_remote_provider
    fixture.service.get_context(project='p', query='secret sk_live_value')
    assert fixture.provider.inputs == []
    assert fixture.fts_spy.queries == ['secret sk_live_value']


def test_remote_opt_in_sends_only_redacted_query(service_with_remote_provider: PrivacyFixture) -> None:
    fixture = service_with_remote_provider
    fixture.service.config.context.allow_remote_query_embeddings = True
    fixture.service.get_context(project='p', query='secret sk_live_value')
    assert fixture.provider.inputs == ['secret [REDACTED]']
    assert fixture.fts_spy.queries == ['secret sk_live_value']


def test_query_text_is_not_persisted_or_logged(
    service_with_remote_provider: PrivacyFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fixture = service_with_remote_provider
    fixture.service.config.context.allow_remote_query_embeddings = True
    fixture.service.get_context(project='p', query='secret sk_live_value')
    assert 'sk_live_value' not in caplog.text
    for path in Path(fixture.service.vault_dir).rglob('*.md'):
        assert 'sk_live_value' not in path.read_text(encoding='utf-8')


def test_feedback_can_be_suppressed_for_duplicate_hooks(local_service: MemoryService) -> None:
    saved = local_service.save(RawMemoryInput(title='Marker', what='ALPHA-42'), project='p')
    local_service.get_context(project='p', query='ALPHA-42', record_feedback=False)
    assert local_service.get_memory_record(saved['id'])['retrieved_count'] == 0
~~~

Keep `RecordingRemoteProvider`, `FTSQuerySpy`, and `PrivacyFixture` in `tests/test_embedding_privacy.py`; the spy wraps the real `fts_search` method instead of inventing test-only attributes on production `MemoryDB`.

- [ ] **Step 2: Run privacy tests and observe remote query leakage**

Run: `uv run --extra dev pytest tests/test_embedding_privacy.py tests/test_remote_embeddings.py -q`

Expected: the config field and `is_remote` capability are absent; after only scaffolding them, the remote provider receives `sk_live_value` unchanged while the FTS spy proves the lexical path also received it.

- [ ] **Step 3: Split lexical and embedding query paths**

~~~python
class EmbeddingProvider(ABC):
    is_remote = False


class OpenAIEmbedding(EmbeddingProvider):
    is_remote = True


class OllamaEmbedding(EmbeddingProvider):
    is_remote = False
~~~

When query context uses a remote provider and opt-in is false, force `use_vectors=False` while preserving the original FTS query. When opt-in is true, compute `embedding_query = redact(query, self.ignore_patterns)` and pass it only to the embedding provider while continuing to pass `query` to FTS. Extend `MemoryService.search` with keyword-only `embedding_query: str | None = None`; `tiered_search` receives separate lexical and embedding strings and defaults the embedding string to the lexical string for all existing callers. Never overwrite or log either value. Add `allow_remote_query_embeddings` parsing to config and expose only effective mode, never query content, in doctor.

Move feedback recording out of lower-level search so get_context can make one explicit call when record_feedback is true. Gemini's hook will retrieve with false and record only after winning its event claim.

- [ ] **Step 4: Run privacy, search, context, and health tests**

Run: `uv run --extra dev pytest tests/test_embedding_privacy.py tests/test_remote_embeddings.py tests/test_search.py tests/test_core.py tests/test_living_memory.py -q`

Expected: local FTS remains unchanged, remote opt-in is enforced, and feedback suppression works.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/config.py src/memory/embeddings/base.py src/memory/embeddings/ollama.py src/memory/embeddings/openai_embed.py src/memory/core.py src/memory/health.py tests/test_embedding_privacy.py tests/test_remote_embeddings.py
git commit -m "feat: keep automatic query embeddings private"
~~~

### Task 7: MCP Contract, Cancellation, and Multi-Process Gate

**Files:**
- Modify: tests/mcp_helpers.py
- Modify: tests/test_mcp_protocol.py
- Modify: tests/test_integration.py
- Modify: src/memory/mcp_server.py

**Interfaces:**
- Verifies initialization/list-tools/call-tool over real MCP streams
- Verifies malformed input, cancellation, large output, zero/one/many roots
- Verifies two bound MCP processes share one vault without cross-project leakage

- [ ] **Step 1: Add executable inventory, cancellation, validation, stdio-concurrency, and cross-agent cases**

Define the blocking service before the cancellation test in `tests/test_mcp_protocol.py`; it is the only test double not already provided by `tests/mcp_helpers.py`:

~~~python
from pathlib import Path
import threading
import uuid

import anyio
from mcp import ClientSession
import pytest

from memory.core import MemoryService
from memory.mcp_authority import MCPServerBinding
from memory.models import RawMemoryInput
from memory.projects import ProjectRegistry, build_project_identity, discover_project_root
from tests.mcp_helpers import (
    decode_object,
    make_workspace,
    open_test_client,
    result_text,
    save_with_details,
)


class BlockingContextService(MemoryService):
    def __init__(
        self,
        memory_home: str,
        context_started: threading.Event,
        release_context: threading.Event,
    ) -> None:
        super().__init__(memory_home)
        self.context_started = context_started
        self.release_context = release_context

    def get_context(self, *args, **kwargs):
        self.context_started.set()
        if not self.release_context.wait(timeout=5):
            raise TimeoutError('test did not release context')
        return super().get_context(*args, **kwargs)


@pytest.mark.anyio
async def test_tool_inventory_and_large_details(tmp_path: Path) -> None:
    root = make_workspace(tmp_path / 'repo')
    memory_home = tmp_path / 'memory-home'
    service = MemoryService(str(memory_home))
    try:
        async with open_test_client(
            service,
            MCPServerBinding('cursor', root, root),
            ProjectRegistry(memory_home),
        ) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == {
                'memory_context', 'memory_search', 'memory_details', 'memory_save'
            }
            body = 'x' * 100_000
            saved = await save_with_details(client, body)
            detail = decode_object(await client.call_tool(
                'memory_details',
                {'memory_id': saved['id']},
            ))
            assert detail['body'] == body
    finally:
        service.close()


@pytest.mark.anyio
async def test_cancelling_context_keeps_next_request_healthy(tmp_path: Path) -> None:
    root = make_workspace(tmp_path / 'repo')
    memory_home = tmp_path / 'memory-home'
    service = MemoryService(str(memory_home))
    context_started = threading.Event()
    release_context = threading.Event()
    identity = build_project_identity(*discover_project_root(root))
    seeded = service.save(
        RawMemoryInput(title='Cancel target', what='cancel marker'),
        project=identity.key,
    )
    cancel_scope = anyio.CancelScope()
    finished = anyio.Event()

    async def invoke_context(client: ClientSession) -> None:
        try:
            with cancel_scope:
                await client.call_tool('memory_context', {'query': 'cancel marker'})
        finally:
            finished.set()

    try:
        async with open_test_client(
            service,
            MCPServerBinding('cursor', root, root),
            ProjectRegistry(memory_home),
            worker_service_factory=lambda: BlockingContextService(
                str(memory_home),
                context_started,
                release_context,
            ),
        ) as client:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(invoke_context, client)
                with anyio.fail_after(2):
                    while not context_started.is_set():
                        await anyio.sleep(0.01)
                cancel_scope.cancel()
                await finished.wait()
                release_context.set()
                next_result = await client.call_tool(
                    'memory_search',
                    {'query': 'next request'},
                )
                assert next_result.isError is False
                assert service.get_memory_record(str(seeded['id']))['retrieved_count'] == 0
                task_group.cancel_scope.cancel()
    finally:
        release_context.set()
        service.close()


@pytest.mark.anyio
async def test_malformed_bound_inputs_are_tool_errors(tmp_path: Path) -> None:
    root = make_workspace(tmp_path / 'repo')
    memory_home = tmp_path / 'memory-home'
    service = MemoryService(str(memory_home))
    try:
        async with open_test_client(
            service,
            MCPServerBinding('cursor', root, root),
            ProjectRegistry(memory_home),
        ) as client:
            for tool, arguments in (
                ('memory_save', {'title': 'x', 'what': 'x', 'idempotency_key': 'not-a-uuid'}),
                ('memory_details', {'memory_id': ''}),
                ('memory_search', {'query': 'x', 'cwd': 42}),
            ):
                result = await client.call_tool(tool, arguments)
                assert result.isError is True
                assert str(root) not in result_text(result)
                assert str(memory_home) not in result_text(result)

        assert service.db.count_memories() == 0
    finally:
        service.close()
~~~

Then add this real-stdio, two-process concurrency/cross-agent test to `tests/test_integration.py`. It uses `open_stdio_client` and `call_and_store` already defined in Task 4, starts both writes before awaiting either, verifies retrieval in both directions, and checks a third project cannot see either ID:

~~~python
from pathlib import Path
import uuid

import anyio
from mcp.types import CallToolResult
import pytest

from tests.mcp_helpers import (
    call_and_store,
    decode_object,
    decode_rows,
    make_workspace,
    open_stdio_client,
)


@pytest.mark.anyio
async def test_two_stdio_agents_save_concurrently_and_share_only_one_project(tmp_path: Path) -> None:
    root = make_workspace(tmp_path / 'shared')
    other_root = make_workspace(tmp_path / 'isolated')
    memory_home = tmp_path / 'memory-home'
    results: dict[str, CallToolResult] = {}

    async with open_stdio_client(memory_home, 'cursor', root) as (cursor, cursor_stderr):
        async with open_stdio_client(memory_home, 'gemini-cli', root) as (gemini, gemini_stderr):
            async with open_stdio_client(memory_home, 'cursor', other_root) as (isolated, isolated_stderr):
                async with anyio.create_task_group() as task_group:
                    task_group.start_soon(
                        call_and_store,
                        results,
                        'cursor',
                        cursor,
                        'memory_save',
                        {
                            'title': 'CURSOR-CONCURRENT-42',
                            'what': 'cursor marker',
                            'idempotency_key': str(uuid.uuid4()),
                        },
                    )
                    task_group.start_soon(
                        call_and_store,
                        results,
                        'gemini',
                        gemini,
                        'memory_save',
                        {
                            'title': 'GEMINI-CONCURRENT-42',
                            'what': 'gemini marker',
                            'idempotency_key': str(uuid.uuid4()),
                        },
                    )

                cursor_saved = decode_object(results['cursor'])
                gemini_saved = decode_object(results['gemini'])
                cursor_found = decode_rows(await cursor.call_tool(
                    'memory_search',
                    {'query': 'GEMINI-CONCURRENT-42'},
                ))
                gemini_found = decode_rows(await gemini.call_tool(
                    'memory_search',
                    {'query': 'CURSOR-CONCURRENT-42'},
                ))
                isolated_found = decode_rows(await isolated.call_tool(
                    'memory_search',
                    {'query': 'CONCURRENT-42'},
                ))

                assert gemini_saved['id'] in {row['id'] for row in cursor_found}
                assert cursor_saved['id'] in {row['id'] for row in gemini_found}
                assert isolated_found == []
                for stderr in (cursor_stderr, gemini_stderr, isolated_stderr):
                    assert 'CURSOR-CONCURRENT-42' not in stderr.getvalue()
                    assert 'GEMINI-CONCURRENT-42' not in stderr.getvalue()
~~~

Keep these imports at the top of their respective test modules and do not introduce pytest fixtures or helper names absent from the snippets.

- [ ] **Step 2: Run protocol tests and observe uncovered failures**

Run: `uv run --extra dev pytest tests/test_mcp_protocol.py tests/test_integration.py -q`

Expected: the current server blocks its event loop during the delayed synchronous read, does not expose four tools, accepts an empty details prefix or invalid idempotency key, and/or cannot start agent/project-bound stdio processes. Capture at least one of those concrete failures before changing production code.

- [ ] **Step 3: Implement the cancellation-safe dispatch and lifecycle required by the RED tests**

Implement these exact boundaries in `src/memory/mcp_server.py`:

~~~python
from collections.abc import Callable
from typing import TypeVar


T = TypeVar('T')


async def run_with_service(
    service_factory: Callable[[], MemoryService],
    call: Callable[[MemoryService], T],
    *,
    abandon_on_cancel: bool,
) -> T:
    def invoke() -> T:
        # The SQLite connection is created, used, and closed in this same
        # worker thread. Never pass the bootstrap service's connection across
        # sqlite3's thread-affinity boundary.
        service = service_factory()
        try:
            return call(service)
        finally:
            service.close()

    return await anyio.to_thread.run_sync(
        invoke,
        abandon_on_cancel=abandon_on_cancel,
    )


async def run_read(
    service_factory: Callable[[], MemoryService],
    call: Callable[[MemoryService], T],
) -> T:
    # Retrieval is side-effect-free until the handler explicitly records
    # feedback after a successful result. Abandoning the worker is therefore
    # safe when the MCP request is cancelled.
    return await run_with_service(
        service_factory,
        call,
        abandon_on_cancel=True,
    )


async def run_save(
    service_factory: Callable[[], MemoryService],
    call: Callable[[MemoryService], dict[str, object]],
) -> dict[str, object]:
    # Do not abandon a canonical mutation. Phase 1's operation lock, journal,
    # and DB transaction finish or roll back before cancellation is re-raised.
    return await run_with_service(
        service_factory,
        call,
        abandon_on_cancel=False,
    )


async def run_server(
    *,
    agent: str | None = None,
    project_root: Path | None = None,
    startup_cwd: Path | None = None,
) -> None:
    bootstrap_service = MemoryService()
    memory_home = Path(bootstrap_service.memory_home)
    binding = MCPServerBinding(agent, project_root, startup_cwd or Path.cwd())
    registry = ProjectRegistry(memory_home)

    def worker_service_factory() -> MemoryService:
        return MemoryService(str(memory_home))

    server = _create_server(
        bootstrap_service,
        binding,
        registry,
        worker_service_factory=worker_service_factory,
    )
    try:
        async with stdio_server() as streams:
            await server.run(
                streams[0],
                streams[1],
                server.create_initialization_options(),
            )
    finally:
        bootstrap_service.close()
~~~

At `_create_server` entry, default `worker_service_factory` to `lambda: MemoryService(str(service.memory_home))`; tests can inject the blocking factory, and production passes the named factory above. Never execute a method of the bootstrap `service` in a worker thread. Bound context/search/details calls use `record_feedback=False` inside `run_read`; only after the awaited read returns and the request remains live does a second short worker-service call record the returned IDs. Thus an abandoned cancelled read cannot mutate feedback later. Bound saves validate JSON types, authority, and UUID before `run_save`; Phase 1 provides the mutation's atomicity. Do not catch AnyIO cancellation or expected project/authority exceptions in the worker helpers: the former propagates, while the latter reaches the single `project_safe_dispatch` mapping. Unexpected diagnostics contain only exception class names, never exception text or arguments.

Keep stdout protocol-clean. Context/search retain the existing limit and token-budget caps. Explicit `memory_details` is intentionally not token-capped and must round-trip the 100,000-character body. Ensure `MemoryService.close()` executes on normal EOF, initialization failure, cancellation, and server exceptions.

- [ ] **Step 4: Run phase and baseline gates**

Run: `uv run --extra dev pytest tests/test_mcp_authority.py tests/test_mcp_server.py tests/test_mcp_protocol.py tests/test_embedding_privacy.py tests/test_integration.py -q`

Expected: all bound/unbound MCP contracts pass.

Run: `uv run --extra dev pytest -q`

Expected: full Python suite passes.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/mcp_server.py tests/mcp_helpers.py tests/test_mcp_protocol.py tests/test_integration.py
git commit -m "test: verify bound mcp contracts"
~~~

## Phase Completion Gate

Run:

~~~bash
uv run --extra dev pytest -q
memory mcp --help
memory doctor --project echovault
git status --short
~~~

Expected: the suite is green, help exposes binding flags, doctor is read-only, and no query/prompt text appears in vault files, SQLite rows, claim files, or diagnostics.
