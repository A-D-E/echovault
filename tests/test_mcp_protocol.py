from pathlib import Path
import threading
import uuid

import anyio
from mcp import ClientSession
from mcp.types import CallToolResult, ListRootsResult, Root
import pytest

from memory.core import MemoryService
from memory.mcp_authority import (
    AUTHORITY_ERROR,
    BOUNDARY_ERROR,
    PROJECT_ERROR,
    ROOTS_ERROR,
    AuthorityConflict,
    MCPServerBinding,
)
from memory.mcp_server import project_safe_dispatch
from memory.models import RawMemoryInput
from memory.projects import (
    MultiRootError,
    ProjectRegistry,
    ProjectResolutionError,
    build_project_identity,
    discover_project_root,
)
from tests.mcp_helpers import (
    assert_public_payload,
    decode_object,
    decode_rows,
    make_workspace,
    open_test_client,
    result_text,
    roots_callback,
    save_with_details,
)


class BlockingContextService(MemoryService):
    def __init__(
        self,
        memory_home: str,
        context_started: threading.Event,
        release_context: threading.Event,
    ) -> None:
        super().__init__(memory_home)
        self.context_started = context_started
        self.release_context = release_context

    def get_context(self, *args, **kwargs):
        self.context_started.set()
        if not self.release_context.wait(timeout=5):
            raise TimeoutError("test did not release context")
        return super().get_context(*args, **kwargs)


@pytest.mark.anyio
async def test_tool_inventory_and_large_details(tmp_path: Path) -> None:
    root = make_workspace(tmp_path / "repo")
    memory_home = tmp_path / "memory-home"
    service = MemoryService(str(memory_home))
    try:
        async with open_test_client(
            service,
            MCPServerBinding("cursor", root, root),
            ProjectRegistry(memory_home),
        ) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "memory_context",
                "memory_search",
                "memory_details",
                "memory_save",
            }
            body = "x" * 100_000
            saved = await save_with_details(client, body)
            detail = decode_object(
                await client.call_tool(
                    "memory_details",
                    {"memory_id": saved["id"]},
                )
            )
            assert detail["body"] == body
    finally:
        service.close()


@pytest.mark.anyio
async def test_cursor_bound_context_off_does_not_fall_back_to_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = make_workspace(tmp_path / "repo")
    memory_home = tmp_path / "memory-home"
    service = MemoryService(str(memory_home))
    service.config.context.agent_modes["cursor"] = "off"
    search_calls = 0

    def fail_if_searched(*args, **kwargs):
        nonlocal search_calls
        search_calls += 1
        raise AssertionError("disabled context must not search")

    monkeypatch.setattr(service, "search", fail_if_searched)
    try:
        async with open_test_client(
            service,
            MCPServerBinding("cursor", root, root),
            ProjectRegistry(memory_home),
        ) as client:
            response = decode_object(
                await client.call_tool("memory_context", {"query": "task"})
            )
        assert response["disabled"] is True
        assert response["policy"]["mode"] == "off"
        assert response["policy"]["source"] == "agent:cursor"
        assert response["memories"] == []
        assert search_calls == 0
    finally:
        service.close()


@pytest.mark.anyio
async def test_cancelling_context_keeps_next_request_healthy(
    tmp_path: Path,
) -> None:
    root = make_workspace(tmp_path / "repo")
    memory_home = tmp_path / "memory-home"
    service = MemoryService(str(memory_home))
    context_started = threading.Event()
    release_context = threading.Event()
    identity = build_project_identity(*discover_project_root(root))
    seeded = service.save(
        RawMemoryInput(title="Cancel target", what="cancel marker"),
        project=identity.key,
    )
    cancel_scope = anyio.CancelScope()
    finished = anyio.Event()

    async def invoke_context(client: ClientSession) -> None:
        try:
            with cancel_scope:
                await client.call_tool(
                    "memory_context",
                    {"query": "cancel marker"},
                )
        finally:
            finished.set()

    try:
        async with open_test_client(
            service,
            MCPServerBinding("cursor", root, root),
            ProjectRegistry(memory_home),
            worker_service_factory=lambda: BlockingContextService(
                str(memory_home),
                context_started,
                release_context,
            ),
        ) as client:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(invoke_context, client)
                with anyio.fail_after(2):
                    while not context_started.is_set():
                        await anyio.sleep(0.01)
                cancel_scope.cancel()
                await finished.wait()
                release_context.set()
                next_result = await client.call_tool(
                    "memory_search",
                    {"query": "next request"},
                )
                assert next_result.isError is False
                assert service.get_memory_record(str(seeded["id"]))[
                    "retrieved_count"
                ] == 0
                task_group.cancel_scope.cancel()
    finally:
        release_context.set()
        service.close()


