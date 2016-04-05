from __future__ import print_function
from __future__ import with_statement
import pytest
import portalocker


def test_exceptions():
    # Open the file 2 times
    a = open('locked_file', 'a')
    b = open('locked_file', 'a')

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


def test_with_timeout():
    # Open the file 2 times
    with pytest.raises(portalocker.AlreadyLocked):
        with portalocker.Lock('locked_file', timeout=0.1) as fh:
            print('writing some stuff to my cache...', file=fh)
            with portalocker.Lock('locked_file', timeout=0.1):
                pass
            print('writing more stuff to my cache...', file=fh)


def test_without_timeout():
    # Open the file 2 times
    with pytest.raises(portalocker.LockException):
        with portalocker.Lock('locked_file', timeout=None) as fh:
            print('writing some stuff to my cache...', file=fh)
            with portalocker.Lock('locked_file', timeout=None):
                pass
            print('writing more stuff to my cache...', file=fh)


def test_simple():
    fh = open('tests/test_file.txt', 'r+')
    portalocker.lock(fh, portalocker.LOCK_EX)
    fh.seek(12)
    fh.write('foo')
    portalocker.unlock(fh)
    fh.close()


def test_class():
    lock = portalocker.Lock('tests/test_file.txt')
    lock2 = portalocker.Lock('tests/test_file.txt', fail_when_locked=False,
                             timeout=0.01)

    with lock:
        lock.acquire()

        with pytest.raises(portalocker.LockException):
            with lock2:
                pass

    with lock2:
        pass


def test_acquire_release():
    lock = portalocker.Lock('tests/test_file.txt')
    lock2 = portalocker.Lock('tests/test_file.txt', fail_when_locked=False)

    lock.acquire()  # acquire lock when nobody is using it
    with pytest.raises(portalocker.LockException):
        # another party should not be able to acquire the lock
        lock2.acquire(timeout=0.01)

        # re-acquire a held lock is a no-op
        lock.acquire()

    lock.release()  # release the lock
    lock.release()  # second release does nothing

