from __future__ import annotations

from pathlib import Path

import pytest

from memory.core import MemoryService
from memory.models import RawMemoryInput


VALID_EVENT = {
    "session_id": "session-1",
    "transcript_path": "/must/not/be-read.json",
    "cwd": "/workspace/repo",
    "hook_event_name": "BeforeAgent",
    "timestamp": "2026-07-14T12:00:00Z",
    "prompt": "Find marker ALPHA-42",
}


class SeededServiceFactory:
    def __init__(self, service: MemoryService) -> None:
        self.service = service

    def __call__(self) -> MemoryService:
        return self.service


def retrieved_count(factory: SeededServiceFactory, marker: str) -> int:
    row = factory.service.db.conn.execute(
        "SELECT retrieved_count FROM memories WHERE what LIKE ?",
        (f"%{marker}%",),
    ).fetchone()
    assert row is not None
    return int(row["retrieved_count"] or 0)


@pytest.fixture
def seeded_service_factory(env_home: Path):
    service = MemoryService(str(env_home))
    service.save(
        RawMemoryInput(
            title="Marker ALPHA-42",
            what="Durable marker ALPHA-42",
            category="context",
            source="cursor",
        ),
        project="workspace-repo",
    )
    yield SeededServiceFactory(service)
    service.close()
