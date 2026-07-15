from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from memory.cli import main
from memory.core import MemoryService
from tests.mcp_helpers import open_stdio_session


@pytest.fixture
def memory_executable() -> Path:
    name = "memory.exe" if os.name == "nt" else "memory"
    sibling = Path(sys.executable).resolve().with_name(name)
    discovered = shutil.which("memory")
    executable = sibling if sibling.is_file() else Path(discovered or "")
    assert executable.is_file()
    return executable.resolve()


@pytest.fixture
def isolated_environment(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    memory_home = home / ".memory"
    command_bin = tmp_path / "command-bin"
    home.mkdir()
    command_bin.mkdir()
    retained = {
        key: os.environ[key]
        for key in (
            "SystemRoot",
            "WINDIR",
            "PATHEXT",
            "COMSPEC",
            "TMP",
            "TEMP",
        )
        if key in os.environ
    }
    return {
        **retained,
        "PATH": str(command_bin),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "MEMORY_HOME": str(memory_home),
        "PYTHONPATH": "",
    }


@pytest.fixture
def v1_service(tmp_path: Path):
    memory_home = tmp_path / "memory-home"
    session = memory_home / "vault/legacy/2026-07-14-session.md"
    session.parent.mkdir(parents=True)
    session.write_text(
        "---\nproject: legacy\n---\n\n"
        "# Session\n\n### Legacy memory\n"
        "**What:** legacy marker remains readable\n",
        encoding="utf-8",
    )
    service = MemoryService(str(memory_home))
    service.import_from_vault()
    try:
        yield service
    finally:
        service.close()


@pytest.mark.anyio
async def test_unbound_mcp_keeps_legacy_save_schema(
    memory_executable: Path,
    isolated_environment: dict[str, str],
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    async with open_stdio_session(
        memory_executable,
        ["mcp"],
        isolated_environment,
        cwd=project,
    ) as session:
        tools = {
            tool.name: tool for tool in (await session.list_tools()).tools
        }
    save_schema = tools["memory_save"].inputSchema
    assert "idempotency_key" not in save_schema.get("required", [])
    assert set(tools) == {
        "memory_context",
        "memory_search",
        "memory_details",
        "memory_save",
    }


@pytest.mark.parametrize(
    "client",
    ["claude-code", "codex", "opencode"],
)
def test_existing_setup_command_remains_available(client: str) -> None:
    result = CliRunner().invoke(main, ["setup", client, "--help"])
    assert result.exit_code == 0


def test_schema_v1_vault_remains_searchable(
    v1_service: MemoryService,
) -> None:
    results = v1_service.search(
        "legacy marker",
        project="legacy",
        use_vectors=False,
    )
    assert [result["title"] for result in results] == ["Legacy memory"]
