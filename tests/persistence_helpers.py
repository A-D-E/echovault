"""State builders and inspectors for persistence integration tests."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from memory.markdown import parse_session_file
from memory.models import RawMemoryInput
from memory.persistence import SaveRequest
from tests.worker_helpers import PROJECT, configured_service


@dataclass(frozen=True)
class ProjectSnapshot:
    markdown: dict[str, tuple[str, str, tuple[str, ...]]]
    database: dict[str, tuple[str, str, tuple[str, ...]]]
    journal_paths: tuple[Path, ...]


def seed_single(memory_home: Path) -> tuple[str, Path]:
    service = configured_service(str(memory_home))
    try:
        result = service.save(
            RawMemoryInput(title="crash-boundary", what="old"),
            project=PROJECT,
            authoritative_source="cursor",
            idempotency_key="77777777-7777-4777-8777-777777777777",
        )
        return str(result["id"]), Path(str(result["file_path"]))
    finally:
        service.close()


def seed_three_historical_memories(
    memory_home: Path,
) -> tuple[str, tuple[str, str]]:
    service = configured_service(str(memory_home))
    try:
        results = []
        for day, title in zip(
            (10, 11, 12),
            ("canonical", "source-one", "source-two"),
            strict=True,
        ):
            operation_id = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"historical:{title}")
            )
            results.append(
                service.persistence.save(
                    SaveRequest(
                        raw=RawMemoryInput(
                            title=title,
                            what=f"{title} body",
                        ),
                        project=PROJECT,
                        source="cursor",
                        operation_id=operation_id,
                        timestamp=f"2026-07-{day:02d}T10:00:00+00:00",
                    )
                )
            )
        return str(results[0]["id"]), (
            str(results[1]["id"]),
            str(results[2]["id"]),
        )
    finally:
        service.close()


def snapshot_project(memory_home: Path) -> ProjectSnapshot:
    markdown: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    project_dir = memory_home / "vault" / PROJECT
    for path in sorted(project_dir.glob("*-session.md")):
        for entry in parse_session_file(path).entries:
            memory = entry.to_memory(str(path))
            markdown[memory.id] = (
                memory.what,
                memory.content_fingerprint or "",
                tuple(
                    operation.operation_id
                    for operation in memory.operations
                ),
            )

    connection = sqlite3.connect(memory_home / "index.db")
    connection.row_factory = sqlite3.Row
    try:
        database = {}
        for row in connection.execute(
            """
            SELECT id, what, content_fingerprint, operation_history
            FROM memories WHERE project = ?
            """,
            (PROJECT,),
        ):
            operations = tuple(
                item["operation_id"]
                for item in json.loads(row["operation_history"])
            )
            database[row["id"]] = (
                row["what"],
                row["content_fingerprint"],
                operations,
            )
    finally:
        connection.close()

    transactions = memory_home / "transactions"
    journals = (
        tuple(sorted(transactions.glob("*.json")))
        if transactions.exists()
        else ()
    )
    return ProjectSnapshot(markdown, database, journals)