@pytest.mark.anyio
async def test_malformed_bound_inputs_are_tool_errors(tmp_path: Path) -> None:
    root = make_workspace(tmp_path / "repo")
    memory_home = tmp_path / "memory-home"
    service = MemoryService(str(memory_home))
    try:
        async with open_test_client(
            service,
            MCPServerBinding("cursor", root, root),
            ProjectRegistry(memory_home),
        ) as client:
            for tool, arguments in (
                (
                    "memory_save",
                    {
                        "title": "x",
                        "what": "x",
                        "idempotency_key": "not-a-uuid",
                    },
                ),
                ("memory_details", {"memory_id": ""}),
                ("memory_search", {"query": "x", "cwd": 42}),
            ):
                result = await client.call_tool(tool, arguments)
                assert result.isError is True
                assert str(root) not in result_text(result)
                assert str(memory_home) not in result_text(result)

        assert service.db.count_memories() == 0
    finally:
        service.close()


@pytest.mark.anyio
async def test_unbound_protocol_does_not_request_client_roots(
    tmp_path: Path,
) -> None:
    memory_home = tmp_path / "memory-home"
    service = MemoryService(str(memory_home))
    calls = 0

    async def forbidden_roots(_context: object) -> ListRootsResult:
        nonlocal calls
        calls += 1
        raise AssertionError("legacy unbound server must not request roots")

    try:
        async with open_test_client(
            service,
            MCPServerBinding(None, None, tmp_path),
            ProjectRegistry(memory_home),
            forbidden_roots,
        ) as client:
            result = await client.call_tool(
                "memory_search",
                {"query": "x", "project": "legacy-project"},
            )
            assert result.isError is False
        assert calls == 0
    finally:
        service.close()


@pytest.mark.anyio
async def test_bound_server_rejects_source_agent_and_project_spoof(
    tmp_path: Path,
) -> None:
    root = make_workspace(tmp_path / "repo")
    memory_home = tmp_path / "memory-home"
    service = MemoryService(str(memory_home))
    try:
        async with open_test_client(
            service,
            MCPServerBinding("cursor", root, root),
            ProjectRegistry(memory_home),
        ) as client:
            for tool, arguments, expected, low_level in (
                (
                    "memory_context",
                    {"query": "x", "agent": "gemini-cli"},
                    AUTHORITY_ERROR,
                    "Conflicting agent",
                ),
                (
                    "memory_save",
                    {
                        "title": "x",
                        "what": "x",
                        "source": "gemini-cli",
                        "idempotency_key": str(uuid.uuid4()),
                    },
                    AUTHORITY_ERROR,
                    "Conflicting source",
                ),
                (
                    "memory_search",
                    {"query": "x", "project": "other"},
                    PROJECT_ERROR,
                    "outside the resolved scope",
                ),
            ):
                result = await client.call_tool(tool, arguments)
                assert result.isError is True
                assert result_text(result) == expected
                assert low_level not in result_text(result)
                assert str(root) not in result_text(result)
                assert str(memory_home) not in result_text(result)
    finally:
        service.close()


@pytest.mark.anyio
async def test_bound_details_hides_other_project_without_feedback(
    tmp_path: Path,
) -> None:
    root = make_workspace(tmp_path / "repo")
    memory_home = tmp_path / "memory-home"
    service = MemoryService(str(memory_home))
    seeded = service.save(
        RawMemoryInput(
            title="Private",
            what="other project",
            details="secret",
        ),
        project="other--111111111111",
    )
    before = service.get_memory_record(str(seeded["id"]))[
        "details_opened_count"
    ]
    try:
        async with open_test_client(
            service,
            MCPServerBinding("cursor", root, root),
            ProjectRegistry(memory_home),
        ) as client:
            result = await client.call_tool(
                "memory_details",
                {"memory_id": str(seeded["id"])},
            )
            assert decode_object(result) == {"status": "not_found"}
        after = service.get_memory_record(str(seeded["id"]))[
            "details_opened_count"
        ]
        assert after == before
    finally:
        service.close()


