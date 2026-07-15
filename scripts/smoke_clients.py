from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
import dataclasses
import hashlib
import hmac
import importlib.metadata
import json
import os
import re
import subprocess
import time
import uuid

import click

from memory.integrations.process import (
    CommandRunner,
    SubprocessRunner,
)
from memory.safe_io import prepare_atomic_text


class SmokeStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_VERIFIED = "not_verified"
    OBSERVATIONAL_MISS = "observational_miss"


class ReportClaim(str, Enum):
    NOT_VERIFIED = "not_verified"
    CLIENT_VERIFIED = "client_verified"


class SmokeClient(str, Enum):
    CURSOR_IDE = "cursor-ide"
    CURSOR_CLI = "cursor-cli"
    GEMINI_CLI = "gemini-cli"


class BoundAgent(str, Enum):
    CURSOR = "cursor"
    GEMINI_CLI = "gemini-cli"


class SmokeScope(str, Enum):
    USER = "user"
    PROJECT = "project"


class SmokeCommand(str, Enum):
    CURSOR_IDE_MANUAL = "cursor-ide-manual"
    CURSOR_INTERACTIVE = "cursor-interactive"
    CURSOR_PROBE = "cursor-probe"
    CURSOR_HEADLESS = "cursor-headless"
    GEMINI_PROBE = "gemini-probe"
    GEMINI_HEADLESS = "gemini-headless"


class EvidenceCode(str, Enum):
    BINARY_MISSING = "binary_missing"
    VERSION_MISMATCH = "version_mismatch"
    AUTH_MISSING = "auth_missing"
    MCP_DISCOVERED = "mcp_discovered"
    FOUR_TOOLS = "four_tools"
    EXTENSION_DISCOVERED = "extension_discovered"
    MARKER_FOUND = "marker_found"
    MARKER_MISSED = "marker_missed"
    TIMEOUT = "timeout"
    CLIENT_ERROR = "client_error"


class ReasonCode(str, Enum):
    VERIFIED = "verified"
    BINARY_MISSING = "binary_missing"
    VERSION_MISMATCH = "version_mismatch"
    AUTH_MISSING = "auth_missing"
    MARKER_MISSED = "marker_missed"
    TIMEOUT = "timeout"
    CLIENT_ERROR = "client_error"


class EvidenceValidationError(ValueError):
    pass


@dataclass(frozen=True)
class BoundMarkerEvidence:
    binding_agent: BoundAgent
    marker_id: str
    operation_id: str
    memory_id: str
    replay_verified: bool

    def __post_init__(self) -> None:
        if not isinstance(self.binding_agent, BoundAgent):
            raise EvidenceValidationError("invalid bound agent")
        for field_name in ("marker_id", "operation_id", "memory_id"):
            value = getattr(self, field_name)
            try:
                uuid.UUID(value)
            except (AttributeError, TypeError, ValueError) as error:
                raise EvidenceValidationError(
                    f"invalid {field_name}"
                ) from error
        if self.replay_verified is not True:
            raise EvidenceValidationError(
                "bound marker replay is not verified"
            )


ALLOWED_SMOKE_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USERPROFILE",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "LANG",
        "LC_ALL",
        "TMP",
        "TEMP",
        "TMPDIR",
        "SystemRoot",
        "WINDIR",
        "PATHEXT",
        "COMSPEC",
    }
)

VERSION_PATTERNS = {
    SmokeClient.CURSOR_IDE: re.compile(r"^\d+\.\d+\.\d+$"),
    SmokeClient.CURSOR_CLI: re.compile(
        r"^\d{4}\.\d{2}\.\d{2}-[0-9a-f]{7}$"
    ),
    SmokeClient.GEMINI_CLI: re.compile(r"^\d+\.\d+\.\d+$"),
}


def validate_version(client: SmokeClient, value: str | None) -> None:
    if (
        value is not None
        and VERSION_PATTERNS[client].fullmatch(value) is None
    ):
        raise EvidenceValidationError("invalid version")


@dataclass(frozen=True)
class SmokeEvidence:
    client: SmokeClient
    expected_version: str
    observed_version: str | None
    scope: SmokeScope
    command: SmokeCommand
    exit_code: int | None
    duration_ms: int
    marker_id: str | None
    marker_digest: str | None
    evidence_codes: tuple[EvidenceCode, ...]
    status: SmokeStatus
    reason_code: ReasonCode

    def __post_init__(self) -> None:
        allowed = {
            (SmokeClient.CURSOR_CLI, SmokeCommand.CURSOR_PROBE),
            (SmokeClient.CURSOR_CLI, SmokeCommand.CURSOR_HEADLESS),
            (SmokeClient.GEMINI_CLI, SmokeCommand.GEMINI_PROBE),
            (SmokeClient.GEMINI_CLI, SmokeCommand.GEMINI_HEADLESS),
        }
        if (self.client, self.command) not in allowed:
            raise EvidenceValidationError(
                "invalid automated client/command"
            )
        validate_version(self.client, self.expected_version)
        validate_version(self.client, self.observed_version)
        marker_values = (self.marker_id, self.marker_digest)
        if (marker_values[0] is None) != (marker_values[1] is None):
            raise EvidenceValidationError(
                "marker id and digest must be paired"
            )
        if self.command in {
            SmokeCommand.CURSOR_PROBE,
            SmokeCommand.GEMINI_PROBE,
        }:
            if marker_values != (None, None):
                raise EvidenceValidationError(
                    "probe evidence has no marker"
                )
        elif None in marker_values:
            raise EvidenceValidationError(
                "headless evidence requires marker"
            )
        if self.marker_id is not None:
            try:
                uuid.UUID(self.marker_id)
            except ValueError as error:
                raise EvidenceValidationError(
                    "invalid marker UUID"
                ) from error
            expected = hashlib.sha256(
                self.marker_id.encode("ascii")
            ).hexdigest()
            if not hmac.compare_digest(self.marker_digest or "", expected):
                raise EvidenceValidationError("invalid marker digest")


