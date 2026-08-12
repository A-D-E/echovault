from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from click.testing import CliRunner
import pytest

from memory.integrations.process import CommandResult
from scripts.smoke_clients import (
    BoundAgent,
    BoundMarkerEvidence,
    EvidenceCode,
    EvidenceValidationError,
    FinalDogfoodReport,
    ManualEvidence,
    ReasonCode,
    ReportClaim,
    SmokeClient,
    SmokeCommand,
    SmokeEvidence,
    SmokeReport,
    SmokeScope,
    SmokeStatus,
    finalize_report,
    headless_command,
    load_bound_marker,
    load_final_report,
    load_partial_report,
    probe_client,
    smoke_cli,
    write_bound_marker,
    write_report,
)


@dataclass
class FakeRunner:
    outcomes: dict[tuple[str, ...], CommandResult] = field(
        default_factory=dict
    )
    calls: list[dict[str, object]] = field(default_factory=list)

    def return_for(
        self,
        argv: list[str],
        *,
        returncode: int,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self.outcomes[tuple(argv)] = CommandResult(
            returncode,
            stdout,
            stderr,
        )

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        capture_output: bool = True,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        self.calls.append(
            {
                "argv": tuple(argv),
                "cwd": cwd,
                "env": None if env is None else dict(env),
            }
        )
        return self.outcomes[tuple(argv)]


def test_cursor_headless_command_uses_current_agent_entrypoint() -> None:
    command = headless_command(
        SmokeClient.CURSOR_CLI,
        "retrieve the seeded marker",
    )
    assert command == [
        "agent",
        "-p",
        "--output-format",
        "json",
        "retrieve the seeded marker",
    ]


def test_gemini_headless_command_is_noninteractive_json() -> None:
    command = headless_command(
        SmokeClient.GEMINI_CLI,
        "retrieve the seeded marker",
    )
    assert command == [
        "gemini",
        "-p",
        "retrieve the seeded marker",
        "--output-format",
        "json",
    ]


def test_unavailable_or_wrong_version_is_not_verified(
    tmp_path: Path,
) -> None:
    fake_runner = FakeRunner()
    fake_runner.return_for(
        ["agent", "--version"],
        returncode=127,
        stderr="missing",
    )
    evidence = probe_client(
        SmokeClient.CURSOR_CLI,
        tmp_path.resolve(),
        fake_runner,
        {"PATH": "/controlled"},
    )
    assert evidence.status == SmokeStatus.NOT_VERIFIED
    assert evidence.expected_version == "2026.07.09-a3815c0"
    assert evidence.marker_id is None
    assert evidence.marker_digest is None


def test_report_does_not_retain_prompt_response_or_environment(
    tmp_path: Path,
) -> None:
    forbidden_fields = {
        "prompt",
        "response",
        "stdout",
        "stderr",
        "environment",
    }
    assert forbidden_fields.isdisjoint(
        field.name for field in dataclasses.fields(SmokeEvidence)
    )
    marker_id = "11111111-1111-4111-8111-111111111111"
    private_prompt = "private prompt secret API_KEY=abc"
    private_response = "private response secret-value"
    prompt_digest = hashlib.sha256(private_prompt.encode()).hexdigest()
    report = SmokeReport(
        echovault_version="0.6.0",
        commit="abc1234",
        generated_at="2026-07-14T12:00:00Z",
        evidence=(
            SmokeEvidence(
                client=SmokeClient.CURSOR_CLI,
                expected_version="2026.07.09-a3815c0",
                observed_version=None,
                scope=SmokeScope.PROJECT,
                command=SmokeCommand.CURSOR_HEADLESS,
                exit_code=127,
                duration_ms=10,
                marker_id=marker_id,
                marker_digest=hashlib.sha256(
                    marker_id.encode("ascii")
                ).hexdigest(),
                evidence_codes=(EvidenceCode.BINARY_MISSING,),
                status=SmokeStatus.NOT_VERIFIED,
                reason_code=ReasonCode.BINARY_MISSING,
            ),
        ),
    )
    path = write_report(report, tmp_path / "report.json")
    raw = path.read_text()
    assert private_prompt not in raw
    assert private_response not in raw
    assert prompt_digest not in raw
    assert "API_KEY" not in raw
    assert "secret" not in raw


def test_adversarial_client_output_and_its_prompt_digest_are_discarded(
    tmp_path: Path,
) -> None:
    prompt = "do not retain prompt-Q7"
    response = "model response with SECRET_TOKEN=top-secret"
    fake_runner = FakeRunner()
    fake_runner.return_for(
        ["agent", "--version"],
        returncode=0,
        stdout=f"2026.07.09-a3815c0 {prompt}",
        stderr=response,
    )
    evidence = probe_client(
        SmokeClient.CURSOR_CLI,
        tmp_path.resolve(),
        fake_runner,
        {"PATH": "/controlled", "API_KEY": "must-be-filtered-before-run"},
    )
    assert evidence.observed_version is None
    assert evidence.reason_code is ReasonCode.VERSION_MISMATCH
    assert EvidenceCode.VERSION_MISMATCH in evidence.evidence_codes
    report = SmokeReport(
        echovault_version="0.6.0",
        commit="abc1234",
        generated_at="2026-07-14T12:00:00Z",
        evidence=(evidence,),
    )
    raw = write_report(report, tmp_path / "adversarial.json").read_text()
    for forbidden in (
        prompt,
        response,
        "top-secret",
        hashlib.sha256(prompt.encode()).hexdigest(),
    ):
        assert forbidden not in raw


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "GITHUB_TOKEN",
        "MY_SECRET",
        "DB_CREDENTIAL",
        "PASSWORD",
        "GEMINI_API_KEY",
        "UNRELATED_VALUE",
    ],
)
def test_probe_forwards_only_allowlisted_environment_keys(
    tmp_path: Path,
    forbidden_key: str,
) -> None:
    runner = FakeRunner()
    runner.return_for(
        ["agent", "--version"],
        returncode=0,
        stdout="2026.07.09-a3815c0\n",
    )
    runner.return_for(
        ["agent", "mcp", "list"],
        returncode=0,
        stdout="echovault\n",
    )
    runner.return_for(
        ["agent", "mcp", "list-tools", "echovault"],
        returncode=0,
        stdout=(
            "memory_context memory_search memory_details memory_save\n"
        ),
    )
    probe_client(
        SmokeClient.CURSOR_CLI,
        tmp_path.resolve(),
        runner,
        {
            "PATH": "/controlled",
            "HOME": "/home/test",
            "LANG": "C.UTF-8",
            "TMP": "/tmp/test",
            forbidden_key: "private-value",
        },
    )
    assert runner.calls
    for call in runner.calls:
        assert call["cwd"] == tmp_path.resolve()
        assert call["env"] == {
            "PATH": "/controlled",
            "HOME": "/home/test",
            "LANG": "C.UTF-8",
            "TMP": "/tmp/test",
        }


