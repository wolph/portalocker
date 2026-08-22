Lock Types
==========

portalocker ships several lock classes. All of them share the retry
policy documented on `LockBase` (``timeout``, ``check_interval`` and
``fail_when_locked``), and all of them work as context managers. What
differs is what they lock, what they leave behind, and what they hand
you when the ``with`` block starts.

This page maps a need to a class. For the platform details behind any
of them (advisory vs. mandatory locking, ``flock`` vs. ``lockf``,
``msvcrt`` vs. ``pywin32``, NFS caveats), see :doc:`platforms`.

Selection guide
---------------

.. list-table::
    :header-rows: 1

    * - Need
      - Class
    * - Lock a file while you read or write it
      - `Lock`
    * - Acquire the same lock again from code nested inside the first
        ``with`` block
      - `RLock`
    * - A pure mutex with no data worth keeping; the lock file should not
        outlive the lock
      - `TemporaryFileLock`
    * - "Only one instance of this program", plus a way to see which PID
        is already running
      - `PidFileLock`
    * - Cap how many processes run a section at once
      - `NamedBoundedSemaphore` (`BoundedSemaphore` is the same idea, but
        deprecated when used without an explicit name)
    * - Coordinate processes across machines, through Redis instead of a
        shared filesystem
      - `RedisLock`

Lock
----

`Lock` opens a file, locks it, and hands the caller the open filehandle.
Releasing unlocks and closes that handle, but leaves the file itself in
place, so it doubles as a plain data file as well as a mutex.

Guarantees:

- The lock is exclusive by default (`LockFlags.EXCLUSIVE`); pass
  ``flags=portalocker.LockFlags.SHARED`` for a shared/read lock.
- Locking is per open filehandle, not per process: two `Lock` instances
  on the same path in the same process still contend with each other.
- A mode containing ``w`` is silently rewritten to ``a`` and the
  truncation is deferred until after the lock is taken, so an existing
  holder's data is never discarded before contention is checked. On
  POSIX the kernel append flag is cleared again once the truncation is
  done, so the handle honours seek positions exactly like a plain
  ``open(mode='w')``.

On Windows that ``a`` substitution is visible: the handle keeps append
semantics, so every ``write`` lands at the end of the file no matter
where you ``seek``. ``fh.write('x'); fh.seek(0); fh.write('y')``
produces ``'xy'`` there, where the builtin ``open(mode='w')`` produces
``'y'``. Sequential writers never notice, but a positioned rewrite
silently corrupts its own output. If you need positioned writes on
Windows, reopen the file after acquiring, or lock with mode ``r+`` and
truncate explicitly once you hold the lock.

Costs:

- The lock file is never removed. Cleaning it up, if it should not
  persist, is the caller's job (see `TemporaryFileLock` for the
  alternative).
- Acquiring twice from separate instances on the same path blocks or
  raises like any other contention; `Lock` itself is not reentrant (see
  `RLock`).

Reach for it when you want the default: locking a real file whose
contents you also want to read or write.

>>> import portalocker
>>> with portalocker.Lock('example.lock', 'w', timeout=1) as fh:
...     _ = fh.write('locked while the block runs')

RLock
-----

`RLock` is the reentrant version of `Lock`: it can be acquired more than
once by the same instance, and only releases the underlying file lock
once a matching number of `RLock.release` calls have been made.

Guarantees:

- Nesting is safe: acquiring an already-held `RLock` returns the same
  filehandle immediately, without touching the operating system again.
- The file is only unlocked and closed once the acquire count returns to
  zero.

Costs:

- Reentrancy is tracked per instance, not per process: a *second*
  `RLock` object on the same path still contends with the first one, so
  passing the file path (rather than the lock object) to nested code
  does not give you reentrancy.
- Releasing more often than it was acquired raises ``LockException``
  rather than being silently ignored.

Reach for it when the same code path may re-enter a block that already
holds the lock, such as a function that locks a file and calls itself,
or calls another function that locks the same file.

>>> import portalocker
>>> lock = portalocker.RLock('example.lock')
>>> fh = lock.acquire()
>>> fh is lock.acquire()  # nested acquire, same handle
True
>>> lock.release()
>>> fh.closed
False
>>> lock.release()
>>> fh.closed
True

