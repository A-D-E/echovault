import errno
import os
from multiprocessing import Event, Process, Queue
from pathlib import Path

import pytest

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


def test_process_lock_times_out_while_another_process_owns_it(tmp_path: Path) -> None:
    ready = Event()
    release = Event()
    process = Process(target=_hold_lock, args=(str(tmp_path / 'project.lock'), ready, release))
    process.start()
    assert ready.wait(2.0)
    with pytest.raises(LockTimeoutError):
        with ProcessFileLock(tmp_path / 'project.lock', timeout=0.1, poll_interval=0.01):
            pass
    release.set()
    process.join(2.0)
    assert process.exitcode == 0


@pytest.mark.skipif(os.name != 'nt', reason='requires Windows msvcrt locking')
def test_windows_process_lock_contends_across_processes(tmp_path: Path) -> None:
    ready = Event()
    release = Event()
    result = Queue()
    lock_path = tmp_path / 'project.lock'
    holder = Process(target=_hold_lock, args=(str(lock_path), ready, release))
    holder.start()
    assert ready.wait(2.0)
    contender = Process(target=_report_lock_timeout, args=(str(lock_path), result))
    contender.start()
    assert result.get(timeout=2.0) == 'timed-out'
    contender.join(2.0)
    release.set()
    holder.join(2.0)
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


def test_unexpected_unlock_error_still_closes_handle(tmp_path: Path, monkeypatch) -> None:
    lock = ProcessFileLock(tmp_path / 'project.lock')
    lock.__enter__()

    def fail() -> None:
        raise OSError(errno.EIO, 'unlock failed')

    monkeypatch.setattr(lock, '_release_once', fail)
    with pytest.raises(OSError, match='unlock failed'):
        lock.__exit__(None, None, None)
    assert lock._handle is None
