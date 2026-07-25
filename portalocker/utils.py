from __future__ import annotations

import abc
import atexit
import collections.abc
import contextlib
import errno
import logging
import os
import pathlib
import random
import tempfile
import time
import typing
import warnings
import weakref

from . import constants, exceptions, portalocker, types
from .types import Filename, Mode

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 5
DEFAULT_CHECK_INTERVAL = 0.25
DEFAULT_FAIL_WHEN_LOCKED = False
LOCK_METHOD = constants.LockFlags.EXCLUSIVE | constants.LockFlags.NON_BLOCKING

__all__ = [
    'Lock',
    'open_atomic',
]


def coalesce(*args: typing.Any, test_value: typing.Any = None) -> typing.Any:
    """Simple coalescing function that returns the first value that is not
    equal to the `test_value`. Or `None` if no value is valid. Usually this
    means that the last given value is the default value.

    Note that the `test_value` is compared using an identity check
    (i.e. `value is not test_value`) so changing the `test_value` won't work
    for all values.

    >>> coalesce(None, 1)
    1
    >>> coalesce()

    >>> coalesce(0, False, True)
    0
    >>> coalesce(0, False, True, test_value=0)
    False

    # This won't work because of the `is not test_value` type testing:
    >>> coalesce([], dict(spam='eggs'), test_value=[])
    []
    """
    return next((arg for arg in args if arg is not test_value), None)


@contextlib.contextmanager
def open_atomic(
    filename: Filename,
    binary: bool = True,
) -> collections.abc.Generator[types.IO]:
    """Open a new file for atomic writing without replacing an existing file.

    The destination must not exist when entering or publishing the context. If
    another actor creates it while the context is open, publication raises
    :class:`FileExistsError` and leaves that destination untouched.

    The implementation writes and synchronizes a temporary file in the
    destination directory, then publishes it with an operation that refuses an
    existing destination. Windows uses an atomic rename; POSIX uses an atomic
    hard link, so the POSIX filesystem must support hard links.

    https://docs.python.org/3/library/os.html#os.link

    >>> filename = 'test_file.txt'
    >>> if os.path.exists(filename):
    ...     os.remove(filename)

    >>> with open_atomic(filename) as fh:
    ...     written = fh.write(b'test')
    >>> assert os.path.exists(filename)
    >>> os.remove(filename)

    >>> import pathlib
    >>> path_filename = pathlib.Path('test_file.txt')

    >>> with open_atomic(path_filename) as fh:
    ...     written = fh.write(b'test')
    >>> assert path_filename.exists()
    >>> path_filename.unlink()
    """
    # `pathlib.Path` cast in case `path` is a `str`
    path: pathlib.Path
    if isinstance(filename, pathlib.Path):
        path = filename
    else:
        path = pathlib.Path(filename)

    if path.exists():
        raise AssertionError(f'{path!r} exists')

    # Create the parent directory if it doesn't exist
    path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        mode=(binary and 'wb') or 'w',
        dir=str(path.parent),
        delete=False,
    ) as temp_fh:
        yield temp_fh
        temp_fh.flush()
        os.fsync(temp_fh.fileno())

    try:
        if os.name == 'nt':  # pragma: not-nt
            os.rename(temp_fh.name, path)
        else:  # pragma: not-posix
            os.link(temp_fh.name, path)
    finally:
        with contextlib.suppress(Exception):
            os.remove(temp_fh.name)


#: The type returned by `LockBase.acquire` and, through it, by
#: `LockBase.__enter__`. Locks that guard a file return the opened
#: filehandle, others return whatever fits their locking model.
AcquireReturnT = typing.TypeVar('AcquireReturnT')


