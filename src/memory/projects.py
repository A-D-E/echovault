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
        roots = tuple(_canonical(root) for root in candidates.mcp_roots)
        if candidates.protocol_cwd is not None:
            protocol_cwd = _canonical(candidates.protocol_cwd)
            matches = tuple(
                root
                for root in roots
                if protocol_cwd == root or root in protocol_cwd.parents
            )
            if len(matches) == 1:
                return matches[0]
        raise MultiRootError(
            'Multiple MCP roots require a protocol cwd contained by exactly one root'
        )
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


def _read_git_metadata(path: Path) -> str:
    try:
        value = path.read_text(encoding='utf-8').strip()
    except (OSError, UnicodeError) as error:
        raise ProjectResolutionError(f'Unable to read Git metadata: {path}') from error
    if not value:
        raise ProjectResolutionError(f'Empty Git metadata: {path}')
    return value


def _git_common_owner(root: Path) -> Path:
    dot_git = root / '.git'
    if dot_git.is_file():
        prefix, separator, value = _read_git_metadata(dot_git).partition(':')
        if separator != ':' or prefix.strip() != 'gitdir' or not value.strip():
            raise ProjectResolutionError(f'Invalid .git pointer: {dot_git}')
        git_dir = _canonical(root / value.strip())
        if not git_dir.is_dir():
            raise ProjectResolutionError(f'Git directory does not exist: {git_dir}')
    elif dot_git.is_dir():
        git_dir = _canonical(dot_git)
    else:
        raise ProjectResolutionError(f'Missing .git metadata: {dot_git}')
    common_file = git_dir / 'commondir'
    if common_file.is_file():
        common_dir = _canonical(git_dir / _read_git_metadata(common_file))
        if not common_dir.is_dir():
            raise ProjectResolutionError(
                f'Common Git directory does not exist: {common_dir}'
            )
    elif common_file.exists():
        raise ProjectResolutionError(f'Invalid commondir metadata: {common_file}')
    else:
        common_dir = git_dir
    return common_dir.parent if common_dir.name == '.git' else common_dir


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