@pytest.mark.anyio
async def test_reads_use_aliases_but_save_writes_only_hashed_key(
    tmp_path: Path,
) -> None:
    root = make_workspace(tmp_path / "repo")
    memory_home = tmp_path / "memory-home"
    service = MemoryService(str(memory_home))
    registry = ProjectRegistry(memory_home)
    identity = build_project_identity(*discover_project_root(root))
    registry.register(identity)
    registry.adopt_legacy("legacy-repo", identity)
    legacy = service.save(
        RawMemoryInput(title="Legacy marker", what="ALIAS-READ-42"),
        project="legacy-repo",
    )
    try:
        async with open_test_client(
            service,
            MCPServerBinding("cursor", root, root),
            registry,
        ) as client:
            found = decode_rows(
                await client.call_tool(
                    "memory_search",
                    {"query": "ALIAS-READ-42", "project": "legacy-repo"},
                )
            )
            assert str(legacy["id"]) in {
                str(row["id"]) for row in found
            }
            saved = decode_object(
                await client.call_tool(
                    "memory_save",
                    {
                        "title": "Canonical marker",
                        "what": "HASHED-WRITE-42",
                        "project": "legacy-repo",
                        "idempotency_key": str(uuid.uuid4()),
                    },
                )
            )
        assert service.get_memory_record(str(saved["id"]))[
            "project"
        ] == identity.key
    finally:
        service.close()


@pytest.mark.anyio
async def test_cursor_save_is_retrievable_by_gemini(tmp_path: Path) -> None:
    root = make_workspace(tmp_path / "repo")
    memory_home = tmp_path / "memory-home"
    service = MemoryService(str(memory_home))
    registry = ProjectRegistry(memory_home)
    try:
        async with open_test_client(
            service,
            MCPServerBinding("cursor", root, root),
            registry,
        ) as cursor_client:
            async with open_test_client(
                service,
                MCPServerBinding("gemini-cli", root, root),
                registry,
            ) as gemini_client:
                saved = decode_object(
                    await cursor_client.call_tool(
                        "memory_save",
                        {
                            "title": "Cross agent",
                            "what": "shared marker",
                            "idempotency_key": str(uuid.uuid4()),
                        },
                    )
                )
                found = decode_rows(
                    await gemini_client.call_tool(
                        "memory_search",
                        {"query": "Cross agent"},
                    )
                )
                assert saved["id"] in {row["id"] for row in found}
    finally:
        service.close()


@pytest.mark.anyio
async def test_bound_tool_results_never_expose_local_paths(
    tmp_path: Path,
) -> None:
    root = make_workspace(tmp_path / "repo")
    memory_home = tmp_path / "memory-home"
    service = MemoryService(str(memory_home))
    try:
        async with open_test_client(
            service,
            MCPServerBinding("cursor", root, root),
            ProjectRegistry(memory_home),
        ) as client:
            save_result = await client.call_tool(
                "memory_save",
                {
                    "title": "Public payload marker",
                    "what": "PUBLIC-PAYLOAD-42",
                    "details": "full body",
                    "idempotency_key": str(uuid.uuid4()),
                },
            )
            saved = decode_object(save_result)
            results = (
                save_result,
                await client.call_tool(
                    "memory_context",
                    {"query": "PUBLIC-PAYLOAD-42"},
                ),
                await client.call_tool(
                    "memory_search",
                    {"query": "PUBLIC-PAYLOAD-42"},
                ),
                await client.call_tool(
                    "memory_details",
                    {"memory_id": saved["id"]},
                ),
            )
            for result in results:
                assert_public_payload(result, root, memory_home)
    finally:
        service.close()


@pytest.mark.anyio
async def test_protocol_uses_single_client_root_without_leaking_paths(
    tmp_path: Path,
) -> None:
    root = make_workspace(tmp_path / "encoded workspace")
    memory_home = tmp_path / "memory-home"
    service = MemoryService(str(memory_home))
    binding = MCPServerBinding("cursor", None, tmp_path)
    registry = ProjectRegistry(memory_home)
    try:
        async with open_test_client(
            service,
            binding,
            registry,
            roots_callback(root),
        ) as client:
            result = await client.call_tool(
                "memory_save",
                {
                    "title": "Root marker",
                    "what": "single root",
                    "idempotency_key": str(uuid.uuid4()),
                },
            )
            saved = decode_object(result)
            record = service.get_memory_record(str(saved["id"]))
            expected = build_project_identity(*discover_project_root(root))
            assert record["project"] == expected.key
            assert_public_payload(result, root, memory_home)
    finally:
        service.close()


