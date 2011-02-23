from nose.tools import assert_raises, raises
import portalocker

def test_exceptions():
    # Open the file 2 times
    a = file('locked_file', 'a')
    b = file('locked_file', 'a')

    # Lock exclusive non-blocking
    lock_flags = portalocker.LOCK_EX | portalocker.LOCK_NB

    # First lock file a
    portalocker.lock(a, lock_flags)

    # Now see if we can lock file b
    assert_raises(portalocker.LockException, portalocker.lock, b, lock_flags)

    # Cleanup
    a.close()
    b.close()

@raises(portalocker.LockException)
def test_with_timeout():
    # Open the file 2 times
    with portalocker.Lock('locked_file', timeout=0.1) as lock:
        with portalocker.Lock('locked_file', timeout=0.1) as lock2:
            pass

@raises(portalocker.LockException)
def test_without_timeout():
    # Open the file 2 times
    with portalocker.Lock('locked_file', timeout=None) as lock:
        with portalocker.Lock('locked_file', timeout=None) as lock2:
            pass

