from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, cast

from memory.integrations.config_io import read_json_strict
from memory.safe_io import ProcessFileLock, fsync_directory, prepare_atomic_text


MANIFEST_NAME = ".echovault-managed.json"


class OwnershipConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class ManagedArtifact:
    path: str
    kind: Literal["file", "json-entry", "marked-block"]
    sha256: str
    locator: str | None = None
    marker: str | None = None


@dataclass(frozen=True)
class OwnershipManifest:
    integration_id: str
    schema_version: int
    asset_version: str
    managed: tuple[ManagedArtifact, ...]


@dataclass(frozen=True)
class TreeSwapJournal:
    operation_id: str
    target: str
    staging: str
    backup: str
    before_sha256: str | None
    after_sha256: str
    phase: str


class TreeFilesystem(Protocol):
    def replace(self, source: Path, target: Path) -> None: ...

    def unlink(self, path: Path) -> None: ...

    def fsync_directory(self, path: Path) -> None: ...

    def checkpoint(self, phase: str) -> None: ...


class LocalTreeFilesystem:
    def replace(self, source: Path, target: Path) -> None:
        os.replace(source, target)

    def unlink(self, path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    def fsync_directory(self, path: Path) -> None:
        fsync_directory(path)

    def checkpoint(self, phase: str) -> None:
        _ = phase


def _safe_relative(value: str) -> PurePosixPath:
    candidate = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise OwnershipConflict(f"Unsafe managed artifact path: {value}")
    return candidate


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _json_pointer(value: object, locator: str) -> object:
    if not locator.startswith("/"):
        raise OwnershipConflict("JSON entry locator must be a JSON pointer")
    current = value
    for raw_part in locator.split("/")[1:]:
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, list):
            try:
                index = int(part)
            except ValueError as error:
                raise OwnershipConflict(
                    f"Managed JSON entry is missing: {locator}"
                ) from error
            if index < 0 or index >= len(current):
                raise OwnershipConflict(
                    f"Managed JSON entry is missing: {locator}"
                )
            current = current[index]
            continue
        else:
            raise OwnershipConflict(f"Managed JSON entry is missing: {locator}")
    return current


def artifact_digest(root: Path, artifact: ManagedArtifact) -> str | None:
    relative = _safe_relative(artifact.path)
    path = root.joinpath(*relative.parts)
    if artifact.kind == "file":
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(metadata.st_mode):
            return None
        return _sha256(path.read_bytes())
    if artifact.kind == "json-entry":
        if artifact.locator is None:
            raise OwnershipConflict("Managed JSON entry lacks locator")
        try:
            document = read_json_strict(path)
            value = _json_pointer(document.data, artifact.locator)
        except (ValueError, OSError, OwnershipConflict):
            return None
        return _sha256(_canonical_json(value))
    if artifact.kind == "marked-block":
        if not artifact.marker:
            raise OwnershipConflict("Managed marked block lacks marker")
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        begin = f"<!-- {artifact.marker}:start -->"
        end = f"<!-- {artifact.marker}:end -->"
        if content.count(begin) != 1 or content.count(end) != 1:
            return None
        start = content.index(begin)
        finish = content.index(end, start) + len(end)
        return _sha256(content[start:finish].encode("utf-8"))
    raise OwnershipConflict(f"Unsupported managed artifact kind: {artifact.kind}")


def _manifest_payload(manifest: OwnershipManifest) -> dict[str, object]:
    return {
        "integration_id": manifest.integration_id,
        "schema_version": manifest.schema_version,
        "asset_version": manifest.asset_version,
        "managed": [asdict(artifact) for artifact in manifest.managed],
    }


