# EchoVault Local Core and Canonical Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the collision-safe, lossless, idempotent local storage core required by every Cursor and Gemini integration.

**Architecture:** Introduce small filesystem/project/merge/persistence modules around the existing MemoryService. Schema-v2 Markdown is written first as the canonical record under a per-project process lock; SQLite and vectors are rebuilt or repaired from it.

**Tech Stack:** Python 3.10–3.14 standard library, Click, SQLite/FTS5/sqlite-vec, pytest, multiprocessing, existing redaction and embedding providers.

## Global Constraints

- Preserve schema-v1 reads and all existing CLI/search/dashboard behavior while adding explicit migration for writes to incomplete v1 records.
- Never execute repository hooks or project code while discovering roots.
- Git worktrees share one project key; unrelated same-named directories do not.
- New saves use hashed project keys; adopted legacy aliases are read-only lookup aliases.
- Markdown schema v2 contains every canonical memory and operation field; SQLite and vectors are derived.
- Every mutator uses the same project lock, deterministic lock ordering, prepared atomic writes, and explicit SQLite transactions. Multi-document mutations additionally use a durable operation journal; no plan step claims that several filesystem replacements are one atomic operation.
- Agent-bound callers supply an idempotency key; legacy direct callers may omit one and receive an internally generated operation ID.
- Creator source and creation time are immutable; duplicate updates record last updater, ordered contributors, and exactly one operation record.
- All persisted text passes through existing redaction before fingerprinting or writing.
- Embedding failure never rolls back a canonical save; stale embeddings never overwrite newer content.
- Begin each behavior change with an observed failing test and keep the 272-test baseline green.

---

## File Responsibility Map

- **Create: src/memory/safe_io.py** — cross-platform process locks, prepared atomic writes, fsync, and digest compare-and-replace primitives.
- **Create: src/memory/projects.py** — root discovery, collision-safe identity, registry, scopes, and legacy aliases.
- **Create: src/memory/merge.py** — deterministic duplicate matching and field-level merge policy.
- **Create: src/memory/persistence.py** — canonical save/mutator state machines, durable multi-document operation journals, recovery, and post-commit vector writes.
- **Create: src/memory/reconcile.py** — schema-v1 enrichment and schema-v2 Markdown-to-SQLite reconciliation.
- **Modify: src/memory/models.py** — typed operation/provenance fields.
- **Modify: src/memory/markdown.py** — schema-v2 render/parse and pure document rendering.
- **Modify: src/memory/db.py** — derived columns, operation ledger, transaction-safe upserts, alias-aware queries, and conditional vectors.
- **Modify: src/memory/core.py** — delegate persistence/reconcile work while preserving public service methods.
- **Modify: src/memory/cli.py** — project alias, migration, and reconcile commands.
- **Modify: src/memory/health.py** — legacy metadata, registry ambiguity, stale temp, and index-drift diagnostics.
- **Create tests:** tests/test_safe_io.py, tests/test_projects.py, tests/test_merge.py, tests/test_persistence.py, tests/test_concurrency.py, tests/test_crash_recovery.py, tests/worker_helpers.py, tests/persistence_helpers.py.
- **Modify tests:** tests/conftest.py, tests/test_models.py, tests/test_markdown.py, tests/test_db.py, tests/test_core.py, tests/test_import.py, tests/test_cli.py.

### Task 1: Cross-Platform Lock and Prepared Atomic Write

**Files:**
- Create: src/memory/safe_io.py
- Create: tests/test_safe_io.py

**Interfaces:**
- Produces: ProcessFileLock(path: Path, timeout: float = 5.0, poll_interval: float = 0.05)
- Produces: prepare_atomic_text(path: Path, content: str, encoding: str = 'utf-8') -> PreparedAtomicWrite
- Produces: PreparedAtomicWrite.replace() and PreparedAtomicWrite.discard()
- Produces: PreparedAtomicWrite.replace_if_digest(expected_digest: str | None) -> None
- Produces: digest_file(path: Path) -> str | None
- Produces: ConcurrentModificationError and translate_windows_lock_error(error: OSError) -> OSError
- Uses: MoveFileExW with MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH on Windows

- [ ] **Step 1: Write failing lock and atomic-write tests**

~~~python
import errno
from multiprocessing import Event, Process, Queue
from pathlib import Path

import pytest

from memory.safe_io import (
    ConcurrentModificationError,
    LockTimeoutError,
    ProcessFileLock,
    digest_file,
    prepare_atomic_text,
    translate_windows_lock_error,
)


def _hold_lock(path: str, ready: Event, release: Event) -> None:
    with ProcessFileLock(Path(path), timeout=1.0):
        ready.set()
        release.wait(5.0)


def test_process_lock_times_out_while_another_process_owns_it(tmp_path: Path) -> None:
    ready = Event()
    release = Event()
    process = Process(target=_hold_lock, args=(str(tmp_path / 'project.lock'), ready, release))
    process.start()
    assert ready.wait(2.0)
    with pytest.raises(LockTimeoutError):
        with ProcessFileLock(tmp_path / 'project.lock', timeout=0.1, poll_interval=0.01):
            pass
    release.set()
    process.join(2.0)
    assert process.exitcode == 0


def test_prepared_write_is_invisible_until_replace_and_preserves_mode(tmp_path: Path) -> None:
    target = tmp_path / 'session.md'
    target.write_text('old\n', encoding='utf-8')
    target.chmod(0o640)
    prepared = prepare_atomic_text(target, 'new\n')
    assert target.read_text(encoding='utf-8') == 'old\n'
    prepared.replace()
    assert target.read_text(encoding='utf-8') == 'new\n'
    assert target.stat().st_mode & 0o777 == 0o640
    assert digest_file(target) is not None


def test_digest_compare_rejects_a_change_after_prepare(tmp_path: Path) -> None:
    target = tmp_path / 'projects.json'
    target.write_text('{"version":1}\n', encoding='utf-8')
    expected = digest_file(target)
    prepared = prepare_atomic_text(target, '{"version":2}\n')
    target.write_text('{"external":true}\n', encoding='utf-8')
    with pytest.raises(ConcurrentModificationError):
        prepared.replace_if_digest(expected)
    assert target.read_text(encoding='utf-8') == '{"external":true}\n'
    prepared.discard()


@pytest.mark.parametrize('code', [errno.EACCES, errno.EAGAIN, errno.EDEADLK])
def test_windows_contention_errors_become_retryable(code: int) -> None:
    translated = translate_windows_lock_error(OSError(code, 'locked'))
    assert isinstance(translated, BlockingIOError)


def test_windows_lock_violation_winerror_becomes_retryable() -> None:
    class WinLockError(OSError):
        winerror = 33

    error = WinLockError('lock violation')
    assert isinstance(translate_windows_lock_error(error), BlockingIOError)


def test_windows_unexpected_lock_error_is_not_retried() -> None:
    original = OSError(errno.EBADF, 'bad descriptor')
    assert translate_windows_lock_error(original) is original


def test_unexpected_acquire_error_closes_handle(tmp_path: Path, monkeypatch) -> None:
    lock = ProcessFileLock(tmp_path / 'project.lock')

    def fail() -> None:
        raise OSError(errno.EBADF, 'bad descriptor')

    monkeypatch.setattr(lock, '_acquire_once', fail)
    with pytest.raises(OSError, match='bad descriptor'):
        lock.__enter__()
    assert lock._handle is None


def test_unexpected_unlock_error_still_closes_handle(tmp_path: Path, monkeypatch) -> None:
    lock = ProcessFileLock(tmp_path / 'project.lock')
    lock.__enter__()

    def fail() -> None:
        raise OSError(errno.EIO, 'unlock failed')

    monkeypatch.setattr(lock, '_release_once', fail)
    with pytest.raises(OSError, match='unlock failed'):
        lock.__exit__(None, None, None)
    assert lock._handle is None
~~~

- [ ] **Step 2: Run the tests and observe the missing module**

Run: `uv run --extra dev pytest tests/test_safe_io.py -q`

Expected: collection fails with `ModuleNotFoundError: No module named 'memory.safe_io'`.

- [ ] **Step 3: Implement the lock and prepared write**

Use an OS-owned advisory lock: `fcntl.flock` on POSIX and a one-byte `msvcrt.locking` region on Windows. A stale path must be harmless because ownership lives in the open file descriptor.

~~~python
from __future__ import annotations

import hashlib
import errno
import os
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


class LockTimeoutError(TimeoutError):
    pass


class ConcurrentModificationError(RuntimeError):
    pass


def translate_windows_lock_error(error: OSError) -> OSError:
    retryable_errno = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
    retryable_winerror = {32, 33}  # sharing and lock violations
    if error.errno in retryable_errno or getattr(error, 'winerror', None) in retryable_winerror:
        return BlockingIOError(error.errno or errno.EACCES, str(error))
    return error


class ProcessFileLock:
    def __init__(self, path: Path, timeout: float = 5.0, poll_interval: float = 0.05):
        self.path = path
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._handle = None

    def __enter__(self) -> 'ProcessFileLock':
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open('a+b')
        self._handle.seek(0)
        if self._handle.read(1) == b'':
            self._handle.write(b'0')
            self._handle.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._acquire_once()
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self._handle.close()
                    self._handle = None
                    raise LockTimeoutError(f'Timed out acquiring lock: {self.path}')
                time.sleep(self.poll_interval)
            except BaseException:
                self._handle.close()
                self._handle = None
                raise

    def _acquire_once(self) -> None:
        assert self._handle is not None
        if os.name == 'nt':
            import msvcrt
            self._handle.seek(0)
            try:
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise translate_windows_lock_error(error)
        else:
            import fcntl
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _release_once(self) -> None:
        assert self._handle is not None
        if os.name == 'nt':
            import msvcrt
            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._handle is None:
            return
        try:
            self._release_once()
        finally:
            self._handle.close()
            self._handle = None


