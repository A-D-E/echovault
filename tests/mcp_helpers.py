from __future__ import annotations

from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Mapping,
    MutableMapping,
    Sequence,
)
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any
import uuid

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import CallToolResult, ListRootsResult, Root, TextContent

from memory.core import MemoryService
from memory.mcp_authority import MCPServerBinding
from memory.mcp_server import _create_server
from memory.projects import ProjectRegistry


RootsCallback = Callable[[Any], Awaitable[ListRootsResult]]
ServiceFactory = Callable[[], MemoryService]


class StderrCapture:
    def __init__(self) -> None:
        self._file = tempfile.TemporaryFile(mode="w+", encoding="utf-8")

    def fileno(self) -> int:
        return self._file.fileno()

    def getvalue(self) -> str:
        self._file.flush()
        position = self._file.tell()
        self._file.seek(0)
        value = self._file.read()
        self._file.seek(position)
        return value

    def close(self) -> None:
        self._file.close()


@asynccontextmanager
async def open_test_client(
    service: MemoryService,
    binding: MCPServerBinding,
    registry: ProjectRegistry,
    list_roots_callback: RootsCallback | None = None,
    worker_service_factory: ServiceFactory | None = None,
) -> AsyncIterator[ClientSession]:
    server = _create_server(
        service,
        binding,
        registry,
        worker_service_factory=worker_service_factory,
    )
    async with create_client_server_memory_streams() as (
        client_streams,
        server_streams,
    ):
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(
                server.run,
                server_streams[0],
                server_streams[1],
                server.create_initialization_options(),
            )
            async with ClientSession(
                *client_streams,
                list_roots_callback=list_roots_callback,
            ) as client:
                await client.initialize()
                yield client
            task_group.cancel_scope.cancel()


def result_text(result: CallToolResult) -> str:
    assert len(result.content) == 1
    content = result.content[0]
    assert isinstance(content, TextContent)
    return content.text


def decode_result(result: CallToolResult) -> object:
    assert result.isError is False
    return json.loads(result_text(result))


def decode_object(result: CallToolResult) -> dict[str, object]:
    payload = decode_result(result)
    assert isinstance(payload, dict)
    return payload


def decode_rows(result: CallToolResult) -> list[dict[str, object]]:
    payload = decode_result(result)
    assert isinstance(payload, list)
    assert all(isinstance(row, dict) for row in payload)
    return payload


def assert_public_payload(
    result: CallToolResult,
    *private_paths: Path,
) -> None:
    payload = decode_result(result)

    def visit(value: object) -> None:
        if isinstance(value, dict):
            assert {"file_path", "root", "canonical_owner"}.isdisjoint(value)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(payload)
    serialized = result_text(result)
    for path in private_paths:
        assert str(path.resolve()) not in serialized


def roots_callback(*roots: Path) -> RootsCallback:
    async def callback(_context: Any) -> ListRootsResult:
        return ListRootsResult(
            roots=[
                Root(uri=root.resolve().as_uri(), name=root.name)
                for root in roots
            ]
        )

    return callback


def make_workspace(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "package.json").write_text("{}", encoding="utf-8")
    return path


@asynccontextmanager
async def open_stdio_client(
    memory_home: Path,
    agent: str,
    project_root: Path,
) -> AsyncIterator[tuple[ClientSession, StderrCapture]]:
    environment = dict(os.environ)
    environment["MEMORY_HOME"] = str(memory_home)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "memory.cli",
            "mcp",
            "--agent",
            agent,
            "--project-root",
            str(project_root),
        ],
        cwd=project_root,
        env=environment,
    )
    stderr = StderrCapture()
    try:
        async with stdio_client(parameters, errlog=stderr) as streams:
            async with ClientSession(*streams) as client:
                await client.initialize()
                yield client, stderr
    finally:
        stderr.close()


@asynccontextmanager
async def open_stdio_session(
    memory_executable: Path,
    args: Sequence[str],
    env: Mapping[str, str],
    *,
    cwd: Path | None = None,
) -> AsyncIterator[ClientSession]:
    """Open a real MCP subprocess using an exact installed executable."""

    parameters = StdioServerParameters(
        command=str(memory_executable),
        args=list(args),
        env=dict(env),
        cwd=cwd,
    )
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            yield session


async def save_with_details(
    client: ClientSession,
    body: str,
) -> dict[str, object]:
    return decode_object(
        await client.call_tool(
            "memory_save",
            {
                "title": "Large details fixture",
                "what": "protocol fixture",
                "details": body,
                "idempotency_key": str(uuid.uuid4()),
            },
        )
    )


async def call_and_store(
    output: MutableMapping[str, CallToolResult],
    key: str,
    client: ClientSession,
    tool: str,
    arguments: dict[str, object],
) -> None:
    output[key] = await client.call_tool(tool, arguments)