def _parse_manifest(payload: object) -> OwnershipManifest:
    if not isinstance(payload, dict):
        raise OwnershipConflict("Managed manifest must be a JSON object")
    if payload.get("schema_version") != 1:
        raise OwnershipConflict("Unsupported managed manifest schema")
    integration_id = payload.get("integration_id")
    asset_version = payload.get("asset_version")
    managed = payload.get("managed")
    if (
        not isinstance(integration_id, str)
        or not integration_id
        or not isinstance(asset_version, str)
        or not asset_version
        or not isinstance(managed, list)
    ):
        raise OwnershipConflict("Managed manifest fields are invalid")
    artifacts: list[ManagedArtifact] = []
    seen: set[tuple[str, str, str | None]] = set()
    for item in managed:
        if not isinstance(item, dict):
            raise OwnershipConflict("Managed artifact is invalid")
        if (
            not isinstance(item.get("path"), str)
            or item.get("kind") not in {"file", "json-entry", "marked-block"}
            or not isinstance(item.get("sha256"), str)
            or (
                item.get("locator") is not None
                and not isinstance(item.get("locator"), str)
            )
            or (
                item.get("marker") is not None
                and not isinstance(item.get("marker"), str)
            )
        ):
            raise OwnershipConflict("Managed artifact fields are invalid")
        try:
            artifact = ManagedArtifact(
                path=cast(str, item["path"]),
                kind=cast(
                    Literal["file", "json-entry", "marked-block"],
                    item["kind"],
                ),
                sha256=cast(str, item["sha256"]),
                locator=cast(str | None, item.get("locator")),
                marker=cast(str | None, item.get("marker")),
            )
        except KeyError as error:
            raise OwnershipConflict("Managed artifact fields are missing") from error
        _safe_relative(artifact.path)
        if artifact.kind not in {"file", "json-entry", "marked-block"}:
            raise OwnershipConflict("Managed artifact kind is invalid")
        if len(artifact.sha256) != 64 or any(
            character not in "0123456789abcdef"
            for character in artifact.sha256
        ):
            raise OwnershipConflict("Managed artifact digest is invalid")
        identity = (artifact.path, artifact.kind, artifact.locator)
        if identity in seen:
            raise OwnershipConflict("Managed artifact is duplicated")
        seen.add(identity)
        artifacts.append(artifact)
    return OwnershipManifest(
        integration_id=integration_id,
        schema_version=1,
        asset_version=asset_version,
        managed=tuple(artifacts),
    )


def load_manifest(root: Path) -> OwnershipManifest:
    path = root / MANIFEST_NAME
    try:
        document = read_json_strict(path)
    except (ValueError, OSError) as error:
        raise OwnershipConflict("Managed manifest cannot be read") from error
    if document.sha256 is None:
        raise OwnershipConflict("Managed manifest is missing")
    return _parse_manifest(document.data)


def write_manifest_atomic(root: Path, manifest: OwnershipManifest) -> Path:
    path = root / MANIFEST_NAME
    serialized = json.dumps(
        _manifest_payload(manifest),
        indent=2,
        sort_keys=True,
    ) + "\n"
    prepared = prepare_atomic_text(path, serialized)
    try:
        prepared.replace()
    finally:
        prepared.discard()
    return path


def verify_managed_content(
    root: Path,
    manifest: OwnershipManifest,
) -> tuple[ManagedArtifact, ...]:
    return tuple(
        artifact
        for artifact in manifest.managed
        if artifact_digest(root, artifact) != artifact.sha256
    )