class LockBase(  # pragma: no cover
    abc.ABC,
    typing.Generic[AcquireReturnT],
):
    #: timeout when trying to acquire a lock
    timeout: float
    #: check interval while waiting for `timeout`
    check_interval: float
    #: skip the timeout and immediately fail if the initial lock fails
    fail_when_locked: bool

    def __init__(
        self,
        timeout: float | None = None,
        check_interval: float | None = None,
        fail_when_locked: bool | None = None,
    ) -> None:
        self.timeout = coalesce(timeout, DEFAULT_TIMEOUT)
        self.check_interval = coalesce(check_interval, DEFAULT_CHECK_INTERVAL)
        self.fail_when_locked = coalesce(
            fail_when_locked,
            DEFAULT_FAIL_WHEN_LOCKED,
        )

    @abc.abstractmethod
    def acquire(
        self,
        timeout: float | None = None,
        check_interval: float | None = None,
        fail_when_locked: bool | None = None,
    ) -> AcquireReturnT: ...

    def _timeout_generator(
        self,
        timeout: float | None,
        check_interval: float | None,
    ) -> typing.Iterator[int]:
        f_timeout = coalesce(timeout, self.timeout, 0.0)
        f_check_interval = coalesce(check_interval, self.check_interval, 0.0)

        yield 0
        i = 0

        start_time = time.perf_counter()
        while start_time + f_timeout > time.perf_counter():
            i += 1
            yield i

            # Take low lock checks into account to stay within the interval
            since_start_time = time.perf_counter() - start_time
            time.sleep(max(0.001, (i * f_check_interval) - since_start_time))

    @abc.abstractmethod
    def release(self) -> None: ...

    def __enter__(self) -> AcquireReturnT:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: typing.Any,  # Should be typing.TracebackType
    ) -> bool | None:
        self.release()
        return None

    def __delete__(self, instance: LockBase[AcquireReturnT]) -> None:
        instance.release()

    # Ensure cleanup on garbage collection as tests rely on this behaviour
    def __del__(self) -> None:  # pragma: no cover - best effort cleanup
        with contextlib.suppress(Exception):
            self.release()


