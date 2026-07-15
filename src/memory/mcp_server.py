"""MCP server exposing memory tools for coding agents."""

import copy
import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Optional, TypeVar
from uuid import UUID

import anyio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

from memory.core import MemoryService
from memory.context_pack import build_context_pack
from memory.mcp_authority import (
    AuthorityConflict,
    MCPServerBinding,
    file_uri_to_path,
    public_project_error,
    resolve_bound_identity,
    resolve_project_scope,
)
from memory.models import RawMemoryInput
from memory.projects import (
    ProjectRegistry,
    ProjectResolutionError,
    ProjectScope,
)


logger = logging.getLogger(__name__)
INTERNAL_ERROR = "EchoVault could not complete the tool request"
ResolvedScope = ProjectScope | str | None
ScopedDispatch = Callable[
    [str, Mapping[str, object], ResolvedScope],
    Awaitable[CallToolResult],
]
T = TypeVar("T")

VALID_CATEGORIES = (
    "decision", "bug", "pattern", "learning", "context", "playbook",
    "known_fix", "constraint", "project_state", "active_work",
)

SAVE_DESCRIPTION = """Save a memory for future sessions. You MUST call this before ending any session where you made changes, fixed bugs, made decisions, or learned something. This is not optional — failing to save means the next session starts from zero.

Save when you:
- Made an architectural or design decision (chose X over Y)
- Fixed a bug (include root cause and solution)
- Discovered a non-obvious pattern or gotcha
- Learned something about the codebase not obvious from code
- Set up infrastructure, tooling, or configuration
- The user corrected you or clarified a requirement

Do NOT save: trivial changes (typos, formatting), info obvious from reading the code, or duplicates of existing memories. Write for a future agent with zero context."""
SAVE_DESCRIPTION += """

When filling `details`, prefer this structure:
- Context
- Options considered
- Decision
- Tradeoffs
- Follow-up"""

SEARCH_DESCRIPTION = """Search memories using keyword and semantic search. Use this when the task-aware memory_context pack is insufficient or when investigating a narrower topic. Explicit search remains available even when automatic context is disabled."""

CONTEXT_DESCRIPTION = """Get a task-aware living-memory context pack for the current project. You MUST call this before feature development, planning, debugging, or architecture work. Pass the current user request verbatim or as a faithful task summary in `query`, and pass your runtime identity in `agent` (for example `claude-code` or `codex`) so policy overrides work. The response includes summaries, structured playbooks, constraints, provenance, and token estimates directly to avoid extra round trips. If policy reports disabled, continue without automatic context; explicit memory_search and memory_save still work."""


