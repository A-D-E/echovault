from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

from memory.integrations.asset_io import (
    render_gemini_assets,
    shell_join_command,
)
from memory.integrations.config_io import (
    ConfigBoundaryError,
    ConfigMalformedError,
    mutate_json_atomic,
    mutate_marked_block,
    read_json_strict,
    validate_target_root,
)
from memory.integrations.ownership import (
    MANIFEST_NAME,
    ManagedArtifact,
    OwnershipConflict,
    OwnershipManifest,
    load_manifest,
    replace_managed_tree,
    verify_managed_content,
    write_manifest_atomic,
)
from memory.integrations.process import CommandRunner, SubprocessRunner
from memory.integrations.types import (
    AdapterCapabilities,
    DiagnosticFinding,
    InstallMode,
    InstallScope,
    IntegrationOptions,
    IntegrationResult,
)
from memory.safe_io import prepare_atomic_text


GEMINI_ASSET_VERSION = "0.6.0"
_HOOK_NAME = "echovault-context"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _write_asset(path: Path, payload: bytes) -> None:
    content = payload.decode("utf-8")
    prepared = prepare_atomic_text(path, content)
    try:
        prepared.replace()
    finally:
        prepared.discard()


def _find_named_hook(
    data: dict[str, Any],
) -> tuple[int, dict[str, Any], dict[str, Any]] | None:
    hooks = data.get("hooks")
    if hooks is None:
        return None
    if not isinstance(hooks, dict):
        raise ConfigMalformedError("hooks must be a JSON object")
    groups = hooks.get("BeforeAgent")
    if groups is None:
        return None
    if not isinstance(groups, list):
        raise ConfigMalformedError("hooks.BeforeAgent must be a JSON array")
    found: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for index, group in enumerate(groups):
        if not isinstance(group, dict):
            raise ConfigMalformedError("BeforeAgent hook group must be an object")
        nested = group.get("hooks")
        if not isinstance(nested, list):
            raise ConfigMalformedError("BeforeAgent hooks must be an array")
        for hook in nested:
            if not isinstance(hook, dict):
                raise ConfigMalformedError("BeforeAgent hook must be an object")
            if hook.get("name") == _HOOK_NAME:
                found.append((index, group, hook))
    if len(found) > 1:
        raise OwnershipConflict("Duplicate echovault-context hooks exist")
    return found[0] if found else None


def _manifest_for_direct(
    *,
    mcp_entry: dict[str, Any],
    hook_group: dict[str, Any] | None,
    hook_index: int | None,
    skill: bytes,
    scope: InstallScope,
) -> OwnershipManifest:
    artifacts = [
        ManagedArtifact(
            path="settings.json",
            kind="json-entry",
            locator="/mcpServers/echovault",
            sha256=_sha256(_canonical_json(mcp_entry)),
        ),
        ManagedArtifact(
            path="skills/echovault/SKILL.md",
            kind="file",
            sha256=_sha256(skill),
        ),
    ]
    if hook_group is not None and hook_index is not None:
        artifacts.append(
            ManagedArtifact(
                path="settings.json",
                kind="json-entry",
                locator=f"/hooks/BeforeAgent/{hook_index}",
                sha256=_sha256(_canonical_json(hook_group)),
            )
        )
    return OwnershipManifest(
        integration_id=f"gemini-{scope.value}-direct",
        schema_version=1,
        asset_version=GEMINI_ASSET_VERSION,
        managed=tuple(artifacts),
    )


