import errno
import os
from multiprocessing import Event, Process, Queue
from pathlib import Path

import pytest

import memory.safe_io as safe_io
from memory.safe_io import (
    ConcurrentModificationError,
    LockTimeoutError,
    ProcessFileLock,
    digest_file,
    prepare_atomic_text,
    translate_windows_lock_error,
)


def _hold_lock(path: str, ready: Event, release: Event) -> None:
    with ProcessFileLock(Path(path), timeout=1.0):
        ready.set()
        release.wait(5.0)


def _report_lock_timeout(path: str, result: Queue) -> None:
    try:
        with ProcessFileLock(Path(path), timeout=0.1, poll_interval=0.01):
            result.put('acquired')
    except LockTimeoutError:
        result.put('timed-out')


def _join_or_terminate(process: Process) -> None:
    process.join(2.0)
    if process.is_alive():
        process.terminate()
        process.join(2.0)


def test_process_lock_times_out_while_another_process_owns_it(tmp_path: Path) -> None:
    ready = Event()
    release = Event()
    process = Process(target=_hold_lock, args=(str(tmp_path / 'project.lock'), ready, release))
    process.start()
    try:
        assert ready.wait(2.0)
        with pytest.raises(LockTimeoutError):
            with ProcessFileLock(tmp_path / 'project.lock', timeout=0.1, poll_interval=0.01):
                pass
    finally:
        release.set()
        _join_or_terminate(process)
    assert process.exitcode == 0


@pytest.mark.skipif(os.name != 'nt', reason='requires Windows msvcrt locking')
def test_windows_process_lock_contends_across_processes(tmp_path: Path) -> None:
    ready = Event()
    release = Event()
    result = Queue()
    lock_path = tmp_path / 'project.lock'
    holder = Process(target=_hold_lock, args=(str(lock_path), ready, release))
    contender = Process(target=_report_lock_timeout, args=(str(lock_path), result))
    holder.start()
    try:
        assert ready.wait(2.0)
        contender.start()
        assert result.get(timeout=2.0) == 'timed-out'
        contender.join(2.0)
    finally:
        release.set()
        if contender.pid is not None:
            _join_or_terminate(contender)
        _join_or_terminate(holder)
        result.close()
        result.join_thread()
    assert contender.exitcode == 0
    assert holder.exitcode == 0


def test_prepared_write_is_invisible_until_replace_and_preserves_mode(tmp_path: Path) -> None:
    target = tmp_path / 'session.md'
    target.write_text('old\n', encoding='utf-8')
    target.chmod(0o640)
    prepared = prepare_atomic_text(target, 'new\n')
    assert target.read_text(encoding='utf-8') == 'old\n'
    prepared.replace()
    assert target.read_text(encoding='utf-8') == 'new\n'
    assert target.stat().st_mode & 0o777 == 0o640
    assert digest_file(target) is not None


def test_prepared_write_replaces_read_only_target_and_preserves_mode(tmp_path: Path) -> None:
    target = tmp_path / 'session.md'
    target.write_text('old\n', encoding='utf-8')
    target.chmod(0o444)
    prepared = prepare_atomic_text(target, 'new\n')
    try:
        prepared.replace()
        assert target.read_text(encoding='utf-8') == 'new\n'
        assert target.stat().st_mode & 0o777 == 0o444
    finally:
        if prepared.temporary.exists():
            prepared.temporary.chmod(0o600)
        prepared.discard()
        if target.exists():
            target.chmod(0o600)


