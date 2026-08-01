from __future__ import annotations

import dataclasses
import importlib.util
import multiprocessing
import multiprocessing.context
import multiprocessing.process
import multiprocessing.queues
import multiprocessing.synchronize
import os
import pathlib
import platform
import time
import typing
from unittest import mock

import pytest

import portalocker
from portalocker import LockFlags, utils

LockKind: typing.TypeAlias = typing.Literal['temporary', 'pid']

# On Windows without the optional pywin32 extra, shared locks are unsupported
# by design and raise ImportError (in the *spawned* children). Skip in the
# parent before spawning so the children are never started in that config.
_needs_win32_extra = pytest.mark.skipif(
    os.name == 'nt' and importlib.util.find_spec('win32file') is None,
    reason='shared locks on Windows require the pywin32 extra '
    '(portalocker[win32])',
)


@dataclasses.dataclass(order=True)
class LockResult:
    """Helper dataclass for multiprocessing lock results."""

    exception_class: type[BaseException] | None = None
    exception_message: str | None = None
    exception_repr: str | None = None


def make_temporary_lock(
    filename: str,
    lock_kind: LockKind,
    *,
    timeout: float,
    fail_when_locked: bool,
) -> portalocker.TemporaryFileLock:
    """Create one of the temporary-path lock implementations under test."""
    lock_type: type[portalocker.TemporaryFileLock]
    if lock_kind == 'temporary':
        lock_type = portalocker.TemporaryFileLock
    else:
        lock_type = portalocker.PidFileLock
    return lock_type(
        filename,
        timeout=timeout,
        fail_when_locked=fail_when_locked,
    )


def native_lock_path(filename: str, lock_kind: LockKind) -> str:
    """Return the pathname carrying the native OS lock."""
    if lock_kind == 'pid':
        return f'{filename}.lock'
    return filename


def hold_inode_waiter(
    filename: str,
    lock_kind: LockKind,
    native_filename: str,
    opened_event: multiprocessing.synchronize.Event,
    acquired_event: multiprocessing.synchronize.Event,
    release_event: multiprocessing.synchronize.Event,
    inode_queue: multiprocessing.queues.Queue[int],
) -> None:
    """Open the original inode, acquire, report it, and hold the lock."""
    original_get_fh: typing.Callable[
        [utils.Lock],
        typing.IO[typing.Any],
    ] = typing.cast(
        typing.Callable[[utils.Lock], typing.IO[typing.Any]],
        utils.Lock._get_fh,
    )

    def announce_open(lock: utils.Lock) -> typing.IO[typing.Any]:
        fh: typing.IO[typing.Any] = original_get_fh(lock)
        if lock.filename == native_filename:
            opened_event.set()
        return fh

    lock: portalocker.TemporaryFileLock = make_temporary_lock(
        filename,
        lock_kind,
        timeout=30,
        fail_when_locked=False,
    )
    try:
        with mock.patch.object(utils.Lock, '_get_fh', announce_open):
            fh: typing.IO[typing.Any] = lock.acquire()
        inode_queue.put(os.fstat(fh.fileno()).st_ino)
        acquired_event.set()
        if not release_event.wait(timeout=30):
            raise TimeoutError('parent did not release the inode waiter')
    finally:
        lock.release()


def lock(
    filename: str,
    fail_when_locked: bool,
    flags: LockFlags,
    timeout: float = 0.1,
    keep_locked: float = 0.05,
) -> LockResult:
    """Helper function for multiprocessing lock tests."""
    try:
        with portalocker.Lock(
            filename,
            timeout=timeout,
            fail_when_locked=fail_when_locked,
            flags=flags,
        ):
            time.sleep(keep_locked)
            return LockResult()

    except Exception as exception:
        return LockResult(
            type(exception),
            str(exception),
            repr(exception),
        )


def shared_lock(filename, **kwargs):
    """Helper for shared lock in multiprocessing tests."""
    with portalocker.Lock(
        filename,
        timeout=0.1,
        fail_when_locked=False,
        flags=LockFlags.SHARED | LockFlags.NON_BLOCKING,
    ):
        time.sleep(0.2)
        return True


def shared_lock_fail(filename, **kwargs):
    """Helper for shared lock fail in multiprocessing tests."""
    with portalocker.Lock(
        filename,
        timeout=0.1,
        fail_when_locked=True,
        flags=LockFlags.SHARED | LockFlags.NON_BLOCKING,
    ):
        time.sleep(0.2)
        return True


def exclusive_lock(filename, **kwargs):
    """Helper for exclusive lock in multiprocessing tests."""
    with portalocker.Lock(
        filename,
        timeout=0.1,
        fail_when_locked=False,
        flags=LockFlags.EXCLUSIVE | LockFlags.NON_BLOCKING,
    ):
        time.sleep(0.2)
        return True


@_needs_win32_extra
@pytest.mark.parametrize('fail_when_locked', [True, False])
@pytest.mark.skipif(
    'pypy' in platform.python_implementation().lower(),
    reason='pypy3 does not support the multiprocessing test',
)
@pytest.mark.flaky(reruns=5, reruns_delay=1)
def test_shared_processes(tmpdir, fail_when_locked):
    """Test that shared locks work correctly across processes."""
    tmpfile = tmpdir.join('test_shared_processes.lock')
    flags = LockFlags.SHARED | LockFlags.NON_BLOCKING
    with multiprocessing.Pool(processes=2) as pool:
        args = tmpfile, fail_when_locked, flags
        results = pool.starmap_async(lock, 2 * [args])

        # Generous ceiling: spawning 2 interpreters can exceed a couple of
        # seconds on a loaded CI runner, which is unrelated to the lock logic.
        for result in results.get(timeout=30):
            if result.exception_class is not None:
                raise result.exception_class
            assert result == LockResult()