def _replace_and_sync(source: Path, target: Path) -> None:
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes

        move_file_ex = ctypes.WinDLL('kernel32', use_last_error=True).MoveFileExW
        move_file_ex.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move_file_ex.restype = wintypes.BOOL
        movefile_replace_existing = 0x00000001
        movefile_write_through = 0x00000008
        if not move_file_ex(
            str(source),
            str(target),
            movefile_replace_existing | movefile_write_through,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return
    os.replace(source, target)
    descriptor = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass
class PreparedAtomicWrite:
    target: Path
    temporary: Path
    original_mode: int | None

    def replace(self) -> None:
        if self.original_mode is not None:
            self.temporary.chmod(self.original_mode)
        _replace_and_sync(self.temporary, self.target)

    def replace_if_digest(self, expected_digest: str | None) -> None:
        current_digest = digest_file(self.target)
        if current_digest != expected_digest:
            raise ConcurrentModificationError(
                f'Concurrent modification of {self.target}: '
                f'expected {expected_digest!r}, got {current_digest!r}'
            )
        self.replace()

    def discard(self) -> None:
        self.temporary.unlink(missing_ok=True)


def prepare_atomic_text(path: Path, content: str, encoding: str = 'utf-8') -> PreparedAtomicWrite:
    path.parent.mkdir(parents=True, exist_ok=True)
    original_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    descriptor, temp_name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    temporary = Path(temp_name)
    try:
        with os.fdopen(descriptor, 'w', encoding=encoding, newline='') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return PreparedAtomicWrite(path, temporary, original_mode)


def digest_file(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()
~~~

`ProcessFileLock.__enter__` must close and clear its handle on every non-contention exception as well as timeout. `__exit__` must attempt unlock in a `try` block and close the handle in `finally`; an unexpected unlock error is surfaced after closing. On Windows, add a platform-gated real two-process test in addition to the pure translation tests above. Digest compare-and-replace is valid only while the caller holds the corresponding cooperative lock; every EchoVault registry/config writer must obey that precondition.

- [ ] **Step 4: Run targeted and baseline tests**

Run: `uv run --extra dev pytest tests/test_safe_io.py -q`

Expected: all safe-IO tests pass.

Run: `uv run --extra dev pytest -q`

Expected: 272 existing tests plus the new tests pass.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/safe_io.py tests/test_safe_io.py
git commit -m "feat: add crash-safe file primitives"
~~~

### Task 2: Shared Project Root and Collision-Safe Identity

**Files:**
- Create: src/memory/projects.py
- Create: tests/test_projects.py

**Interfaces:**
- Produces: ProjectCandidates, ProjectIdentity, ProjectResolutionError, MultiRootError
- Produces: select_project_candidate(candidates: ProjectCandidates) -> Path
- Produces: discover_project_root(candidate: Path) -> tuple[Path, str | None]
- Produces: build_project_identity(root: Path, marker: str | None) -> ProjectIdentity

- [ ] **Step 1: Write failing identity tests**

~~~python
from pathlib import Path

import pytest

from memory.projects import (
    MultiRootError,
    ProjectCandidates,
    build_project_identity,
    discover_project_root,
    select_project_candidate,
)


def test_nearest_marker_wins_for_nested_cwd(tmp_path: Path) -> None:
    root = tmp_path / 'workspace'
    nested = root / 'src' / 'feature'
    nested.mkdir(parents=True)
    (root / 'pyproject.toml').write_text('[project]\nname="demo"\n')
    discovered, marker = discover_project_root(nested)
    identity = build_project_identity(discovered, marker)
    assert identity.root == root.resolve()
    assert identity.display_name == 'workspace'
    assert identity.key.startswith('workspace--')
    assert len(identity.key.rsplit('--', 1)[1]) == 12


def test_same_named_roots_get_different_keys(tmp_path: Path) -> None:
    left = tmp_path / 'left' / 'api'
    right = tmp_path / 'right' / 'api'
    left.mkdir(parents=True)
    right.mkdir(parents=True)
    (left / 'package.json').write_text('{}')
    (right / 'package.json').write_text('{}')
    left_id = build_project_identity(*discover_project_root(left))
    right_id = build_project_identity(*discover_project_root(right))
    assert left_id.display_name == right_id.display_name == 'api'
    assert left_id.key != right_id.key


def test_linked_worktree_uses_common_git_owner(tmp_path: Path) -> None:
    owner = tmp_path / 'main'
    common = owner / '.git'
    worktree = tmp_path / 'feature'
    git_dir = common / 'worktrees' / 'feature'
    git_dir.mkdir(parents=True)
    worktree.mkdir()
    common.mkdir(exist_ok=True)
    (worktree / '.git').write_text(f'gitdir: {git_dir}\n')
    (git_dir / 'commondir').write_text('../..\n')
    owner_id = build_project_identity(*discover_project_root(owner))
    worktree_id = build_project_identity(*discover_project_root(worktree))
    assert worktree_id.key == owner_id.key
    assert worktree_id.display_name == 'feature'


def test_multiple_mcp_roots_require_explicit_resolution(tmp_path: Path) -> None:
    candidates = ProjectCandidates(mcp_roots=(tmp_path / 'one', tmp_path / 'two'))
    with pytest.raises(MultiRootError):
        select_project_candidate(candidates)
~~~

- [ ] **Step 2: Run the tests and observe the missing project module**

Run: `uv run --extra dev pytest tests/test_projects.py -q`

Expected: collection fails because `memory.projects` does not exist.

- [ ] **Step 3: Implement deterministic root selection and identity**

The implementation must parse `.git`, `gitdir:`, and `commondir` files directly; it must not invoke Git.

~~~python
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path


class ProjectResolutionError(ValueError):
    pass


class MultiRootError(ProjectResolutionError):
    pass


@dataclass(frozen=True)
class ProjectCandidates:
    explicit_root: Path | None = None
    mcp_roots: tuple[Path, ...] = ()
    protocol_cwd: Path | None = None
    startup_cwd: Path | None = None


@dataclass(frozen=True)
class ProjectIdentity:
    root: Path
    display_name: str
    key: str
    marker: str | None
    canonical_owner: Path


def _canonical(path: Path) -> Path:
    return Path(os.path.realpath(os.path.abspath(os.path.expanduser(str(path)))))


def select_project_candidate(candidates: ProjectCandidates) -> Path:
    if candidates.explicit_root is not None:
        return _canonical(candidates.explicit_root)
    if len(candidates.mcp_roots) > 1:
        raise MultiRootError('Automatic routing supports one MCP root per process')
    if candidates.mcp_roots:
        return _canonical(candidates.mcp_roots[0])
    if candidates.protocol_cwd is not None:
        return _canonical(candidates.protocol_cwd)
    if candidates.startup_cwd is not None:
        return _canonical(candidates.startup_cwd)
    return _canonical(Path.cwd())


def discover_project_root(candidate: Path) -> tuple[Path, str | None]:
    current = _canonical(candidate)
    if current.is_file():
        current = current.parent
    for directory in (current, *current.parents):
        if (directory / '.git').exists():
            return directory, '.git'
        if (directory / 'pyproject.toml').is_file():
            return directory, 'pyproject.toml'
        if (directory / 'package.json').is_file():
            return directory, 'package.json'
    return current, None


def _git_common_owner(root: Path) -> Path:
    dot_git = root / '.git'
    git_dir = dot_git
    if dot_git.is_file():
        prefix, value = dot_git.read_text(encoding='utf-8').split(':', 1)
        if prefix.strip() != 'gitdir':
            raise ProjectResolutionError(f'Invalid .git pointer: {dot_git}')
        git_dir = _canonical(root / value.strip())
    common_file = git_dir / 'commondir'
    common_dir = _canonical(git_dir / common_file.read_text(encoding='utf-8').strip()) if common_file.is_file() else git_dir
    return common_dir.parent if common_dir.name == '.git' else root


def _slug(value: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', value.casefold()).strip('-')
    return slug or 'project'


def build_project_identity(root: Path, marker: str | None) -> ProjectIdentity:
    canonical_root = _canonical(root)
    owner = _git_common_owner(canonical_root) if marker == '.git' else canonical_root
    identity_name = owner.name
    digest = hashlib.sha256(os.path.normcase(str(owner)).encode('utf-8')).hexdigest()[:12]
    return ProjectIdentity(
        root=canonical_root,
        display_name=canonical_root.name,
        key=f'{_slug(identity_name)}--{digest}',
        marker=marker,
        canonical_owner=owner,
    )
~~~

- [ ] **Step 4: Run targeted and baseline tests**

Run: `uv run --extra dev pytest tests/test_projects.py -q`

Expected: nested, collision, worktree, and multi-root tests pass on the current platform.

Run: `uv run --extra dev pytest -q`

Expected: full suite passes.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/projects.py tests/test_projects.py
git commit -m "feat: resolve collision-safe project identities"
~~~

### Task 3: Project Registry and Explicit Legacy Alias Adoption

**Files:**
- Modify: src/memory/projects.py
- Modify: src/memory/cli.py
- Modify: src/memory/health.py
- Modify: tests/test_projects.py
- Modify: tests/test_cli.py

**Interfaces:**
- Produces: ProjectScope(identity: ProjectIdentity, aliases: tuple[str, ...])
- Produces: ProjectRegistry(memory_home: Path).register(identity) -> ProjectScope
- Produces: ProjectRegistry.adopt_legacy(legacy_key, identity, force_reassign=False) -> ProjectScope
- Produces: ProjectRegistryConflict when the registry digest changes twice during one mutation
- Produces CLI: memory project adopt-legacy LEGACY_KEY --project-root PATH [--force-reassign]

- [ ] **Step 1: Add failing registry and CLI tests**

~~~python
def test_registry_adopts_sole_legacy_basename_once(tmp_path: Path) -> None:
    memory_home = tmp_path / '.memory'
    legacy = memory_home / 'vault' / 'api'
    legacy.mkdir(parents=True)
    first_root = tmp_path / 'one' / 'api'
    second_root = tmp_path / 'two' / 'api'
    first_root.mkdir(parents=True)
    second_root.mkdir(parents=True)
    (first_root / 'package.json').write_text('{}')
    (second_root / 'package.json').write_text('{}')
    registry = ProjectRegistry(memory_home)
    first = registry.register(build_project_identity(*discover_project_root(first_root)))
    second = registry.register(build_project_identity(*discover_project_root(second_root)))
    assert first.aliases == ('api',)
    assert second.aliases == ()


def test_adopt_legacy_refuses_reassignment_without_force(tmp_path: Path) -> None:
    registry = ProjectRegistry(tmp_path / '.memory')
    left = ProjectIdentity(tmp_path / 'left', 'left', 'left--111111111111', None, tmp_path / 'left')
    right = ProjectIdentity(tmp_path / 'right', 'right', 'right--222222222222', None, tmp_path / 'right')
    registry.adopt_legacy('legacy', left)
    with pytest.raises(LegacyAliasConflict):
        registry.adopt_legacy('legacy', right)
    scope = registry.adopt_legacy('legacy', right, force_reassign=True)
    assert scope.aliases == ('legacy',)


def test_registry_digest_cas_retries_once_from_fresh_external_state(tmp_path: Path) -> None:
    registry = ProjectRegistry(tmp_path / '.memory')
    identity = ProjectIdentity(tmp_path / 'root', 'root', 'root--111111111111', None, tmp_path / 'root')
    calls = 0

    def change_once(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            external = {
                'schema_version': 1,
                'projects': {},
                'legacy_aliases': {},
                'external_marker': 'preserve-me',
            }
            path.write_text(json.dumps(external, sort_keys=True) + '\n', encoding='utf-8')

    registry._before_replace = change_once
    registry.register(identity)
    data = json.loads(registry.path.read_text(encoding='utf-8'))
    assert calls == 2
    assert data['external_marker'] == 'preserve-me'
    assert identity.key in data['projects']


def test_registry_digest_cas_stops_after_second_conflict(tmp_path: Path) -> None:
    registry = ProjectRegistry(tmp_path / '.memory')
    identity = ProjectIdentity(tmp_path / 'root', 'root', 'root--111111111111', None, tmp_path / 'root')
    revision = 0

    def change_every_time(path: Path) -> None:
        nonlocal revision
        revision += 1
        path.write_text(
            json.dumps({'schema_version': 1, 'projects': {}, 'legacy_aliases': {}, 'revision': revision}) + '\n',
            encoding='utf-8',
        )

    registry._before_replace = change_every_time
    with pytest.raises(ProjectRegistryConflict):
        registry.register(identity)
    assert json.loads(registry.path.read_text(encoding='utf-8'))['revision'] == 2
~~~

Add a CliRunner test that invokes the exact command, then asserts `projects.json` assigns the alias to the hashed key and that a second reassignment exits non-zero without `--force-reassign`.

- [ ] **Step 2: Run the focused tests and observe missing registry symbols**

Run: `uv run --extra dev pytest tests/test_projects.py tests/test_cli.py -k "registry or adopt_legacy" -q`

Expected: imports or assertions fail because ProjectRegistry and the project command do not exist.

- [ ] **Step 3: Implement the locked registry and command**

Store only local diagnostics in `MEMORY_HOME/projects.json`:

~~~json
{
  "schema_version": 1,
  "projects": {
    "api--111111111111": {
      "display_name": "api",
      "roots": ["/local/path/api"]
    }
  },
  "legacy_aliases": {
    "api": "api--111111111111"
  }
}
~~~

Implement ProjectScope with a stable key order. Every registry mutation must read bytes and their SHA-256 while holding `projects.json.lock`, prepare the complete replacement, invoke `replace_if_digest` with the digest from that same read, and keep the lock until replacement completes. A digest conflict discards the prepared file, releases the lock, then repeats the entire read/transform/render sequence exactly once. A second conflict raises `ProjectRegistryConflict` without writing EchoVault's stale candidate. This is a real digest CAS; the lock alone or a digest computed only after rendering does not satisfy the contract.

~~~python
import json
from collections.abc import Callable

from memory.safe_io import (
    ConcurrentModificationError,
    ProcessFileLock,
    digest_file,
    prepare_atomic_text,
)


@dataclass(frozen=True)
class ProjectScope:
    identity: ProjectIdentity
    aliases: tuple[str, ...] = ()

    @property
    def storage_keys(self) -> tuple[str, ...]:
        return (self.identity.key, *self.aliases)


class LegacyAliasConflict(ProjectResolutionError):
    pass


class ProjectRegistryConflict(ProjectResolutionError):
    pass


class ProjectRegistry:
    def __init__(self, memory_home: Path):
        self.memory_home = memory_home
        self.path = memory_home / 'projects.json'
        self.lock_path = memory_home / 'projects.json.lock'
        self._before_replace: Callable[[Path], None] = lambda path: None

    def _mutate(self, transform):
        for attempt in range(2):
            with ProcessFileLock(self.lock_path):
                data, expected_digest = self._read_with_digest()
                result = transform(data)
                rendered = json.dumps(data, sort_keys=True, indent=2) + '\n'
                prepared = prepare_atomic_text(self.path, rendered)
                try:
                    self._before_replace(self.path)
                    prepared.replace_if_digest(expected_digest)
                except ConcurrentModificationError as error:
                    prepared.discard()
                    if attempt == 1:
                        raise ProjectRegistryConflict(
                            'projects.json changed during both CAS attempts'
                        ) from error
                    continue
                return result
        raise AssertionError('unreachable')

    def register(self, identity: ProjectIdentity) -> ProjectScope:
        def transform(data):
            projects = data.setdefault('projects', {})
            record = projects.setdefault(identity.key, {'display_name': identity.display_name, 'roots': []})
            root = str(identity.root)
            if root not in record['roots']:
                record['roots'].append(root)
            aliases = data.setdefault('legacy_aliases', {})
            legacy_dir = self.memory_home / 'vault' / identity.display_name
            if legacy_dir.is_dir() and identity.display_name not in aliases:
                same_name = [key for key, item in projects.items() if item['display_name'] == identity.display_name]
                if same_name == [identity.key]:
                    aliases[identity.display_name] = identity.key
            return self._scope(identity, data)
        return self._mutate(transform)
~~~

`_read_with_digest` must read the file once as bytes, return `(default_registry, None)` only when it is absent, return the parsed object plus `sha256(raw_bytes)`, and distinguish an empty or malformed existing file from absence. Empty/malformed registry state raises ProjectResolutionError and is never replaced. `_before_replace` defaults to a no-op and exists only as a deterministic CAS test seam. `adopt_legacy` uses the same `_mutate` path. Add the Click group and command using resolve_memory_home, discover_project_root, build_project_identity, and ProjectRegistry.

- [ ] **Step 4: Run registry, CLI, and health tests**

Run: `uv run --extra dev pytest tests/test_projects.py tests/test_cli.py tests/test_dashboard.py -q`

Expected: alias adoption, conflict, reassignment, and existing CLI/dashboard tests pass.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/projects.py src/memory/cli.py src/memory/health.py tests/test_projects.py tests/test_cli.py
git commit -m "feat: register projects and adopt legacy aliases"
~~~

### Task 4: Lossless Markdown Schema v2

**Files:**
- Modify: src/memory/models.py
- Modify: src/memory/markdown.py
- Modify: tests/test_models.py
- Modify: tests/test_markdown.py

**Interfaces:**
- Produces: MemoryOperation dataclass and full provenance fields on Memory
- Produces: SessionDocument.schema_version and SessionEntry.metadata_complete
- Produces: SessionEntry.to_memory(file_path: str) -> Memory for a lossless schema-v2 rebuild
- Produces: render_session_document(document, tags=None, sources=None) -> str
- Preserves: parse_session_file accepts schema v1 without rewriting it

- [ ] **Step 1: Add failing full round-trip and legacy-read tests**

~~~python
from dataclasses import asdict, replace


def test_schema_v2_round_trip_preserves_every_memory_field(tmp_path: Path, sample_memory: Memory) -> None:
    details = 'Context:\n\nExact details body.'
    sample_memory.project = 'api--111111111111'
    sample_memory.source = 'cursor'
    sample_memory.category = 'decision'
    sample_memory.tags = ['API', 'validation']
    sample_memory.related_files = ['src/api/routes.py']
    sample_memory.created_at = '2026-07-14T10:00:00+00:00'
    sample_memory.updated_at = '2026-07-14T11:00:00+00:00'
    sample_memory.status = 'archived'
    sample_memory.archived_at = '2026-07-14T11:30:00+00:00'
    sample_memory.archive_reason = 'superseded'
    sample_memory.superseded_by = 'memory-2'
    sample_memory.structured_data = {
        'constraints': ['Never expose internal models'],
        'steps': ['Validate output'],
    }
    sample_memory.confidence = 0.95
    sample_memory.valid_from = '2026-07-14'
    sample_memory.valid_until = '2027-07-14'
    sample_memory.commit_sha = 'abc123'
    sample_memory.branch = 'main'
    sample_memory.links = ['https://example.test/decision']
    sample_memory.last_verified = '2026-07-14T10:30:00+00:00'
    sample_memory.creator_source = 'cursor'
    sample_memory.last_updated_by = 'gemini-cli'
    sample_memory.contributors = ['cursor', 'gemini-cli']
    sample_memory.content_fingerprint = 'sha256:abc'
    sample_memory.history_complete = False
    sample_memory.updated_count = 1
    sample_memory.operations = [
        MemoryOperation(
            operation_id='op-1',
            source='cursor',
            action='created',
            request_fingerprint='req-1',
            timestamp='2026-07-14T10:00:00+00:00',
            branch='main',
            commit_sha='abc123',
        )
    ]
    path = Path(write_session_memory(str(tmp_path), sample_memory, '2026-07-14', details=details))
    parsed = parse_session_file(path)
    assert parsed.schema_version == 2
    entry = parsed.entries[0]
    assert entry.metadata_complete is True
    assert set(entry.metadata) == {
        'project', 'tags', 'category', 'related_files', 'section_anchor',
        'created_at', 'updated_at', 'status', 'archived_at',
        'archive_reason', 'superseded_by', 'structured_data', 'confidence',
        'valid_from', 'valid_until', 'commit_sha', 'branch', 'links',
        'last_verified', 'creator_source', 'last_updated_by', 'contributors',
        'operations', 'content_fingerprint', 'history_complete', 'updated_count',
    }
    expected = replace(
        sample_memory,
        file_path=str(path),
        section_anchor=entry.metadata['section_anchor'],
    )
    assert asdict(entry.to_memory(file_path=str(path))) == asdict(expected)
    assert entry.details == details


def test_schema_v2_metadata_comment_escapes_comment_terminators(sample_memory: Memory) -> None:
    sample_memory.structured_data = {'constraint': 'never emit -- inside comments'}
    rendered = render_session_document(document_with(sample_memory))
    metadata_line = next(
        line for line in rendered.splitlines()
        if line.startswith('<!-- echovault-metadata-v2:')
    )
    assert '-- inside' not in metadata_line
    assert '\\u002d\\u002d inside' in metadata_line


def test_schema_v1_remains_readable_but_incomplete(tmp_path: Path) -> None:
    path = tmp_path / '2026-07-14-session.md'
    path.write_text('---\nproject: legacy\ntags: [one]\n---\n\n# Session\n\n### Old\n**What:** readable\n', encoding='utf-8')
    parsed = parse_session_file(path)
    assert parsed.schema_version == 1
    assert parsed.entries[0].what == 'readable'
    assert parsed.entries[0].metadata_complete is False
~~~

- [ ] **Step 2: Run the round-trip tests and observe missing fields**

Run: `uv run --extra dev pytest tests/test_models.py tests/test_markdown.py -k "schema_v2 or schema_v1 or round_trip" -q`

Expected: failures name MemoryOperation, schema_version, metadata, or metadata_complete.

- [ ] **Step 3: Add typed provenance and canonical metadata rendering**

Add this model shape and initialize it in Memory.from_raw:

~~~python
@dataclass(frozen=True)
class MemoryOperation:
    operation_id: str
    source: Optional[str]
    action: str
    request_fingerprint: str
    timestamp: str
    branch: Optional[str] = None
    commit_sha: Optional[str] = None


@dataclass
class Memory:
    id: str
    title: str
    what: str
    why: Optional[str]
    impact: Optional[str]
    tags: list[str]
    category: Optional[str]
    project: str
    source: Optional[str]
    related_files: list[str]
    file_path: str
    section_anchor: str
    created_at: str
    updated_at: str
    status: str = 'active'
    archived_at: Optional[str] = None
    archive_reason: Optional[str] = None
    superseded_by: Optional[str] = None
    structured_data: dict = field(default_factory=dict)
    confidence: Optional[float] = None
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    commit_sha: Optional[str] = None
    branch: Optional[str] = None
    links: list[str] = field(default_factory=list)
    last_verified: Optional[str] = None
    creator_source: Optional[str] = None
    last_updated_by: Optional[str] = None
    contributors: list[str] = field(default_factory=list)
    operations: list[MemoryOperation] = field(default_factory=list)
    content_fingerprint: Optional[str] = None
    history_complete: bool = True
    updated_count: int = 0
~~~

Render frontmatter `schema_version: 2`. Add one safe single-line comment immediately after the stable memory ID:

~~~text
<!-- echovault-metadata-v2: {"branch":"main","category":"decision","contributors":["cursor"],"created_at":"2026-07-14T10:00:00+00:00"} -->
~~~

Generate canonical JSON with sorted keys and compact separators; replace every literal `--` inside the serialized JSON with two `\\u002d` escapes before placing it in the comment. The schema-v2 metadata object has exactly the key set asserted above. Stable ID comes from the ID comment; title, what, why, impact, source, and details come from the readable section; file_path comes from the parsed path. `SessionEntry.to_memory` combines those sources and rejects a schema-v2 entry when any required metadata key is absent or has the wrong type. No Memory field may fall back to an aggregate frontmatter value. Split write_session_document into pure render_session_document plus a thin write wrapper. Define `document_with` before the tests as a local fixture helper that builds one SessionDocument from the supplied Memory.

- [ ] **Step 4: Run all model and Markdown tests**

Run: `uv run --extra dev pytest tests/test_models.py tests/test_markdown.py -q`

Expected: all existing rendering behavior remains and both schema tests pass.

Run: `uv run --extra dev pytest -q`

Expected: full suite passes.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/models.py src/memory/markdown.py tests/test_models.py tests/test_markdown.py
git commit -m "feat: add lossless markdown schema v2"
~~~

### Task 5: Deterministic Duplicate Merge and Contributor Provenance

**Files:**
- Create: src/memory/merge.py
- Create: tests/test_merge.py
- Modify: src/memory/models.py
- Modify: tests/conftest.py

**Interfaces:**
- Produces: MergeContext
- Produces: is_duplicate(incoming: RawMemoryInput, project: str, candidates: list[dict], normalization_pool: list[dict]) -> dict | None
- Produces: merge_duplicate(existing: Memory, existing_details: str | None, incoming: RawMemoryInput, context: MergeContext) -> tuple[Memory, str | None]

- [ ] **Step 1: Write failing cross-agent merge tests**

First add the shared `sample_memory` fixture to `tests/conftest.py`. Module-local
fixtures in existing tests remain valid and take precedence there; this fixture
supplies the newly created test modules:

~~~python
from memory.models import Memory


@pytest.fixture
def sample_memory() -> Memory:
    return Memory(
        id='11111111-1111-4111-8111-111111111111',
        title='Use FastAPI for API endpoints',
        what='Implemented REST API using FastAPI framework',
        why='FastAPI provides automatic validation and documentation',
        impact='Reduces boilerplate code',
        tags=['api', 'fastapi'],
        category='decision',
        project='p--1',
        source='cursor',
        related_files=['/src/api/main.py'],
        file_path='2026-07-14-session.md',
        section_anchor='use-fastapi-for-api-endpoints',
        created_at='2026-07-14T10:00:00+00:00',
        updated_at='2026-07-14T10:00:00+00:00',
        creator_source='cursor',
        last_updated_by='cursor',
        contributors=['cursor'],
    )
~~~

Then create `tests/test_merge.py` with the imports and tests below:

~~~python
import pytest

from memory.merge import (
    MergeContext,
    _ordered_union_with_projection,
    _path_key,
    is_duplicate,
    merge_duplicate,
)
from memory.models import Memory, RawMemoryInput


def test_cross_agent_merge_preserves_creator_and_unions_fields(sample_memory: Memory) -> None:
    sample_memory.source = 'cursor'
    sample_memory.creator_source = 'cursor'
    sample_memory.last_updated_by = 'cursor'
    sample_memory.contributors = ['cursor']
    incoming = RawMemoryInput(
        title='Use FastAPI for API endpoints',
        what='Added explicit response models',
        why=None,
        tags=['API', 'validation'],
        related_files=['src/api/../api/routes.py'],
        constraints=['Never expose internal models'],
        details='Response models now validate outbound data.',
        source='spoofed',
    )
    context = MergeContext(
        operation_id='op-gemini',
        source='gemini-cli',
        timestamp='2026-07-14T11:00:00+00:00',
        request_fingerprint='req-gemini',
        branch='feature',
        commit_sha='def456',
    )
    merged, details = merge_duplicate(sample_memory, 'Original details', incoming, context)
    assert merged.id == sample_memory.id
    assert merged.title == sample_memory.title
    assert merged.created_at == sample_memory.created_at
    assert merged.source == merged.creator_source == 'cursor'
    assert merged.last_updated_by == 'gemini-cli'
    assert merged.contributors == ['cursor', 'gemini-cli']
    assert merged.tags == ['api', 'fastapi', 'validation']
    assert merged.related_files == ['/src/api/main.py', 'src/api/routes.py']
    assert merged.structured_data['constraints'] == ['Never expose internal models']
    assert merged.why == sample_memory.why
    assert merged.what == 'Added explicit response models'
    assert details is not None and 'op-gemini' in details and 'Original details' in details
    assert len(merged.operations) == len(sample_memory.operations) + 1


def test_empty_collections_and_null_scalars_do_not_clear(sample_memory: Memory) -> None:
    incoming = RawMemoryInput(title=sample_memory.title, what='new', tags=[], related_files=[])
    context = MergeContext('op-2', 'codex', '2026-07-14T12:00:00+00:00', 'req-2')
    merged, _ = merge_duplicate(sample_memory, None, incoming, context)
    assert merged.tags == sample_memory.tags
    assert merged.related_files == sample_memory.related_files
    assert merged.why == sample_memory.why


@pytest.mark.parametrize(
    ('top_score', 'expected_id'),
    [(7.0, 'same-project'), (6.999, None)],
)
def test_duplicate_threshold_and_title_contract(top_score: float, expected_id: str | None) -> None:
    incoming = RawMemoryInput(title=' Straße ', what='routing')
    same_project = [
        {'id': 'same-project', 'project': 'p--1', 'title': 'STRASSE', 'score': top_score},
    ]
    normalization_pool = [
        *same_project,
        {'id': 'foreign', 'project': 'other--2', 'title': 'Straße', 'score': 10.0},
    ]
    result = is_duplicate(incoming, 'p--1', same_project, normalization_pool)
    assert (result or {}).get('id') == expected_id


def test_projection_precedes_normalization_and_preserves_first_value() -> None:
    assert _ordered_union_with_projection(
        [' src\\api\\../api/routes.py '],
        ['src/api/routes.py', 'src/api/models.py'],
        lambda value: value.casefold(),
        _path_key,
    ) == ['src/api/routes.py', 'src/api/models.py']


def test_only_highest_same_project_fts_row_can_match() -> None:
    incoming = RawMemoryInput(title='Exact title', what='body')
    candidates = [
        {'id': 'first', 'project': 'p--1', 'title': 'Different title', 'score': 10.0},
        {'id': 'second', 'project': 'p--1', 'title': 'Exact title', 'score': 9.0},
    ]
    assert is_duplicate(incoming, 'p--1', candidates, candidates) is None


def test_equal_score_tie_preserves_fts_order() -> None:
    incoming = RawMemoryInput(title='Exact title', what='body')
    candidates = [
        {'id': 'first', 'project': 'p--1', 'title': 'Exact title', 'score': 10.0},
        {'id': 'second', 'project': 'p--1', 'title': 'Exact title', 'score': 10.0},
    ]
    assert is_duplicate(incoming, 'p--1', candidates, candidates)['id'] == 'first'
~~~

- [ ] **Step 2: Run tests and observe the missing merge module**

Run: `uv run --extra dev pytest tests/test_merge.py -q`

Expected: collection fails because `memory.merge` does not exist.

- [ ] **Step 3: Implement the pure merge policy**

~~~python
from __future__ import annotations

import copy
import math
import posixpath
from dataclasses import dataclass, replace

from memory.models import Memory, MemoryOperation, RawMemoryInput


@dataclass(frozen=True)
class MergeContext:
    operation_id: str
    source: str | None
    timestamp: str
    request_fingerprint: str
    branch: str | None = None
    commit_sha: str | None = None


def _ordered_union_with_projection(
    existing: list[str],
    incoming: list[str],
    normalizer,
    projector,
) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw_value in (*existing, *incoming):
        projected = projector(raw_value)
        if not projected:
            continue
        key = normalizer(projected)
        if key in seen:
            continue
        result.append(projected)
        seen.add(key)
    return result


def _ordered_union(existing: list[str], incoming: list[str], normalizer) -> list[str]:
    return _ordered_union_with_projection(
        existing,
        incoming,
        normalizer,
        lambda value: value.strip(),
    )


def _path_key(value: str) -> str:
    return posixpath.normpath(value.strip().replace('\\', '/'))


def is_duplicate(
    incoming: RawMemoryInput,
    project: str,
    candidates: list[dict],
    normalization_pool: list[dict],
) -> dict | None:
    same_project = [item for item in candidates if item['project'] == project]
    if not same_project:
        return None
    top = same_project[0]  # MemoryDB.fts_search already orders descending.
    raw_scores = [float(item['score']) for item in normalization_pool]
    if not raw_scores or any(not math.isfinite(score) for score in raw_scores):
        return None
    top_score = float(top['score'])
    if not math.isfinite(top_score):
        return None
    scores = [max(0.0, score) for score in raw_scores]
    maximum = max(scores, default=0.0)
    normalized = max(0.0, top_score) / maximum if maximum > 0.0 else 0.0
    title_matches = incoming.title.strip().casefold() == str(top['title']).strip().casefold()
    return top if normalized >= 0.7 and title_matches else None


def merge_duplicate(
    existing: Memory,
    existing_details: str | None,
    incoming: RawMemoryInput,
    context: MergeContext,
) -> tuple[Memory, str | None]:
    merged = copy.deepcopy(existing)
    for name in ('what', 'why', 'impact', 'category', 'confidence', 'valid_from', 'valid_until', 'commit_sha', 'branch', 'last_verified'):
        value = getattr(incoming, name)
        if value is not None:
            setattr(merged, name, value)
    merged.tags = _ordered_union(merged.tags, incoming.tags, lambda value: value.casefold())
    merged.related_files = _ordered_union_with_projection(
        merged.related_files,
        incoming.related_files,
        _path_key,
        _path_key,
    )
    merged.links = _ordered_union(merged.links, incoming.links, lambda value: value)
    structured = copy.deepcopy(merged.structured_data)
    for name in ('triggers', 'prerequisites', 'steps', 'verification', 'follow_ups', 'constraints', 'alternatives_rejected', 'open_questions'):
        structured[name] = _ordered_union(structured.get(name, []), getattr(incoming, name), lambda value: value)
    merged.structured_data = structured
    merged.updated_at = context.timestamp
    merged.last_updated_by = context.source
    if context.source and context.source not in merged.contributors:
        merged.contributors.append(context.source)
    merged.updated_count += 1
    merged.operations.append(MemoryOperation(
        operation_id=context.operation_id,
        source=context.source,
        action='updated',
        request_fingerprint=context.request_fingerprint,
        timestamp=context.timestamp,
        branch=context.branch,
        commit_sha=context.commit_sha,
    ))
    details = existing_details
    if incoming.details:
        header = f'--- update {context.timestamp} by {context.source or "unknown"} op {context.operation_id} ---'
        details = '\n\n'.join(part for part in (existing_details, header, incoming.details) if part)
    return merged, details
~~~

The persistence caller builds `query = f'{incoming.title} {incoming.what}'`, obtains `candidates = db.fts_search(query, limit=5, project=project)`, and uses those ordered rows as the only selectable matches. To preserve the existing score contract, `normalization_pool` is `candidates` unless exactly one same-project row exists; in that case it is `db.fts_search(query, limit=5) or candidates`. A foreign-project row may therefore influence the denominator but can never be selected. Scores are clamped at zero, the highest same-project row is divided by the maximum pool score, `>= 0.7` is inclusive, and title equality is trim plus Unicode `casefold()`. Empty, zero, NaN, or infinite scores are rejected before division. Tests pin the exact 0.7 boundary, one-result broad normalization, FTS order/ties, a foreign matching title that must never be returned, and Unicode case-folding. Projection always happens before normalization for both existing and incoming collection values; this preserves the first cleaned spelling/path while preventing lexically equivalent duplicates.

- [ ] **Step 4: Run merge and model tests**

Run: `uv run --extra dev pytest tests/test_merge.py tests/test_models.py -q`

Expected: pure merge tests pass with immutable creator provenance.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/merge.py src/memory/models.py tests/conftest.py tests/test_merge.py
git commit -m "feat: merge duplicate memories deterministically"
~~~

### Task 6: Derived SQLite Projection and Operation Ledger

**Files:**
- Modify: src/memory/db.py
- Modify: tests/test_db.py

**Interfaces:**
- Produces: MemoryDB.transaction()
- Produces: MemoryDB.upsert_memory(memory: Memory, details: str | None) -> int
- Produces: MemoryDB.get_operation(project: str, operation_id: str) -> dict | None
- Produces: MemoryDB.upsert_operation(project: str, memory_id: str, operation: MemoryOperation) -> None
- Produces: MemoryDB.invalidate_vector(memory_id: str) -> None
- Produces: MemoryDB.has_vector(memory_id: str) -> bool
- Produces: MemoryDB.upsert_vector_if_current(memory_id, expected_fingerprint, embedding) -> bool

- [ ] **Step 1: Add failing transaction, ledger, and vector-CAS tests**

~~~python
from dataclasses import replace
from queue import Queue
import threading


def test_transaction_rolls_back_memory_and_operation(db: MemoryDB, sample_memory: Memory) -> None:
    operation = MemoryOperation('op-1', 'cursor', 'created', 'req-1', '2026-07-14T10:00:00+00:00')
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.upsert_memory(sample_memory, 'details')
            db.upsert_operation(sample_memory.project, sample_memory.id, operation)
            raise RuntimeError('inject rollback')
    assert db.get_memory(sample_memory.id) is None
    assert db.get_operation(sample_memory.project, operation.operation_id) is None


def test_operation_id_is_unique_within_project_only(db: MemoryDB, sample_memory: Memory) -> None:
    operation = MemoryOperation('same-op', 'cursor', 'created', 'req-1', '2026-07-14T10:00:00+00:00')
    with db.transaction():
        db.upsert_memory(sample_memory, None)
        db.upsert_operation('one', sample_memory.id, operation)
        db.upsert_operation('two', sample_memory.id, operation)
    assert db.get_operation('one', 'same-op') is not None
    assert db.get_operation('two', 'same-op') is not None


def test_old_embedding_cannot_replace_new_fingerprint(db: MemoryDB, sample_memory: Memory) -> None:
    sample_memory.content_fingerprint = 'new'
    with db.transaction():
        db.upsert_memory(sample_memory, None)
    assert db.upsert_vector_if_current(sample_memory.id, 'old', [0.1, 0.2]) is False
    assert db.upsert_vector_if_current(sample_memory.id, 'new', [0.1, 0.2]) is True
    assert db.has_vector(sample_memory.id) is True


def test_vector_cas_check_delete_insert_is_one_write_transaction(
    tmp_path: Path, sample_memory: Memory,
) -> None:
    db_path = tmp_path / 'index.db'
    bootstrap_db = MemoryDB(db_path)
    sample_memory.content_fingerprint = 'first'
    with bootstrap_db.transaction():
        bootstrap_db.upsert_memory(sample_memory, None)
    bootstrap_db.close()
    fingerprint_read = threading.Event()
    allow_vector_write = threading.Event()
    update_finished = threading.Event()
    cas_result: Queue[bool] = Queue()

    def run_cas() -> None:
        embedding_db = MemoryDB(db_path)
        try:
            embedding_db._vector_cas_test_barrier = lambda: (
                fingerprint_read.set(), allow_vector_write.wait(timeout=2.0)
            )
            cas_result.put(embedding_db.upsert_vector_if_current(
                sample_memory.id, 'first', [0.1, 0.2]
            ))
        finally:
            embedding_db.close()

    cas = threading.Thread(target=run_cas)
    cas.start()
    assert fingerprint_read.wait(timeout=2.0)

    def update_to_second() -> None:
        updater_db = MemoryDB(db_path)
        try:
            changed = replace(sample_memory, content_fingerprint='second')
            with updater_db.transaction():
                updater_db.upsert_memory(changed, None)
                updater_db.invalidate_vector(changed.id)
            update_finished.set()
        finally:
            updater_db.close()

    updater = threading.Thread(target=update_to_second)
    updater.start()
    assert update_finished.wait(timeout=0.05) is False
    allow_vector_write.set()
    cas.join(timeout=2.0)
    updater.join(timeout=2.0)
    assert not cas.is_alive()
    assert not updater.is_alive()
    assert cas_result.get_nowait() is True
    verification_db = MemoryDB(db_path)
    try:
        assert verification_db.get_memory(sample_memory.id)['content_fingerprint'] == 'second'
        assert verification_db.has_vector(sample_memory.id) is False
    finally:
        verification_db.close()
~~~

- [ ] **Step 2: Run DB tests and observe missing methods**

Run: `uv run --extra dev pytest tests/test_db.py -k "transaction or operation_id or fingerprint" -q`

Expected: failures identify the missing transaction, operation, and conditional-vector APIs.

- [ ] **Step 3: Add additive columns, ledger, and explicit transaction control**

Initialize connections with `PRAGMA foreign_keys=ON`, `PRAGMA journal_mode=WAL`, and a bounded `PRAGMA busy_timeout`. Add these derived columns without rebuilding existing databases:

~~~sql
ALTER TABLE memories ADD COLUMN creator_source TEXT;
ALTER TABLE memories ADD COLUMN last_updated_by TEXT;
ALTER TABLE memories ADD COLUMN contributors TEXT DEFAULT '[]';
ALTER TABLE memories ADD COLUMN operation_history TEXT DEFAULT '[]';
ALTER TABLE memories ADD COLUMN content_fingerprint TEXT;
ALTER TABLE memories ADD COLUMN history_complete INTEGER DEFAULT 1;
~~~

Create the ledger:

~~~sql
CREATE TABLE IF NOT EXISTS save_operations (
    project TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    action TEXT NOT NULL,
    source TEXT,
    timestamp TEXT NOT NULL,
    branch TEXT,
    commit_sha TEXT,
    PRIMARY KEY (project, operation_id)
);
CREATE INDEX IF NOT EXISTS save_operations_memory_id
ON save_operations(memory_id);
~~~

Implement a transaction context that owns the only commit for grouped writes:

~~~python
from contextlib import contextmanager


@contextmanager
def transaction(self):
    if self.conn.in_transaction:
        raise RuntimeError('Nested MemoryDB transactions are not supported')
    self.conn.execute('BEGIN IMMEDIATE')
    try:
        yield self
    except BaseException:
        self.conn.rollback()
        raise
    else:
        self.conn.commit()
~~~

Refactor insert_memory, update_memory, detail replacement, delete_memory, vector invalidation, and metadata setters so the new internal variants never commit. Preserve the existing public wrappers by opening a transaction only when no transaction is active.

For vector compare-and-swap, open one `BEGIN IMMEDIATE` transaction, read the current memory fingerprint, return false on mismatch, delete the old vector, insert the replacement, and commit. No commit or lock release occurs between the fingerprint comparison and vector insert. A concurrent canonical update therefore waits, then changes the fingerprint and invalidates the just-written old vector in its own transaction. Expose a private callable `_vector_cas_test_barrier` initialized to `None` and invoke it immediately after the fingerprint read only so the barrier race above can prove the transaction boundary; it is never configurable through production APIs.

- [ ] **Step 4: Run DB and full Python suites**

Run: `uv run --extra dev pytest tests/test_db.py -q`

Expected: DB migration, rollback, uniqueness, and vector-CAS tests pass.

Run: `uv run --extra dev pytest -q`

Expected: full suite passes without transaction errors.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/db.py tests/test_db.py
git commit -m "feat: add derived operation ledger"
~~~

### Task 7: Canonical Save State Machine and Idempotent Replay

**Files:**
- Create: src/memory/persistence.py
- Create: tests/test_persistence.py
- Modify: src/memory/core.py
- Modify: src/memory/markdown.py
- Modify: src/memory/redaction.py
- Modify: src/memory/db.py
- Modify: tests/conftest.py

**Interfaces:**
- Produces: SaveRequest and SaveConflict
- Produces: LegacyMetadataRequiredError
- Produces: redact_memory_input(raw, patterns) -> RawMemoryInput
- Produces: request_fingerprint(raw, project, source) -> str
- Produces: embedding_text(*, title, what, why, impact, tags) -> str
- Produces: content_fingerprint(memory: Memory) -> str
- Produces: CanonicalPersistence.save(request: SaveRequest) -> dict[str, object]
- Changes: MemoryService.save(raw, project=None, *, authoritative_source=None, idempotency_key=None)

- [ ] **Step 1: Write failing create/update/replay and crash-boundary tests**

Extend `tests/conftest.py` with a shared service fixture. It depends on the
existing `env_home` fixture so the deterministic fake embedding provider stays
active for the complete service lifetime:

~~~python
from pathlib import Path

from memory.core import MemoryService


@pytest.fixture
def service(env_home: Path):
    instance = MemoryService(str(env_home))
    try:
        yield instance
    finally:
        instance.db.close()
~~~

Create `tests/test_persistence.py` with these complete imports followed by the
tests below:

~~~python
import copy
from datetime import date
import hashlib
import json
from pathlib import Path

import pytest

from memory.core import MemoryService
from memory.markdown import parse_session_file
from memory.models import RawMemoryInput
from memory.persistence import (
    LegacyMetadataRequiredError,
    SaveConflict,
    content_fingerprint,
    embedding_text,
)
~~~

~~~python
def test_same_operation_and_payload_replays_without_second_update(service: MemoryService) -> None:
    raw = RawMemoryInput(title='Stable decision', what='Use one canonical writer', source='spoofed')
    created = service.save(
        raw,
        project='project--111111111111',
        authoritative_source='cursor',
        idempotency_key='11111111-1111-4111-8111-111111111111',
    )
    replayed = service.save(
        raw,
        project='project--111111111111',
        authoritative_source='cursor',
        idempotency_key='11111111-1111-4111-8111-111111111111',
    )
    record = service.get_memory_record(created['id'])
    assert replayed == {**created, 'action': 'replayed'}
    assert record['updated_count'] == 0
    assert json.loads(record['contributors']) == ['cursor']
    assert len(json.loads(record['operation_history'])) == 1


def test_reused_operation_with_different_payload_conflicts(service: MemoryService) -> None:
    key = '22222222-2222-4222-8222-222222222222'
    service.save(RawMemoryInput(title='One', what='first'), project='p--1', idempotency_key=key)
    with pytest.raises(SaveConflict):
        service.save(RawMemoryInput(title='One', what='different'), project='p--1', idempotency_key=key)


def test_duplicate_update_rewrites_historical_markdown(service: MemoryService) -> None:
    first = service.save(RawMemoryInput(title='Decision', what='old', tags=['one']), project='p--1')
    before_fingerprint = service.get_memory_record(first['id'])['content_fingerprint']
    second = service.save(
        RawMemoryInput(title='Decision', what='new', tags=['two']),
        project='p--1',
        authoritative_source='gemini-cli',
        idempotency_key='33333333-3333-4333-8333-333333333333',
    )
    assert second['id'] == first['id']
    content = Path(first['file_path']).read_text(encoding='utf-8')
    assert '**What:** new' in content
    assert '"last_updated_by":"gemini-cli"' in content
    record = service.get_memory_record(first['id'])
    assert record['content_fingerprint'] != before_fingerprint
    entry = parse_session_file(Path(first['file_path'])).entries[0]
    assert entry.metadata['content_fingerprint'] == record['content_fingerprint']
    assert record['content_fingerprint'] == content_fingerprint(entry.to_memory(first['file_path']))


def test_input_object_is_not_mutated_by_redaction(service: MemoryService) -> None:
    raw = RawMemoryInput(title='token sk-live-secret', what='token sk-live-secret', links=['https://x.test/?key=sk-live-secret'])
    original = copy.deepcopy(raw)
    service.save(raw, project='p--1')
    assert raw == original


def test_write_targeting_v1_file_requires_explicit_migration(service: MemoryService) -> None:
    legacy_file = (
        Path(service.vault_dir)
        / 'legacy'
        / f'{date.today().isoformat()}-session.md'
    )
    legacy_file.parent.mkdir(parents=True)
    legacy_file.write_text(
        '---\nproject: legacy\n---\n\n'
        '# Session\n\n### Legacy\n**What:** old\n',
        encoding='utf-8',
    )
    before = legacy_file.read_bytes()
    with pytest.raises(
        LegacyMetadataRequiredError,
        match='memory migrate vault-metadata',
    ):
        service.save(
            RawMemoryInput(title='New', what='must not rewrite v1'),
            project='legacy',
        )
    assert legacy_file.read_bytes() == before


def test_content_fingerprint_hashes_exact_embedding_bytes(service: MemoryService) -> None:
    saved = service.save(
        RawMemoryInput(
            title='Vector contract',
            what='stable bytes',
            why='recovery',
            impact='no stale vector',
            tags=['one', 'two'],
        ),
        project='p--1',
    )
    memory = service.get_memory_record(saved['id'])
    text = embedding_text(
        title=memory['title'],
        what=memory['what'],
        why=memory['why'],
        impact=memory['impact'],
        tags=json.loads(memory['tags']),
    )
    expected = 'sha256:' + hashlib.sha256(
        text.encode('utf-8')
    ).hexdigest()
    assert memory['content_fingerprint'] == expected
~~~

- [ ] **Step 2: Run persistence tests and observe missing save parameters**

Run: `uv run --extra dev pytest tests/test_persistence.py -q`

Expected: MemoryService.save rejects authoritative_source/idempotency_key or fails the Markdown/provenance assertions.

- [ ] **Step 3: Implement the prepared-write state machine**

Define the immutable request:

~~~python
from collections.abc import Sequence


@dataclass(frozen=True)
class SaveRequest:
    raw: RawMemoryInput
    project: str
    source: str | None
    operation_id: str
    timestamp: str


class SaveConflict(ValueError):
    pass


class CanonicalDriftError(RuntimeError):
    pass


class LegacyMetadataRequiredError(RuntimeError):
    pass


def embedding_text(
    *,
    title: str,
    what: str,
    why: str | None,
    impact: str | None,
    tags: Sequence[str],
) -> str:
    fields = (
        title,
        what,
        why or '',
        impact or '',
        ' '.join(tags),
    )
    return ' '.join(value.strip() for value in fields if value.strip())


def content_fingerprint(memory: Memory) -> str:
    payload = embedding_text(
        title=memory.title,
        what=memory.what,
        why=memory.why,
        impact=memory.impact,
        tags=memory.tags,
    ).encode('utf-8')
    return 'sha256:' + hashlib.sha256(payload).hexdigest()
~~~

redact_memory_input must deep-copy and redact title, what, why, impact, details, every list item, branch, commit SHA, dates, and links. Compute the request fingerprint from compact sorted JSON of those redacted normalized fields plus authoritative project and source. Never hash the unredacted request.

Implement this exact order under `MEMORY_HOME/locks/<project>.lock`:

1. Re-read Markdown and the derived ledger.
2. If Markdown contains the operation with the same fingerprint, upsert its
   canonical memory/details projection and any missing ledger row in one SQLite
   transaction, then return replayed.
3. If the operation exists only in SQLite, raise CanonicalDriftError.
4. If the operation ID exists with another fingerprint, raise SaveConflict.
5. Detect duplicates and apply merge_duplicate, or create a new Memory and created operation.
6. Refuse the operation with LegacyMetadataRequiredError if any target document is schema v1. After all create/merge fields and operation history are final, compute `embedding_text` exactly once, compute `content_fingerprint` from those captured UTF-8 bytes, assign it to the Memory, and only then render Markdown or upsert SQLite. Never retain an incoming or previous fingerprint after content changes.
7. Render the full schema-v2 document and call prepare_atomic_text before opening SQLite transaction.
8. In one DB transaction, upsert memory/details/operation and invalidate the old vector.
9. Replace Markdown and fsync its directory.
10. Commit SQLite and release the project lock.
11. Outside the lock, embed the captured embedding_text bytes and call upsert_vector_if_current with the same content_fingerprint.

Expose a named fault callback in CanonicalPersistence solely for deterministic tests:

~~~python
FaultCallback = Callable[[str], None]


class CanonicalPersistence:
    def __init__(self, memory_home: Path, db: MemoryDB, patterns: list[str], fault: FaultCallback | None = None):
        self.memory_home = memory_home
        self.vault_dir = memory_home / 'vault'
        self.db = db
        self.patterns = patterns
        self.fault = fault or (lambda phase: None)
~~~

Call it at `after_temp_fsync`, `after_db_write`, `after_markdown_replace`, `after_db_commit`, and `before_vector_write`. Every exception path discards an uncommitted temporary file and rolls back an open transaction.

MemoryService.save keeps its old positional contract and constructs an internal UUID when idempotency_key is absent. It passes authoritative_source instead of trusting raw.source when provided.

- [ ] **Step 4: Run persistence, core, Markdown, and DB tests**

Run: `uv run --extra dev pytest tests/test_persistence.py tests/test_core.py tests/test_markdown.py tests/test_db.py -q`

Expected: created, updated, replayed, conflict, redaction, and historical-file tests pass.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/persistence.py src/memory/core.py src/memory/markdown.py src/memory/redaction.py src/memory/db.py tests/conftest.py tests/test_persistence.py
git commit -m "feat: persist memories idempotently"
~~~

### Task 8: Canonical Update, Archive, Restore, Merge, and Delete

**Files:**
- Modify: src/memory/persistence.py
- Modify: src/memory/core.py
- Modify: src/memory/cli.py
- Modify: src/memory/dashboard_old.py
- Modify: src/memory/health.py
- Modify: src/memory/safe_io.py
- Modify: tests/test_core.py
- Create: tests/test_canonical_mutations.py

**Interfaces:**
- Produces: UNSET sentinel and MemoryPatch
- Produces: CanonicalPersistence.update, archive, restore, merge, delete
- Produces: JournalTarget, OperationJournal, JournalRecoveryConflict
- Produces: persist_operation_journal(memory_home: Path, operation: OperationJournal) -> Path
- Produces: acquire_project_locks(memory_home: Path, project_keys: Sequence[str], lock_factory=ProcessFileLock)
- Produces: CanonicalPersistence.recover_pending_operations(project_keys: Sequence[str]) -> list[str]
- Produces: CanonicalPersistence.startup_recoveries: tuple[str, ...]
- Produces: memory admin apply --json-stdin for trusted local UI bridging
- Produces JSON: {"status": ACTION, "memory_id": ID} for every successful action

- [ ] **Step 1: Write failing canonical-mutation tests**

~~~python
import json
from pathlib import Path

from memory.core import MemoryService
from memory.markdown import parse_session_file
from memory.models import RawMemoryInput
from memory.persistence import (
    JournalTarget,
    MemoryPatch,
    OperationJournal,
    content_fingerprint,
    persist_operation_journal,
)
from memory.safe_io import prepare_atomic_text


def test_delete_removes_markdown_and_derived_rows(service: MemoryService) -> None:
    saved = service.save(RawMemoryInput(title='Delete me', what='gone'), project='p--1')
    assert service.delete(saved['id']) is True
    assert saved['id'] not in Path(saved['file_path']).read_text(encoding='utf-8')
    assert service.get_memory_record(saved['id']) is None


def test_archive_and_restore_update_both_stores(service: MemoryService) -> None:
    saved = service.save(RawMemoryInput(title='Lifecycle', what='state'), project='p--1')
    service.archive_memory(saved['id'], reason='reviewed')
    assert '**Archived:**' in Path(saved['file_path']).read_text(encoding='utf-8')
    assert service.get_memory_record(saved['id'])['status'] == 'archived'
    service.restore_memory(saved['id'])
    assert service.get_memory_record(saved['id'])['status'] == 'active'
    assert '**Archived:**' not in Path(saved['file_path']).read_text(encoding='utf-8')


def test_admin_patch_distinguishes_omitted_clear_and_replace(service: MemoryService) -> None:
    saved = service.save(RawMemoryInput(title='Patch', what='old', why='keep', impact='clear'), project='p--1')
    service.update_memory_record(
        saved['id'],
        patch=MemoryPatch(title='Renamed', what='new', impact=None, tags=[]),
        actor='dashboard',
    )
    record = service.get_memory_record(saved['id'])
    assert record['title'] == 'Renamed'
    assert record['why'] == 'keep'
    assert record['impact'] is None
    assert json.loads(record['tags']) == []
    assert record['source'] == record['creator_source']


def test_admin_update_recomputes_exact_fingerprint_before_both_writes(service: MemoryService) -> None:
    saved = service.save(RawMemoryInput(title='Patch', what='old', tags=['one']), project='p--1')
    before = service.get_memory_record(saved['id'])['content_fingerprint']
    service.update_memory_record(
        saved['id'],
        patch=MemoryPatch(what='new', tags=['two']),
        actor='dashboard',
    )
    row = service.get_memory_record(saved['id'])
    entry = parse_session_file(Path(saved['file_path'])).entries[0]
    rebuilt = entry.to_memory(saved['file_path'])
    assert row['content_fingerprint'] != before
    assert row['content_fingerprint'] == entry.metadata['content_fingerprint']
    assert row['content_fingerprint'] == content_fingerprint(rebuilt)


def test_operation_journal_is_durable_and_contains_no_memory_text(tmp_path: Path) -> None:
    memory_home = tmp_path / '.memory'
    targets = []
    for index in range(3):
        target = memory_home / 'vault' / 'p--1' / f'2026-07-{index + 10}-session.md'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f'old-{index}\n', encoding='utf-8')
        prepared = prepare_atomic_text(target, f'new-{index}\n')
        targets.append(JournalTarget.from_prepared(memory_home, prepared))
    operation = OperationJournal(
        schema_version=1,
        operation_id='55555555-5555-4555-8555-555555555555',
        action='merge',
        project_keys=('p--1',),
        affected_memory_ids=('canonical-id', 'source-1', 'source-2'),
        canonical_memory_id='canonical-id',
        targets=tuple(targets),
        created_at='2026-07-14T10:00:00+00:00',
    )
    journal_path = persist_operation_journal(memory_home, operation)
    journal = json.loads(journal_path.read_text(encoding='utf-8'))
    assert journal['action'] == 'merge'
    assert journal['affected_memory_ids'] == ['canonical-id', 'source-1', 'source-2']
    assert len(journal['targets']) == 3
    assert all(set(item) == {'target', 'temporary', 'before_sha256', 'after_sha256'} for item in journal['targets'])
    serialized = json.dumps(journal)
    assert 'canonical body' not in serialized
    assert 'source body' not in serialized
    assert [target.read_text(encoding='utf-8') for target in sorted((memory_home / 'vault' / 'p--1').glob('*.md'))] == [
        'old-0\n', 'old-1\n', 'old-2\n',
    ]
~~~

- [ ] **Step 2: Run mutation tests and observe Markdown drift**

Run: `uv run --extra dev pytest tests/test_canonical_mutations.py -q`

Expected: delete leaves the memory ID in Markdown, or MemoryPatch is missing.

- [ ] **Step 3: Route every mutator through one coordinator**

~~~python
class _Unset:
    pass


UNSET = _Unset()


@dataclass(frozen=True)
class MemoryPatch:
    title: str | _Unset = UNSET
    what: str | _Unset = UNSET
    why: str | None | _Unset = UNSET
    impact: str | None | _Unset = UNSET
    category: str | None | _Unset = UNSET
    tags: list[str] | _Unset = UNSET
    details: str | None | _Unset = UNSET


@dataclass(frozen=True)
class JournalTarget:
    target: str
    temporary: str
    before_sha256: str | None
    after_sha256: str

    @classmethod
    def from_prepared(cls, memory_home: Path, prepared: PreparedAtomicWrite) -> 'JournalTarget':
        root = memory_home.resolve()
        after_sha256 = digest_file(prepared.temporary)
        if after_sha256 is None:
            raise FileNotFoundError(prepared.temporary)
        return cls(
            target=prepared.target.resolve().relative_to(root).as_posix(),
            temporary=prepared.temporary.resolve().relative_to(root).as_posix(),
            before_sha256=digest_file(prepared.target),
            after_sha256=after_sha256,
        )


@dataclass(frozen=True)
class OperationJournal:
    schema_version: int
    operation_id: str
    action: str
    project_keys: tuple[str, ...]
    affected_memory_ids: tuple[str, ...]
    canonical_memory_id: str | None
    targets: tuple[JournalTarget, ...]
    created_at: str


class JournalRecoveryConflict(RuntimeError):
    pass


def persist_operation_journal(memory_home: Path, operation: OperationJournal) -> Path:
    journal_path = memory_home / 'transactions' / f'{operation.operation_id}.json'
    payload = json.dumps(asdict(operation), sort_keys=True, separators=(',', ':')) + '\n'
    prepared = prepare_atomic_text(journal_path, payload)
    prepared.replace()
    return journal_path
~~~

An omitted field preserves its value; None explicitly clears nullable scalars; an empty list explicitly clears a collection. Do not expose source/creator_source in MemoryPatch. Each mutation gets a generated operation ID, uses the project lock, rewrites canonical Markdown through a prepared write, appends actor/contributor/history metadata, updates derived rows in one transaction, and conditionally re-embeds changed active content. After applying every create/update/merge/lifecycle patch in memory, recompute each affected Memory's fingerprint with Task 7's exact function before rendering or DB upsert. Archive/delete invalidates the affected vector; restore re-embeds active content; update/merge re-embed only when the exact fingerprint changed. A source archived by merge remains in Markdown with its correctly recomputed fingerprint but has no active vector.

For a multi-document mutation, acquire sorted project locks and refuse cross-project merges. `acquire_project_locks` validates each key as one filename-safe project key, deduplicates with `sorted(set(project_keys))`, and enters `MEMORY_HOME/locks/<key>.lock` through an ExitStack so release is reverse order; `lock_factory` is a test seam. Update the canonical memory first, archive sources with superseded_by, then use journal-coordinated, individually atomic file replacements. Never describe the group as one filesystem-atomic replace.

The durable journal lives at `MEMORY_HOME/transactions/<operation-id>.json`. All paths inside it are normalized paths relative to MEMORY_HOME; loading rejects absolute paths, `..`, duplicate targets, an operation ID that differs from the filename, unsorted/duplicate project keys, and unknown fields. It contains action/IDs/project keys and target digests only—never titles, memory bodies, details, prompts, or credentials. The exact write order under all sorted project locks is:

1. Recover or fail on any existing journal that intersects the locked project keys.
2. Render every final schema-v2 document, call `prepare_atomic_text` for each, record each target/temp relative path plus the target's before digest and prepared temp's after digest, then call `fault('after_all_temps_fsync')`. A crash here leaves old canonical/derived state and only uncommitted temps.
3. Write and fsync the complete journal through `prepare_atomic_text`; replace it, fsync `MEMORY_HOME/transactions`, then call `fault('after_journal_fsync')`. From this point recovery always rolls forward.
4. Begin one SQLite transaction, apply all derived memory, details, ledger, and vector-invalidating writes, then call `fault('after_db_write')` before any target replacement.
5. Replace targets in lexicographic relative-path order. Each replacement is atomic only for that target and is followed by `fault(f'after_target_replace:{index}')`.
6. Commit SQLite, call `fault('after_db_commit')`, unlink the journal, fsync the transactions directory, and call `fault('after_journal_remove')`.
7. Release locks and perform captured embeddings with `upsert_vector_if_current`.

Recovery reacquires every journal project lock in sorted order. For each target, compute the current digest: an after digest is already complete; a before digest may be replaced only when the named temp still has the after digest; any other digest or a missing/corrupt required temp raises `JournalRecoveryConflict` and leaves journal, files, and DB untouched. To resume a before-digest target, construct `PreparedAtomicWrite(target, temporary, stat.S_IMODE(target.stat().st_mode) if target.exists() else None)` and call its normal replace path, preserving the same Windows/POSIX durability contract. Once all targets have their after digests, parse those documents, rebuild/upsert every affected ID and its operation rows, delete derived rows for affected IDs absent after a delete, and invalidate their vectors in one idempotent DB transaction. Then discard any still-existing listed temp whose digest is the recorded after digest, remove/fsync the journal, and conditionally rebuild active vectors. Recovery after an already committed DB transaction follows the same idempotent path. Empty session documents remain valid schema-v2 files, so the journal never needs a non-atomic file deletion. Service construction scans all journals, recovers them before exposing the service, and stores recovered IDs in `startup_recoveries`; every later mutation also checks intersecting journals. Doctor uses a read-only journal parser and only reports `pending_operation_journal` or `journal_recovery_conflict`.

Add a hidden local bridge:

~~~text
memory admin apply --json-stdin
~~~

It reads one JSON object from stdin, validates an action from create/update/archive/restore/merge/delete, invokes the service, and writes exactly `{"status": ACTION, "memory_id": ID}` to stdout. Create lets CanonicalPersistence generate the ID and returns it; callers cannot inject an ID. Diagnostics go only to stderr. The command accepts no shell fragments or executable paths.

- [ ] **Step 4: Run mutation and existing dashboard service tests**

Run: `uv run --extra dev pytest tests/test_canonical_mutations.py tests/test_core.py tests/test_dashboard.py tests/test_cli.py -q`

Expected: every mutation leaves Markdown and SQLite consistent.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/persistence.py src/memory/core.py src/memory/cli.py src/memory/dashboard_old.py src/memory/health.py src/memory/safe_io.py tests/test_core.py tests/test_canonical_mutations.py
git commit -m "feat: make all memory mutations canonical"
~~~

### Task 9: Route Rust Dashboard Writes Through Canonical Persistence

**Files:**
- Create: dashboard/src/mutation.rs
- Modify: dashboard/src/main.rs
- Modify: dashboard/src/db.rs
- Modify: dashboard/src/app.rs
- Modify: dashboard/src/editor.rs
- Modify: dashboard/Cargo.toml
- Modify: dashboard/Cargo.lock
- Modify: src/memory/cli.py
- Create: tests/fixtures/dashboard-mutations-v1.json
- Create: tests/test_admin_bridge_contract.py
- Create: dashboard/tests/python_bridge.rs
- Create: tests/test_dashboard_bridge_e2e.py

**Interfaces:**
- Produces: MutationRequest serializable enum
- Produces: MutationResponse deserializable result with status and canonical memory_id
- Produces: MutationClient trait
- Produces: CliMutationClient invoking memory admin apply --json-stdin
- Produces: AdminMutationRequest, AdminMutationValidationError, and parse_admin_request(payload: object) -> AdminMutationRequest
- Produces: one shared JSON request corpus consumed by Rust serde and Python validation tests
- Changes: Db remains the read model and delegates every write to MutationClient

- [ ] **Step 1: Add failing Rust tests with a fake mutation client**

Add this complete test module to `mutation.rs`. The fake records typed requests;
the named temporary SQLite file remains alive for the complete `Db` lifetime
and contains the minimum read-model schema used by the dashboard:

~~~rust
#[cfg(test)]
mod tests {
use super::{MutationClient, MutationRequest, MutationResponse};
use crate::db::Db;
use rusqlite::Connection;
use std::sync::{Arc, Mutex};
use tempfile::NamedTempFile;


#[derive(Clone)]
struct RecordingMutationClient {
    requests: Arc<Mutex<Vec<MutationRequest>>>,
    response: MutationResponse,
}


impl RecordingMutationClient {
    fn returning(status: &str, memory_id: &str) -> Self {
        Self {
            requests: Arc::new(Mutex::new(Vec::new())),
            response: MutationResponse {
                status: status.to_string(),
                memory_id: memory_id.to_string(),
            },
        }
    }

    fn requests(&self) -> Vec<MutationRequest> {
        self.requests.lock().unwrap().clone()
    }
}


impl MutationClient for RecordingMutationClient {
    fn apply(&self, request: &MutationRequest) -> rusqlite::Result<MutationResponse> {
        self.requests.lock().unwrap().push(request.clone());
        Ok(self.response.clone())
    }
}


fn fixture_db() -> NamedTempFile {
    let file = NamedTempFile::new().unwrap();
    let connection = Connection::open(file.path()).unwrap();
    connection.execute_batch(
        "
        CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            what TEXT NOT NULL,
            why TEXT,
            impact TEXT,
            tags TEXT,
            category TEXT,
            project TEXT NOT NULL,
            source TEXT,
            status TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            superseded_by TEXT
        );
        CREATE TABLE memory_details (
            memory_id TEXT PRIMARY KEY,
            body TEXT NOT NULL
        );
        ",
    ).unwrap();
    file
}


#[test]
fn archive_is_sent_to_canonical_cli() {
    let fixture = fixture_db();
    let fake = RecordingMutationClient::returning("archived", "memory-1");
    let db = Db::open_with_mutations(fixture.path(), Box::new(fake.clone())).unwrap();
    db.archive_memory("memory-1", "dashboard").unwrap();
    assert_eq!(
        fake.requests(),
        vec![MutationRequest::Archive {
            memory_id: "memory-1".to_string(),
            reason: "dashboard".to_string(),
            actor: "dashboard".to_string(),
        }]
    );
}


#[test]
fn create_uses_the_canonical_id_returned_by_python() {
    let fixture = fixture_db();
    let fake = RecordingMutationClient::returning(
        "created",
        "11111111-1111-4111-8111-111111111111",
    );
    let db = Db::open_with_mutations(fixture.path(), Box::new(fake.clone())).unwrap();
    let memory_id = db.insert_memory(
        "Title",
        "What",
        None,
        None,
        None,
        &[],
        None,
        "project--111111111111",
        None,
    ).unwrap();
    assert_eq!(memory_id, "11111111-1111-4111-8111-111111111111");
    assert!(matches!(fake.requests()[0], MutationRequest::Create { .. }));
}


#[test]
fn shared_golden_requests_round_trip_without_schema_drift() {
    let cases: Vec<serde_json::Value> = serde_json::from_str(include_str!(
        "../../tests/fixtures/dashboard-mutations-v1.json"
    )).unwrap();
    for case in cases {
        let request = case.get("request").unwrap().clone();
        let parsed: MutationRequest = serde_json::from_value(request.clone()).unwrap();
        assert_eq!(serde_json::to_value(parsed).unwrap(), request);
    }
}
}
~~~

Create `tests/fixtures/dashboard-mutations-v1.json` as an array with exactly six named cases—create, update, archive, restore, merge, delete—and fully concrete request objects matching the enum, including the fixed UUID-shaped IDs shown below.

~~~json
[
  {"name":"create","request":{"action":"create","title":"Golden create","what":"created through canonical bridge","why":null,"impact":null,"category":"decision","tags":["golden"],"source":"dashboard","project":"golden--111111111111","details":"Golden details","actor":"dashboard"}},
  {"name":"update","request":{"action":"update","memory_id":"11111111-1111-4111-8111-111111111111","title":"Golden update","what":"updated through canonical bridge","why":null,"impact":null,"category":"decision","tags":["golden","updated"],"details":null,"actor":"dashboard"}},
  {"name":"archive","request":{"action":"archive","memory_id":"11111111-1111-4111-8111-111111111111","reason":"golden archive","actor":"dashboard"}},
  {"name":"restore","request":{"action":"restore","memory_id":"11111111-1111-4111-8111-111111111111","actor":"dashboard"}},
  {"name":"merge","request":{"action":"merge","canonical_id":"11111111-1111-4111-8111-111111111111","source_ids":["22222222-2222-4222-8222-222222222222"],"actor":"dashboard"}},
  {"name":"delete","request":{"action":"delete","memory_id":"22222222-2222-4222-8222-222222222222","actor":"dashboard"}}
]
~~~

Create the Python consumer at the explicit path
`tests/test_admin_bridge_contract.py`:

~~~python
import copy
import json
from pathlib import Path

import pytest

from memory.cli import AdminMutationValidationError, parse_admin_request


GOLDEN_PATH = Path(__file__).parent / 'fixtures' / 'dashboard-mutations-v1.json'


def _cases() -> list[dict[str, object]]:
    payload = json.loads(GOLDEN_PATH.read_text(encoding='utf-8'))
    assert isinstance(payload, list)
    return payload


def _create_request() -> dict[str, object]:
    case = next(item for item in _cases() if item['name'] == 'create')
    request = case['request']
    assert isinstance(request, dict)
    return copy.deepcopy(request)


def test_admin_request_parser_accepts_shared_golden_corpus() -> None:
    parsed_actions = [parse_admin_request(case['request']).action for case in _cases()]
    assert parsed_actions == ['create', 'update', 'archive', 'restore', 'merge', 'delete']


def test_admin_request_parser_rejects_unknown_key() -> None:
    request = _create_request()
    request['unexpected'] = True
    with pytest.raises(AdminMutationValidationError, match='unknown field'):
        parse_admin_request(request)


def test_admin_request_parser_rejects_caller_supplied_create_id() -> None:
    request = _create_request()
    request['memory_id'] = '33333333-3333-4333-8333-333333333333'
    with pytest.raises(AdminMutationValidationError, match='memory_id'):
        parse_admin_request(request)


def test_admin_request_parser_rejects_shell_fields() -> None:
    request = _create_request()
    request['command'] = 'sh -c ignored'
    with pytest.raises(AdminMutationValidationError, match='command'):
        parse_admin_request(request)


def test_admin_request_parser_requires_actor() -> None:
    request = _create_request()
    del request['actor']
    with pytest.raises(AdminMutationValidationError, match='actor'):
        parse_admin_request(request)
~~~

- [ ] **Step 2: Run Rust tests and observe missing bridge types**

Run: `cargo test --manifest-path dashboard/Cargo.toml`

Expected: compilation fails because MutationClient, MutationRequest, and open_with_mutations do not exist.

Run: `uv run --extra dev pytest tests/test_admin_bridge_contract.py -q`

Expected: collection fails because `AdminMutationValidationError` and
`parse_admin_request` do not exist yet. This is the Python RED gate; do not
implement the JSON-stdin command before observing it.

- [ ] **Step 3: Implement JSON-stdin subprocess delegation**

~~~rust
use serde::{Deserialize, Serialize};
use std::io::Write;
use std::process::{Command, Stdio};


#[derive(Clone, Debug, PartialEq, Deserialize, Serialize)]
#[serde(tag = "action", rename_all = "snake_case")]
pub enum MutationRequest {
    Create {
        title: String,
        what: String,
        why: Option<String>,
        impact: Option<String>,
        category: Option<String>,
        tags: Vec<String>,
        source: Option<String>,
        project: String,
        details: Option<String>,
        actor: String,
    },
    Update {
        memory_id: String,
        title: String,
        what: String,
        why: Option<String>,
        impact: Option<String>,
        category: Option<String>,
        tags: Vec<String>,
        details: Option<String>,
        actor: String,
    },
    Archive { memory_id: String, reason: String, actor: String },
    Restore { memory_id: String, actor: String },
    Merge { canonical_id: String, source_ids: Vec<String>, actor: String },
    Delete { memory_id: String, actor: String },
}


#[derive(Clone, Debug, PartialEq, Deserialize)]
pub struct MutationResponse {
    pub status: String,
    pub memory_id: String,
}


pub trait MutationClient {
    fn apply(&self, request: &MutationRequest) -> rusqlite::Result<MutationResponse>;
}


pub struct CliMutationClient {
    pub executable: String,
    pub memory_home: String,
}


impl MutationClient for CliMutationClient {
    fn apply(&self, request: &MutationRequest) -> rusqlite::Result<MutationResponse> {
        let mut child = Command::new(&self.executable)
            .args(["admin", "apply", "--json-stdin"])
            .env("MEMORY_HOME", &self.memory_home)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|error| rusqlite::Error::ToSqlConversionFailure(Box::new(error)))?;
        let payload = serde_json::to_vec(request)
            .map_err(|error| rusqlite::Error::ToSqlConversionFailure(Box::new(error)))?;
        child.stdin.as_mut().unwrap().write_all(&payload)
            .map_err(|error| rusqlite::Error::ToSqlConversionFailure(Box::new(error)))?;
        let output = child.wait_with_output()
            .map_err(|error| rusqlite::Error::ToSqlConversionFailure(Box::new(error)))?;
        if output.status.success() {
            serde_json::from_slice(&output.stdout)
                .map_err(|error| rusqlite::Error::ToSqlConversionFailure(Box::new(error)))
        } else {
            Err(rusqlite::Error::ToSqlConversionFailure(
                Box::new(std::io::Error::new(std::io::ErrorKind::Other, String::from_utf8_lossy(&output.stderr).into_owned()))
            ))
        }
    }
}
~~~

Add serde and serde_json to Cargo.toml. Keep all SELECT queries in Rust. Replace every direct write in db.rs with a MutationClient call and refresh the read model after success. Change Db::insert_memory to omit the old caller-generated ID and return the `memory_id` parsed from MutationResponse; remove uuid_v4 from the create path. Other Db mutators require the returned ID to match the requested/canonical ID before reporting success. Make creator source read-only in the editor so an administrative update cannot rewrite immutable provenance.

In `src/memory/cli.py`, define the pure `parse_admin_request` validator before
the Click command. It accepts only a JSON object, requires `action` and `actor`,
uses an exact allowed/required-key set per action matching the golden corpus,
rejects unknown keys before constructing `AdminMutationRequest`, rejects
`memory_id` for create, and never accepts `command`, `args`, `executable`, or
any other shell field. `AdminMutationValidationError` is the single public
validation error used by both the unit tests and the command's stderr-only
diagnostic path.

~~~python
from dataclasses import dataclass
from typing import Literal, cast


AdminAction = Literal['create', 'update', 'archive', 'restore', 'merge', 'delete']


@dataclass(frozen=True)
class AdminMutationRequest:
    action: AdminAction
    payload: dict[str, object]


class AdminMutationValidationError(ValueError):
    pass


_ADMIN_ALLOWED_FIELDS: dict[AdminAction, frozenset[str]] = {
    'create': frozenset({
        'action', 'title', 'what', 'why', 'impact', 'category', 'tags',
        'source', 'project', 'details', 'actor',
    }),
    'update': frozenset({
        'action', 'memory_id', 'title', 'what', 'why', 'impact', 'category',
        'tags', 'details', 'actor',
    }),
    'archive': frozenset({'action', 'memory_id', 'reason', 'actor'}),
    'restore': frozenset({'action', 'memory_id', 'actor'}),
    'merge': frozenset({'action', 'canonical_id', 'source_ids', 'actor'}),
    'delete': frozenset({'action', 'memory_id', 'actor'}),
}

_ADMIN_REQUIRED_FIELDS: dict[AdminAction, frozenset[str]] = {
    'create': frozenset({'action', 'title', 'what', 'project', 'actor'}),
    'update': frozenset({'action', 'memory_id', 'actor'}),
    'archive': frozenset({'action', 'memory_id', 'reason', 'actor'}),
    'restore': frozenset({'action', 'memory_id', 'actor'}),
    'merge': frozenset({'action', 'canonical_id', 'source_ids', 'actor'}),
    'delete': frozenset({'action', 'memory_id', 'actor'}),
}


def parse_admin_request(payload: object) -> AdminMutationRequest:
    if not isinstance(payload, dict) or not all(isinstance(key, str) for key in payload):
        raise AdminMutationValidationError('request must be one JSON object')
    raw_action = payload.get('action')
    if not isinstance(raw_action, str) or raw_action not in _ADMIN_ALLOWED_FIELDS:
        raise AdminMutationValidationError('action must be create, update, archive, restore, merge, or delete')
    action = cast(AdminAction, raw_action)
    unknown = sorted(set(payload) - _ADMIN_ALLOWED_FIELDS[action])
    if unknown:
        raise AdminMutationValidationError(f'unknown field: {unknown[0]}')
    missing = sorted(_ADMIN_REQUIRED_FIELDS[action] - set(payload))
    if missing:
        raise AdminMutationValidationError(f'missing required field: {missing[0]}')
    actor = payload['actor']
    if not isinstance(actor, str) or not actor.strip():
        raise AdminMutationValidationError('actor must be a non-empty string')
    return AdminMutationRequest(action=action, payload=dict(payload))
~~~

Add one real cross-language E2E, not another mock. `dashboard/tests/python_bridge.rs` defines the ignored test `cli_mutation_client_writes_canonical_python_storage`; it reads `ECHOVAULT_TEST_MEMORY_EXECUTABLE` and `MEMORY_HOME`, constructs `CliMutationClient`, creates a memory, then updates the returned canonical ID and asserts both typed responses. `tests/test_dashboard_bridge_e2e.py` locates the installed `memory` console script with `shutil.which('memory')`, sets those two environment variables to a fresh tmp_path, and runs exactly:

~~~python
subprocess.run(
    [
        'cargo', 'test', '--manifest-path', 'dashboard/Cargo.toml',
        '--test', 'python_bridge',
        'cli_mutation_client_writes_canonical_python_storage',
        '--', '--ignored', '--exact',
    ],
    cwd=REPOSITORY_ROOT,
    env=environment,
    check=True,
    text=True,
    capture_output=True,
)
~~~

After Cargo returns, the Python test opens `index.db` read-only and parses the single schema-v2 session file. It asserts one shared generated ID, final `what == 'updated by rust'`, `last_updated_by == 'dashboard'`, identical Markdown/SQLite operation IDs and content fingerprints, and no direct Rust-created row without canonical Markdown. The Rust test never runs by default without its explicit ignored invocation, keeping ordinary `cargo test` hermetic.

- [ ] **Step 4: Run Rust and Python dashboard tests**

Run: `cargo test --manifest-path dashboard/Cargo.toml`

Expected: bridge unit tests pass.

Run: `uv run --extra dev pytest tests/test_dashboard.py tests/test_canonical_mutations.py tests/test_admin_bridge_contract.py -q`

Expected: Python dashboard-service and canonical mutation tests pass.

Run: `uv run --extra dev pytest tests/test_dashboard_bridge_e2e.py -q`

Expected: the real Rust client reaches `memory admin apply`, and shared Markdown/SQLite identity, provenance, ledger, and fingerprint assertions pass.

- [ ] **Step 5: Commit**

~~~bash
git add dashboard/src/mutation.rs dashboard/src/main.rs dashboard/src/db.rs dashboard/src/app.rs dashboard/src/editor.rs dashboard/tests/python_bridge.rs dashboard/Cargo.toml dashboard/Cargo.lock src/memory/cli.py tests/fixtures/dashboard-mutations-v1.json tests/test_admin_bridge_contract.py tests/test_dashboard_bridge_e2e.py
git commit -m "fix: route dashboard writes through canonical storage"
~~~

### Task 10: Explicit Schema-v1 Metadata Migration

**Files:**
- Create: src/memory/reconcile.py
- Modify: src/memory/core.py
- Modify: src/memory/cli.py
- Modify: src/memory/health.py
- Create: tests/test_vault_migration.py
- Modify: tests/test_markdown.py

**Interfaces:**
- Consumes: LegacyMetadataRequiredError from Task 7
- Produces: migrate_vault_metadata(service, project=None, dry_run=False) -> dict[str, object]
- Produces CLI: memory migrate vault-metadata [--project PROJECT] [--dry-run]

- [ ] **Step 1: Write failing migration tests**

~~~python
from pathlib import Path

import pytest

from memory.core import MemoryService
from memory.markdown import parse_session_file
from memory.models import Memory
from memory.persistence import content_fingerprint


@pytest.fixture
def legacy_file(service: MemoryService) -> Path:
    path = Path(service.vault_dir) / 'legacy' / '2000-01-01-session.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    memory_id = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
    details = 'Legacy details preserved exactly.'
    path.write_text(
        '---\n'
        'project: legacy\n'
        'created: 2000-01-01T10:00:00+00:00\n'
        'tags: [aggregate-only]\n'
        'sources: [cursor]\n'
        '---\n\n'
        '# Legacy Session\n\n'
        '## Decisions\n\n'
        '### Legacy decision\n'
        f'<!-- memory-id: {memory_id} -->\n'
        '**What:** Legacy canonical text\n'
        '**Why:** Preserve historical metadata\n'
        '**Impact:** Migration remains lossless\n'
        '**Source:** cursor\n\n'
        '<details>\n'
        f'{details}\n'
        '</details>\n',
        encoding='utf-8',
    )
    row = Memory(
        id=memory_id,
        title='Legacy decision',
        what='Legacy canonical text',
        why='Preserve historical metadata',
        impact='Migration remains lossless',
        tags=['per-memory-tag'],
        category='decision',
        project='legacy',
        source='cursor',
        related_files=['src/legacy.py'],
        file_path=str(path),
        section_anchor='legacy-decision',
        created_at='2000-01-01T10:00:00+00:00',
        updated_at='2000-01-02T10:00:00+00:00',
        structured_data={'constraints': ['preserve DB-only metadata']},
        confidence=0.8,
        branch='legacy-branch',
    )
    service.db.insert_memory(row, details)
    return path


def test_migration_enriches_from_matching_db_row_without_guessing(service: MemoryService, legacy_file: Path) -> None:
    result = service.migrate_vault_metadata(project='legacy')
    assert result['migrated'] == 1
    parsed = parse_session_file(legacy_file)
    assert parsed.schema_version == 2
    metadata = parsed.entries[0].metadata
    assert metadata['history_complete'] is False
    assert metadata['creator_source'] == 'cursor'
    assert metadata['operations'][0]['action'] == 'migrated'
    rebuilt = parsed.entries[0].to_memory(str(legacy_file))
    assert rebuilt.source == metadata['creator_source']
    assert metadata['content_fingerprint'] == content_fingerprint(rebuilt)
    row = service.get_memory_record(rebuilt.id)
    assert row['content_fingerprint'] == metadata['content_fingerprint']
    operation = service.db.get_operation('legacy', metadata['operations'][0]['operation_id'])
    assert operation is not None
    assert operation['memory_id'] == rebuilt.id
    assert operation['action'] == 'migrated'
    second = service.migrate_vault_metadata(project='legacy')
    assert second['migrated'] == 0
~~~

- [ ] **Step 2: Run migration tests and observe missing command/API**

Run: `uv run --extra dev pytest tests/test_vault_migration.py tests/test_markdown.py -k "v1 or migration" -q`

Expected: the migration API is missing and the old cp1251 append test still expects an implicit v1 rewrite.

- [ ] **Step 3: Implement lossless enrichment**

Match v1 entries to derived rows only by unambiguous project, historical file path, stable ID if present, or unique section anchor. Never assign aggregate frontmatter tags or sources to every entry. If no unique DB row exists, report the file/anchor and leave the entire file unchanged.

For a known source set source, creator_source, last_updated_by, and the first contributor to it. Preserve an unknown source as null with an empty contributor list. Set history_complete false and append one deterministic synthetic migration operation whose ID is the SHA-256 of project, normalized relative file path, memory ID/anchor, and literal schema target `2`. After the complete migrated Memory is assembled, recompute `content_fingerprint` with Task 7's exact function before rendering.

Migrate one file at a time under its project lock with the same canonical ordering as a normal single-document save: render/prepare the full v2 file; begin a DB transaction; upsert every enriched Memory/details row and its synthetic operation ledger row; invalidate vectors whose fingerprint changed; replace/fsync Markdown; commit DB. Only then report the file migrated, and conditionally rebuild active vectors with `upsert_vector_if_current`. A failure before replacement rolls back DB and leaves v1 untouched; a failure after replacement leaves authoritative v2 plus old DB for explicit reconciliation. Migration is not complete if only Markdown changed—the test above requires Markdown, derived row, ledger, and exact fingerprint to agree synchronously on success.

Replace the existing implicit legacy encoding rewrite test with refusal plus explicit migration coverage.

- [ ] **Step 4: Run migration, import, Markdown, and CLI tests**

Run: `uv run --extra dev pytest tests/test_vault_migration.py tests/test_import.py tests/test_markdown.py tests/test_cli.py -q`

Expected: v1 reads remain valid, v1 writes refuse, migration is lossless and idempotent.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/reconcile.py src/memory/core.py src/memory/cli.py src/memory/health.py tests/test_vault_migration.py tests/test_markdown.py
git commit -m "feat: migrate legacy vault metadata explicitly"
~~~

### Task 11: Canonical Markdown-to-SQLite Reconciliation

**Files:**
- Modify: src/memory/reconcile.py
- Modify: src/memory/core.py
- Modify: src/memory/cli.py
- Modify: src/memory/health.py
- Create: tests/test_reconcile.py
- Modify: tests/test_import.py

**Interfaces:**
- Changes: MemoryService.import_from_vault(*, reconcile: bool = False, project: str | None = None)
- Produces CLI: memory import --reconcile [--project PROJECT]
- Produces: reconcile_project(service, project) -> ReconcileReport
- ReconcileReport fields: inserted, updated, deleted, ledger_rows, rebuilt_vectors, invalid_fingerprints, blockers, destructive_cleanup

- [ ] **Step 1: Write failing repair and non-destructive mixed-schema tests**

~~~python
from pathlib import Path

import pytest

from memory.core import MemoryService
from memory.markdown import parse_session_file
from memory.models import Memory, RawMemoryInput
from memory.persistence import content_fingerprint


@pytest.fixture
def mixed_project(service: MemoryService) -> Path:
    service.save(
        RawMemoryInput(title='Complete v2 entry', what='safe canonical row'),
        project='mixed',
        authoritative_source='cursor',
        idempotency_key='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
    )
    legacy_path = Path(service.vault_dir) / 'mixed' / '2000-01-01-session.md'
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_id = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
    legacy_path.write_text(
        '---\n'
        'project: mixed\n'
        'created: 2000-01-01T10:00:00+00:00\n'
        'tags: [legacy]\n'
        'sources: [cursor]\n'
        '---\n\n'
        '# Mixed Legacy Session\n\n'
        '### Incomplete v1 entry\n'
        f'<!-- memory-id: {legacy_id} -->\n'
        '**What:** must remain derived while v1 blocks cleanup\n'
        '**Source:** cursor\n',
        encoding='utf-8',
    )
    service.db.insert_memory(
        Memory(
            id=legacy_id,
            title='Incomplete v1 entry',
            what='must remain derived while v1 blocks cleanup',
            why=None,
            impact=None,
            tags=['legacy'],
            category='context',
            project='mixed',
            source='cursor',
            related_files=[],
            file_path=str(legacy_path),
            section_anchor='incomplete-v1-entry',
            created_at='2000-01-01T10:00:00+00:00',
            updated_at='2000-01-01T10:00:00+00:00',
        ),
        None,
    )
    return legacy_path.parent


def test_reconcile_repairs_changed_row_and_missing_ledger(service: MemoryService) -> None:
    saved = service.save(
        RawMemoryInput(title='Canonical', what='markdown wins'),
        project='p--1',
        authoritative_source='cursor',
        idempotency_key='44444444-4444-4444-8444-444444444444',
    )
    service.db.conn.execute("UPDATE memories SET what = 'drift' WHERE id = ?", (saved['id'],))
    service.db.conn.execute('DELETE FROM save_operations')
    service.db.conn.commit()
    report = service.import_from_vault(reconcile=True, project='p--1')
    assert report['updated'] == 1
    assert service.get_memory_record(saved['id'])['what'] == 'markdown wins'
    assert service.db.get_operation('p--1', '44444444-4444-4444-8444-444444444444') is not None


def test_mixed_v1_v2_scan_never_deletes_derived_rows(service: MemoryService, mixed_project: Path) -> None:
    before = service.db.count_memories(project='mixed')
    assert service.get_memory_record('cccccccc-cccc-4ccc-8ccc-cccccccccccc') is not None
    report = service.import_from_vault(reconcile=True, project='mixed')
    assert report['destructive_cleanup'] is False
    assert service.db.count_memories(project='mixed') == before
    assert service.get_memory_record('cccccccc-cccc-4ccc-8ccc-cccccccccccc') is not None


def test_reconcile_rejects_corrupt_canonical_fingerprint_without_mutation(service: MemoryService) -> None:
    saved = service.save(RawMemoryInput(title='Integrity', what='canonical'), project='p--1')
    path = Path(saved['file_path'])
    row_before = dict(service.get_memory_record(saved['id']))
    expected = row_before['content_fingerprint']
    content = path.read_text(encoding='utf-8')
    corrupted = content.replace(
        f'"content_fingerprint":"{expected}"',
        '"content_fingerprint":"sha256:corrupt"',
    )
    assert corrupted != content
    path.write_text(corrupted, encoding='utf-8')
    report = service.import_from_vault(reconcile=True, project='p--1')
    assert report['invalid_fingerprints'] == [saved['id']]
    assert report['destructive_cleanup'] is False
    assert dict(service.get_memory_record(saved['id'])) == row_before
    finding = next(item for item in service.doctor(project='p--1')['findings'] if item['code'] == 'invalid_content_fingerprint')
    assert finding['memory_id'] == saved['id']
    assert finding['repair'] == 'Fix or restore canonical Markdown, then run: memory import --reconcile --project p--1'


def test_reconcile_rebuilds_missing_vector_through_fingerprint_cas(service: MemoryService, monkeypatch) -> None:
    saved = service.save(RawMemoryInput(title='Vector', what='rebuild me'), project='p--1')
    expected = service.get_memory_record(saved['id'])['content_fingerprint']
    service.db.invalidate_vector(saved['id'])
    calls: list[tuple[str, str]] = []
    real_upsert = service.db.upsert_vector_if_current

    def recording_upsert(memory_id: str, fingerprint: str, embedding: list[float]) -> bool:
        calls.append((memory_id, fingerprint))
        return real_upsert(memory_id, fingerprint, embedding)

    monkeypatch.setattr(service.db, 'upsert_vector_if_current', recording_upsert)
    report = service.import_from_vault(reconcile=True, project='p--1')
    assert report['rebuilt_vectors'] == 1
    assert calls == [(saved['id'], expected)]
    assert service.db.has_vector(saved['id']) is True
~~~

- [ ] **Step 2: Run reconciliation tests and observe unsupported option**

Run: `uv run --extra dev pytest tests/test_reconcile.py tests/test_import.py -q`

Expected: import_from_vault rejects reconcile/project or fails to repair changed rows.

- [ ] **Step 3: Rebuild derived state only from complete v2 scans**

Use parse_session_file as the only Markdown parser; remove the duplicate parser from core.py. Preserve stable IDs and created timestamps. Before trusting any v2 entry, rebuild its Memory and recompute Task 7's exact `content_fingerprint` from the exact embedding text. If the stored metadata fingerprint differs, add the stable ID to sorted `invalid_fingerprints`, skip that entry's memory/ledger/vector writes, mark the scan incomplete so no destructive cleanup occurs, and leave Markdown untouched. Doctor performs the same pure comparison and reports `invalid_content_fingerprint` plus the exact repair command asserted above.

For every complete, valid v2 project scan, open one SQLite transaction, upsert missing and changed memories/details, rebuild save_operations, invalidate vectors for changed fingerprints, and remove derived rows/ledger entries absent from Markdown. For valid active entries whose row changed or `db.has_vector(id)` is false, capture `(id, content_fingerprint, embedding_text)`; for archived/deleted records, invalidate and capture nothing. Commit and release the project lock before embedding. For each captured item call the provider, then only `upsert_vector_if_current(id, captured_fingerprint, embedding)`; increment `rebuilt_vectors` only when it returns true. Never call an unconditional vector insert from reconciliation. If any file is v1, malformed, unreadable, ambiguous, journal-conflicted, or fingerprint-invalid, perform only non-destructive upserts for other valid entries and report the blocker.

Doctor must compare canonical content fingerprints and operation IDs without mutating anything. It reports exact repair commands and stale prepared temp files; it never runs reconciliation automatically.

- [ ] **Step 4: Run reconciliation, import, health, and full suites**

Run: `uv run --extra dev pytest tests/test_reconcile.py tests/test_import.py tests/test_living_memory.py tests/test_cli.py -q`

Expected: repair, idempotent second run, v2 deletion, and mixed-schema safety pass.

Run: `uv run --extra dev pytest -q`

Expected: full Python suite passes.

- [ ] **Step 5: Commit**

~~~bash
git add src/memory/reconcile.py src/memory/core.py src/memory/cli.py src/memory/health.py tests/test_reconcile.py tests/test_import.py
git commit -m "feat: reconcile derived index from markdown"
~~~

### Task 12: Multiprocess, Fault-Injection, and Final Core Gate

**Files:**
- Create: tests/test_concurrency.py
- Create: tests/test_crash_recovery.py
- Create: tests/worker_helpers.py
- Create: tests/persistence_helpers.py
- Modify: src/memory/persistence.py
- Modify: src/memory/db.py
- Modify: src/memory/safe_io.py
- Modify: src/memory/health.py

**Interfaces:**
- Verifies every documented save failure boundary
- Verifies every multi-document journal replacement boundary and conflict stop
- Verifies sorted project locks and lock timeouts
- Verifies 80 unique process writes occur exactly once
- Verifies delayed embeddings use content-fingerprint compare-and-swap

- [ ] **Step 1: Add executable shared helpers and top-level spawn workers**

~~~python
"""tests/worker_helpers.py — every multiprocessing target stays importable under spawn."""
from __future__ import annotations

import os
import traceback
import uuid
from multiprocessing.queues import Queue

from memory.core import MemoryService
from memory.models import RawMemoryInput

PROJECT = 'shared--111111111111'
OPERATION_NAMESPACE = uuid.UUID('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')


class FixedEmbedding:
    def embed(self, text: str) -> list[float]:
        checksum = sum(text.encode('utf-8')) % 997
        return [float(checksum) / 997.0, 0.25, 0.75]


def configured_service(memory_home: str) -> MemoryService:
    service = MemoryService(memory_home)
    service._embedding_provider = FixedEmbedding()
    return service


def write_ten_memories(memory_home: str, worker: int, errors: Queue) -> None:
    service = configured_service(memory_home)
    try:
        for offset in range(10):
            ordinal = worker * 10 + offset
            service.save(
                RawMemoryInput(title=f'worker-{ordinal}', what=f'value-{ordinal}'),
                project=PROJECT,
                authoritative_source='cursor',
                idempotency_key=str(uuid.uuid5(OPERATION_NAMESPACE, f'worker:{ordinal}')),
            )
        errors.put(None)
    except BaseException:
        errors.put(traceback.format_exc())
        raise
    finally:
        service.db.close()


def crash_save_worker(memory_home: str, phase: str) -> None:
    service = configured_service(memory_home)
    service.persistence.fault = lambda current: os._exit(91) if current == phase else None
    service.save(
        RawMemoryInput(title='crash-boundary', what='new'),
        project=PROJECT,
        authoritative_source='gemini-cli',
        idempotency_key='66666666-6666-4666-8666-666666666666',
    )
    raise AssertionError(f'fault phase was not reached: {phase}')


def crash_merge_worker(
    memory_home: str,
    canonical_id: str,
    source_ids: tuple[str, ...],
    phase: str,
) -> None:
    service = configured_service(memory_home)
    service.persistence.fault = lambda current: os._exit(92) if current == phase else None
    service.merge_memories(
        canonical_id,
        list(source_ids),
        actor='dashboard',
        operation_id='88888888-8888-4888-8888-888888888888',
    )
    raise AssertionError(f'fault phase was not reached: {phase}')
~~~

~~~python
"""tests/persistence_helpers.py — state inspection contains no hidden pytest fixtures."""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from memory.markdown import parse_session_file
from memory.models import RawMemoryInput
from memory.persistence import SaveRequest
from tests.worker_helpers import FixedEmbedding, PROJECT, configured_service


@dataclass(frozen=True)
class ProjectSnapshot:
    markdown: dict[str, tuple[str, str, tuple[str, ...]]]
    database: dict[str, tuple[str, str, tuple[str, ...]]]
    journal_paths: tuple[Path, ...]


def seed_single(memory_home: Path) -> tuple[str, Path]:
    service = configured_service(str(memory_home))
    result = service.save(
        RawMemoryInput(title='crash-boundary', what='old'),
        project=PROJECT,
        authoritative_source='cursor',
        idempotency_key='77777777-7777-4777-8777-777777777777',
    )
    service.db.close()
    return str(result['id']), Path(str(result['file_path']))


def seed_three_historical_memories(memory_home: Path) -> tuple[str, tuple[str, str]]:
    service = configured_service(str(memory_home))
    results = []
    for day, title in zip((10, 11, 12), ('canonical', 'source-one', 'source-two'), strict=True):
        operation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'historical:{title}'))
        results.append(service.persistence.save(SaveRequest(
            raw=RawMemoryInput(title=title, what=f'{title} body'),
            project=PROJECT,
            source='cursor',
            operation_id=operation_id,
            timestamp=f'2026-07-{day:02d}T10:00:00+00:00',
        )))
    service.db.close()
    return str(results[0]['id']), (str(results[1]['id']), str(results[2]['id']))