class Lock(LockBase[typing.IO[typing.Any]]):
    """Lock manager with built-in timeout

    Args:
        filename: filename
        mode: the open mode, 'a' or 'ab' should be used for writing. When mode
            contains `w` the file will be truncated to 0 bytes.
        timeout: timeout when trying to acquire a lock
        check_interval: check interval while waiting
        fail_when_locked: after the initial lock failed, return an error
            or lock the file. This does not wait for the timeout.
        flags: the locking flags. Note that shared locks
            (``LockFlags.SHARED``) on Windows require the optional
            ``pywin32`` package (``pip install "portalocker[win32]"``);
            without it, acquiring a shared lock raises ``ImportError``.
        raise_on_release_error: raise cleanup errors after both unlocking and
            closing have been attempted. Disabled by default for compatibility.
        **file_open_kwargs: The kwargs for the `open(...)` call

    fail_when_locked is useful when multiple threads/processes can race
    when creating a file. If set to true than the system will wait till
    the lock was acquired and then return an AlreadyLocked exception.

    Note that the file is opened first and locked later. So using 'w' as
    mode will result in truncate _BEFORE_ the lock is checked.
    """

    fh: types.IO | None
    filename: str
    mode: str
    truncate: bool
    timeout: float
    check_interval: float
    fail_when_locked: bool
    flags: constants.LockFlags
    raise_on_release_error: bool
    file_open_kwargs: dict[str, typing.Any]

    def __init__(
        self,
        filename: Filename,
        mode: Mode = 'a',
        timeout: float | None = None,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
        fail_when_locked: bool = DEFAULT_FAIL_WHEN_LOCKED,
        flags: constants.LockFlags = LOCK_METHOD,
        *,
        raise_on_release_error: bool = False,
        **file_open_kwargs: typing.Any,
    ) -> None:
        if 'w' in mode:
            truncate = True
            mode = typing.cast(Mode, mode.replace('w', 'a'))
        else:
            truncate = False

        if timeout is None:
            timeout = DEFAULT_TIMEOUT
        elif not (flags & constants.LockFlags.NON_BLOCKING):
            warnings.warn(
                'timeout has no effect in blocking mode',
                stacklevel=1,
            )  # pragma: nt-no-pywin32

        self.fh = None
        self.filename = str(filename)
        self.mode = mode
        self.truncate = truncate
        self.flags = flags
        self.raise_on_release_error = raise_on_release_error
        self.file_open_kwargs = file_open_kwargs
        super().__init__(timeout, check_interval, fail_when_locked)

    def acquire(
        self,
        timeout: float | None = None,
        check_interval: float | None = None,
        fail_when_locked: bool | None = None,
    ) -> typing.IO[typing.Any]:
        """Acquire the locked filehandle"""

        fail_when_locked = coalesce(fail_when_locked, self.fail_when_locked)

        if (
            not (self.flags & constants.LockFlags.NON_BLOCKING)
            and timeout is not None
        ):
            warnings.warn(
                'timeout has no effect in blocking mode',
                stacklevel=1,
            )  # pragma: nt-no-pywin32

        # If we already have a filehandle, return it
        fh = self.fh
        if fh:
            return fh

        # Get a new filehandler
        fh = self._get_fh()

        def try_close() -> None:  # pragma: no cover
            # Silently try to close the handle if possible, ignore all issues
            if fh is not None:
                with contextlib.suppress(Exception):
                    fh.close()

        exception = None
        # Try till the timeout has passed
        for _ in self._timeout_generator(timeout, check_interval):
            exception = None
            try:
                # Try to lock
                fh = self._get_lock(fh)
                break
            except exceptions.LockException as exc:
                # Python will automatically remove the variable from memory
                # unless you save it in a different location
                exception = exc

                # We already tried to the get the lock
                # If fail_when_locked is True, stop trying
                if fail_when_locked:
                    try_close()
                    raise exceptions.AlreadyLocked(exception) from exc
            except Exception as exc:
                # Something went wrong with the locking mechanism.
                # Wrap in a LockException and re-raise:
                try_close()
                raise exceptions.LockException(exc) from exc

            # Wait a bit

        if exception:
            try_close()
            # We got a timeout... reraising
            raise exception

        # Prepare the filehandle (truncate if needed)
        fh = self._prepare_fh(fh)

        self.fh = fh
        return fh

    def __enter__(self) -> typing.IO[typing.Any]:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: typing.Any,
    ) -> bool | None:
        if not self.raise_on_release_error or exc_value is None:
            self.release()
            return None

        try:
            self.release()
        except Exception as release_error:
            previous_context: BaseException | None = exc_value.__context__
            release_error.__context__ = previous_context
            exc_value.__context__ = release_error
            with contextlib.suppress(Exception):
                exc_value.add_note(  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
                    'portalocker release failed; see exception context',
                )
        return None

    def release(self) -> None:
        """Release the currently locked file handle."""
        fh = self.fh
        if fh:
            release_errors: list[Exception] = []
            # On Windows, closing the handle also releases the lock. Ensure we
            # always close, even if unlock raises due to edge cases when
            # preparing/restoring file position.
            try:
                try:
                    portalocker.unlock(fh)
                except Exception as exception:
                    release_errors.append(exception)
            finally:
                try:
                    fh.close()
                except Exception as exception:
                    release_errors.append(exception)
                self.fh = None

            if self.raise_on_release_error and release_errors:
                primary_error: Exception = release_errors[0]
                if len(release_errors) > 1:
                    raise primary_error from release_errors[1]
                raise primary_error

    def _get_fh(self) -> types.IO:
        """Get a new filehandle"""
        return typing.cast(
            types.IO,
            open(  # noqa: SIM115
                self.filename,
                self.mode,
                **self.file_open_kwargs,
            ),
        )

    def _get_lock(self, fh: types.IO) -> types.IO:
        """
        Try to lock the given filehandle

        returns LockException if it fails"""
        portalocker.lock(fh, self.flags)
        return fh

    def _prepare_fh(self, fh: types.IO) -> types.IO:
        """
        Prepare the filehandle for usage

        If truncate is a number, the file will be truncated to that amount of
        bytes
        """
        if self.truncate:
            fh.seek(0)
            fh.truncate(0)

        return fh


