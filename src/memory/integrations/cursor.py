from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any

from memory.integrations.asset_io import render_cursor_assets
from memory.integrations.config_io import (
    ConfigBoundaryError,
    ConfigMalformedError,
    mutate_json_atomic,
    read_json_strict,
    validate_target_root,
)
from memory.integrations.ownership import (
    MANIFEST_NAME,
    ManagedArtifact,
    OwnershipConflict,
    OwnershipManifest,
    artifact_digest,
    load_manifest,
    replace_managed_tree,
    verify_managed_content,
    write_manifest_atomic,
)
from memory.integrations.types import (
    AdapterCapabilities,
    DiagnosticFinding,
    InstallMode,
    InstallScope,
    IntegrationOptions,
    IntegrationResult,
)
from memory.safe_io import ProcessFileLock, prepare_atomic_text


CURSOR_ASSET_VERSION = "0.6.0"


def _asset_version() -> str:
    return CURSOR_ASSET_VERSION


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _project_manifest(
    *,
    mcp_entry: dict[str, object],
    rule: bytes,
    skill: bytes,
    asset_version: str,
) -> OwnershipManifest:
    return OwnershipManifest(
        integration_id="cursor-project",
        schema_version=1,
        asset_version=asset_version,
        managed=(
            ManagedArtifact(
                path="mcp.json",
                kind="json-entry",
                locator="/mcpServers/echovault",
                sha256=_sha256(_canonical_json(mcp_entry)),
            ),
            ManagedArtifact(
                path="rules/echovault.mdc",
                kind="file",
                sha256=_sha256(rule),
            ),
            ManagedArtifact(
                path="skills/echovault/SKILL.md",
                kind="file",
                sha256=_sha256(skill),
            ),
        ),
    )


def _write_asset(path: Path, payload: bytes) -> None:
    try:
        content = payload.decode("utf-8")
    except UnicodeError as error:  # pragma: no cover - package assets are validated
        raise OwnershipConflict("Cursor asset is not valid UTF-8") from error
    prepared = prepare_atomic_text(path, content)
    try:
        prepared.replace()
    finally:
        prepared.discard()


