from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from memory.core import MemoryService
from memory.models import RawMemoryInput
from memory.integrations.gemini import GeminiAdapter
from memory.integrations.process import CommandResult
from memory.integrations.types import InstallMode, InstallScope, IntegrationOptions


VALID_EVENT = {
    "session_id": "session-1",
    "transcript_path": "/must/not/be-read.json",
    "cwd": "/workspace/repo",
    "hook_event_name": "BeforeAgent",
    "timestamp": "2026-07-14T12:00:00Z",
    "prompt": "Find marker ALPHA-42",
}


class SeededServiceFactory:
    def __init__(self, service: MemoryService) -> None:
        self.service = service

    def __call__(self) -> MemoryService:
        return self.service


def retrieved_count(factory: SeededServiceFactory, marker: str) -> int:
    row = factory.service.db.conn.execute(
        "SELECT retrieved_count FROM memories WHERE what LIKE ?",
        (f"%{marker}%",),
    ).fetchone()
    assert row is not None
    return int(row["retrieved_count"] or 0)


class RecordingRunner:
    def __init__(
        self,
        *,
        installed: bool = False,
        installed_version: str = "0.6.0",
        client_version: str = "0.50.0",
        list_on_stderr: bool = False,
    ) -> None:
        self.installed = installed
        self.installed_version = installed_version
        self.client_version = client_version
        self.list_on_stderr = list_on_stderr
        self.argv: list[list[str]] = []
        self.calls: list[dict[str, object]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        capture_output: bool = True,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        _ = timeout
        command = list(argv)
        self.argv.append(command)
        self.calls.append(
            {
                "argv": command,
                "capture_output": capture_output,
                "cwd": cwd,
                "env": None if env is None else dict(env),
            }
        )
        if command == ["gemini", "--version"]:
            return CommandResult(0, f"{self.client_version}\n", "")
        if command == ["gemini", "extensions", "list"]:
            line = (
                f"echovault {self.installed_version} local\n"
                if self.installed
                else ""
            )
            if self.list_on_stderr:
                return CommandResult(0, "", line)
            return CommandResult(0, line, "")
        if command[1:3] == ["extensions", "install"]:
            self.installed = True
        elif command[1:3] == ["extensions", "uninstall"]:
            self.installed = False
        elif command[1:3] == ["extensions", "update"]:
            self.installed_version = "0.6.0"
        return CommandResult(0, "", "")


@pytest.fixture
def gemini_050_runner() -> RecordingRunner:
    return RecordingRunner(client_version="0.50.0")


def gemini_adapter(runner: RecordingRunner) -> GeminiAdapter:
    return GeminiAdapter(runner=runner)


def project_direct_options(
    project: Path,
    *,
    config_root: Path | None = None,
    command: str | None = None,
    force_managed: bool = False,
) -> IntegrationOptions:
    return IntegrationOptions(
        scope=InstallScope.PROJECT,
        mode=InstallMode.DIRECT,
        config_root=config_root,
        project_root=project,
        command=command,
        force_managed=force_managed,
        config_root_explicit=config_root is not None,
    )


def user_direct_options(
    root: Path,
    memory: Path,
    *,
    force_managed: bool = False,
) -> IntegrationOptions:
    return IntegrationOptions(
        scope=InstallScope.USER,
        mode=InstallMode.DIRECT,
        config_root=root,
        project_root=None,
        command=str(memory),
        force_managed=force_managed,
        config_root_explicit=True,
    )


def native_options(
    home: Path,
    memory: Path | None = None,
    *,
    force_managed: bool = False,
) -> IntegrationOptions:
    return IntegrationOptions(
        scope=InstallScope.USER,
        mode=InstallMode.NATIVE,
        config_root=home / ".gemini",
        project_root=None,
        command=str(memory or Path(sys.executable)),
        force_managed=force_managed,
        config_root_explicit=False,
    )


def platform_echovault_config(home: Path) -> Path:
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "echovault"
    if os.name == "nt":
        return home / "AppData" / "Roaming" / "echovault"
    return home / ".config" / "echovault"


def seed_gemini_with_other_hook(tmp_path: Path) -> Path:
    root = tmp_path / ".gemini"
    root.mkdir()
    (root / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "BeforeAgent": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "name": "other-hook",
                                    "type": "command",
                                    "command": "other",
                                }
                            ],
                        }
                    ]
                }
            }
        )
    )
    return root


def named_hook(
    settings: dict[str, object],
    event: str,
    name: str,
) -> dict[str, object] | None:
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return None
    groups = hooks.get(event, [])
    if not isinstance(groups, list):
        return None
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            continue
        for hook in group["hooks"]:
            if isinstance(hook, dict) and hook.get("name") == name:
                return hook
    return None


@pytest.fixture
def seeded_service_factory(env_home: Path):
    service = MemoryService(str(env_home))
    service.save(
        RawMemoryInput(
            title="Marker ALPHA-42",
            what="Durable marker ALPHA-42",
            category="context",
            source="cursor",
        ),
        project="workspace-repo",
    )
    yield SeededServiceFactory(service)
    service.close()