class RLock(Lock):
    """
    A reentrant lock, functions in a similar way to threading.RLock in that it
    can be acquired multiple times.  When the corresponding number of release()
    calls are made the lock will finally release the underlying file lock.
    """

    def __init__(
        self,
        filename: Filename,
        mode: Mode = 'a',
        timeout: float = DEFAULT_TIMEOUT,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
        fail_when_locked: bool = False,
        flags: constants.LockFlags = LOCK_METHOD,
    ) -> None:
        super().__init__(
            filename,
            mode,
            timeout,
            check_interval,
            fail_when_locked,
            flags,
        )
        self._acquire_count = 0

    def acquire(
        self,
        timeout: float | None = None,
        check_interval: float | None = None,
        fail_when_locked: bool | None = None,
    ) -> typing.IO[typing.Any]:
        fh: typing.IO[typing.Any]
        if self._acquire_count >= 1:
            assert self.fh is not None
            fh = self.fh
        else:
            fh = super().acquire(timeout, check_interval, fail_when_locked)
        self._acquire_count += 1
        return fh

    def release(self) -> None:
        if self._acquire_count == 0:  # pragma: no branch - covered by tests
            raise exceptions.LockException(
                'Cannot release more times than acquired',
            )

        if self._acquire_count == 1:  # pragma: no branch - trivial guard
            super().release()
        self._acquire_count -= 1


def _fh_matches_path(fh: types.IO, filename: str) -> bool:  # pragma: not-posix
    """Return whether ``fh`` still refers to the file now at ``filename``.

    A competing releaser can unlink (and a third party recreate) ``filename``
    in the window between our ``open`` and our lock, which would leave two
    processes each holding a lock on a *different* inode for the same name
    (split-brain). Comparing the handle's inode with the path's inode detects
    that swap. This is a POSIX-only concern: on Windows a locked file cannot be
    unlinked, so no swap is possible.
    """
    try:
        return os.fstat(fh.fileno()).st_ino == os.stat(filename).st_ino
    except FileNotFoundError:
        # The path was unlinked and not (yet) recreated.
        return False


