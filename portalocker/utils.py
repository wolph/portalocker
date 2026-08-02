"""High level locking utilities built on the low level lockers.

Where `portalocker.portalocker` wraps a single locking syscall, this module
turns that primitive into ready-to-use objects: context managers that open,
lock, retry, unlock and close a file for you.

The hierarchy:

- `LockBase`: the abstract base. It owns the ``timeout`` /
  ``check_interval`` / ``fail_when_locked`` retry semantics that every other
  class here inherits, and the `LockBase._timeout_generator` that implements
  them.
- `Lock`: the workhorse. Opens a file, locks it, and hands the filehandle
  to the caller.
- `RLock`: a `Lock` that may be acquired several times by the same
  instance and is only released once the acquire count drops to zero.
- `TemporaryFileLock`: a `Lock` whose lock file is unlinked on release,
  including at interpreter exit.
- `PidFileLock`: a `TemporaryFileLock` that publishes the owning PID, so a
  contender can report *who* holds the lock instead of merely failing.
- `BoundedSemaphore` / `NamedBoundedSemaphore`: N-slot counting
  semaphores built from N separate lock files.

`open_atomic` is unrelated to locking: it writes to a temporary file and
renames it into place, so readers never observe a partially written file.

Only `Lock` and `open_atomic` are listed in this module's ``__all__``, but
every concrete class above is re-exported from the `portalocker` package
itself. The abstract `LockBase` is not; import it from here.

Example:
    >>> import portalocker
    >>> with portalocker.Lock('somefile', 'w', timeout=1) as fh:
    ...     _ = fh.write('the file is locked for the duration of the block')

See Also:
    `portalocker.portalocker`: the platform specific locking primitives
    this module builds on.
"""

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
    """Abstract base class for every lock in portalocker.

    It contributes two things to its subclasses: the retry policy stored in
    the three attributes below, and the `LockBase._timeout_generator` that
    turns that policy into a sequence of attempts. Subclasses only have to
    implement `acquire` and `release`; the context manager protocol, the
    descriptor hook and the garbage collection fallback come for free.

    The class is generic over `AcquireReturnT`, the type `acquire` returns
    and therefore the type bound by ``with``. `Lock` and its descendants
    specialize it to an open filehandle, `RedisLock` to itself and
    `BoundedSemaphore` to the `Lock` it took, or `None` when it gave up
    without one.

    Attributes:
        timeout: Total number of seconds `acquire` keeps retrying a lock
            that is held by somebody else, before giving up and raising.
            Defaults to `DEFAULT_TIMEOUT` (5 seconds). The clock starts
            *after* the first attempt, so a timeout of ``0`` still means
            "try exactly once". Retrying only makes sense for non-blocking
            lock flags: with a blocking flag the operating system itself
            waits inside the very first attempt, which is why `Lock` warns
            when a timeout is combined with blocking flags.
        check_interval: Seconds between two attempts while waiting for
            `timeout` to expire. Defaults to `DEFAULT_CHECK_INTERVAL`
            (0.25 seconds). It is a target rather than a plain sleep: the
            time the attempt itself took is subtracted, see
            `LockBase._timeout_generator`.
        fail_when_locked: Whether to give up as soon as the *first* attempt
            finds the lock taken, instead of retrying until `timeout`
            expires. Defaults to `DEFAULT_FAIL_WHEN_LOCKED` (`False`,
            i.e. retry). Note that it changes the exception as well as the
            timing: a failure with `fail_when_locked` raises
            `AlreadyLocked`, whereas exhausting the timeout re-raises the
            last `LockException` from the underlying locking call. The
            distinction matters when several processes race to create the
            same file and you would rather hear about the contention
            immediately than a handful of seconds later.

    Note:
        Every one of the three is also a per-call argument of `acquire`.
        The argument wins when it is not `None`; otherwise the attribute
        set here is used.

    See Also:
        `Lock`: the file based implementation nearly everything else in
        this module derives from.
    """

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
        """Store the retry policy shared by every lock.

        Args:
            timeout: Value for the `timeout` attribute. `None` selects
                `DEFAULT_TIMEOUT`.
            check_interval: Value for the `check_interval` attribute.
                `None` selects `DEFAULT_CHECK_INTERVAL`.
            fail_when_locked: Value for the `fail_when_locked` attribute.
                `None` selects `DEFAULT_FAIL_WHEN_LOCKED`.

        Note:
            The defaults are resolved through `coalesce`, which uses an
            identity check. Passing ``0`` or ``False`` therefore keeps that
            value; only a literal `None` falls back to the default.
        """
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
    ) -> AcquireReturnT:
        """Take the lock, retrying until `timeout` expires.

        Abstract; see `Lock.acquire` for the reference implementation and
        `RLock.acquire`, `BoundedSemaphore.acquire` and `RedisLock.acquire`
        for the variations.

        Args:
            timeout: Overrides the `timeout` attribute for this call only.
            check_interval: Overrides the `check_interval` attribute for
                this call only.
            fail_when_locked: Overrides the `fail_when_locked` attribute
                for this call only.

        Returns:
            Whatever the concrete lock guards. For every file based lock in
            this module that is the open, locked filehandle.

        Raises:
            ~portalocker.exceptions.AlreadyLocked: The first attempt failed and
                `fail_when_locked` was set.
            ~portalocker.exceptions.LockException: The lock could not be taken
                before `timeout` expired, or the underlying locking call failed
                for a reason other than contention.
        """

    def _timeout_generator(
        self,
        timeout: float | None,
        check_interval: float | None,
    ) -> typing.Iterator[int]:
        """Yield an attempt number per lock attempt, pacing the retries.

        The generator is the whole retry policy. A caller loops over it and
        tries to lock once per iteration; the sleeping happens inside the
        generator, in between.

        Attempt ``0`` is yielded before the clock even starts, so a
        `timeout` of ``0`` still buys exactly one attempt. The sleep for an
        attempt happens after control returns from it, and it targets a
        fixed schedule instead of a fixed pause: attempt ``i`` sleeps until
        ``i * check_interval`` seconds have passed since the clock started,
        so a slow attempt eats into its own interval rather than adding to
        it. The sleep never drops below a millisecond, which keeps a
        ``check_interval`` of ``0`` from spinning the CPU flat out.

        One consequence worth knowing: because the sleep trails the yield,
        attempts ``0`` and ``1`` both happen right away, and only from
        there on are attempts spaced out. Iteration stops as soon as the
        deadline has passed, so the caller decides what a final failure
        means; the generator itself never raises.

        Args:
            timeout: Seconds to keep retrying. `None` uses the `timeout`
                attribute.
            check_interval: Seconds between attempts. `None` uses the
                `check_interval` attribute.

        Yields:
            The attempt number, starting at ``0``.
        """
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
    def release(self) -> None:
        """Give up the lock.

        Abstract; the implementations differ in how much they clean up.
        `Lock.release` unlocks and closes the filehandle,
        `TemporaryFileLock.release` also unlinks the lock file, and
        `RLock.release` only does either once the acquire count reaches
        zero.

        Implementations are expected to tolerate being called on an
        instance that holds nothing, because `__del__` calls this on every
        lock that is garbage collected.
        """

    def __enter__(self) -> AcquireReturnT:
        """Acquire the lock with the instance defaults.

        Returns:
            Whatever `acquire` returns, which is the value bound by
            ``as`` in a ``with`` statement.
        """
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: typing.Any,  # Should be typing.TracebackType
    ) -> bool | None:
        """Release the lock when the ``with`` block ends.

        Args:
            exc_type: Type of the exception leaving the block, if any.
            exc_value: The exception instance, if any.
            traceback: The traceback of that exception, if any.

        Returns:
            `None`, so an exception raised inside the block keeps
            propagating once the lock has been released.
        """
        self.release()
        return None

    def __delete__(self, instance: LockBase[AcquireReturnT]) -> None:
        """Release a lock that is deleted through the descriptor protocol.

        Python routes ``del owner.attribute`` here when a lock instance is
        stored as a class attribute of ``owner``, which lets a lock be
        released by deleting the attribute that holds it. The argument is
        the object the attribute was deleted from, and it is that object's
        `release` which gets called.

        Args:
            instance: The object the attribute was deleted from.
        """
        instance.release()

    # Ensure cleanup on garbage collection as tests rely on this behaviour
    def __del__(self) -> None:  # pragma: no cover - best effort cleanup
        """Release the lock when the object is garbage collected.

        A safety net for locks that are dropped without ever being
        released, and the mechanism behind `TemporaryFileLock` removing its
        lock file once the last reference goes away. Every exception is
        suppressed: raising from a finalizer only prints an ignored
        traceback, and by this point there may not be much of an
        interpreter left to clean up with.

        Do not rely on it for correctness. It is best effort, it runs at an
        unpredictable moment on implementations without reference counting
        such as PyPy, and it may not run at all if the interpreter exits
        abruptly.
        """
        with contextlib.suppress(Exception):
            self.release()