def handle_memory_save(
    service: MemoryService,
    title: str,
    what: str,
    why: Optional[str] = None,
    impact: Optional[str] = None,
    tags: Optional[list[str]] = None,
    category: Optional[str] = None,
    related_files: Optional[list[str]] = None,
    details: Optional[str] = None,
    project: Optional[str] = None,
    triggers: Optional[list[str]] = None,
    prerequisites: Optional[list[str]] = None,
    steps: Optional[list[str]] = None,
    verification: Optional[list[str]] = None,
    follow_ups: Optional[list[str]] = None,
    constraints: Optional[list[str]] = None,
    alternatives_rejected: Optional[list[str]] = None,
    open_questions: Optional[list[str]] = None,
    confidence: Optional[float] = None,
    valid_from: Optional[str] = None,
    valid_until: Optional[str] = None,
    commit_sha: Optional[str] = None,
    branch: Optional[str] = None,
    links: Optional[list[str]] = None,
    last_verified: Optional[str] = None,
    authoritative_source: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> str:
    """Handle memory_save tool call. Returns JSON string."""
    project = project or os.path.basename(os.getcwd())

    if category and category not in VALID_CATEGORIES:
        category = "context"

    raw = RawMemoryInput(
        title=title[:60],
        what=what,
        why=why,
        impact=impact,
        tags=tags or [],
        category=category,
        related_files=related_files or [],
        details=details,
        triggers=triggers or [], prerequisites=prerequisites or [], steps=steps or [],
        verification=verification or [], follow_ups=follow_ups or [],
        constraints=constraints or [], open_questions=open_questions or [],
        alternatives_rejected=alternatives_rejected or [],
        confidence=confidence, valid_from=valid_from, valid_until=valid_until,
        commit_sha=commit_sha, branch=branch, links=links or [],
        last_verified=last_verified,
    )

    result = service.save(
        raw,
        project=project,
        authoritative_source=authoritative_source,
        idempotency_key=idempotency_key,
    )
    return json.dumps(result)


def handle_memory_search(
    service: MemoryService,
    query: str,
    limit: int = 5,
    project: ProjectScope | str | None = None,
    record_feedback: bool = True,
) -> str:
    """Handle memory_search tool call. Returns JSON string."""
    results = service.search(
        query,
        limit=limit,
        project=project,
        record_feedback=record_feedback,
    )

    clean = []
    for r in results:
        tags_raw = r.get("tags", "[]")
        if isinstance(tags_raw, str):
            try:
                tags_list = json.loads(tags_raw)
            except (json.JSONDecodeError, TypeError):
                tags_list = []
        elif isinstance(tags_raw, list):
            tags_list = tags_raw
        else:
            tags_list = []

        clean.append({
            "id": r["id"],
            "title": r["title"],
            "what": r["what"],
            "why": r.get("why"),
            "impact": r.get("impact"),
            "category": r.get("category"),
            "tags": tags_list,
            "project": r.get("project"),
            "created_at": r.get("created_at", "")[:10],
            "score": round(r.get("score", 0), 2),
            "has_details": bool(r.get("has_details")),
        })
    return json.dumps(clean)


def handle_memory_context(
    service: MemoryService,
    project: ProjectScope | str | None = None,
    limit: int = 10,
    query: Optional[str] = None,
    agent: Optional[str] = None,
    token_budget: Optional[int] = None,
    record_feedback: bool = True,
) -> str:
    """Handle memory_context tool call. Returns JSON string."""
    project = project or os.path.basename(os.getcwd())

    policy = service.context_policy(agent)
    if not policy["enabled"]:
        return json.dumps({"total": service.db.count_memories(project=project), "showing": 0, "memories": [], "disabled": True, "policy": policy})
    results, total = service.get_context(
        limit=limit,
        project=project,
        query=query,
        agent=agent,
        token_budget=token_budget,
        record_feedback=record_feedback,
    )

    return json.dumps(build_context_pack(results, total=total))


def legacy_tool_definitions() -> tuple[Tool, ...]:
    """Return the stable unbound MCP tool contract."""
    return (
            Tool(
                name="memory_save",
                description=SAVE_DESCRIPTION,
                inputSchema={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Short title, max 60 chars."},
                        "what": {"type": "string", "description": "1-2 sentences. The essence a future agent needs."},
                        "why": {"type": "string", "description": "Reasoning behind the decision or fix."},
                        "impact": {"type": "string", "description": "What changed as a result."},
                        "tags": {"type": "array", "items": {"type": "string"}, "description": "Relevant tags."},
                        "category": {
                            "type": "string",
                            "enum": list(VALID_CATEGORIES),
                            "description": "decision: chose X over Y. bug: fixed a problem. pattern: reusable gotcha. learning: non-obvious discovery. context: project setup/architecture.",
                        },
                        "related_files": {"type": "array", "items": {"type": "string"}, "description": "File paths involved."},
                        "details": {
                            "type": "string",
                            "description": (
                                "Full context for a future agent with zero context. "
                                "Prefer: Context, Options considered, Decision, Tradeoffs, Follow-up."
                            ),
                        },
                        "project": {"type": "string", "description": "Project name. Auto-detected from cwd if omitted."},
                        "triggers": {"type": "array", "items": {"type": "string"}},
                        "prerequisites": {"type": "array", "items": {"type": "string"}},
                        "steps": {"type": "array", "items": {"type": "string"}},
                        "verification": {"type": "array", "items": {"type": "string"}},
                        "follow_ups": {"type": "array", "items": {"type": "string"}},
                        "constraints": {"type": "array", "items": {"type": "string"}},
                        "alternatives_rejected": {"type": "array", "items": {"type": "string"}},
                        "open_questions": {"type": "array", "items": {"type": "string"}},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "valid_from": {"type": "string"}, "valid_until": {"type": "string"},
                        "commit_sha": {"type": "string"}, "branch": {"type": "string"},
                        "links": {"type": "array", "items": {"type": "string"}},
                        "last_verified": {"type": "string"},
                    },
                    "required": ["title", "what"],
                },
            ),
            Tool(
                name="memory_search",
                description=SEARCH_DESCRIPTION,
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search terms"},
                        "limit": {"type": "integer", "default": 5, "description": "Max results"},
                        "project": {"type": "string", "description": "Filter to project."},
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="memory_context",
                description=CONTEXT_DESCRIPTION,
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project": {"type": "string", "description": "Project name. Auto-detected from cwd if omitted."},
                        "limit": {"type": "integer", "default": 10, "description": "Max memories"},
                        "query": {"type": "string", "description": "Required for task-aware work: the current user request or a faithful task summary."},
                        "agent": {"type": "string", "description": "Your runtime identity for policy overrides: claude-code, codex, cursor, or opencode."},
                        "token_budget": {"type": "integer", "default": 1200, "description": "Approximate context token budget."},
                    },
                },
            ),
            Tool(
                name="memory_details",
                description="Get the full details body for one memory ID or unique prefix.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "memory_id": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Full memory ID or unique literal prefix.",
                        },
                    },
                    "required": ["memory_id"],
                },
            ),
        )


