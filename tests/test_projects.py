import json
from pathlib import Path

import pytest

from memory.projects import (
    LegacyAliasConflict,
    MultiRootError,
    ProjectCandidates,
    ProjectIdentity,
    ProjectRegistry,
    ProjectRegistryConflict,
    ProjectResolutionError,
    build_project_identity,
    discover_project_root,
    select_project_candidate,
    validate_storage_key,
)


def test_nearest_marker_wins_for_nested_cwd(tmp_path: Path) -> None:
    root = tmp_path / 'workspace'
    nearer = root / 'src'
    nested = nearer / 'feature'
    nested.mkdir(parents=True)
    (root / '.git').mkdir()
    (nearer / 'package.json').write_text('{}')
    discovered, marker = discover_project_root(nested)
    identity = build_project_identity(discovered, marker)
    assert identity.root == nearer.resolve()
    assert identity.display_name == 'src'
    assert identity.key.startswith('src--')
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


def marked_workspace(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "package.json").write_text("{}", encoding="utf-8")
    return path


def test_multiple_mcp_roots_accept_cwd_inside_exactly_one(
    tmp_path: Path,
) -> None:
    left = marked_workspace(tmp_path / "left")
    right = marked_workspace(tmp_path / "right")
    nested = right / "src"
    nested.mkdir()
    selected = select_project_candidate(
        ProjectCandidates(mcp_roots=(left, right), protocol_cwd=nested)
    )
    assert selected == nested.resolve()


@pytest.mark.parametrize("cwd_kind", ["missing", "outside", "overlap"])
def test_multiple_mcp_roots_require_one_unambiguous_cwd(
    tmp_path: Path,
    cwd_kind: str,
) -> None:
    outer = marked_workspace(tmp_path / "outer")
    nested_root = marked_workspace(outer / "nested")
    other = marked_workspace(tmp_path / "other")
    cwd = {
        "missing": None,
        "outside": tmp_path / "outside",
        "overlap": nested_root / "src",
    }[cwd_kind]
    if cwd is not None:
        cwd.mkdir(parents=True, exist_ok=True)
    roots = (
        (outer, nested_root)
        if cwd_kind == "overlap"
        else (outer, other)
    )
    with pytest.raises(MultiRootError, match="exactly one"):
        select_project_candidate(
            ProjectCandidates(mcp_roots=roots, protocol_cwd=cwd)
        )


def test_explicit_root_precedes_multiple_mcp_roots(tmp_path: Path) -> None:
    explicit = tmp_path / 'explicit'
    candidates = ProjectCandidates(
        explicit_root=explicit,
        mcp_roots=(tmp_path / 'one', tmp_path / 'two'),
        protocol_cwd=tmp_path / 'two' / 'src',
    )

    assert select_project_candidate(candidates) == explicit.resolve()


def test_protocol_cwd_selects_unique_containing_mcp_root(tmp_path: Path) -> None:
    first = tmp_path / 'first'
    second = tmp_path / 'second'
    protocol_cwd = second / 'src' / 'feature'
    protocol_cwd.mkdir(parents=True)
    first.mkdir()
    candidates = ProjectCandidates(
        mcp_roots=(first, second),
        protocol_cwd=protocol_cwd,
    )

    assert select_project_candidate(candidates) == protocol_cwd.resolve()


def test_protocol_cwd_outside_mcp_roots_is_ambiguous(tmp_path: Path) -> None:
    candidates = ProjectCandidates(
        mcp_roots=(tmp_path / 'one', tmp_path / 'two'),
        protocol_cwd=tmp_path / 'elsewhere',
    )

    with pytest.raises(MultiRootError):
        select_project_candidate(candidates)


def test_protocol_cwd_matching_overlapping_mcp_roots_is_ambiguous(
    tmp_path: Path,
) -> None:
    outer = tmp_path / 'workspace'
    inner = outer / 'packages'
    protocol_cwd = inner / 'api'
    protocol_cwd.mkdir(parents=True)
    candidates = ProjectCandidates(
        mcp_roots=(outer, inner),
        protocol_cwd=protocol_cwd,
    )

    with pytest.raises(MultiRootError):
        select_project_candidate(candidates)