def _tree_digest(root: Path) -> str | None:
    if not root.exists():
        return None
    if root.is_symlink() or not root.is_dir():
        raise OwnershipConflict("Managed tree must be a local directory")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise OwnershipConflict("Managed tree cannot contain symlinks")
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise OwnershipConflict("Managed tree contains a special file")
        payload = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _fsync_tree(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    directories = [path for path in root.rglob("*") if path.is_dir()]
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        fsync_directory(directory)
    fsync_directory(root)


def _manifest_for_assets(
    assets: dict[str, bytes],
    *,
    integration_id: str,
    asset_version: str,
) -> OwnershipManifest:
    artifacts = tuple(
        ManagedArtifact(
            path=path,
            kind="file",
            sha256=_sha256(payload),
        )
        for path, payload in sorted(assets.items())
    )
    return OwnershipManifest(
        integration_id=integration_id,
        schema_version=1,
        asset_version=asset_version,
        managed=artifacts,
    )


def stage_managed_tree(
    root: Path,
    assets: dict[str, bytes],
    *,
    staging: Path,
    integration_id: str = "echovault",
    asset_version: str = "current",
) -> OwnershipManifest:
    for relative, payload in assets.items():
        _safe_relative(relative)
        if not isinstance(payload, bytes):
            raise OwnershipConflict("Managed asset payload must be bytes")
    if staging.exists():
        shutil.rmtree(staging)
    if root.exists():
        _tree_digest(root)
        shutil.copytree(root, staging)
        if (root / MANIFEST_NAME).is_file():
            old_manifest = load_manifest(root)
            for artifact in old_manifest.managed:
                if artifact.kind != "file" or artifact.path in assets:
                    continue
                old_path = staging.joinpath(
                    *_safe_relative(artifact.path).parts
                )
                old_path.unlink(missing_ok=True)
    else:
        staging.mkdir(parents=True)

    for relative, payload in assets.items():
        target = staging.joinpath(*_safe_relative(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    manifest = _manifest_for_assets(
        assets,
        integration_id=integration_id,
        asset_version=asset_version,
    )
    write_manifest_atomic(staging, manifest)
    if verify_managed_content(staging, manifest):
        raise OwnershipConflict("Staged managed tree failed verification")
    _fsync_tree(staging)
    return manifest


def _journal_path(parent: Path, operation_id: str) -> Path:
    return parent / f".echovault-tree-{operation_id}.json"


def _write_tree_journal(parent: Path, journal: TreeSwapJournal) -> Path:
    path = _journal_path(parent, journal.operation_id)
    serialized = json.dumps(asdict(journal), sort_keys=True, indent=2) + "\n"
    prepared = prepare_atomic_text(path, serialized)
    try:
        prepared.replace()
    finally:
        prepared.discard()
    return path


def _load_tree_journal(path: Path, root: Path) -> TreeSwapJournal:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        journal = TreeSwapJournal(**payload)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as error:
        raise OwnershipConflict("Tree swap journal is malformed") from error
    for value in (journal.target, journal.staging, journal.backup):
        if Path(value).name != value or value in {"", ".", ".."}:
            raise OwnershipConflict("Tree swap journal path is unsafe")
    if journal.target != root.name:
        raise OwnershipConflict("Tree swap journal targets another tree")
    return journal


def _cleanup_swap(
    parent: Path,
    journal_path: Path,
    staging: Path,
    backup: Path,
    filesystem: TreeFilesystem,
) -> None:
    if staging.exists():
        filesystem.unlink(staging)
    if backup.exists():
        filesystem.unlink(backup)
    filesystem.unlink(journal_path)
    filesystem.fsync_directory(parent)


def _recover_one(
    root: Path,
    journal_path: Path,
    filesystem: TreeFilesystem,
) -> None:
    journal = _load_tree_journal(journal_path, root)
    parent = root.parent
    staging = parent / journal.staging
    backup = parent / journal.backup
    target_digest = _tree_digest(root)
    staging_digest = _tree_digest(staging)
    backup_digest = _tree_digest(backup)
    known = {None, journal.before_sha256, journal.after_sha256}
    if any(value not in known for value in (target_digest, staging_digest, backup_digest)):
        raise OwnershipConflict("Tree swap contains an unknown tree digest")

    if target_digest == journal.after_sha256:
        _cleanup_swap(parent, journal_path, staging, backup, filesystem)
        return
    if staging_digest != journal.after_sha256:
        raise OwnershipConflict("Recoverable staged managed tree is missing")
    if target_digest == journal.before_sha256 and root.exists():
        if backup.exists():
            raise OwnershipConflict("Tree swap backup unexpectedly exists")
        filesystem.replace(root, backup)
        filesystem.fsync_directory(parent)
    elif target_digest is not None:
        raise OwnershipConflict("Managed tree target has an unknown state")
    if backup_digest not in {None, journal.before_sha256}:
        raise OwnershipConflict("Managed tree backup has an unknown state")
    filesystem.replace(staging, root)
    filesystem.fsync_directory(parent)
    if _tree_digest(root) != journal.after_sha256:
        raise OwnershipConflict("Recovered managed tree digest does not match")
    _cleanup_swap(parent, journal_path, staging, backup, filesystem)


def _recover_locked(root: Path, filesystem: TreeFilesystem) -> None:
    parent = root.parent
    candidates: list[Path] = []
    for path in sorted(parent.glob(".echovault-tree-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise OwnershipConflict("Tree swap journal cannot be inspected")
        if isinstance(payload, dict) and payload.get("target") == root.name:
            candidates.append(path)
    if len(candidates) > 1:
        raise OwnershipConflict("Multiple tree swap journals target one tree")
    if candidates:
        _recover_one(root, candidates[0], filesystem)


def recover_managed_tree_swap(root: Path) -> None:
    candidate = root.expanduser().absolute()
    if os.path.lexists(candidate) and candidate.is_symlink():
        raise OwnershipConflict("Managed tree target cannot be a symlink")
    root = candidate.resolve(strict=False)
    root.parent.mkdir(parents=True, exist_ok=True)
    lock = root.parent / f".{root.name}.echovault.lock"
    filesystem = LocalTreeFilesystem()
    with ProcessFileLock(lock):
        _recover_locked(root, filesystem)


def replace_managed_tree(
    root: Path,
    assets: dict[str, bytes],
    *,
    force_managed: bool,
    integration_id: str = "echovault",
    asset_version: str = "current",
    _filesystem: TreeFilesystem | None = None,
) -> OwnershipManifest:
    candidate = root.expanduser().absolute()
    if os.path.lexists(candidate) and candidate.is_symlink():
        raise OwnershipConflict("Managed tree target cannot be a symlink")
    if (
        candidate.is_dir()
        and not (candidate / MANIFEST_NAME).is_file()
        and any(candidate.iterdir())
    ):
        raise OwnershipConflict(
            "Existing integration tree is not EchoVault-managed"
        )
    root = candidate.resolve(strict=False)
    root.parent.mkdir(parents=True, exist_ok=True)
    filesystem = _filesystem or LocalTreeFilesystem()
    lock = root.parent / f".{root.name}.echovault.lock"
    with ProcessFileLock(lock):
        _recover_locked(root, filesystem)
        before_sha256 = _tree_digest(root)
        if root.exists():
            manifest_path = root / MANIFEST_NAME
            if not manifest_path.is_file():
                if any(root.iterdir()):
                    raise OwnershipConflict(
                        "Existing integration tree is not EchoVault-managed"
                    )
            else:
                manifest = load_manifest(root)
                conflicts = verify_managed_content(root, manifest)
                if conflicts and not force_managed:
                    raise OwnershipConflict(
                        "Managed integration content was modified"
                    )

        operation_id = str(uuid.uuid4())
        staging = root.parent / f".echovault-staging-{operation_id}"
        backup = root.parent / f".echovault-backup-{operation_id}"
        try:
            manifest = stage_managed_tree(
                root,
                assets,
                staging=staging,
                integration_id=integration_id,
                asset_version=asset_version,
            )
        except BaseException:
            if staging.exists():
                filesystem.unlink(staging)
                filesystem.fsync_directory(root.parent)
            raise
        after_sha256 = _tree_digest(staging)
        assert after_sha256 is not None
        if before_sha256 == after_sha256:
            filesystem.unlink(staging)
            filesystem.fsync_directory(root.parent)
            return manifest

        journal = TreeSwapJournal(
            operation_id=operation_id,
            target=root.name,
            staging=staging.name,
            backup=backup.name,
            before_sha256=before_sha256,
            after_sha256=after_sha256,
            phase="journal",
        )
        journal_path = _write_tree_journal(root.parent, journal)
        filesystem.checkpoint("journal")
        if root.exists():
            filesystem.replace(root, backup)
            filesystem.fsync_directory(root.parent)
        journal = TreeSwapJournal(**{**asdict(journal), "phase": "backup-rename"})
        _write_tree_journal(root.parent, journal)
        filesystem.checkpoint("backup-rename")
        filesystem.replace(staging, root)
        filesystem.fsync_directory(root.parent)
        journal = TreeSwapJournal(**{**asdict(journal), "phase": "target-rename"})
        _write_tree_journal(root.parent, journal)
        filesystem.checkpoint("target-rename")
        if _tree_digest(root) != after_sha256:
            raise OwnershipConflict("Installed managed tree digest does not match")
        _cleanup_swap(root.parent, journal_path, staging, backup, filesystem)
        return manifest
