from __future__ import annotations

import argparse
import re
import sys
import tarfile
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from memory.integrations.asset_io import REQUIRED_PACKAGE_ASSETS


@dataclass(frozen=True)
class ArchiveInspection:
    path: Path
    members: tuple[str, ...]
    missing_assets: tuple[str, ...]
    duplicate_members: tuple[str, ...]
    unsafe_members: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not (
            self.missing_assets
            or self.duplicate_members
            or self.unsafe_members
        )


def _unsafe(name: str) -> bool:
    candidate = PurePosixPath(name)
    return (
        not name
        or "\\" in name
        or candidate.is_absolute()
        or ".." in candidate.parts
        or re.match(r"^[A-Za-z]:", name) is not None
    )


def _inspection(
    path: Path,
    names: list[str],
    required: tuple[str, ...],
) -> ArchiveInspection:
    counts = Counter(names)
    members = tuple(sorted(names))
    return ArchiveInspection(
        path=path.resolve(),
        members=members,
        missing_assets=tuple(
            sorted(name for name in required if name not in counts)
        ),
        duplicate_members=tuple(
            sorted(name for name, count in counts.items() if count > 1)
        ),
        unsafe_members=tuple(sorted(name for name in names if _unsafe(name))),
    )


def inspect_wheel(path: Path) -> ArchiveInspection:
    with zipfile.ZipFile(path) as archive:
        names = [item.filename for item in archive.infolist()]
    required = tuple(
        f"memory/integrations/assets/{asset}"
        for asset in REQUIRED_PACKAGE_ASSETS
    )
    return _inspection(path, names, required)


def inspect_sdist(path: Path) -> ArchiveInspection:
    with tarfile.open(path, "r:*") as archive:
        names = [item.name for item in archive.getmembers()]
    safe_names = [name for name in names if not _unsafe(name)]
    top_levels = {
        PurePosixPath(name).parts[0]
        for name in safe_names
        if PurePosixPath(name).parts
    }
    if len(top_levels) == 1:
        top = next(iter(top_levels))
        required = (
            f"{top}/pyproject.toml",
            *(
                f"{top}/src/memory/integrations/assets/{asset}"
                for asset in REQUIRED_PACKAGE_ASSETS
            ),
        )
    else:
        required = ("<single-top-level-distribution-directory>",)
    return _inspection(path, names, tuple(required))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify EchoVault wheel and sdist runtime assets."
    )
    parser.add_argument("archives", type=Path, nargs="+")
    arguments = parser.parse_args()
    failed = False
    for path in arguments.archives:
        inspection = (
            inspect_wheel(path)
            if path.suffix == ".whl"
            else inspect_sdist(path)
        )
        for label, values in (
            ("missing", inspection.missing_assets),
            ("duplicate", inspection.duplicate_members),
            ("unsafe", inspection.unsafe_members),
        ):
            for value in values:
                print(f"{path}: {label}: {value}", file=sys.stderr)
        failed = failed or not inspection.valid
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
