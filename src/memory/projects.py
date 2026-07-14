from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from memory.safe_io import (
    ConcurrentModificationError,
    ProcessFileLock,
    prepare_atomic_text,
)


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


def _validate_legacy_key(value: object) -> str:
    if not isinstance(value, str):
        raise ProjectResolutionError('Invalid legacy alias: expected a string basename')
    if not value.strip() or value in {'.', '..'}:
        raise ProjectResolutionError('Invalid legacy alias: expected a non-empty basename')
    if '/' in value or '\\' in value:
        raise ProjectResolutionError('Invalid legacy alias: expected a single local basename')
    if any(unicodedata.category(character) == 'Cc' for character in value):
        raise ProjectResolutionError('Invalid legacy alias: control characters are not allowed')
    if Path(value).name != value or Path(value).parts != (value,):
        raise ProjectResolutionError('Invalid legacy alias: expected a single local basename')
    return value


class ProjectRegistry:
    def __init__(self, memory_home: Path):
        self.memory_home = memory_home
        self.path = memory_home / 'projects.json'
        self.lock_path = memory_home / 'projects.json.lock'
        self._before_replace: Callable[[Path], None] = lambda path: None

    @staticmethod
    def _default_data() -> dict[str, object]:
        return {
            'schema_version': 1,
            'projects': {},
            'legacy_aliases': {},
        }

    def _read_with_digest(self) -> tuple[dict[str, object], str | None]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return self._default_data(), None
        except OSError as error:
            raise ProjectResolutionError('Unable to read projects.json') from error

        if not raw:
            raise ProjectResolutionError('Existing projects.json is empty')
        try:
            data = json.loads(raw.decode('utf-8'))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ProjectResolutionError('Existing projects.json is malformed') from error
        if not isinstance(data, dict):
            raise ProjectResolutionError('Existing projects.json must contain an object')
        self._validate_data(data)
        return data, hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _validate_data(data: dict[str, object]) -> None:
        schema_version = data.get('schema_version')
        if type(schema_version) is not int or schema_version != 1:
            raise ProjectResolutionError('Unsupported projects.json schema version')
        projects = data.get('projects')
        aliases = data.get('legacy_aliases')
        if not isinstance(projects, dict) or not isinstance(aliases, dict):
            raise ProjectResolutionError('Existing projects.json has invalid mappings')
        for key, record in projects.items():
            if not isinstance(key, str) or not isinstance(record, dict):
                raise ProjectResolutionError('Existing projects.json has invalid projects')
            display_name = record.get('display_name')
            roots = record.get('roots')
            if not isinstance(display_name, str) or not isinstance(roots, list):
                raise ProjectResolutionError('Existing projects.json has invalid project records')
            if not all(isinstance(root, str) for root in roots):
                raise ProjectResolutionError('Existing projects.json has invalid project roots')
        for alias, project_key in aliases.items():
            _validate_legacy_key(alias)
            if not isinstance(project_key, str):
                raise ProjectResolutionError(
                    'Existing projects.json has invalid legacy aliases'
                )
            if project_key not in projects:
                raise ProjectResolutionError(
                    'Existing projects.json has a dangling legacy alias'
                )

    @staticmethod
    def _register_identity(data: dict[str, object], identity: ProjectIdentity) -> None:
        projects = data['projects']
        assert isinstance(projects, dict)
        if identity.key in projects:
            record = projects[identity.key]
            assert isinstance(record, dict)
            if record['display_name'] != identity.display_name:
                raise ProjectResolutionError(
                    'Existing project display name conflicts with this identity'
                )
        else:
            record = {'display_name': identity.display_name, 'roots': []}
            projects[identity.key] = record
        assert isinstance(record, dict)
        roots = record['roots']
        assert isinstance(roots, list)
        root = str(identity.root)
        if root not in roots:
            roots.append(root)

    @staticmethod
    def _scope(identity: ProjectIdentity, data: dict[str, object]) -> ProjectScope:
        aliases = data['legacy_aliases']
        assert isinstance(aliases, dict)
        assigned = tuple(
            sorted(alias for alias, project_key in aliases.items() if project_key == identity.key)
        )
        return ProjectScope(identity=identity, aliases=assigned)

    def _mutate(
        self,
        transform: Callable[[dict[str, object]], ProjectScope],
    ) -> ProjectScope:
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
                except BaseException:
                    prepared.discard()
                    raise
                return result
        raise AssertionError('unreachable')

    def register(self, identity: ProjectIdentity) -> ProjectScope:
        def transform(data: dict[str, object]) -> ProjectScope:
            self._register_identity(data, identity)
            projects = data['projects']
            aliases = data['legacy_aliases']
            assert isinstance(projects, dict)
            assert isinstance(aliases, dict)
            legacy_dir = self.memory_home / 'vault' / identity.display_name
            if legacy_dir.is_dir():
                legacy_key = _validate_legacy_key(identity.display_name)
            else:
                legacy_key = None
            if legacy_key is not None and legacy_key not in aliases:
                same_name = [
                    key
                    for key, record in projects.items()
                    if isinstance(record, dict)
                    and record.get('display_name') == identity.display_name
                ]
                if same_name == [identity.key]:
                    aliases[legacy_key] = identity.key
            return self._scope(identity, data)

        return self._mutate(transform)

    def adopt_legacy(
        self,
        legacy_key: str,
        identity: ProjectIdentity,
        force_reassign: bool = False,
    ) -> ProjectScope:
        legacy_key = _validate_legacy_key(legacy_key)

        def transform(data: dict[str, object]) -> ProjectScope:
            self._register_identity(data, identity)
            aliases = data['legacy_aliases']
            assert isinstance(aliases, dict)
            existing = aliases.get(legacy_key)
            if existing is not None and existing != identity.key and not force_reassign:
                raise LegacyAliasConflict(
                    f'Legacy alias {legacy_key!r} is already assigned to another project'
                )
            aliases[legacy_key] = identity.key
            return self._scope(identity, data)

        return self._mutate(transform)


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
