from __future__ import annotations

from multiprocessing import get_context
from pathlib import Path
from threading import Event, Thread

from memory.core import MemoryService
from memory.health import doctor_home
from memory.models import RawMemoryInput
from memory.persistence import acquire_project_locks
from memory.safe_io import LockTimeoutError, ProcessFileLock
from tests.persistence_helpers import seed_single, snapshot_project
from tests.worker_helpers import PROJECT, configured_service, write_ten_memories


def test_project_locks_are_acquired_sorted_and_released_reverse(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class RecordingLock:
        def __init__(self, path: Path):
            self.path = path

        def __enter__(self) -> "RecordingLock":
            events.append(f"enter:{self.path.stem}")
            return self

        def __exit__(
            self,
            exc_type: object,
            exc: object,
            traceback: object,
        ) -> None:
            events.append(f"exit:{self.path.stem}")

    with acquire_project_locks(
        tmp_path,
        ["z-project", "a-project", "m-project", "a-project"],
        lock_factory=RecordingLock,
    ):
        events.append("body")

    assert events == [
        "enter:a-project",
        "enter:m-project",
        "enter:z-project",
        "body",
        "exit:z-project",
        "exit:m-project",
        "exit:a-project",
    ]


def test_lock_timeout_is_typed_and_released_lock_can_be_reacquired(
    tmp_path: Path,
) -> None:
    path = tmp_path / "project.lock"

    with ProcessFileLock(path):
        try:
            with ProcessFileLock(path, timeout=0.01):
                raise AssertionError("contended lock was acquired")
        except LockTimeoutError:
            pass

    with ProcessFileLock(path, timeout=0.01):
        assert path.exists()


def test_eight_processes_write_eighty_unique_operations(
    tmp_path: Path,
) -> None:
    memory_home = tmp_path / ".memory"
    context = get_context("spawn")
    errors = context.Queue()
    processes = [
        context.Process(
            target=write_ten_memories,
            args=(str(memory_home), worker, errors),
        )
        for worker in range(8)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(60)
        assert process.exitcode == 0

    assert [errors.get(timeout=2) for _ in processes] == [None] * 8
    snapshot = snapshot_project(memory_home)
    operation_ids = {
        operation_id
        for _what, _fingerprint, operations in snapshot.markdown.values()
        for operation_id in operations
    }
    assert len(snapshot.markdown) == 80
    assert len(operation_ids) == 80
    assert snapshot.markdown == snapshot.database

    service = configured_service(str(memory_home))
    try:
        assert service.db.count_memories(project=PROJECT) == 80
    finally:
        service.close()
    assert doctor_home(memory_home, project=PROJECT)["status"] == "ok"


def test_delayed_old_embedding_cannot_overwrite_new_fingerprint(
    tmp_path: Path,
) -> None:
    memory_home = tmp_path / ".memory"
    memory_id, _ = seed_single(memory_home)
    first_started = Event()
    release_first = Event()
    first_cas_results: list[bool] = []
    thread_errors: list[BaseException] = []

    class BlockingFirstEmbedding:
        def embed(self, text: str) -> list[float]:
            first_started.set()
            assert release_first.wait(10)
            return [0.1, 0.2, 0.3]

    def first_update() -> None:
        service = MemoryService(str(memory_home))
        service._embedding_provider = BlockingFirstEmbedding()
        real_upsert = service.db.upsert_vector_if_current

        def recording_upsert(
            current_memory_id: str,
            fingerprint: str,
            embedding: list[float],
        ) -> bool:
            result = real_upsert(
                current_memory_id,
                fingerprint,
                embedding,
            )
            first_cas_results.append(result)
            return result

        service.db.upsert_vector_if_current = recording_upsert
        try:
            service.save(
                RawMemoryInput(
                    title="crash-boundary",
                    what="first update",
                ),
                project=PROJECT,
                authoritative_source="cursor",
                idempotency_key="99999999-9999-4999-8999-999999999999",
            )
        except BaseException as error:
            thread_errors.append(error)
        finally:
            service.close()

    thread = Thread(target=first_update)
    thread.start()
    assert first_started.wait(10)

    second = configured_service(str(memory_home))
    try:
        second.save(
            RawMemoryInput(
                title="crash-boundary",
                what="second update",
            ),
            project=PROJECT,
            authoritative_source="gemini-cli",
            idempotency_key="aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa",
        )
        second_fingerprint = second.get_memory_record(memory_id)[
            "content_fingerprint"
        ]
    finally:
        second.close()

    release_first.set()
    thread.join(10)
    assert not thread.is_alive()
    assert thread_errors == []
    assert first_cas_results == [False]

    snapshot = snapshot_project(memory_home)
    assert snapshot.markdown == snapshot.database
    assert snapshot.markdown[memory_id][0] == "second update"
    assert snapshot.markdown[memory_id][1] == second_fingerprint
