import dataclasses
import multiprocessing
import multiprocessing.synchronize
import platform
import time

import pytest

import portalocker
from portalocker import LockFlags


@dataclasses.dataclass(order=True)
class LockResult:
    """Helper dataclass for multiprocessing lock results."""

    exception_class: type[BaseException] | None = None
    exception_message: str | None = None
    exception_repr: str | None = None


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