@pytest.mark.anyio
async def test_protocol_rejects_cwd_outside_root_without_disclosing_it(
    tmp_path: Path,
) -> None:
    root = make_workspace(tmp_path / "workspace")
    outside = tmp_path / "private-outside"
    outside.mkdir()
    memory_home = tmp_path / "memory-home"
    service = MemoryService(str(memory_home))
    binding = MCPServerBinding("cursor", None, tmp_path)
    try:
        async with open_test_client(
            service,
            binding,
            ProjectRegistry(memory_home),
            roots_callback(root),
        ) as client:
            result = await client.call_tool(
                "memory_search",
                {"query": "x", "cwd": str(outside)},
            )
            assert result.isError is True
            assert result_text(result) == BOUNDARY_ERROR
            assert "private-outside" not in result_text(result)
            assert str(root) not in result_text(result)
            assert "Resolved path escapes authority boundary" not in result_text(
                result
            )
    finally:
        service.close()


@pytest.mark.anyio
async def test_protocol_requests_roots_again_for_each_call(
    tmp_path: Path,
) -> None:
    left = make_workspace(tmp_path / "left")
    right = make_workspace(tmp_path / "right")
    advertised = [left]
    calls = 0

    async def changing_roots(_context: object) -> ListRootsResult:
        nonlocal calls
        calls += 1
        return ListRootsResult(
            roots=[
                Root(uri=path.resolve().as_uri(), name=path.name)
                for path in advertised
            ]
        )

    memory_home = tmp_path / "memory-home"
    service = MemoryService(str(memory_home))
    try:
        async with open_test_client(
            service,
            MCPServerBinding("cursor", None, tmp_path),
            ProjectRegistry(memory_home),
            changing_roots,
        ) as client:
            first = decode_object(
                await client.call_tool(
                    "memory_save",
                    {
                        "title": "Left",
                        "what": "left",
                        "idempotency_key": str(uuid.uuid4()),
                    },
                )
            )
            advertised[:] = [right]
            second = decode_object(
                await client.call_tool(
                    "memory_save",
                    {
                        "title": "Right",
                        "what": "right",
                        "idempotency_key": str(uuid.uuid4()),
                    },
                )
            )
        left_record = service.get_memory_record(str(first["id"]))
        right_record = service.get_memory_record(str(second["id"]))
        assert left_record["project"] != right_record["project"]
        assert calls == 2
    finally:
        service.close()


@pytest.mark.anyio
async def test_protocol_many_roots_require_cwd_inside_exactly_one(
    tmp_path: Path,
) -> None:
    left = make_workspace(tmp_path / "left")
    right = make_workspace(tmp_path / "right")
    nested = right / "src"
    nested.mkdir()
    memory_home = tmp_path / "memory-home"
    service = MemoryService(str(memory_home))
    try:
        async with open_test_client(
            service,
            MCPServerBinding("cursor", None, tmp_path),
            ProjectRegistry(memory_home),
            roots_callback(left, right),
        ) as client:
            accepted = await client.call_tool(
                "memory_save",
                {
                    "title": "Selected right",
                    "what": "many roots",
                    "cwd": str(nested),
                    "idempotency_key": str(uuid.uuid4()),
                },
            )
            saved = decode_object(accepted)
            expected = build_project_identity(*discover_project_root(right))
            assert service.get_memory_record(str(saved["id"]))[
                "project"
            ] == expected.key

            rejected = await client.call_tool(
                "memory_search",
                {"query": "x"},
            )
            assert rejected.isError is True
            assert result_text(rejected) == ROOTS_ERROR
            assert str(left) not in result_text(rejected)
            assert str(right) not in result_text(rejected)
            assert "exactly one root" not in result_text(rejected)
    finally:
        service.close()


@pytest.mark.anyio
async def test_protocol_zero_roots_uses_only_marked_startup(
    tmp_path: Path,
) -> None:
    marked = make_workspace(tmp_path / "marked workspace")
    startup = marked / "tools"
    startup.mkdir()
    generic = tmp_path / "generic-home"
    generic.mkdir()

    for startup_cwd, should_succeed in ((startup, True), (generic, False)):
        memory_home = tmp_path / f"memory-{should_succeed}"
        service = MemoryService(str(memory_home))
        try:
            async with open_test_client(
                service,
                MCPServerBinding("cursor", None, startup_cwd),
                ProjectRegistry(memory_home),
                roots_callback(),
            ) as client:
                result = await client.call_tool(
                    "memory_search",
                    {"query": "x"},
                )
                assert result.isError is (not should_succeed)
                if should_succeed:
                    assert_public_payload(result, marked, memory_home)
                else:
                    assert result_text(result) == ROOTS_ERROR
                    assert str(generic) not in result_text(result)
        finally:
            service.close()


