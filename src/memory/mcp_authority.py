from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence
from urllib.parse import unquote, urlparse

from memory.projects import (
    MultiRootError,
    ProjectCandidates,
    ProjectRegistry,
    ProjectResolutionError,
    ProjectScope,
    _is_within,
    build_project_identity,
    discover_project_root,
    select_project_candidate,
)


BOUNDARY_ERROR = "Request path is outside the authorized project boundary"
ROOTS_ERROR = "MCP roots do not resolve to one authorized project"
PROJECT_ERROR = "Requested project does not match the authorized project"
AUTHORITY_ERROR = "Request conflicts with the bound MCP authority"

AuthorityKind = Literal["boundary", "project", "authority"]


class AuthorityConflict(ProjectResolutionError):
    def __init__(self, kind: AuthorityKind, internal_message: str) -> None:
        self.kind = kind
        super().__init__(internal_message)


def public_project_error(error: ProjectResolutionError) -> str:
    """Map an internal resolution failure to a fixed path-free message."""
    if isinstance(error, AuthorityConflict):
        if error.kind == "boundary":
            return BOUNDARY_ERROR
        if error.kind == "project":
            return PROJECT_ERROR
        return AUTHORITY_ERROR
    return ROOTS_ERROR


@dataclass(frozen=True)
class MCPServerBinding:
    agent: str | None
    project_root: Path | None
    startup_cwd: Path

    @property
    def is_bound(self) -> bool:
        return self.agent is not None or self.project_root is not None


def resolve_bound_identity(
    bound_agent: str | None,
    requested: str | None,
    *,
    field_name: Literal["agent", "source"],
) -> str | None:
    if bound_agent is None:
        return requested
    if requested is not None and requested != bound_agent:
        raise AuthorityConflict(
            "authority",
            f"Conflicting {field_name}: server is bound to {bound_agent}",
        )
    return bound_agent


def file_uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
        raise ProjectResolutionError(
            "MCP root URI must be a local file URI"
        )
    path = unquote(parsed.path)
    if (
        os.name == "nt"
        and path.startswith("/")
        and len(path) > 2
        and path[2] == ":"
    ):
        path = path[1:]
    return Path(path).resolve()


def ensure_path_within(path: Path, boundary: Path) -> Path:
    resolved = path.resolve()
    root = boundary.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise AuthorityConflict(
            "boundary",
            "Resolved path escapes authority boundary",
        ) from error
    return resolved


def _register_candidate(
    registry: ProjectRegistry,
    candidate: Path,
) -> ProjectScope:
    root, marker = discover_project_root(candidate)
    return registry.register(build_project_identity(root, marker))


def _validate_requested_project(
    scope: ProjectScope,
    requested_project: str | None,
) -> None:
    if (
        requested_project is not None
        and requested_project not in scope.storage_keys
    ):
        raise AuthorityConflict(
            "project",
            "Requested project is outside the resolved scope",
        )


def resolve_project_scope(
    binding: MCPServerBinding,
    registry: ProjectRegistry,
    *,
    client_roots: Sequence[Path],
    cwd: Path | None,
    requested_project: str | None,
) -> ProjectScope | str | None:
    """Resolve one project authority while preserving legacy unbound calls."""
    if not binding.is_bound:
        return requested_project

    roots = tuple(root.resolve() for root in client_roots)

    if binding.project_root is not None:
        explicit_candidate = select_project_candidate(
            ProjectCandidates(explicit_root=binding.project_root)
        )
        authorized_root, _marker = discover_project_root(explicit_candidate)
        for root in roots:
            ensure_path_within(root, authorized_root)
        if cwd is not None:
            ensure_path_within(cwd, authorized_root)
        scope = _register_candidate(registry, explicit_candidate)
        _validate_requested_project(scope, requested_project)
        return scope

    if roots:
        candidate = select_project_candidate(
            ProjectCandidates(mcp_roots=roots, protocol_cwd=cwd)
        )
        if len(roots) == 1:
            selected_boundary = roots[0]
        else:
            assert cwd is not None
            containing = tuple(
                root for root in roots if _is_within(cwd, root)
            )
            if len(containing) != 1:
                raise MultiRootError(
                    "Multiple MCP roots require cwd inside exactly one root"
                )
            selected_boundary = containing[0]
        if cwd is not None:
            ensure_path_within(cwd, selected_boundary)
        discovered_root, _marker = discover_project_root(candidate)
        ensure_path_within(discovered_root, selected_boundary)
        scope = _register_candidate(registry, candidate)
    else:
        startup_root, startup_marker = discover_project_root(
            binding.startup_cwd
        )
        if startup_marker is not None:
            if cwd is not None:
                ensure_path_within(cwd, startup_root)
            scope = registry.register(
                build_project_identity(startup_root, startup_marker)
            )
        else:
            if cwd is None:
                raise ProjectResolutionError(
                    "No marked startup or protocol cwd candidate"
                )
            cwd_root, cwd_marker = discover_project_root(cwd)
            if cwd_marker is None:
                raise ProjectResolutionError(
                    "Protocol cwd has no project marker"
                )
            scope = registry.register(
                build_project_identity(cwd_root, cwd_marker)
            )

    _validate_requested_project(scope, requested_project)
    return scope