class TemporaryFileLock(Lock):
    def __init__(
        self,
        filename: str = '.lock',
        timeout: float = DEFAULT_TIMEOUT,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
        fail_when_locked: bool = True,
        flags: constants.LockFlags = LOCK_METHOD,
    ) -> None:
        super().__init__(
            filename=filename,
            mode='w',
            timeout=timeout,
            check_interval=check_interval,
            fail_when_locked=fail_when_locked,
            flags=flags,
        )
        # Avoid keeping a strong reference to self, otherwise GC can't
        # collect and tests expecting deletion won't pass.
        wr = weakref.ref(self)

        def _finalize_release(
            ref: typing.Callable[[], TemporaryFileLock | None] = wr,
        ) -> None:  # pragma: no cover - best effort
            obj = ref()
            if obj is not None:
                with contextlib.suppress(Exception):
                    obj.release()

        atexit.register(_finalize_release)

    def acquire(
        self,
        timeout: float | None = None,
        check_interval: float | None = None,
        fail_when_locked: bool | None = None,
    ) -> typing.IO[typing.Any]:
        """Acquire the lock, guarding against split-brain path swaps."""
        return self._acquire_verified(
            self,
            self.filename,
            timeout,
            check_interval,
            fail_when_locked,
        )

    @staticmethod
    def _acquire_verified(
        lock: Lock,
        filename: str,
        timeout: float | None,
        check_interval: float | None,
        fail_when_locked: bool | None,
    ) -> typing.IO[typing.Any]:
        """Acquire ``lock`` and confirm the handle still names ``filename``.

        A competing releaser can unlink (and a third party recreate)
        ``filename`` between our ``open`` and our lock, so two processes could
        each hold a lock on a different inode for the same name. After locking
        we verify the handle still points at the current path; on a mismatch we
        drop the stale handle and re-acquire, bounded by the timeout (no
        unbounded spin). No-op on Windows, where a locked file cannot be
        swapped.

        Shared by ``TemporaryFileLock`` and the ``PidFileLock`` sidecar lock so
        both surfaces get the same guarantee.
        """
        for _ in lock._timeout_generator(timeout, check_interval):
            fh = Lock.acquire(lock, timeout, check_interval, fail_when_locked)
            if os.name == 'nt':  # Windows: a locked file can't be swapped.
                return fh  # pragma: not-nt
            if _fh_matches_path(fh, filename):  # pragma: not-posix
                return fh  # pragma: not-posix
            # Stale handle: the path was unlinked+recreated behind our back.
            Lock.release(lock)  # pragma: not-posix
        raise exceptions.AlreadyLocked(  # pragma: not-posix
            exceptions.LockException.LOCK_FAILED,
            f'{filename!r} kept being replaced while locking (split-brain)',
        )

    def release(self) -> None:
        """Release the file lock and remove the temporary file.

        On POSIX the file is unlinked while the lock is *still held*, so a
        competing acquirer cannot grab the freshly created path in the window
        between unlock and unlink (split-brain). On Windows an open/locked
        file cannot be unlinked, so there we unlock and close first, then
        remove with a short retry for AV/scanner share violations.

        Releasing an object that holds nothing is a no-op: a stale object
        (double release, or garbage collection of a failed acquire calling
        ``__del__``) must never unlink the path out from under the current
        holder.
        """
        if self.fh is None:
            # Not holding the lock; the path (if any) belongs to another
            # holder now.
            return
        if os.name == 'nt':  # pragma: no cover
            Lock.release(self)
            if os.path.isfile(self.filename):
                for _ in range(5):
                    try:
                        os.unlink(self.filename)
                        break
                    except PermissionError:
                        time.sleep(0.05)
                    except FileNotFoundError:
                        break
        else:  # pragma: not-posix
            # Unlink first, while we still hold the lock, then unlock+close.
            # The unlock must run even when the unlink fails (e.g. a
            # PermissionError from a read-only directory), otherwise the
            # error would leave the lock held forever.
            try:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.filename)
            finally:
                Lock.release(self)


