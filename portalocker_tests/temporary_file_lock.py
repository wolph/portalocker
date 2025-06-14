import os

import portalocker


def test_temporary_file_lock(tmpfile):
    """The lock file must be deleted on context exit and GC must close
    the lock gracefully."""
    with portalocker.TemporaryFileLock(tmpfile):
        pass

    assert not os.path.isfile(tmpfile)

    lock = portalocker.TemporaryFileLock(tmpfile)
    lock.acquire()
    del lock
