4.1.1:

 * Fixed ``RedisLock`` stale-holder cleanup killing healthy holders of
   other channels. The cleanup prefix-matched ``CLIENT LIST`` names, so a
   probe on channel ``a`` matched the holders of a channel named
   ``a-lock-b`` (whose connections are named ``a-lock-b-lock-<id>``) and
   killed them, along with any unrelated client whose name happened to
   start with ``a-lock-``. Client names are now matched exactly against
   the ``<channel>-lock-<32 character hex holder id>`` shape, and the
   bare legacy ``<channel>-lock`` name is still reaped as before (#142)
 * Fixed ``RedisLock`` sleeping out a jittered ``check_interval`` before its
   first acquisition attempt, which cost an uncontended acquire roughly
   250ms for nothing. The retry generator now yields immediately and only
   sleeps between attempts. The probe poll loops stay paced because their
   ``get_message(timeout=...)`` calls block on their own (#144)
 * Behaviour change: acquiring a ``RedisLock`` instance that is already
   holding a lock now raises ``portalocker.LockException`` instead of
   ``AssertionError``. The assert was the only re-acquire guard and
   ``python -O`` strips asserts, which silently orphaned the worker thread
   and left a phantom holder on the channel (#144)
 * Documented a known mixed-version limitation of ``RedisLock``: holders on
   portalocker 3.2.0 and older share one connection name, so one live plus
   one crashed legacy holder on a channel cannot be told apart and block
   waiters until the crashed holder's TCP connection dies on its own (#144)
 * Made ``RedisLock`` teardown exception safe. ``release`` now runs every
   teardown step even when an earlier one fails, clears ``thread``,
   ``pubsub`` and a self-created connection regardless, and re-raises only
   the first error, so a failing ``UNSUBSCRIBE`` (Redis unreachable while
   the lock was held) no longer leaves a stale ``pubsub`` behind that made
   every later ``acquire`` on the instance fail its already-active guard
   (#140)
 * Fixed the ``RedisLock`` rollback for a worker thread that fails to
   start: the never-started thread is no longer joined, so the original
   error propagates out of ``acquire`` instead of ``RuntimeError: cannot
   join thread before it is started``, and the subscribed pubsub no longer
   leaks. A rollback that fails as well is logged instead of replacing the
   original error (#140)
 * ``RedisLock.release`` no longer checks a fresh connection out of the
   pool purely to send ``UNSUBSCRIBE`` for a subscription the worker
   thread already discarded when it stopped. The unsubscribe now only runs
   when the pubsub still owns a connection, which is also what made
   ``RedisLock.__del__`` fail loudly at interpreter shutdown (#140)
 * ``RedisLock.__del__`` is now best effort like ``LockBase.__del__`` and
   suppresses all errors instead of surfacing them as interpreter-level
   "Exception ignored in" messages during garbage collection or shutdown
   (#140)
 * Fixed a contended ``RedisLock`` with a self-created connection (no
   ``connection=`` argument) killing its own worker thread and delivering a
   ``KeyboardInterrupt`` to the main thread. The release between retries
   closed and cleared the connection while ``acquire`` kept resubscribing on
   a stale reference, so ``channel_handler`` failed its connection assert
   and the failure was escalated to the main thread. Retries now drop only
   the subscription and keep the connection; the connection is closed on
   final release or when ``acquire`` gives up (#136)
 * Fixed a ``RedisLock`` probe reading at most one reply per polling
   interval, which capped a probe at roughly ten replies inside the default
   one second ``unavailable_timeout`` no matter how fast the holders
   answered. On a channel with more holders than that every probe came up
   short and killed healthy holders whose replies were sitting unread in
   the prober's own buffer. The reply loop now drains all buffered replies
   within each interval, so the interval paces the polling instead of
   capping the throughput (#138)
 * Closed three ``RedisLock.acquire`` races that could let two writers both
   conclude they held the lock exclusively. Subscribing now waits for the
   server's subscribe confirmation instead of sleeping 10ms, so a subscriber
   count can no longer run before the server registered the subscription.
   Mode promotions and ping answers now serialize on a ``threading.Lock``,
   so a probe can no longer read a torn or stale ``pending`` from a writer
   mid-promotion. A probe now re-checks the subscriber count immediately
   before pinging as well as after collecting replies. Count-preserving
   churn between those two checks remains undetectable because ``PUBSUB
   NUMSUB`` reports counts rather than identities, and is tolerated because
   losing waiters retry. Also documented that ``RedisLock`` requires a
   single standalone Redis endpoint, since ``PUBSUB NUMSUB`` is node-local
   in cluster and replica setups (#139)
 * Fixed ``Lock.acquire`` leaking an open, still locked filehandle when
   preparing the file failed after the lock was taken, for example mode
   ``w`` on a macOS append-only (``chflags uappnd``) file, where the
   deferred truncate raises ``EPERM``. ``self.fh`` was never assigned, so
   ``release`` was a no-op while the escaping traceback pinned the handle
   alive and the file stayed locked indefinitely. The handle is now
   unlocked and closed before the original error escapes
 * Behaviour change: ``Lock.acquire`` now retries only contention, which
   the locking backend reports as ``AlreadyLocked``. A plain
   ``LockException``, such as ``flock`` refusing a FIFO or an NFS/SMB
   mount without locking support, ``ENOLCK``, or the ``EOFError`` some
   NFS setups raise from ``fcntl``, is permanent: it is now raised
   immediately instead of being retried for the whole timeout, because
   retrying cannot make a filesystem grow locking support. With
   ``fail_when_locked=True`` it is no longer wrapped in
   ``AlreadyLocked``, which claimed somebody held a lock on a filesystem
   that cannot lock at all. The same classification reaches
   ``BoundedSemaphore``: a plain ``LockException`` while probing a slot
   used to be mistaken for a taken slot and skipped, it now propagates
   and aborts the acquire. Custom lockers must raise ``AlreadyLocked``
   for contention to keep being retried, as the bundled lockers already
   do
 * Fixed ``LockBase._timeout_generator`` sleeping past its deadline by up
   to one full ``check_interval``: a contended ``Lock(timeout=0.5,
   check_interval=3)`` gave up after roughly 3 seconds instead of 0.5.
   Every sleep is now capped at the time remaining until the deadline.
   ``RedisLock`` is unaffected, it overrides the generator with its own
 * Fixed the "timeout has no effect in blocking mode" warning firing
   twice for a ``Lock`` built with an explicit timeout (at construction
   and again on every ``acquire``), pointing at portalocker's own source
   instead of the caller, and firing for ``RLock``,
   ``TemporaryFileLock`` and ``PidFileLock`` instances constructed
   without any timeout argument, which failed user suites running with
   ``filterwarnings = error``. The subclasses now forward ``None`` so the
   default timeout no longer counts as caller-provided, and the warning
   fires at most once per lock instance, with a stacklevel computed by
   walking past portalocker's internal frames so it names the caller's
   file from every entry point, subclass constructors included
 * Behaviour change: ``RLock.acquire`` on an instance whose acquire count
   claims the lock is held while no filehandle exists now raises
   ``portalocker.LockException`` instead of relying on a bare ``assert``,
   which ``python -O`` strips, silently handing the caller ``None`` as
   the filehandle
 * Fixed positioned writes for ``Lock`` modes containing ``w`` on POSIX.
   The deferred-truncation ``a`` substitution left ``O_APPEND`` set, so
   the kernel ignored seek positions and ``fh.write('x'); fh.seek(0);
   fh.write('y')`` produced ``'xy'`` where the builtin ``open(mode='w')``
   produces ``'y'``. The append flag is now cleared once the truncation
   is done. Windows offers no way to drop the flag from an open handle,
   so there the append semantics remain and are documented

4.1.0:

 * Documentation release. No runtime behaviour changed; the only edits to
   executable code replaced two ``...`` bodies on the abstract
   ``LockBase.acquire``/``LockBase.release`` stubs with docstrings.
 * Every module, class, function, private helper and dunder method in the
   package now carries a Google-style docstring. ``ruff``'s pydocstyle rules
   are enabled for ``portalocker/`` with no exemptions
 * Added seven narrative guides: quickstart, lock types, platform behaviour,
   Redis locks, the ``combine`` CLI, troubleshooting, and a 3.x to 4.0.0
   migration guide. The API reference moved under ``docs/api/`` and a
   changelog page was added
 * ``docs/platforms.rst`` documents the advisory-versus-mandatory
   distinction, ``flock`` versus ``lockf``, msvcrt versus pywin32, and the
   networked-filesystem caveats that cause most locking confusion
 * ``docs/cli.rst`` documents the combiner's ASCII-only requirement on
   ``portalocker/*.py``, ``README.rst`` and ``LICENSE``, which was previously
   only discoverable by reading ``__main__.py``
 * Doctests now run for ``docs/*.rst`` and ``README.rst`` as well as the
   package, so every documented example is executed on each supported
   platform and interpreter. No ``# doctest: +SKIP`` remains anywhere
 * Fixed the README telling readers to unlock a filehandle it had already
   closed, which raised ``ValueError: I/O operation on closed file``
 * Fixed the README demonstrating ``BoundedSemaphore`` without a ``name``,
   which the library itself deprecates in favour of ``NamedBoundedSemaphore``
 * Removed the obsolete Python 2 installation section from the README

4.0.0:

 * Fixed ``open_atomic()`` replacing a destination created while its context
   was open on POSIX; publication now raises ``FileExistsError`` and preserves
   the concurrent winner (#114)
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
 * Added ``PidFileLock.fail_closed()`` for ownership-only contexts; contention
   raises ``AlreadyLocked`` before entering the body and exposes the competing
   PID through ``AlreadyLocked.holder_pid`` when readable (#118)
 * Packaging switched to the ``uv_build`` backend; releases are published to
   PyPI through GitHub Actions Trusted Publishing
 * ``python -m portalocker combine``: ``--output-file`` now opens lazily;
   the vendored single-file output correctly includes ``RedisLock`` (it was
   always ``None`` before); the smoke-run now uses ``sys.executable``
   (fixes Windows)
 * Fixed ``PidFileLock`` ignoring ``timeout`` when ``fail_when_locked=False``;
   all PID-publication failures now transactionally release the sidecar and
   preserve the publication error if cleanup also fails (#116)
 * Fixed a ``TemporaryFileLock``/``PidFileLock`` unlock-then-unlink race that
   could let two processes hold the same lock (split-brain); release now
   unlinks before unlocking on POSIX and acquire re-verifies file identity
   (inode) after locking (#115)
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
 * ``Lock.release()`` continues suppressing unlock and close errors by default;
   closing is always attempted and the file handle reference is cleared.
   Callers can opt into reporting cleanup failures with
   ``Lock(..., raise_on_release_error=True)`` (#117)
 * ``TemporaryFileLock.release()`` and ``PidFileLock.release()`` are now
   no-ops when the object does not hold the lock, so a stale object (double
   release, or garbage collection of a failed acquire) can no longer unlink
   the lock file out from under the current holder
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