class PidFileLock(TemporaryFileLock):
    """
    A lock that writes the current process PID to the file and can read
    the PID of the process that currently holds the lock.

    When used as a context manager:
    - Returns None if we successfully acquired the lock
    - Returns the PID (int) if another process holds the lock
    """

    def __init__(
        self,
        filename: str = '.pid',
        timeout: float = DEFAULT_TIMEOUT,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
        fail_when_locked: bool = True,
        flags: constants.LockFlags = LOCK_METHOD,
    ) -> None:
        super().__init__(
            filename=filename,
            timeout=timeout,
            check_interval=check_interval,
            fail_when_locked=fail_when_locked,
            flags=flags,
        )
        self._acquired_lock = False
        # Use a sidecar file for the actual OS-level lock so the PID file
        # remains readable on platforms (notably Windows) with mandatory
        # byte-range locking. This preserves existing public API/behavior.
        self._lockfile = f'{self.filename}.lock'
        self._inner_lock: Lock | None = None

    def _write_pid(self) -> None:
        """Publish the current PID and preserve operation errors on close."""
        pid_file: typing.TextIO = open(  # noqa: SIM115
            self.filename,
            'a+',
            encoding='ascii',
        )
        try:
            pid_file.seek(0)
            pid_file.truncate()
            pid_file.write(str(os.getpid()))
            pid_file.flush()
            try:
                os.fsync(pid_file.fileno())
            except OSError as error:
                if error.errno not in (errno.EINVAL, errno.ENOTSUP):
                    raise
        except Exception as error:
            try:
                pid_file.close()
            except Exception as close_error:
                raise error from close_error
            raise
        pid_file.close()

    def _rollback_failed_acquire(
        self,
        inner_lock: Lock,
    ) -> Exception | None:
        """Release a failed sidecar and return any secondary cleanup error.

        ``Lock.release`` currently clears its handle without raising. Keep the
        fallback close so rollback remains safe if that contract changes or a
        custom/monkeypatched release exits early.
        """
        cleanup_error: Exception | None = None
        try:
            inner_lock.release()
        except Exception as error:
            cleanup_error = error

        fh: types.IO | None = inner_lock.fh
        if fh is not None:
            try:
                fh.close()
            except Exception as close_error:
                if cleanup_error is None:
                    cleanup_error = close_error
                else:
                    cleanup_error.__cause__ = close_error
            finally:
                inner_lock.fh = None

        self._inner_lock = None
        self._acquired_lock = False
        return cleanup_error

    def acquire(
        self,
        timeout: float | None = None,
        check_interval: float | None = None,
        fail_when_locked: bool | None = None,
    ) -> typing.IO[typing.Any]:
        """Acquire the lock and write the current PID to the file"""
        fail_when_locked = coalesce(fail_when_locked, self.fail_when_locked)

        # Acquire the sidecar lock file using a normal Lock instance.
        inner_lock = Lock(
            self._lockfile,
            mode='a',
            timeout=timeout if fail_when_locked is False else 0,
            check_interval=coalesce(
                check_interval if fail_when_locked is False else 0.0,
                DEFAULT_CHECK_INTERVAL,
            ),
            fail_when_locked=True,
            flags=LOCK_METHOD,
        )
        self._inner_lock = inner_lock
        try:
            # Reuse the split-brain guard so the sidecar lock gets the same
            # inode-verification as a direct `TemporaryFileLock`.
            self._acquire_verified(
                inner_lock,
                self._lockfile,
                timeout,
                check_interval,
                fail_when_locked,
            )
        except Exception as exc:
            # Don't leak the (failed) sidecar reference on any error.
            self._inner_lock = None
            # `fail_when_locked=True` raises `AlreadyLocked` on the first
            # contention, while a timed-out `fail_when_locked=False` acquire
            # re-raises the last plain `LockException` - from contention or
            # from repeated lock failures (e.g. ENOLCK, NFS quirks). Normalize
            # any plain `LockException` to `AlreadyLocked` so `__enter__` and
            # callers see one predictable surface; anything else propagates.
            if isinstance(exc, exceptions.LockException) and not isinstance(
                exc,
                exceptions.AlreadyLocked,
            ):
                raise exceptions.AlreadyLocked(*exc.args) from exc
            raise

        try:
            self._write_pid()
        except Exception as error:
            cleanup_error: Exception | None = self._rollback_failed_acquire(
                inner_lock,
            )
            if cleanup_error is not None:
                publication_cause: BaseException | None = error.__cause__
                if publication_cause is not None:
                    cause_tail: BaseException = cleanup_error
                    while cause_tail.__cause__ is not None:
                        cause_tail = cause_tail.__cause__
                    cause_tail.__cause__ = publication_cause
                raise error from cleanup_error
            raise

        self._acquired_lock = True
        # No need to keep a direct fh on the PID file; return the lock's fh
        # to satisfy the context manager typing contract.
        assert inner_lock.fh is not None
        return inner_lock.fh

    def read_pid(self) -> int | None:
        """Read the PID from the lock file if it exists and is readable"""
        try:
            if os.path.exists(self.filename):
                with open(self.filename) as f:
                    content = f.read().strip()
                    if content:
                        return int(content)
        except (ValueError, OSError):
            pass
        return None

    def fail_closed(self) -> contextlib.AbstractContextManager[None]:
        """Return a context that enters only after acquiring this lock.

        :raises AlreadyLocked: if another process holds the lock. Its
            ``holder_pid`` attribute contains the competing PID when readable.
        """
        return _PidFileLockFailClosedContext(self)

    # `PidFileLock` deliberately breaks the `Lock.__enter__` contract: it
    # reports the competing PID instead of returning a filehandle.
    def __enter__(self) -> int | None:  # type: ignore[override]  # ty: ignore[invalid-method-override]
        """
        Context manager entry that returns:
        - None if we successfully acquired the lock
        - PID (int) if another process holds the lock
        """
        try:
            self.acquire()
        except exceptions.AlreadyLocked:
            # Another process holds the lock, try to read its PID
            return self.read_pid()

        return None  # We successfully acquired the lock

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: typing.Any,
    ) -> bool | None:
        if self._acquired_lock:  # pragma: no branch - trivial guard
            self.release()
            self._acquired_lock = False
        return None

    def release(self) -> None:
        """Release the sidecar lock and remove the PID + sidecar files.

        On POSIX both the PID file and the sidecar lock file are unlinked while
        the sidecar lock is *still held*, so a competing acquirer cannot grab
        the sidecar path in the window between unlock and unlink (split-brain).
        The PID file itself carries no OS lock (the sidecar holds it), but it
        is removed in the same held window for consistency. On Windows the
        locked sidecar cannot be unlinked, so it is released first and removed
        after.

        Releasing an object that does not hold the sidecar is a no-op: a
        stale object (double release, or garbage collection of a failed
        acquire calling ``__del__``) must never unlink the PID or sidecar
        files out from under the current holder.
        """
        inner_lock = self._inner_lock
        if inner_lock is None:
            # Not holding the sidecar; the files belong to another holder.
            return
        if os.name == 'nt':  # pragma: no cover
            self._inner_lock = None
            with contextlib.suppress(Exception):
                inner_lock.release()
            with contextlib.suppress(Exception):
                os.unlink(self.filename)
            with contextlib.suppress(Exception):
                if os.path.isfile(self._lockfile):
                    os.unlink(self._lockfile)
        else:  # pragma: not-posix
            # Unlink both paths while the sidecar lock is still held. The
            # sidecar unlock must run even when an unlink fails (e.g. a
            # PermissionError from a read-only directory), otherwise the
            # error would leave the sidecar held forever. `_inner_lock` is
            # only cleared once the unlock actually runs, so a failed
            # release keeps its reference and can be retried.
            try:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.filename)
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self._lockfile)
            finally:
                self._inner_lock = None
                with contextlib.suppress(Exception):
                    inner_lock.release()