def test_malformed_git_pointer_raises_resolution_error(tmp_path: Path) -> None:
    root = tmp_path / 'workspace'
    root.mkdir()
    (root / '.git').write_text('not-a-git-pointer\n')

    discovered, marker = discover_project_root(root)

    with pytest.raises(ProjectResolutionError, match='Invalid .git pointer'):
        build_project_identity(discovered, marker)


def test_malformed_git_pointer_prefix_raises_resolution_error(tmp_path: Path) -> None:
    root = tmp_path / 'workspace'
    git_dir = tmp_path / 'git-dir'
    root.mkdir()
    git_dir.mkdir()
    (root / '.git').write_text(f'worktree: {git_dir}\n')

    with pytest.raises(ProjectResolutionError, match='Invalid .git pointer'):
        build_project_identity(root, '.git')


def test_empty_gitdir_raises_resolution_error(tmp_path: Path) -> None:
    root = tmp_path / 'workspace'
    root.mkdir()
    (root / '.git').write_text('gitdir: \n')

    with pytest.raises(ProjectResolutionError):
        build_project_identity(root, '.git')


def test_missing_gitdir_target_raises_resolution_error(tmp_path: Path) -> None:
    root = tmp_path / 'workspace'
    root.mkdir()
    (root / '.git').write_text('gitdir: ../missing.git\n')

    with pytest.raises(ProjectResolutionError):
        build_project_identity(root, '.git')


def test_missing_git_marker_raises_resolution_error(tmp_path: Path) -> None:
    root = tmp_path / 'workspace'
    root.mkdir()

    with pytest.raises(ProjectResolutionError):
        build_project_identity(root, '.git')


def test_empty_commondir_raises_resolution_error(tmp_path: Path) -> None:
    root = tmp_path / 'workspace'
    git_dir = tmp_path / 'main.git' / 'worktrees' / 'workspace'
    root.mkdir()
    git_dir.mkdir(parents=True)
    (root / '.git').write_text(f'gitdir: {git_dir}\n')
    (git_dir / 'commondir').write_text(' \n')

    with pytest.raises(ProjectResolutionError):
        build_project_identity(root, '.git')


def test_missing_commondir_target_raises_resolution_error(tmp_path: Path) -> None:
    root = tmp_path / 'workspace'
    git_dir = tmp_path / 'main.git' / 'worktrees' / 'workspace'
    root.mkdir()
    git_dir.mkdir(parents=True)
    (root / '.git').write_text(f'gitdir: {git_dir}\n')
    (git_dir / 'commondir').write_text('../../missing.git\n')

    with pytest.raises(ProjectResolutionError):
        build_project_identity(root, '.git')


def test_invalid_utf8_gitdir_raises_resolution_error(tmp_path: Path) -> None:
    root = tmp_path / 'workspace'
    root.mkdir()
    (root / '.git').write_bytes(b'gitdir: \xff\n')

    with pytest.raises(ProjectResolutionError):
        build_project_identity(root, '.git')


def test_invalid_utf8_commondir_raises_resolution_error(tmp_path: Path) -> None:
    root = tmp_path / 'workspace'
    git_dir = tmp_path / 'main.git' / 'worktrees' / 'workspace'
    root.mkdir()
    git_dir.mkdir(parents=True)
    (root / '.git').write_text(f'gitdir: {git_dir}\n')
    (git_dir / 'commondir').write_bytes(b'\xff\n')

    with pytest.raises(ProjectResolutionError):
        build_project_identity(root, '.git')


