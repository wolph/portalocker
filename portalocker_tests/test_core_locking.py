import pytest

import portalocker
from portalocker import exceptions, utils


def test_utils_base():
    """Test that LockBase can be subclassed."""

    class Test(utils.LockBase):
        pass


def test_exceptions(tmpfile):
    """Test that locking a file twice raises LockException."""
    with open(tmpfile, 'a') as a, open(tmpfile, 'a') as b:
        # Lock exclusive non-blocking
        lock_flags = portalocker.LOCK_EX | portalocker.LOCK_NB

        # First lock file a
        portalocker.lock(a, lock_flags)

        # Now see if we can lock file b
        with pytest.raises(portalocker.LockException):
            portalocker.lock(b, lock_flags)


def test_simple(tmpfile):
    """Test that locking and writing to a file works as expected."""
    with open(tmpfile, 'w') as fh:
        fh.write('spam and eggs')

    with open(tmpfile, 'r+') as fh:
        portalocker.lock(fh, portalocker.LOCK_EX)

        fh.seek(13)
        fh.write('foo')

        # Make sure we didn't overwrite the original text
        fh.seek(0)
        assert fh.read(13) == 'spam and eggs'

        portalocker.unlock(fh)


def test_truncate(tmpfile):
    """Test that truncating a file works as expected."""
    with open(tmpfile, 'w') as fh:
        fh.write('spam and eggs')

    with portalocker.Lock(tmpfile, mode='a+') as fh:
        # Make sure we didn't overwrite the original text
        fh.seek(0)
        assert fh.read(13) == 'spam and eggs'

    with portalocker.Lock(tmpfile, mode='w+') as fh:
        # Make sure we truncated the file
        assert fh.read() == ''


def test_class(tmpfile):
    """Test that Lock context manager works as expected."""
    lock = portalocker.Lock(tmpfile)
    lock2 = portalocker.Lock(tmpfile, fail_when_locked=False, timeout=0.01)

    with lock:
        lock.acquire()

        with pytest.raises(portalocker.LockException), lock2:
            pass

    with lock2:
        pass


def test_acquire_release(tmpfile):
    """Test that acquire and release work as expected."""
    lock = portalocker.Lock(tmpfile)
    lock2 = portalocker.Lock(tmpfile, fail_when_locked=False)

    lock.acquire()  # acquire lock when nobody is using it
    with pytest.raises(portalocker.LockException):
        # another party should not be able to acquire the lock
        lock2.acquire(timeout=0.01)

        # re-acquire a held lock is a no-op
        lock.acquire()

    lock.release()  # release the lock
    lock.release()  # second release does nothing


def test_release_unacquired(tmpfile):
    """Test that releasing an unacquired RLock raises LockException."""
    with pytest.raises(portalocker.LockException):
        portalocker.RLock(tmpfile).release()


def test_exception(monkeypatch, tmpfile):
    """Do we stop immediately if the locking fails, even with a timeout?"""

    def patched_lock(*args, **kwargs):
        raise ValueError('Test exception')

    monkeypatch.setattr('portalocker.utils.portalocker.lock', patched_lock)
    lock = portalocker.Lock(tmpfile, 'w', timeout=float('inf'))

    with pytest.raises(exceptions.LockException):
        lock.acquire()
