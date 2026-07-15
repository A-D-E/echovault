from __future__ import annotations

import hashlib
import json
from importlib.metadata import PackageNotFoundError, version
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
    load_manifest,
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


def _asset_version() -> str:
    try:
        return version("echovault")
    except PackageNotFoundError:  # pragma: no cover - editable installs have metadata
        return "0.6.0"


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
        raise ValueError("Cursor user setup requires the native plugin installer")

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
        _ = options
        return IntegrationResult(
            status="unchanged",
            message="Cursor integration is not installed",
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
