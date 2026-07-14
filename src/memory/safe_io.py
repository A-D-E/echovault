from __future__ import annotations

import errno
import hashlib
import os
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


class LockTimeoutError(TimeoutError):
    pass


class ConcurrentModificationError(RuntimeError):
    pass


def translate_windows_lock_error(error: OSError) -> OSError:
    retryable_errno = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
    retryable_winerror = {32, 33}  # sharing and lock violations
    if error.errno in retryable_errno or getattr(error, 'winerror', None) in retryable_winerror:
        return BlockingIOError(error.errno or errno.EACCES, str(error))
    return error


class ProcessFileLock:
    def __init__(self, path: Path, timeout: float = 5.0, poll_interval: float = 0.05):
        self.path = path
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._handle = None

    def __enter__(self) -> 'ProcessFileLock':
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open('a+b')
        self._handle.seek(0)
        if self._handle.read(1) == b'':
            self._handle.write(b'0')
            self._handle.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._acquire_once()
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self._handle.close()
                    self._handle = None
                    raise LockTimeoutError(f'Timed out acquiring lock: {self.path}')
                time.sleep(self.poll_interval)
            except BaseException:
                self._handle.close()
                self._handle = None
                raise

    def _acquire_once(self) -> None:
        assert self._handle is not None
        if os.name == 'nt':
            import msvcrt
            self._handle.seek(0)
            try:
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise translate_windows_lock_error(error)
        else:
            import fcntl
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _release_once(self) -> None:
        assert self._handle is not None
        if os.name == 'nt':
            import msvcrt
            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._handle is None:
            return
        try:
            self._release_once()
        finally:
            self._handle.close()
            self._handle = None


def _replace_and_sync(source: Path, target: Path) -> None:
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes

        move_file_ex = ctypes.WinDLL('kernel32', use_last_error=True).MoveFileExW
        move_file_ex.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move_file_ex.restype = wintypes.BOOL
        movefile_replace_existing = 0x00000001
        movefile_write_through = 0x00000008
        if not move_file_ex(
            str(source),
            str(target),
            movefile_replace_existing | movefile_write_through,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return
    os.replace(source, target)
    descriptor = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass
class PreparedAtomicWrite:
    target: Path
    temporary: Path
    original_mode: int | None

    def replace(self) -> None:
        if self.original_mode is not None:
            self.temporary.chmod(self.original_mode)
        _replace_and_sync(self.temporary, self.target)

    def replace_if_digest(self, expected_digest: str | None) -> None:
        current_digest = digest_file(self.target)
        if current_digest != expected_digest:
            raise ConcurrentModificationError(
                f'Concurrent modification of {self.target}: '
                f'expected {expected_digest!r}, got {current_digest!r}'
            )
        self.replace()

    def discard(self) -> None:
        self.temporary.unlink(missing_ok=True)


def prepare_atomic_text(path: Path, content: str, encoding: str = 'utf-8') -> PreparedAtomicWrite:
    path.parent.mkdir(parents=True, exist_ok=True)
    original_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    descriptor, temp_name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    temporary = Path(temp_name)
    try:
        with os.fdopen(descriptor, 'w', encoding=encoding, newline='') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return PreparedAtomicWrite(path, temporary, original_mode)


def digest_file(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()