@pytest.mark.parametrize("advertises_roots_capability", [False, True])
@pytest.mark.anyio
async def test_protocol_generic_startup_accepts_explicit_marked_cwd_without_roots(
    tmp_path: Path,
    advertises_roots_capability: bool,
) -> None:
    generic = tmp_path / "generic-home"
    project = make_workspace(tmp_path / "workspace")
    cwd = project / "src"
    generic.mkdir()
    cwd.mkdir()
    memory_home = tmp_path / "memory-home"
    service = MemoryService(str(memory_home))
    roots = roots_callback() if advertises_roots_capability else None
    try:
        async with open_test_client(
            service,
            MCPServerBinding("cursor", None, generic),
            ProjectRegistry(memory_home),
            roots,
        ) as client:
            accepted = await client.call_tool(
                "memory_search",
                {"query": "x", "cwd": str(cwd)},
            )
            assert accepted.isError is False
            assert_public_payload(accepted, project, memory_home)
            rejected = await client.call_tool(
                "memory_search",
                {"query": "x", "cwd": str(generic)},
            )
            assert rejected.isError is True
            assert result_text(rejected) == ROOTS_ERROR
            assert str(generic) not in result_text(rejected)
    finally:
        service.close()


@pytest.mark.anyio
async def test_protocol_without_roots_capability_uses_marked_startup(
    tmp_path: Path,
) -> None:
    root = make_workspace(tmp_path / "startup")
    startup = root / "tools"
    startup.mkdir()
    memory_home = tmp_path / "memory-home"
    service = MemoryService(str(memory_home))
    try:
        async with open_test_client(
            service,
            MCPServerBinding("cursor", None, startup),
            ProjectRegistry(memory_home),
        ) as client:
            result = await client.call_tool(
                "memory_search",
                {"query": "x"},
            )
            assert result.isError is False
            assert_public_payload(result, root, memory_home)
    finally:
        service.close()


@pytest.mark.parametrize("corruption", ["git-pointer", "registry-json"])
@pytest.mark.anyio
async def test_protocol_maps_low_level_project_errors_without_path_leak(
    tmp_path: Path,
    corruption: str,
) -> None:
    root = make_workspace(tmp_path / "workspace")
    memory_home = tmp_path / "memory-home"
    registry = ProjectRegistry(memory_home)
    private_marker = tmp_path / "private-location"
    if corruption == "git-pointer":
        (root / ".git").write_text(
            f'gitdir: {private_marker / "missing-git-dir"}\n',
            encoding="utf-8",
        )
    else:
        memory_home.mkdir(parents=True, exist_ok=True)
        (memory_home / "projects.json").write_text(
            f'{{"private":"{private_marker}"',
            encoding="utf-8",
        )
    service = MemoryService(str(memory_home))
    try:
        async with open_test_client(
            service,
            MCPServerBinding("cursor", root, root),
            registry,
            roots_callback(root),
        ) as client:
            result = await client.call_tool(
                "memory_search",
                {"query": "x"},
            )
            assert result.isError is True
            assert result_text(result) == ROOTS_ERROR
            payload = result_text(result)
            for forbidden in (str(root), str(private_marker), str(memory_home)):
                assert forbidden not in payload
            for low_level in (
                "gitdir",
                "projects.json",
                "missing-git-dir",
                "private-location",
            ):
                assert low_level not in payload
    finally:
        service.close()


@pytest.mark.anyio
async def test_project_safe_dispatch_never_forwards_exception_text(
    tmp_path: Path,
) -> None:
    private = tmp_path / "private-workspace"
    cases = (
        (
            AuthorityConflict("boundary", f"escaped {private}"),
            BOUNDARY_ERROR,
        ),
        (
            AuthorityConflict("project", f"alias data {private}"),
            PROJECT_ERROR,
        ),
        (
            AuthorityConflict("authority", f"bound agent data {private}"),
            AUTHORITY_ERROR,
        ),
        (MultiRootError(f"roots were {private}"), ROOTS_ERROR),
        (
            ProjectResolutionError(f"bad registry at {private}"),
            ROOTS_ERROR,
        ),
    )
    for error, expected in cases:
        async def fail(
            error: ProjectResolutionError = error,
        ) -> CallToolResult:
            raise error

        result = await project_safe_dispatch(fail)
        assert result.isError is True
        assert result_text(result) == expected
        assert str(private) not in result_text(result)
        assert str(error) not in result_text(result)
