from pathlib import Path

import pytest

from memory.projects import (
    MultiRootError,
    ProjectCandidates,
    ProjectResolutionError,
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


def test_malformed_git_pointer_raises_resolution_error(tmp_path: Path) -> None:
    root = tmp_path / 'workspace'
    root.mkdir()
    (root / '.git').write_text('not-a-git-pointer\n')

    discovered, marker = discover_project_root(root)

    with pytest.raises(ProjectResolutionError, match='Invalid .git pointer'):
        build_project_identity(discovered, marker)
