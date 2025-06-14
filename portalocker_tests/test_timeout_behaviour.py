import pytest

import portalocker


def test_with_timeout(tmpfile):
    """
    Test that AlreadyLocked is raised when a file is locked with a timeout.
    """
    # Open the file 2 times
    with pytest.raises(portalocker.AlreadyLocked):  # noqa: SIM117
        with portalocker.Lock(tmpfile, timeout=0.1) as fh:
            print('writing some stuff to my cache...', file=fh)
            with portalocker.Lock(
                tmpfile,
                timeout=0.1,
                mode='wb',
                fail_when_locked=True,
            ):
                pass
            print('writing more stuff to my cache...', file=fh)


def test_without_timeout(tmpfile):
    """
    Test that LockException is raised when a file is locked without a
    timeout."""
    # Open the file 2 times
    with pytest.raises(portalocker.LockException):  # noqa: SIM117
        with portalocker.Lock(tmpfile, timeout=None) as fh:
            print('writing some stuff to my cache...', file=fh)
            with portalocker.Lock(tmpfile, timeout=None, mode='w'):
                pass
            print('writing more stuff to my cache...', file=fh)


def test_without_fail(tmpfile):
    """Test that LockException is raised when fail_when_locked is False."""
    # Open the file 2 times
    with pytest.raises(portalocker.LockException):  # noqa: SIM117
        with portalocker.Lock(tmpfile, timeout=0.1) as fh:
            print('writing some stuff to my cache...', file=fh)
            lock = portalocker.Lock(tmpfile, timeout=0.1)
            lock.acquire(check_interval=0.05, fail_when_locked=False)
