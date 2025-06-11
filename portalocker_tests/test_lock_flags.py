import os

import pytest

import portalocker
from portalocker import LockFlags
from portalocker_tests.conftest import LOCKERS


def test_exclusive(tmpfile):
    """Test that exclusive lock prevents reading and writing by others."""
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


def test_shared(tmpfile):
    """Test that shared lock allows reading but not writing by others."""
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
def test_blocking_timeout(tmpfile, locker):
    """Test that a warning is raised when using a blocking timeout."""
    flags = LockFlags.SHARED

    with pytest.warns(UserWarning):  # noqa: SIM117
        with portalocker.Lock(tmpfile, 'a+', timeout=5, flags=flags):
            pass

    lock = portalocker.Lock(tmpfile, 'a+', flags=flags)
    with pytest.warns(UserWarning):
        lock.acquire(timeout=5)


@pytest.mark.skipif(
    os.name == 'nt',
    reason='Windows uses an entirely different lockmechanism, which does not '
    'support NON_BLOCKING flag within a single process.',
)
@pytest.mark.parametrize('locker', LOCKERS, indirect=True)
def test_nonblocking(tmpfile, locker):
    """Test that using NON_BLOCKING flag raises RuntimeError."""
    with open(tmpfile, 'w') as fh, pytest.raises(RuntimeError):
        portalocker.lock(fh, LockFlags.NON_BLOCKING)