BoundedSemaphore
-----------------

`BoundedSemaphore` is a counting semaphore built from ``maximum`` lock
files: acquiring means locking whichever one is still free, so up to
``maximum`` holders can run at once.

Prefer `NamedBoundedSemaphore` instead of this class directly. Without
an explicit ``name``, `BoundedSemaphore` falls back to the shared
default name ``'bounded_semaphore'``, which makes two unrelated programs
on the same machine collide on the same slots, and constructing it that
way raises a ``DeprecationWarning``. If you do use `BoundedSemaphore`
directly, always pass a ``name``.

Guarantees:

- `BoundedSemaphore.acquire` sweeps the slots in a fixed numerical order
  on every attempt, so all contenders race for slot 0 first, then slot
  1, and so on.
- ``fail_when_locked`` is consulted only after the ``timeout`` has
  expired, unlike every other lock class here: a full semaphore always
  retries for the whole timeout, even with the flag set. Running out of
  time then raises ``AlreadyLocked`` with ``fail_when_locked=True``
  (the default) and returns ``None`` with ``fail_when_locked=False``.
  Both are divergences kept as they have behaved since 3.2.0, so check
  the return value when you pass ``fail_when_locked=False``.
- `BoundedSemaphore.release` only unlocks the held slot; the lock files
  themselves stay on disk so the same slots can be reused later.

Costs:

- Constructing it without a ``name`` (or with the literal default name)
  emits a ``DeprecationWarning`` and risks colliding with unrelated
  programs; see `NamedBoundedSemaphore`.
- Acquiring while already holding a slot is a programming error
  (``LockException``), so release first. Before 4.2.0 this guard was an
  ``assert``, so ``python -O`` silently took a second slot instead.
- The slot files must survive while their slots are held. A slot file
  that is deleted externally mid-hold silently admits an extra holder
  (the operating system lock lives on the deleted inode, invisible to
  new acquirers), and the default ``directory`` is the system temporary
  directory, exactly where tmp cleaners prune. Point ``directory`` at a
  location exempt from cleanup for anything long running, and prefer a
  private directory on multi-user systems: a slot file owned by another
  user typically raises ``PermissionError`` out of ``acquire`` instead
  of counting as busy.

>>> import portalocker
>>> semaphore = portalocker.BoundedSemaphore(2, name='workers', directory='')
>>> with semaphore as lock:
...     lock is not None
True

NamedBoundedSemaphore
----------------------

`NamedBoundedSemaphore` is the recommended form of `BoundedSemaphore`:
identical behaviour, but the name is either one you chose or one
generated at random, never the shared default that makes unrelated
programs collide. Constructing it never emits the deprecation warning
that a name-less `BoundedSemaphore` does.

Guarantees and costs are otherwise the same as `BoundedSemaphore`: slots
are locked in numerical order, release leaves the lock files behind, and
holding a slot twice from the same instance is a programming error.

Because the semaphore works across processes, give it an explicit
``name`` whenever more than one process needs to share it; leaving
``name`` unset generates a random one, which only makes sense when the
semaphore object itself (not just its name) is handed to the other
processes, for instance to worker processes spawned by the current one.

Reach for `NamedBoundedSemaphore` whenever you would otherwise reach for
`BoundedSemaphore`: to cap the number of processes or threads running a
section at once, across as many operating systems as portalocker
supports.

>>> import portalocker
>>> semaphore = portalocker.NamedBoundedSemaphore(2, name='workers', directory='')
>>> with semaphore as lock:
...     lock is not None
True

PidFileLock
-----------

`PidFileLock` is the classic "only one instance of this daemon" lock. It
writes the current process's PID to ``filename`` and, when contended,
reports the PID of whoever already holds it.

Two files are involved: `filename` holds the human-readable PID, and a
sidecar ``<filename>.lock`` next to it carries the actual operating
system lock. The split exists because locking is mandatory on Windows,
so a lock taken directly on the PID file would stop anyone from reading
it.

Guarantees:

