Troubleshooting
===============

Six symptoms that show up more than once, each with its cause and its
fix. This page assumes you already picked a lock class and are past the
happy path; for that groundwork see :doc:`quickstart` and
:doc:`lock-types`.

My lock doesn't stop another process from writing
-------------------------------------------------

**Cause:** ``portalocker`` locks are advisory on POSIX: a process that
never asks for the lock is never blocked by it, and never learns one
exists. See :doc:`platforms` for the full explanation, including a
demonstration of the clobber and why the same code *is* enforced on
Windows.

**Fix:** there is no flag that makes POSIX locking mandatory. Every
process that touches the file, including code you do not control, has
to take the same lock before reading or writing it.

ImportError when taking a shared lock on Windows
------------------------------------------------

**Cause:** requesting a shared lock
(``flags=portalocker.LockFlags.SHARED``) on Windows needs the optional
``pywin32`` package, no longer installed by default since 4.0.0 -- see
:doc:`platforms` for why ``msvcrt`` alone cannot provide one. Without
it, the failure reads:

.. code-block:: text

    ImportError: Shared locks on Windows require the win32 extra
    (pywin32); msvcrt provides no true shared lock. Install it with:
    pip install "portalocker[win32]"

Through the module-level `portalocker.lock` function, or a directly
constructed `Win32Locker`, that ``ImportError`` propagates unchanged.
Through `Lock.acquire`, it is caught like any other exception and
re-raised as a ``LockException`` wrapping it, so the message above still
appears in ``str(exc)`` even though the exception type is different.

**Fix:** install the extra.

.. code-block:: console

    pip install "portalocker[win32]"

The other process doesn't see my data
-------------------------------------

**Cause:** a lock serialises *access*, not visibility -- your bytes can
still be sitting in a buffer when another process looks. See "Flush
before you release" in :doc:`platforms` for the two layers involved.

**Fix:** call ``flush()``, then `os.fsync`, before another process needs
to see the write:

>>> import os
>>> import portalocker
>>> with portalocker.Lock('output.txt', 'w', timeout=5) as fh:
...     _ = fh.write('visible to the next reader')
...     fh.flush()
...     os.fsync(fh.fileno())

AlreadyLocked raised immediately without waiting
------------------------------------------------

**Cause:** two independent settings skip the retry loop and fail on the
very first contended attempt instead of waiting up to ``timeout``:

- ``fail_when_locked=True``. `TemporaryFileLock` and `PidFileLock` both
  default to this, unlike `Lock`, so switching to either one changes
  this behaviour even if ``timeout`` itself is left untouched -- see
  :doc:`lock-types`.
- ``timeout=0``. The retry clock in `LockBase._timeout_generator` only
  starts *after* the first attempt, so a ``timeout`` of ``0`` still buys
  exactly one attempt and nothing more, regardless of
  ``fail_when_locked``.

>>> import portalocker
>>> first = portalocker.Lock('contended.lock', fail_when_locked=True)
>>> _ = first.acquire()
>>> second = portalocker.Lock('contended.lock', fail_when_locked=True)
>>> try:
...     second.acquire()
... except portalocker.AlreadyLocked:
...     print('raised on the first attempt, no retry')
raised on the first attempt, no retry
>>> first.release()

**Fix:** if retrying for the full ``timeout`` is what you want, leave
``fail_when_locked`` at its default (``False``) and pass a positive
``timeout``.

A stale lock file is left behind after a crash
----------------------------------------------

**Cause:** `TemporaryFileLock` and `PidFileLock` remove their lock file
in `release`, with two fallbacks for code that forgets to call it:
garbage collection (`LockBase.__del__`) and an `atexit` handler
registered when the lock is constructed. None of those three run when
the process is killed outright -- ``SIGKILL``, a segfault, a lost VM --
so the file stays on disk.

This is expected, and mostly harmless. The kernel releases the
process's advisory lock the moment it dies, file present or not, so the
next `acquire` on the same path succeeds immediately; the leftover file
is inert litter, not a lock that needs clearing by hand. For
`PidFileLock` specifically, `PidFileLock.read_pid` can then report a PID
that no longer exists -- it only ever says who last *wrote* the file,
never whether that process is still alive.

4.0.0 fixed a related but distinct race in the same area, tracked as
issue #115: a competing acquirer could open and lock the very inode a
releaser was in the middle of unlinking, so both processes believed they
held the lock at once (split-brain). ``acquire`` now re-verifies, after
locking, that its handle still names the current path, and retries
within the timeout if a race is detected.

**Fix:** nothing to do for the stale file itself; it is safe to ignore
or delete. See :doc:`lock-types` for what `TemporaryFileLock` guarantees
on release, and `PidFileLock.release` for the PID file's own release
behaviour.

str() of a lock exception changed after upgrading
-------------------------------------------------

**Cause:** before 4.0.0, ``str(exc)`` on POSIX was whatever the bare
``OSError`` from ``fcntl`` reported. Since 4.0.0, every lock failure --
POSIX and Windows alike -- passes an error code as the first argument
and the message as the second, matching the two-argument convention
`BaseLockException` already followed on Windows. That makes
``str(exc)`` on POSIX a 2-tuple repr instead of the bare message:

>>> from portalocker import exceptions
>>> exc = exceptions.LockException(
...     exceptions.LockException.LOCK_FAILED,
...     'Resource temporarily unavailable',
... )
>>> str(exc)
"(1, 'Resource temporarily unavailable')"
>>> exc.strerror
'Resource temporarily unavailable'

**Fix:** read `BaseLockException.strerror` instead of parsing
``str(exc)``; it has held the message consistently on both platforms
since 4.0.0. See :doc:`migration` for the rest of what changed in that
release.