class Lock(LockBase[typing.IO[typing.Any]]):
    """Lock manager with built-in timeout.

    The class most users want. It opens `filename`, locks it, retries for
    as long as the inherited retry policy allows, and hands the open
    filehandle to the caller. Releasing unlocks and closes that handle; the
    file itself stays behind, which is what makes the lock usable as a
    plain data file as well as a mutex.

    Example:
        >>> import portalocker
        >>> with portalocker.Lock('somefile', 'w', timeout=1) as fh:
        ...     _ = fh.write('locked while the block runs')

    Warning:
        The file is opened before it is locked, so letting `open` truncate
        would discard another holder's data *before* anybody checks whether
        the lock is free. That is why a mode containing ``w`` is silently
        turned into ``a`` and the truncation is deferred to
        `Lock._prepare_fh`, which runs only after the lock has been taken.

    Note:
        Locking is per open filehandle, not per process. Two `Lock`
        instances on the same path in the same process do contend with each
        other, which is what makes single process examples and tests
        meaningful.

    See Also:
        `LockBase`: documents the ``timeout``, ``check_interval`` and
        ``fail_when_locked`` retry semantics shared by every lock.
        `RLock`: the reentrant variant.
        `TemporaryFileLock`: removes the lock file on release.
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
        """Configure the lock; nothing is opened or locked yet.

        Args:
            filename: Path of the file to lock. Anything `str` accepts,
                including a `pathlib.Path`. It is stored as a string.
            mode: Open mode for the file. Use ``'a'`` or ``'ab'`` to write.
                A mode containing ``w`` is rewritten to ``a`` and the
                truncation is postponed until the lock has been taken, see
                the class warning.
            timeout: See `LockBase`. `None` selects `DEFAULT_TIMEOUT`.
            check_interval: See `LockBase`.
            fail_when_locked: See `LockBase`.
            flags: Locking flags, defaulting to
                ``LockFlags.EXCLUSIVE | LockFlags.NON_BLOCKING``. Shared
                locks (``LockFlags.SHARED``) on Windows require the
                optional ``pywin32`` package
                (``pip install "portalocker[win32]"``). Without it
                `acquire` fails with a `LockException` wrapping the
                ``ImportError`` that names the extra; the bare
                ``ImportError`` only escapes `portalocker.lock`, which
                does no wrapping.
            raise_on_release_error: Report errors that happen while
                unlocking or closing in `release`, instead of swallowing
                them. Off by default for backwards compatibility.
            **file_open_kwargs: Passed straight through to `open`, for
                things like ``encoding`` or ``buffering``.

        Warns:
            UserWarning: An explicit `timeout` was combined with blocking
                `flags`, i.e. flags without ``LockFlags.NON_BLOCKING``.
                The operating system blocks inside the first attempt in
                that case, so there is nothing left for the timeout to do.
        """
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
        """Open the file, lock it and return the filehandle.

        Calling this on a lock that is already held is cheap and safe: the
        filehandle taken earlier is returned as is, without touching the
        operating system.

        Args:
            timeout: Overrides `timeout` for this call. See `LockBase`.
            check_interval: Overrides `check_interval` for this call.
            fail_when_locked: Overrides `fail_when_locked` for this call.

        Returns:
            The open, locked filehandle. It is stored on the instance as
            well, and stays valid until `release`.

        Raises:
            ~portalocker.exceptions.AlreadyLocked: The first attempt found the
                file locked and `fail_when_locked` was set.
            ~portalocker.exceptions.LockException: Retrying did not help and
                `timeout` expired; the exception is the last one the locking
                call produced. Also raised, wrapping the original, when
                something other than contention goes wrong, such as the locking
                backend refusing the flags.
            OSError: Opening the file failed, for instance because the
                directory does not exist or the mode is not permitted.
                Only locking failures are translated; failures from `open`
                propagate untouched.

        Warns:
            UserWarning: A `timeout` was passed while the lock uses
                blocking flags, where it has no effect.

        Example:
            >>> import portalocker
            >>> lock = portalocker.Lock('somefile', timeout=1)
            >>> fh = lock.acquire()
            >>> fh is lock.acquire()
            True
            >>> lock.release()
        """
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
            """Close the filehandle opened above, ignoring any failure.

            Every path that leaves `acquire` without a lock runs this
            first, so a failed acquire does not leak the descriptor it
            opened. It deliberately stays quiet: the caller is already on
            its way to raising something more interesting than whatever
            `close` might complain about.
            """
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
        """Acquire the lock and return the filehandle to bind with ``as``.

        Returns:
            The open, locked filehandle, exactly as `acquire` returns it.

        Example:
            >>> import portalocker
            >>> with portalocker.Lock('somefile', 'a', timeout=1) as fh:
            ...     _ = fh.write('text')
        """
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: typing.Any,
    ) -> bool | None:
        """Release the lock, preserving an exception from the block.

        Overrides `LockBase.__exit__` for one reason: when
        ``raise_on_release_error`` is set and the block is already leaving
        with an exception, a second failure during `release` must not
        replace the first. The release error is chained onto the original
        as its ``__context__`` and a note is attached, so both remain
        visible in the traceback while the original is what propagates.

        Args:
            exc_type: Type of the exception leaving the block, if any.
            exc_value: The exception instance, if any.
            traceback: The traceback of that exception, if any.

        Returns:
            `None`; exceptions from the block are never suppressed.

        Raises:
            Exception: Whatever `release` raises, but only when
                ``raise_on_release_error`` is set *and* the block itself
                ended without an exception.
        """
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
        """Unlock and close the file handle, if this instance holds one.

        Doing nothing when no lock is held is deliberate: `__del__` calls
        this on every lock that is collected. Unlocking and closing are
        both always attempted and the stored handle is always cleared, even
        when one of the two fails.

        Raises:
            Exception: Only when the lock was built with
                ``raise_on_release_error=True``. The first failure of the
                unlock and close pair is raised, chained from the second
                if both failed. By default such failures are swallowed.
        """
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
        """Open the file and return the new, still unlocked filehandle."""
        return typing.cast(
            types.IO,
            open(  # noqa: SIM115
                self.filename,
                self.mode,
                **self.file_open_kwargs,
            ),
        )

    def _get_lock(self, fh: types.IO) -> types.IO:
        """Lock `fh` with the configured flags.

        Args:
            fh: The filehandle returned by `Lock._get_fh`.

        Returns:
            The same filehandle, now locked.

        Raises:
            ~portalocker.exceptions.LockException: The file is locked by
                somebody else, or the locking call failed.
        """
        portalocker.lock(fh, self.flags)
        return fh

    def _prepare_fh(self, fh: types.IO) -> types.IO:
        """Make the locked filehandle ready for the caller.

        Truncation happens here rather than in `open`, so that a lock
        opened with mode ``w`` cannot discard the contents of a file
        somebody else is holding.

        Args:
            fh: The locked filehandle.

        Returns:
            The same filehandle, emptied and rewound when the lock was
            created with a truncating mode.
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

    Reentrancy is per instance, not per process: it is this object's own
    acquire count that is tracked, so a *second* `RLock` on the same file
    still contends with the first one.

    Example:
        >>> import portalocker
        >>> lock = portalocker.RLock('somefile')
        >>> fh = lock.acquire()
        >>> fh is lock.acquire()
        True
        >>> lock.release()
        >>> fh.closed
        False
        >>> lock.release()
        >>> fh.closed
        True

    See Also:
        `Lock`: the non-reentrant version and the source of every
        constructor argument.
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
        """Configure the lock and start with an acquire count of zero.

        Args:
            filename: Path of the file to lock.
            mode: Open mode for the file, see `Lock`.
            timeout: See `LockBase`.
            check_interval: See `LockBase`.
            fail_when_locked: See `LockBase`.
            flags: Locking flags, see `Lock`.

        Note:
            Unlike `Lock`, this constructor takes neither
            ``raise_on_release_error`` nor extra `open` keyword arguments.
        """
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
        """Take the lock, or note that this instance already has it.

        The first call locks the file through `Lock.acquire`. Every later
        call only bumps the acquire count and hands back the same
        filehandle, so the arguments are ignored once the lock is held.

        Args:
            timeout: Overrides `timeout` for the first call. See
                `LockBase`.
            check_interval: Overrides `check_interval` for the first call.
            fail_when_locked: Overrides `fail_when_locked` for the first
                call.

        Returns:
            The open, locked filehandle. The same object for every nested
            acquire.

        Raises:
            ~portalocker.exceptions.AlreadyLocked: As `Lock.acquire`, on the
                first call only.
            ~portalocker.exceptions.LockException: As `Lock.acquire`, on the
                first call only.
            OSError: As `Lock.acquire`, on the first call only.
        """
        fh: typing.IO[typing.Any]
        if self._acquire_count >= 1:
            assert self.fh is not None
            fh = self.fh
        else:
            fh = super().acquire(timeout, check_interval, fail_when_locked)
        self._acquire_count += 1
        return fh

    def release(self) -> None:
        """Drop one acquire, unlocking once the count reaches zero.

        Raises:
            ~portalocker.exceptions.LockException: Released more often than
                acquired. That is a bookkeeping error rather than contention,
                so it is reported rather than ignored, unlike the tolerant
                `Lock.release` this eventually delegates to.
        """
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
    """A `Lock` whose lock file only exists while the lock is held.

    Use it when the file is purely a mutex and leaving it behind would be
    litter. `release` unlinks the path, and so do the two fallbacks that
    catch a program which forgets to: `LockBase.__del__` when the object is
    collected, and an `atexit` handler registered by the constructor when
    the interpreter shuts down while the lock is still held.

    That handler holds a `weakref.ref` rather than the lock itself, so
    registering it does not keep the object alive; a lock that is collected
    earlier simply leaves the handler with nothing to do.

    Releasing an instance that does not hold the lock is a no-op. Without
    that rule a stale object, released twice or finalized after a failed
    acquire, would unlink the path out from under whoever holds the lock at
    that moment. Added in 4.0.0 as part of the split-brain fix (#115),
    together with the inode re-check in `acquire`.

    Example:
        >>> import os
        >>> import portalocker
        >>> lock = portalocker.TemporaryFileLock('somefile.lock')
        >>> _ = lock.acquire()
        >>> os.path.isfile('somefile.lock')
        True
        >>> lock.release()
        >>> os.path.isfile('somefile.lock')
        False
        >>> lock.release()

    See Also:
        `PidFileLock`: adds the owning PID to the file.
    """

    def __init__(
        self,
        filename: str = '.lock',
        timeout: float = DEFAULT_TIMEOUT,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
        fail_when_locked: bool = True,
        flags: constants.LockFlags = LOCK_METHOD,
    ) -> None:
        """Configure the lock and arm the interpreter exit cleanup.

        Args:
            filename: Path of the lock file, ``'.lock'`` by default. It is
                created on acquire and removed on release.
            timeout: See `LockBase`.
            check_interval: See `LockBase`.
            fail_when_locked: See `LockBase`. Defaults to `True` here,
                unlike `Lock`: a lock file that exists usually means a live
                owner, so failing straight away is the more useful answer.
            flags: Locking flags, see `Lock`.

        Note:
            The mode is fixed to ``'w'``, so the file is emptied once the
            lock has been taken.
        """
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
            """Release the lock at interpreter exit, if it still exists.

            Registered with `atexit` so a process that dies with the lock
            held still removes its lock file. The weak reference is passed
            as a default argument instead of being closed over, keeping the
            registration from pinning the lock in memory; once the lock has
            been collected the reference resolves to `None` and this does
            nothing. Errors are suppressed, since the interpreter is on its
            way out and nobody is left to handle them.

            Args:
                ref: The weak reference to the lock. Bound at registration
                    time; never pass this yourself.
            """
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

    The classic "only one instance of this daemon" lock. Two files are
    involved: `filename` holds the readable PID, and a sidecar
    ``<filename>.lock`` next to it carries the actual operating system
    lock. The split exists because Windows locking is mandatory, so a lock
    taken on the PID file itself would stop anyone from reading it.

    Example:
        >>> import os
        >>> import portalocker
        >>> with portalocker.PidFileLock('somefile.pid') as holder_pid:
        ...     holder_pid is None  # None means we are the holder
        True

    See Also:
        `PidFileLock.fail_closed`: for the common case where a contended
        lock should abort the block instead of running it.
    """

    def __init__(
        self,
        filename: str = '.pid',
        timeout: float = DEFAULT_TIMEOUT,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
        fail_when_locked: bool = True,
        flags: constants.LockFlags = LOCK_METHOD,
    ) -> None:
        """Configure the lock and derive the sidecar lock file name.

        Args:
            filename: Path of the PID file, ``'.pid'`` by default. The
                sidecar lock file is this path with ``.lock`` appended.
            timeout: See `LockBase`. Only has an effect together with
                ``fail_when_locked=False``.
            check_interval: See `LockBase`.
            fail_when_locked: See `LockBase`. Defaults to `True`, as for
                `TemporaryFileLock`.
            flags: Locking flags applied to the sidecar file, see `Lock`.

        Note:
            Neither file is created here; that happens on acquire.
        """
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
        """Lock the sidecar file and publish the current PID.

        Args:
            timeout: Overrides `timeout` for this call. See `LockBase`.
                Only used when `fail_when_locked` resolves to `False`.
            check_interval: Overrides `check_interval` for this call, under
                the same condition.
            fail_when_locked: Overrides `fail_when_locked` for this call.

        Returns:
            The filehandle of the sidecar lock file. It exists to satisfy
            the `Lock` typing contract; read the PID through
            `PidFileLock.read_pid` instead of from this handle.

        Raises:
            ~portalocker.exceptions.AlreadyLocked: Somebody else holds the
                lock. Every plain `LockException` from the sidecar is
                normalized to this, so callers have a single exception to catch
                whether the failure came from `fail_when_locked` or from an
                expired timeout.
            Exception: Anything else the sidecar `Lock` raises, such as an
                `OSError` from opening it, propagates unchanged. So does a
                failure to publish the PID, for instance because the
                filesystem is full; the sidecar lock is rolled back first,
                so that failure never leaves the lock held, and a rollback
                error of its own is chained onto the original.
        """
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
        """Read the PID from the lock file, if it exists and is readable.

        Returns:
            The PID recorded in the file, or `None` when the file is
            missing, empty, unreadable or does not contain a number. Note
            that a returned PID only says who *wrote* the file; the process
            may since have died.
        """
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

        The fail-closed counterpart of using the lock directly as a context
        manager: contention aborts before the block runs instead of running
        it with a PID in hand.

        Returns:
            A context manager that binds `None` and guarantees the block
            only runs while this process owns the lock.

        Raises:
            ~portalocker.exceptions.AlreadyLocked: On entry, when the lock
                could not be taken. Usually another process holds it, but
                `PidFileLock.acquire` also collapses every other plain
                `LockException` from the sidecar onto this type. Its
                ``holder_pid`` attribute carries the competing PID when it
                could be read.
            Exception: On entry, anything else `PidFileLock.acquire`
                raises, unchanged; the same pass-through class documented
                on `PidFileLock.__enter__`.

        Example:
            >>> import portalocker
            >>> lock = portalocker.PidFileLock('somefile.pid')
            >>> with lock.fail_closed():
            ...     print('exclusive work happens here')
            exclusive work happens here
        """
        return _PidFileLockFailClosedContext(self)

    # `PidFileLock` deliberately breaks the `Lock.__enter__` contract: it
    # reports the competing PID instead of returning a filehandle.
    def __enter__(self) -> int | None:  # type: ignore[override]  # ty: ignore[invalid-method-override]
        """Acquire the lock, reporting the competing PID on failure.

        Deliberately breaks the `Lock.__enter__` contract: instead of
        returning a filehandle it returns who is in the way, and contention
        is *not* an error here. The body of the ``with`` block therefore
        runs either way and has to check the value it was given.

        Returns:
            `None` when the lock was acquired, or the PID of the process
            holding it. The PID may also be `None` on contention, when the
            holder's PID cannot be read.

        Raises:
            Exception: Anything `acquire` raises other than
                `AlreadyLocked`; only contention is turned into a return
                value.

        Example:
            >>> import portalocker
            >>> lock = portalocker.PidFileLock('somefile.pid')
            >>> with lock as holder_pid:
            ...     if holder_pid is None:
            ...         print('acquired')
            ...     else:
            ...         print(f'held by {holder_pid}')
            acquired

        See Also:
            `PidFileLock.fail_closed`: refuses to enter the block at all
            when the lock is held.
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
        """Release the lock, but only if this instance actually took it.

        `PidFileLock.__enter__` also enters the block when somebody else
        holds the lock, and in that case there is nothing to release; the
        PID and sidecar files belong to the other holder and must be left
        alone.

        Args:
            exc_type: Type of the exception leaving the block, if any.
            exc_value: The exception instance, if any.
            traceback: The traceback of that exception, if any.

        Returns:
            `None`; exceptions from the block keep propagating.
        """
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
    """Fail-closed context adapter for :class:`PidFileLock`.

    `PidFileLock` on purpose breaks the `Lock.__enter__` contract: entering
    it succeeds whether or not the lock was acquired, because reporting
    *who* holds a pidfile is often the whole point. The price is that a
    caller who forgets to inspect the value silently runs the guarded body
    without holding anything, which is exactly the mutual exclusion bug the
    lock was supposed to prevent.

    This adapter buys the guarantee back for callers that only care about
    ownership: the block runs if and only if this process holds the lock,
    and contention raises `AlreadyLocked` before the body is entered, with
    the competing PID attached to the exception rather than returned. That
    keeps the fail-open behaviour available for the callers that want it,
    while the fail-closed one is a method call away.

    Obtained through `PidFileLock.fail_closed`; not meant to be
    instantiated directly.
    """

    def __init__(self, lock: PidFileLock) -> None:
        """Wrap the lock this context will acquire.

        Args:
            lock: The `PidFileLock` to guard. Only referenced; nothing is
                acquired until the context is entered.
        """
        self._lock: PidFileLock = lock

    def __enter__(self) -> None:
        """Acquire the lock, or refuse to enter the block.

        Returns:
            `None`. There is nothing useful to bind: reaching the body is
            itself the confirmation that this process holds the lock.

        Raises:
            ~portalocker.exceptions.AlreadyLocked: The lock could not be taken.
                Usually another process holds it, but `PidFileLock.acquire`
                also collapses every other plain `LockException` from the
                sidecar onto this type, so a single ``except`` clause covers
                both. The ``holder_pid`` attribute is filled in with the
                competing PID first, or with `None` when the PID file could not
                be read.
            Exception: Anything else `PidFileLock.acquire` raises passes
                through untouched: an `OSError` from opening the sidecar
                file, or a failure to publish the PID, with the rollback
                error chained onto it.
        """
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
        """Release the lock by delegating to the wrapped lock.

        Args:
            exc_type: Type of the exception leaving the block, if any.
            exc_value: The exception instance, if any.
            traceback: The traceback of that exception, if any.

        Returns:
            Whatever `PidFileLock.__exit__` returns, which is `None`, so
            exceptions from the block keep propagating.

        Note:
            Only reached when `__enter__` succeeded, so the lock is always
            held here; the ownership check inside `PidFileLock.__exit__` is
            belt and braces.
        """
        return self._lock.__exit__(exc_type, exc_value, traceback)


class BoundedSemaphore(LockBase['Lock | None']):
    """
    Bounded semaphore to prevent too many parallel processes from running.

    A slot is a lock file: `maximum` of them are generated from
    `filename_pattern`, and acquiring means locking whichever one is still
    free. Releasing does not delete the files, it only unlocks them.

    Prefer `NamedBoundedSemaphore`, a drop-in replacement for this class.
    Without an explicit `name` this class falls back to the shared default
    name ``bounded_semaphore``, so two completely unrelated programs on the
    same machine end up sharing one semaphore; constructing one that way
    emits a `DeprecationWarning`. Passing a `name` here is equivalent and
    warning-free.

    >>> semaphore = BoundedSemaphore(2, directory='')
    >>> str(semaphore.get_filenames()[0])
    'bounded_semaphore.00.lock'
    >>> str(sorted(semaphore.get_random_filenames())[1])
    'bounded_semaphore.01.lock'

    See Also:
        `NamedBoundedSemaphore`: the same thing with a mandatory or
        generated name.
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
        """Configure the semaphore; no lock file is created yet.

        Args:
            maximum: Number of slots, and therefore the number of lock
                files. That many holders may run at once.
            name: Name shared by every process that takes part in this
                semaphore. Leaving it at the default is deprecated, see
                the class documentation.
            filename_pattern: Pattern for the lock file names, formatted
                with ``name`` and ``number``.
            directory: Directory holding the lock files, the system
                temporary directory by default. It must already exist;
                acquiring inside a missing directory raises
                `FileNotFoundError`.
            timeout: See `LockBase`.
            check_interval: See `LockBase`.
            fail_when_locked: See `LockBase`. Defaults to `True` here, so
                a full semaphore raises `AlreadyLocked` straight away.

        Warns:
            DeprecationWarning: `name` is empty or left at the default
                ``'bounded_semaphore'``. Use `NamedBoundedSemaphore`, or
                pass a name of your own.
        """
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
        """Return the path of every slot, in order.

        Returns:
            One path per slot, from number ``0`` up to ``maximum - 1``.

        Example:
            >>> semaphore = BoundedSemaphore(2, name='example', directory='')
            >>> [str(filename) for filename in semaphore.get_filenames()]
            ['example.00.lock', 'example.01.lock']
        """
        return [self.get_filename(n) for n in range(self.maximum)]

    def get_random_filenames(self) -> typing.Sequence[pathlib.Path]:
        """Return the path of every slot, in a random order.

        A helper for callers that want to spread the contention out
        themselves: hand the result to `try_lock` and different processes
        start their sweep at different slots. `acquire` does not call this;
        it sweeps the slots in numerical order, see `BoundedSemaphore`.

        Returns:
            The same paths `get_filenames` returns, in a random order. The
            shuffle happens on a fresh list; `get_filenames` is unaffected.

        Example:
            >>> semaphore = BoundedSemaphore(2, name='example', directory='')
            >>> names = semaphore.get_random_filenames()
            >>> sorted(str(filename) for filename in names)
            ['example.00.lock', 'example.01.lock']
        """
        filenames = list(self.get_filenames())
        random.shuffle(filenames)
        return filenames

    def get_filename(self, number: int) -> pathlib.Path:
        """Build the path of a single slot.

        Args:
            number: The slot number. Callers normally stay within
                ``range(maximum)``, but any integer formats fine.

        Returns:
            `directory` joined with `filename_pattern` formatted with the
            semaphore `name` and this `number`.

        Example:
            >>> semaphore = BoundedSemaphore(2, name='example', directory='')
            >>> str(semaphore.get_filename(1))
            'example.01.lock'
        """
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
        """Take one of the `maximum` slots.

        The slot list is built once with `get_filenames`, so every attempt
        sweeps the slots in numerical order and keeps the first one that
        locks. The sweep repeats until a slot is free or the timeout
        expires. That order is fixed and identical in every process, so all
        contenders race for slot ``0`` first.

        Args:
            timeout: Overrides `timeout` for this call. See `LockBase`.
            check_interval: Overrides `check_interval` for this call.
            fail_when_locked: Overrides `fail_when_locked` for this call.
                Unlike the rest of the retry policy this one is consulted
                only after the timeout has expired: the semaphore always
                keeps trying for the full timeout, and this decides whether
                running out of time raises or returns.

        Returns:
            The `Lock` holding the slot that was taken, which is also
            stored as the `lock` attribute. `None` when no slot became
            free and `fail_when_locked` resolves to `False`.

        Raises:
            ~portalocker.exceptions.AlreadyLocked: All slots stayed taken for
                the whole timeout and `fail_when_locked` resolves to `True`.
            AssertionError: This instance already holds a slot. Release it
                before acquiring again.
            OSError: Raised straight through from `try_lock`, for instance
                `FileNotFoundError` when `directory` does not exist. The
                instance stays usable, so a later call can succeed once the
                cause is fixed.
        """
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
        """Try each candidate file once and keep the first one that locks.

        A single sweep with no waiting: every candidate is locked with
        ``fail_when_locked=True``, so a busy slot is skipped immediately
        rather than waited on.

        Args:
            filenames: The candidate slot files, tried in the given order.

        Returns:
            `True` when a slot was taken, in which case the `lock`
            attribute now holds its `Lock`. `False` when every candidate
            was already taken; the `lock` attribute is then left alone.

        Raises:
            Exception: Anything other than `AlreadyLocked` coming out of
                `Lock.acquire`, such as `FileNotFoundError` for a missing
                `directory`. The `lock` attribute is reset to `None`
                first, so the failure cannot brick the instance for later
                calls.
        """
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
        """Give the slot back, if this instance holds one.

        The lock file itself is left on disk; only the operating system
        lock is dropped, which is what makes the slot available again.
        Doing nothing when no slot is held keeps `LockBase.__del__` safe.
        """
        if self.lock is not None:
            self.lock.release()
            self.lock = None


class NamedBoundedSemaphore(BoundedSemaphore):
    """
    Bounded semaphore to prevent too many parallel processes from running.

    The recommended form of `BoundedSemaphore`: identical behaviour, but the
    name is either yours or randomly generated, never the shared default
    that makes unrelated programs collide.

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
        """Configure the semaphore, generating a name when none is given.

        Args:
            maximum: Number of slots. See `BoundedSemaphore`.
            name: Name shared by every participating process. `None`
                generates a random one, which only makes sense when the
                semaphore object itself is handed to the other processes.
            filename_pattern: Pattern for the lock file names.
            directory: Directory holding the lock files.
            timeout: See `LockBase`.
            check_interval: See `LockBase`.
            fail_when_locked: See `LockBase`.

        Note:
            The generated name is passed on explicitly, so the default
            ``name=None`` never triggers the `DeprecationWarning` that
            `BoundedSemaphore` emits for a missing name. An explicit `name`
            is forwarded untouched, so ``''`` or ``'bounded_semaphore'``
            still warns.
        """
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