class GeminiAdapter:
    integration_id = "gemini"
    agent = "gemini-cli"
    capabilities = AdapterCapabilities(
        mcp=True,
        rules=False,
        skills=True,
        extensions=True,
        hooks=True,
    )

    def __init__(self, runner: CommandRunner | None = None) -> None:
        self.runner = runner or SubprocessRunner()

    def _supports_hooks(self, cwd: Path) -> bool:
        try:
            result = self.runner.run(
                ["gemini", "--version"],
                timeout=5.0,
                cwd=cwd.resolve(),
            )
        except (OSError, TimeoutError):
            return False
        if result.returncode != 0:
            return False
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", result.stdout)
        return bool(match and tuple(map(int, match.groups())) >= (0, 50, 0))

    @staticmethod
    def _resolve_executable(command: str | None) -> Path:
        selected = command or "memory"
        candidate = Path(selected).expanduser()
        if not candidate.is_absolute():
            discovered = shutil.which(selected)
            if discovered is None:
                raise ValueError(f"EchoVault executable was not found: {selected}")
            candidate = Path(discovered)
        resolved = candidate.resolve()
        try:
            metadata = resolved.stat()
        except OSError as error:
            raise ValueError(
                f"EchoVault executable cannot be inspected: {selected}"
            ) from error
        if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
            raise ValueError(
                f"EchoVault command must be an executable regular file: {selected}"
            )
        return resolved

    def _direct_paths(
        self,
        options: IntegrationOptions,
    ) -> tuple[Path, Path, str]:
        if options.scope is InstallScope.PROJECT:
            if options.project_root is None:
                raise ValueError("Gemini project setup requires a project root")
            project = options.project_root.expanduser().resolve()
            if not project.is_dir():
                raise ConfigBoundaryError("Gemini project root is not a directory")
            target = options.config_root or project / ".gemini"
            config_root = validate_target_root(
                target,
                project,
                options.config_root_explicit,
            )
            return config_root, project / "GEMINI.md", options.command or "memory"
        target = options.config_root or Path.home() / ".gemini"
        config_root = validate_target_root(
            target,
            Path.home(),
            options.config_root_explicit,
        )
        command = str(self._resolve_executable(options.command))
        return config_root, config_root / "GEMINI.md", command

    def setup(self, options: IntegrationOptions) -> IntegrationResult:
        if options.mode is InstallMode.NATIVE:
            return self._setup_native(options)
        return self._setup_direct(options)

    @staticmethod
    def _platform_config(home: Path) -> Path:
        if sys.platform == "darwin":
            return home / "Library" / "Application Support" / "echovault"
        if os.name == "nt":
            return home / "AppData" / "Roaming" / "echovault"
        return home / ".config" / "echovault"

    def _native_paths(self, options: IntegrationOptions) -> tuple[Path, Path]:
        if options.scope is not InstallScope.USER:
            raise ValueError("Gemini native extension is user-scoped")
        gemini_root = (options.config_root or Path.home() / ".gemini").expanduser()
        home = gemini_root.parent.resolve()
        platform_root = self._platform_config(home)
        source_candidate = (
            platform_root / "integrations/gemini-extension/echovault"
        )
        source = validate_target_root(
            source_candidate,
            platform_root,
            explicit=False,
        )
        return source, gemini_root.resolve()

    def _manager(
        self,
        argv: list[str],
        *,
        cwd: Path,
        capture_output: bool = True,
    ):
        result = self.runner.run(
            argv,
            timeout=30.0,
            capture_output=capture_output,
            cwd=cwd.resolve(),
            env=dict(os.environ),
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(
                f"Gemini extension manager failed: {detail or result.returncode}"
            )
        return result

    @staticmethod
    def _installed_version(output: str) -> str | None:
        for line in output.splitlines():
            match = re.match(r"^\s*echovault\s+(\S+)", line)
            if match:
                return match.group(1)
        return None

    def _setup_native(self, options: IntegrationOptions) -> IntegrationResult:
        source, _gemini_root = self._native_paths(options)
        command = str(self._resolve_executable(options.command))
        assets = render_gemini_assets(
            memory_command=command,
            hook_command=shell_join_command(
                [command, "hook", "gemini", "before-agent"]
            ),
            version=GEMINI_ASSET_VERSION,
        )
        source_current = False
        manifest_path = source / MANIFEST_NAME
        if manifest_path.is_file():
            manifest = load_manifest(source)
            source_current = (
                manifest.integration_id == "gemini-native"
                and manifest.asset_version == GEMINI_ASSET_VERSION
                and not verify_managed_content(source, manifest)
                and all(
                    (source / relative).is_file()
                    and (source / relative).read_bytes() == payload
                    for relative, payload in assets.items()
                )
            )

        replace_managed_tree(
            source,
            assets,
            force_managed=options.force_managed,
            integration_id="gemini-native",
            asset_version=GEMINI_ASSET_VERSION,
        )
        manager_cwd = source.parent
        self._manager(
            ["gemini", "extensions", "validate", str(source)],
            cwd=manager_cwd,
        )
        listed = self._manager(
            ["gemini", "extensions", "list"],
            cwd=manager_cwd,
        )
        installed_version = self._installed_version(listed.stdout)
        if installed_version is None:
            self._manager(
                ["gemini", "extensions", "install", str(source)],
                cwd=manager_cwd,
                capture_output=False,
            )
            status = "installed"
        elif installed_version != GEMINI_ASSET_VERSION or not source_current:
            self._manager(
                ["gemini", "extensions", "update", "echovault"],
                cwd=manager_cwd,
            )
            status = "updated"
        else:
            status = "unchanged"
        verified = self._manager(
            ["gemini", "extensions", "list"],
            cwd=manager_cwd,
        )
        if self._installed_version(verified.stdout) != GEMINI_ASSET_VERSION:
            raise RuntimeError("Gemini extension manager did not activate EchoVault")
        return IntegrationResult(
            status=status,
            message=f"Gemini native extension {status}",
            paths=(source,),
        )

    def _setup_direct(self, options: IntegrationOptions) -> IntegrationResult:
        config_root, context_path, command = self._direct_paths(options)
        hook_supported = self._supports_hooks(
            options.project_root or config_root.parent
        )
        hook_command = shell_join_command(
            [command, "hook", "gemini", "before-agent"]
        )
        assets = render_gemini_assets(
            memory_command=command,
            hook_command=hook_command,
            version=GEMINI_ASSET_VERSION,
        )
        extension = json.loads(assets["gemini-extension.json"])
        desired_mcp = extension["mcpServers"]["echovault"]
        hook_document = json.loads(assets["hooks/hooks.json"])
        desired_hook_group = hook_document["hooks"]["BeforeAgent"][0]
        skill = assets["skills/echovault/SKILL.md"]
        context = assets["GEMINI.md"].decode("utf-8")

        settings_path = config_root / "settings.json"
        document = read_json_strict(settings_path)
        servers = document.data.get("mcpServers")
        if servers is None:
            servers = {}
        if not isinstance(servers, dict):
            raise ConfigMalformedError("mcpServers must be a JSON object")
        current_mcp = servers.get("echovault")
        current_hook = _find_named_hook(document.data)
        manifest_path = config_root / MANIFEST_NAME
        existing_manifest: OwnershipManifest | None = None
        conflicts: tuple[ManagedArtifact, ...] = ()
        if manifest_path.is_file():
            existing_manifest = load_manifest(config_root)
            expected_id = f"gemini-{options.scope.value}-direct"
            if existing_manifest.integration_id != expected_id:
                raise OwnershipConflict(
                    "Gemini direct manifest belongs to another integration"
                )
            conflicts = verify_managed_content(config_root, existing_manifest)
            if conflicts and not options.force_managed:
                raise OwnershipConflict("Managed Gemini direct content was modified")
        mcp_owned = existing_manifest is not None and any(
            item.locator == "/mcpServers/echovault"
            for item in existing_manifest.managed
        )
        if current_mcp is not None and current_mcp != desired_mcp and not mcp_owned:
            raise OwnershipConflict("A custom Gemini MCP named echovault exists")
        if current_hook is not None:
            _index, group, _hook = current_hook
            hook_owned = existing_manifest is not None and any(
                item.locator == f"/hooks/BeforeAgent/{_index}"
                for item in existing_manifest.managed
            )
            if group != desired_hook_group and not hook_owned:
                raise OwnershipConflict(
                    "A custom Gemini hook named echovault-context exists"
                )

        if hook_supported:
            hook_index = current_hook[0] if current_hook is not None else self._hook_count(document.data)
            manifest_hook = desired_hook_group
        else:
            hook_index = None
            manifest_hook = None
        desired_manifest = _manifest_for_direct(
            mcp_entry=desired_mcp,
            hook_group=manifest_hook,
            hook_index=hook_index,
            skill=skill,
            scope=options.scope,
        )
        desired_block = f"<!-- echovault:start -->\n{context.rstrip()}\n<!-- echovault:end -->"
        context_current = (
            context_path.is_file()
            and context_path.read_text(encoding="utf-8").count(desired_block) == 1
        )
        skill_path = config_root / "skills/echovault/SKILL.md"
        hook_current = (
            not hook_supported
            or (
                current_hook is not None
                and current_hook[1] == desired_hook_group
            )
        )
        if (
            existing_manifest == desired_manifest
            and not conflicts
            and current_mcp == desired_mcp
            and hook_current
            and skill_path.is_file()
            and skill_path.read_bytes() == skill
            and context_current
        ):
            return IntegrationResult(
                status="unchanged",
                message="Gemini direct integration is already current",
                paths=(config_root, context_path),
            )

        existed = existing_manifest is not None or current_mcp is not None

        def install_settings(data: dict[str, Any]) -> dict[str, Any]:
            target_servers = data.setdefault("mcpServers", {})
            if not isinstance(target_servers, dict):
                raise ConfigMalformedError("mcpServers must be a JSON object")
            target_servers["echovault"] = desired_mcp
            if hook_supported:
                hooks = data.setdefault("hooks", {})
                if not isinstance(hooks, dict):
                    raise ConfigMalformedError("hooks must be a JSON object")
                groups = hooks.setdefault("BeforeAgent", [])
                if not isinstance(groups, list):
                    raise ConfigMalformedError(
                        "hooks.BeforeAgent must be a JSON array"
                    )
                observed = _find_named_hook(data)
                if observed is None:
                    groups.append(desired_hook_group)
                else:
                    groups[observed[0]] = desired_hook_group
            return data

        mutate_json_atomic(settings_path, install_settings)
        _write_asset(skill_path, skill)
        mutate_marked_block(
            context_path,
            marker="echovault",
            block=context,
        )
        write_manifest_atomic(config_root, desired_manifest)
        if verify_managed_content(config_root, desired_manifest):
            raise OwnershipConflict("Installed Gemini direct content failed verification")
        status = "updated" if existed else "installed"
        warnings = (
            ()
            if hook_supported
            else ("Gemini CLI lacks BeforeAgent hook support; MCP/static only",)
        )
        return IntegrationResult(
            status=status,
            message=f"Gemini direct integration {status}",
            paths=(settings_path, skill_path, context_path, manifest_path),
            warnings=warnings,
        )

    @staticmethod
    def _hook_count(data: dict[str, Any]) -> int:
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            return 0
        groups = hooks.get("BeforeAgent")
        return len(groups) if isinstance(groups, list) else 0

    def uninstall(self, options: IntegrationOptions) -> IntegrationResult:
        _ = options
        return IntegrationResult(
            status="unchanged",
            message="Gemini integration is not installed",
        )

    def diagnose(
        self,
        options: IntegrationOptions,
    ) -> tuple[DiagnosticFinding, ...]:
        if options.mode is InstallMode.NATIVE:
            return ()
        config_root, _context, _command = self._direct_paths(options)
        return (
            DiagnosticFinding(
                code="gemini.scope",
                status="ok" if config_root.is_dir() else "warning",
                message=f"Gemini direct configuration: {config_root}",
                path=config_root,
            ),
        )