def tool_definitions(binding: MCPServerBinding) -> tuple[Tool, ...]:
    """Derive bound schemas without mutating the legacy definitions."""
    tools = copy.deepcopy(legacy_tool_definitions())
    by_name = {tool.name: tool for tool in tools}
    for tool in tools:
        tool.inputSchema.setdefault("properties", {})["cwd"] = {
            "type": "string",
            "description": (
                "Caller cwd; must remain inside the negotiated project root."
            ),
        }
        project_schema = tool.inputSchema["properties"].get("project")
        if project_schema is not None:
            project_schema["description"] = (
                "Optional validation value; must match the authoritative "
                "bound project."
            )
    save_schema = by_name["memory_save"].inputSchema
    save_schema["properties"]["source"] = {
        "type": "string",
        "description": (
            "Optional validation value; must match the authoritative bound "
            "agent identity when one is configured."
        ),
    }
    if binding.agent is not None:
        save_schema["properties"]["idempotency_key"] = {
            "type": "string",
            "format": "uuid",
        }
        save_schema["required"] = [
            *save_schema.get("required", []),
            "idempotency_key",
        ]
        by_name["memory_context"].inputSchema["properties"]["agent"][
            "description"
        ] = (
            "Optional validation value; must match the authoritative bound "
            "agent identity."
        )
    return tools


def tool_error(message: str) -> CallToolResult:
    return CallToolResult(
        isError=True,
        content=[TextContent(type="text", text=message)],
    )


def success_text(text: str) -> CallToolResult:
    payload = json.loads(text)
    if isinstance(payload, dict):
        payload.pop("file_path", None)
    return CallToolResult(
        isError=False,
        content=[
            TextContent(
                type="text",
                text=json.dumps(payload),
            )
        ],
    )


def legacy_project(scope: ResolvedScope) -> str:
    if isinstance(scope, ProjectScope):
        return scope.identity.key
    if isinstance(scope, str):
        return scope
    return os.path.basename(os.getcwd())


