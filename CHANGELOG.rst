4.0.0:

 * Python 3.10 or later is now required; Python 3.9 (EOL) support dropped
 * ``pywin32`` is no longer installed by default on Windows; the msvcrt-based
   locker is the default and works dependency-free for exclusive locks.
   Shared locks on Windows require ``portalocker[win32]``, and an informative
   ``ImportError`` is raised otherwise (#104)
 * POSIX lock exceptions now populate ``.strerror`` and pass the message as
   a second positional argument, matching the Windows exception contract.
   This changes the ``str()`` output of these exceptions on POSIX from the
   bare ``OSError`` text to a 2-tuple repr
 * ``LockBase`` is now generic over the acquire return type (typing-only
   change; downstream ``Lock`` subclasses are unaffected)
 * Added ``PidFileLock`` for pidfile-based locking (#106)
 * Packaging switched to the ``uv_build`` backend; releases are published to
   PyPI through GitHub Actions Trusted Publishing
 * ``python -m portalocker combine``: ``--output-file`` now opens lazily;
   the vendored single-file output correctly includes ``RedisLock`` (it was
   always ``None`` before); the smoke-run now uses ``sys.executable``
   (fixes Windows)
 * Fixed ``PidFileLock`` ignoring ``timeout`` when ``fail_when_locked=False``;
   its sidecar lock is now released if the PID write fails
 * Fixed a ``TemporaryFileLock``/``PidFileLock`` unlock-then-unlink race that
   could let two processes hold the same lock (split-brain); release now
   unlinks before unlocking on POSIX and acquire re-verifies file identity
   (inode) after locking
 * Fixed ``BoundedSemaphore`` staying permanently "Already locked" after a
   non-contention error (e.g. a missing directory)
 * Fixed ``RedisLock`` crashed-holder detection: the liveness check was
   always satisfied by its own subscribe confirmation, so dead holders were
   never reaped; the ping is now published only after the subscription is
   confirmed active, pubsub connections no longer leak on the reap path,
   ``fail_when_locked=True`` fails fast instead of polling the full timeout,
   internally-created connections are closed on release, and a failed
   ``acquire()`` rolls back cleanly so the lock can be retried
 * Added shared ``RedisLock`` readers through ``LockFlags.SHARED``. Waiting
   writers gate new readers to prevent starvation, holder-specific heartbeats
   preserve stale-client cleanup, and legacy Redis lock responses are treated
   as exclusive for mixed-version safety (#124)
 * Fixed ``FlockLocker``/``LockfLocker`` on POSIX to use their named syscall
   (previously silently used the global ``LOCKER``); the module-level
   ``lock()``/``unlock()`` on POSIX now accept all documented ``LOCKER``
   forms (tuple/instance/class)
 * Fixed the Windows msvcrt locker locking from the current file position
   instead of byte 0 for raw file descriptors, which could break mutual
   exclusion on files larger than 64KiB; unexpected Win32 errors now raise
   ``LockException`` per the documented contract
 * ``RedisLock`` tests now run everywhere via ``fakeredis``, with a live
   Redis server still tested in CI
 * ``Lock.release()`` no longer propagates unlock errors; the file handle is
   always closed and cleared so a release can never leave a dangling lock
 * ``TemporaryFileLock.release()`` and ``PidFileLock.release()`` are now
   no-ops when the object does not hold the lock, so a stale object (double
   release, or garbage collection of a failed acquire) can no longer unlink
   the lock file out from under the current holder (#115)
 * ``TemporaryFileLock`` no longer keeps a strong ``atexit`` reference to
   itself, so unused instances can be garbage collected; cleanup at
   interpreter exit still happens via a weak reference
 * ``NamedBoundedSemaphore`` is now exported from the top-level
   ``portalocker`` namespace

For newer changes please look at the comments for the Git tags:
https://github.com/WoLpH/portalocker/tags

For more details the commit log for the master branch could be useful:
https://github.com/WoLpH/portalocker/commits/master

1.5:

 * Moved tests to prevent collisions with other packages

1.4:

 * Added optional file open parameters

1.3:

 * Improved documentation
 * Added file handle to locking exceptions

1.2:

 * Added signed releases and tags to PyPI and Git


1.1:

 * Added support for Python 3.6+
 * Using real time to calculate timeout

1.0:

 * Complete code refactor.
   
   - Splitting of code in logical classes
   - 100% test coverage and change in API behaviour
   - The default behavior of the `Lock` class has changed to append instead of
     write/truncate.

0.6:

 * Added msvcrt support for Windows

0.5:

 * Python 3 support

0.4:

 * Fixing a few bugs, added coveralls support, switched to py.test and added
   100% test coverage.

    - Fixing exception thrown when fail_when_locked is true
    - Fixing exception "Lock object has no attribute '_release_lock'" when
      fail_when_locked is true due to the call to Lock._release_lock() which
      fails because _release_lock is not defined.

0.3:

 * Now actually returning the file descriptor from the `Lock` class

0.2:

 * Added `Lock` class to help prevent cache race conditions

0.1:

 * Initial release
