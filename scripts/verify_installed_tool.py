from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

import memory
from memory.integrations.process import is_executable_regular_file

try:
    from scripts.validate_cursor_plugin import validate_cursor_plugin
except ModuleNotFoundError:  # Direct file execution from scripts/.
    from validate_cursor_plugin import validate_cursor_plugin


@dataclass(frozen=True)
class VerificationCheck:
    name: str
    status: str = "passed"


@dataclass(frozen=True)
class VerificationReport:
    checks: tuple[VerificationCheck, ...]

    @property
    def passed(self) -> bool:
        return all(item.status == "passed" for item in self.checks)


def resolve_memory_executable(path: Path) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as error:
        raise ValueError("memory must be an executable regular file") from error
    if not is_executable_regular_file(resolved):
        raise ValueError("memory must be an executable regular file")
    return resolved


def _snapshot(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _run(
    executable: Path,
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(executable), *args],
        cwd=cwd,
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def _require_success(result: subprocess.CompletedProcess[str], name: str) -> None:
    if result.returncode != 0:
        raise RuntimeError(f"installed-tool check failed: {name}")


@asynccontextmanager
async def _stdio(
    executable: Path,
    args: list[str],
    env: dict[str, str],
    cwd: Path,
):
    parameters = StdioServerParameters(
        command=str(executable),
        args=args,
        env=env,
        cwd=cwd,
    )
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            yield session


def _payload(result) -> object:
    if result.isError or len(result.content) != 1:
        raise RuntimeError("installed MCP returned an error")
    content = result.content[0]
    if not isinstance(content, TextContent):
        raise RuntimeError("installed MCP returned non-text content")
    return json.loads(content.text)


async def _verify_mcp(
    executable: Path,
    env: dict[str, str],
    project: Path,
) -> str:
    operation_id = str(uuid.uuid4())
    marker_id = str(uuid.uuid4())
    arguments = {
        "title": f"Installed wheel marker {marker_id}",
        "what": f"Deterministic installed-wheel marker {marker_id}",
        "details": f"Installed-wheel MCP detail marker {marker_id}",
        "idempotency_key": operation_id,
    }
    async with _stdio(
        executable,
        [
            "mcp",
            "--agent",
            "cursor",
            "--project-root",
            str(project),
        ],
        env,
        project,
    ) as session:
        tools = {tool.name for tool in (await session.list_tools()).tools}
        if tools != {
            "memory_context",
            "memory_search",
            "memory_details",
            "memory_save",
        }:
            raise RuntimeError("installed bound MCP tool inventory differs")
        first = _payload(await session.call_tool("memory_save", arguments))
        second = _payload(await session.call_tool("memory_save", arguments))
    if not isinstance(first, dict) or not isinstance(second, dict):
        raise RuntimeError("installed bound save payload is invalid")
    if first.get("action") not in {"created", "updated"}:
        raise RuntimeError("installed bound save was not created")
    if second.get("action") != "replayed":
        raise RuntimeError("installed bound save did not replay")
    memory_id = first.get("id")
    if not isinstance(memory_id, str) or second.get("id") != memory_id:
        raise RuntimeError("installed bound replay changed memory identity")

    async with _stdio(executable, ["mcp"], env, project) as session:
        tools = {tool.name for tool in (await session.list_tools()).tools}
        if tools != {
            "memory_context",
            "memory_search",
            "memory_details",
            "memory_save",
        }:
            raise RuntimeError("installed unbound MCP tool inventory differs")
        detail = _payload(
            await session.call_tool(
                "memory_details",
                {"memory_id": memory_id},
            )
        )
    if not isinstance(detail, dict) or marker_id not in str(detail.get("body")):
        raise RuntimeError("unbound MCP could not read the bound marker")
    return memory_id


def _controlled_environment(root: Path) -> tuple[dict[str, str], Path, Path]:
    home = root / "home"
    command_bin = root / "command-bin"
    temporary = root / "tmp"
    for path in (home, command_bin, temporary):
        path.mkdir(parents=True)
    retained = {
        key: os.environ[key]
        for key in ("SystemRoot", "WINDIR", "PATHEXT", "COMSPEC")
        if key in os.environ
    }
    env = {
        **retained,
        "PATH": str(command_bin),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "MEMORY_HOME": str(home / ".memory"),
        "PYTHONPATH": "",
        "TMP": str(temporary),
        "TEMP": str(temporary),
        "TMPDIR": str(temporary),
        "LANG": "C.UTF-8",
    }
    return env, home, command_bin


def _verify_in_root(executable: Path, root: Path) -> VerificationReport:
    checks: list[VerificationCheck] = []
    env, home, command_bin = _controlled_environment(root)
    project = (root / "workspace/repo").resolve()
    project.mkdir(parents=True)
    (project / ".git").mkdir()
    (project / "package.json").write_text("{}", encoding="utf-8")

    checkout = Path.cwd().resolve()
    package_root = Path(memory.__file__).resolve().parent
    source_package = checkout / "src/memory"
    if package_root == source_package or package_root.is_relative_to(
        source_package
    ):
        raise RuntimeError("memory import resolved from the source checkout")
    if project.is_relative_to(checkout) or project.is_relative_to(package_root):
        raise RuntimeError("isolated project is inside a forbidden root")
    if env["PYTHONPATH"] != "":
        raise RuntimeError("isolated environment retained PYTHONPATH")
    for name in ("agent", "cursor-agent", "gemini"):
        if shutil.which(name, path=str(command_bin)) is not None:
            raise RuntimeError("isolated PATH exposes an agent client")
    checks.append(VerificationCheck("isolated-wheel-import"))

    result = _run(executable, ["--help"], cwd=project, env=env)
    _require_success(result, "help")
    checks.append(VerificationCheck("memory-help"))

    cursor_home = home / ".cursor"
    cursor_args = [
        "setup",
        "cursor",
        "--config-dir",
        str(cursor_home),
        "--command",
        str(executable),
    ]
    _require_success(
        _run(executable, cursor_args, cwd=project, env=env),
        "cursor-global-setup",
    )
    plugin = cursor_home / "plugins/local/echovault"
    if validate_cursor_plugin(plugin):
        raise RuntimeError("installed Cursor plugin did not validate")
    cursor_before = _snapshot(plugin)
    _require_success(
        _run(executable, cursor_args, cwd=project, env=env),
        "cursor-global-repeat",
    )
    if _snapshot(plugin) != cursor_before:
        raise RuntimeError("installed Cursor plugin repeat was not byte stable")
    _require_success(
        _run(
            executable,
            ["doctor", "--agent", "cursor", "--project-root", str(project)],
            cwd=project,
            env=env,
        ),
        "cursor-doctor",
    )
    (plugin / "mine.txt").write_text("preserve", encoding="utf-8")
    checks.append(VerificationCheck("cursor-global"))

    cursor_project = project / ".cursor"
    (cursor_project / "rules").mkdir(parents=True)
    (cursor_project / "mcp.json").write_text(
        json.dumps(
            {
                "theme": "mine",
                "mcpServers": {
                    "other": {"command": "other", "args": []}
                },
            }
        ),
        encoding="utf-8",
    )
    (cursor_project / "rules/mine.mdc").write_text(
        "mine",
        encoding="utf-8",
    )
    _require_success(
        _run(
            executable,
            ["setup", "cursor", "--project", "--command", str(executable)],
            cwd=project,
            env=env,
        ),
        "cursor-project-setup",
    )
    _require_success(
        _run(
            executable,
            ["uninstall", "cursor", "--project"],
            cwd=project,
            env=env,
        ),
        "cursor-project-uninstall",
    )
    cursor_remaining = json.loads(
        (cursor_project / "mcp.json").read_text(encoding="utf-8")
    )
    if cursor_remaining.get("theme") != "mine" or not (
        cursor_project / "rules/mine.mdc"
    ).is_file():
        raise RuntimeError("Cursor project uninstall removed unrelated state")
    checks.append(VerificationCheck("cursor-project"))

    gemini_root = project / ".gemini"
    gemini_root.mkdir()
    (gemini_root / "settings.json").write_text(
        json.dumps({"theme": "mine"}),
        encoding="utf-8",
    )
    (project / "GEMINI.md").write_text("# Mine\n", encoding="utf-8")
    gemini_args = [
        "setup",
        "gemini",
        "--project",
        "--command",
        str(executable),
    ]
    _require_success(
        _run(executable, gemini_args, cwd=project, env=env),
        "gemini-project-setup",
    )
    gemini_before = _snapshot(project)
    _require_success(
        _run(executable, gemini_args, cwd=project, env=env),
        "gemini-project-repeat",
    )
    if _snapshot(project) != gemini_before:
        raise RuntimeError("Gemini project repeat was not byte stable")
    _require_success(
        _run(
            executable,
            [
                "doctor",
                "--agent",
                "gemini-cli",
                "--project-root",
                str(project),
            ],
            cwd=project,
            env=env,
        ),
        "gemini-doctor",
    )
    checks.append(VerificationCheck("gemini-project"))

    retrieval_nonce = f"QUERY-{uuid.uuid4()}"
    transcript_sentinel = f"TRANSCRIPT-{uuid.uuid4()}"
    hook_event = {
        "session_id": "installed-wheel-session",
        "cwd": str(project),
        "hook_event_name": "BeforeAgent",
        "timestamp": "2026-07-14T12:00:00Z",
        "prompt": retrieval_nonce,
        "transcript_path": transcript_sentinel,
    }
    hook = _run(
        executable,
        ["hook", "gemini", "before-agent"],
        cwd=project,
        env=env,
        input_text=json.dumps(hook_event),
    )
    _require_success(hook, "gemini-before-agent")
    if not isinstance(json.loads(hook.stdout), dict):
        raise RuntimeError("Gemini hook did not emit one JSON object")

    anyio.run(_verify_mcp, executable, env, project)
    checks.append(VerificationCheck("mcp-bound-unbound-replay"))

    forbidden = (
        retrieval_nonce.encode("utf-8"),
        transcript_sentinel.encode("utf-8"),
    )
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        payload = path.read_bytes()
        if any(value in payload for value in forbidden):
            raise RuntimeError("hook retained prompt or transcript metadata")
    checks.append(VerificationCheck("hook-privacy"))

    _require_success(
        _run(
            executable,
            ["uninstall", "gemini", "--project"],
            cwd=project,
            env=env,
        ),
        "gemini-project-uninstall",
    )
    gemini_remaining = json.loads(
        (gemini_root / "settings.json").read_text(encoding="utf-8")
    )
    if gemini_remaining != {"theme": "mine"}:
        raise RuntimeError("Gemini uninstall removed unrelated settings")
    if (project / "GEMINI.md").read_text(encoding="utf-8") != "# Mine\n":
        raise RuntimeError("Gemini uninstall removed unrelated context")
    _require_success(
        _run(
            executable,
            ["uninstall", "cursor", "--config-dir", str(cursor_home)],
            cwd=project,
            env=env,
        ),
        "cursor-global-uninstall",
    )
    if (plugin / "mine.txt").read_text(encoding="utf-8") != "preserve":
        raise RuntimeError("Cursor uninstall removed unrelated plugin state")
    checks.append(VerificationCheck("scope-exact-uninstall"))
    return VerificationReport(tuple(checks))


def verify_installed_tool(
    memory_executable: Path,
    *,
    keep_home: bool = False,
) -> VerificationReport:
    executable = resolve_memory_executable(memory_executable)
    if keep_home and os.environ.get("CI"):
        raise ValueError("--keep-home is forbidden in CI")
    if keep_home:
        root = Path(tempfile.mkdtemp(prefix="echovault-wheel-"))
        report = _verify_in_root(executable, root.resolve())
        print(root.resolve())
        return report
    with tempfile.TemporaryDirectory(prefix="echovault-wheel-") as directory:
        return _verify_in_root(executable, Path(directory).resolve())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify an installed EchoVault wheel as a black box."
    )
    parser.add_argument("--memory-executable", type=Path, required=True)
    parser.add_argument("--keep-home", action="store_true")
    arguments = parser.parse_args()
    try:
        report = verify_installed_tool(
            arguments.memory_executable,
            keep_home=arguments.keep_home,
        )
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError):
        print("installed-wheel verification failed", file=sys.stderr)
        return 1
    print(json.dumps(asdict(report), sort_keys=True))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