@dataclass(frozen=True)
class ManualEvidence:
    client: SmokeClient
    expected_version: str
    observed_version: str | None
    scope: SmokeScope
    command: SmokeCommand
    evidence_codes: tuple[EvidenceCode, ...]
    status: SmokeStatus
    reason_code: ReasonCode

    def __post_init__(self) -> None:
        allowed = {
            (SmokeClient.CURSOR_IDE, SmokeCommand.CURSOR_IDE_MANUAL),
            (SmokeClient.CURSOR_CLI, SmokeCommand.CURSOR_INTERACTIVE),
        }
        if (self.client, self.command) not in allowed:
            raise EvidenceValidationError("invalid manual client/command")
        validate_version(self.client, self.expected_version)
        validate_version(self.client, self.observed_version)


@dataclass(frozen=True)
class SmokeReport:
    echovault_version: str
    commit: str
    generated_at: str
    evidence: tuple[SmokeEvidence, ...]
    manual_evidence: tuple[ManualEvidence, ...] = ()
    report_claim: ReportClaim = ReportClaim.NOT_VERIFIED

    def __post_init__(self) -> None:
        if not isinstance(self.report_claim, ReportClaim):
            raise EvidenceValidationError("invalid report claim")
        if (
            type(self) is SmokeReport
            and self.report_claim is not ReportClaim.NOT_VERIFIED
        ):
            raise EvidenceValidationError(
                "partial report claim must be not_verified"
            )
        if re.fullmatch(r"[0-9a-f]{7,40}", self.commit) is None:
            raise EvidenceValidationError("invalid commit")
        try:
            datetime.strptime(self.generated_at, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError as error:
            raise EvidenceValidationError("invalid generated_at") from error
        if not self.generated_at.endswith("Z"):
            raise EvidenceValidationError("invalid generated_at")
        if re.fullmatch(r"\d+\.\d+\.\d+", self.echovault_version) is None:
            raise EvidenceValidationError("invalid EchoVault version")


def validate_cross_agent_gate(
    markers: tuple[BoundMarkerEvidence, ...],
) -> None:
    required_agents = {BoundAgent.CURSOR, BoundAgent.GEMINI_CLI}
    if (
        len(markers) != 2
        or {item.binding_agent for item in markers} != required_agents
    ):
        raise EvidenceValidationError(
            "final dogfood requires exactly one Cursor-bound and one "
            "Gemini-bound marker"
        )
    if any(item.replay_verified is not True for item in markers):
        raise EvidenceValidationError(
            "every bound marker must verify replay"
        )
    for field_name in ("marker_id", "operation_id", "memory_id"):
        if len({getattr(item, field_name) for item in markers}) != 2:
            raise EvidenceValidationError(
                f"bound markers require distinct {field_name} values"
            )


EXPECTED_CLIENT_VERSIONS = {
    SmokeClient.CURSOR_IDE: "3.11.19",
    SmokeClient.CURSOR_CLI: "2026.07.09-a3815c0",
    SmokeClient.GEMINI_CLI: "0.50.0",
}
REQUIRED_AUTOMATED_CELLS = {
    (SmokeClient.CURSOR_CLI, SmokeCommand.CURSOR_PROBE),
    (SmokeClient.CURSOR_CLI, SmokeCommand.CURSOR_HEADLESS),
    (SmokeClient.GEMINI_CLI, SmokeCommand.GEMINI_PROBE),
    (SmokeClient.GEMINI_CLI, SmokeCommand.GEMINI_HEADLESS),
}
REQUIRED_MANUAL_CELLS = {
    (SmokeClient.CURSOR_IDE, SmokeCommand.CURSOR_IDE_MANUAL),
    (SmokeClient.CURSOR_CLI, SmokeCommand.CURSOR_INTERACTIVE),
}


def validate_client_verified_gate(report: SmokeReport) -> None:
    automated = {
        (item.client, item.command): item for item in report.evidence
    }
    manual = {
        (item.client, item.command): item
        for item in report.manual_evidence
    }
    if (
        len(automated) != len(report.evidence)
        or set(automated) != REQUIRED_AUTOMATED_CELLS
    ):
        raise EvidenceValidationError(
            "client_verified requires each automated cell once"
        )
    if (
        len(manual) != len(report.manual_evidence)
        or set(manual) != REQUIRED_MANUAL_CELLS
    ):
        raise EvidenceValidationError(
            "client_verified requires each manual cell once"
        )
    for item in report.evidence:
        expected = EXPECTED_CLIENT_VERSIONS[item.client]
        if (
            item.expected_version != expected
            or item.observed_version != expected
            or item.status is not SmokeStatus.PASSED
            or item.reason_code is not ReasonCode.VERIFIED
            or (
                item.command
                in {
                    SmokeCommand.CURSOR_HEADLESS,
                    SmokeCommand.GEMINI_HEADLESS,
                }
                and EvidenceCode.MARKER_FOUND not in item.evidence_codes
            )
        ):
            raise EvidenceValidationError(
                "client_verified requires exact pinned automated success"
            )
    for item in report.manual_evidence:
        expected = EXPECTED_CLIENT_VERSIONS[item.client]
        if (
            item.expected_version != expected
            or item.observed_version != expected
            or item.status
            not in {
                SmokeStatus.PASSED,
                SmokeStatus.OBSERVATIONAL_MISS,
            }
            or (
                item.status is SmokeStatus.PASSED
                and (
                    item.reason_code is not ReasonCode.VERIFIED
                    or EvidenceCode.MARKER_FOUND
                    not in item.evidence_codes
                )
            )
            or (
                item.status is SmokeStatus.OBSERVATIONAL_MISS
                and (
                    item.reason_code is not ReasonCode.MARKER_MISSED
                    or EvidenceCode.MARKER_MISSED
                    not in item.evidence_codes
                )
            )
        ):
            raise EvidenceValidationError(
                "client_verified requires an exact pinned manual observation"
            )


@dataclass(frozen=True)
class FinalDogfoodReport(SmokeReport):
    bound_markers: tuple[BoundMarkerEvidence, ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        validate_cross_agent_gate(self.bound_markers)
        if self.report_claim is ReportClaim.CLIENT_VERIFIED:
            validate_client_verified_gate(self)


PARTIAL_REPORT_FIELDS = frozenset(
    {
        "echovault_version",
        "commit",
        "generated_at",
        "evidence",
        "manual_evidence",
        "report_claim",
    }
)
FINAL_REPORT_FIELDS = PARTIAL_REPORT_FIELDS | {"bound_markers"}
SMOKE_EVIDENCE_FIELDS = frozenset(
    {
        "client",
        "expected_version",
        "observed_version",
        "scope",
        "command",
        "exit_code",
        "duration_ms",
        "marker_id",
        "marker_digest",
        "evidence_codes",
        "status",
        "reason_code",
    }
)
MANUAL_EVIDENCE_FIELDS = frozenset(
    {
        "client",
        "expected_version",
        "observed_version",
        "scope",
        "command",
        "evidence_codes",
        "status",
        "reason_code",
    }
)
BOUND_MARKER_FIELDS = frozenset(
    {
        "binding_agent",
        "marker_id",
        "operation_id",
        "memory_id",
        "replay_verified",
    }
)


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise EvidenceValidationError("invalid JSON object")
        result[key] = value
    return result


def _load_json(path: Path) -> object:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except EvidenceValidationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceValidationError("invalid JSON document") from error


def _exact_object(
    value: object,
    fields: frozenset[str] | set[str],
    label: str,
) -> dict[str, object]:
    if type(value) is not dict or set(value) != set(fields):
        raise EvidenceValidationError(f"invalid {label} schema")
    return value


def _string(value: object, label: str) -> str:
    if type(value) is not str:
        raise EvidenceValidationError(f"invalid {label}")
    return value


def _optional_string(value: object, label: str) -> str | None:
    if value is not None and type(value) is not str:
        raise EvidenceValidationError(f"invalid {label}")
    return value


def _enum(enum_type, value: object, label: str):
    if type(value) is not str:
        raise EvidenceValidationError(f"invalid {label}")
    try:
        return enum_type(value)
    except ValueError as error:
        raise EvidenceValidationError(f"invalid {label}") from error


def _evidence_codes(value: object) -> tuple[EvidenceCode, ...]:
    if type(value) is not list:
        raise EvidenceValidationError("invalid evidence codes")
    return tuple(
        _enum(EvidenceCode, item, "evidence code") for item in value
    )


def _smoke_evidence(value: object) -> SmokeEvidence:
    item = _exact_object(
        value,
        SMOKE_EVIDENCE_FIELDS,
        "smoke evidence",
    )
    exit_code = item["exit_code"]
    if exit_code is not None and type(exit_code) is not int:
        raise EvidenceValidationError("invalid exit code")
    duration_ms = item["duration_ms"]
    if type(duration_ms) is not int or duration_ms < 0:
        raise EvidenceValidationError("invalid duration")
    return SmokeEvidence(
        client=_enum(SmokeClient, item["client"], "client"),
        expected_version=_string(
            item["expected_version"],
            "expected version",
        ),
        observed_version=_optional_string(
            item["observed_version"],
            "observed version",
        ),
        scope=_enum(SmokeScope, item["scope"], "scope"),
        command=_enum(SmokeCommand, item["command"], "command"),
        exit_code=exit_code,
        duration_ms=duration_ms,
        marker_id=_optional_string(item["marker_id"], "marker id"),
        marker_digest=_optional_string(
            item["marker_digest"],
            "marker digest",
        ),
        evidence_codes=_evidence_codes(item["evidence_codes"]),
        status=_enum(SmokeStatus, item["status"], "status"),
        reason_code=_enum(
            ReasonCode,
            item["reason_code"],
            "reason code",
        ),
    )


def _manual_evidence(value: object) -> ManualEvidence:
    item = _exact_object(
        value,
        MANUAL_EVIDENCE_FIELDS,
        "manual evidence",
    )
    return ManualEvidence(
        client=_enum(SmokeClient, item["client"], "client"),
        expected_version=_string(
            item["expected_version"],
            "expected version",
        ),
        observed_version=_optional_string(
            item["observed_version"],
            "observed version",
        ),
        scope=_enum(SmokeScope, item["scope"], "scope"),
        command=_enum(SmokeCommand, item["command"], "command"),
        evidence_codes=_evidence_codes(item["evidence_codes"]),
        status=_enum(SmokeStatus, item["status"], "status"),
        reason_code=_enum(
            ReasonCode,
            item["reason_code"],
            "reason code",
        ),
    )


def _bound_marker(value: object) -> BoundMarkerEvidence:
    item = _exact_object(value, BOUND_MARKER_FIELDS, "bound marker")
    if type(item["replay_verified"]) is not bool:
        raise EvidenceValidationError("invalid replay verification")
    return BoundMarkerEvidence(
        binding_agent=_enum(
            BoundAgent,
            item["binding_agent"],
            "bound agent",
        ),
        marker_id=_string(item["marker_id"], "marker id"),
        operation_id=_string(item["operation_id"], "operation id"),
        memory_id=_string(item["memory_id"], "memory id"),
        replay_verified=item["replay_verified"],
    )


def _report_values(
    value: object,
    fields: frozenset[str] | set[str],
) -> tuple[dict[str, object], dict[str, object]]:
    item = _exact_object(value, fields, "report")
    evidence = item["evidence"]
    manual_evidence = item["manual_evidence"]
    if type(evidence) is not list or type(manual_evidence) is not list:
        raise EvidenceValidationError("invalid report evidence")
    values = {
        "echovault_version": _string(
            item["echovault_version"],
            "EchoVault version",
        ),
        "commit": _string(item["commit"], "commit"),
        "generated_at": _string(
            item["generated_at"],
            "generated timestamp",
        ),
        "evidence": tuple(_smoke_evidence(record) for record in evidence),
        "manual_evidence": tuple(
            _manual_evidence(record) for record in manual_evidence
        ),
        "report_claim": _enum(
            ReportClaim,
            item["report_claim"],
            "report claim",
        ),
    }
    return item, values


def load_partial_report(path: Path) -> SmokeReport:
    _, values = _report_values(
        _load_json(path),
        PARTIAL_REPORT_FIELDS,
    )
    if values["report_claim"] is not ReportClaim.NOT_VERIFIED:
        raise EvidenceValidationError(
            "partial report claim must be not_verified"
        )
    return SmokeReport(**values)


def load_bound_marker(path: Path) -> BoundMarkerEvidence:
    return _bound_marker(_load_json(path))


def load_final_report(path: Path) -> FinalDogfoodReport:
    item, values = _report_values(_load_json(path), FINAL_REPORT_FIELDS)
    markers = item["bound_markers"]
    if type(markers) is not list:
        raise EvidenceValidationError("invalid bound marker list")
    return FinalDogfoodReport(
        **values,
        bound_markers=tuple(_bound_marker(marker) for marker in markers),
    )


def write_bound_marker(
    marker: BoundMarkerEvidence,
    output: Path,
) -> Path:
    payload = (
        json.dumps(dataclasses.asdict(marker), indent=2, sort_keys=True)
        + "\n"
    )
    prepared = prepare_atomic_text(output, payload)
    prepared.replace()
    return output


def finalize_report(
    partial_path: Path,
    marker_paths: tuple[Path, ...],
    output: Path,
    claim: ReportClaim,
) -> Path:
    if len(marker_paths) != 2:
        raise EvidenceValidationError(
            "finalize requires exactly two marker files"
        )
    partial = load_partial_report(partial_path)
    markers = tuple(load_bound_marker(path) for path in marker_paths)
    report = FinalDogfoodReport(
        echovault_version=partial.echovault_version,
        commit=partial.commit,
        generated_at=partial.generated_at,
        evidence=partial.evidence,
        manual_evidence=partial.manual_evidence,
        report_claim=claim,
        bound_markers=markers,
    )
    return write_report(report, output)


def validate_final_report(
    path: Path,
    required_claim: ReportClaim | None = None,
) -> FinalDogfoodReport:
    report = load_final_report(path)
    if (
        required_claim is not None
        and report.report_claim is not required_claim
    ):
        raise EvidenceValidationError("final report has the wrong claim")
    return report


def build_smoke_environment(
    environment: Mapping[str, str],
) -> dict[str, str]:
    return {
        key: value
        for key, value in environment.items()
        if key in ALLOWED_SMOKE_ENV_KEYS
    }


def headless_command(client: SmokeClient, prompt: str) -> list[str]:
    if client is SmokeClient.CURSOR_CLI:
        return ["agent", "-p", "--output-format", "json", prompt]
    if client is SmokeClient.GEMINI_CLI:
        return [
            "gemini",
            "-p",
            prompt,
            "--output-format",
            "json",
        ]
    raise ValueError(f"Unsupported headless smoke client: {client.value}")


def _workspace(path: Path) -> Path:
    if path.is_symlink():
        raise EvidenceValidationError("workspace must not be a symlink")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise EvidenceValidationError("workspace is unavailable") from error
    if not resolved.is_dir():
        raise EvidenceValidationError("workspace must be a directory")
    return resolved


def _probe_result(
    *,
    client: SmokeClient,
    observed_version: str | None,
    returncode: int | None,
    duration_ms: int,
    codes: tuple[EvidenceCode, ...],
    status: SmokeStatus,
    reason: ReasonCode,
) -> SmokeEvidence:
    command = (
        SmokeCommand.CURSOR_PROBE
        if client is SmokeClient.CURSOR_CLI
        else SmokeCommand.GEMINI_PROBE
    )
    return SmokeEvidence(
        client=client,
        expected_version=EXPECTED_CLIENT_VERSIONS[client],
        observed_version=observed_version,
        scope=SmokeScope.PROJECT,
        command=command,
        exit_code=returncode,
        duration_ms=duration_ms,
        marker_id=None,
        marker_digest=None,
        evidence_codes=codes,
        status=status,
        reason_code=reason,
    )


def probe_client(
    client: SmokeClient,
    workspace: Path,
    runner: CommandRunner,
    environment: Mapping[str, str],
) -> SmokeEvidence:
    if client not in {
        SmokeClient.CURSOR_CLI,
        SmokeClient.GEMINI_CLI,
    }:
        raise EvidenceValidationError("unsupported automated client")
    cwd = _workspace(workspace)
    safe_environment = build_smoke_environment(environment)
    version_command = (
        ["agent", "--version"]
        if client is SmokeClient.CURSOR_CLI
        else ["gemini", "--version"]
    )
    started = time.monotonic()
    try:
        version = runner.run(
            version_command,
            timeout=10.0,
            cwd=cwd,
            env=safe_environment,
        )
    except FileNotFoundError:
        duration = max(0, round((time.monotonic() - started) * 1000))
        return _probe_result(
            client=client,
            observed_version=None,
            returncode=127,
            duration_ms=duration,
            codes=(EvidenceCode.BINARY_MISSING,),
            status=SmokeStatus.NOT_VERIFIED,
            reason=ReasonCode.BINARY_MISSING,
        )
    except (TimeoutError, subprocess.TimeoutExpired):
        duration = max(0, round((time.monotonic() - started) * 1000))
        return _probe_result(
            client=client,
            observed_version=None,
            returncode=None,
            duration_ms=duration,
            codes=(EvidenceCode.TIMEOUT,),
            status=SmokeStatus.FAILED,
            reason=ReasonCode.TIMEOUT,
        )
    duration = max(0, round((time.monotonic() - started) * 1000))
    if version.returncode == 127:
        return _probe_result(
            client=client,
            observed_version=None,
            returncode=version.returncode,
            duration_ms=duration,
            codes=(EvidenceCode.BINARY_MISSING,),
            status=SmokeStatus.NOT_VERIFIED,
            reason=ReasonCode.BINARY_MISSING,
        )
    if version.returncode != 0:
        return _probe_result(
            client=client,
            observed_version=None,
            returncode=version.returncode,
            duration_ms=duration,
            codes=(EvidenceCode.AUTH_MISSING,),
            status=SmokeStatus.NOT_VERIFIED,
            reason=ReasonCode.AUTH_MISSING,
        )
    expected = EXPECTED_CLIENT_VERSIONS[client]
    stripped_version = version.stdout.strip()
    observed = stripped_version if stripped_version == expected else None
    if observed is None:
        return _probe_result(
            client=client,
            observed_version=None,
            returncode=version.returncode,
            duration_ms=duration,
            codes=(EvidenceCode.VERSION_MISMATCH,),
            status=SmokeStatus.NOT_VERIFIED,
            reason=ReasonCode.VERSION_MISMATCH,
        )

    if client is SmokeClient.CURSOR_CLI:
        discovery_commands = (
            ["agent", "mcp", "list"],
            ["agent", "mcp", "list-tools", "echovault"],
        )
    else:
        discovery_commands = (["gemini", "extensions", "list"],)
    try:
        discovery = [
            runner.run(
                command,
                timeout=15.0,
                cwd=cwd,
                env=safe_environment,
            )
            for command in discovery_commands
        ]
    except (TimeoutError, subprocess.TimeoutExpired):
        duration = max(0, round((time.monotonic() - started) * 1000))
        return _probe_result(
            client=client,
            observed_version=observed,
            returncode=None,
            duration_ms=duration,
            codes=(EvidenceCode.TIMEOUT,),
            status=SmokeStatus.FAILED,
            reason=ReasonCode.TIMEOUT,
        )
    except OSError:
        duration = max(0, round((time.monotonic() - started) * 1000))
        return _probe_result(
            client=client,
            observed_version=observed,
            returncode=None,
            duration_ms=duration,
            codes=(EvidenceCode.CLIENT_ERROR,),
            status=SmokeStatus.FAILED,
            reason=ReasonCode.CLIENT_ERROR,
        )
    duration = max(0, round((time.monotonic() - started) * 1000))
    if any(result.returncode != 0 for result in discovery):
        return _probe_result(
            client=client,
            observed_version=observed,
            returncode=next(
                result.returncode
                for result in discovery
                if result.returncode != 0
            ),
            duration_ms=duration,
            codes=(EvidenceCode.CLIENT_ERROR,),
            status=SmokeStatus.FAILED,
            reason=ReasonCode.CLIENT_ERROR,
        )
    if client is SmokeClient.CURSOR_CLI:
        server_found = "echovault" in discovery[0].stdout.split()
        tools = set(discovery[1].stdout.split())
        complete = {
            "memory_context",
            "memory_search",
            "memory_details",
            "memory_save",
        }.issubset(tools)
        codes = (
            *((EvidenceCode.MCP_DISCOVERED,) if server_found else ()),
            *((EvidenceCode.FOUR_TOOLS,) if complete else ()),
        )
        healthy = server_found and complete
    else:
        healthy = "echovault" in discovery[0].stdout.split()
        codes = (
            (EvidenceCode.EXTENSION_DISCOVERED,) if healthy else ()
        )
    return _probe_result(
        client=client,
        observed_version=observed,
        returncode=0,
        duration_ms=duration,
        codes=codes or (EvidenceCode.CLIENT_ERROR,),
        status=SmokeStatus.PASSED if healthy else SmokeStatus.FAILED,
        reason=(
            ReasonCode.VERIFIED if healthy else ReasonCode.CLIENT_ERROR
        ),
    )


def run_headless_smoke(
    client: SmokeClient,
    workspace: Path,
    marker_id: str,
    runner: CommandRunner,
    environment: Mapping[str, str],
) -> SmokeEvidence:
    try:
        uuid.UUID(marker_id)
    except (AttributeError, TypeError, ValueError) as error:
        raise EvidenceValidationError("invalid marker UUID") from error
    prerequisite = probe_client(client, workspace, runner, environment)
    command = (
        SmokeCommand.CURSOR_HEADLESS
        if client is SmokeClient.CURSOR_CLI
        else SmokeCommand.GEMINI_HEADLESS
    )
    marker_digest = hashlib.sha256(
        marker_id.encode("ascii")
    ).hexdigest()

    def evidence(
        *,
        returncode: int | None,
        duration_ms: int,
        codes: tuple[EvidenceCode, ...],
        status: SmokeStatus,
        reason: ReasonCode,
    ) -> SmokeEvidence:
        return SmokeEvidence(
            client=client,
            expected_version=prerequisite.expected_version,
            observed_version=prerequisite.observed_version,
            scope=SmokeScope.PROJECT,
            command=command,
            exit_code=returncode,
            duration_ms=duration_ms,
            marker_id=marker_id,
            marker_digest=marker_digest,
            evidence_codes=codes,
            status=status,
            reason_code=reason,
        )

    if prerequisite.status is not SmokeStatus.PASSED:
        return evidence(
            returncode=prerequisite.exit_code,
            duration_ms=prerequisite.duration_ms,
            codes=prerequisite.evidence_codes,
            status=prerequisite.status,
            reason=prerequisite.reason_code,
        )

    cwd = _workspace(workspace)
    safe_environment = build_smoke_environment(environment)
    prompt = (
        f"Retrieve the EchoVault marker {marker_id}; "
        "report whether it is available."
    )
    started = time.monotonic()
    try:
        result = runner.run(
            headless_command(client, prompt),
            timeout=120.0,
            cwd=cwd,
            env=safe_environment,
        )
    except (TimeoutError, subprocess.TimeoutExpired):
        duration = max(0, round((time.monotonic() - started) * 1000))
        return evidence(
            returncode=None,
            duration_ms=duration,
            codes=(EvidenceCode.TIMEOUT,),
            status=SmokeStatus.FAILED,
            reason=ReasonCode.TIMEOUT,
        )
    except OSError:
        duration = max(0, round((time.monotonic() - started) * 1000))
        return evidence(
            returncode=None,
            duration_ms=duration,
            codes=(EvidenceCode.CLIENT_ERROR,),
            status=SmokeStatus.FAILED,
            reason=ReasonCode.CLIENT_ERROR,
        )
    duration = max(0, round((time.monotonic() - started) * 1000))
    if result.returncode != 0:
        return evidence(
            returncode=result.returncode,
            duration_ms=duration,
            codes=(EvidenceCode.CLIENT_ERROR,),
            status=SmokeStatus.FAILED,
            reason=ReasonCode.CLIENT_ERROR,
        )
    if marker_id in result.stdout:
        return evidence(
            returncode=0,
            duration_ms=duration,
            codes=(EvidenceCode.MARKER_FOUND,),
            status=SmokeStatus.PASSED,
            reason=ReasonCode.VERIFIED,
        )
    return evidence(
        returncode=0,
        duration_ms=duration,
        codes=(EvidenceCode.MARKER_MISSED,),
        status=(
            SmokeStatus.OBSERVATIONAL_MISS
            if client is SmokeClient.CURSOR_CLI
            else SmokeStatus.FAILED
        ),
        reason=ReasonCode.MARKER_MISSED,
    )


def write_report(report: SmokeReport, output: Path) -> Path:
    payload = (
        json.dumps(dataclasses.asdict(report), indent=2, sort_keys=True)
        + "\n"
    )
    prepared = prepare_atomic_text(output, payload)
    prepared.replace()
    rows = [
        "# EchoVault client smoke report",
        "",
        f"- EchoVault: {report.echovault_version}",
        f"- Commit: {report.commit}",
        f"- Generated: {report.generated_at}",
        f"- Claim: {report.report_claim.value}",
        "",
        "| Client | Version | Scope | Status | Evidence |",
        "|---|---|---|---|---|",
    ]
    for item in (*report.evidence, *report.manual_evidence):
        rows.append(
            f'| {item.client.value} | '
            f'{item.observed_version or "unavailable"} '
            f'| {item.scope.value} | {item.status.value} '
            f'| {", ".join(code.value for code in item.evidence_codes)} |'
        )
    if isinstance(report, FinalDogfoodReport):
        rows.extend(
            [
                "",
                "| Bound agent | Marker ID | Memory ID | Replay |",
                "|---|---|---|---|",
            ]
        )
        for marker in report.bound_markers:
            rows.append(
                f"| {marker.binding_agent.value} | {marker.marker_id} "
                f"| {marker.memory_id} | verified |"
            )
    summary = output.with_suffix(".md")
    prepared_summary = prepare_atomic_text(
        summary,
        "\n".join(rows) + "\n",
    )
    prepared_summary.replace()
    return output


@click.group()
def smoke_cli() -> None:
    """Create sanitized EchoVault client evidence."""


def _partial_report(output: Path, commit: str) -> SmokeReport:
    if output.exists():
        report = load_partial_report(output)
        if report.commit != commit:
            raise EvidenceValidationError("partial report commit mismatch")
        return report
    return SmokeReport(
        echovault_version=importlib.metadata.version("echovault"),
        commit=commit,
        generated_at=datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        evidence=(),
    )


def append_automated_evidence(
    output: Path,
    commit: str,
    evidence: SmokeEvidence,
) -> Path:
    report = _partial_report(output, commit)
    cell = (evidence.client, evidence.command)
    if cell in {
        (item.client, item.command) for item in report.evidence
    }:
        raise EvidenceValidationError("duplicate automated evidence cell")
    return write_report(
        replace(report, evidence=(*report.evidence, evidence)),
        output,
    )


def append_manual_evidence(
    output: Path,
    commit: str,
    evidence: ManualEvidence,
) -> Path:
    report = _partial_report(output, commit)
    cell = (evidence.client, evidence.command)
    if cell in {
        (item.client, item.command) for item in report.manual_evidence
    }:
        raise EvidenceValidationError("duplicate manual evidence cell")
    return write_report(
        replace(
            report,
            manual_evidence=(*report.manual_evidence, evidence),
        ),
        output,
    )


def _require_pinned_version(
    client: SmokeClient,
    expected_version: str,
) -> None:
    if expected_version != EXPECTED_CLIENT_VERSIONS[client]:
        raise EvidenceValidationError(
            "expected version is not the pinned client version"
        )


@smoke_cli.command("probe")
@click.option(
    "--client",
    type=click.Choice(["cursor-cli", "gemini-cli"]),
    required=True,
)
@click.option(
    "--workspace",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
)
@click.option("--output", type=click.Path(path_type=Path), required=True)
@click.option("--expected-version", required=True)
@click.option("--commit", required=True)
def probe_command(
    client: str,
    workspace: Path,
    output: Path,
    expected_version: str,
    commit: str,
) -> None:
    try:
        selected = SmokeClient(client)
        _require_pinned_version(selected, expected_version)
        evidence = probe_client(
            selected,
            workspace,
            SubprocessRunner(),
            os.environ,
        )
        append_automated_evidence(output, commit, evidence)
    except EvidenceValidationError as error:
        raise click.ClickException(str(error)) from None
    click.echo(f"appended {client} probe evidence")


@smoke_cli.command("headless")
@click.option(
    "--client",
    type=click.Choice(["cursor-cli", "gemini-cli"]),
    required=True,
)
@click.option(
    "--workspace",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
)
@click.option("--output", type=click.Path(path_type=Path), required=True)
@click.option("--expected-version", required=True)
@click.option("--marker-id", required=True)
@click.option("--commit", required=True)
def headless_command_cli(
    client: str,
    workspace: Path,
    output: Path,
    expected_version: str,
    marker_id: str,
    commit: str,
) -> None:
    try:
        selected = SmokeClient(client)
        _require_pinned_version(selected, expected_version)
        evidence = run_headless_smoke(
            selected,
            workspace,
            marker_id,
            SubprocessRunner(),
            os.environ,
        )
        append_automated_evidence(output, commit, evidence)
    except EvidenceValidationError as error:
        raise click.ClickException(str(error)) from None
    click.echo(f"appended {client} headless evidence")


@smoke_cli.command("manual")
@click.option(
    "--client",
    type=click.Choice(["cursor-ide", "cursor-cli"]),
    required=True,
)
@click.option("--output", type=click.Path(path_type=Path), required=True)
@click.option(
    "--manual-command",
    type=click.Choice(
        ["cursor-ide-manual", "cursor-interactive"]
    ),
    required=True,
)
@click.option(
    "--manual-status",
    type=click.Choice([item.value for item in SmokeStatus]),
    required=True,
)
@click.option(
    "--reason-code",
    type=click.Choice([item.value for item in ReasonCode]),
    required=True,
)
@click.option(
    "--evidence-code",
    type=click.Choice([item.value for item in EvidenceCode]),
    multiple=True,
    required=True,
)
@click.option("--expected-version", required=True)
@click.option("--observed-version")
@click.option("--commit", required=True)
def manual_command_cli(
    client: str,
    output: Path,
    manual_command: str,
    manual_status: str,
    reason_code: str,
    evidence_code: tuple[str, ...],
    expected_version: str,
    observed_version: str | None,
    commit: str,
) -> None:
    try:
        selected = SmokeClient(client)
        _require_pinned_version(selected, expected_version)
        evidence = ManualEvidence(
            client=selected,
            expected_version=expected_version,
            observed_version=observed_version,
            scope=SmokeScope.PROJECT,
            command=SmokeCommand(manual_command),
            evidence_codes=tuple(
                EvidenceCode(code) for code in evidence_code
            ),
            status=SmokeStatus(manual_status),
            reason_code=ReasonCode(reason_code),
        )
        append_manual_evidence(output, commit, evidence)
    except (EvidenceValidationError, ValueError) as error:
        message = (
            str(error)
            if isinstance(error, EvidenceValidationError)
            else "invalid manual evidence"
        )
        raise click.ClickException(message) from None
    click.echo(f"appended {client} manual evidence")


@smoke_cli.command("record-bound-marker")
@click.option(
    "--binding-agent",
    type=click.Choice(["cursor", "gemini-cli"]),
    required=True,
)
@click.option("--marker-id", required=True)
@click.option("--operation-id", required=True)
@click.option("--memory-id", required=True)
@click.option("--replay-verified", is_flag=True, required=True)
@click.option("--output", type=click.Path(path_type=Path), required=True)
def record_bound_marker_command(
    binding_agent: str,
    marker_id: str,
    operation_id: str,
    memory_id: str,
    replay_verified: bool,
    output: Path,
) -> None:
    try:
        marker = BoundMarkerEvidence(
            binding_agent=BoundAgent(binding_agent),
            marker_id=marker_id,
            operation_id=operation_id,
            memory_id=memory_id,
            replay_verified=replay_verified,
        )
        write_bound_marker(marker, output)
    except EvidenceValidationError as error:
        raise click.ClickException(str(error)) from None
    click.echo("wrote sanitized bound marker")


@smoke_cli.command("finalize")
@click.option(
    "--partial",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--bound-marker",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    multiple=True,
    required=True,
)
@click.option(
    "--claim",
    type=click.Choice(["not_verified", "client_verified"]),
    required=True,
)
@click.option("--output", type=click.Path(path_type=Path), required=True)
def finalize_command(
    partial: Path,
    bound_marker: tuple[Path, ...],
    claim: str,
    output: Path,
) -> None:
    try:
        finalize_report(
            partial,
            bound_marker,
            output,
            ReportClaim(claim),
        )
    except EvidenceValidationError as error:
        raise click.ClickException(str(error)) from None
    click.echo(f"wrote {claim} final report")


@smoke_cli.command("validate-final")
@click.option(
    "--report",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--require-claim",
    type=click.Choice(["not_verified", "client_verified"]),
)
def validate_final_command(
    report: Path,
    require_claim: str | None,
) -> None:
    required = (
        None if require_claim is None else ReportClaim(require_claim)
    )
    try:
        parsed = validate_final_report(report, required)
        if (
            parsed.report_claim is ReportClaim.CLIENT_VERIFIED
            and required is not ReportClaim.CLIENT_VERIFIED
        ):
            raise EvidenceValidationError(
                "client_verified validation requires "
                "--require-claim client_verified"
            )
    except EvidenceValidationError as error:
        raise click.ClickException(str(error)) from None
    click.echo(f"valid {parsed.report_claim.value} final report")


if __name__ == "__main__":
    smoke_cli()
