from pathlib import Path

import pytest

from memory.integrations.ownership import (
    LocalTreeFilesystem,
    OwnershipConflict,
    load_manifest,
    recover_managed_tree_swap,
    replace_managed_tree,
    verify_managed_content,
)


def rendered_fixture(version: str = "0.5.0") -> dict[str, bytes]:
    return {
        "rules/echovault.mdc": f"policy {version}\n".encode(),
        "skills/echovault/SKILL.md": f"skill {version}\n".encode(),
    }


def install_fixture_tree(tmp_path: Path) -> Path:
    root = tmp_path / "managed-tree"
    replace_managed_tree(root, rendered_fixture(), force_managed=False)
    return root


def upgraded_fixture() -> dict[str, bytes]:
    return rendered_fixture("0.6.0")


class SimulatedTreeSwapCrash(RuntimeError):
    pass


class CrashInjectingTreeFilesystem:
    def __init__(self, crash_after: str) -> None:
        self.delegate = LocalTreeFilesystem()
        self.crash_after = crash_after

    def replace(self, source: Path, target: Path) -> None:
        self.delegate.replace(source, target)

    def unlink(self, path: Path) -> None:
        self.delegate.unlink(path)

    def fsync_directory(self, path: Path) -> None:
        self.delegate.fsync_directory(path)

    def checkpoint(self, phase: str) -> None:
        if phase == self.crash_after:
            raise SimulatedTreeSwapCrash(phase)


def test_modified_managed_file_requires_force(tmp_path: Path) -> None:
    root = install_fixture_tree(tmp_path)
    (root / "rules" / "echovault.mdc").write_text("user edit")
    conflicts = verify_managed_content(root, load_manifest(root))
    assert [conflict.path for conflict in conflicts] == [
        "rules/echovault.mdc"
    ]
    with pytest.raises(OwnershipConflict):
        replace_managed_tree(
            root,
            rendered_fixture(),
            force_managed=False,
        )


def test_force_never_claims_unrelated_file(tmp_path: Path) -> None:
    root = install_fixture_tree(tmp_path)
    (root / "notes.txt").write_text("user data")
    replace_managed_tree(root, upgraded_fixture(), force_managed=True)
    assert (root / "notes.txt").read_text() == "user data"


@pytest.mark.parametrize(
    "crash_after",
    ["journal", "backup-rename", "target-rename"],
)
def test_tree_swap_recovers_after_each_durable_phase(
    tmp_path: Path,
    crash_after: str,
) -> None:
    root = install_fixture_tree(tmp_path)
    filesystem = CrashInjectingTreeFilesystem(crash_after)
    with pytest.raises(SimulatedTreeSwapCrash):
        replace_managed_tree(
            root,
            upgraded_fixture(),
            force_managed=False,
            _filesystem=filesystem,
        )
    recover_managed_tree_swap(root)
    assert verify_managed_content(root, load_manifest(root)) == ()
    assert not list(root.parent.glob(".echovault-tree-*.json"))
    assert not list(root.parent.glob(".echovault-backup-*"))


def test_unmarked_nonempty_tree_is_never_claimed_even_with_force(
    tmp_path: Path,
) -> None:
    root = tmp_path / "managed-tree"
    root.mkdir()
    (root / "user.txt").write_text("mine")
    with pytest.raises(OwnershipConflict):
        replace_managed_tree(root, rendered_fixture(), force_managed=True)
    assert (root / "user.txt").read_text() == "mine"


@pytest.mark.parametrize("unsafe", ["../escape", "/absolute", "a/../../b"])
def test_managed_asset_paths_reject_traversal(
    tmp_path: Path,
    unsafe: str,
) -> None:
    with pytest.raises(OwnershipConflict):
        replace_managed_tree(
            tmp_path / "root",
            {unsafe: b"bad"},
            force_managed=False,
        )
