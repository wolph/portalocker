from __future__ import print_function
from __future__ import with_statement

import pytest
import portalocker


def test_exceptions(tmpfile):
    # Open the file 2 times
    a = open(tmpfile, 'a')
    b = open(tmpfile, 'a')

    # Lock exclusive non-blocking
    lock_flags = portalocker.LOCK_EX | portalocker.LOCK_NB

    # First lock file a
    portalocker.lock(a, lock_flags)

    # Now see if we can lock file b
    with pytest.raises(portalocker.LockException):
        portalocker.lock(b, lock_flags)

    # Cleanup
    a.close()
    b.close()


def test_with_timeout(tmpfile):
    # Open the file 2 times
    with pytest.raises(portalocker.AlreadyLocked):
        with portalocker.Lock(tmpfile, timeout=0.1) as fh:
            print('writing some stuff to my cache...', file=fh)
            with portalocker.Lock(tmpfile, timeout=0.1, mode='wb'):
                pass
            print('writing more stuff to my cache...', file=fh)


def test_without_timeout(tmpfile):
    # Open the file 2 times
    with pytest.raises(portalocker.LockException):
        with portalocker.Lock(tmpfile, timeout=None) as fh:
            print('writing some stuff to my cache...', file=fh)
            with portalocker.Lock(tmpfile, timeout=None, mode='w'):
                pass
            print('writing more stuff to my cache...', file=fh)


def test_without_fail(tmpfile):
    # Open the file 2 times
    with pytest.raises(portalocker.LockException):
        with portalocker.Lock(tmpfile, timeout=0.1) as fh:
            print('writing some stuff to my cache...', file=fh)
            lock = portalocker.Lock(tmpfile, timeout=0.1)
            lock.acquire(check_interval=0.05, fail_when_locked=False)


def test_simple(tmpfile):
    with open(tmpfile, 'w') as fh:
        fh.write('spam and eggs')

    fh = open(tmpfile, 'r+')
    portalocker.lock(fh, portalocker.LOCK_EX)

    fh.seek(13)
    fh.write('foo')

    # Make sure we didn't overwrite the original text
    fh.seek(0)
    assert fh.read(13) == 'spam and eggs'

    portalocker.unlock(fh)
    fh.close()


def test_truncate(tmpfile):
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
    lock = portalocker.Lock(tmpfile)
    lock2 = portalocker.Lock(tmpfile, fail_when_locked=False, timeout=0.01)

    with lock:
        lock.acquire()

        with pytest.raises(portalocker.LockException):
            with lock2:
                pass

    with lock2:
        pass


def test_acquire_release(tmpfile):
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

