from __future__ import annotations

import argparse
from pathlib import Path

from memory.integrations.asset_io import read_package_asset
from memory.safe_io import prepare_atomic_text


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DESTINATION = REPOSITORY_ROOT / "skills/echovault/SKILL.md"


def sync_skill(destination: Path, check: bool = False) -> bool:
    """Synchronize the repository mirror; return whether it was current."""

    payload = read_package_asset("common/echovault-skill.md")
    try:
        current = destination.read_bytes()
    except FileNotFoundError:
        current = None
    if current == payload:
        return True
    if check:
        return False
    prepared = prepare_atomic_text(destination, payload.decode("utf-8"))
    try:
        prepared.replace()
    finally:
        prepared.discard()
    return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Synchronize the canonical EchoVault skill mirror."
    )
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    current = sync_skill(DEFAULT_DESTINATION, check=arguments.check)
    if arguments.check and not current:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