@pytest.mark.parametrize('metadata_name', ['.git', 'commondir'])
def test_git_metadata_read_failure_raises_resolution_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata_name: str,
) -> None:
    root = tmp_path / 'workspace'
    git_dir = tmp_path / 'main.git' / 'worktrees' / 'workspace'
    root.mkdir()
    git_dir.mkdir(parents=True)
    dot_git = root / '.git'
    common_file = git_dir / 'commondir'
    dot_git.write_text(f'gitdir: {git_dir}\n')
    common_file.write_text('../..\n')
    failing_path = dot_git if metadata_name == '.git' else common_file
    original_read_text = Path.read_text

    def read_text(
        path: Path,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> str:
        if path == failing_path:
            raise OSError('simulated metadata read failure')
        return original_read_text(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, 'read_text', read_text)

    with pytest.raises(ProjectResolutionError):
        build_project_identity(root, '.git')


def test_sibling_worktrees_share_non_dot_git_common_owner(tmp_path: Path) -> None:
    common = tmp_path / 'repositories' / 'project.git'
    identities = []
    for name in ('first', 'second'):
        worktree = tmp_path / name
        git_dir = common / 'worktrees' / name
        worktree.mkdir()
        git_dir.mkdir(parents=True)
        (worktree / '.git').write_text(f'gitdir: {git_dir}\n')
        (git_dir / 'commondir').write_text('../..\n')
        identities.append(build_project_identity(*discover_project_root(worktree)))

    assert identities[0].canonical_owner == common.resolve()
    assert identities[1].canonical_owner == common.resolve()
    assert identities[0].key == identities[1].key


def test_symlink_alias_builds_repeatable_identity(tmp_path: Path) -> None:
    root = tmp_path / 'workspace'
    alias = tmp_path / 'alias'
    root.mkdir()
    (root / 'package.json').write_text('{}')
    try:
        alias.symlink_to(root, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip('symlinks are not supported on this platform')

    direct = build_project_identity(*discover_project_root(root))
    through_alias = build_project_identity(*discover_project_root(alias))

    assert through_alias == direct


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
    left = ProjectIdentity(
        tmp_path / 'left',
        'left',
        'left--111111111111',
        None,
        tmp_path / 'left',
    )
    right = ProjectIdentity(
        tmp_path / 'right',
        'right',
        'right--222222222222',
        None,
        tmp_path / 'right',
    )
    registry.adopt_legacy('legacy', left)
    with pytest.raises(LegacyAliasConflict):
        registry.adopt_legacy('legacy', right)
    conflicted = json.loads(registry.path.read_text(encoding='utf-8'))
    assert right.key not in conflicted['projects']
    assert list(registry.path.parent.glob('.projects.json.*.tmp')) == []
    scope = registry.adopt_legacy('legacy', right, force_reassign=True)
    assert scope.aliases == ('legacy',)


def test_registry_digest_cas_retries_once_from_fresh_external_state(
    tmp_path: Path,
) -> None:
    registry = ProjectRegistry(tmp_path / '.memory')
    identity = ProjectIdentity(
        tmp_path / 'root',
        'root',
        'root--111111111111',
        None,
        tmp_path / 'root',
    )
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
    identity = ProjectIdentity(
        tmp_path / 'root',
        'root',
        'root--111111111111',
        None,
        tmp_path / 'root',
    )
    revision = 0

    def change_every_time(path: Path) -> None:
        nonlocal revision
        revision += 1
        path.write_text(
            json.dumps(
                {
                    'schema_version': 1,
                    'projects': {},
                    'legacy_aliases': {},
                    'revision': revision,
                }
            )
            + '\n',
            encoding='utf-8',
        )

    registry._before_replace = change_every_time
    with pytest.raises(ProjectRegistryConflict):
        registry.register(identity)
    assert json.loads(registry.path.read_text(encoding='utf-8'))['revision'] == 2
    assert list(registry.path.parent.glob('.projects.json.*.tmp')) == []


def test_registry_discards_prepared_temp_after_non_cas_error(tmp_path: Path) -> None:
    registry = ProjectRegistry(tmp_path / '.memory')
    identity = ProjectIdentity(
        tmp_path / 'root',
        'root',
        'root--111111111111',
        None,
        tmp_path / 'root',
    )

    def fail_before_replace(path: Path) -> None:
        raise OSError('simulated registry failure')

    registry._before_replace = fail_before_replace
    with pytest.raises(OSError, match='simulated registry failure'):
        registry.register(identity)

    assert not registry.path.exists()
    assert list(registry.path.parent.glob('.projects.json.*.tmp')) == []


@pytest.mark.parametrize('existing', ['', '{not valid json'])
def test_registry_rejects_empty_or_malformed_existing_state(
    tmp_path: Path,
    existing: str,
) -> None:
    registry = ProjectRegistry(tmp_path / '.memory')
    registry.path.parent.mkdir(parents=True)
    registry.path.write_text(existing, encoding='utf-8')
    identity = ProjectIdentity(
        tmp_path / 'root',
        'root',
        'root--111111111111',
        None,
        tmp_path / 'root',
    )

    with pytest.raises(ProjectResolutionError):
        registry.register(identity)

    assert registry.path.read_text(encoding='utf-8') == existing


@pytest.mark.parametrize(
    'raw',
    [
        pytest.param(b'\xff', id='invalid-utf8'),
        pytest.param(b'[]', id='non-object'),
        pytest.param(
            json.dumps(
                {
                    'schema_version': 2,
                    'projects': {},
                    'legacy_aliases': {},
                }
            ).encode('utf-8'),
            id='wrong-schema',
        ),
        pytest.param(
            json.dumps(
                {
                    'schema_version': 1,
                    'projects': {
                        'root--111111111111': {
                            'display_name': ['root'],
                            'roots': 'not-a-list',
                        }
                    },
                    'legacy_aliases': {},
                }
            ).encode('utf-8'),
            id='invalid-project-record',
        ),
        pytest.param(
            json.dumps(
                {
                    'schema_version': 1,
                    'projects': {},
                    'legacy_aliases': {'legacy': 'missing--111111111111'},
                }
            ).encode('utf-8'),
            id='dangling-alias',
        ),
    ],
)
def test_registry_rejects_semantically_corrupt_existing_state(
    tmp_path: Path,
    raw: bytes,
) -> None:
    registry = ProjectRegistry(tmp_path / '.memory')
    registry.path.parent.mkdir(parents=True)
    registry.path.write_bytes(raw)
    identity = ProjectIdentity(
        tmp_path / 'root',
        'root',
        'root--111111111111',
        None,
        tmp_path / 'root',
    )

    with pytest.raises(ProjectResolutionError):
        registry.register(identity)

    assert registry.path.read_bytes() == raw
    assert list(registry.path.parent.glob('.projects.json.*.tmp')) == []


def test_registry_rejects_inconsistent_existing_identity(tmp_path: Path) -> None:
    registry = ProjectRegistry(tmp_path / '.memory')
    registry.path.parent.mkdir(parents=True)
    existing = {
        'schema_version': 1,
        'projects': {
            'root--111111111111': {
                'display_name': 'different-name',
                'roots': ['/existing/root'],
            }
        },
        'legacy_aliases': {},
    }
    registry.path.write_text(json.dumps(existing) + '\n', encoding='utf-8')
    before = registry.path.read_bytes()
    identity = ProjectIdentity(
        tmp_path / 'root',
        'root',
        'root--111111111111',
        None,
        tmp_path / 'root',
    )

    with pytest.raises(ProjectResolutionError, match='display name'):
        registry.register(identity)

    assert registry.path.read_bytes() == before


@pytest.mark.parametrize(
    'legacy_key',
    ['', '   ', '.', '..', 'nested/name', 'nested\\name', 'nul\x00name', 'line\nbreak'],
)
def test_adopt_legacy_rejects_unsafe_local_basename(
    tmp_path: Path,
    legacy_key: str,
) -> None:
    registry = ProjectRegistry(tmp_path / '.memory')
    identity = ProjectIdentity(
        tmp_path / 'root',
        'root',
        'root--111111111111',
        None,
        tmp_path / 'root',
    )

    with pytest.raises(ProjectResolutionError, match='legacy alias'):
        registry.adopt_legacy(legacy_key, identity)

    assert not registry.path.exists()


@pytest.mark.parametrize(
    'legacy_key',
    ['', '   ', '.', '..', 'nested/name', 'nested\\name', 'nul\x00name', 'line\nbreak'],
)
def test_registry_rejects_loaded_unsafe_legacy_alias(
    tmp_path: Path,
    legacy_key: str,
) -> None:
    registry = ProjectRegistry(tmp_path / '.memory')
    registry.path.parent.mkdir(parents=True)
    existing = {
        'schema_version': 1,
        'projects': {
            'root--111111111111': {
                'display_name': 'root',
                'roots': ['/existing/root'],
            }
        },
        'legacy_aliases': {legacy_key: 'root--111111111111'},
    }
    registry.path.write_text(json.dumps(existing) + '\n', encoding='utf-8')
    before = registry.path.read_bytes()
    identity = ProjectIdentity(
        tmp_path / 'root',
        'root',
        'root--111111111111',
        None,
        tmp_path / 'root',
    )

    with pytest.raises(ProjectResolutionError, match='legacy alias'):
        registry.register(identity)

    assert registry.path.read_bytes() == before


def test_adopt_legacy_accepts_safe_unicode_and_spaces(tmp_path: Path) -> None:
    registry = ProjectRegistry(tmp_path / '.memory')
    identity = ProjectIdentity(
        tmp_path / 'root',
        'root',
        'root--111111111111',
        None,
        tmp_path / 'root',
    )

    scope = registry.adopt_legacy('Kunden Akte Ü', identity)

    assert scope.aliases == ('Kunden Akte Ü',)


def test_project_scope_orders_aliases_after_canonical_storage_key(tmp_path: Path) -> None:
    registry = ProjectRegistry(tmp_path / '.memory')
    identity = ProjectIdentity(
        tmp_path / 'root',
        'root',
        'root--111111111111',
        None,
        tmp_path / 'root',
    )

    registry.adopt_legacy('zeta', identity)
    scope = registry.adopt_legacy('alpha', identity)

    assert scope.aliases == ('alpha', 'zeta')
    assert scope.storage_keys == (identity.key, 'alpha', 'zeta')


@pytest.mark.parametrize(
    'storage_key',
    ['', '   ', '.', '..', '../outside', 'nested/name', 'nested\\name', '/absolute'],
)
def test_storage_key_validator_rejects_non_local_paths(storage_key: str) -> None:
    with pytest.raises(ProjectResolutionError, match='storage key'):
        validate_storage_key(storage_key)


def test_registry_resolves_alias_and_canonical_key_without_writing(tmp_path: Path) -> None:
    registry = ProjectRegistry(tmp_path / '.memory')
    identity = ProjectIdentity(
        tmp_path / 'root',
        'root',
        'root--111111111111',
        None,
        tmp_path / 'root',
    )
    registry.adopt_legacy('zeta', identity)
    registry.adopt_legacy('alpha', identity)
    before = registry.path.read_bytes()

    from_alias = registry.resolve('alpha')
    from_canonical = registry.resolve(identity.key)

    assert from_alias == from_canonical
    assert from_alias is not None
    assert from_alias.identity.key == identity.key
    assert from_alias.storage_keys == (identity.key, 'alpha', 'zeta')
    assert registry.path.read_bytes() == before


def test_registry_resolution_rejects_unsafe_key_without_creating_state(
    tmp_path: Path,
) -> None:
    registry = ProjectRegistry(tmp_path / '.memory')

    with pytest.raises(ProjectResolutionError, match='storage key'):
        registry.resolve('../outside')

    assert not registry.path.exists()
