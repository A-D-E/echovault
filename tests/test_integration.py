"""Integration tests for full end-to-end memory flows.

These tests verify that all components work together correctly:
- Save, search, and details retrieval
- Secret redaction across all layers
- Multi-agent session handling
"""

import os
from pathlib import Path
import uuid

import anyio
from mcp.types import CallToolResult
import pytest

from memory.core import MemoryService
from memory.models import RawMemoryInput
from tests.mcp_helpers import (
    call_and_store,
    decode_object,
    decode_rows,
    make_workspace,
    open_stdio_client,
)


@pytest.mark.anyio
async def test_two_stdio_agents_save_concurrently_and_share_only_one_project(
    tmp_path: Path,
) -> None:
    root = make_workspace(tmp_path / "shared")
    other_root = make_workspace(tmp_path / "isolated")
    memory_home = tmp_path / "memory-home"
    results: dict[str, CallToolResult] = {}

    async with open_stdio_client(
        memory_home,
        "cursor",
        root,
    ) as (cursor, cursor_stderr):
        async with open_stdio_client(
            memory_home,
            "gemini-cli",
            root,
        ) as (gemini, gemini_stderr):
            async with open_stdio_client(
                memory_home,
                "cursor",
                other_root,
            ) as (isolated, isolated_stderr):
                async with anyio.create_task_group() as task_group:
                    task_group.start_soon(
                        call_and_store,
                        results,
                        "cursor",
                        cursor,
                        "memory_save",
                        {
                            "title": "CURSOR-CONCURRENT-42",
                            "what": "cursor marker",
                            "idempotency_key": str(uuid.uuid4()),
                        },
                    )
                    task_group.start_soon(
                        call_and_store,
                        results,
                        "gemini",
                        gemini,
                        "memory_save",
                        {
                            "title": "GEMINI-CONCURRENT-42",
                            "what": "gemini marker",
                            "idempotency_key": str(uuid.uuid4()),
                        },
                    )

                cursor_saved = decode_object(results["cursor"])
                gemini_saved = decode_object(results["gemini"])
                cursor_found = decode_rows(
                    await cursor.call_tool(
                        "memory_search",
                        {"query": "GEMINI-CONCURRENT-42"},
                    )
                )
                gemini_found = decode_rows(
                    await gemini.call_tool(
                        "memory_search",
                        {"query": "CURSOR-CONCURRENT-42"},
                    )
                )
                isolated_found = decode_rows(
                    await isolated.call_tool(
                        "memory_search",
                        {"query": "CONCURRENT-42"},
                    )
                )

                assert gemini_saved["id"] in {
                    row["id"] for row in cursor_found
                }
                assert cursor_saved["id"] in {
                    row["id"] for row in gemini_found
                }
                assert isolated_found == []
                for stderr in (
                    cursor_stderr,
                    gemini_stderr,
                    isolated_stderr,
                ):
                    assert "CURSOR-CONCURRENT-42" not in stderr.getvalue()
                    assert "GEMINI-CONCURRENT-42" not in stderr.getvalue()