def hold_exclusive_lock(
    filename: str,
    flags: LockFlags,
    locked_event: multiprocessing.synchronize.Event,
    release_event: multiprocessing.synchronize.Event,
) -> None:
    """Acquire an exclusive lock, announce it, and hold until told to stop.

    Signalling ``locked_event`` only after the lock is held lets the parent
    start the competing acquire deterministically instead of racing on
    timing.
    """
    with portalocker.Lock(
        filename,
        timeout=30,
        fail_when_locked=False,
        flags=flags,
    ):
        locked_event.set()
        # Keep holding the lock until the parent has finished its (blocked)
        # attempt. The ceiling is a safety net, not a timing assumption.
        release_event.wait(timeout=30)


@pytest.mark.parametrize('fail_when_locked', [True, False])
@pytest.mark.skipif(
    'pypy' in platform.python_implementation().lower(),
    reason='pypy3 does not support the multiprocessing test',
)
@pytest.mark.flaky(reruns=5, reruns_delay=1)
def test_exclusive_processes(
    tmpdir: str,
    fail_when_locked: bool,
) -> None:
    """A second process must not be able to take an exclusive lock while
    another process holds it.

    Deterministic by construction: the holder signals ``locked`` only once it
    owns the lock, and keeps holding it until we set ``release``. So our own
    acquire attempt in between is guaranteed to be contended. Ceilings are
    generous (30s) because they only guard against a hung child, not the lock
    logic.
    """
    tmpfile = str(tmpdir.join('test_exclusive_processes.lock'))
    flags = LockFlags.EXCLUSIVE | LockFlags.NON_BLOCKING

    locked = multiprocessing.Event()
    release = multiprocessing.Event()
    holder = multiprocessing.Process(
        target=hold_exclusive_lock,
        args=(tmpfile, flags, locked, release),
    )
    holder.start()
    try:
        assert locked.wait(timeout=30), 'holder never acquired the lock'

        # The holder still owns the lock, so this attempt must be blocked and
        # surface a LockException (AlreadyLocked when fail_when_locked, a plain
        # timeout otherwise).
        with (
            pytest.raises(portalocker.LockException),
            portalocker.Lock(
                tmpfile,
                timeout=0.5,
                fail_when_locked=fail_when_locked,
                flags=flags,
            ),
        ):
            pass  # pragma: no cover - lock must never be granted here
    finally:
        release.set()
        holder.join(timeout=30)

    assert holder.exitcode == 0


@pytest.mark.parametrize('lock_kind', ['temporary', 'pid'])
@pytest.mark.skipif(
    os.name == 'nt',
    reason='POSIX-only inode replacement race',
)
@pytest.mark.skipif(
    'pypy' in platform.python_implementation().lower(),
    reason='pypy3 does not support the multiprocessing test',
)
def test_temporary_lock_waiters_converge_on_current_inode(
    tmp_path: pathlib.Path,
    lock_kind: LockKind,
) -> None:
    """#115: waiters must reject an obsolete unlinked lock inode."""
    context: multiprocessing.context.SpawnContext = (
        multiprocessing.get_context('spawn')
    )
    filename: str = str(tmp_path / f'{lock_kind}.lock')
    native_filename: str = native_lock_path(filename, lock_kind)
    holder: portalocker.TemporaryFileLock = make_temporary_lock(
        filename,
        lock_kind,
        timeout=30,
        fail_when_locked=False,
    )
    opened_event: multiprocessing.synchronize.Event = context.Event()
    acquired_event: multiprocessing.synchronize.Event = context.Event()
    release_event: multiprocessing.synchronize.Event = context.Event()
    inode_queue: multiprocessing.queues.Queue[int] = context.Queue()
    waiter: multiprocessing.process.BaseProcess = context.Process(
        target=hold_inode_waiter,
        args=(
            filename,
            lock_kind,
            native_filename,
            opened_event,
            acquired_event,
            release_event,
            inode_queue,
        ),
    )
    holder_released: bool = False

    holder.acquire()
    waiter.start()
    try:
        assert opened_event.wait(timeout=30), (
            'waiter never opened the holder inode'
        )
        holder.release()
        holder_released = True
        assert acquired_event.wait(timeout=30), 'waiter never acquired'

        waiter_inode: int = inode_queue.get(timeout=30)
        contender: portalocker.TemporaryFileLock = make_temporary_lock(
            filename,
            lock_kind,
            timeout=0,
            fail_when_locked=True,
        )
        try:
            with pytest.raises(portalocker.AlreadyLocked):
                contender.acquire()
        finally:
            contender.release()

        current_inode: int = os.stat(native_filename).st_ino
        assert waiter_inode == current_inode
    finally:
        if not holder_released:
            holder.release()
        release_event.set()
        waiter.join(timeout=30)
        if waiter.is_alive():
            waiter.terminate()
            waiter.join(timeout=30)
        inode_queue.close()
        inode_queue.join_thread()

    assert waiter.exitcode == 0
