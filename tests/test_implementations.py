from nose.tools import assert_raises
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

