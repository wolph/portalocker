import importlib.util
import os

import pytest

import portalocker
from portalocker import LockFlags
from portalocker_tests.conftest import LOCKERS

# Since portalocker 4.0.0 pywin32 is an optional extra. On Windows without it,
# `win32file` is unavailable and shared locks are unsupported by design
# (msvcrt has no true shared lock), so they raise ImportError pointing at the
# `portalocker[win32]` extra. Skip the shared-lock cases in that configuration.
_needs_win32_extra = pytest.mark.skipif(
    os.name == 'nt' and importlib.util.find_spec('win32file') is None,
    reason='shared locks on Windows require the pywin32 extra '
    '(portalocker[win32])',
)


def test_exclusive(tmpdir):
    """Test that exclusive lock prevents reading and writing by others."""
    tmpfile = tmpdir.join('test_exclusive.lock')
    text_0 = 'spam and eggs'
    with open(tmpfile, 'w') as fh:
        fh.write(text_0)

    with open(tmpfile) as fh:
        portalocker.lock(fh, portalocker.LOCK_EX | portalocker.LOCK_NB)

        # Make sure we can't read the locked file
        with (
            pytest.raises(portalocker.LockException),
            open(
                tmpfile,
                'r+',
            ) as fh2,
        ):
            portalocker.lock(fh2, portalocker.LOCK_EX | portalocker.LOCK_NB)
            assert fh2.read() == text_0

        # Make sure we can't write the locked file
        with (
            pytest.raises(portalocker.LockException),
            open(
                tmpfile,
                'w+',
            ) as fh2,
        ):
            portalocker.lock(fh2, portalocker.LOCK_EX | portalocker.LOCK_NB)
            fh2.write('surprise and fear')

        # Make sure we can explicitly unlock the file
        portalocker.unlock(fh)


@_needs_win32_extra
def test_shared(tmpdir):
    """Test that shared lock allows reading but not writing by others."""
    tmpfile = tmpdir.join('test_shared.lock')
    with open(tmpfile, 'w') as fh:
        fh.write('spam and eggs')

    with open(tmpfile) as f:
        portalocker.lock(f, portalocker.LOCK_SH | portalocker.LOCK_NB)

        # Make sure we can read the locked file
        with open(tmpfile) as fh2:
            portalocker.lock(fh2, portalocker.LOCK_SH | portalocker.LOCK_NB)
            assert fh2.read() == 'spam and eggs'

        # Make sure we can't write the locked file
        with (
            pytest.raises(portalocker.LockException),
            open(
                tmpfile,
                'w+',
            ) as fh2,
        ):
            portalocker.lock(fh2, portalocker.LOCK_EX | portalocker.LOCK_NB)
            fh2.write('surprise and fear')

        # Make sure we can explicitly unlock the file
        portalocker.unlock(f)


@pytest.mark.parametrize('locker', LOCKERS, indirect=True)
def test_blocking_timeout(tmpdir, locker):
    """Test that a warning is raised when using a blocking timeout."""
    tmpfile = tmpdir.join('test_blocking_timeout.lock')
    flags = LockFlags.SHARED

    with pytest.warns(UserWarning):  # noqa: SIM117
        with portalocker.Lock(tmpfile, 'a+', timeout=5, flags=flags):
            pass

    lock = portalocker.Lock(tmpfile, 'a+', flags=flags)
    with pytest.warns(UserWarning):
        lock.acquire(timeout=5)


@pytest.mark.parametrize('locker', LOCKERS, indirect=True)
def test_nonblocking(tmpdir, locker):
    """Test that using NON_BLOCKING flag raises RuntimeError."""
    tmpfile = tmpdir.join('test_nonblocking.lock')
    with open(tmpfile, 'w') as fh, pytest.raises(RuntimeError):
        portalocker.lock(fh, LockFlags.NON_BLOCKING)


# ``LockFlags.UNBLOCK`` is ``msvcrt.LK_UNLCK`` (0) on Windows, so an
# UNBLOCK-bearing combination is indistinguishable from the same flags
# without it there. The UNBLOCK-specific rejections only exist on POSIX.
_unblock_is_distinct = pytest.mark.skipif(
    os.name == 'nt',
    reason='LockFlags.UNBLOCK is 0 on Windows, the combination is '
    'indistinguishable from the plain lock flags',
)


@_unblock_is_distinct
@pytest.mark.parametrize(
    'flags',
    [
        LockFlags.UNBLOCK,
        LockFlags.EXCLUSIVE | LockFlags.UNBLOCK,
        LockFlags.SHARED | LockFlags.UNBLOCK,
        LockFlags.EXCLUSIVE | LockFlags.NON_BLOCKING | LockFlags.UNBLOCK,
    ],
)
def test_lock_rejects_unblock_flag(tmpdir, flags):
    """``lock()`` must reject UNBLOCK-bearing flags, releasing is unlock()'s
    job.
    """
    tmpfile = tmpdir.join('test_lock_rejects_unblock.lock')
    with (
        open(tmpfile, 'w') as fh,
        pytest.raises(RuntimeError, match='unlock'),
    ):
        portalocker.lock(fh, flags)


@_unblock_is_distinct
def test_lock_with_unblock_does_not_release(tmpdir):
    """A held lock must survive a rejected ``lock(fh, UNBLOCK)`` call.

    Before 4.2.0 that call silently *released* the lock on POSIX, because
    the UNBLOCK bit went straight through to ``fcntl.flock``.
    """
    tmpfile = tmpdir.join('test_unblock_no_release.lock')
    with open(tmpfile, 'w') as fh:
        portalocker.lock(fh, LockFlags.EXCLUSIVE | LockFlags.NON_BLOCKING)

        with pytest.raises(RuntimeError):
            portalocker.lock(fh, LockFlags.UNBLOCK)

        # The lock must still be held: a second handle still contends.
        with (
            open(tmpfile, 'w') as fh2,
            pytest.raises(portalocker.AlreadyLocked),
        ):
            portalocker.lock(fh2, LockFlags.EXCLUSIVE | LockFlags.NON_BLOCKING)

        portalocker.unlock(fh)


def test_lock_rejects_shared_and_exclusive(tmpdir):
    """``lock()`` must reject SHARED|EXCLUSIVE, they contradict each other."""
    tmpfile = tmpdir.join('test_shared_exclusive.lock')
    with (
        open(tmpfile, 'w') as fh,
        pytest.raises(
            RuntimeError, match=r'SHARED.*EXCLUSIVE|EXCLUSIVE.*SHARED'
        ),
    ):
        portalocker.lock(fh, LockFlags.SHARED | LockFlags.EXCLUSIVE)


def test_lock_rejects_zero_flags(tmpdir):
    """``lock()`` with no flags at all must fail with a clear message."""
    tmpfile = tmpdir.join('test_zero_flags.lock')
    with (
        open(tmpfile, 'w') as fh,
        pytest.raises(RuntimeError, match=r'SHARED|EXCLUSIVE'),
    ):
        portalocker.lock(fh, LockFlags(0))