def snapshot_project(memory_home: Path) -> ProjectSnapshot:
    markdown: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    for path in sorted((memory_home / 'vault' / PROJECT).glob('*-session.md')):
        for entry in parse_session_file(path).entries:
            memory = entry.to_memory(str(path))
            markdown[memory.id] = (
                memory.what,
                memory.content_fingerprint or '',
                tuple(operation.operation_id for operation in memory.operations),
            )
    connection = sqlite3.connect(memory_home / 'index.db')
    connection.row_factory = sqlite3.Row
    database = {}
    for row in connection.execute(
        'SELECT id, what, content_fingerprint, operation_history FROM memories WHERE project = ?',
        (PROJECT,),
    ):
        operations = tuple(item['operation_id'] for item in json.loads(row['operation_history']))
        database[row['id']] = (row['what'], row['content_fingerprint'], operations)
    connection.close()
    journals = tuple(sorted((memory_home / 'transactions').glob('*.json'))) if (memory_home / 'transactions').exists() else ()
    return ProjectSnapshot(markdown, database, journals)
~~~

`tests` is made importable with an empty `tests/__init__.py` if it is not already a package. Do not place multiprocessing targets inside test functions or closures; the final gate explicitly uses `multiprocessing.get_context('spawn')` on every OS.

- [ ] **Step 2: Add the failing single-file and journal crash matrices**

