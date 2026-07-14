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
        prefix, separator, value = dot_git.read_text(encoding='utf-8').partition(':')
        if separator != ':' or prefix.strip() != 'gitdir':
            raise ProjectResolutionError(f'Invalid .git pointer: {dot_git}')
        git_dir = _canonical(root / value.strip())
    common_file = git_dir / 'commondir'
    common_dir = (
        _canonical(git_dir / common_file.read_text(encoding='utf-8').strip())
        if common_file.is_file()
        else git_dir
    )
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