def test_cursor_ide_and_interactive_cli_evidence_use_distinct_pins_and_commands(
    tmp_path: Path,
) -> None:
    report = SmokeReport(
        echovault_version="0.6.0",
        commit="abc1234",
        generated_at="2026-07-14T12:00:00Z",
        evidence=(),
        manual_evidence=(
            ManualEvidence(
                client=SmokeClient.CURSOR_IDE,
                expected_version="3.11.19",
                observed_version="3.11.19",
                scope=SmokeScope.PROJECT,
                command=SmokeCommand.CURSOR_IDE_MANUAL,
                evidence_codes=(EvidenceCode.MARKER_FOUND,),
                status=SmokeStatus.PASSED,
                reason_code=ReasonCode.VERIFIED,
            ),
            ManualEvidence(
                client=SmokeClient.CURSOR_CLI,
                expected_version="2026.07.09-a3815c0",
                observed_version="2026.07.09-a3815c0",
                scope=SmokeScope.PROJECT,
                command=SmokeCommand.CURSOR_INTERACTIVE,
                evidence_codes=(EvidenceCode.MARKER_FOUND,),
                status=SmokeStatus.PASSED,
                reason_code=ReasonCode.VERIFIED,
            ),
        ),
    )
    payload = json.loads(
        write_report(report, tmp_path / "manual.json").read_text()
    )
    assert payload["manual_evidence"][0]["expected_version"] == "3.11.19"
    assert (
        payload["manual_evidence"][1]["expected_version"]
        == "2026.07.09-a3815c0"
    )


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("commit", "not-a-git-id"),
        ("generated_at", "not-rfc3339"),
    ],
)
def test_report_rejects_invalid_controlled_metadata(
    field_name: str,
    invalid_value: str,
) -> None:
    valid = SmokeReport(
        echovault_version="0.6.0",
        commit="abc1234",
        generated_at="2026-07-14T12:00:00Z",
        evidence=(),
    )
    with pytest.raises(EvidenceValidationError):
        replace(valid, **{field_name: invalid_value})


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("expected_version", "private-version-text"),
        ("observed_version", "0.50.0 secret"),
        ("marker_id", "not-a-uuid"),
        ("marker_digest", "not-a-sha256"),
    ],
)
def test_evidence_rejects_invalid_version_or_marker_metadata(
    field_name: str,
    invalid_value: str,
) -> None:
    valid = SmokeEvidence(
        client=SmokeClient.GEMINI_CLI,
        expected_version="0.50.0",
        observed_version="0.50.0",
        scope=SmokeScope.PROJECT,
        command=SmokeCommand.GEMINI_HEADLESS,
        exit_code=0,
        duration_ms=10,
        marker_id="11111111-1111-4111-8111-111111111111",
        marker_digest=hashlib.sha256(
            b"11111111-1111-4111-8111-111111111111"
        ).hexdigest(),
        evidence_codes=(EvidenceCode.MARKER_FOUND,),
        status=SmokeStatus.PASSED,
        reason_code=ReasonCode.VERIFIED,
    )
    with pytest.raises(EvidenceValidationError):
        replace(valid, **{field_name: invalid_value})


