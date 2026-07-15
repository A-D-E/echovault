from __future__ import annotations

import json
from pathlib import Path

from memory.integrations.cursor import CursorAdapter
from memory.integrations.registry import get_adapter
from memory.integrations.types import InstallMode, InstallScope, IntegrationOptions


def cursor_adapter() -> CursorAdapter:
    adapter = get_adapter("cursor")
    assert isinstance(adapter, CursorAdapter)
    return adapter


def project_options(
    project: Path,
    *,
    command: str | None = None,
    force_managed: bool = False,
    config_root: Path | None = None,
    config_root_explicit: bool = False,
) -> IntegrationOptions:
    return IntegrationOptions(
        scope=InstallScope.PROJECT,
        mode=InstallMode.DIRECT,
        config_root=config_root,
        project_root=project,
        command=command,
        force_managed=force_managed,
        config_root_explicit=config_root_explicit,
    )


def user_options(
    cursor_root: Path,
    *,
    command: str,
    force_managed: bool = False,
) -> IntegrationOptions:
    return IntegrationOptions(
        scope=InstallScope.USER,
        mode=InstallMode.NATIVE,
        config_root=cursor_root,
        project_root=None,
        command=command,
        force_managed=force_managed,
        config_root_explicit=True,
    )


def snapshot_tree(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def seed_project_with_other_mcp(tmp_path: Path) -> Path:
    project = tmp_path / "repo"
    cursor = project / ".cursor"
    cursor.mkdir(parents=True)
    (cursor / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "other": {
                        "command": "other",
                        "env": {"TOKEN": "unchanged"},
                    },
                },
            }
        )
    )
    return project
