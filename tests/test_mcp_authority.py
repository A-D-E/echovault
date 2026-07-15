from pathlib import Path

import pytest

from memory.mcp_authority import (
    AUTHORITY_ERROR,
    BOUNDARY_ERROR,
    PROJECT_ERROR,
    ROOTS_ERROR,
    AuthorityConflict,
    MCPServerBinding,
    ensure_path_within,
    file_uri_to_path,
    public_project_error,
    resolve_bound_identity,
    resolve_project_scope,
)
from memory.projects import (
    MultiRootError,
    ProjectIdentity,
    ProjectRegistry,
    ProjectResolutionError,
    ProjectScope,
    build_project_identity,
    discover_project_root,
)


def marked_workspace(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "package.json").write_text("{}", encoding="utf-8")
    return path


def new_registry(tmp_path: Path) -> ProjectRegistry:
    return ProjectRegistry(tmp_path / "memory-home")


def test_bound_identity_accepts_omitted_or_matching_and_rejects_spoof() -> None:
    assert resolve_bound_identity("cursor", None, field_name="source") == "cursor"
    assert (
        resolve_bound_identity("cursor", "cursor", field_name="source")
        == "cursor"
    )
    with pytest.raises(AuthorityConflict) as raised:
        resolve_bound_identity(
            "cursor",
            "gemini-cli",
            field_name="source",
        )
    assert public_project_error(raised.value) == AUTHORITY_ERROR


def test_public_project_error_ignores_low_level_text_and_paths(
    tmp_path: Path,
) -> None:
    private = tmp_path / "private-repository"
    cases = (
        (
            AuthorityConflict("boundary", f"escaped through {private}"),
            BOUNDARY_ERROR,
        ),
        (
            AuthorityConflict("project", f"alias registry at {private}"),
            PROJECT_ERROR,
        ),
        (
            AuthorityConflict("authority", f"bound source details at {private}"),
            AUTHORITY_ERROR,
        ),
        (MultiRootError(f"candidate roots include {private}"), ROOTS_ERROR),
        (
            ProjectResolutionError(f"invalid gitdir pointer {private}"),
            ROOTS_ERROR,
        ),
    )
    for error, expected in cases:
        public = public_project_error(error)
        assert public == expected
        assert str(private) not in public
        assert "gitdir" not in public
        assert "candidate roots" not in public