~~~python
import json
from multiprocessing import get_context
from pathlib import Path
from threading import Event, Thread

import pytest

from memory.health import doctor
from memory.markdown import parse_session_file
from memory.models import RawMemoryInput
from memory.persistence import JournalRecoveryConflict, acquire_project_locks, content_fingerprint
from tests.persistence_helpers import seed_single, seed_three_historical_memories, snapshot_project
from tests.worker_helpers import PROJECT, configured_service, crash_merge_worker, crash_save_worker, write_ten_memories


def test_project_locks_are_acquired_sorted_and_released_reverse(tmp_path: Path) -> None:
    events: list[str] = []

    class RecordingLock:
        def __init__(self, path: Path):
            self.path = path

        def __enter__(self):
            events.append(f'enter:{self.path.stem}')
            return self

        def __exit__(self, exc_type, exc, traceback):
            events.append(f'exit:{self.path.stem}')

    with acquire_project_locks(
        tmp_path,
        ['z-project', 'a-project', 'm-project', 'a-project'],
        lock_factory=RecordingLock,
    ):
        events.append('body')
    assert events == [
        'enter:a-project', 'enter:m-project', 'enter:z-project', 'body',
        'exit:z-project', 'exit:m-project', 'exit:a-project',
    ]


@pytest.mark.parametrize(
    ('phase', 'expected_markdown', 'expected_db'),
    [
        ('after_temp_fsync', 'old', 'old'),
        ('after_db_write', 'old', 'old'),
        ('after_markdown_replace', 'new', 'old'),
        ('after_db_commit', 'new', 'new'),
        ('before_vector_write', 'new', 'new'),
    ],
)
def test_save_failure_boundary_recovers_by_contract(
    tmp_path: Path,
    phase: str,
    expected_markdown: str,
    expected_db: str,
) -> None:
    memory_home = tmp_path / '.memory'
    memory_id, _ = seed_single(memory_home)
    process = get_context('spawn').Process(
        target=crash_save_worker,
        args=(str(memory_home), phase),
    )
    process.start()
    process.join(20)
    assert process.exitcode == 91
    snapshot = snapshot_project(memory_home)
    assert snapshot.markdown[memory_id][0] == expected_markdown
    assert snapshot.database[memory_id][0] == expected_db
    if expected_markdown != expected_db:
        service = configured_service(str(memory_home))
        service.import_from_vault(reconcile=True, project=PROJECT)
        service.db.close()
        repaired = snapshot_project(memory_home)
        assert repaired.markdown[memory_id] == repaired.database[memory_id]
    if phase == 'after_db_commit':
        operation_id = '66666666-6666-4666-8666-666666666666'
        service = configured_service(str(memory_home))
        try:
            replayed = service.save(
                RawMemoryInput(title='crash-boundary', what='new'),
                project=PROJECT,
                authoritative_source='gemini-cli',
                idempotency_key=operation_id,
            )
            assert replayed['id'] == memory_id
            assert replayed['action'] == 'replayed'
            row = service.get_memory_record(memory_id)
            history = json.loads(row['operation_history'])
            assert sum(item['operation_id'] == operation_id for item in history) == 1
            assert row['updated_count'] == 1
        finally:
            service.db.close()
        replayed_snapshot = snapshot_project(memory_home)
        assert replayed_snapshot.markdown == replayed_snapshot.database
        assert replayed_snapshot.markdown[memory_id][2].count(operation_id) == 1