class CursorAdapter:
    integration_id = "cursor"
    agent = "cursor"
    capabilities = AdapterCapabilities(
        mcp=True,
        rules=True,
        skills=True,
        extensions=True,
        hooks=False,
    )

    def setup(self, options: IntegrationOptions) -> IntegrationResult:
        if options.scope is InstallScope.PROJECT:
            return self._setup_project(options)
        return self._setup_user(options)

    def _resolve_user_command(self, command: str | None) -> Path:
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

    def _user_root(self, options: IntegrationOptions) -> Path:
        if options.mode is not InstallMode.NATIVE:
            raise ValueError("Cursor user setup requires native mode")
        if options.config_root is None:
            return (Path.home() / ".cursor").resolve()
        return options.config_root.expanduser().resolve()

    def _setup_user(self, options: IntegrationOptions) -> IntegrationResult:
        cursor_root = self._user_root(options)
        command = self._resolve_user_command(options.command)
        assets = render_cursor_assets(
            command=str(command),
            version=_asset_version(),
        )
        plugin_manifest = json.loads(assets[".cursor-plugin/plugin.json"])
        plugin_mcp = json.loads(assets["mcp.json"])
        if (
            plugin_manifest.get("mcpServers") != "mcp.json"
            or plugin_manifest.get("rules") != "./rules/"
            or plugin_manifest.get("skills") != "./skills/"
            or plugin_mcp.get("mcpServers", {}).get("echovault") is None
        ):
            raise OwnershipConflict("Packaged Cursor plugin assets are invalid")

        legacy_path = cursor_root / "mcp.json"
        legacy_document = read_json_strict(legacy_path)
        legacy_servers = legacy_document.data.get("mcpServers")
        if legacy_servers is None:
            legacy_servers = {}
        if not isinstance(legacy_servers, dict):
            raise ConfigMalformedError("mcpServers must be a JSON object")
        legacy_entry = legacy_servers.get("echovault")
        recognized_direct_entries = (
            {
                "command": "memory",
                "args": ["mcp"],
                "type": "stdio",
            },
            {
                "command": "memory",
                "args": ["mcp", "--agent", "cursor"],
            },
        )
        if (
            legacy_entry is not None
            and legacy_entry not in recognized_direct_entries
        ):
            raise OwnershipConflict(
                "A custom Cursor MCP entry named echovault already exists"
            )

        plugin = cursor_root / "plugins" / "local" / "echovault"
        plugin_existed = plugin.exists()
        plugin_current = False
        if (plugin / MANIFEST_NAME).is_file():
            current_manifest = load_manifest(plugin)
            if current_manifest.integration_id != "cursor-user":
                raise OwnershipConflict(
                    "Cursor plugin manifest belongs to another integration"
                )
            plugin_current = (
                current_manifest.asset_version == _asset_version()
                and not verify_managed_content(plugin, current_manifest)
                and all(
                    (plugin / relative).is_file()
                    and (plugin / relative).read_bytes() == payload
                    for relative, payload in assets.items()
                )
            )
        if plugin_current and legacy_entry is None:
            return IntegrationResult(
                status="unchanged",
                message="Cursor local plugin is already current; no reload needed",
                paths=(plugin,),
            )

        replace_managed_tree(
            plugin,
            assets,
            force_managed=options.force_managed,
            integration_id="cursor-user",
            asset_version=_asset_version(),
        )

        if legacy_entry is not None:
            expected_legacy = legacy_entry

            def remove_legacy(data: dict[str, Any]) -> dict[str, Any]:
                servers = data.get("mcpServers")
                if not isinstance(servers, dict):
                    raise ConfigMalformedError("mcpServers must be a JSON object")
                observed = servers.get("echovault")
                if observed != expected_legacy:
                    raise OwnershipConflict(
                        "Cursor MCP entry changed during plugin installation"
                    )
                del servers["echovault"]
                if not servers:
                    del data["mcpServers"]
                return data

            mutate_json_atomic(
                legacy_path,
                remove_legacy,
                remove_if_empty=True,
            )

        status = "updated" if plugin_existed or legacy_entry is not None else "installed"
        return IntegrationResult(
            status=status,
            message=f"Cursor local plugin {status}; restart or reload Cursor",
            paths=(plugin,),
        )

    def _project_root(self, options: IntegrationOptions) -> tuple[Path, Path]:
        if options.project_root is None:
            raise ValueError("Cursor project setup requires a project root")
        project_root = options.project_root.expanduser().resolve()
        if not project_root.is_dir():
            raise ConfigBoundaryError(
                f"Cursor project root is not a directory: {options.project_root}"
            )
        target = options.config_root or project_root / ".cursor"
        cursor_root = validate_target_root(
            target,
            project_root,
            options.config_root_explicit,
        )
        return project_root, cursor_root

    def _setup_project(self, options: IntegrationOptions) -> IntegrationResult:
        if options.mode is not InstallMode.DIRECT:
            raise ValueError("Cursor project setup requires direct mode")
        _project_root, cursor_root = self._project_root(options)
        command = options.command if options.command is not None else "memory"
        rendered = render_cursor_assets(command=command, version=_asset_version())
        mcp_document = json.loads(rendered["mcp.json"])
        desired_entry = mcp_document["mcpServers"]["echovault"]
        if not isinstance(desired_entry, dict):  # pragma: no cover - packaged JSON
            raise OwnershipConflict("Packaged Cursor MCP entry is invalid")
        rule = rendered["rules/echovault.mdc"]
        skill = rendered["skills/echovault/SKILL.md"]
        desired_manifest = _project_manifest(
            mcp_entry=desired_entry,
            rule=rule,
            skill=skill,
            asset_version=_asset_version(),
        )

        cursor_root.mkdir(parents=True, exist_ok=True)
        lock = cursor_root.parent / f".{cursor_root.name}.echovault-project.lock"
        with ProcessFileLock(lock):
            return self._setup_project_locked(
                cursor_root,
                desired_entry,
                desired_manifest,
                rule,
                skill,
                options.force_managed,
                options.config_root_explicit,
            )

    def _setup_project_locked(
        self,
        cursor_root: Path,
        desired_entry: dict[str, Any],
        desired_manifest: OwnershipManifest,
        rule: bytes,
        skill: bytes,
        force_managed: bool,
        explicit_root: bool,
    ) -> IntegrationResult:
        manifest_path = cursor_root / MANIFEST_NAME
        existing_manifest: OwnershipManifest | None = None
        managed_conflicts: tuple[ManagedArtifact, ...] = ()
        if manifest_path.exists():
            existing_manifest = load_manifest(cursor_root)
            if existing_manifest.integration_id != "cursor-project":
                raise OwnershipConflict(
                    "Cursor project manifest belongs to another integration"
                )
            managed_conflicts = verify_managed_content(
                cursor_root,
                existing_manifest,
            )
            if managed_conflicts and not force_managed:
                raise OwnershipConflict("Managed Cursor project content was modified")

        mcp_path = cursor_root / "mcp.json"
        document = read_json_strict(mcp_path)
        servers = document.data.get("mcpServers")
        if servers is None:
            servers = {}
        if not isinstance(servers, dict):
            raise ConfigMalformedError("mcpServers must be a JSON object")
        current_entry = servers.get("echovault")
        legacy_entry = {
            "command": "memory",
            "args": ["mcp"],
            "type": "stdio",
        }
        entry_is_owned = existing_manifest is not None and any(
            artifact.kind == "json-entry"
            and artifact.path == "mcp.json"
            and artifact.locator == "/mcpServers/echovault"
            for artifact in existing_manifest.managed
        )
        if (
            current_entry is not None
            and current_entry != desired_entry
            and current_entry != legacy_entry
            and not entry_is_owned
        ):
            raise OwnershipConflict(
                "A custom Cursor MCP entry named echovault already exists"
            )

        desired_files = {
            cursor_root / "rules/echovault.mdc": rule,
            cursor_root / "skills/echovault/SKILL.md": skill,
        }
        if existing_manifest is None:
            for path, payload in desired_files.items():
                if path.exists() and path.read_bytes() != payload:
                    raise OwnershipConflict(
                        f"A custom Cursor integration asset already exists: {path}"
                    )

        all_files_current = all(
            path.is_file() and path.read_bytes() == payload
            for path, payload in desired_files.items()
        )
        manifest_current = (
            existing_manifest == desired_manifest and not managed_conflicts
        )
        if (
            current_entry == desired_entry
            and all_files_current
            and manifest_current
        ):
            warnings = (
                ("Using explicitly selected Cursor configuration root",)
                if explicit_root
                else ()
            )
            return IntegrationResult(
                status="unchanged",
                message="Cursor project integration is already current",
                paths=(cursor_root,),
                warnings=warnings,
            )

        existed = existing_manifest is not None or current_entry is not None

        def install_entry(data: dict[str, Any]) -> dict[str, Any]:
            target_servers = data.setdefault("mcpServers", {})
            if not isinstance(target_servers, dict):
                raise ConfigMalformedError("mcpServers must be a JSON object")
            observed = target_servers.get("echovault")
            if (
                observed is not None
                and observed != current_entry
                and observed != desired_entry
            ):
                raise OwnershipConflict(
                    "Cursor MCP entry changed during integration setup"
                )
            target_servers["echovault"] = desired_entry
            return data

        mutate_json_atomic(mcp_path, install_entry)
        for path, payload in desired_files.items():
            _write_asset(path, payload)
        write_manifest_atomic(cursor_root, desired_manifest)
        if verify_managed_content(cursor_root, desired_manifest):
            raise OwnershipConflict("Installed Cursor project content failed verification")

        status = "updated" if existed else "installed"
        warnings = (
            ("Using explicitly selected Cursor configuration root",)
            if explicit_root
            else ()
        )
        return IntegrationResult(
            status=status,
            message=f"Cursor project integration {status}",
            paths=(
                mcp_path,
                *desired_files,
                manifest_path,
            ),
            warnings=warnings,
        )

    def uninstall(self, options: IntegrationOptions) -> IntegrationResult:
        if options.scope is InstallScope.PROJECT:
            return self._uninstall_project(options)
        return self._uninstall_user(options)

    @staticmethod
    def _remove_empty_parents(path: Path, *, stop: Path) -> None:
        parent = path.parent
        while parent != stop and parent.is_dir():
            try:
                parent.rmdir()
            except OSError:
                return
            parent = parent.parent

    @staticmethod
    def _recognized_project_entry(entry: object) -> bool:
        return entry in (
            {
                "command": "memory",
                "args": ["mcp"],
                "type": "stdio",
            },
            {
                "command": "memory",
                "args": ["mcp", "--agent", "cursor"],
            },
        )

    def _remove_mcp_entry(
        self,
        path: Path,
        *,
        expected: object,
    ) -> None:
        def remove(data: dict[str, Any]) -> dict[str, Any]:
            servers = data.get("mcpServers")
            if not isinstance(servers, dict):
                raise ConfigMalformedError("mcpServers must be a JSON object")
            if servers.get("echovault") != expected:
                raise OwnershipConflict(
                    "Cursor MCP entry changed during uninstall"
                )
            del servers["echovault"]
            if not servers:
                del data["mcpServers"]
            return data

        mutate_json_atomic(path, remove)

    def _uninstall_project(self, options: IntegrationOptions) -> IntegrationResult:
        _project_root, cursor_root = self._project_root(options)
        mcp_path = cursor_root / "mcp.json"
        document = read_json_strict(mcp_path)
        servers = document.data.get("mcpServers")
        if servers is None:
            servers = {}
        if not isinstance(servers, dict):
            raise ConfigMalformedError("mcpServers must be a JSON object")
        current_entry = servers.get("echovault")
        manifest_path = cursor_root / MANIFEST_NAME

        if not manifest_path.exists():
            if current_entry is None or not self._recognized_project_entry(
                current_entry
            ):
                return IntegrationResult(
                    status="unchanged",
                    message="Cursor project integration is not installed",
                )
            self._remove_mcp_entry(mcp_path, expected=current_entry)
            return IntegrationResult(
                status="removed",
                message="Removed legacy Cursor project integration",
                paths=(mcp_path,),
            )

        manifest = load_manifest(cursor_root)
        if manifest.integration_id != "cursor-project":
            raise OwnershipConflict(
                "Cursor project manifest belongs to another integration"
            )
        json_artifact = next(
            (
                artifact
                for artifact in manifest.managed
                if artifact.kind == "json-entry"
                and artifact.path == "mcp.json"
                and artifact.locator == "/mcpServers/echovault"
            ),
            None,
        )
        if json_artifact is None:
            raise OwnershipConflict("Cursor project manifest lacks its MCP claim")
        if current_entry is not None and artifact_digest(
            cursor_root,
            json_artifact,
        ) != json_artifact.sha256:
            raise OwnershipConflict(
                "Custom Cursor MCP content cannot be removed, even with force"
            )
        conflicts = verify_managed_content(cursor_root, manifest)
        if conflicts and not options.force_managed:
            raise OwnershipConflict("Managed Cursor project content was modified")

        if current_entry is not None:
            self._remove_mcp_entry(mcp_path, expected=current_entry)
        removed_paths: list[Path] = []
        for artifact in manifest.managed:
            if artifact.kind != "file":
                continue
            path = cursor_root.joinpath(*artifact.path.split("/"))
            path.unlink(missing_ok=True)
            removed_paths.append(path)
            self._remove_empty_parents(path, stop=cursor_root)
        manifest_path.unlink()
        removed_paths.append(manifest_path)
        return IntegrationResult(
            status="removed",
            message="Removed Cursor project integration",
            paths=tuple(removed_paths),
        )

    def _uninstall_user(self, options: IntegrationOptions) -> IntegrationResult:
        cursor_root = self._user_root(options)
        direct_path = cursor_root / "mcp.json"
        document = read_json_strict(direct_path)
        servers = document.data.get("mcpServers")
        if servers is None:
            servers = {}
        if not isinstance(servers, dict):
            raise ConfigMalformedError("mcpServers must be a JSON object")
        direct_entry = servers.get("echovault")
        removable_direct = (
            direct_entry is not None
            and self._recognized_project_entry(direct_entry)
        )

        plugin = cursor_root / "plugins" / "local" / "echovault"
        manifest_path = plugin / MANIFEST_NAME
        if not manifest_path.exists():
            if removable_direct:
                self._remove_mcp_entry(direct_path, expected=direct_entry)
                return IntegrationResult(
                    status="removed",
                    message="Removed legacy Cursor user integration",
                    paths=(direct_path,),
                )
            return IntegrationResult(
                status="unchanged",
                message="Cursor local plugin is not installed",
            )

        manifest = load_manifest(plugin)
        if manifest.integration_id != "cursor-user":
            raise OwnershipConflict(
                "Cursor plugin manifest belongs to another integration"
            )
        conflicts = verify_managed_content(plugin, manifest)
        if conflicts and not options.force_managed:
            raise OwnershipConflict("Managed Cursor plugin content was modified")

        removed_paths: list[Path] = []
        for artifact in manifest.managed:
            if artifact.kind != "file":
                raise OwnershipConflict(
                    "Cursor plugin manifest contains an unsupported claim"
                )
            path = plugin.joinpath(*artifact.path.split("/"))
            path.unlink(missing_ok=True)
            removed_paths.append(path)
            self._remove_empty_parents(path, stop=plugin)
        manifest_path.unlink()
        removed_paths.append(manifest_path)
        try:
            plugin.rmdir()
        except OSError:
            pass
        if removable_direct:
            self._remove_mcp_entry(direct_path, expected=direct_entry)
            removed_paths.append(direct_path)
        return IntegrationResult(
            status="removed",
            message="Removed Cursor local plugin; restart or reload Cursor",
            paths=tuple(removed_paths),
        )

    def diagnose(
        self,
        options: IntegrationOptions,
    ) -> tuple[DiagnosticFinding, ...]:
        if options.scope is not InstallScope.PROJECT:
            return ()
        _project_root, cursor_root = self._project_root(options)
        return (
            DiagnosticFinding(
                code="cursor.scope",
                status="ok" if cursor_root.exists() else "warning",
                message=f"Cursor project configuration: {cursor_root}",
                path=cursor_root,
            ),
        )
