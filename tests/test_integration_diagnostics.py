from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from memory.core import MemoryService
from memory.health import doctor, doctor_home
from memory.integrations.process import CommandResult
from tests.integration_helpers import cursor_adapter, project_options, snapshot_tree
from tests.gemini_helpers import (
    RecordingRunner as GeminiRunner,
    gemini_adapter,
    project_direct_options,
)


class RecordingRunner:
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.calls: list[
            tuple[tuple[str, ...], Path | None, dict[str, str] | None]
        ] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        capture_output: bool = True,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        _ = timeout, capture_output
        self.calls.append(
            (tuple(argv), cwd, None if env is None else dict(env))
        )
        return self.result


def install_cursor_fixture(tmp_path: Path) -> tuple[MemoryService, Path]:
    memory_home = tmp_path / "memory-home"
    project = tmp_path / "repo"
    project.mkdir()
    cursor_adapter().setup(project_options(project))
    return MemoryService(str(memory_home)), project


def test_cursor_doctor_is_read_only(tmp_path: Path) -> None:
    service, project = install_cursor_fixture(tmp_path)
    fake_runner = RecordingRunner(
        CommandResult(0, "echovault: connected\n", "")
    )
    before = snapshot_tree(tmp_path)
    try:
        report = doctor(
            service,
            agent="cursor",
            project_root=project,
            runner=fake_runner,
        )
        assert snapshot_tree(tmp_path) == before
    finally:
        service.close()
    codes = {
        finding["code"] for finding in report["integration_findings"]
    }
    assert codes >= {
        "cursor.scope",
        "cursor.manifest",
        "cursor.mcp",
        "cursor.rule",
        "cursor.skill",
        "cursor.context-policy",
        "cursor.cloud-boundary",
    }
    assert len(fake_runner.calls) == 2
    assert all(call[1] == project.resolve() for call in fake_runner.calls)
    assert fake_runner.calls[0][0] == ("agent", "mcp", "list")
    assert fake_runner.calls[1][0] == (
        "agent",
        "mcp",
        "list-tools",
        "echovault",
    )


def test_cursor_doctor_reports_effective_disabled_policy(tmp_path: Path) -> None:
    service, project = install_cursor_fixture(tmp_path)
    service.config.context.agent_modes["cursor"] = "off"
    try:
        report = doctor(
            service,
            agent="cursor",
            project_root=project,
            runner=RecordingRunner(CommandResult(127, "", "not installed")),
        )
    finally:
        service.close()
    policy = next(
        item
        for item in report["integration_findings"]
        if item["code"] == "cursor.context-policy"
    )
    assert policy["mode"] == "off"
    assert policy["source"] == "agent:cursor"