class _PidFileLockFailClosedContext(
    contextlib.AbstractContextManager[None],
):
    """Fail-closed context adapter for :class:`PidFileLock`."""

    def __init__(self, lock: PidFileLock) -> None:
        self._lock: PidFileLock = lock

    def __enter__(self) -> None:
        try:
            self._lock.acquire()
        except exceptions.AlreadyLocked as exc:
            exc.holder_pid = self._lock.read_pid()
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: typing.Any,
    ) -> bool | None:
        return self._lock.__exit__(exc_type, exc_value, traceback)


class BoundedSemaphore(LockBase['Lock | None']):
    """
    Bounded semaphore to prevent too many parallel processes from running

    This method is deprecated because multiple processes that are completely
    unrelated could end up using the same semaphore.  To prevent this,
    use `NamedBoundedSemaphore` instead. The
    `NamedBoundedSemaphore` is a drop-in replacement for this class.

    >>> semaphore = BoundedSemaphore(2, directory='')
    >>> str(semaphore.get_filenames()[0])
    'bounded_semaphore.00.lock'
    >>> str(sorted(semaphore.get_random_filenames())[1])
    'bounded_semaphore.01.lock'
    """

    lock: Lock | None

    def __init__(
        self,
        maximum: int,
        name: str = 'bounded_semaphore',
        filename_pattern: str = '{name}.{number:02d}.lock',
        directory: str = tempfile.gettempdir(),
        timeout: float | None = DEFAULT_TIMEOUT,
        check_interval: float | None = DEFAULT_CHECK_INTERVAL,
        fail_when_locked: bool | None = True,
    ) -> None:
        self.maximum = maximum
        self.name = name
        self.filename_pattern = filename_pattern
        self.directory = directory
        self.lock = None
        super().__init__(
            timeout=timeout,
            check_interval=check_interval,
            fail_when_locked=fail_when_locked,
        )

        if not name or name == 'bounded_semaphore':
            warnings.warn(
                '`BoundedSemaphore` without an explicit `name` '
                'argument is deprecated, use NamedBoundedSemaphore',
                DeprecationWarning,
                stacklevel=1,
            )

    def get_filenames(self) -> typing.Sequence[pathlib.Path]:
        return [self.get_filename(n) for n in range(self.maximum)]

    def get_random_filenames(self) -> typing.Sequence[pathlib.Path]:
        filenames = list(self.get_filenames())
        random.shuffle(filenames)
        return filenames

    def get_filename(self, number: int) -> pathlib.Path:
        return pathlib.Path(self.directory) / self.filename_pattern.format(
            name=self.name,
            number=number,
        )

    def acquire(
        self,
        timeout: float | None = None,
        check_interval: float | None = None,
        fail_when_locked: bool | None = None,
    ) -> Lock | None:
        assert not self.lock, 'Already locked'

        filenames = self.get_filenames()

        for n in self._timeout_generator(timeout, check_interval):
            logger.debug('trying lock (attempt %d) %r', n, filenames)
            # no branch
            if self.try_lock(filenames):  # pragma: no branch
                return self.lock  # pragma: no cover

        if fail_when_locked := coalesce(
            fail_when_locked,
            self.fail_when_locked,
        ):
            raise exceptions.AlreadyLocked()

        return None

    def try_lock(self, filenames: typing.Sequence[Filename]) -> bool:
        filename: Filename
        for filename in filenames:
            logger.debug('trying lock for %r', filename)
            lock = Lock(filename, fail_when_locked=True)
            try:
                lock.acquire()
            except exceptions.AlreadyLocked:
                # Taken by someone else; try the next candidate file.
                continue
            except Exception:
                # Any other failure (e.g. a missing directory raising
                # `FileNotFoundError` from the underlying `open`) must not
                # leave a half-set lock behind, otherwise the
                # `assert not self.lock` guard in `acquire` would brick the
                # instance on the next call. Reset and propagate.
                self.lock = None
                raise
            else:
                # Only record the lock once it is actually held.
                self.lock = lock
                logger.debug('locked %r', filename)
                return True

        return False

    def release(self) -> None:  # pragma: no cover
        if self.lock is not None:
            self.lock.release()
            self.lock = None


