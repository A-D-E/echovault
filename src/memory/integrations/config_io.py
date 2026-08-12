from __future__ import annotations

import copy
import hashlib
import json
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, TypeVar

import tomlkit

from memory.safe_io import (
    ConcurrentModificationError,
    ProcessFileLock,
    digest_file,
    prepare_atomic_text,
)


class ConfigState(str, Enum):
    MISSING = "missing"
    EMPTY = "empty"
    VALID = "valid"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class ConfigDocument:
    path: Path
    state: ConfigState
    data: Any
    raw: bytes
    sha256: str | None
    mode: int | None


@dataclass(frozen=True)
class FileMutationResult:
    changed: bool
    before_sha256: str | None
    after_sha256: str | None


class ConfigMalformedError(ValueError):
    pass


class ConfigConflictError(RuntimeError):
    pass


class ConfigBoundaryError(ValueError):
    pass


def validate_target_root(
    target: Path,
    boundary: Path,
    explicit: bool,
) -> Path:
    resolved_target = target.expanduser().resolve()
    resolved_boundary = boundary.expanduser().resolve()
    if explicit:
        return resolved_target
    if not resolved_target.is_relative_to(resolved_boundary):
        raise ConfigBoundaryError(
            f"Implicit target escapes selected root: {target}"
        )
    return resolved_target


def _read_raw(path: Path) -> tuple[bytes, str | None, int | None]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return b"", None, None
    if not stat.S_ISREG(metadata.st_mode):
        raise ConfigMalformedError(f"Configuration is not a regular file: {path}")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ConfigMalformedError(
            f"Configuration cannot be read: {path}"
        ) from error
    return (
        raw,
        hashlib.sha256(raw).hexdigest(),
        stat.S_IMODE(metadata.st_mode),
    )


def read_json_strict(path: Path) -> ConfigDocument:
    raw, sha256, mode = _read_raw(path)
    if sha256 is None:
        return ConfigDocument(
            path,
            ConfigState.MISSING,
            {},
            raw,
            sha256,
            mode,
        )
    if not raw.strip():
        return ConfigDocument(
            path,
            ConfigState.EMPTY,
            {},
            raw,
            sha256,
            mode,
        )
    try:
        data = json.loads(raw.decode("utf-8"))
    except UnicodeError as error:
        raise ConfigMalformedError(
            f"Malformed JSON in {path}: invalid UTF-8"
        ) from error
    except json.JSONDecodeError as error:
        raise ConfigMalformedError(
            f"Malformed JSON in {path}:{error.lineno}:{error.colno}"
        ) from error
    if not isinstance(data, dict):
        raise ConfigMalformedError(
            f"Malformed JSON in {path}: root must be an object"
        )
    return ConfigDocument(
        path,
        ConfigState.VALID,
        data,
        raw,
        sha256,
        mode,
    )


def read_toml_strict(path: Path) -> ConfigDocument:
    raw, sha256, mode = _read_raw(path)
    if sha256 is None:
        return ConfigDocument(
            path,
            ConfigState.MISSING,
            tomlkit.document(),
            raw,
            sha256,
            mode,
        )
    if not raw.strip():
        return ConfigDocument(
            path,
            ConfigState.EMPTY,
            tomlkit.document(),
            raw,
            sha256,
            mode,
        )
    try:
        text = raw.decode("utf-8")
        data = tomlkit.parse(text)
    except (UnicodeError, Exception) as error:
        raise ConfigMalformedError(f"Malformed TOML in {path}") from error
    return ConfigDocument(
        path,
        ConfigState.VALID,
        data,
        raw,
        sha256,
        mode,
    )


DocumentT = TypeVar("DocumentT")


