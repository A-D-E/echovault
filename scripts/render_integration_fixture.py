from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Literal

from memory.integrations.asset_io import (
    render_cursor_assets,
    render_gemini_assets,
    shell_join_command,
)
from memory.safe_io import prepare_atomic_text


def render_fixture(
    client: Literal["cursor", "gemini"],
    output: Path,
    memory_command: str,
    version: str,
) -> Path:
    """Render one client bundle without reading client or user state."""

    candidate = output.expanduser().absolute()
    if os.path.lexists(candidate) and candidate.is_symlink():
        raise FileExistsError("fixture output cannot be a symlink")
    if candidate.exists() and any(candidate.iterdir()):
        raise FileExistsError("fixture output must be absent or empty")
    root = candidate.resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    if client == "cursor":
        assets = render_cursor_assets(
            command=memory_command,
            version=version,
        )
    elif client == "gemini":
        hook_command = shell_join_command(
            [memory_command, "hook", "gemini", "before-agent"]
        )
        assets = render_gemini_assets(
            memory_command=memory_command,
            hook_command=hook_command,
            version=version,
        )
    else:
        raise ValueError(f"Unsupported client: {client}")
    for relative_path, payload in assets.items():
        target = root.joinpath(*relative_path.split("/")).resolve(
            strict=False
        )
        if not target.is_relative_to(root):
            raise ValueError("rendered asset escapes fixture root")
        prepared = prepare_atomic_text(target, payload.decode("utf-8"))
        try:
            prepared.replace()
        finally:
            prepared.discard()
    return root


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render a deterministic EchoVault client fixture."
    )
    parser.add_argument("--client", choices=("cursor", "gemini"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--memory-command", required=True)
    parser.add_argument("--version", required=True)
    arguments = parser.parse_args()
    root = render_fixture(
        arguments.client,
        arguments.output,
        arguments.memory_command,
        arguments.version,
    )
    print(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
