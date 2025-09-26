import pytest

import portalocker


def test_rlock_acquire_release_count(tmpdir):
    """Test that RLock acquire/release count works as expected."""
    tmpfile = tmpdir.join('test_rlock_acquire_release_count.lock')
    lock = portalocker.RLock(tmpfile)
    # Twice acquire
    h = lock.acquire()
    assert not h.closed
    lock.acquire()
    assert not h.closed

    # Two release
    lock.release()
    assert not h.closed
    lock.release()
    assert h.closed


def test_rlock_acquire_release(tmpdir):
    """Test that RLock acquire/release works as expected."""
    tmpfile = tmpdir.join('test_rlock_acquire_release.lock')
    lock = portalocker.RLock(tmpfile)
    lock2 = portalocker.RLock(tmpfile, fail_when_locked=False)

    lock.acquire()  # acquire lock when nobody is using it
    with pytest.raises(portalocker.LockException):
        # another party should not be able to acquire the lock
        lock2.acquire(timeout=0.01)

    # Now acquire again
    lock.acquire()

    lock.release()  # release the lock
    lock.release()  # second release does nothing
