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
import inspect
import logging
import os
import pathlib
import random
import tempfile
import threading
import time
import typing
import warnings
import weakref
from types import FrameType

from . import constants, exceptions, portalocker, types
from .types import BinaryMode, Filename, Mode, TextMode

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


#: Errno values that mean the filesystem refused the hard link itself,
#: rather than the specific call failing: no hard link support at all
#: (``EPERM`` on exFAT and some FUSE mounts, ``ENOTSUP`` /
#: ``EOPNOTSUPP`` on some SMB and NFS mounts) or the link count limit
#: (``EMLINK``). ``EXDEV`` is deliberately absent, since the temporary
#: file lives in the destination's own directory and a cross-device link
#: is therefore impossible. `open_atomic` reacts to these errnos by
#: publishing through its rename fallback instead.
_HARD_LINK_FALLBACK_ERRNOS: frozenset[int] = frozenset(
    (
        errno.EPERM,
        errno.ENOTSUP,
        errno.EOPNOTSUPP,
        errno.EMLINK,
    )
)


def _annotate_preserved_payload(error: BaseException, temp_name: str) -> None:
    """Record on ``error`` that the written payload survives on disk.

    `open_atomic` keeps its temporary file when publication fails, so the
    caller's data is never silently destroyed. This helper welds the
    location of that file onto the exception the caller is about to
    receive, since the temporary name is random and otherwise unknowable.

    Args:
        error: The exception about to be re-raised by `open_atomic`. For
            an `OSError` carrying a ``strerror`` the note is appended
            there, so it shows up in ``str(error)`` and the traceback.
            Any other exception gets the note appended to its ``args``
            for the same effect.
        temp_name: Path of the preserved temporary file.
    """
    note: str = f'payload preserved at {temp_name}'
    if isinstance(error, OSError) and error.strerror:
        error.strerror = f'{error.strerror} ({note})'
    else:
        error.args = (*error.args, note)


#: Upper bound on the random temporary names `_open_exclusive_temp`
#: tries before giving up, mirroring `tempfile.TMP_MAX`. With 64 bits of
#: entropy per name it only trips when the entropy source is broken, and
#: then an `OSError` beats an endless loop.
_TEMP_NAME_ATTEMPTS: int = 10000


def _open_exclusive_temp(
    path: pathlib.Path,
    binary: bool,
) -> tuple[types.IO, str]:
    """Create `open_atomic`'s temporary file next to its destination.

    The file is created with mode ``0o666`` passed straight to `os.open`,
    so the kernel subtracts the process umask at creation time, exactly
    like a plain `open` call. That is deliberate: `tempfile` would create
    a private ``0o600`` file, which both publication primitives preserve,
    and correcting the mode afterwards would take either a ``chmod``
    (refused by some network mounts) or a umask round-trip (a
    process-global mutation that briefly leaks mode ``0o777`` file
    creation to every other thread). No global state is touched here.

    The temporary basename is a fixed 33 bytes
    (``.portalocker.<16 hex>.tmp``) and deliberately does not embed the
    destination's name: a destination basename near the usual 255 byte
    filesystem limit must not push the temporary name over it. ``O_EXCL``
    guards the randomly generated name against collisions and symlinks:
    an occupied name is rolled again, at most `_TEMP_NAME_ATTEMPTS`
    times.

    Args:
        path: The destination the temporary file will be published to.
            The temporary file lands in the same directory under a hidden
            randomized name.
        binary: Open the file in binary mode (``'wb'``) rather than text
            mode (``'w'``).

    Returns:
        The open filehandle and the temporary file's path.

    Raises:
        OSError: No free temporary name was found within
            `_TEMP_NAME_ATTEMPTS` attempts, which practically means the
            randomness source is broken. Raised as plain `OSError`, never
            `FileExistsError`, so it cannot be mistaken for the
            destination existing.
    """

    def _exclusive_opener(opener_path: str, flags: int) -> int:
        """Open ``opener_path`` exclusively with kernel-applied mode."""
        return os.open(opener_path, flags | os.O_EXCL, 0o666)

    for _ in range(_TEMP_NAME_ATTEMPTS):
        temp_name: str = str(
            path.parent / f'.portalocker.{os.urandom(8).hex()}.tmp',
        )
        try:
            # Not a `with`: the handle is handed back to `open_atomic`,
            # which enters it around the caller's body.
            temp_fh: types.IO = open(  # noqa: SIM115
                temp_name,
                'wb' if binary else 'w',
                opener=_exclusive_opener,
            )
        except FileExistsError:
            # Another actor owns this random name, so roll a new one.
            continue
        return temp_fh, temp_name

    raise OSError(
        f'no usable temporary file name found in {str(path.parent)!r}',
    )


def _publish_exclusive(temp_name: str, path: pathlib.Path) -> None:
    """Publish ``temp_name`` at ``path``, refusing an existing target.

    Windows renames, which refuses an existing destination on its own.
    POSIX hard links, and on filesystems that cannot hard link (errno in
    `_HARD_LINK_FALLBACK_ERRNOS`) falls back to an existence check plus
    rename. The fallback keeps the content atomic but cannot reliably
    refuse concurrent publishers. See the `open_atomic` docstring.

    Args:
        temp_name: The written and synchronized temporary file.
        path: The destination. Its directory already exists.

    Raises:
        FileExistsError: The destination exists, either reported by the
            platform primitive or found by the fallback's check.
        OSError: The publication primitive failed for another reason.
    """
    if os.name == 'nt':  # pragma: not-nt
        os.rename(temp_name, path)
    else:  # pragma: not-posix
        try:
            os.link(temp_name, path)
        except OSError as link_error:
            if link_error.errno not in _HARD_LINK_FALLBACK_ERRNOS:
                raise
            # No hard link support here (exFAT, some SMB/NFS/FUSE): fall
            # back to check-then-rename. `lexists` instead of `exists` so
            # a dangling symlink destination is refused the way the hard
            # link refuses it, rather than silently replaced.
            if os.path.lexists(path):
                raise FileExistsError(
                    errno.EEXIST,
                    os.strerror(errno.EEXIST),
                    str(path),
                ) from link_error
            os.rename(temp_name, path)