def cross_agent_markers() -> tuple[
    BoundMarkerEvidence,
    BoundMarkerEvidence,
]:
    return (
        BoundMarkerEvidence(
            binding_agent=BoundAgent.CURSOR,
            marker_id="11111111-1111-4111-8111-111111111111",
            operation_id="22222222-2222-4222-8222-222222222222",
            memory_id="33333333-3333-4333-8333-333333333333",
            replay_verified=True,
        ),
        BoundMarkerEvidence(
            binding_agent=BoundAgent.GEMINI_CLI,
            marker_id="44444444-4444-4444-8444-444444444444",
            operation_id="55555555-5555-4555-8555-555555555555",
            memory_id="66666666-6666-4666-8666-666666666666",
            replay_verified=True,
        ),
    )


def final_report(
    markers: tuple[BoundMarkerEvidence, ...],
    *,
    claim: ReportClaim = ReportClaim.NOT_VERIFIED,
    evidence: tuple[SmokeEvidence, ...] = (),
    manual_evidence: tuple[ManualEvidence, ...] = (),
) -> FinalDogfoodReport:
    return FinalDogfoodReport(
        echovault_version="0.6.0",
        commit="abc1234",
        generated_at="2026-07-14T12:00:00Z",
        evidence=evidence,
        manual_evidence=manual_evidence,
        report_claim=claim,
        bound_markers=markers,
    )


def verified_partial_report() -> SmokeReport:
    def automated(
        client: SmokeClient,
        command: SmokeCommand,
        version: str,
        marker_id: str | None = None,
    ) -> SmokeEvidence:
        return SmokeEvidence(
            client=client,
            expected_version=version,
            observed_version=version,
            scope=SmokeScope.PROJECT,
            command=command,
            exit_code=0,
            duration_ms=10,
            marker_id=marker_id,
            marker_digest=(
                None
                if marker_id is None
                else hashlib.sha256(marker_id.encode("ascii")).hexdigest()
            ),
            evidence_codes=(EvidenceCode.MARKER_FOUND,),
            status=SmokeStatus.PASSED,
            reason_code=ReasonCode.VERIFIED,
        )

    def manual(
        client: SmokeClient,
        command: SmokeCommand,
        version: str,
    ) -> ManualEvidence:
        return ManualEvidence(
            client=client,
            expected_version=version,
            observed_version=version,
            scope=SmokeScope.PROJECT,
            command=command,
            evidence_codes=(EvidenceCode.MARKER_FOUND,),
            status=SmokeStatus.PASSED,
            reason_code=ReasonCode.VERIFIED,
        )

    return SmokeReport(
        echovault_version="0.6.0",
        commit="abc1234",
        generated_at="2026-07-14T12:00:00Z",
        evidence=(
            automated(
                SmokeClient.CURSOR_CLI,
                SmokeCommand.CURSOR_PROBE,
                "2026.07.09-a3815c0",
            ),
            automated(
                SmokeClient.CURSOR_CLI,
                SmokeCommand.CURSOR_HEADLESS,
                "2026.07.09-a3815c0",
                "77777777-7777-4777-8777-777777777777",
            ),
            automated(
                SmokeClient.GEMINI_CLI,
                SmokeCommand.GEMINI_PROBE,
                "0.50.0",
            ),
            automated(
                SmokeClient.GEMINI_CLI,
                SmokeCommand.GEMINI_HEADLESS,
                "0.50.0",
                "88888888-8888-4888-8888-888888888888",
            ),
        ),
        manual_evidence=(
            manual(
                SmokeClient.CURSOR_IDE,
                SmokeCommand.CURSOR_IDE_MANUAL,
                "3.11.19",
            ),
            manual(
                SmokeClient.CURSOR_CLI,
                SmokeCommand.CURSOR_INTERACTIVE,
                "2026.07.09-a3815c0",
            ),
        ),
        report_claim=ReportClaim.NOT_VERIFIED,
    )


