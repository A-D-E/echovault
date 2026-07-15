from __future__ import annotations

import os
import shutil
import stat
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from memory.config import resolve_context_mode
from memory.integrations.config_io import ConfigMalformedError, read_json_strict
from memory.integrations.ownership import (
    MANIFEST_NAME,
    OwnershipConflict,
    load_manifest,
    verify_managed_content,
)
from memory.integrations.process import CommandRunner, SubprocessRunner
from memory.integrations.types import DiagnosticFinding


DiagnosticStatus = Literal[
    "ok",
    "degraded",
    "warning",
    "unhealthy",
    "shadowed",
]


def _finding(
    code: str,
    status: DiagnosticStatus,
    message: str,
    *,
    path: Path | None = None,
    command: str | None = None,
    **metadata: object,
) -> dict[str, object]:
    finding = DiagnosticFinding(
        code=code,
        status=status,
        message=message,
        path=path,
        command=command,
    )
    payload = asdict(finding)
    if path is not None:
        payload["path"] = str(path)
    payload.update(metadata)
    return {key: value for key, value in payload.items() if value is not None}


def _cursor_project_findings(project_root: Path) -> list[dict[str, object]]:
    cursor_root = project_root / ".cursor"
    findings = [
        _finding(
            "cursor.project",
            "ok",
            f"Cursor project context resolves to {project_root}",
            path=project_root,
            project_name=project_root.name,
        ),
        _finding(
            "cursor.scope",
            "ok" if cursor_root.is_dir() else "warning",
            f"Cursor project scope: {cursor_root}",
            path=cursor_root,
        )
    ]
    manifest_path = cursor_root / MANIFEST_NAME
    if not manifest_path.is_file():
        findings.append(
            _finding(
                "cursor.manifest",
                "warning",
                "Cursor project ownership manifest is missing",
                path=manifest_path,
            )
        )
    else:
        try:
            manifest = load_manifest(cursor_root)
            conflicts = verify_managed_content(cursor_root, manifest)
            healthy = (
                manifest.integration_id == "cursor-project" and not conflicts
            )
            findings.append(
                _finding(
                    "cursor.manifest",
                    "ok" if healthy else "unhealthy",
                    (
                        "Cursor project ownership is verified"
                        if healthy
                        else "Cursor project ownership has modified claims"
                    ),
                    path=manifest_path,
                )
            )
        except OwnershipConflict as error:
            findings.append(
                _finding(
                    "cursor.manifest",
                    "unhealthy",
                    str(error),
                    path=manifest_path,
                )
            )

    mcp_path = cursor_root / "mcp.json"
    command: str | None = None
    try:
        document = read_json_strict(mcp_path)
        servers = document.data.get("mcpServers", {})
        entry = servers.get("echovault") if isinstance(servers, dict) else None
        valid = (
            isinstance(entry, dict)
            and entry.get("args") == ["mcp", "--agent", "cursor"]
            and isinstance(entry.get("command"), str)
        )
        if valid:
            command = entry["command"]
        findings.append(
            _finding(
                "cursor.mcp",
                "ok" if valid else "unhealthy",
                (
                    "Cursor uses the bound EchoVault MCP contract"
                    if valid
                    else "Cursor bound EchoVault MCP entry is missing or invalid"
                ),
                path=mcp_path,
                command=command,
            )
        )
    except ConfigMalformedError as error:
        findings.append(
            _finding(
                "cursor.mcp",
                "unhealthy",
                str(error),
                path=mcp_path,
            )
        )

    for name, relative in (
        ("rule", "rules/echovault.mdc"),
        ("skill", "skills/echovault/SKILL.md"),
    ):
        path = cursor_root.joinpath(*relative.split("/"))
        findings.append(
            _finding(
                f"cursor.{name}",
                "ok" if path.is_file() else "unhealthy",
                (
                    f"Cursor curated {name} is installed"
                    if path.is_file()
                    else f"Cursor curated {name} is missing"
                ),
                path=path,
            )
        )

    if command is not None:
        executable = None
        candidate = Path(command).expanduser()
        if candidate.is_absolute():
            try:
                metadata = candidate.stat()
                if stat.S_ISREG(metadata.st_mode) and os.access(candidate, os.X_OK):
                    executable = str(candidate)
            except OSError:
                pass
        else:
            executable = shutil.which(command)
        findings.append(
            _finding(
                "cursor.executable",
                "ok" if executable else "degraded",
                (
                    f"EchoVault executable resolves to {executable}"
                    if executable
                    else f"Portable EchoVault command is not on PATH: {command}"
                ),
                command=command,
            )
        )
    return findings


def cursor_diagnostics(
    service: object,
    *,
    project_root: Path,
    runner: CommandRunner | None = None,
) -> tuple[dict[str, object], ...]:
    root = project_root.expanduser().resolve()
    findings = _cursor_project_findings(root)
    config = getattr(service, "config", None)
    if config is not None:
        mode, source = resolve_context_mode(config, "cursor")
    else:
        mode, source = "auto", "default"
    findings.append(
        _finding(
            "cursor.context-policy",
            "degraded" if mode == "off" else "ok",
            f"Effective Cursor context policy is {mode} ({source})",
            mode=mode,
            source=source,
        )
    )

    global_plugin = Path.home() / ".cursor/plugins/local/echovault"
    if (root / ".cursor" / MANIFEST_NAME).is_file() and (
        global_plugin / MANIFEST_NAME
    ).is_file():
        findings.append(
            _finding(
                "cursor.shadowing",
                "shadowed",
                "Project integration takes precedence over the global Cursor plugin",
                path=global_plugin,
            )
        )

    findings.append(
        _finding(
            "cursor.cloud-boundary",
            "degraded",
            (
                "Local IDE, CLI, and Agents Window runs can use this vault; "
                "isolated Cursor Cloud agents need EchoVault and vault storage "
                "provisioned inside their execution environment"
            ),
        )
    )

    selected_runner = runner or SubprocessRunner()
    checks = (
        ("cursor.client-mcp", ("agent", "mcp", "list")),
        (
            "cursor.client-tools",
            ("agent", "mcp", "list-tools", "echovault"),
        ),
    )
    for code, argv in checks:
        try:
            result = selected_runner.run(
                argv,
                timeout=5.0,
                cwd=root,
            )
            healthy = result.returncode == 0
            detail = (result.stdout if healthy else result.stderr).strip()
        except (OSError, TimeoutError) as error:
            healthy = False
            detail = str(error)
        metadata: dict[str, object] = {}
        if code == "cursor.client-tools":
            metadata["expected_tools"] = [
                "memory_context",
                "memory_search",
                "memory_details",
                "memory_save",
            ]
        findings.append(
            _finding(
                code,
                "ok" if healthy else "degraded",
                detail or "Cursor Agent CLI capability is unavailable",
                command=" ".join(argv),
                **metadata,
            )
        )
    return tuple(findings)


def integration_diagnostics(
    service: object,
    *,
    agent: str,
    project_root: Path,
    runner: CommandRunner | None = None,
) -> tuple[dict[str, object], ...]:
    if agent == "cursor":
        return cursor_diagnostics(
            service,
            project_root=project_root,
            runner=runner,
        )
    return (
        _finding(
            f"{agent}.integration",
            "warning",
            f"No integration diagnostics are registered for {agent}",
        ),
    )