def test_multi_document_crash_before_journal_keeps_old_state(tmp_path: Path) -> None:
    memory_home = tmp_path / '.memory'
    canonical_id, source_ids = seed_three_historical_memories(memory_home)
    before = snapshot_project(memory_home)
    process = get_context('spawn').Process(
        target=crash_merge_worker,
        args=(
            str(memory_home),
            canonical_id,
            source_ids,
            'after_all_temps_fsync',
        ),
    )
    process.start()
    process.join(20)
    assert process.exitcode == 92
    after = snapshot_project(memory_home)
    assert after.markdown == before.markdown
    assert after.database == before.database
    assert after.journal_paths == ()
    assert any(
        item['code'] == 'stale_prepared_temp'
        for item in doctor(str(memory_home), project=PROJECT)['findings']
    )


@pytest.mark.parametrize(
    ('phase', 'has_pending_journal'),
    [
        ('after_journal_fsync', True),
        ('after_db_write', True),
        ('after_target_replace:0', True),
        ('after_target_replace:1', True),
        ('after_target_replace:2', True),
        ('after_db_commit', True),
        ('after_journal_remove', False),
    ],
)
def test_multi_document_merge_rolls_forward_after_every_replacement(
    tmp_path: Path,
    phase: str,
    has_pending_journal: bool,
) -> None:
    memory_home = tmp_path / '.memory'
    canonical_id, source_ids = seed_three_historical_memories(memory_home)
    process = get_context('spawn').Process(
        target=crash_merge_worker,
        args=(str(memory_home), canonical_id, source_ids, phase),
    )
    process.start()
    process.join(20)
    assert process.exitcode == 92
    assert bool(snapshot_project(memory_home).journal_paths) is has_pending_journal

    service = configured_service(str(memory_home))
    expected_recoveries = (
        ('88888888-8888-4888-8888-888888888888',)
        if has_pending_journal
        else ()
    )
    assert service.persistence.startup_recoveries == expected_recoveries
    service.db.close()
    snapshot = snapshot_project(memory_home)
    assert snapshot.journal_paths == ()
    assert snapshot.markdown == snapshot.database
    assert snapshot.markdown[canonical_id][0] == 'canonical body'
    assert all(source_id in snapshot.markdown for source_id in source_ids)
    for path in sorted((memory_home / 'vault' / PROJECT).glob('*-session.md')):
        for entry in parse_session_file(path).entries:
            memory = entry.to_memory(str(path))
            assert memory.content_fingerprint == content_fingerprint(memory)
    assert doctor(str(memory_home), project=PROJECT)['status'] == 'ok'