class NamedBoundedSemaphore(BoundedSemaphore):
    """
    Bounded semaphore to prevent too many parallel processes from running

    It's also possible to specify a timeout when acquiring the lock to wait
    for a resource to become available.  This is very similar to
    `threading.BoundedSemaphore` but works across multiple processes and across
    multiple operating systems.

    Because this works across multiple processes it's important to give the
    semaphore a name.  This name is used to create the lock files.  If you
    don't specify a name, a random name will be generated.  This means that
    you can't use the same semaphore in multiple processes unless you pass the
    semaphore object to the other processes.

    >>> semaphore = NamedBoundedSemaphore(2, name='test')
    >>> str(semaphore.get_filenames()[0])
    '...test.00.lock'

    >>> semaphore = NamedBoundedSemaphore(2)
    >>> 'bounded_semaphore' in str(semaphore.get_filenames()[0])
    True

    """

    def __init__(
        self,
        maximum: int,
        name: str | None = None,
        filename_pattern: str = '{name}.{number:02d}.lock',
        directory: str = tempfile.gettempdir(),
        timeout: float | None = DEFAULT_TIMEOUT,
        check_interval: float | None = DEFAULT_CHECK_INTERVAL,
        fail_when_locked: bool | None = True,
    ) -> None:
        if name is None:
            name = f'bounded_semaphore.{random.randint(0, 1000000):d}'
        super().__init__(
            maximum,
            name,
            filename_pattern,
            directory,
            timeout,
            check_interval,
            fail_when_locked,
        )