def make_legacy_scoped_dispatch(service: MemoryService) -> ScopedDispatch:
    """Bridge resolved scope to the existing direct handlers."""
    async def dispatch(
        name: str,
        arguments: Mapping[str, object],
        scope: ResolvedScope,
    ) -> CallToolResult:
        project = legacy_project(scope)
        call_arguments = dict(arguments)
        call_arguments.pop("cwd", None)
        call_arguments.pop("project", None)

        if name == "memory_save":
            call_arguments.pop("idempotency_key", None)
            call_arguments.pop("source", None)
            return success_text(
                handle_memory_save(
                    service,
                    project=project,
                    **call_arguments,
                )
            )
        if name == "memory_search":
            return success_text(
                handle_memory_search(
                    service,
                    project=project,
                    **call_arguments,
                )
            )
        if name == "memory_context":
            return success_text(
                handle_memory_context(
                    service,
                    project=project,
                    **call_arguments,
                )
            )
        if name == "memory_details":
            memory_id = call_arguments.get("memory_id")
            if not isinstance(memory_id, str) or not memory_id:
                return tool_error("memory_id must be a non-empty string")
            detail = service.get_details(memory_id, project=project)
            payload = (
                {"status": "not_found"}
                if detail is None
                else {
                    "status": "ok",
                    "memory_id": detail.memory_id,
                    "body": detail.body,
                }
            )
            return success_text(json.dumps(payload))
        return tool_error("Unknown EchoVault tool")

    return dispatch


def read_project(scope: ResolvedScope) -> ProjectScope | str:
    if scope is None:
        return os.path.basename(os.getcwd())
    return scope


def write_project(scope: ResolvedScope) -> str:
    if isinstance(scope, ProjectScope):
        return scope.identity.key
    if scope is None:
        return os.path.basename(os.getcwd())
    return scope


def handle_memory_details(
    service: MemoryService,
    memory_id: str,
    *,
    scope: ResolvedScope,
    record_feedback: bool = True,
) -> str:
    detail = service.get_details(
        memory_id,
        project=read_project(scope),
        record_feedback=record_feedback,
    )
    if detail is None:
        return json.dumps({"status": "not_found"})
    return json.dumps(
        {
            "status": "ok",
            "memory_id": detail.memory_id,
            "body": detail.body,
        }
    )


async def run_with_service(
    service_factory: Callable[[], MemoryService],
    call: Callable[[MemoryService], T],
    *,
    abandon_on_cancel: bool,
) -> T:
    def invoke() -> T:
        service = service_factory()
        try:
            return call(service)
        finally:
            service.close()

    return await anyio.to_thread.run_sync(
        invoke,
        abandon_on_cancel=abandon_on_cancel,
    )


async def run_read(
    service_factory: Callable[[], MemoryService],
    call: Callable[[MemoryService], T],
) -> T:
    return await run_with_service(
        service_factory,
        call,
        abandon_on_cancel=True,
    )


async def run_save(
    service_factory: Callable[[], MemoryService],
    call: Callable[[MemoryService], dict[str, object]],
) -> dict[str, object]:
    return await run_with_service(
        service_factory,
        call,
        abandon_on_cancel=False,
    )


async def _record_feedback(
    service_factory: Callable[[], MemoryService],
    memory_ids: list[str],
    event: str = "retrieved",
) -> None:
    if not memory_ids:
        return
    await run_with_service(
        service_factory,
        lambda service: service.db.record_feedback(memory_ids, event),
        abandon_on_cancel=False,
    )