def test_recovery_stops_when_a_target_matches_neither_digest(tmp_path: Path) -> None:
    memory_home = tmp_path / '.memory'
    canonical_id, source_ids = seed_three_historical_memories(memory_home)
    process = get_context('spawn').Process(
        target=crash_merge_worker,
        args=(str(memory_home), canonical_id, source_ids, 'after_target_replace:0'),
    )
    process.start()
    process.join(20)
    assert process.exitcode == 92
    journal_path = snapshot_project(memory_home).journal_paths[0]
    journal = json.loads(journal_path.read_text(encoding='utf-8'))
    conflicted = memory_home / journal['targets'][1]['target']
    conflicted.write_text('externally changed\n', encoding='utf-8')
    before_db = snapshot_project(memory_home).database
    with pytest.raises(JournalRecoveryConflict):
        configured_service(str(memory_home))
    assert journal_path.exists()
    assert snapshot_project(memory_home).database == before_db


def test_eight_processes_write_eighty_unique_operations(tmp_path: Path) -> None:
    memory_home = tmp_path / '.memory'
    context = get_context('spawn')
    errors = context.Queue()
    processes = [
        context.Process(target=write_ten_memories, args=(str(memory_home), worker, errors))
        for worker in range(8)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    assert [errors.get(timeout=2) for _ in processes] == [None] * 8
    snapshot = snapshot_project(memory_home)
    operation_ids = {
        operation_id
        for _, _, operations in snapshot.markdown.values()
        for operation_id in operations
    }
    assert len(operation_ids) == 80
    assert snapshot.markdown == snapshot.database
    service = configured_service(str(memory_home))
    assert service.db.count_memories(project='shared--111111111111') == 80
    service.db.close()
    assert doctor(str(memory_home), project=PROJECT)['status'] == 'ok'


def test_delayed_old_embedding_cannot_overwrite_new_fingerprint(tmp_path: Path) -> None:
    memory_home = tmp_path / '.memory'
    memory_id, _ = seed_single(memory_home)
    first_started = Event()
    release_first = Event()
    first_cas_results: list[bool] = []
    thread_errors: list[BaseException] = []

    class BlockingFirstEmbedding:
        def embed(self, text: str) -> list[float]:
            first_started.set()
            assert release_first.wait(10)
            return [0.1, 0.2, 0.3]

    def first_update() -> None:
        service = MemoryService(str(memory_home))
        service._embedding_provider = BlockingFirstEmbedding()
        real_upsert = service.db.upsert_vector_if_current

        def recording_upsert(memory_id: str, fingerprint: str, embedding: list[float]) -> bool:
            result = real_upsert(memory_id, fingerprint, embedding)
            first_cas_results.append(result)
            return result

        service.db.upsert_vector_if_current = recording_upsert
        try:
            service.save(
                RawMemoryInput(title='crash-boundary', what='first update'),
                project=PROJECT,
                authoritative_source='cursor',
                idempotency_key='99999999-9999-4999-8999-999999999999',
            )
        except BaseException as error:
            thread_errors.append(error)
        finally:
            service.db.close()

    thread = Thread(target=first_update)
    thread.start()
    assert first_started.wait(10)
    second = configured_service(str(memory_home))
    second.save(
        RawMemoryInput(title='crash-boundary', what='second update'),
        project=PROJECT,
        authoritative_source='gemini-cli',
        idempotency_key='aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa',
    )
    second_fingerprint = second.get_memory_record(memory_id)['content_fingerprint']
    second.db.close()
    release_first.set()
    thread.join(10)
    assert not thread.is_alive()
    assert thread_errors == []
    assert first_cas_results == [False]
    snapshot = snapshot_project(memory_home)
    assert snapshot.markdown == snapshot.database
    assert snapshot.markdown[memory_id][0] == 'second update'
    assert snapshot.markdown[memory_id][1] == second_fingerprint
~~~

Use the fixed operation ID `88888888-8888-4888-8888-888888888888` in the merge helper/API call so the recovery assertion is deterministic; add an explicit optional `operation_id` parameter to the trusted internal merge coordinator, while the public service still generates one. The delayed-embedding test uses separate MemoryService/SQLite connections per thread, so it tests the storage contract rather than violating sqlite3's thread affinity.

- [ ] **Step 3: Run RED gates and name the missing behavior**

Run: `uv run --extra dev python -m compileall -q tests/worker_helpers.py tests/persistence_helpers.py`

Expected: helper modules compile before multiprocessing starts; fix import/type errors in tests only, without touching production behavior.

Run: `uv run --extra dev pytest tests/test_concurrency.py tests/test_crash_recovery.py -q`

Expected RED: journal tests fail at the first unimplemented recovery/order assertion (initially missing `recover_pending_operations` or no durable journal); the 80-write and delayed-vector tests may additionally expose lock/CAS defects. Record the first failing assertion in the task notes before implementation.

- [ ] **Step 4: Make the smallest corrections required by the matrix**

Ensure:

- lock timeout returns a typed retryable error and never falls through to an unlocked write;
- stale lock files do not block after their owning process exits;
- multi-project operations acquire lock paths in sorted key order;
- an after-replace/before-commit failure leaves authoritative Markdown for explicit reconciliation;
- a lost response after commit replays with the same operation ID;
- a durable journal is fsynced before the first of several file replacements, recovery rolls forward at every replacement index, and the journal directory is fsynced after removal;
- a target/temp digest mismatch stops recovery without touching derived rows or deleting the journal;
- an older embedding can neither survive invalidation nor overwrite a newer fingerprint;
- prepared temp files are removed or reported deterministically;
- Windows flushes the prepared file through os.fsync/FlushFileBuffers and
  performs replacement with MoveFileExW using MOVEFILE_REPLACE_EXISTING and
  MOVEFILE_WRITE_THROUGH; POSIX uses os.replace plus parent-directory fsync.

- [ ] **Step 5: Run GREEN gates in increasing scope**

Run: `uv run --extra dev pytest tests/test_crash_recovery.py -q`

Expected GREEN: every single-file phase, every journal replacement index, conflict stop, and delayed-vector CAS passes.

Run: `uv run --extra dev pytest tests/test_concurrency.py -q`

Expected GREEN: all eight spawn workers exit zero, exactly 80 distinct operation IDs exist in both stores, and doctor is healthy.

Run: `uv run --extra dev pytest tests/test_safe_io.py tests/test_projects.py tests/test_merge.py tests/test_persistence.py tests/test_canonical_mutations.py tests/test_vault_migration.py tests/test_reconcile.py tests/test_concurrency.py tests/test_crash_recovery.py -q`

Expected: all new core tests pass.

Run: `uv run --extra dev pytest -q`

Expected: full Python suite passes.

Run: `cargo test --manifest-path dashboard/Cargo.toml`

Expected: Rust dashboard tests pass with no direct SQLite mutation path.

- [ ] **Step 6: Commit**

~~~bash
git add src/memory/persistence.py src/memory/db.py src/memory/safe_io.py src/memory/health.py tests/__init__.py tests/worker_helpers.py tests/persistence_helpers.py tests/test_concurrency.py tests/test_crash_recovery.py
git commit -m "test: harden canonical storage recovery"
~~~

## Phase Completion Gate

Run:

~~~bash
uv run --extra dev pytest -q
cargo test --manifest-path dashboard/Cargo.toml
uv run --extra dev pytest tests/test_admin_bridge_contract.py tests/test_dashboard_bridge_e2e.py -q
git status --short
~~~

Expected: all tests pass, the ignored Rust bridge is exercised through its Python E2E wrapper, and the worktree contains only intentional phase commits. Review Markdown-v2 samples, one migrated v1 file, operation replay, a repaired crash fixture, and the Rust bridge before starting the bound MCP plan.