def test_full_save_search_details_flow(env_home):
    """Test complete flow: save memories, search across projects, filter by source/project."""
    svc = MemoryService(memory_home=str(env_home))

    # Save first memory with details
    svc.save(
        RawMemoryInput(
            title="JWT Refresh Token Rotation",
            what="Rotate refresh tokens on every use, 7-day expiry",
            why="Static tokens flagged as security risk in audit",
            impact="Mutex lock for concurrent refresh. Changed auth.ts",
            tags=["auth", "jwt", "security"],
            category="decision",
            source="claude-code",
            details="Considered static tokens (rejected), short-lived only (rejected), rotation (chosen).",
        ),
        project="my-api",
    )

    # Save second memory without details
    svc.save(
        RawMemoryInput(
            title="PostgreSQL Over MongoDB",
            what="PostgreSQL for all persistent data",
            why="Need ACID for financial transactions",
            tags=["database", "postgres"],
            category="decision",
            source="claude-code",
        ),
        project="my-api",
    )

    # Save third memory to different project
    svc.save(
        RawMemoryInput(
            title="Redis Cache Setup",
            what="Redis on port 6379 for session store",
            category="context",
            source="codex",
        ),
        project="other-project",
    )

    # Test 1: Search across all projects for "authentication security"
    results = svc.search("authentication security")
    assert len(results) >= 1

    # Find the JWT result
    jwt_result = next((r for r in results if "JWT" in r["title"]), None)
    assert jwt_result is not None
    assert jwt_result["has_details"]  # SQLite returns 1 for True, check truthy

    # Test 2: Get details and verify content
    detail = svc.get_details(jwt_result["id"])
    assert detail is not None
    assert "Considered" in detail.body

    # Test 3: Search scoped to specific project
    results = svc.search("database", project="my-api")
    assert len(results) >= 1
    assert all(r["project"] == "my-api" for r in results)

    # Test 4: Search filtered by source
    results = svc.search("cache", source="codex")
    assert len(results) >= 1
    assert all(r["source"] == "codex" for r in results)

    # Test 5: Verify markdown vault directories exist
    vault = os.path.join(str(env_home), "vault")
    assert os.path.exists(os.path.join(vault, "my-api"))
    assert os.path.exists(os.path.join(vault, "other-project"))

    svc.close()


def test_secret_redaction_e2e(env_home):
    """Test that secrets are redacted in DB, details, and markdown files."""
    svc = MemoryService(memory_home=str(env_home))

    # Save memory with secrets in both what and details
    svc.save(
        RawMemoryInput(
            title="Stripe Config",
            what="Stripe key sk_live_abc123xyz configured for payments",
            details="Webhook secret: <redacted>whsec_secret123</redacted>",
            category="context",
        ),
        project="test",
    )

    # Test 1: Search and verify secrets are redacted in DB results
    results = svc.search("stripe")
    assert len(results) >= 1
    assert "sk_live_" not in results[0]["what"]
    assert "[REDACTED]" in results[0]["what"]

    # Test 2: Get details and verify secrets are redacted
    detail = svc.get_details(results[0]["id"])
    assert detail is not None
    assert "whsec_secret123" not in detail.body
    assert "[REDACTED]" in detail.body

    # Test 3: Read markdown file and verify secrets are not present
    vault = os.path.join(str(env_home), "vault", "test")
    assert os.path.exists(vault)

    markdown_files = [f for f in os.listdir(vault) if f.endswith(".md")]
    assert len(markdown_files) >= 1

    for filename in markdown_files:
        with open(os.path.join(vault, filename)) as f:
            content = f.read()

        # Verify both secrets are redacted in markdown
        assert "sk_live_" not in content
        assert "whsec_secret123" not in content
        assert "[REDACTED]" in content

    svc.close()


def test_multi_agent_same_session(env_home):
    """Test that multiple agents can save to the same session file on the same day."""
    svc = MemoryService(memory_home=str(env_home))

    # Save memory from first agent (claude-code)
    svc.save(
        RawMemoryInput(
            title="Auth Decision",
            what="JWT chosen",
            category="decision",
            source="claude-code",
        ),
        project="shared-project",
    )

    # Save memory from second agent (codex)
    svc.save(
        RawMemoryInput(
            title="Cache Setup",
            what="Redis configured",
            category="context",
            source="codex",
        ),
        project="shared-project",
    )

    # Test 1: Both memories should be searchable
    results = svc.search("shared project setup")
    assert len(results) >= 1

    # Test 2: Only one session file should exist (same day)
    vault = os.path.join(str(env_home), "vault", "shared-project")
    assert os.path.exists(vault)

    markdown_files = [f for f in os.listdir(vault) if f.endswith(".md")]
    assert len(markdown_files) == 1

    # Test 3: Both source names should appear in the markdown content
    with open(os.path.join(vault, markdown_files[0])) as f:
        content = f.read()

    assert "claude-code" in content
    assert "codex" in content

    svc.close()