def test_cursor_doctor_reports_project_shadowing_global_plugin(
    tmp_path: Path,
    fake_memory: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "repo"
    home.mkdir()
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cursor_adapter().setup(project_options(project))
    from tests.integration_helpers import user_options

    cursor_adapter().setup(
        user_options(home / ".cursor", command=str(fake_memory))
    )
    service = MemoryService(str(tmp_path / "memory-home"))
    try:
        report = doctor(
            service,
            agent="cursor",
            project_root=project,
            runner=RecordingRunner(CommandResult(0, "echovault", "")),
        )
    finally:
        service.close()
    shadow = next(
        item
        for item in report["integration_findings"]
        if item["code"] == "cursor.shadowing"
    )
    assert shadow["status"] == "shadowed"


def test_cursor_doctor_home_without_database_remains_read_only(
    tmp_path: Path,
) -> None:
    memory_home = tmp_path / "memory-home"
    project = tmp_path / "repo"
    memory_home.mkdir()
    project.mkdir()
    cursor_adapter().setup(project_options(project))
    runner = RecordingRunner(CommandResult(127, "", "agent unavailable"))
    before = snapshot_tree(tmp_path)
    report = doctor_home(
        memory_home,
        agent="cursor",
        project_root=project,
        runner=runner,
    )
    assert snapshot_tree(tmp_path) == before
    assert report["integration_findings"]
    assert not (memory_home / "index.db").exists()


def test_cursor_doctor_uses_legacy_cli_alias_when_agent_command_is_absent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import memory.integrations.diagnostics as diagnostics

    project = tmp_path / "repo"
    project.mkdir()
    cursor_adapter().setup(project_options(project))
    calls: list[tuple[str, ...]] = []

    def fake_which(command: str) -> str | None:
        if command == "cursor-agent":
            return "/opt/cursor-agent"
        if command == "memory":
            return "/opt/memory"
        return None

    def fake_run(self, argv, **kwargs):
        calls.append(tuple(argv))
        return CommandResult(0, "echovault", "")

    monkeypatch.setattr(diagnostics.shutil, "which", fake_which)
    monkeypatch.setattr(diagnostics.SubprocessRunner, "run", fake_run)
    service = MemoryService(str(tmp_path / "memory-home"))
    try:
        doctor(service, agent="cursor", project_root=project)
    finally:
        service.close()
    assert calls[0] == ("cursor-agent", "mcp", "list")
    assert calls[1] == (
        "cursor-agent",
        "mcp",
        "list-tools",
        "echovault",
    )


def test_gemini_doctor_is_read_only_and_reports_full_contract(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    memory_home = tmp_path / "memory-home"
    project = tmp_path / "repo"
    home.mkdir()
    project.mkdir()
    (project / "package.json").write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    runner = GeminiRunner(client_version="0.50.1")
    gemini_adapter(runner).setup(project_direct_options(project))
    service = MemoryService(str(memory_home))
    runner.argv.clear()
    runner.calls.clear()
    before = snapshot_tree(tmp_path)
    try:
        report = doctor(
            service,
            agent="gemini-cli",
            project_root=project,
            runner=runner,
        )
        assert snapshot_tree(tmp_path) == before
    finally:
        service.close()

    findings = {
        item["code"]: item for item in report["integration_findings"]
    }
    assert set(findings) >= {
        "gemini.project",
        "gemini.version",
        "gemini.state",
        "gemini.native-enabled",
        "gemini.manifest",
        "gemini.mcp",
        "gemini.context",
        "gemini.skill",
        "gemini.hook",
        "gemini.tools",
        "gemini.project-scope",
        "gemini.claims",
        "gemini.context-policy",
        "gemini.query-privacy",
        "gemini.mcp.precedence",
    }
    assert findings["gemini.tools"]["expected_tools"] == [
        "memory_context",
        "memory_search",
        "memory_details",
        "memory_save",
    ]
    assert findings["gemini.project-scope"]["project_key"]
    assert findings["gemini.context-policy"]["mode"] == "auto"
    assert not (memory_home / "hook-events").exists()
    assert all(
        call in (
            ["gemini", "--version"],
            ["gemini", "extensions", "list"],
        )
        for call in runner.argv
    )


def test_gemini_doctor_reports_disabled_hook_policy_without_claims(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    memory_home = tmp_path / "memory-home"
    project = tmp_path / "repo"
    home.mkdir()
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    runner = GeminiRunner(client_version="0.50.1")
    gemini_adapter(runner).setup(project_direct_options(project))
    service = MemoryService(str(memory_home))
    service.config.context.agent_modes["gemini-cli"] = "off"
    try:
        report = doctor(
            service,
            agent="gemini-cli",
            project_root=project,
            runner=runner,
        )
    finally:
        service.close()
    policy = next(
        item
        for item in report["integration_findings"]
        if item["code"] == "gemini.context-policy"
    )
    assert policy["status"] == "degraded"
    assert policy["mode"] == "off"
    assert policy["source"] == "agent:gemini-cli"
    assert not (memory_home / "hook-events").exists()
