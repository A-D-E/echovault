from __future__ import annotations

import errno
import hashlib
import os
import secrets
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
        try:
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
                        raise LockTimeoutError(f'Timed out acquiring lock: {self.path}')
                    time.sleep(self.poll_interval)
        except BaseException:
            handle = self._handle
            self._handle = None
            try:
                handle.close()
            except BaseException:
                pass
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
    fsync_directory(target.parent)


def fsync_directory(path: Path) -> None:
    """Durably persist directory-entry changes where the platform permits it."""
    if os.name == 'nt':
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_text_exclusive(
    directory: Path,
    filename: str,
    content: str,
    encoding: str = 'utf-8',
) -> Path:
    """Durably publish a complete text file without replacing any entry.

    On POSIX the directory is pinned with a no-follow descriptor and the fully
    fsynced temporary is published with a hard link.  Linking is atomic and
    fails for regular, symlink, and dangling-symlink collisions.  The Windows
    fallback performs the same no-overwrite publication with a same-directory
    hard link after rejecting a reparse-like directory identity change.
    """
    if not filename or Path(filename).name != filename or filename in {'.', '..'}:
        raise ValueError('filename must be one safe path component')

    parent = directory.parent.resolve()
    if directory.parent.resolve(strict=False) != parent:
        raise OSError('publication parent changed identity')

    if os.name == 'nt':
        if directory.exists():
            metadata = directory.lstat()
            is_junction = bool(
                hasattr(directory, 'is_junction') and directory.is_junction()
            )
            if (
                directory.is_symlink()
                or is_junction
                or not stat.S_ISDIR(metadata.st_mode)
            ):
                raise OSError('publication directory must be a local directory')
        else:
            directory.mkdir(parents=False)
        if directory.resolve(strict=False) != directory:
            raise OSError('publication directory changed identity')
        prepared = prepare_atomic_text(directory / filename, content, encoding)
        try:
            import ctypes
            from ctypes import wintypes

            move_file_ex = ctypes.WinDLL('kernel32', use_last_error=True).MoveFileExW
            move_file_ex.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
            move_file_ex.restype = wintypes.BOOL
            movefile_write_through = 0x00000008
            if not move_file_ex(
                str(prepared.temporary),
                str(prepared.target),
                movefile_write_through,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            prepared.discard()
        return directory / filename

    root_flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    root_fd = os.open(parent, root_flags)
    directory_fd: int | None = None
    temporary_name: str | None = None
    try:
        try:
            os.mkdir(directory.name, mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        directory_fd = os.open(directory.name, root_flags, dir_fd=root_fd)
        directory_metadata = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_metadata.st_mode):
            raise OSError('publication directory must be a directory')

        payload = content.encode(encoding)
        for _attempt in range(128):
            candidate = f'.{filename}.{secrets.token_hex(12)}.tmp'
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, 'O_NOFOLLOW', 0)
            )
            try:
                descriptor = os.open(candidate, flags, 0o600, dir_fd=directory_fd)
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        else:  # pragma: no cover - cryptographically improbable exhaustion
            raise FileExistsError('unable to reserve publication temporary')

        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

        os.link(
            temporary_name,
            filename,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.fsync(directory_fd)
        os.unlink(temporary_name, dir_fd=directory_fd)
        temporary_name = None
        os.fsync(directory_fd)
        return directory / filename
    finally:
        if temporary_name is not None and directory_fd is not None:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        if directory_fd is not None:
            os.close(directory_fd)
        os.close(root_fd)


def read_regular_text_bounded(
    path: Path,
    *,
    max_bytes: int = 1_048_576,
    encoding: str = 'utf-8',
) -> str:
    """Read one bounded regular file without following symlinks or FIFOs."""
    initial = path.lstat()
    if not stat.S_ISREG(initial.st_mode):
        raise OSError('path is not a regular file')
    if initial.st_size > max_bytes:
        raise OSError('file exceeds the bounded read limit')
    flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0)
    if os.name == 'nt':
        flags |= getattr(os, 'O_BINARY', 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError('path is not a regular file')
        if (metadata.st_dev, metadata.st_ino) != (initial.st_dev, initial.st_ino):
            raise OSError('path changed identity before it was opened')
        if metadata.st_size > max_bytes:
            raise OSError('file exceeds the bounded read limit')
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b''.join(chunks)
        if len(payload) > max_bytes:
            raise OSError('file exceeds the bounded read limit')
        final = path.lstat()
        if (final.st_dev, final.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise OSError('path changed identity while it was read')
        return payload.decode(encoding, errors='strict')
    finally:
        os.close(descriptor)


@dataclass
class PreparedAtomicWrite:
    target: Path
    temporary: Path
    original_mode: int | None

    def replace(self) -> None:
        if self.original_mode is not None:
            descriptor = os.open(self.temporary, os.O_RDWR)
            try:
                self.temporary.chmod(self.original_mode)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
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