@contextlib.contextmanager
def open_atomic(
    filename: Filename,
    binary: bool = True,
) -> collections.abc.Generator[types.IO]:
    """Open a new file for atomic writing without replacing an existing file.

    The destination must not exist when entering or publishing the context.
    A destination that exists on entry raises :class:`FileExistsError`
    straight away. If another actor creates it while the context is open,
    publication raises :class:`FileExistsError` as well and leaves that
    destination untouched.

    The implementation writes and synchronizes a temporary file in the
    destination directory, then publishes it with an operation that refuses
    an existing destination. Windows uses an atomic rename and POSIX an
    atomic hard link. On POSIX filesystems without hard link support (exFAT
    and some SMB, NFS and FUSE mounts) the hard link fails with an errno in
    `_HARD_LINK_FALLBACK_ERRNOS` and publication falls back to an existence
    check followed by a rename. The fallback still publishes the content
    atomically, but it does not reliably refuse concurrent publishers:
    two of them can both pass the check, and the later rename then
    replaces the earlier file. The strong no-replace guarantee requires
    hard link support.

    The two failure directions clean up differently. When the caller's
    body raises, the temporary file is removed: the payload is incomplete
    and keeping it would leak one file per failed attempt. When the body
    completed but *publication* fails, for any reason, the temporary file
    is kept so the finished payload is not destroyed, and the raised
    exception names its path. Publication succeeding is the only other
    thing that removes it.

    The published file carries the permissions a plain `open` would have
    given it: the temporary file is created with mode ``0o666``, so the
    kernel applies the process umask at creation time and the
    process-wide umask itself is never touched. A body that closes the
    handle itself is fine: the file is synchronized through a fresh
    descriptor instead.

    Note:
        The destination *content* is synchronized to disk before
        publication, but the directory entry is not (no ``fsync`` on the
        directory). After a power loss the name may be missing even
        though the context exited cleanly.

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
        raise FileExistsError(
            errno.EEXIST,
            os.strerror(errno.EEXIST),
            str(path),
        )

    # Create the parent directory if it doesn't exist
    path.parent.mkdir(parents=True, exist_ok=True)

    temp_fh, temp_name = _open_exclusive_temp(path, binary)
    try:
        with temp_fh:
            yield temp_fh
            if temp_fh.closed:
                # The body closed the handle itself, so reopen briefly to
                # keep the payload synchronized before publication.
                sync_fd: int = os.open(temp_name, os.O_RDWR)
                try:
                    os.fsync(sync_fd)
                finally:
                    os.close(sync_fd)
            else:
                temp_fh.flush()
                os.fsync(temp_fh.fileno())
    except BaseException:
        # The caller's body (or the flush) failed: there is no complete
        # payload worth keeping, so remove the temporary file instead of
        # leaking one per failed attempt.
        with contextlib.suppress(Exception):
            temp_fh.close()
        with contextlib.suppress(Exception):
            os.remove(temp_name)
        raise

    try:
        _publish_exclusive(temp_name, path)
    except Exception as error:
        # Keep the temporary file: deleting it here would silently
        # destroy the payload the caller just wrote.
        _annotate_preserved_payload(error, temp_name)
        raise

    with contextlib.suppress(Exception):
        os.remove(temp_name)


#: The type returned by `LockBase.acquire` and, through it, by
#: `LockBase.__enter__`. Locks that guard a file return the opened
#: filehandle, others return whatever fits their locking model.
AcquireReturnT = typing.TypeVar('AcquireReturnT')

if typing.TYPE_CHECKING:
    # `typing.TypeVar` only learned the PEP 696 ``default`` argument in
    # Python 3.13, and this package supports 3.10 without runtime
    # dependencies. Type checkers bundle the `typing_extensions` stubs,
    # so the default is visible to them without the package being
    # installed. At runtime the plain `typing.TypeVar` below is used and
    # the default simply does not exist, which changes nothing because
    # defaults only matter to static analysis.
    from typing_extensions import TypeVar as _TypeVarWithDefault

    #: The filehandle type a `Lock` hands out, driven by the open mode:
    #: ``IO[str]`` for text modes, ``IO[bytes]`` for binary modes. The
    #: default keeps a bare ``Lock`` annotation meaningful: it is the
    #: text filehandle, matching the default mode of ``'a'``.
    IOT = _TypeVarWithDefault(
        'IOT',
        bound=typing.IO[typing.Any],
        default=typing.IO[str],
    )
else:
    IOT = typing.TypeVar('IOT', bound=typing.IO[typing.Any])


#: Every live `LockBase` instance, registered at construction so
#: `_reinit_state_locks_after_fork` can reach their state locks in a
#: forked child. A `weakref.WeakSet`, so membership never keeps a lock
#: alive and collected locks drop out on their own.
_live_locks: weakref.WeakSet[LockBase[typing.Any]] = weakref.WeakSet()


def _reinit_state_locks_after_fork() -> None:
    """Reset every live instance's Python locks in a freshly forked child.

    A child forked while any thread holds an instance state lock inherits
    that lock in its locked state, owned by a thread that does not exist
    in the child, and the child's first `release`, `acquire` or
    interpreter-exit cleanup then deadlocks on it forever. The window is
    real: any state-lock scope that reaches the operating system releases
    the GIL, so an unlucky ``os.fork`` from another thread lands inside
    it. This is the same problem the standard library's ``logging``
    module has with its handler locks, solved the same way: registered
    with ``os.register_at_fork`` below, the child gets every registered
    lock reinitialized to a fresh unlocked one before it runs any Python
    code of its own. Each instance reports its locks through
    `LockBase._fork_reinit_locks` - the state lock for every lock kind,
    plus whatever else a subclass guards with its own Python lock (the
    mode lock of `~portalocker.redis.RedisLock`, held by its worker
    thread for every ping answer). Only Python locks are reset; which OS
    locks the child actually holds is unchanged, since those live on
    file descriptors, not on Python locks.

    On Windows this function is a quiet no-op. CPython only compiles
    ``_at_fork_reinit`` into its lock types on platforms with ``fork``
    (the method sits behind ``#ifdef HAVE_FORK`` in
    ``Modules/_threadmodule.c``), so ``nt`` builds lack it entirely.
    Nothing is lost by skipping such locks: without ``fork`` there is no
    inherited-lock problem to repair, but a direct call (tests, embedders
    running platform-neutral cleanup) must not explode either.
    """
    for lock in list(_live_locks):
        instance_locks: tuple[threading.Lock | threading.RLock, ...] = (
            lock._fork_reinit_locks()  # pyright: ignore[reportPrivateUsage]
        )
        for instance_lock in instance_locks:
            # `_at_fork_reinit` has existed on every lock type since
            # CPython 3.9, but only on builds with `fork`, and it is
            # missing from typeshed everywhere. The `getattr` covers
            # both.
            reinit: typing.Callable[[], None] | None = getattr(
                instance_lock,
                '_at_fork_reinit',
                None,
            )
            if reinit is not None:
                reinit()  # pragma: not-posix


# Windows has no fork, and no `os.register_at_fork` to register with. The
# hook exists on every POSIX build and never on nt, so the condition is a
# platform constant: branch tracking is disabled instead of pretending
# both outcomes are reachable on one platform, and the nt-unreachable
# registration itself is excluded only there.
_register_at_fork = getattr(os, 'register_at_fork', None)
if _register_at_fork is not None:  # pragma: no branch - platform constant
    _register_at_fork(  # pragma: not-posix
        after_in_child=_reinit_state_locks_after_fork,
    )


class LockBase(
    abc.ABC,
    typing.Generic[AcquireReturnT],
):
    """Abstract base class for every lock in portalocker.

    It contributes two things to its subclasses: the retry policy stored in
    the three attributes below, and the `LockBase._timeout_generator` that
    turns that policy into a sequence of attempts. Subclasses only have to
    implement `acquire` and `release`, and the context manager protocol
    and the descriptor hook come for free.

    Garbage collection of a lock object deliberately leaves the lock
    alone. Portalocker 4.0.0 released held locks from a ``__del__``
    finalizer, which tore down locks whose filehandle the caller was still
    using (``fh = Lock(...).acquire()`` keeps the filehandle alive, not
    the lock object). 4.2.0 removed that finalizer and restored the 3.2.0
    behaviour.

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
            i.e. retry). Either way a lock that stays contended surfaces
            as `AlreadyLocked`: with `fail_when_locked` after the first
            attempt, without it once the timeout runs out. Contention is
            also the only condition that is retried at all. Any other
            failure, such as a `LockException` from a backend that
            cannot lock the file, is permanent and propagates
            immediately instead of burning the timeout. The timing
            matters when several processes race to create the same file
            and you would rather hear about the contention immediately
            than a handful of seconds later.

    Note:
        Every one of the three is also a per-call argument of `acquire`.
        The argument wins when it is not `None`; otherwise the attribute
        set here is used.

    Note:
        A single lock instance shared across threads is synchronized for
        state consistency only, not for granted-lock semantics: the
        internal bookkeeping (which filehandle is held, the acquire
        count, which semaphore slot is taken) cannot be corrupted by
        concurrent calls, but which thread's `acquire` wins, or whether a
        concurrent `release` makes another thread's `acquire` succeed, is
        still scheduling. Two threads racing `acquire` on one instance
        is explicitly unsupported: with a per-process locker (POSIX
        ``lockf``) both lock calls succeed, the second publication
        overwrites the first, and the overwritten descriptor's eventual
        close releases the process's record locks on the file. Use one
        instance per thread, or a `threading.Lock` of your own, when you
        need per-thread mutual exclusion.

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
    #: Guards this instance's acquire/release state transitions (claiming
    #: the held filehandle, publishing a freshly acquired one, moving the
    #: reentrancy count). Deliberately reentrant: a signal handler that
    #: calls `release` while the owning thread sits inside a state
    #: transition must claim-and-no-op instead of deadlocking against its
    #: own thread. The scope is kept to plain attribute swaps; it is never
    #: held around a blocking OS locking call, so `acquire` timeouts
    #: cannot deadlock behind it.
    _state_lock: threading.RLock

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
        self._state_lock = threading.RLock()
        # Registered so a forked child can reinitialize this instance's
        # Python locks (`_fork_reinit_locks`), see
        # `_reinit_state_locks_after_fork`.
        _live_locks.add(self)

    def _fork_reinit_locks(
        self,
    ) -> tuple[threading.Lock | threading.RLock, ...]:
        """Report the Python locks a forked child must reinitialize.

        Consumed by `_reinit_state_locks_after_fork`: everything listed
        here is reset to a fresh unlocked lock in a freshly forked
        child, because the child may have inherited it locked by a
        thread that does not exist there. A subclass that guards state
        with an extra per-instance Python lock extends the tuple
        (`~portalocker.redis.RedisLock` adds the mode lock its worker
        thread takes for every ping answer). The hook resets each lock
        independently and acquires none of them, so listing several
        locks creates no ordering between them.

        Returns:
            The locks to reinitialize; the state lock alone here.
        """
        return (self._state_lock,)

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
        it. A sleep is also capped at the time left until the deadline, so
        a ``check_interval`` larger than the remaining `timeout` cannot
        stretch the total wait past the timeout itself. The sleep never
        drops below a millisecond, which keeps a ``check_interval`` of
        ``0`` from spinning the CPU flat out.

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

        start_time: float = time.perf_counter()
        deadline: float = start_time + f_timeout
        while deadline > time.perf_counter():
            i += 1
            yield i

            # Take slow lock checks into account to stay within the
            # interval, and never sleep past the deadline itself: a
            # check_interval larger than the remaining timeout would
            # otherwise overshoot the timeout by up to a full interval.
            now: float = time.perf_counter()
            scheduled: float = (i * f_check_interval) - (now - start_time)
            remaining: float = deadline - now
            time.sleep(max(0.001, min(scheduled, remaining)))

    @abc.abstractmethod
    def release(self) -> None:
        """Give up the lock.

        Abstract; the implementations differ in how much they clean up.
        `Lock.release` unlocks and closes the filehandle,
        `TemporaryFileLock.release` also unlinks the lock file, and
        `RLock.release` only does either once the acquire count reaches
        zero.

        Implementations are expected to tolerate being called on an
        instance that holds nothing: double releases and the ``atexit``
        fallback of `TemporaryFileLock` both do exactly that.
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

    def __delete__(self, instance: object) -> None:
        """Release this lock when it is deleted from an owning object.

        Python routes ``del owner.attribute`` here when a lock instance is
        stored as a class attribute of ``owner``, which lets a lock be
        released by deleting the attribute that holds it. The lock itself
        is the descriptor, so it is this lock's own `release` that runs.
        The owning object is received as `instance` and is not touched.

        Args:
            instance: The object the attribute was deleted from. Unused,
                since the lock releases itself.
        """
        self.release()


def _stacklevel_beyond_module() -> int:
    """Return the `warnings.warn` stacklevel of the first foreign frame.

    Computed for the caller: starting from the function that called this
    helper, every consecutive stack frame that still lives in this
    module is skipped, and the returned stacklevel makes
    `warnings.warn`, invoked from that caller, attribute the warning to
    the first frame outside the module. That keeps warnings pointing at
    the user's own code no matter how many subclass constructors or
    ``acquire`` wrappers sit in between: `Lock` warns through one
    internal frame, `RLock` and `TemporaryFileLock` through two,
    `PidFileLock` through three.

    Python 3.12 grew ``warnings.warn(skip_file_prefixes=...)`` for
    exactly this job. This helper is the 3.10 compatible spelling.

    Returns:
        The stacklevel to pass to `warnings.warn` from the caller's
        frame, at least ``1``. Falls back to ``1``, which names the
        caller itself, when the interpreter offers no frame
        introspection (CPython always does).
    """
    frame: FrameType | None = inspect.currentframe()
    if frame is None:
        return 1
    # Start at the caller of this helper: stacklevel 1 is its own frame.
    frame = frame.f_back
    stacklevel: int = 1
    while frame is not None and frame.f_code.co_filename == __file__:
        stacklevel += 1
        frame = frame.f_back
    return stacklevel


def _restore_positional_writes(fh: types.IO) -> None:  # pragma: not-posix
    """Clear the kernel append flag so ``fh`` honours seek positions.

    `Lock` opens a mode containing ``w`` as ``a`` to defer the truncation
    until the lock is held, but ``O_APPEND`` makes the kernel ignore the
    seek position on every ``write``: ``fh.write('x'); fh.seek(0);
    fh.write('y')`` yields ``'xy'`` where plain ``open(mode='w')`` yields
    ``'y'``. Clearing the flag after the truncation restores the write
    semantics the caller asked for.

    POSIX only: Windows has no ``fcntl`` and offers no way to drop the
    append flag from an open handle, so there the substitution keeps
    append semantics and is documented instead.

    Args:
        fh: The open, locked, already truncated filehandle.
    """
    import fcntl

    fd: int = fh.fileno()
    flags: int = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_APPEND)


def _chain_release_error(
    exc_value: BaseException,
    release_error: BaseException,
) -> None:
    """Chain a release failure under the exception leaving a block.

    The body-exception-wins discipline shared by `Lock.__exit__` and
    `~portalocker.redis.RedisLock.__exit__`: the release error becomes
    the ``__context__`` of the exception already propagating out of the
    ``with`` block and a note is attached, so both remain visible in
    the traceback while the block's own exception is what the caller
    sees.

    Errors raised while ``exc_value`` was in flight carry it as their
    implicit ``__context__``. Splicing the release error underneath
    ``exc_value`` would then close a reference cycle that loops naive
    chain walkers, so those back links are snipped first. The walk is
    bounded instead of tracked, because a release chain deeper than
    this is not worth preserving.

    Args:
        exc_value: The exception leaving the ``with`` block.
        release_error: What `release` raised while ``exc_value`` was in
            flight.
    """
    previous_context: BaseException | None = exc_value.__context__
    release_error.__context__ = previous_context
    link: BaseException | None = release_error
    depth: int = 0
    while link is not None and depth < 10:
        if link.__context__ is exc_value:
            link.__context__ = None
        link = link.__cause__ or link.__context__
        depth += 1
    exc_value.__context__ = release_error
    with contextlib.suppress(Exception):
        exc_value.add_note(  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
            'portalocker release failed; see exception context',
        )


class Lock(LockBase[IOT]):
    """Lock manager with built-in timeout.

    The class most users want. It opens `filename`, locks it, retries for
    as long as the inherited retry policy allows, and hands the open
    filehandle to the caller. Releasing unlocks and closes that handle; the
    file itself stays behind, which is what makes the lock usable as a
    plain data file as well as a mutex.

    The class is generic over the filehandle it hands out, and the open
    mode decides the specialization: a text mode yields a
    ``Lock[IO[str]]``, a binary mode a ``Lock[IO[bytes]]``, so type
    checkers know whether ``fh.read()`` produces `str` or `bytes`
    (issue #97). A bare ``Lock`` annotation means ``Lock[IO[str]]``,
    matching the default mode of ``'a'``.

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

    Warning:
        The ``a`` substitution has a side effect on Windows: the handle
        keeps the kernel's append semantics, so every ``write`` lands at
        the end of the file no matter where you ``seek``. A positioned
        rewrite such as ``fh.write('x'); fh.seek(0); fh.write('y')``
        produces ``'xy'`` there, where plain ``open(mode='w')`` produces
        ``'y'``. On POSIX portalocker clears the append flag once the
        truncation is done, so ``w`` and ``w+`` behave exactly like the
        builtin ``open``. If you need positioned writes on Windows,
        reopen the file after acquiring, or lock with mode ``r+`` and
        truncate explicitly.

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

    fh: IOT | None
    filename: str
    mode: str
    truncate: bool
    timeout: float
    check_interval: float
    fail_when_locked: bool
    flags: constants.LockFlags
    raise_on_release_error: bool
    file_open_kwargs: dict[str, typing.Any]
    #: whether the "timeout has no effect in blocking mode" warning has
    #: already been emitted for this instance. It fires at most once per
    #: lock, at construction or on the first `acquire` with a timeout.
    #: A real class-level default, so a subclass that skips
    #: `Lock.__init__` can still `acquire` without an `AttributeError`
    _timeout_warned: bool = False

    @typing.overload
    def __init__(
        self: Lock[typing.IO[str]],
        filename: Filename,
        mode: TextMode = 'a',
        timeout: float | None = None,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
        fail_when_locked: bool = DEFAULT_FAIL_WHEN_LOCKED,
        flags: constants.LockFlags = LOCK_METHOD,
        *,
        raise_on_release_error: bool = False,
        **file_open_kwargs: typing.Any,
    ) -> None: ...

    @typing.overload
    def __init__(
        self: Lock[typing.IO[bytes]],
        filename: Filename,
        mode: BinaryMode,
        timeout: float | None = None,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
        fail_when_locked: bool = DEFAULT_FAIL_WHEN_LOCKED,
        flags: constants.LockFlags = LOCK_METHOD,
        *,
        raise_on_release_error: bool = False,
        **file_open_kwargs: typing.Any,
    ) -> None: ...

    # The catch-all for a mode that is not a literal at the call site (a
    # `Mode`-typed variable, a conditional): no specialization can be
    # picked, so the filehandle honestly stays ``IO[Any]`` exactly as it
    # was in 4.2.0. Without this overload each checker invents its own
    # wrong answer for dynamic modes: one the text default, another the
    # first branch of a conditional.
    @typing.overload
    def __init__(
        self: Lock[typing.IO[typing.Any]],
        filename: Filename,
        mode: Mode,
        timeout: float | None = None,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
        fail_when_locked: bool = DEFAULT_FAIL_WHEN_LOCKED,
        flags: constants.LockFlags = LOCK_METHOD,
        *,
        raise_on_release_error: bool = False,
        **file_open_kwargs: typing.Any,
    ) -> None: ...

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
                including a `pathlib.Path`. It is stored as an absolute
                path string: a relative path is resolved against the
                working directory once, here, so an ``os.chdir`` between
                acquire and release (the daemonize idiom does
                ``chdir('/')``) cannot redirect the release, or the
                interpreter-exit cleanup of the subclasses, at another
                process's equally-named lock files. Changed in 4.2.0;
                the ``filename`` attribute used to keep the path as
                given.
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
                The warning is emitted at most once per instance: here,
                or on the first `acquire` that passes a timeout.
        """
        self._init(
            filename,
            mode,
            timeout,
            check_interval,
            fail_when_locked,
            flags,
            raise_on_release_error=raise_on_release_error,
            **file_open_kwargs,
        )

    def _init(
        self,
        filename: Filename,
        mode: Mode,
        timeout: float | None,
        check_interval: float,
        fail_when_locked: bool,
        flags: constants.LockFlags,
        *,
        raise_on_release_error: bool,
        **file_open_kwargs: typing.Any,
    ) -> None:
        """Shared constructor body behind the overloaded `__init__`.

        `__init__` is overloaded so the open mode picks the filehandle
        specialization, and an overloaded method cannot be super-called
        from a generic subclass: no overload's ``self`` type matches the
        subclass's still-unsolved type variable. Generic subclasses such
        as `RLock` therefore run their parent initialization through this
        plain method instead.
        """
        if 'w' in mode:
            truncate = True
            mode = typing.cast(Mode, mode.replace('w', 'a'))
        else:
            truncate = False

        self.fh = None
        self.filename = os.path.abspath(filename)
        self.mode = mode
        self.truncate = truncate
        self.flags = flags
        self.raise_on_release_error = raise_on_release_error
        self.file_open_kwargs = file_open_kwargs

        self._timeout_warned = False
        if timeout is None:
            timeout = DEFAULT_TIMEOUT
        else:
            self._warn_blocking_timeout(timeout)
        super().__init__(timeout, check_interval, fail_when_locked)

    def _warn_blocking_timeout(self, timeout: float | None) -> None:
        """Warn that `timeout` cannot work with blocking flags, only once.

        With flags lacking ``LockFlags.NON_BLOCKING`` the operating
        system waits inside the locking call itself, so a timeout has
        nothing left to do. The warning fires at most once per instance,
        whether that happens at construction or on the first `acquire`
        that passes a timeout. Its stacklevel is computed by
        `_stacklevel_beyond_module`, so it names the caller's own file
        for every entry point: a `Lock` built directly, the `RLock`,
        `TemporaryFileLock` and `PidFileLock` constructors, and the
        ``acquire`` chains of all four.

        Args:
            timeout: The caller-provided timeout, or `None` when the
                caller did not pass one, which never warns.
        """
        if (
            timeout is None
            or self._timeout_warned
            or bool(self.flags & constants.LockFlags.NON_BLOCKING)
        ):
            return
        self._timeout_warned = True
        warnings.warn(
            'timeout has no effect in blocking mode',
            stacklevel=_stacklevel_beyond_module(),
        )

    def acquire(
        self,
        timeout: float | None = None,
        check_interval: float | None = None,
        fail_when_locked: bool | None = None,
    ) -> IOT:
        """Open the file, lock it and return the filehandle.

        Calling this on a lock that is already held is cheap and safe: the
        filehandle taken earlier is returned as is, without touching the
        operating system.

        Two threads *racing* this method on one instance is unsupported.
        The instance's bookkeeping stays consistent, but with a
        per-process locker (POSIX ``lockf``) both lock calls succeed,
        one thread's filehandle overwrites the other's, and the
        orphaned descriptor's eventual close releases the process's
        record locks on the file, the winner's included: that is how
        POSIX record locks work, and no publication strategy on this
        side can paper over it. Give each thread its own lock instance
        instead.

        Args:
            timeout: Overrides `timeout` for this call. See `LockBase`.
            check_interval: Overrides `check_interval` for this call.
            fail_when_locked: Overrides `fail_when_locked` for this call.

        Returns:
            The open, locked filehandle. It is stored on the instance as
            well, and stays valid until `release`.

        Raises:
            ~portalocker.exceptions.AlreadyLocked: The first attempt found the
                file locked and `fail_when_locked` was set, or `timeout`
                expired while the file stayed locked by somebody else.
                Only contention is retried: the locking backend signals it
                by raising `AlreadyLocked` itself.
            ~portalocker.exceptions.LockException: The backend failed for
                a reason other than contention, such as a filesystem
                without locking support or refused flags. This is
                permanent, so it is raised immediately without burning
                the timeout, and without being dressed up as
                `AlreadyLocked`. A non-portalocker error from the locking
                call is wrapped in this type as well.
            OSError: Opening or preparing the file failed, for instance
                because the directory does not exist or the mode is not
                permitted. Only locking failures are translated. Failures
                from `open` and from the deferred truncation propagate
                untouched. When the failure happens after the lock was
                taken, the file is unlocked and closed again first.

        Warns:
            UserWarning: A `timeout` was passed while the lock uses
                blocking flags, where it has no effect. Emitted at most
                once per instance, counting the constructor's warning.

        Example:
            >>> import portalocker
            >>> lock = portalocker.Lock('somefile', timeout=1)
            >>> fh = lock.acquire()
            >>> fh is lock.acquire()
            True
            >>> lock.release()
        """
        fail_when_locked = coalesce(fail_when_locked, self.fail_when_locked)
        self._warn_blocking_timeout(timeout)

        # If we already have a filehandle, return it
        fh = self.fh
        if fh:
            return fh

        # Get a new filehandler
        fh = self._get_fh()

        try:
            exception = None
            # Try till the timeout has passed
            for _ in self._timeout_generator(timeout, check_interval):
                exception = None
                try:
                    # Try to lock
                    fh = self._get_lock(fh)
                    break
                except exceptions.AlreadyLocked as exc:
                    # Somebody else holds the lock. Retrying can help
                    # here, so keep trying until the timeout expires.
                    # Python would remove the exception from memory once
                    # the handler ends unless it is saved in a different
                    # location.
                    exception = exc

                    # We already tried to get the lock
                    # If fail_when_locked is True, stop trying
                    if fail_when_locked:
                        # Propagate the locker's own args (OSError plus
                        # message on POSIX, code plus message on Windows)
                        # so `strerror` is populated on the exception
                        # users actually catch, and forward `fh` and
                        # `holder_pid` so `fh_name` and the holder
                        # survive a pickle across a multiprocessing
                        # boundary (pickling drops `__cause__`, where the
                        # original exception stays reachable in-process).
                        raise exceptions.AlreadyLocked(
                            *exc.args,
                            fh=exc.fh,
                            holder_pid=getattr(exc, 'holder_pid', None),
                        ) from exc
                except exceptions.LockException:
                    # The backend failed for a reason other than
                    # contention: a filesystem without locking support,
                    # no more locks available, refused flags. Retrying
                    # cannot change that, so fail fast with the backend's
                    # own exception instead of burning the timeout or
                    # claiming somebody holds the lock.
                    raise
                except Exception as exc:
                    # Something went wrong with the locking mechanism.
                    # Wrap in a LockException and re-raise:
                    raise exceptions.LockException(exc) from exc

                # Wait a bit

            if exception:
                # We got a timeout... reraising
                raise exception  # noqa: TRY301

            # Prepare the filehandle (truncate if needed)
            fh = self._prepare_locked_fh(fh)
        except BaseException:
            # Every failed exit runs through here, interrupts included: a
            # `KeyboardInterrupt` in the retry sleep used to leak the
            # opened descriptor for the traceback's lifetime, and one
            # landing after a successful lock left the OS lock held by an
            # untracked descriptor with `release` a silent no-op. The
            # unlock is a harmless no-op on a descriptor that never got
            # the lock, and both calls stay quiet so they cannot replace
            # whatever is propagating.
            with contextlib.suppress(Exception):
                portalocker.unlock(fh)
            with contextlib.suppress(Exception):
                fh.close()
            raise

        with self._state_lock:
            self.fh = fh
        return fh

    def _prepare_locked_fh(self, fh: IOT) -> IOT:
        """Run `Lock._prepare_fh`, rolling the lock back when it fails.

        Preparation runs after the lock was taken, so a failure there,
        for example a truncate refused by an append-only file, must give
        the lock back: the caller's traceback may keep `fh` alive
        indefinitely, and a close alone would leave the file locked for
        as long as the exception is referenced. The handle is therefore
        unlocked explicitly and closed before the original error
        escapes, both best effort so they cannot mask that error. The
        rollback covers `BaseException`, not just `Exception`: a
        ``KeyboardInterrupt`` delivered inside the preparation would
        otherwise strand the OS lock on an untracked descriptor, with
        `release` a silent no-op because ``self.fh`` was never set.

        Args:
            fh: The freshly locked filehandle.

        Returns:
            The prepared filehandle, on success.

        Raises:
            BaseException: Whatever `Lock._prepare_fh` raised, unchanged.
        """
        try:
            return self._prepare_fh(fh)
        except BaseException:
            with contextlib.suppress(Exception):
                portalocker.unlock(fh)
            with contextlib.suppress(Exception):
                fh.close()
            raise

    def __enter__(self) -> IOT:
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

        Overrides `LockBase.__exit__` for one reason: when the block is
        already leaving with an exception, a failure during `release` must
        not replace it. The release error is chained onto the original as
        its ``__context__`` and a note is attached, so both remain visible
        in the traceback while the original is what propagates. `release`
        only raises when ``raise_on_release_error`` is set, but the
        protection holds either way, so even a subclass whose `release`
        fails unexpectedly cannot mask the block's own exception.
        `PidFileLock` overrides ``__exit__`` with an ownership check but
        routes the actual release through this method, so the guarantee
        covers it as well.

        Args:
            exc_type: Type of the exception leaving the block, if any.
            exc_value: The exception instance, if any.
            traceback: The traceback of that exception, if any.

        Returns:
            `None`; exceptions from the block are never suppressed.

        Raises:
            Exception: Whatever `release` raises, but only when the block
                itself ended without an exception. With the default
                ``raise_on_release_error=False`` that is nothing at all.
        """
        try:
            self.release()
        except Exception as release_error:
            if exc_value is None:
                # Nothing to mask, the release error is the only failure.
                raise
            _chain_release_error(exc_value, release_error)
        return None

    def release(self) -> None:
        """Unlock and close the file handle, if this instance holds one.

        Doing nothing when no lock is held is deliberate: double releases
        and the ``atexit`` fallback of `TemporaryFileLock` rely on it.

        The stored handle is claimed atomically before any OS call runs:
        exactly one caller takes ownership and tears the lock down, every
        concurrent or reentrant caller finds nothing and returns. Two
        threads releasing at once therefore cannot both reach the unlock,
        where the loser used to run it on a closed, possibly reused file
        descriptor and silently dropped whichever lock that descriptor
        number belonged to by then. The claim-first ordering also means an
        interrupt escaping ``close`` (a ``KeyboardInterrupt`` during a
        buffered flush) can no longer leave the instance believing it
        still holds the lock.

        Raises:
            Exception: Only when the lock was built with
                ``raise_on_release_error=True``. The first failure of the
                unlock and close pair is raised, chained from the second
                if both failed. By default such failures are suppressed
                and logged at warning level instead.
        """
        fh: types.IO | None = self._claim_fh()
        if fh is not None:
            self._release_claimed_fh(fh)

    def _claim_fh(self) -> types.IO | None:
        """Atomically take ownership of the stored filehandle.

        The swap runs under the instance state lock, so of any number of
        concurrent callers exactly one receives the handle and everyone
        else receives `None`. The state lock is reentrant, which makes the
        claim signal safe: a handler that calls `release` while this
        thread sits inside the swap claims `None` instead of deadlocking.

        Returns:
            The filehandle this instance held, now owned by the caller,
            or `None` when another claim (or none at all) got there first.
        """
        fh: types.IO | None
        with self._state_lock:
            fh, self.fh = self.fh, None
        return fh

    def _release_claimed_fh(self, fh: types.IO) -> None:
        """Unlock and close a filehandle claimed via `Lock._claim_fh`.

        Both steps are always attempted: on Windows closing the handle is
        what releases the lock, so a failing unlock must not skip the
        close. The error policy is the one documented on `Lock.release`.

        Args:
            fh: The claimed filehandle. The caller owns it exclusively;
                the instance no longer references it.

        Raises:
            Exception: Only with ``raise_on_release_error`` set, exactly
                as documented on `Lock.release`.
        """
        release_errors: list[Exception] = []
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

        if release_errors:
            if self.raise_on_release_error:
                primary_error: Exception = release_errors[0]
                if len(release_errors) > 1:
                    raise primary_error from release_errors[1]
                raise primary_error
            for release_error in release_errors:
                logger.warning(
                    'suppressed error while releasing lock on %r: %r',
                    self.filename,
                    release_error,
                )

    def _get_fh(self) -> IOT:
        """Open the file and return the new, still unlocked filehandle."""
        return typing.cast(
            'IOT',
            open(  # noqa: SIM115
                self.filename,
                self.mode,
                **self.file_open_kwargs,
            ),
        )

    def _get_lock(self, fh: IOT) -> IOT:
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

    def _prepare_fh(self, fh: IOT) -> IOT:
        """Make the locked filehandle ready for the caller.

        Truncation happens here rather than in `open`, so that a lock
        opened with mode ``w`` cannot discard the contents of a file
        somebody else is holding. On POSIX the kernel append flag the
        ``a`` substitution introduced is cleared again afterwards, so the
        handle honours seek positions exactly like the ``w`` mode the
        caller asked for, see `_restore_positional_writes`.

        Args:
            fh: The locked filehandle.

        Returns:
            The same filehandle, emptied and rewound when the lock was
            created with a truncating mode.
        """
        if self.truncate:
            fh.seek(0)
            fh.truncate(0)
            if os.name == 'posix':  # pragma: no branch - platform constant
                _restore_positional_writes(fh)  # pragma: not-posix

        return fh


class RLock(Lock[IOT]):
    """
    A reentrant lock, functions in a similar way to threading.RLock in that it
    can be acquired multiple times.  When the corresponding number of release()
    calls are made the lock will finally release the underlying file lock.

    Like `Lock`, the open mode picks the filehandle specialization: text
    modes yield an ``RLock[IO[str]]``, binary modes an
    ``RLock[IO[bytes]]``, and a bare ``RLock`` annotation means the text
    variant.

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

    @typing.overload
    def __init__(
        self: RLock[typing.IO[str]],
        filename: Filename,
        mode: TextMode = 'a',
        timeout: float | None = None,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
        fail_when_locked: bool = False,
        flags: constants.LockFlags = LOCK_METHOD,
    ) -> None: ...

    @typing.overload
    def __init__(
        self: RLock[typing.IO[bytes]],
        filename: Filename,
        mode: BinaryMode,
        timeout: float | None = None,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
        fail_when_locked: bool = False,
        flags: constants.LockFlags = LOCK_METHOD,
    ) -> None: ...

    # Catch-all for non-literal modes, see the matching `Lock` overload.
    @typing.overload
    def __init__(
        self: RLock[typing.IO[typing.Any]],
        filename: Filename,
        mode: Mode,
        timeout: float | None = None,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
        fail_when_locked: bool = False,
        flags: constants.LockFlags = LOCK_METHOD,
    ) -> None: ...

    def __init__(
        self,
        filename: Filename,
        mode: Mode = 'a',
        timeout: float | None = None,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
        fail_when_locked: bool = False,
        flags: constants.LockFlags = LOCK_METHOD,
    ) -> None:
        """Configure the lock and start with an acquire count of zero.

        Args:
            filename: Path of the file to lock.
            mode: Open mode for the file, see `Lock`.
            timeout: See `LockBase`. `None` selects `DEFAULT_TIMEOUT`.
                Only an explicit value counts as "a timeout was given"
                for the blocking mode warning documented on `Lock`.
            check_interval: See `LockBase`.
            fail_when_locked: See `LockBase`.
            flags: Locking flags, see `Lock`.

        Note:
            Unlike `Lock`, this constructor takes neither
            ``raise_on_release_error`` nor extra `open` keyword arguments.
        """
        # `Lock.__init__` is overloaded, and no overload's `self` type
        # can bind this class's still-unsolved type variable, so the
        # parent initialization runs through the plain `Lock._init`.
        super()._init(
            filename,
            mode,
            timeout,
            check_interval,
            fail_when_locked,
            flags,
            raise_on_release_error=False,
        )
        self._acquire_count = 0

    def acquire(
        self,
        timeout: float | None = None,
        check_interval: float | None = None,
        fail_when_locked: bool | None = None,
    ) -> IOT:
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
                first call only. Also raised when the instance claims to
                be acquired but holds no filehandle, which means the
                bookkeeping was corrupted, for example by tampering with
                the private state or a partially failed release. An
                ``assert`` would vanish under ``python -O`` and hand the
                caller `None` instead of a filehandle, so this is a real
                exception.
            OSError: As `Lock.acquire`, on the first call only.
        """
        fh: IOT
        with self._state_lock:
            if self._acquire_count >= 1:
                if self.fh is None:
                    raise exceptions.LockException(
                        'RLock claims to be acquired but holds no '
                        'filehandle, its state was corrupted. Refusing '
                        'to hand out None',
                    )
                self._acquire_count += 1
                return self.fh
        # First acquire: take the OS lock outside the state lock, so a
        # blocking or retrying acquire cannot stall another thread's
        # nested acquire or release behind it.
        fh = super().acquire(timeout, check_interval, fail_when_locked)
        with self._state_lock:
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
        fh: types.IO | None
        with self._state_lock:
            if self._acquire_count == 0:
                raise exceptions.LockException(
                    'Cannot release more times than acquired',
                )
            self._acquire_count -= 1
            # The count reaching zero and the claim of the filehandle
            # must be one atomic step: with the claim in a later scope a
            # racing acquire slipped in between, saw the count at zero
            # with the handle still published, and its fast path handed
            # out the very handle this thread then closed.
            fh = self._claim_fh() if self._acquire_count == 0 else None
        # Only the OS unlock and close run outside the state lock.
        if fh is not None:
            self._release_claimed_fh(fh)


def _fh_matches_path(fh: types.IO, filename: str) -> bool:  # pragma: not-posix
    """Return whether ``fh`` still refers to the file now at ``filename``.

    A competing releaser can unlink (and a third party recreate) ``filename``
    in the window between our ``open`` and our lock, which would leave two
    processes each holding a lock on a *different* inode for the same name
    (split-brain). Comparing the handle's inode with the path's inode detects
    that swap. This is a POSIX-only concern: on Windows a locked file cannot be
    unlinked, so no swap is possible. The comparison uses
    `os.path.samestat`, which checks the device as well as the inode, so a
    recycled inode number on another filesystem cannot alias the lock
    file.
    """
    try:
        return os.path.samestat(os.fstat(fh.fileno()), os.stat(filename))
    except FileNotFoundError:
        # The path was unlinked and not (yet) recreated.
        return False
    except ValueError:
        # ``fh`` is closed (``fileno()`` refuses closed files), usually
        # because a reentrant release claimed it: it certainly no longer
        # guards the path, and reporting that beats leaking the raw
        # `ValueError` through the held-lock verification.
        return False
    except OSError as error:
        if error.errno == errno.EBADF:
            # The descriptor died at the OS level under a still-open
            # file object (a reentrant release closing it between our
            # ``fileno()`` and the ``fstat``): same verdict as the
            # closed handle above.
            return False
        raise


#: Live `TemporaryFileLock` instances (and `PidFileLock`, which inherits
#: the registration) that `_release_locks_at_exit` releases when the
#: interpreter shuts down, each mapped to the pid of the owning process:
#: the one that constructed the lock, until a fresh acquire re-records
#: the acquiring process. A `weakref.WeakKeyDictionary`, so membership
#: never keeps a lock alive and collected locks drop out on their own.
_exit_releases: weakref.WeakKeyDictionary[TemporaryFileLock, int] = (
    weakref.WeakKeyDictionary()
)


def _release_locks_at_exit() -> None:
    """Release every still-live `TemporaryFileLock` at interpreter exit.

    Registered with `atexit` exactly once, at import time, instead of once
    per constructed lock: registering per instance and never unregistering
    made a long-lived process accumulate one dead callback for every lock
    it ever constructed. Releasing is a no-op for an instance that holds
    nothing, so already-released locks cost nothing here. Errors are
    suppressed, since the interpreter is on its way out and nobody is left
    to handle them.

    Locks owned by another process are skipped. A forked child inherits
    the parent's live locks (and this hook), and releasing them on the
    child's normal exit would unlink the parent's lock files while the
    parent still believes it holds them: the classic daemonize sequence
    of acquire-then-fork lost its lock the moment either side exited.
    Ownership is recorded at construction and re-recorded on every fresh
    acquire, so the process that actually took a lock is the one that
    releases it here.
    """
    current_pid: int = os.getpid()
    for lock, owner_pid in list(_exit_releases.items()):
        if owner_pid != current_pid:
            continue
        with contextlib.suppress(Exception):
            lock.release()


atexit.register(_release_locks_at_exit)


class TemporaryFileLock(Lock[typing.IO[str]]):
    """A `Lock` whose lock file only exists while the lock is held.

    Use it when the file is purely a mutex and leaving it behind would be
    litter. `release` unlinks the path, and a single module level
    `atexit` hook does the same for a program that forgets to release
    and still holds the lock when the interpreter shuts down. Garbage
    collection of the lock object is deliberately not a trigger. A
    finalizer that unlinked the path used to destroy locks whose
    filehandle the caller was still using.

    That hook tracks instances through a weak mapping, so it neither
    keeps a lock alive nor grows with the number of locks a process has
    ever constructed. The exit cleanup therefore needs the wrapper to
    still be referenced: a lock collected earlier drops out of the
    mapping and leaves the hook with nothing to do, so a still-locked,
    discarded wrapper leaves its file behind at exit. The OS lock itself
    is released once the filehandle is closed or collected, so the
    leftover is litter rather than a held lock. The hook also only
    releases locks owned by the exiting process itself: a forked child
    inherits the parent's live locks, and releasing them on the child's
    exit would unlink the files of a lock the parent still holds. The
    owning pid is recorded at construction and re-recorded on every
    fresh acquire, so a lock constructed in the parent but acquired
    inside a forked child is cleaned up at that child's exit, while the
    daemonize shape (acquire, fork, child exits) keeps the parent's lock
    intact.

    Warning:
        The exit hook is pid-aware, but a ``with`` block is not: a child
        forked inside ``with lock:`` inherits the block and runs
        ``__exit__`` when it falls out of it, releasing the lock and
        unlinking the file while the parent still believes it holds
        them. The classic daemonize sequence (fork inside the guarded
        block, parent exits or child does the work) must therefore
        either fork outside the ``with`` block or make sure only one of
        the two processes leaves it, for instance by ending the child
        with ``os._exit`` instead of falling through.

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
        filename: Filename = '.lock',
        timeout: float | None = None,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
        fail_when_locked: bool = True,
        flags: constants.LockFlags = LOCK_METHOD,
        *,
        raise_on_release_error: bool = False,
    ) -> None:
        """Configure the lock and arm the interpreter exit cleanup.

        Args:
            filename: Path of the lock file, ``'.lock'`` by default.
                Anything `str` accepts, including `pathlib.Path`. It is
                created on acquire and removed on release.
            timeout: See `LockBase`. `None` selects `DEFAULT_TIMEOUT`.
                Only an explicit value counts as "a timeout was given"
                for the blocking mode warning documented on `Lock`.
            check_interval: See `LockBase`.
            fail_when_locked: See `LockBase`. Defaults to `True` here,
                unlike `Lock`: a lock file that exists usually means a live
                owner, so failing straight away is the more useful answer.
            flags: Locking flags, see `Lock`.
            raise_on_release_error: Report errors from `release`, the
                unlink included, instead of suppressing and logging
                them. See `Lock`. Accepted here since 4.2.0; strict mode
                used to require setting the attribute after
                construction.

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
            raise_on_release_error=raise_on_release_error,
        )
        # Track the instance for the module level atexit hook. The weak
        # mapping keeps no strong reference, so garbage collection stays
        # in charge of locks that die before the interpreter does, and
        # construction registers nothing with atexit itself. The pid pins
        # the exit time cleanup to the owning process; `acquire`
        # re-records it, so whichever process actually takes the lock
        # (possibly a forked child) is the one whose exit cleans it up.
        _exit_releases[self] = os.getpid()

    def acquire(
        self,
        timeout: float | None = None,
        check_interval: float | None = None,
        fail_when_locked: bool | None = None,
    ) -> typing.IO[str]:
        """Acquire the lock, guarding against split-brain path swaps.

        Re-acquiring while already holding the lock is an idempotent no-op
        that returns the held filehandle and ignores the arguments, like
        `RLock`. When a third party unlinked or replaced the lock file in
        the meantime the re-acquire raises
        `~portalocker.exceptions.LockException` instead and leaves the held
        filehandle untouched. See `TemporaryFileLock._acquire_verified` for
        the full contract.

        A fresh acquire also records the calling process as the owner for
        the interpreter-exit cleanup. Construction records it too, but a
        lock constructed before a fork and acquired inside the child
        belongs to the child, and its exit must clean the file up. The
        idempotent re-acquire deliberately leaves the recorded owner
        alone: a forked child re-acquiring an inherited held lock gets
        the shared filehandle back, and reassigning ownership to the
        child would let its exit unlink the file the parent still
        depends on.
        """
        freshly_acquired: bool = self.fh is None
        fh: typing.IO[str] = self._acquire_verified(
            self,
            self.filename,
            timeout,
            check_interval,
            fail_when_locked,
        )
        if freshly_acquired:
            _exit_releases[self] = os.getpid()
        return fh

    @staticmethod
    def _acquire_verified(
        lock: Lock[typing.IO[str]],
        filename: str,
        timeout: float | None,
        check_interval: float | None,
        fail_when_locked: bool | None,
    ) -> typing.IO[str]:
        """Acquire ``lock`` and confirm the handle still names ``filename``.

        A competing releaser can unlink (and a third party recreate)
        ``filename`` between our ``open`` and our lock, so two processes
        could each hold a lock on a different inode for the same name.
        After locking we verify the handle still points at the current
        path. No-op on Windows, where a locked file cannot be swapped.

        The contract, in three parts:

        * Already held and still valid: when ``lock`` holds a filehandle
          whose inode still matches ``filename``, the call is an idempotent
          no-op returning that same filehandle. The held lock is never
          released and re-acquired, since the gap between the two is a
          window a competitor can win.
        * Already held but compromised: when the held filehandle no longer
          matches ``filename``, a third party unlinked or replaced the path
          (tmpwatch cleaning ``/tmp`` is enough) and mutual exclusion is
          already lost. The call raises
          `~portalocker.exceptions.LockException` naming the external
          unlink and leaves the held filehandle untouched: it is not closed
          and the lock is not silently swapped to the new inode, because
          only the caller knows whether its pending writes still matter.
        * Fresh acquire: the verify-and-retry loop runs against one shared
          deadline. The first attempt passes the caller's ``timeout``
          through unchanged and every retry is handed only the remaining
          budget, so the total wall time respects the single timeout
          instead of compounding per retry. An iteration that finds a stale
          handle releases it and then either retries or raises. The loop
          never ends on a bare release.

        Shared by ``TemporaryFileLock`` and the ``PidFileLock`` sidecar lock so
        both surfaces get the same guarantee.

        Raises:
            ~portalocker.exceptions.LockException: The lock was already
                held, but ``filename`` was unlinked or replaced externally
                in the meantime.
            ~portalocker.exceptions.AlreadyLocked: A fresh acquire kept
                finding the path replaced until the timeout budget ran out,
                or the underlying `Lock.acquire` gave up on contention.
        """
        held_fh: typing.IO[str] | None = lock.fh
        if held_fh is not None:
            if os.name == 'nt':  # Windows: a locked file can't be swapped.
                return held_fh  # pragma: not-nt
            if _fh_matches_path(held_fh, filename):  # pragma: not-posix
                return held_fh  # pragma: not-posix
            raise exceptions.LockException(  # pragma: not-posix
                f'{filename!r} was unlinked or replaced externally while '
                f'the lock was held: the lock is compromised, the held '
                f'filehandle is untouched',
            )

        f_timeout: float = coalesce(timeout, lock.timeout, 0.0)
        deadline: float = time.perf_counter() + f_timeout
        # The first attempt passes the caller's timeout through unchanged.
        # Every retry only gets what remains of the shared deadline.
        attempt_timeout: float | None = timeout
        fh: typing.IO[str]
        for _ in lock._timeout_generator(timeout, check_interval):
            fh = Lock.acquire(
                lock,
                attempt_timeout,
                check_interval,
                fail_when_locked,
            )
            if fh.closed:
                # A reentrant release (a signal handler is the usual
                # culprit) claimed and closed the handle between the
                # acquire returning it and this verification. There is
                # nothing to verify and nothing left to release, so try
                # again within the remaining budget.
                attempt_timeout = max(0.0, deadline - time.perf_counter())
                continue
            if os.name == 'nt':  # Windows: a locked file can't be swapped.
                return fh  # pragma: not-nt
            if _fh_matches_path(fh, filename):  # pragma: not-posix
                return fh  # pragma: not-posix
            # Stale handle: the path was unlinked+recreated behind our back.
            Lock.release(lock)  # pragma: not-posix
            attempt_timeout = max(  # pragma: not-posix
                0.0,
                deadline - time.perf_counter(),
            )
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
        (a double release, or a release after a failed acquire) must never
        unlink the path out from under the current holder. The held
        filehandle is claimed atomically before the first OS call, so a
        release that reenters from a signal handler, or runs concurrently
        in another thread, finds the state already claimed and removes
        nothing: the outer release still holds the OS lock at that point,
        and only it unlinks the path, exactly once.

        On POSIX the unlink also only runs when the held filehandle still
        names the path. After a third party unlinked or replaced the lock
        file the path belongs to whoever recreated it, so a compromised
        holder frees its OS lock but leaves the path alone, with a warning
        in the log instead of a deleted competitor lock.

        Raises:
            Exception: An unlink failure other than the file already being
                gone, but only when ``raise_on_release_error`` is set, and
                the lock itself is always released first. By default such
                failures are suppressed and logged at warning level, like
                the unlock and close failures in `Lock.release`.
        """
        fh: types.IO | None = self._claim_fh()
        if fh is None:
            # Not holding the lock; the path (if any) belongs to another
            # holder now.
            return
        unlink_error: Exception | None
        if os.name == 'nt':  # pragma: not-nt
            unlink_error = self._release_nt(fh)
        else:  # pragma: not-posix
            unlink_error = self._release_posix(fh)
        if unlink_error is None:
            return
        if self.raise_on_release_error:
            raise unlink_error
        logger.warning(
            'suppressed error while removing lock file %r: %r',
            self.filename,
            unlink_error,
        )

    def _release_nt(self, fh: types.IO) -> Exception | None:
        """Unlock and close first, then remove the file with a short retry.

        A locked file cannot be unlinked on Windows, hence the ordering,
        and an AV or indexing scanner can hold the freshly closed file
        open for a moment, hence the retry. Only `PermissionError` is
        transient in that way, so only it is retried, and the retry sleep
        is skipped after the final attempt: sleeping after giving up only
        delays the caller. Any other failure is captured on the first
        attempt, like the POSIX path captures its unlink errors, so the
        flag contract of `release` applies to it instead of the error
        escaping regardless of the flag. Both changed in 4.2.0.

        Args:
            fh: The filehandle claimed by `release`.

        Returns:
            The unlink failure for `release` to report (the last denial
            after the retries, or the first non-retryable error), or
            `None` when the file was removed or was already gone.
        """
        self._release_claimed_fh(fh)
        unlink_error: Exception | None = None
        last_attempt: int = 4
        # The retry loop needs per-attempt error classification, so the
        # try/except stays inside it despite the PERF203 overhead.
        for attempt in range(last_attempt + 1):
            try:
                os.unlink(self.filename)
            except FileNotFoundError:  # noqa: PERF203
                # Already gone, nothing left to remove.
                return None
            except PermissionError as error:
                unlink_error = error
                if attempt < last_attempt:
                    time.sleep(0.05)
            except Exception as error:
                # Not a transient share violation: retrying cannot help.
                return error
            else:
                return None
        return unlink_error

    def _release_posix(
        self, fh: types.IO
    ) -> Exception | None:  # pragma: not-posix
        """Unlink while the lock is still held, then unlock and close.

        The ordering closes the split-brain window: a competing acquirer
        cannot grab the path between unlock and unlink when the unlink
        comes first. The unlock must run even when the unlink fails (e.g.
        a `PermissionError` from a read-only directory), otherwise the
        error would leave the lock held forever.

        Args:
            fh: The filehandle claimed by `release`. It still carries the
                OS lock while the unlink runs.

        Returns:
            The unlink failure for the caller to report, or `None` when
            the file was removed or was already gone.

        Raises:
            Exception: Whatever `Lock._release_claimed_fh` raises, which
                it only does with ``raise_on_release_error`` set. A failed
                unlink is kept visible by chaining it onto that error.
        """
        unlink_error: Exception | None = None
        try:
            if not _fh_matches_path(fh, self.filename):
                # After a third party unlinked or replaced the lock file
                # the path belongs to whoever recreated it, so removing
                # it would destroy that holder's lock.
                logger.warning(
                    'not unlinking %r: it no longer belongs to this '
                    'lock (unlinked or replaced externally)',
                    self.filename,
                )
            else:
                os.unlink(self.filename)
        except FileNotFoundError:
            # Already gone, nothing left to remove.
            pass
        except Exception as error:
            unlink_error = error
        try:
            self._release_claimed_fh(fh)
        except Exception as release_error:
            if unlink_error is not None:
                raise release_error from unlink_error
            raise
        return unlink_error


class PidFileLock(TemporaryFileLock):
    """
    A lock that writes the current process PID to the file and can read
    the PID of the process that currently holds the lock.

    When used as a context manager:

    - Returns None if we successfully acquired the lock
    - Returns the PID (int) if another process holds the lock
    - Raises AlreadyLocked if another process holds the lock but its PID
      cannot be read, so a missing or corrupt PID file can never make a
      bystander believe it is the holder

    The classic "only one instance of this daemon" lock. Two files are
    involved: `filename` holds the readable PID, and a sidecar
    ``<filename>.lock`` next to it carries the actual operating system
    lock. The split exists because Windows locking is mandatory, so a lock
    taken on the PID file itself would stop anyone from reading it.

    Warning:
        A ``with lock:`` block does not survive a fork inside it: the
        child inherits the block and runs ``__exit__`` when it falls out
        of it, releasing the lock and unlinking the PID and sidecar
        files while the parent still believes it holds them. The classic
        daemonize sequence must fork outside the ``with`` block, or end
        the child with ``os._exit`` so the inherited block is never
        left. The interpreter-exit cleanup itself is pid-aware and only
        runs in the process that acquired the lock.

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
        filename: Filename = '.pid',
        timeout: float | None = None,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
        fail_when_locked: bool = True,
        flags: constants.LockFlags = LOCK_METHOD,
        *,
        raise_on_release_error: bool = False,
    ) -> None:
        """Configure the lock and derive the sidecar lock file name.

        Args:
            filename: Path of the PID file, ``'.pid'`` by default.
                Anything `str` accepts, including `pathlib.Path`. The
                sidecar lock file is this path with ``.lock`` appended.
            timeout: See `LockBase`. `None` selects `DEFAULT_TIMEOUT`.
                Only has an effect together with
                ``fail_when_locked=False``.
            check_interval: See `LockBase`.
            fail_when_locked: See `LockBase`. Defaults to `True`, as for
                `TemporaryFileLock`.
            flags: Locking flags applied to the sidecar file, see `Lock`.
            raise_on_release_error: Report errors from `release`, the
                PID file and sidecar unlinks included, instead of
                suppressing and logging them. See `Lock`. Accepted here
                since 4.2.0; strict mode used to require setting the
                attribute after construction.

        Note:
            Neither file is created here; that happens on acquire.
        """
        super().__init__(
            filename=filename,
            timeout=timeout,
            check_interval=check_interval,
            fail_when_locked=fail_when_locked,
            flags=flags,
            raise_on_release_error=raise_on_release_error,
        )
        self._acquired_lock = False
        # Use a sidecar file for the actual OS-level lock so the PID file
        # remains readable on platforms (notably Windows) with mandatory
        # byte-range locking. This preserves existing public API/behavior.
        self._lockfile = f'{self.filename}.lock'
        self._inner_lock: Lock | None = None

    def _write_pid(self) -> None:
        """Atomically publish the current PID, preserving close errors.

        The PID is written to a temporary file next to `filename` and then
        moved over it with `os.replace`, so a concurrent `read_pid` sees
        either the previous holder's complete PID or ours, never a
        truncated or empty file. The old truncate-in-place approach
        exposed both windows between taking the sidecar lock and finishing
        the write. The temporary file is removed again on any failure, and
        a close failure after a write failure is chained onto the original
        error instead of replacing it.
        """
        temp_path: str = f'{self.filename}.{os.getpid()}.tmp'
        pid_file: typing.TextIO = open(  # noqa: SIM115
            temp_path,
            'w',
            encoding='ascii',
        )
        try:
            try:
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
            os.replace(temp_path, self.filename)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp_path)
            raise

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

        # Clear the published state only when this rollback's own sidecar
        # is the published one (a failure after publication, e.g. the
        # exit-hook registration raising). A never-published failed
        # sidecar leaves nothing to clear, and wiping unconditionally
        # erased the state a *winning* thread had published on the same
        # instance, orphaning its held sidecar.
        with self._state_lock:
            if self._inner_lock is inner_lock:
                self._inner_lock = None
                self._acquired_lock = False
        return cleanup_error

    def acquire(
        self,
        timeout: float | None = None,
        check_interval: float | None = None,
        fail_when_locked: bool | None = None,
    ) -> typing.IO[str]:
        """Lock the sidecar file and publish the current PID.

        Calling this on an instance that already holds the lock first
        verifies that the sidecar lock file still names the held lock,
        then returns the held sidecar filehandle as-is: the arguments are
        ignored and neither the sidecar lock nor the PID file is touched,
        so re-acquiring while held is cheap and can never drop the lock,
        not even for an instant. When a third party unlinked or replaced
        the sidecar in the meantime the re-acquire raises
        `~portalocker.exceptions.LockException` and leaves the held
        filehandle untouched, exactly like `TemporaryFileLock.acquire`.

        Args:
            timeout: Overrides `timeout` for this call. See `LockBase`: the
                argument wins when it is not `None`, otherwise the instance
                attribute applies. Only used when `fail_when_locked`
                resolves to `False`.
            check_interval: Overrides `check_interval` for this call, under
                the same rules.
            fail_when_locked: Overrides `fail_when_locked` for this call.

        Returns:
            The filehandle of the sidecar lock file. It exists to satisfy
            the `Lock` typing contract; read the PID through
            `PidFileLock.read_pid` instead of from this handle.

        Raises:
            ~portalocker.exceptions.AlreadyLocked: Somebody else holds the
                lock, whether the contention surfaced through
                `fail_when_locked` or through an expired timeout.
                Contention is the only thing this type means here.
            ~portalocker.exceptions.LockException: A terminal, retry-proof
                failure. Either the sidecar backend cannot lock at all
                (``ENOLCK``, an unsupported filesystem), or the instance
                already holds the lock but the sidecar lock file was
                unlinked or replaced externally in the meantime. Neither
                is contention, so neither is dressed up as
                `AlreadyLocked`. Changed in 4.2.0: plain lock exceptions
                from the sidecar used to be normalized to
                `AlreadyLocked`, which told callers to retry failures
                retrying cannot fix.
            Exception: Anything else the sidecar `Lock` raises, such as an
                `OSError` from opening it, propagates unchanged. So does a
                failure to publish the PID, for instance because the
                filesystem is full; the sidecar lock is rolled back first,
                so that failure never leaves the lock held, and a rollback
                error of its own is chained onto the original.
        """
        held_lock: Lock | None = self._inner_lock
        if held_lock is not None and held_lock.fh is not None:
            # Already holding: the documented idempotent re-acquire.
            # Building a replacement sidecar Lock here would discard the
            # held one, and the discarded object's garbage collected
            # teardown used to release the held OS lock mid-call, a window
            # a competitor could win. Routed through `_acquire_verified`
            # so its held branch applies here exactly as it does for
            # `TemporaryFileLock`: a still-valid sidecar returns the held
            # filehandle untouched, a sidecar that a third party unlinked
            # or replaced raises instead of silently returning a
            # filehandle whose lock no longer guards the path.
            return self._acquire_verified(
                held_lock,
                self._lockfile,
                timeout,
                check_interval,
                fail_when_locked,
            )

        # Resolve the call arguments against the instance attributes first
        # (the argument wins when it is not None, see `LockBase`), so the
        # sidecar Lock below inherits this instance's retry policy instead
        # of falling back to the module defaults.
        timeout_: float = coalesce(timeout, self.timeout)
        check_interval_: float = coalesce(check_interval, self.check_interval)
        fail_when_locked_: bool = coalesce(
            fail_when_locked,
            self.fail_when_locked,
        )

        # Acquire the sidecar lock file using a normal Lock instance.
        inner_lock = Lock(
            self._lockfile,
            mode='a',
            timeout=timeout_ if fail_when_locked_ is False else 0,
            check_interval=(
                check_interval_ if fail_when_locked_ is False else 0.0
            ),
            fail_when_locked=True,
            flags=LOCK_METHOD,
        )
        # `_inner_lock` is only published once the sidecar lock is held
        # *and* the PID is written, at the very end. Publishing it earlier
        # would let an acquire interrupted by `KeyboardInterrupt` or
        # `SystemExit` while waiting (a SIGTERM handler calling `sys.exit`
        # is the usual daemon idiom, and `except Exception` catches
        # neither) leave the instance claiming a lock it never took, and
        # its release would then unlink files that belong to the actual
        # holder.
        sidecar_fh: typing.IO[str]
        try:
            # Reuse the split-brain guard so the sidecar lock gets the same
            # inode-verification as a direct `TemporaryFileLock`.
            sidecar_fh = self._acquire_verified(
                inner_lock,
                self._lockfile,
                timeout_,
                check_interval_,
                fail_when_locked_,
            )
        except BaseException:
            # Roll the sidecar back on every failed verified acquire. On
            # plain contention the sidecar `Lock` already cleaned itself
            # up and the rollback is a no-op, but an error raised *after*
            # the sidecar lock was taken (an `OSError` from the inode
            # verification, or an interrupt such as `KeyboardInterrupt`
            # landing before this method publishes the lock) must not
            # strand the OS lock on a traceback-pinned local, where
            # refcounting would never free it. The exception itself
            # propagates unchanged: `AlreadyLocked` already means
            # contention, and a plain `LockException` is a terminal
            # backend failure (`ENOLCK`, an unsupported filesystem) that
            # 4.2.0 no longer dresses up as `AlreadyLocked`, because
            # telling callers to retry a permanent failure contradicts
            # the retry contract.
            self._rollback_failed_acquire(inner_lock)
            raise

        try:
            self._write_pid()
            with self._state_lock:
                self._inner_lock = inner_lock
                self._acquired_lock = True
            # Successful publication makes this process the owner for the
            # interpreter-exit cleanup: a lock constructed before a fork
            # but acquired inside the child belongs to the child.
            _exit_releases[self] = os.getpid()
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
        except BaseException:
            # An interrupt (`KeyboardInterrupt`, `SystemExit`) here must
            # not strand the OS sidecar lock on the local variable. A
            # pinned traceback keeps this frame, and with it the sidecar
            # `Lock`, alive, so refcount collection never releases the
            # lock and every contender stays blocked. Roll the sidecar
            # back and let the interrupt propagate. A rollback failure of
            # its own is deliberately dropped: the interrupt matters more
            # than a secondary cleanup error.
            self._rollback_failed_acquire(inner_lock)
            raise

        # No need to keep a direct fh on the PID file; return the locally
        # bound sidecar handle to satisfy the context manager typing
        # contract. Deliberately not read back from the instance: a
        # signal handler's `release()` landing between the publication
        # above and this return claims the shared state, and an assert
        # on it fired in exactly that window (returning `None` under
        # ``python -O``). The local stays valid either way; after such a
        # reentrant release it is simply already closed, as for any
        # release-right-after-acquire.
        return sidecar_fh

    def read_pid(self) -> int | None:
        """Read the PID from the lock file, if it exists and is readable.

        Returns:
            The PID recorded in the file, or `None` when the file is
            missing, unreadable, or does not contain a plain positive
            decimal number. Validation is strict on purpose: only ASCII
            digits with a value greater than zero pass, so signs,
            underscores, non-ASCII digits, zero and negative values are
            all treated as unreadable. `int` happily parses ``-1`` or
            ``1_000``, and the obvious consumer feeds the result straight
            to ``os.kill``, where ``-1`` signals every process the user
            owns. The file is read as bytes and validated as ASCII, so
            content the locale encoding cannot decode also comes back as
            `None` instead of raising ``UnicodeDecodeError`` as it did
            before 4.2.0. Digit runs longer than 20 characters are junk
            by the same rule (no real PID needs them), and rejecting
            them before the `int` call keeps CPython's 4300-digit
            conversion limit from escaping as a ``ValueError``. Note
            that a returned PID only says who *wrote* the file, the
            process may since have died.
        """
        pid, _error = self._read_pid()
        return pid

    def _read_pid(self) -> tuple[int | None, OSError | None]:
        """Read and validate the PID, also reporting the read error.

        The error half exists for `__enter__`: when a contended entry has
        to refuse fail-open because the holder PID is unreadable, the
        raised `AlreadyLocked` chains from the actual `OSError` instead of
        swallowing it, so the traceback names the real problem (a missing
        file, a permission error). `read_pid` keeps its simpler public
        contract and drops the error half.

        Returns:
            A ``(pid, error)`` pair. ``pid`` follows the `read_pid` rules
            and ``error`` is the `OSError` that made the file unreadable,
            or `None` when the file was readable (even if its content did
            not validate).
        """
        try:
            with open(self.filename, 'rb') as f:
                raw: bytes = f.read()
        except OSError as error:
            return None, error
        # A valid PID is ASCII digits, so the file is read as bytes and
        # decoded as ASCII with undecodable bytes replaced: the
        # replacement characters fail the digit validation below exactly
        # like any other junk. Reading text with the locale encoding
        # instead let bytes the locale cannot decode (arbitrary junk on
        # a cp1252 Windows, invalid UTF-8 on POSIX) escape as a
        # `UnicodeDecodeError` where the contract promises `None`.
        content: str = raw.decode('ascii', errors='replace').strip()
        if not (content.isascii() and content.isdigit()):
            return None, None
        if len(content) > 20:
            # CPython refuses `int` conversions past 4300 digits with a
            # `ValueError` (see `sys.set_int_max_str_digits`), and no
            # real PID comes close anyway: a 64-bit ``pid_max`` is 20
            # digits. Longer content is junk, reported like any other.
            return None, None
        pid: int = int(content)
        if pid > 0:
            return pid, None
        return None, None

    def fail_closed(self) -> contextlib.AbstractContextManager[None]:
        """Return a context that enters only after acquiring this lock.

        The fail-closed counterpart of using the lock directly as a context
        manager: contention aborts before the block runs instead of running
        it with a PID in hand.

        Returns:
            A context manager that binds `None` and guarantees the block
            only runs while this process owns the lock.

        Raises:
            ~portalocker.exceptions.AlreadyLocked: On entry, when another
                process holds the lock. Its ``holder_pid`` attribute
                carries the competing PID when it could be read.
            Exception: On entry, anything else `PidFileLock.acquire`
                raises, unchanged: a terminal
                `~portalocker.exceptions.LockException` from a backend
                that cannot lock at all, an `OSError` from opening the
                sidecar, or a failed PID publication; the same
                pass-through class documented on `PidFileLock.__enter__`.

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
            holding it.

        Raises:
            ~portalocker.exceptions.AlreadyLocked: The lock is held by
                another process *and* that holder's PID cannot be read
                (the PID file is missing, unreadable or invalid).
                Returning `None` in that case would falsely report this
                process as the holder and run the block without mutual
                exclusion, so an unreadable holder fails closed.
            Exception: Anything `acquire` raises other than
                `AlreadyLocked`. Readable contention is turned into a
                return value.

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
        except exceptions.AlreadyLocked as exc:
            # Another process holds the lock, try to read its PID
            holder_pid, read_error = self._read_pid()
            if holder_pid is None:
                # The lock is held but the holder's PID cannot be read:
                # the PID file is missing, unreadable or invalid.
                # Returning the `None` we-are-the-holder sentinel here
                # would make the caller run its exclusive block next to a
                # live holder, so a broken PID file fails closed instead.
                # The cause chain names the real problem: the read error
                # when there was one, the contention otherwise. The
                # contention always stays reachable as the context.
                raise exceptions.AlreadyLocked(
                    'the lock is held but the holder PID could not be '
                    'read, refusing to report this process as the holder',
                ) from (read_error if read_error is not None else exc)
            return holder_pid

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
        alone. When this instance did take the lock, the release is routed
        through `Lock.__exit__`, so its guarantees apply here unchanged: a
        release failure never replaces the exception leaving the block (it
        is chained as its ``__context__`` with a note attached), and with
        a clean block a release failure only surfaces when
        ``raise_on_release_error`` is set.

        Args:
            exc_type: Type of the exception leaving the block, if any.
            exc_value: The exception instance, if any.
            traceback: The traceback of that exception, if any.

        Returns:
            `None`; exceptions from the block keep propagating.

        Raises:
            Exception: Whatever `release` raises, but only when the block
                itself ended without an exception, exactly as documented
                on `Lock.__exit__`. With the default
                ``raise_on_release_error=False`` that is nothing at all.
        """
        if not self._acquired_lock:
            return None
        return super().__exit__(exc_type, exc_value, traceback)

    def release(self) -> None:
        """Release the sidecar lock and remove the PID + sidecar files.

        On POSIX both the PID file and the sidecar lock file are unlinked while
        the sidecar lock is *still held*, so a competing acquirer cannot grab
        the sidecar path in the window between unlock and unlink (split-brain).
        The PID file itself carries no OS lock (the sidecar holds it), but it
        is removed in the same held window for consistency. On Windows the
        locked sidecar cannot be unlinked, so only the sidecar removal
        happens after its release. The PID file is still unlinked *before*
        the sidecar lock is dropped, since removing it afterwards could
        delete the PID a fast successor has already published.

        Releasing an object that does not hold the sidecar is a no-op: a
        stale object (a double release, or a release after a failed
        acquire) must never unlink the PID or sidecar files out from under
        the current holder. The same applies to an object whose sidecar
        `Lock` no longer holds a filehandle, for instance after an acquire
        interrupted while waiting: the files belong to whoever holds the
        sidecar lock now. Both the sidecar lock and its filehandle are
        claimed atomically before the first OS call, so a release that
        reenters from a signal handler, or runs concurrently in another
        thread, finds the state already claimed and removes nothing: the
        outer release still holds the sidecar lock at that point, and only
        it unlinks the two files, exactly once.

        On POSIX the unlinks also only run when the held sidecar handle
        still names the sidecar path. After a third party unlinked or
        replaced the files a competitor may own them already, so a
        compromised holder frees its OS lock but leaves both paths alone,
        with a warning in the log instead of a deleted competitor lock.

        Raises:
            Exception: An unlink failure other than a file already being
                gone, but only when ``raise_on_release_error`` is set,
                and the sidecar lock itself is always released first. By
                default such failures are suppressed and logged at
                warning level, matching `TemporaryFileLock.release`.
                Before 4.2.0 the flag was ignored here: the POSIX branch
                leaked unlink errors regardless of it and the Windows
                branch swallowed them regardless of it.
        """
        inner_lock: Lock | None
        with self._state_lock:
            inner_lock, self._inner_lock = self._inner_lock, None
            self._acquired_lock = False
        if inner_lock is None:
            # Not holding the sidecar lock, so the PID and sidecar files
            # belong to whoever holds it now.
            return
        sidecar_fh: types.IO | None = inner_lock._claim_fh()
        if sidecar_fh is None:
            # The sidecar `Lock` holds no filehandle (an acquire
            # interrupted while waiting, or a concurrent claim won): the
            # OS lock is already gone and unlinking the paths would
            # destroy the current holder's lock.
            return
        # Mirror the strictness flag onto the sidecar, so unlock and
        # close failures follow the same policy as this lock's own
        # errors instead of the sidecar's construction-time default.
        inner_lock.raise_on_release_error = self.raise_on_release_error
        unlink_error: Exception | None
        if os.name == 'nt':
            unlink_error = self._release_files_nt(inner_lock, sidecar_fh)
        else:  # pragma: not-posix
            unlink_error = self._release_files_posix(inner_lock, sidecar_fh)
        if unlink_error is None:
            return
        if self.raise_on_release_error:
            raise unlink_error
        logger.warning(
            'suppressed error while removing lock files %r and %r: %r',
            self.filename,
            self._lockfile,
            unlink_error,
        )

    def _release_files_nt(
        self,
        inner_lock: Lock,
        sidecar_fh: types.IO,
    ) -> Exception | None:
        """Tear down the Windows way: PID file, sidecar unlock, sidecar.

        The PID file is unlinked first, while the sidecar lock is still
        held: removing it after the release could delete the PID a fast
        successor has already published. The locked sidecar file itself
        cannot be unlinked on Windows, so its removal has to follow its
        release.

        Args:
            inner_lock: The claimed sidecar `Lock`.
            sidecar_fh: Its claimed filehandle, still carrying the OS
                lock.

        Returns:
            The first unlink failure for `release` to report, or `None`.

        Raises:
            Exception: Whatever `Lock._release_claimed_fh` raises, which
                it only does with ``raise_on_release_error`` set. A failed
                PID file unlink is kept visible by chaining it onto that
                error.
        """
        unlink_error: Exception | None = None
        try:
            os.unlink(self.filename)
        except FileNotFoundError:
            pass
        except Exception as error:
            unlink_error = error
        try:
            inner_lock._release_claimed_fh(sidecar_fh)
        except Exception as release_error:
            if unlink_error is not None:
                raise release_error from unlink_error
            raise
        try:
            os.unlink(self._lockfile)
        except FileNotFoundError:
            pass
        except Exception as error:
            if unlink_error is None:
                unlink_error = error
            else:
                logger.warning(
                    'suppressed additional error while removing %r: %r',
                    self._lockfile,
                    error,
                )
        return unlink_error

    def _release_files_posix(
        self,
        inner_lock: Lock,
        sidecar_fh: types.IO,
    ) -> Exception | None:  # pragma: not-posix
        """Unlink both files while the sidecar lock is still held.

        The ordering closes the split-brain window, exactly like
        `TemporaryFileLock._release_posix`, and the unlinks only run when
        the held sidecar handle still names the sidecar path. The sidecar
        unlock must run even when an unlink fails (e.g. a
        `PermissionError` from a read-only directory), otherwise the
        error would leave the sidecar held forever.

        Args:
            inner_lock: The claimed sidecar `Lock`.
            sidecar_fh: Its claimed filehandle, still carrying the OS
                lock while the unlinks run.

        Returns:
            The first unlink failure for `release` to report, or `None`.

        Raises:
            Exception: Whatever `Lock._release_claimed_fh` raises, which
                it only does with ``raise_on_release_error`` set. A failed
                unlink is kept visible by chaining it onto that error.
        """
        unlink_error: Exception | None = None
        try:
            if _fh_matches_path(sidecar_fh, self._lockfile):
                unlink_error = self._unlink_owned_files()
            else:
                logger.warning(
                    'not unlinking %r and %r: the sidecar lock file '
                    'no longer belongs to this lock (unlinked or '
                    'replaced externally)',
                    self.filename,
                    self._lockfile,
                )
        finally:
            try:
                inner_lock._release_claimed_fh(sidecar_fh)
            except Exception as release_error:
                if unlink_error is not None:
                    raise release_error from unlink_error
                raise
        return unlink_error

    def _unlink_owned_files(self) -> Exception | None:  # pragma: not-posix
        """Unlink the PID file and the sidecar, reporting the first error.

        A file that is already gone is fine; any other failure on the
        first file must not stop the second removal, so the first error
        is captured and later ones are logged.

        Returns:
            The first unlink failure, or `None` when both files were
            removed or already gone.
        """
        unlink_error: Exception | None = None
        for path in (self.filename, self._lockfile):
            try:
                os.unlink(path)
            except FileNotFoundError:  # noqa: PERF203
                pass
            except Exception as error:
                if unlink_error is None:
                    unlink_error = error
                else:
                    logger.warning(
                        'suppressed additional error while removing %r: %r',
                        path,
                        error,
                    )
        return unlink_error


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
            ~portalocker.exceptions.AlreadyLocked: Another process holds
                the lock. The ``holder_pid`` attribute is filled in with
                the competing PID first, or with `None` when the PID file
                could not be read.
            Exception: Anything else `PidFileLock.acquire` raises passes
                through untouched: a terminal
                `~portalocker.exceptions.LockException` from a backend
                that cannot lock at all, an `OSError` from opening the
                sidecar file, or a failure to publish the PID, with the
                rollback error chained onto it.
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

    One instance holds at most one slot, atomically: threads sharing an
    instance cannot corrupt its bookkeeping, but only one of them gets
    the slot and the others see `~portalocker.exceptions.LockException`
    from the already-taken guard. Give each thread its own instance when
    every thread needs a slot of its own.

    The `fail_when_locked` handling diverges from every other lock in
    this module and is kept as it has behaved since 3.2.0. The flag is
    consulted only once the ``timeout`` has expired: a full semaphore
    always retries for the whole timeout, even with the flag set, where
    the other locks fail fast on the first attempt. And when time does
    run out, ``fail_when_locked=True`` (the default) raises
    `AlreadyLocked` while ``fail_when_locked=False`` returns `None`
    where the other locks raise, so check the return value.

    The slot files must survive for as long as a slot is held. A slot
    file that something else deletes mid-hold silently admits an extra
    holder, because the operating system lock lives on the deleted inode
    where no new acquirer can see it. The default `directory` is the
    system temporary directory, which tmp cleaners prune on many systems,
    so point `directory` somewhere exempt from cleanup for anything long
    running. On a multi-user system prefer a private directory as well: a
    slot file created by another user is typically not writable for you,
    and `acquire` then raises `PermissionError` instead of treating the
    slot as busy.

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
            fail_when_locked: See `LockBase`. Defaults to `True` here.
                Unlike the other lock classes the flag is consulted only
                once `timeout` has expired: a full semaphore retries for
                the whole timeout first, and the flag then decides
                between raising `AlreadyLocked` and returning `None`.

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
            # `stacklevel=2` attributes the warning to whoever constructed
            # the semaphore. At the default `stacklevel=1` Python blamed
            # this line instead, so the "once per location" filter
            # deduplicated the warning globally and every caller after the
            # first constructed a colliding semaphore without any warning
            # at all.
            warnings.warn(
                '`BoundedSemaphore` without an explicit `name` '
                'argument is deprecated, use NamedBoundedSemaphore',
                DeprecationWarning,
                stacklevel=2,
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
        expires. That order is fixed and identical in every process, so
        all contenders race for slot ``0`` first.

        Args:
            timeout: Overrides `timeout` for this call. See `LockBase`.
            check_interval: Overrides `check_interval` for this call.
            fail_when_locked: Overrides `fail_when_locked` for this call.
                Unlike the rest of the retry policy this one is consulted
                only after the timeout has expired: the semaphore always
                keeps trying for the full timeout, and this decides
                whether running out of time raises `AlreadyLocked` or
                returns `None`. Both outcomes diverge from the other lock
                classes, in timing and in type. See the class docstring.

        Returns:
            The `Lock` holding the slot that was taken, which is also
            stored as the `lock` attribute. `None` when no slot became
            free within the timeout and `fail_when_locked` resolves to
            `False`.

        Raises:
            ~portalocker.exceptions.AlreadyLocked: All slots stayed taken
                for the whole timeout and `fail_when_locked` resolves to
                `True`.
            ~portalocker.exceptions.LockException: This instance already
                holds a slot. Release it before acquiring again. Changed in
                4.2.0: this guard used to be an ``assert``, which
                ``python -O`` strips, and a second acquire then silently
                took a second slot and leaked the first. The guard is also
                enforced atomically inside `try_lock` since 4.2.0: two
                threads racing this method on one instance used to both
                take a slot, with the second publication overwriting the
                first and leaking that slot until garbage collection. Now
                exactly one thread wins and the other raises this
                exception.
            OSError: Raised straight through from `try_lock`, for instance
                `FileNotFoundError` when `directory` does not exist. The
                instance stays usable, so a later call can succeed once the
                cause is fixed.
        """
        if self.lock is not None:
            raise exceptions.LockException('Already locked')

        filenames = self.get_filenames()

        for n in self._timeout_generator(timeout, check_interval):
            logger.debug('trying lock (attempt %d) %r', n, filenames)
            if self.try_lock(filenames):
                return self.lock

        if coalesce(fail_when_locked, self.fail_when_locked):
            raise exceptions.AlreadyLocked()

        return None

    def try_lock(self, filenames: typing.Sequence[Filename]) -> bool:
        """Try each candidate file once and keep the first one that locks.

        A single sweep with no waiting: every candidate is locked with
        ``fail_when_locked=True``, so a busy slot is skipped immediately
        rather than waited on. The sweep itself runs *outside* the
        instance state lock: each slot attempt opens and locks a file,
        which releases the GIL, and holding the state lock across those
        OS calls left a wide window for ``os.fork`` in another thread to
        capture it locked and hang the child. Only the publication on
        the `lock` attribute takes the state lock, re-checking the
        already-taken guard in the same scope: a thread that locked a
        slot but finds another thread published first releases its slot
        again and raises, so two racing sweeps end with exactly one slot
        held either way.

        Args:
            filenames: The candidate slot files, tried in the given order.

        Returns:
            `True` when a slot was taken, in which case the `lock`
            attribute now holds its `Lock`. `False` when every candidate
            was already taken; the `lock` attribute is then left alone.

        Raises:
            ~portalocker.exceptions.LockException: This instance already
                holds a slot, checked when the sweep starts and re-checked
                atomically at publication. Before 4.2.0 a concurrent
                sweep took a second slot instead and the overwritten one
                leaked until garbage collection.
            Exception: Anything other than `AlreadyLocked` coming out of
                `Lock.acquire`, such as `FileNotFoundError` for a missing
                `directory`. The `lock` attribute is untouched, so the
                failure cannot brick the instance for later calls.
            BaseException: An interrupt (`KeyboardInterrupt`,
                `SystemExit`) landing between the successful slot lock
                and the end of its publication, re-raised after the
                slot has been rolled back. Without the rollback the OS
                lock would be stranded on a local that only refcount
                garbage collection releases, and a pinned traceback
                blocks the slot indefinitely - the same window
                `PidFileLock.acquire` closes for its sidecar.
        """
        if self.lock is not None:
            raise exceptions.LockException('Already locked')
        filename: Filename
        for filename in filenames:
            logger.debug('trying lock for %r', filename)
            lock = Lock(filename, fail_when_locked=True)
            # Only record the lock once it is actually held, and only
            # when no other thread published a slot meanwhile. Any
            # non-contention failure (e.g. a missing directory raising
            # `FileNotFoundError` from the underlying `open`) propagates
            # with `lock` still unset, so the instance stays usable.
            published: bool = False
            try:
                try:
                    lock.acquire()
                except exceptions.AlreadyLocked:
                    # Taken by someone else; try the next candidate file.
                    continue
                with self._state_lock:
                    if self.lock is None:
                        self.lock = lock
                        published = True
            except BaseException:
                # An interrupt after the slot lock succeeded must not
                # strand the OS lock on a traceback-pinned local, where
                # refcounting would never free it. When it landed after
                # the publication, un-publish first - guarded by
                # identity, so a slot another thread published stays
                # untouched - and give the slot back either way.
                # `Lock.acquire` rolls its own failures back before
                # raising, which makes the release below a no-op for
                # interrupts landing inside the acquire itself.
                with self._state_lock:
                    if self.lock is lock:
                        self.lock = None
                lock.release()
                raise
            if published:
                logger.debug('locked %r', filename)
                return True
            # Lost the publication race: another thread already holds a
            # slot through this instance. Give the extra slot back and
            # report the double acquire.
            lock.release()
            raise exceptions.LockException('Already locked')

        return False

    def release(self) -> None:
        """Give the slot back, if this instance holds one.

        The lock file itself is left on disk. Only the operating system
        lock is dropped, which is what makes the slot available again.
        Doing nothing when no slot is held keeps release safe to call
        any number of times, including from finalizers. The slot is
        claimed atomically under the instance state lock, so of several
        concurrent releases exactly one tears the slot lock down and the
        rest are no-ops.
        """
        lock: Lock | None
        with self._state_lock:
            lock, self.lock = self.lock, None
        if lock is not None:
            lock.release()


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

    The slot files live in `directory`, the shared system temporary
    directory by default. Prefer a private directory that no tmp cleaner
    prunes: see `BoundedSemaphore` for how a deleted or foreign-owned slot
    file breaks the semaphore.

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
