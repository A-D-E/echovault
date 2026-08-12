from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal, Protocol


class InstallScope(str, Enum):
    USER = "user"
    PROJECT = "project"


class InstallMode(str, Enum):
    NATIVE = "native"
    DIRECT = "direct"


@dataclass(frozen=True)
class AdapterCapabilities:
    mcp: bool
    rules: bool
    skills: bool
    extensions: bool
    hooks: bool


@dataclass(frozen=True)
class IntegrationOptions:
    scope: InstallScope
    mode: InstallMode
    config_root: Path | None
    project_root: Path | None
    command: str | None
    force_managed: bool = False
    config_root_explicit: bool = False


@dataclass(frozen=True)
class IntegrationResult:
    status: Literal["installed", "updated", "unchanged", "removed"]
    message: str
    paths: tuple[Path, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiagnosticFinding:
    code: str
    status: Literal[
        "ok",
        "degraded",
        "warning",
        "unhealthy",
        "shadowed",
    ]
    message: str
    path: Path | None = None
    command: str | None = None


class IntegrationAdapter(Protocol):
    integration_id: str
    agent: str
    capabilities: AdapterCapabilities

    def setup(self, options: IntegrationOptions) -> IntegrationResult:
        raise NotImplementedError

    def uninstall(self, options: IntegrationOptions) -> IntegrationResult:
        raise NotImplementedError

    def diagnose(
        self,
        options: IntegrationOptions,
    ) -> tuple[DiagnosticFinding, ...]:
        raise NotImplementedError
