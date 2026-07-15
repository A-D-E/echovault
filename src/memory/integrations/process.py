from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
import subprocess
from typing import Mapping, Protocol, Sequence


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        capture_output: bool = True,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        raise NotImplementedError


def is_executable_regular_file(path: Path) -> bool:
    """Return whether a path is a runnable regular file on this platform."""

    try:
        metadata = path.stat()
    except OSError:
        return False
    if not stat.S_ISREG(metadata.st_mode):
        return False
    if os.name == "nt":
        configured = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
        executable_suffixes = {
            suffix.lower()
            for suffix in configured.split(";")
            if suffix
        }
        return path.suffix.lower() in executable_suffixes
    return os.access(path, os.X_OK)


class SubprocessRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        capture_output: bool = True,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        completed = subprocess.run(
            list(argv),
            timeout=timeout,
            check=False,
            shell=False,
            text=True,
            capture_output=capture_output,
            cwd=None if cwd is None else cwd.resolve(),
            env=None if env is None else dict(env),
        )
        return CommandResult(
            completed.returncode,
            completed.stdout or "",
            completed.stderr or "",
        )
