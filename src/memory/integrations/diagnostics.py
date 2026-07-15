from __future__ import annotations

import os
import re
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
    client_command = "agent"
    if runner is None and shutil.which("agent") is None:
        client_command = (
            "cursor-agent" if shutil.which("cursor-agent") else "agent"
        )
    checks = (
        ("cursor.client-mcp", (client_command, "mcp", "list")),
        (
            "cursor.client-tools",
            (client_command, "mcp", "list-tools", "echovault"),
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


def _gemini_project_artifacts(
    project_root: Path,
) -> tuple[list[dict[str, object]], bool]:
    root = project_root / ".gemini"
    findings: list[dict[str, object]] = []
    manifest_path = root / MANIFEST_NAME
    manifest_healthy = False
    if manifest_path.is_file():
        try:
            manifest = load_manifest(root)
            conflicts = verify_managed_content(root, manifest)
            manifest_healthy = (
                manifest.integration_id == "gemini-project-direct"
                and not conflicts
            )
            message = (
                "Gemini project ownership is verified"
                if manifest_healthy
                else "Gemini project ownership has modified claims"
            )
        except OwnershipConflict as error:
            message = str(error)
    else:
        message = "Gemini project ownership manifest is missing"
    findings.append(
        _finding(
            "gemini.manifest",
            "ok" if manifest_healthy else "unhealthy",
            message,
            path=manifest_path,
        )
    )

    settings_path = root / "settings.json"
    mcp_valid = False
    hook_valid = False
    command: str | None = None
    try:
        data = read_json_strict(settings_path).data
        servers = data.get("mcpServers", {})
        entry = servers.get("echovault") if isinstance(servers, dict) else None
        mcp_valid = (
            isinstance(entry, dict)
            and isinstance(entry.get("command"), str)
            and entry.get("args") == ["mcp", "--agent", "gemini-cli"]
        )
        if mcp_valid:
            command = entry["command"]
        hooks = data.get("hooks", {})
        groups = hooks.get("BeforeAgent", []) if isinstance(hooks, dict) else []
        matches = []
        if isinstance(groups, list):
            for group in groups:
                nested = group.get("hooks", []) if isinstance(group, dict) else []
                if isinstance(nested, list):
                    matches.extend(
                        item
                        for item in nested
                        if isinstance(item, dict)
                        and item.get("name") == "echovault-context"
                    )
        hook_valid = len(matches) == 1
    except ConfigMalformedError:
        pass
    findings.append(
        _finding(
            "gemini.mcp",
            "ok" if mcp_valid else "unhealthy",
            (
                "Gemini uses the bound EchoVault MCP contract"
                if mcp_valid
                else "Gemini bound EchoVault MCP entry is missing or invalid"
            ),
            path=settings_path,
            command=command,
        )
    )

    context_path = project_root / "GEMINI.md"
    context_valid = False
    try:
        content = context_path.read_text(encoding="utf-8")
        context_valid = (
            content.count("<!-- echovault:start -->") == 1
            and content.count("<!-- echovault:end -->") == 1
        )
    except (OSError, UnicodeError):
        pass
    findings.append(
        _finding(
            "gemini.context",
            "ok" if context_valid else "unhealthy",
            (
                "Gemini curated static context is installed"
                if context_valid
                else "Gemini curated static context is missing or malformed"
            ),
            path=context_path,
        )
    )

    skill_path = root / "skills/echovault/SKILL.md"
    findings.append(
        _finding(
            "gemini.skill",
            "ok" if skill_path.is_file() else "unhealthy",
            (
                "Gemini curated EchoVault skill is installed"
                if skill_path.is_file()
                else "Gemini curated EchoVault skill is missing"
            ),
            path=skill_path,
        )
    )
    findings.append(
        _finding(
            "gemini.hook",
            "ok" if hook_valid else "degraded",
            (
                "Gemini BeforeAgent context hook is installed"
                if hook_valid
                else "Gemini BeforeAgent context hook is unavailable"
            ),
            path=settings_path,
        )
    )
    findings.append(
        _finding(
            "gemini.tools",
            "ok" if mcp_valid else "unhealthy",
            "Gemini MCP contract exposes the four bound memory tools",
            expected_tools=[
                "memory_context",
                "memory_search",
                "memory_details",
                "memory_save",
            ],
        )
    )
    return findings, hook_valid


def gemini_diagnostics(
    service: object,
    *,
    project_root: Path,
    runner: CommandRunner | None = None,
) -> tuple[dict[str, object], ...]:
    from memory.integrations.gemini import GeminiAdapter
    from memory.integrations.gemini_state import ArtifactState
    from memory.projects import (
        ProjectRegistry,
        ProjectResolutionError,
        build_project_identity,
        discover_project_root,
    )

    root = project_root.expanduser().resolve()
    selected_runner = runner or SubprocessRunner()
    adapter = GeminiAdapter(runner=selected_runner)
    state = adapter.detect(project_root=root)
    findings: list[dict[str, object]] = [
        _finding(
            "gemini.project",
            "ok",
            f"Gemini project context resolves to {root}",
            path=root,
            project_name=root.name,
        )
    ]

    version: str | None = None
    try:
        result = selected_runner.run(
            ["gemini", "--version"],
            timeout=5.0,
            cwd=root,
        )
        match = re.search(r"(\d+\.\d+\.\d+)", result.stdout)
        if result.returncode == 0 and match:
            version = match.group(1)
    except (OSError, TimeoutError):
        pass
    hook_supported = bool(
        version
        and tuple(map(int, version.split("."))) >= (0, 50, 0)
    )
    findings.append(
        _finding(
            "gemini.version",
            "ok" if version else "degraded",
            (
                f"Gemini CLI version {version} supports managed hooks"
                if version and hook_supported
                else (
                    f"Gemini CLI version {version} lacks managed hook support"
                    if version
                    else "Gemini CLI version could not be determined"
                )
            ),
            command="gemini --version",
            version=version,
            hook_supported=hook_supported,
        )
    )

    values = {
        "native": state.native.value,
        "user_direct": state.user_direct.value,
        "project_direct": state.project_direct.value,
    }
    artifacts = (state.native, state.user_direct, state.project_direct)
    if any(
        item in {ArtifactState.CUSTOM, ArtifactState.MALFORMED}
        for item in artifacts
    ):
        state_status: DiagnosticStatus = "unhealthy"
    elif any(item is ArtifactState.MODIFIED for item in artifacts):
        state_status = "unhealthy"
    elif any(item is ArtifactState.OWNED_OUTDATED for item in artifacts):
        state_status = "degraded"
    else:
        state_status = "ok"
    findings.append(
        _finding(
            "gemini.state",
            state_status,
            "Gemini native/user/project installation state was detected",
            **values,
        )
    )
    findings.append(
        _finding(
            "gemini.native-enabled",
            "ok",
            (
                "Gemini native EchoVault extension is enabled"
                if state.native_enabled
                else "Gemini native EchoVault extension is disabled"
            ),
            enabled=state.native_enabled,
        )
    )

    artifact_findings, hook_installed = _gemini_project_artifacts(root)
    findings.extend(artifact_findings)
    if hook_installed and not hook_supported:
        for index, finding in enumerate(findings):
            if finding["code"] == "gemini.hook":
                findings[index] = {
                    **finding,
                    "status": "degraded",
                    "message": "Installed BeforeAgent hook is unsupported by this Gemini CLI",
                }

    global_present = state.native in {
        ArtifactState.OWNED,
        ArtifactState.OWNED_OUTDATED,
        ArtifactState.MODIFIED,
    } or state.user_direct in {
        ArtifactState.OWNED,
        ArtifactState.OWNED_OUTDATED,
        ArtifactState.MODIFIED,
    }
    findings.append(
        _finding(
            "gemini.mcp.precedence",
            "shadowed" if global_present else "ok",
            (
                "Project Gemini MCP takes precedence over the global integration"
                if global_present
                else "Project Gemini MCP is the selected EchoVault scope"
            ),
            path=root / ".gemini/settings.json",
        )
    )

    project_key = ""
    aliases: tuple[str, ...] = ()
    try:
        discovered, marker = discover_project_root(root)
        identity = build_project_identity(discovered, marker)
        project_key = identity.key
        memory_home = Path(str(getattr(service, "memory_home")))
        resolved = ProjectRegistry(memory_home).resolve(identity.key)
        if resolved is not None:
            aliases = resolved.aliases
    except (OSError, ProjectResolutionError, TypeError, ValueError):
        pass
    findings.append(
        _finding(
            "gemini.project-scope",
            "ok" if project_key else "degraded",
            "Gemini project storage scope is resolved",
            project_key=project_key,
            aliases=list(aliases),
        )
    )

    memory_home = Path(str(getattr(service, "memory_home", "")))
    claim_root = memory_home / "hook-events"
    claims_healthy = not os.path.lexists(claim_root) or (
        claim_root.is_dir() and not claim_root.is_symlink()
    )
    findings.append(
        _finding(
            "gemini.claims",
            "ok" if claims_healthy else "unhealthy",
            (
                "Gemini hook claim storage is healthy or will be created lazily"
                if claims_healthy
                else "Gemini hook claim storage is unsafe"
            ),
            path=claim_root,
        )
    )

    config = getattr(service, "config", None)
    if config is not None:
        mode, source = resolve_context_mode(config, "gemini-cli")
    else:
        mode, source = "auto", "default"
    findings.append(
        _finding(
            "gemini.context-policy",
            "degraded" if mode == "off" else "ok",
            f"Effective Gemini context policy is {mode} ({source})",
            mode=mode,
            source=source,
        )
    )
    embedding = getattr(config, "embedding", None)
    context = getattr(config, "context", None)
    remote = getattr(embedding, "provider", None) == "openai"
    allowed = bool(
        getattr(context, "allow_remote_query_embeddings", False)
    )
    query_mode = (
        "local" if not remote else ("remote_redacted" if allowed else "fts_only")
    )
    findings.append(
        _finding(
            "gemini.query-privacy",
            "ok" if query_mode != "fts_only" else "degraded",
            f"Automatic Gemini query mode is {query_mode}",
            provider_scope="remote" if remote else "local",
            allow_remote_query_embeddings=allowed,
            automatic_query_embedding=query_mode,
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
    if agent in {"gemini", "gemini-cli"}:
        return gemini_diagnostics(
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
