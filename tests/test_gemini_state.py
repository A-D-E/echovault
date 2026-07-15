import pytest

from memory.integrations.gemini_state import (
    ArtifactState,
    GeminiInstallationState,
    GeminiStateConflict,
    GeminiTarget,
    InvalidGeminiState,
    plan_gemini_transition,
)


N = GeminiTarget.NATIVE
U = GeminiTarget.USER_DIRECT
P = GeminiTarget.PROJECT_DIRECT


def state(
    *,
    n: bool = False,
    u: bool = False,
    p: bool = False,
) -> GeminiInstallationState:
    return GeminiInstallationState(
        native=ArtifactState.OWNED if n else ArtifactState.ABSENT,
        user_direct=ArtifactState.OWNED if u else ArtifactState.ABSENT,
        project_direct=ArtifactState.OWNED if p else ArtifactState.ABSENT,
    )


@pytest.mark.parametrize(
    ("current", "target", "expected"),
    [
        (state(), N, "install"),
        (state(), U, "install"),
        (state(), P, "install"),
        (state(n=True), N, "unchanged"),
        (state(n=True), U, "conflict_uninstall_native"),
        (state(n=True), P, "install"),
        (state(u=True), N, "conflict_uninstall_user_direct"),
        (state(u=True), U, "unchanged"),
        (state(u=True), P, "install"),
        (state(p=True), N, "install"),
        (state(p=True), U, "install"),
        (state(p=True), P, "unchanged"),
        (state(n=True, p=True), N, "unchanged"),
        (state(n=True, p=True), U, "conflict_uninstall_native"),
        (state(n=True, p=True), P, "unchanged"),
        (state(u=True, p=True), N, "conflict_uninstall_user_direct"),
        (state(u=True, p=True), U, "unchanged"),
        (state(u=True, p=True), P, "unchanged"),
    ],
)
def test_setup_matrix(
    current: GeminiInstallationState,
    target: GeminiTarget,
    expected: str,
) -> None:
    transition = plan_gemini_transition(current, target, operation="setup")
    assert transition.code == expected


@pytest.mark.parametrize("with_project", [False, True])
def test_invalid_n_plus_u_blocks_setup(with_project: bool) -> None:
    invalid = state(n=True, u=True, p=with_project)
    for target in (N, U, P):
        with pytest.raises(InvalidGeminiState):
            plan_gemini_transition(invalid, target, operation="setup")


@pytest.mark.parametrize("target", [N, U, P])
def test_owned_target_uninstall_is_scope_exact(target: GeminiTarget) -> None:
    current = (
        state(n=True, p=True)
        if target is N
        else (state(u=True, p=True) if target is U else state(n=True, p=True))
    )
    transition = plan_gemini_transition(
        current,
        target,
        operation="uninstall",
    )
    assert transition.code == "remove"
    assert transition.target is target
    assert transition.mutation == "remove"


@pytest.mark.parametrize(
    "artifact",
    [ArtifactState.MODIFIED, ArtifactState.CUSTOM, ArtifactState.MALFORMED],
)
def test_target_conflict_never_proposes_mutation(
    artifact: ArtifactState,
) -> None:
    current = GeminiInstallationState(native=artifact)
    with pytest.raises(GeminiStateConflict):
        plan_gemini_transition(current, N, operation="setup")


def test_outdated_owned_target_plans_update() -> None:
    current = GeminiInstallationState(native=ArtifactState.OWNED_OUTDATED)
    transition = plan_gemini_transition(current, N, operation="setup")
    assert transition.code == "update"
    assert transition.mutation == "update"


def test_custom_other_global_mode_blocks_second_global_mode() -> None:
    current = GeminiInstallationState(native=ArtifactState.CUSTOM)
    with pytest.raises(GeminiStateConflict):
        plan_gemini_transition(current, U, operation="setup")


def state_with_target(
    target: GeminiTarget,
    artifact: ArtifactState,
) -> GeminiInstallationState:
    values = {
        "native": ArtifactState.ABSENT,
        "user_direct": ArtifactState.ABSENT,
        "project_direct": ArtifactState.ABSENT,
    }
    values[
        {
            N: "native",
            U: "user_direct",
            P: "project_direct",
        }[target]
    ] = artifact
    return GeminiInstallationState(**values)


@pytest.mark.parametrize("target", [N, U, P])
@pytest.mark.parametrize(
    ("operation", "expected"),
    [("setup", "update"), ("uninstall", "remove")],
)
def test_force_managed_allows_only_selected_modified_target(
    target: GeminiTarget,
    operation: str,
    expected: str,
) -> None:
    modified = state_with_target(target, ArtifactState.MODIFIED)
    with pytest.raises(GeminiStateConflict):
        plan_gemini_transition(modified, target, operation=operation)
    transition = plan_gemini_transition(
        modified,
        target,
        operation=operation,
        force_managed=True,
    )
    assert transition.code == expected


@pytest.mark.parametrize(
    "artifact",
    [ArtifactState.CUSTOM, ArtifactState.MALFORMED],
)
@pytest.mark.parametrize("operation", ["setup", "uninstall"])
def test_force_never_overrides_custom_or_malformed(
    artifact: ArtifactState,
    operation: str,
) -> None:
    with pytest.raises(GeminiStateConflict):
        plan_gemini_transition(
            state_with_target(N, artifact),
            N,
            operation=operation,
            force_managed=True,
        )


def test_force_never_overrides_modified_other_global_scope() -> None:
    current = GeminiInstallationState(user_direct=ArtifactState.MODIFIED)
    with pytest.raises(GeminiStateConflict):
        plan_gemini_transition(
            current,
            N,
            operation="setup",
            force_managed=True,
        )
