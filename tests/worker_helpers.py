"""Importable multiprocessing workers for canonical-storage tests."""

from __future__ import annotations

import os
import traceback
import uuid
from multiprocessing.queues import Queue

from memory.core import MemoryService
from memory.models import RawMemoryInput


PROJECT = "shared--111111111111"
OPERATION_NAMESPACE = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


class FixedEmbedding:
    def embed(self, text: str) -> list[float]:
        checksum = sum(text.encode("utf-8")) % 997
        return [float(checksum) / 997.0, 0.25, 0.75]


def configured_service(
    memory_home: str,
    *,
    recover_pending: bool = True,
) -> MemoryService:
    service = MemoryService(memory_home, recover_pending=recover_pending)
    service._embedding_provider = FixedEmbedding()
    return service


def write_ten_memories(memory_home: str, worker: int, errors: Queue) -> None:
    service = configured_service(memory_home)
    try:
        for offset in range(10):
            ordinal = worker * 10 + offset
            service.save(
                RawMemoryInput(
                    title=f"worker-{ordinal}",
                    what=f"value-{ordinal}",
                ),
                project=PROJECT,
                authoritative_source="cursor",
                idempotency_key=str(
                    uuid.uuid5(OPERATION_NAMESPACE, f"worker:{ordinal}")
                ),
            )
        errors.put(None)
    except BaseException:
        errors.put(traceback.format_exc())
        raise
    finally:
        service.close()


def crash_save_worker(memory_home: str, phase: str) -> None:
    service = configured_service(memory_home)
    service.persistence.fault = (
        lambda current: os._exit(91) if current == phase else None
    )
    service.save(
        RawMemoryInput(title="crash-boundary", what="new"),
        project=PROJECT,
        authoritative_source="gemini-cli",
        idempotency_key="66666666-6666-4666-8666-666666666666",
    )
    raise AssertionError(f"fault phase was not reached: {phase}")


def crash_merge_worker(
    memory_home: str,
    canonical_id: str,
    source_ids: tuple[str, ...],
    phase: str,
) -> None:
    service = configured_service(memory_home)
    service.persistence.fault = (
        lambda current: os._exit(92) if current == phase else None
    )
    service.merge_memories(
        canonical_id,
        list(source_ids),
        actor="dashboard",
        operation_id="88888888-8888-4888-8888-888888888888",
    )
    raise AssertionError(f"fault phase was not reached: {phase}")