def test_partial_probe_report_allows_no_bound_markers() -> None:
    partial = SmokeReport(
        echovault_version="0.6.0",
        commit="abc1234",
        generated_at="2026-07-14T12:00:00Z",
        evidence=(),
    )
    assert partial.evidence == ()
    assert partial.report_claim is ReportClaim.NOT_VERIFIED


def test_final_report_requires_exact_cursor_and_gemini_bound_markers(
    tmp_path: Path,
) -> None:
    report = final_report(cross_agent_markers())
    payload = json.loads(
        write_report(report, tmp_path / "final.json").read_text()
    )
    assert [item["binding_agent"] for item in payload["bound_markers"]] == [
        "cursor",
        "gemini-cli",
    ]
    assert all(item["replay_verified"] for item in payload["bound_markers"])


def test_final_report_rejects_missing_duplicate_or_unreplayed_binding() -> None:
    cursor, gemini = cross_agent_markers()
    with pytest.raises(EvidenceValidationError):
        final_report((cursor,))
    with pytest.raises(EvidenceValidationError):
        final_report(
            (
                cursor,
                replace(gemini, binding_agent=BoundAgent.CURSOR),
            )
        )
    with pytest.raises(EvidenceValidationError):
        final_report((cursor, replace(gemini, replay_verified=False)))


@pytest.mark.parametrize("field_name", ["marker_id", "operation_id", "memory_id"])
def test_final_report_requires_distinct_cross_agent_ids(
    field_name: str,
) -> None:
    cursor, gemini = cross_agent_markers()
    duplicate = replace(
        gemini,
        **{field_name: getattr(cursor, field_name)},
    )
    with pytest.raises(EvidenceValidationError):
        final_report((cursor, duplicate))


def test_finalize_and_validate_final_cli_round_trip(tmp_path: Path) -> None:
    partial_path = write_report(
        verified_partial_report(),
        tmp_path / "partial-smoke.json",
    )
    marker_paths = []
    for marker in cross_agent_markers():
        marker_path = (
            tmp_path / f"{marker.binding_agent.value}-bound-marker.json"
        )
        write_bound_marker(marker, marker_path)
        marker_paths.append(marker_path)

    runner = CliRunner()
    result = runner.invoke(
        smoke_cli,
        [
            "finalize",
            "--partial",
            str(partial_path),
            "--bound-marker",
            str(marker_paths[0]),
            "--bound-marker",
            str(marker_paths[1]),
            "--claim",
            "client_verified",
            "--output",
            str(tmp_path / "client-verified-final.json"),
        ],
    )
    assert result.exit_code == 0, result.output

    validation = runner.invoke(
        smoke_cli,
        [
            "validate-final",
            "--report",
            str(tmp_path / "client-verified-final.json"),
            "--require-claim",
            "client_verified",
        ],
    )
    assert validation.exit_code == 0, validation.output
    assert validation.output.strip() == "valid client_verified final report"


def test_smoke_cli_exposes_every_shell_gate_subcommand() -> None:
    assert set(smoke_cli.commands) == {
        "probe",
        "headless",
        "manual",
        "record-bound-marker",
        "finalize",
        "validate-final",
    }


def test_smoke_script_file_entrypoint_is_executable() -> None:
    script = Path(__file__).parents[1] / "scripts" / "smoke_clients.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    for command in (
        "probe",
        "headless",
        "manual",
        "finalize",
        "validate-final",
    ):
        assert command in result.stdout


