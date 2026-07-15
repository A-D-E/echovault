from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

from memory.integrations.ownership import OwnershipConflict


class GeminiTarget(str, Enum):
    NATIVE = "native"
    USER_DIRECT = "user-direct"
    PROJECT_DIRECT = "project-direct"


class ArtifactState(str, Enum):
    ABSENT = "absent"
    OWNED = "owned"
    OWNED_OUTDATED = "owned-outdated"
    MODIFIED = "modified"
    CUSTOM = "custom"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class GeminiInstallationState:
    native: ArtifactState = ArtifactState.ABSENT
    user_direct: ArtifactState = ArtifactState.ABSENT
    project_direct: ArtifactState = ArtifactState.ABSENT
    native_enabled: bool = True
    conflicts: tuple[str, ...] = ()


@dataclass(frozen=True)
class GeminiTransition:
    code: str
    target: GeminiTarget
    mutation: Literal["install", "update", "remove", "none"]
    message: str


class InvalidGeminiState(ValueError):
    pass


class GeminiStateConflict(OwnershipConflict):
    pass


_PRESENT_MANAGED = {
    ArtifactState.OWNED,
    ArtifactState.OWNED_OUTDATED,
    ArtifactState.MODIFIED,
}


def _target_state(
    state: GeminiInstallationState,
    target: GeminiTarget,
) -> ArtifactState:
    return {
        GeminiTarget.NATIVE: state.native,
        GeminiTarget.USER_DIRECT: state.user_direct,
        GeminiTarget.PROJECT_DIRECT: state.project_direct,
    }[target]


def plan_gemini_transition(
    state: GeminiInstallationState,
    target: GeminiTarget,
    operation: Literal["setup", "uninstall"],
    force_managed: bool = False,
) -> GeminiTransition:
    if operation == "setup" and (
        state.native in _PRESENT_MANAGED
        and state.user_direct in _PRESENT_MANAGED
    ):
        raise InvalidGeminiState(
            "Native and user-direct Gemini modes cannot coexist"
        )

    current = _target_state(state, target)
    if current in {ArtifactState.CUSTOM, ArtifactState.MALFORMED}:
        raise GeminiStateConflict(f"{target.value} is {current.value}")
    if current is ArtifactState.MODIFIED and not force_managed:
        raise GeminiStateConflict(
            f"{target.value} is modified; use --force-managed"
        )

    if operation == "uninstall":
        code = "remove" if current in _PRESENT_MANAGED else "unchanged"
        mutation: Literal["remove", "none"] = (
            "remove" if current in _PRESENT_MANAGED else "none"
        )
        return GeminiTransition(code, target, mutation, code)

    if (
        target is GeminiTarget.NATIVE
        and state.user_direct is not ArtifactState.ABSENT
    ):
        if state.user_direct in {
            ArtifactState.OWNED,
            ArtifactState.OWNED_OUTDATED,
        }:
            return GeminiTransition(
                "conflict_uninstall_user_direct",
                target,
                "none",
                "Run memory uninstall gemini --direct first",
            )
        raise GeminiStateConflict(
            f"user-direct is {state.user_direct.value}; force applies only "
            "to the selected target"
        )
    if (
        target is GeminiTarget.USER_DIRECT
        and state.native is not ArtifactState.ABSENT
    ):
        if state.native in {
            ArtifactState.OWNED,
            ArtifactState.OWNED_OUTDATED,
        }:
            return GeminiTransition(
                "conflict_uninstall_native",
                target,
                "none",
                "Run memory uninstall gemini first",
            )
        raise GeminiStateConflict(
            f"native is {state.native.value}; force applies only to the "
            "selected target"
        )
    if current is ArtifactState.OWNED:
        return GeminiTransition("unchanged", target, "none", "unchanged")
    if current in {ArtifactState.OWNED_OUTDATED, ArtifactState.MODIFIED}:
        return GeminiTransition("update", target, "update", "update")
    return GeminiTransition("install", target, "install", "install")