def make_bound_scoped_dispatch(
    service: MemoryService,
    binding: MCPServerBinding,
    *,
    service_factory: Callable[[], MemoryService] | None = None,
) -> ScopedDispatch:
    """Create the authoritative four-tool callback for a bound server."""
    selected_factory = service_factory or (
        lambda: MemoryService(str(service.memory_home))
    )

    async def dispatch(
        name: str,
        arguments: Mapping[str, object],
        scope: ResolvedScope,
    ) -> CallToolResult:
        if not isinstance(scope, ProjectScope):
            raise ProjectResolutionError(
                "Bound dispatch requires a canonical scope"
            )

        call_arguments = dict(arguments)
        call_arguments.pop("cwd", None)
        call_arguments.pop("project", None)

        if name == "memory_context":
            requested_agent = call_arguments.pop("agent", None)
            if requested_agent is not None and not isinstance(
                requested_agent,
                str,
            ):
                raise AuthorityConflict(
                    "authority",
                    "agent must be a string",
                )
            agent = resolve_bound_identity(
                binding.agent,
                requested_agent,
                field_name="agent",
            )
            text = await run_read(
                selected_factory,
                lambda worker: handle_memory_context(
                    worker,
                    project=read_project(scope),
                    agent=agent,
                    record_feedback=False,
                    **call_arguments,
                ),
            )
            payload = json.loads(text)
            memory_ids = [
                str(item["id"])
                for item in payload.get("memories", [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ] if isinstance(payload, dict) else []
            await _record_feedback(selected_factory, memory_ids)
            return success_text(text)

        if name == "memory_search":
            text = await run_read(
                selected_factory,
                lambda worker: handle_memory_search(
                    worker,
                    project=read_project(scope),
                    record_feedback=False,
                    **call_arguments,
                ),
            )
            payload = json.loads(text)
            memory_ids = [
                str(item["id"])
                for item in payload
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ] if isinstance(payload, list) else []
            await _record_feedback(selected_factory, memory_ids)
            return success_text(text)

        if name == "memory_details":
            memory_id = call_arguments.get("memory_id")
            if not isinstance(memory_id, str) or not memory_id:
                raise AuthorityConflict(
                    "authority",
                    "memory_id must be non-empty",
                )
            text = await run_read(
                selected_factory,
                lambda worker: handle_memory_details(
                    worker,
                    memory_id,
                    scope=scope,
                    record_feedback=False,
                ),
            )
            payload = json.loads(text)
            detail_ids = (
                [str(payload["memory_id"])]
                if (
                    isinstance(payload, dict)
                    and payload.get("status") == "ok"
                    and isinstance(payload.get("memory_id"), str)
                )
                else []
            )
            await _record_feedback(
                selected_factory,
                detail_ids,
                "details_opened",
            )
            return success_text(text)

        if name == "memory_save":
            requested_source = call_arguments.pop("source", None)
            if requested_source is not None and not isinstance(
                requested_source,
                str,
            ):
                raise AuthorityConflict(
                    "authority",
                    "source must be a string",
                )
            source = resolve_bound_identity(
                binding.agent,
                requested_source,
                field_name="source",
            )
            operation_id = call_arguments.pop("idempotency_key", None)
            if binding.agent is not None:
                if not isinstance(operation_id, str):
                    raise AuthorityConflict(
                        "authority",
                        "idempotency key is required",
                    )
                try:
                    UUID(operation_id)
                except ValueError as error:
                    raise AuthorityConflict(
                        "authority",
                        "idempotency key is not a UUID",
                    ) from error
            result = await run_save(
                selected_factory,
                lambda worker: json.loads(
                    handle_memory_save(
                        worker,
                        project=write_project(scope),
                        authoritative_source=source,
                        idempotency_key=(
                            operation_id
                            if isinstance(operation_id, str)
                            else None
                        ),
                        **call_arguments,
                    )
                ),
            )
            return success_text(json.dumps(result))

        return tool_error("Unknown EchoVault tool")

    return dispatch


async def resolve_call_scope(
    server: Server,
    binding: MCPServerBinding,
    registry: ProjectRegistry,
    arguments: Mapping[str, object],
) -> ResolvedScope:
    if not binding.is_bound:
        requested_project = arguments.get("project")
        return resolve_project_scope(
            binding,
            registry,
            client_roots=(),
            cwd=None,
            requested_project=(
                requested_project
                if isinstance(requested_project, str)
                else None
            ),
        )

    capabilities = server.request_context.session.client_params.capabilities
    client_roots: tuple[Path, ...] = ()
    if capabilities.roots is not None:
        roots_result = await server.request_context.session.list_roots()
        client_roots = tuple(
            file_uri_to_path(str(root.uri))
            for root in roots_result.roots
        )

    raw_cwd = arguments.get("cwd")
    if raw_cwd is not None and not isinstance(raw_cwd, str):
        raise AuthorityConflict("authority", "cwd must be a string path")
    raw_project = arguments.get("project")
    if raw_project is not None and not isinstance(raw_project, str):
        raise AuthorityConflict("authority", "project must be a string")
    return resolve_project_scope(
        binding,
        registry,
        client_roots=client_roots,
        cwd=Path(raw_cwd) if raw_cwd else None,
        requested_project=raw_project,
    )


async def project_safe_dispatch(
    operation: Callable[[], Awaitable[CallToolResult]],
) -> CallToolResult:
    try:
        return await operation()
    except ProjectResolutionError as error:
        return tool_error(public_project_error(error))
    except Exception as error:
        logger.error(
            "Unhandled MCP tool failure (%s)",
            type(error).__name__,
        )
        return tool_error(INTERNAL_ERROR)


def install_scoped_call_handler(
    server: Server,
    binding: MCPServerBinding,
    registry: ProjectRegistry,
    selected_dispatch: ScopedDispatch,
) -> None:
    @server.call_tool()
    async def call_tool(
        name: str,
        arguments: dict[str, object],
    ) -> CallToolResult:
        async def resolve_and_dispatch() -> CallToolResult:
            scope = await resolve_call_scope(
                server,
                binding,
                registry,
                arguments,
            )
            return await selected_dispatch(name, arguments, scope)

        return await project_safe_dispatch(resolve_and_dispatch)


def configure_task4_dispatch(
    server: Server,
    service: MemoryService,
    binding: MCPServerBinding,
    registry: ProjectRegistry,
    scoped_dispatch: ScopedDispatch | None,
    worker_service_factory: Callable[[], MemoryService],
) -> None:
    selected_dispatch = scoped_dispatch or (
        make_bound_scoped_dispatch(
            service,
            binding,
            service_factory=worker_service_factory,
        )
        if binding.is_bound
        else make_legacy_scoped_dispatch(service)
    )
    install_scoped_call_handler(
        server,
        binding,
        registry,
        selected_dispatch,
    )


def _create_server(
    service: MemoryService,
    binding: MCPServerBinding | None = None,
    registry: ProjectRegistry | None = None,
    *,
    worker_service_factory: Callable[[], MemoryService] | None = None,
    scoped_dispatch: ScopedDispatch | None = None,
) -> Server:
    """Create and configure the MCP server with memory tools."""
    binding = binding or MCPServerBinding(None, None, Path.cwd())
    registry = registry or ProjectRegistry(Path(service.memory_home))
    def default_worker_service() -> MemoryService:
        worker = MemoryService(str(service.memory_home))
        worker.config = copy.deepcopy(service.config)
        return worker

    selected_worker_factory = worker_service_factory or default_worker_service
    server = Server("echovault")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        definitions = (
            tool_definitions(binding)
            if binding.is_bound
            else legacy_tool_definitions()
        )
        return list(definitions)

    configure_task4_dispatch(
        server,
        service,
        binding,
        registry,
        scoped_dispatch,
        selected_worker_factory,
    )

    return server


async def run_server(
    *,
    agent: str | None = None,
    project_root: Path | None = None,
    startup_cwd: Path | None = None,
) -> None:
    """Run the MCP server with stdio transport."""
    service = MemoryService()
    try:
        binding = MCPServerBinding(
            agent,
            project_root,
            startup_cwd or Path.cwd(),
        )
        registry = ProjectRegistry(Path(service.memory_home))

        def worker_service_factory() -> MemoryService:
            return MemoryService(str(service.memory_home))

        server = _create_server(
            service,
            binding,
            registry,
            worker_service_factory=worker_service_factory,
        )
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        service.close()