def test_cwd_must_remain_inside_client_root(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    assert ensure_path_within(root / "src", root) == (root / "src").resolve()
    with pytest.raises(AuthorityConflict) as raised:
        ensure_path_within(tmp_path / "other", root)
    public = public_project_error(raised.value)
    assert public == BOUNDARY_ERROR
    assert str(root) not in public
    assert str(tmp_path / "other") not in public


def test_file_uri_decodes_spaces_and_rejects_non_file_scheme(
    tmp_path: Path,
) -> None:
    assert file_uri_to_path((tmp_path / "my repo").as_uri()) == (
        tmp_path / "my repo"
    ).resolve()
    with pytest.raises(ProjectResolutionError) as raised:
        file_uri_to_path("https://example.test/repo")
    assert public_project_error(raised.value) == ROOTS_ERROR


def test_legacy_unbound_scope_returns_original_project_without_registry_write(
    tmp_path: Path,
) -> None:
    class RegistryMustNotBeCalled:
        def register(self, identity: ProjectIdentity) -> ProjectScope:
            raise AssertionError(
                "legacy unbound resolution must not fabricate a project path"
            )

    binding = MCPServerBinding(
        agent=None,
        project_root=None,
        startup_cwd=tmp_path,
    )
    registry = RegistryMustNotBeCalled()
    assert resolve_project_scope(
        binding,
        registry,
        client_roots=(tmp_path / "ignored",),
        cwd=tmp_path / "ignored",
        requested_project="legacy-project",
    ) == "legacy-project"
    assert (
        resolve_project_scope(
            binding,
            registry,
            client_roots=(),
            cwd=None,
            requested_project=None,
        )
        is None
    )


def test_project_bound_root_is_authoritative_and_accepts_key_or_alias(
    tmp_path: Path,
) -> None:
    root = marked_workspace(tmp_path / "repo")
    nested = root / "src"
    nested.mkdir()
    registry = new_registry(tmp_path)
    identity = build_project_identity(*discover_project_root(root))
    registry.register(identity)
    scope_with_alias = registry.adopt_legacy("legacy-repo", identity)
    binding = MCPServerBinding("cursor", root, tmp_path)

    for requested in (None, scope_with_alias.identity.key, "legacy-repo"):
        scope = resolve_project_scope(
            binding,
            registry,
            client_roots=(root,),
            cwd=nested,
            requested_project=requested,
        )
        assert isinstance(scope, ProjectScope)
        assert scope.identity.key == scope_with_alias.identity.key
        assert "legacy-repo" in scope.storage_keys

    with pytest.raises(AuthorityConflict) as raised:
        resolve_project_scope(
            binding,
            registry,
            client_roots=(root,),
            cwd=nested,
            requested_project="other-project",
        )
    assert public_project_error(raised.value) == PROJECT_ERROR


def test_project_bound_server_rejects_conflicting_root_without_disclosing_paths(
    tmp_path: Path,
) -> None:
    root = marked_workspace(tmp_path / "repo")
    other = marked_workspace(tmp_path / "private-other")
    binding = MCPServerBinding("cursor", root, tmp_path)
    with pytest.raises(AuthorityConflict) as raised:
        resolve_project_scope(
            binding,
            new_registry(tmp_path),
            client_roots=(other,),
            cwd=root,
            requested_project=None,
        )
    message = public_project_error(raised.value)
    assert message == BOUNDARY_ERROR
    assert "private-other" not in message
    assert str(root) not in message


def test_global_bound_server_uses_cwd_to_select_one_of_many_roots(
    tmp_path: Path,
) -> None:
    left = marked_workspace(tmp_path / "left")
    right = marked_workspace(tmp_path / "right")
    nested = right / "src"
    nested.mkdir()
    binding = MCPServerBinding("gemini-cli", None, tmp_path)
    scope = resolve_project_scope(
        binding,
        new_registry(tmp_path),
        client_roots=(left, right),
        cwd=nested,
        requested_project=None,
    )
    assert isinstance(scope, ProjectScope)
    assert scope.identity.key == build_project_identity(
        *discover_project_root(right)
    ).key


def test_global_bound_server_rejects_many_roots_without_unique_cwd(
    tmp_path: Path,
) -> None:
    left = marked_workspace(tmp_path / "left")
    right = marked_workspace(tmp_path / "right")
    binding = MCPServerBinding("cursor", None, tmp_path)
    with pytest.raises(MultiRootError) as raised:
        resolve_project_scope(
            binding,
            new_registry(tmp_path),
            client_roots=(left, right),
            cwd=None,
            requested_project=None,
        )
    assert public_project_error(raised.value) == ROOTS_ERROR


def test_global_bound_server_without_roots_uses_marked_startup_boundary(
    tmp_path: Path,
) -> None:
    root = marked_workspace(tmp_path / "repo")
    startup = root / "tools"
    startup.mkdir()
    cwd = root / "src"
    cwd.mkdir()
    scope = resolve_project_scope(
        MCPServerBinding("cursor", None, startup),
        new_registry(tmp_path),
        client_roots=(),
        cwd=cwd,
        requested_project=None,
    )
    assert isinstance(scope, ProjectScope)
    assert scope.identity.key == build_project_identity(
        *discover_project_root(root)
    ).key


def test_global_bound_server_without_roots_uses_marked_protocol_cwd_from_generic_startup(
    tmp_path: Path,
) -> None:
    generic = tmp_path / "generic-home"
    project = marked_workspace(tmp_path / "workspace")
    cwd = project / "src"
    generic.mkdir()
    cwd.mkdir()
    binding = MCPServerBinding("cursor", None, generic)
    scope = resolve_project_scope(
        binding,
        new_registry(tmp_path),
        client_roots=(),
        cwd=cwd,
        requested_project=None,
    )
    assert isinstance(scope, ProjectScope)
    assert scope.identity.key == build_project_identity(
        *discover_project_root(project)
    ).key
    with pytest.raises(ProjectResolutionError) as raised:
        resolve_project_scope(
            binding,
            new_registry(tmp_path),
            client_roots=(),
            cwd=generic,
            requested_project=None,
        )
    assert public_project_error(raised.value) == ROOTS_ERROR


def test_symlink_cwd_cannot_escape_authorized_root(tmp_path: Path) -> None:
    root = marked_workspace(tmp_path / "repo")
    outside = tmp_path / "secret-location"
    outside.mkdir()
    link = root / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("host does not permit test symlink creation")
    binding = MCPServerBinding("cursor", root, root)
    with pytest.raises(AuthorityConflict) as raised:
        resolve_project_scope(
            binding,
            new_registry(tmp_path),
            client_roots=(root,),
            cwd=link,
            requested_project=None,
        )
    public = public_project_error(raised.value)
    assert public == BOUNDARY_ERROR
    assert "secret-location" not in public
    assert str(root) not in public
