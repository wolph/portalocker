4.2.0:

 * Fixed ``RedisLock`` non-blocking acquisition raising ``AlreadyLocked``
   for the writer that had just won the election. The fail check ran
   before the promotion check, so two ``fail_when_locked`` writers on a
   free channel could both fail. The winner now takes the lock when no
   shared holder remains, so exactly one of two non-blocking contenders
   succeeds. One pre-existing reply-staleness window around the
   uncontended fast path is disclosed rather than closed and now also
   reaches non-blocking winners. It has only been reproduced with
   injected scheduling, and a confirm-probe fix is left for a separate
   issue (#143)
 * Fixed an elected ``RedisLock`` writer being usurped by a later writer
   with a lower holder id. Holder records now carry an ``elected`` field
   and pending writers defer to an advertised incumbent instead of
   rerunning the election against it. Because a ping reply is a
   snapshot that can predate the election it should have reported, the
   incumbent also holds its promotion while a lower-id newcomer that
   has not advertised seeing the election is visible, rather than
   promoting past it into a possible second exclusive holder. Records
   keep protocol version 1, 4.0 and 4.1 holders ignore the field and
   keep the old election on mixed channels, so no coordinated upgrade
   is needed. Protection is complete once every writer on a channel
   runs 4.2 or later, at the cost of one extra probe round whenever a
   newcomer's reply raced the incumbent's election (#143)
 * ``RedisLock`` with ``fail_when_locked`` now raises only on a
   conclusive probe showing the channel is held. Inconclusive probes
   retry within ``timeout``, which also lets a non-blocking acquire
   succeed after reaping a crashed holder instead of failing
   spuriously, at the cost of non-blocking latency of up to ``timeout``
   on a noisy channel. Pass ``timeout=0`` to keep the strict
   single-attempt behaviour (#143)

4.1.1:

 * Made the 100% coverage gate measure what it claims: the entire
   Windows locking implementation, the ``LockBase`` base class (the
   timeout generator, the context manager protocol and the retry
   plumbing), the version discovery fallbacks, the redis-less
   ``RedisLock`` stub and the pubsub failure escalation were all
   excluded wholesale via blanket ``pragma: no cover`` comments, so the
   gate passed without those regions ever being measured. Platform
   splits now use the per-OS conditional coverage rules (measured on
   the platform they run on, excluded only where they cannot run), the
   policy-wide exemptions for guard raises (``raise AssertionError``,
   ``raise NotImplementedError``, ``except ImportError:`` and friends)
   are gone from the coverage configuration, and the newly measured
   code is exercised by tests on every platform. The fork-safety hook
   registration was also tagged for the wrong platform: it was excluded
   on POSIX, where it runs, and measured on Windows, where it cannot
   run. Two unreachable defensive ``import msvcrt`` guards inside the
   ``os.name == 'nt'`` branch were removed outright: ``msvcrt`` ships
   with every Windows Python build
 * Fixed two concurrent ``release()`` calls on one ``Lock`` unlocking a
   stranger's lock: both callers passed the held-handle guard, and the
   loser then ran the OS unlock on a closed and possibly reused file
   descriptor, silently dropping whichever lock that descriptor number
   belonged to by then. Every lock now claims its state atomically under
   a per-instance reentrant state lock, so exactly one caller tears the
   lock down and concurrent or reentrant callers no-op
 * Fixed a ``release()`` reentering from a signal handler (the standard
   SIGTERM graceful-shutdown idiom) between the ownership guard and the
   unlink of ``TemporaryFileLock.release`` and ``PidFileLock.release``
   unlinking the lock files of whoever acquired the lock in between. The
   handle is claimed before the first OS call, so the reentrant release
   finds nothing to do and the successor's files survive
 * Fixed a ``KeyboardInterrupt`` escaping ``fh.close()`` during release
   leaving the handle stored after the file was unlinked, which let the
   next release pass the guard and unlink the successor's file. The
   stored handle is cleared before the close is attempted
 * Fixed two threads sharing one ``BoundedSemaphore`` instance both
   taking a slot: the second publication overwrote the first, one exit
   then released the other thread's slot and the orphaned slot stayed
   locked until garbage collection. The publication re-checks the
   already-taken guard in one atomic step now, so the losing thread
   gives its extra slot back and gets a ``LockException`` instead of
   leaking it. The slot sweep itself deliberately runs outside the
   instance state lock, so an ``os.fork`` in another thread cannot
   capture the state lock held across the sweep's OS calls
 * Fixed a child forked while any thread held an instance state lock
   deadlocking forever on its first ``release()``, ``acquire()`` or
   interpreter-exit cleanup: the child inherited the lock in its locked
   state, owned by a thread that does not exist there. Every live lock
   instance's state lock is reinitialized in the child via
   ``os.register_at_fork``, the way the standard library's ``logging``
   module protects its handler locks
 * Fixed ``RLock.release`` zeroing the count and claiming the handle in
   two separate state-lock scopes: an acquire racing into the gap saw
   the count at zero with the handle still published, took the fast
   path, and was handed the very filehandle the release then closed.
   The count transition and the claim are one atomic scope now
 * Fixed a failing ``PidFileLock.acquire`` contender's rollback wiping
   the instance state a winning thread had published concurrently: the
   winner's ``__exit__`` then no-oped and garbage collection of its
   orphaned sidecar freed the OS lock in the middle of the guarded
   block. The rollback only clears the state when its own failed
   sidecar is the published one
 * Fixed ``PidFileLock.acquire`` crashing with ``AssertionError`` (or
   returning ``None`` under ``python -O``) when a signal handler's
   ``release()`` landed between publishing the lock and returning: the
   return value is the locally bound sidecar handle now, never re-read
   from the shared state
 * Documented that two threads racing ``Lock.acquire`` on one instance
   is unsupported: with a per-process locker (POSIX ``lockf``) both
   lock calls succeed, the second publication overwrites the first, and
   the overwritten descriptor's eventual close releases the process's
   record locks on the file. That is inherent to POSIX record locks
   (any descriptor's close drops them), so no publication strategy can
   paper over it; use one instance per thread
 * Hardened the verified acquire of ``TemporaryFileLock`` and the
   ``PidFileLock`` sidecar against reentrant releases: a handle a
   signal handler claimed and closed mid-acquire is retried within the
   remaining timeout budget instead of failing the inode verification,
   and a closed or OS-level-dead handle found by the held-lock
   re-acquire reports the documented compromised-lock ``LockException``
   instead of leaking a raw ``ValueError`` from ``fileno()`` or an
   ``EBADF`` ``OSError`` from ``fstat``
 * Behaviour change: ``Lock`` resolves its path with ``os.path.abspath``
   at construction, so the ``filename`` attribute now holds an absolute
   path. A relative path used to be resolved on every later OS call,
   and an ``os.chdir`` between acquire and release (the daemonize idiom
   does ``chdir('/')``) made release and the interpreter-exit cleanup
   unlink another process's equally-named lock files at the new working
   directory while leaving the lock's own files behind
 * Fixed a ``KeyboardInterrupt`` during ``Lock.acquire`` leaking the
   opened descriptor for the traceback's lifetime when it landed in the
   retry sleep, and leaving the OS lock held by an untracked descriptor
   (with ``release`` a silent no-op) when it landed between the
   successful lock and the publication of the handle. Every failed exit
   from ``acquire``, interrupts included, now unlocks and closes the
   descriptor first; ``PidFileLock.acquire`` rolls its sidecar back the
   same way
 * Fixed the interpreter-exit cleanup skipping a ``TemporaryFileLock``
   or ``PidFileLock`` constructed before a fork and acquired inside the
   child: the owning pid was recorded at construction only, so the
   child's exit left its lock file, and for ``PidFileLock`` a stale PID
   payload, behind. Ownership is re-recorded on every fresh acquire.
   Also documented that a ``with lock:`` block entered before a fork
   runs ``__exit__`` in both processes, so the daemonize pattern must
   fork outside the block or leave the child via ``os._exit``
 * Fixed ``RLock`` losing acquire counts when two threads nested
   acquires on one instance: the bare read-modify-write let one
   increment overwrite the other, and the later releases closed the
   file while a hold was still outstanding. The counter transitions run
   under the instance state lock now, which also closes the equivalent
   lost update on free-threaded (no-GIL) builds
 * Fixed ``PidFileLock.__exit__`` replacing the ``with`` body's own
   exception with a release error and ignoring
   ``raise_on_release_error`` in both directions: the POSIX release
   leaked unlink errors with the flag unset and the Windows release
   swallowed them with it set. The exit path routes through
   ``Lock.__exit__`` now (release failures are chained onto the body's
   exception instead of masking it) and ``PidFileLock.release`` follows
   the flag: unlink failures are logged by default and raised in strict
   mode
 * Fixed ``TemporaryFileLock`` and ``PidFileLock`` rejecting the
   ``raise_on_release_error`` keyword their documentation described:
   both constructors accept and forward it now
 * Fixed the Windows unlink retry of ``TemporaryFileLock.release``
   letting every non-``PermissionError`` failure escape regardless of
   ``raise_on_release_error``, unlike the POSIX path, and sleeping once
   more after its final failed attempt. Non-retryable errors follow the
   flag contract now and the trailing sleep is gone
 * Behaviour change: ``PidFileLock.acquire`` no longer normalizes plain
   ``LockException`` failures from the sidecar to ``AlreadyLocked``.
   ``AlreadyLocked`` means contention and nothing else; a terminal
   backend failure (``ENOLCK``, an unsupported filesystem) propagates
   as itself instead of telling callers to retry a failure retrying
   cannot fix
 * Fixed lock exceptions being unpicklable when they carried an open file
   object on ``fh``, which made every contention raised inside a
   ``multiprocessing`` worker crash the result pipe with
   ``MaybeEncodingError: ... cannot pickle 'TextIOWrapper'`` instead of
   delivering ``AlreadyLocked`` to the parent. Pickling now drops the
   handle (an integer descriptor is kept) and the new ``fh_name``
   attribute preserves the file's name as a plain string, recursively
   for wrapped lock exceptions. ``copy.deepcopy`` drops the handle the
   same way, while ``copy.copy`` keeps ``fh`` shared with the original
   via an explicit ``__copy__``, since a shallow copy never leaves the
   process where the handle is valid. A handle whose ``name`` lookup
   raises (a detached ``io.TextIOWrapper``) no longer breaks exception
   construction
 * Fixed the ``AlreadyLocked`` raised by ``Lock.acquire`` with
   ``fail_when_locked=True`` having ``strerror=None`` with the OS message
   buried inside a wrapped inner exception. The wrap now propagates the
   locker's own arguments, so ``.strerror`` is populated at the ``Lock``
   level as the migration guide promises. ``args[0]`` is therefore now
   the original ``OSError`` (POSIX) or error code (Windows) instead of
   the inner lock exception, which stays reachable as ``__cause__``.
   The wrap also forwards ``fh`` and ``holder_pid`` from the inner
   exception, so ``fh_name`` and the holder PID survive a pickle across
   a multiprocessing boundary, where ``__cause__`` does not
 * Behaviour change: a failing POSIX module-level ``unlock`` now raises
   ``portalocker.LockException`` instead of leaking the raw ``OSError``,
   matching what the Windows unlock has always done. The ``OSError``
   (and its ``errno``) stays reachable through ``args[0]`` and
   ``__cause__``. The NFS-specific ``EOFError`` is wrapped as well,
   matching what ``lock`` already did
 * Deprecated ``exceptions.FileToLarge``: no version of this package has
   ever raised it, so code catching it catches nothing. The class stays
   for backwards compatibility and now emits a ``DeprecationWarning`` on
   instantiation
 * Corrected the exception documentation: the docs claimed every raise
   passes the ``LOCK_FAILED`` code as ``args[0]``, but the POSIX lockers
   put the originating ``OSError`` there. Both real shapes are now
   described, the doctests build the honest POSIX shape, the module-level
   POSIX ``lock``/``unlock`` docstrings note that a raw
   ``(lock, unlock)`` callable tuple owns its own error translation, and
   the README ``'r+'`` example now creates its file first
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
 * ``RedisLock.__del__`` is now best effort and suppresses all errors
   instead of surfacing them as interpreter-level "Exception ignored in"
   messages during garbage collection or shutdown (#140)
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
 * Fixed a 4.0.0 regression where garbage collection of a lock object tore
   down a held lock: ``LockBase.__del__`` released the OS lock, closed the
   filehandle and, for ``TemporaryFileLock`` and ``PidFileLock``, unlinked
   the lock file the moment the wrapper was collected. The throwaway idiom
   ``fh = Lock(path).acquire()`` therefore lost mutual exclusion instantly,
   since only the filehandle stays referenced. The finalizer is removed,
   restoring the 3.2.0 semantics, and ``TemporaryFileLock`` still cleans
   up a lock held at interpreter exit through its ``atexit`` handler.
   That handler resolves a weak reference, so the exit cleanup needs the
   wrapper to still be referenced. A wrapper discarded mid-run leaves
   the lock file behind at exit, with the lock itself released once the
   filehandle is closed or collected
 * ``release()`` now honours ``raise_on_release_error=False`` on the
   ``Lock`` and ``TemporaryFileLock`` teardown paths:
   ``TemporaryFileLock.release()`` suppressed only ``FileNotFoundError``
   from the unlink, so for example a ``PermissionError`` from a read-only
   directory escaped despite the default and could replace an exception
   already leaving a ``with`` block. Suppressed release errors are now
   logged at warning level instead of disappearing, which also means
   that with no logging configured they print to stderr through Python's
   last-resort handler. That is intentional visibility for previously
   silent failures, not a new bug. ``Lock.__exit__`` guarantees the
   block's own exception wins whether or not ``raise_on_release_error``
   is set, with the release error chained on as its ``__context__``, and
   the chain is kept free of the reference cycle that a release error
   raised while the body exception was in flight used to create.
   ``PidFileLock`` overrides ``__exit__`` and its release does not yet
   honour the flag, so its unlink errors can still escape and mask a
   body exception. That fix is tracked separately
 * Fixed ``LockBase.__delete__`` releasing the wrong object: deleting a
   lock stored as a class attribute (``del owner.attribute``) called
   ``release()`` on the owner instead of the lock, raising
   ``AttributeError`` and leaving the lock held. The lock now releases
   itself
 * Corrected the 4.0.0 changelog entry that claimed ``Lock.release()``
   "continues suppressing unlock and close errors by default": 3.x
   propagated those errors, so the suppression was a 4.0.0 behaviour
   change. The default stays as documented in 4.0.0, now with the
   warning-level logging described above
 * Behaviour change: the module-level ``lock()`` now validates its flags
   on every platform, before any system call. Flags carrying
   ``LockFlags.UNBLOCK`` raise ``RuntimeError`` (on POSIX they used to
   silently *release* the held lock, since the bit went straight through
   to ``fcntl``). ``SHARED | EXCLUSIVE`` raises ``RuntimeError``. A flag
   set naming no lock type at all (``LockFlags(0)`` or ``NON_BLOCKING``
   alone) raises ``RuntimeError`` on every platform instead of on POSIX
   only
 * Fixed the ``MsvcrtLocker`` fallback table for ``LK_*`` constants
   missing from ``msvcrt``. The old values were shifted against the real
   ``<sys/locking.h>`` numbers, so a "blocking lock" through the fallback
   would have issued an *unlock* (``LK_LOCK`` fell back to the
   ``LK_UNLCK`` value). The corrected values also now live on the locker
   instance instead of being ``setattr``'d onto the shared stdlib
   ``msvcrt`` module
 * ``Win32Locker.lock`` now wraps ``OSError`` (for example a stale file
   descriptor handed to ``msvcrt.get_osfhandle``) in ``LockException``,
   matching the unlock path. Previously the raw ``OSError`` escaped
   ``lock()``
 * ``Win32Locker`` now creates a fresh ``OVERLAPPED`` structure for every
   ``LockFileEx``/``UnlockFileEx`` call instead of reusing one cached
   instance across calls and threads, which the Win32 API contract
   forbids
 * ``python -m portalocker combine`` now reads and assembles all of its
   inputs before opening ``--output-file``. It used to truncate the
   output file first, so a non-ASCII byte in an input destroyed a
   pre-existing build and died with a raw traceback. Decode failures in
   ``README.rst`` and ``LICENSE`` now log the same snippet, naming the
   offending file, that the source modules already got
 * Behaviour change: without the optional redis dependency installed,
   ``portalocker.RedisLock`` is now a stub class whose constructor raises
   ``ImportError`` naming the dependency and the install command
   (``pip install "portalocker[redis]"``). It used to be ``None``, so
   constructing it failed with ``TypeError: 'NoneType' object is not
   callable``. Code that compared ``RedisLock is None`` to detect the
   extra should try constructing it and catch ``ImportError`` instead
 * Removed the legacy universal-newline (``U``) mode strings from the
   ``portalocker.types.Mode`` literal. Python 3.11 removed them, and 3.10
   only accepted them with a warning, so this narrows the static typing
   contract only and changes nothing at runtime
 * Documented that a blocking ``msvcrt`` lock retries ten times at one
   second intervals and then raises, instead of blocking indefinitely
   like POSIX, and added the missing 4.0.0 migration note for
   ``LockfLocker``: it silently used ``flock`` up to 3.2.0, and the
   switch to real ``lockf`` changes same-process contention, makes
   closing any descriptor for the file drop the locks, and leaves a 3.x
   holder and a 4.x holder unable to exclude each other on Linux
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
 * Behaviour change: fixed ``TemporaryFileLock.acquire`` destroying a held
   lock when a third party unlinked or replaced the lock file (a tmp
   cleaner sweeping ``/tmp`` is enough). The inode re-check introduced with
   the 4.0.0 split-brain fix released and closed the caller's live
   filehandle on the mismatch. With the default timeout it then silently
   re-acquired a new inode (an unlocked window a competitor could win,
   with the caller's original handle left closed), and with ``timeout=0``
   it raised ``AlreadyLocked`` after having dropped the lock it actually
   held. Re-acquiring while holding a still-valid lock file is now an
   idempotent no-op returning the held filehandle, and a held lock whose
   path was unlinked or replaced externally now raises ``LockException``
   and leaves the held filehandle untouched instead of closing it. A
   ``PidFileLock`` re-acquire runs the same verification on its sidecar.
   On both classes ``release`` now also skips the unlink, with a warning
   in the log, when the held handle no longer names the path, so a
   compromised holder cannot delete the lock file a competitor has since
   created. The inode comparison uses ``os.path.samestat``, which checks
   the device as well as the inode
 * Fixed the ``TemporaryFileLock.acquire`` verification retry restarting
   the full timeout for every attempt, which compounded the worst-case
   wall time to roughly ``timeout**2 / check_interval``. The retries now
   share a single deadline and every retry is only handed the remaining
   budget
 * Fixed ``PidFileLock.acquire`` on an instance that already holds the
   lock dropping the held OS lock mid-call: the old sidecar ``Lock`` was
   overwritten, and the discarded object's teardown released the lock
   before the replacement re-acquired it, a window another process could
   win. A second acquire on a holding instance is now an idempotent no-op
   that touches neither the sidecar lock nor the PID file
 * Fixed ``PidFileLock`` ignoring its instance-level ``timeout`` and
   ``check_interval`` when acquiring with ``fail_when_locked=False``: the
   sidecar ``Lock`` was built from the per-call arguments only, so a
   ``None`` argument silently selected the five second module default
   instead of the instance attribute, making
   ``PidFileLock(path, timeout=0.4).acquire()`` block for five seconds
   while ``acquire(timeout=0.4)`` behaved. The call arguments are now
   coalesced with the instance attributes first, per the documented
   ``LockBase`` contract
 * Fixed a contender interrupted while waiting for a ``PidFileLock``
   destroying the live holder's lock on its own exit. ``acquire`` stored
   the sidecar ``Lock`` before taking it and only ``except Exception``
   cleared it, so a ``KeyboardInterrupt`` or ``SystemExit`` (a SIGTERM
   handler calling ``sys.exit`` is the usual daemon idiom) left the
   instance claiming a lock it never took, and its release, explicit or
   via the exit handler, unlinked the PID and sidecar files belonging to
   the actual holder, letting the next acquirer create a second holder.
   The sidecar reference is now only published after a fully successful
   acquire, and ``release`` additionally refuses to unlink anything when
   the sidecar ``Lock`` no longer holds a filehandle. An interrupt
   arriving between taking the sidecar lock and publishing it now rolls
   the sidecar back as well, where it previously stranded the OS lock on
   a local variable that only garbage collection could release, so a
   pinned traceback kept every contender blocked
 * Behaviour change: ``PidFileLock`` used as a context manager now raises
   ``AlreadyLocked`` on entry when another process holds the lock but its
   PID cannot be read (missing, unreadable or invalid PID file). It used
   to return ``None`` in that case, which the documented contract defines
   as "this process is the holder", so a chmod'ed or deleted PID file made
   callers run their exclusive block next to a live holder
 * ``PidFileLock.read_pid`` now only accepts a plain positive ASCII
   decimal and reports anything else as unreadable (``None``). It used to
   parse everything ``int`` accepts, including ``-1``, ``0``, ``+7``,
   ``1_000`` and non-ASCII digits, and the obvious consumer feeds the
   result to ``os.kill``, where ``-1`` signals every process the user owns
 * Fixed ``PidFileLock`` publishing the PID by truncating the PID file in
   place, which let a concurrent reader observe the previous (possibly
   dead) holder's PID or an empty file mid-write. The PID is now written
   to a temporary file next to the PID file and moved over it with
   ``os.replace``, so readers see either the old complete PID or the new
   complete PID
 * Fixed the Windows ``PidFileLock.release`` path unlinking the PID file
   after releasing the sidecar lock, which could delete the PID a fast
   successor had already published. The PID file, which carries no OS lock
   on any platform, is now unlinked before the sidecar release, mirroring
   the POSIX ordering
 * ``TemporaryFileLock`` and ``PidFileLock`` now annotate ``filename`` as
   ``types.Filename`` like ``Lock`` does, so passing a ``pathlib.Path``,
   which always worked at runtime, no longer fails static type checking
 * Documentation fix: the 4.1.0 documentation gave ``BoundedSemaphore``
   two contradicting ``fail_when_locked`` contracts. The constructor
   docstring promised that a full semaphore raises ``AlreadyLocked``
   straight away, while ``acquire`` documented what the code has
   actually done since 3.2.0: the flag is consulted only once the
   ``timeout`` has expired. The runtime behaviour is unchanged and the
   constructor docstring was the one corrected. A full semaphore retries
   for the whole timeout and then raises ``AlreadyLocked``, or returns
   ``None`` with ``fail_when_locked=False``. Both the timing and that
   ``None`` return diverge from the other lock classes and are now
   called out loudly in the documentation
 * Behaviour change: acquiring a ``BoundedSemaphore`` or
   ``NamedBoundedSemaphore`` instance that already holds a slot now
   raises ``portalocker.LockException`` instead of ``AssertionError``.
   The assert was the only re-acquire guard and ``python -O`` strips
   asserts, so a second acquire silently consumed a second slot,
   overwrote the reference to the first and starved competitors of a
   slot nobody could release anymore
 * Fixed the ``BoundedSemaphore`` default-name ``DeprecationWarning``
   being attributed to portalocker's own source (``stacklevel=1``). It
   now points at the constructing caller, so it names the code to fix
   and deduplicates per call site instead of once globally
 * Fixed ``TemporaryFileLock`` and ``PidFileLock`` registering one
   ``atexit`` callback per constructed instance and never unregistering
   it, which grew without bound in long-running processes churning
   through short-lived locks. A single module level hook registered once
   at import now releases whichever locks are still held at interpreter
   exit
 * The interpreter-exit cleanup for ``TemporaryFileLock`` and
   ``PidFileLock`` no longer releases locks a forked child inherited
   from its parent: the exit hook now only releases locks constructed by
   the exiting process itself. The child inherits the live lock objects,
   and its normal exit unlinked the parent's lock files while the parent
   still believed it held them, breaking the classic acquire-then-fork
   daemonize sequence. Together with 4.1.1's removal of lock teardown at
   garbage collection time this closes that fork hole for locks acquired
   before forking. A lock constructed in the parent but acquired inside
   a forked child is still not cleaned up at that child's exit
 * Behaviour change: ``portalocker.open_atomic`` now raises
   ``FileExistsError`` when the destination already exists on entry,
   matching its documentation and the publication-time race. It raised
   ``AssertionError`` before
 * Fixed a 4.0.0 regression in ``portalocker.open_atomic``: publication
   uses a hard link on POSIX, which hard-failed on filesystems without
   hard link support (exFAT and some SMB, NFS and FUSE mounts) where
   3.2.0's rename worked, and the cleanup then deleted the freshly
   written payload as well. Publication now falls back to an existence
   check plus rename on those filesystems. The fallback still publishes
   content atomically but cannot reliably refuse concurrent publishers,
   so the strong no-replace guarantee continues to require hard link
   support, as the documentation now states
 * Behaviour change: when ``open_atomic`` fails to publish, for any
   reason, the temporary file is now kept and the raised exception names
   its path, where the payload was previously deleted without a trace.
   An exception from the caller's body, on the other hand, now removes
   the temporary file that used to be leaked, and a body that closes the
   handle itself no longer breaks publication because the payload is
   synchronized through a fresh descriptor
 * Fixed ``open_atomic`` publishing destinations with the private
   ``0o600`` permissions of its temporary file. The temporary file is
   now created with mode ``0o666`` so the kernel applies the process
   umask at creation, and the destination carries the permissions a
   plain ``open`` would have produced, without portalocker ever touching
   the process-wide umask
 * Documented two ``BoundedSemaphore`` operational hazards: a slot file
   deleted externally mid-hold silently admits an extra holder, and the
   default directory is the tmp-cleaner-patrolled system temporary
   directory, so long-running semaphores need a private directory exempt
   from cleanup. Also documented that ``open_atomic`` does not fsync the
   directory entry, so the published name itself is not guaranteed
   durable across power loss

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
 * Behaviour change: ``Lock.release()`` now suppresses unlock and close
   errors by default, where 3.x propagated them (and an unlock failure
   skipped the close). Closing is always attempted and the file handle
   reference is cleared. Callers can opt into reporting cleanup failures
   with ``Lock(..., raise_on_release_error=True)`` (#117)
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