@pytest.mark.parametrize("marker_count", [0, 1, 3])
def test_finalize_requires_exactly_two_marker_files(
    tmp_path: Path,
    marker_count: int,
) -> None:
    partial_path = write_report(
        verified_partial_report(),
        tmp_path / "partial-smoke.json",
    )
    marker_paths = []
    cursor, gemini = cross_agent_markers()
    for index, marker in enumerate((cursor, gemini, cursor)[:marker_count]):
        path = tmp_path / f"marker-{index}.json"
        write_bound_marker(marker, path)
        marker_paths.append(path)
    with pytest.raises(EvidenceValidationError, match="exactly two"):
        finalize_report(
            partial_path,
            tuple(marker_paths),
            tmp_path / "final.json",
            ReportClaim.CLIENT_VERIFIED,
        )


def test_client_verified_claim_rejects_not_verified_client_evidence(
    tmp_path: Path,
) -> None:
    baseline = verified_partial_report()
    partial = replace(
        baseline,
        evidence=(
            replace(
                baseline.evidence[0],
                observed_version=None,
                status=SmokeStatus.NOT_VERIFIED,
                reason_code=ReasonCode.AUTH_MISSING,
            ),
            *baseline.evidence[1:],
        ),
    )
    partial_path = write_report(partial, tmp_path / "partial-smoke.json")
    marker_paths = []
    for marker in cross_agent_markers():
        path = tmp_path / f"{marker.binding_agent.value}.json"
        write_bound_marker(marker, path)
        marker_paths.append(path)
    with pytest.raises(EvidenceValidationError, match="client_verified"):
        finalize_report(
            partial_path,
            tuple(marker_paths),
            tmp_path / "final.json",
            ReportClaim.CLIENT_VERIFIED,
        )


def test_client_verified_claim_rejects_inconsistent_status_reason() -> None:
    partial = verified_partial_report()
    inconsistent = replace(
        partial.manual_evidence[0],
        reason_code=ReasonCode.AUTH_MISSING,
        evidence_codes=(EvidenceCode.AUTH_MISSING,),
    )
    with pytest.raises(EvidenceValidationError, match="client_verified"):
        final_report(
            cross_agent_markers(),
            claim=ReportClaim.CLIENT_VERIFIED,
            evidence=partial.evidence,
            manual_evidence=(
                inconsistent,
                *partial.manual_evidence[1:],
            ),
        )


@pytest.mark.parametrize(
    ("target", "required_field"),
    [
        ("partial", "manual_evidence"),
        ("evidence", "status"),
        ("manual", "status"),
        ("marker", "memory_id"),
        ("final", "bound_markers"),
    ],
)
@pytest.mark.parametrize("mutation", ["missing", "unknown"])
def test_json_loaders_fail_closed_on_missing_or_unknown_fields(
    tmp_path: Path,
    target: str,
    required_field: str,
    mutation: str,
) -> None:
    partial_path = write_report(
        verified_partial_report(),
        tmp_path / "partial-smoke.json",
    )
    marker_paths = []
    for marker in cross_agent_markers():
        path = tmp_path / f"{marker.binding_agent.value}.json"
        write_bound_marker(marker, path)
        marker_paths.append(path)
    final_path = finalize_report(
        partial_path,
        tuple(marker_paths),
        tmp_path / "client-verified-final.json",
        ReportClaim.CLIENT_VERIFIED,
    )

    if target == "marker":
        path = marker_paths[0]
        payload = json.loads(path.read_text())
        container = payload
        loader = load_bound_marker
    elif target == "final":
        path = final_path
        payload = json.loads(path.read_text())
        container = payload
        loader = load_final_report
    else:
        path = partial_path
        payload = json.loads(path.read_text())
        container = {
            "partial": payload,
            "evidence": payload["evidence"][0],
            "manual": payload["manual_evidence"][0],
        }[target]
        loader = load_partial_report

    if mutation == "missing":
        container.pop(required_field)
    else:
        container["SECRET_PROMPT_VALUE"] = "must never be echoed"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EvidenceValidationError) as exc_info:
        loader(path)
    assert "must never be echoed" not in str(exc_info.value)
    assert "SECRET_PROMPT_VALUE" not in str(exc_info.value)
