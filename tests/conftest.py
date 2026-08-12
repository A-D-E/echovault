import os
import random
from pathlib import Path
from unittest.mock import patch

import pytest

from memory.embeddings.base import EmbeddingProvider
from memory.core import MemoryService
from memory.models import Memory


class FakeEmbeddingProvider(EmbeddingProvider):
    """Deterministic fake embedding provider for tests.

    Returns reproducible vectors based on text hash so that
    identical inputs produce identical embeddings.
    """

    def __init__(self, dim: int = 768):
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        rng = random.Random(text)
        vec = [rng.gauss(0, 1) for _ in range(self.dim)]
        # L2 normalize
        norm = sum(x * x for x in vec) ** 0.5
        return [x / norm for x in vec]


@pytest.fixture
def sample_memory() -> Memory:
    return Memory(
        id="11111111-1111-4111-8111-111111111111",
        title="Use FastAPI for API endpoints",
        what="Implemented REST API using FastAPI framework",
        why="FastAPI provides automatic validation and documentation",
        impact="Reduces boilerplate code",
        tags=["api", "fastapi"],
        category="decision",
        project="p--1",
        source="cursor",
        related_files=["/src/api/main.py"],
        file_path="2026-07-14-session.md",
        section_anchor="use-fastapi-for-api-endpoints",
        created_at="2026-07-14T10:00:00+00:00",
        updated_at="2026-07-14T10:00:00+00:00",
        creator_source="cursor",
        last_updated_by="cursor",
        contributors=["cursor"],
    )


@pytest.fixture
def tmp_vault(tmp_path):
    """Provides a temporary vault directory for tests."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    return tmp_path


@pytest.fixture
def env_home(tmp_vault, monkeypatch):
    """Overrides MEMORY_HOME and patches embedding provider for tests."""
    monkeypatch.setenv("MEMORY_HOME", str(tmp_vault))

    fake = FakeEmbeddingProvider(dim=768)

    with patch.object(
        __import__("memory.core", fromlist=["MemoryService"]).MemoryService,
        "_create_embedding_provider",
        return_value=fake,
    ):
        yield tmp_vault


@pytest.fixture
def service(env_home: Path):
    instance = MemoryService(str(env_home))
    try:
        yield instance
    finally:
        instance.db.close()


@pytest.fixture
def fake_memory(tmp_path: Path) -> Path:
    executable = tmp_path / "bin" / (
        "memory.exe" if os.name == "nt" else "memory"
    )
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    return executable