def test_preserved_mode_is_synced_before_replace(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / 'session.md'
    target.write_text('old\n', encoding='utf-8')
    target.chmod(0o640)
    events: list[str] = []
    original_chmod = Path.chmod

    def record_chmod(path: Path, mode: int) -> None:
        events.append('chmod')
        original_chmod(path, mode)

    monkeypatch.setattr(Path, 'chmod', record_chmod)
    monkeypatch.setattr(safe_io.os, 'fsync', lambda descriptor: events.append('fsync'))
    monkeypatch.setattr(
        safe_io,
        '_replace_and_sync',
        lambda source, replacement_target: events.append('replace'),
    )

    prepared = prepare_atomic_text(target, 'new\n')
    try:
        prepared.replace()
        chmod_index = events.index('chmod')
        replace_index = events.index('replace')
        assert 'fsync' in events[chmod_index + 1:replace_index]
    finally:
        prepared.discard()


def test_digest_compare_rejects_a_change_after_prepare(tmp_path: Path) -> None:
    target = tmp_path / 'projects.json'
    target.write_text('{"version":1}\n', encoding='utf-8')
    expected = digest_file(target)
    prepared = prepare_atomic_text(target, '{"version":2}\n')
    target.write_text('{"external":true}\n', encoding='utf-8')
    with pytest.raises(ConcurrentModificationError):
        prepared.replace_if_digest(expected)
    assert target.read_text(encoding='utf-8') == '{"external":true}\n'
    prepared.discard()


def test_digest_compare_rejects_changed_prepared_payload(tmp_path: Path) -> None:
    target = tmp_path / 'projects.json'
    target.write_text('{"version":1}\n', encoding='utf-8')
    expected_target = digest_file(target)
    prepared = prepare_atomic_text(target, '{"version":2}\n')
    expected_temporary = digest_file(prepared.temporary)
    prepared.temporary.write_text('{"unexpected":true}\n', encoding='utf-8')

    with pytest.raises(ConcurrentModificationError):
        prepared.replace_if_digest(expected_target, expected_temporary)

    assert target.read_text(encoding='utf-8') == '{"version":1}\n'
    prepared.discard()


def test_digest_rejects_oversized_sparse_file(tmp_path: Path) -> None:
    path = tmp_path / 'oversized.md'
    with path.open('wb') as handle:
        handle.truncate(safe_io.MAX_DIGEST_FILE_BYTES + 1)

    with pytest.raises(OSError, match='size limit'):
        digest_file(path)


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='FIFO is POSIX-specific')
def test_digest_rejects_fifo_without_opening_it(tmp_path: Path) -> None:
    path = tmp_path / 'prepared.fifo'
    os.mkfifo(path)

    with pytest.raises(OSError, match='regular file'):
        digest_file(path)


@pytest.mark.parametrize('code', [errno.EACCES, errno.EAGAIN, errno.EDEADLK])
def test_windows_contention_errors_become_retryable(code: int) -> None:
    translated = translate_windows_lock_error(OSError(code, 'locked'))
    assert isinstance(translated, BlockingIOError)


def test_windows_lock_violation_winerror_becomes_retryable() -> None:
    class WinLockError(OSError):
        winerror = 33

    error = WinLockError('lock violation')
    assert isinstance(translate_windows_lock_error(error), BlockingIOError)


def test_windows_unexpected_lock_error_is_not_retried() -> None:
    original = OSError(errno.EBADF, 'bad descriptor')
    assert translate_windows_lock_error(original) is original


def test_unexpected_acquire_error_closes_handle(tmp_path: Path, monkeypatch) -> None:
    lock = ProcessFileLock(tmp_path / 'project.lock')

    def fail() -> None:
        raise OSError(errno.EBADF, 'bad descriptor')

    monkeypatch.setattr(lock, '_acquire_once', fail)
    with pytest.raises(OSError, match='bad descriptor'):
        lock.__enter__()
    assert lock._handle is None


def test_initialization_error_closes_handle(tmp_path: Path, monkeypatch) -> None:
    class SeekFailingHandle:
        closed = False

        def seek(self, offset: int) -> None:
            raise OSError(errno.EIO, 'seek failed')

        def close(self) -> None:
            self.closed = True

    handle = SeekFailingHandle()
    lock = ProcessFileLock(tmp_path / 'project.lock')
    monkeypatch.setattr(Path, 'open', lambda *args, **kwargs: handle)

    with pytest.raises(OSError, match='seek failed'):
        lock.__enter__()

    assert handle.closed
    assert lock._handle is None


def test_retry_sleep_error_closes_handle(tmp_path: Path, monkeypatch) -> None:
    lock = ProcessFileLock(tmp_path / 'project.lock')

    def contend() -> None:
        raise BlockingIOError(errno.EAGAIN, 'busy')

    def fail_sleep(interval: float) -> None:
        raise OSError(errno.EIO, 'sleep failed')

    monkeypatch.setattr(lock, '_acquire_once', contend)
    monkeypatch.setattr(safe_io.time, 'sleep', fail_sleep)
    try:
        with pytest.raises(OSError, match='sleep failed'):
            lock.__enter__()
        assert lock._handle is None
    finally:
        if lock._handle is not None:
            lock._handle.close()


def test_unexpected_unlock_error_still_closes_handle(tmp_path: Path, monkeypatch) -> None:
    lock = ProcessFileLock(tmp_path / 'project.lock')
    lock.__enter__()

    def fail() -> None:
        raise OSError(errno.EIO, 'unlock failed')

    monkeypatch.setattr(lock, '_release_once', fail)
    with pytest.raises(OSError, match='unlock failed'):
        lock.__exit__(None, None, None)
    assert lock._handle is None