- Used as a context manager, entering the ``with`` block does not raise
  on ordinary contention: it returns `None` when this process took the
  lock, or the competing PID (an `int`) when it did not. The block runs
  either way, so the body has to check the value. The one exception is a
  holder whose PID cannot be read (a missing, unreadable or corrupt PID
  file): that raises ``AlreadyLocked`` instead of returning `None`,
  because `None` would falsely report this process as the holder.
- The exception surface follows the retry contract: ``AlreadyLocked``
  means contention and nothing else, whether it surfaced through
  ``fail_when_locked`` or an expired timeout. A plain ``LockException``
  is a terminal backend failure (``ENOLCK``, an unsupported filesystem)
  and propagates as itself, since retrying it cannot help. Before 4.2.0
  every plain ``LockException`` from the sidecar was dressed up as
  ``AlreadyLocked``.

Costs:

- The fail-open entry behaviour is a deliberate departure from every
  other lock on this page. Use `PidFileLock.fail_closed` instead of
  the plain context manager when a contended lock should abort the block
  rather than run it with a competing PID in hand.
- Two files on disk instead of one.

>>> import portalocker
>>> with portalocker.PidFileLock('example.pid') as holder_pid:
...     holder_pid is None  # None means this process holds the lock
True

>>> with portalocker.PidFileLock('example.pid').fail_closed():
...     print('exclusive work happens here')
exclusive work happens here

TemporaryFileLock
-----------------

`TemporaryFileLock` is a `Lock` whose lock file only exists while the
lock is held. Use it when the file is purely a mutex, with no data worth
keeping, and leaving it behind afterwards would just be litter.

Guarantees:

- `TemporaryFileLock.release` unlinks the lock file. For a caller that
  forgets to release, a single module level `atexit` hook releases any
  lock still held when the interpreter shuts down. Garbage collection of
  the lock object deliberately leaves a held lock and its file alone,
  since the caller may still be using the filehandle. The hook only acts
  in the process that owns the lock: ownership is recorded at
  construction and re-recorded on every fresh acquire, so a lock
  constructed before a fork and acquired inside the child is cleaned up
  by the child's exit, while a forked child exiting does not unlink the
  file of a lock its parent acquired and still holds.
- Releasing an instance that does not hold the lock is a no-op, so a
  stale or double-released instance cannot unlink the file out from
  under whoever holds it at that moment.

Costs:

- ``fail_when_locked`` defaults to `True` here (unlike `Lock`), so
  contention raises ``AlreadyLocked`` immediately rather than retrying
  for the full ``timeout``.
- The mode is fixed to ``'w'``: the file is always emptied once the lock
  is taken, so it is not meant to carry data between processes.

.. warning::

   A ``with lock:`` block does not survive a fork inside it. The child
   inherits the block and runs ``__exit__`` when it falls out of it,
   releasing the lock and unlinking the file while the parent still
   believes it holds them. The classic daemonize pattern (fork inside
   the guarded block, one side exits) must fork outside the ``with``
   block, or end the child with ``os._exit`` so the inherited block is
   never left. This applies to `TemporaryFileLock` and `PidFileLock`
   alike; the interpreter-exit cleanup itself is pid-aware and stays
   safe across forks.

>>> import os
>>> import portalocker
>>> lock = portalocker.TemporaryFileLock('example.lock')
>>> _ = lock.acquire()
>>> os.path.isfile('example.lock')
True
>>> lock.release()
>>> os.path.isfile('example.lock')
False

RedisLock
---------

`RedisLock` coordinates processes across machines through a Redis
pubsub channel instead of a shared filesystem: a holder subscribes to
the channel, and the lock is released the instant that connection
drops, whether the process closed it cleanly, crashed, or lost the
network, with no expiring key to wait out. See :doc:`redis` for the
pubsub design, its shared/exclusive election, and crashed-holder
reaping.

>>> import fakeredis
>>> import portalocker
>>> connection = fakeredis.FakeStrictRedis(
...     server=fakeredis.FakeServer(), decode_responses=True
... )
>>> with portalocker.RedisLock('example_channel', connection=connection):
...     print('do something here')
do something here