def _mutate_atomic(
    path: Path,
    *,
    reader: Callable[[Path], ConfigDocument],
    mutator: Callable[[DocumentT], DocumentT],
    serializer: Callable[[DocumentT], str | None],
    max_conflict_retries: int,
) -> FileMutationResult:
    if max_conflict_retries < 0:
        raise ValueError("max_conflict_retries must not be negative")
    lock_path = Path(f"{path}.echovault.lock")
    with ProcessFileLock(lock_path):
        for attempt in range(max_conflict_retries + 1):
            document = reader(path)
            working = copy.deepcopy(document.data)
            updated = mutator(working)
            rendered = serializer(updated)
            if rendered is None:
                if document.sha256 is None:
                    return FileMutationResult(False, None, None)
                current_digest = digest_file(path)
                if current_digest != document.sha256:
                    if attempt < max_conflict_retries:
                        continue
                    raise ConfigConflictError(
                        f"Configuration changed during deletion: {path}"
                    )
                path.unlink()
                from memory.safe_io import fsync_directory

                fsync_directory(path.parent)
                return FileMutationResult(True, document.sha256, None)
            payload = rendered.encode("utf-8")
            after_sha256 = hashlib.sha256(payload).hexdigest()
            if document.sha256 == after_sha256:
                return FileMutationResult(
                    False,
                    document.sha256,
                    document.sha256,
                )

            prepared = prepare_atomic_text(path, rendered)
            try:
                current_digest = digest_file(path)
                if current_digest != document.sha256:
                    if attempt < max_conflict_retries:
                        continue
                    raise ConfigConflictError(
                        f"Configuration changed during mutation: {path}"
                    )
                try:
                    prepared.replace_if_digest(
                        document.sha256,
                        after_sha256,
                    )
                except ConcurrentModificationError as error:
                    if attempt < max_conflict_retries:
                        continue
                    raise ConfigConflictError(
                        f"Configuration changed during replacement: {path}"
                    ) from error
                return FileMutationResult(
                    True,
                    document.sha256,
                    after_sha256,
                )
            finally:
                prepared.discard()
    raise AssertionError("unreachable mutation retry state")


def mutate_json_atomic(
    path: Path,
    mutator: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    max_conflict_retries: int = 1,
    remove_if_empty: bool = False,
) -> FileMutationResult:
    return _mutate_atomic(
        path,
        reader=read_json_strict,
        mutator=mutator,
        serializer=lambda data: (
            None
            if remove_if_empty and not data
            else json.dumps(
                data,
                indent=2,
                ensure_ascii=False,
                sort_keys=False,
            )
            + "\n"
        ),
        max_conflict_retries=max_conflict_retries,
    )


def mutate_toml_atomic(
    path: Path,
    mutator: Callable[[Any], Any],
    *,
    max_conflict_retries: int = 1,
) -> FileMutationResult:
    return _mutate_atomic(
        path,
        reader=read_toml_strict,
        mutator=mutator,
        serializer=tomlkit.dumps,
        max_conflict_retries=max_conflict_retries,
    )


def _read_text_document(path: Path) -> ConfigDocument:
    raw, sha256, mode = _read_raw(path)
    state = ConfigState.MISSING if sha256 is None else ConfigState.VALID
    if sha256 is not None and not raw.strip():
        state = ConfigState.EMPTY
    try:
        text = raw.decode("utf-8")
    except UnicodeError as error:
        raise ConfigMalformedError(
            f"Malformed UTF-8 text in {path}"
        ) from error
    return ConfigDocument(path, state, text, raw, sha256, mode)


def mutate_marked_block(
    path: Path,
    *,
    marker: str,
    block: str | None,
    max_conflict_retries: int = 1,
) -> FileMutationResult:
    if not marker or any(character.isspace() for character in marker):
        raise ValueError("marker must be one non-empty token")
    begin = f"<!-- {marker}:start -->"
    end = f"<!-- {marker}:end -->"

    def update(content: str) -> str:
        begin_count = content.count(begin)
        end_count = content.count(end)
        if begin_count != end_count or begin_count > 1:
            raise ConfigMalformedError(
                f"Unmatched or duplicate marked block in {path}"
            )
        managed = (
            f"{begin}\n{block.rstrip()}\n{end}"
            if block is not None
            else ""
        )
        if begin_count == 1:
            start = content.index(begin)
            finish = content.index(end, start) + len(end)
            prefix = content[:start].rstrip()
            suffix = content[finish:].strip()
            parts = [part for part in (prefix, managed, suffix) if part]
            return "\n\n".join(parts) + ("\n" if parts else "")
        if block is None:
            return content
        prefix = content.rstrip()
        return (prefix + "\n\n" if prefix else "") + managed + "\n"

    return _mutate_atomic(
        path,
        reader=_read_text_document,
        mutator=update,
        serializer=lambda content: content,
        max_conflict_retries=max_conflict_retries,
    )
