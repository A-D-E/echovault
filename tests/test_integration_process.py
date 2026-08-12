from pathlib import Path
import subprocess

import pytest

import memory.integrations.process as process
from memory.integrations.process import (
    CommandResult,
    SubprocessRunner,
)


def test_subprocess_runner_never_uses_a_shell_and_forwards_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            argv,
            0,
            "2026.07.09-a3815c0\n",
            "",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = SubprocessRunner().run(
        ["agent", "--version"],
        timeout=2.0,
        cwd=tmp_path,
        env={"HOME": str(tmp_path)},
    )
    assert result == CommandResult(0, "2026.07.09-a3815c0\n", "")
    assert captured["argv"] == ["agent", "--version"]
    assert captured["shell"] is False
    assert captured["cwd"] == tmp_path.resolve()
    assert captured["env"] == {"HOME": str(tmp_path)}


def test_windows_executable_requires_pathext_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extensionless = tmp_path / "memory"
    extensionless.write_text("binary", encoding="utf-8")
    executable = tmp_path / "memory.EXE"
    executable.write_text("binary", encoding="utf-8")
    assert hasattr(process, "is_executable_regular_file")

    monkeypatch.setattr(process.os, "name", "nt")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")

    assert process.is_executable_regular_file(extensionless) is False
    assert process.is_executable_regular_file(executable) is True
