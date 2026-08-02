Migrating from 3.x to 4.0.0
===========================

Most 3.x code runs unchanged on 4.0.0. `portalocker.Lock`, the
module-level `portalocker.lock` and `portalocker.unlock`, the ``with``
statement, and every exception type kept their names and their meaning.
What follows is everything that can still require an edit, ordered from
most to least likely to affect you.

Windows no longer installs ``pywin32`` for you
-----------------------------------------------

Up to 3.x, ``pip install portalocker`` pulled in ``pywin32`` on Windows.
Since 4.0.0 it does not: the default is `MsvcrtLocker`, which calls
``msvcrt.locking`` from the standard library and needs nothing extra.

**Exclusive locks are unaffected.** That covers the common case,
including every plain `portalocker.Lock`, on a dependency-free install.

**Shared locks are affected.** ``msvcrt.locking`` has no shared mode at
all, so ``LockFlags.SHARED`` on Windows delegates to `Win32Locker`, which
calls ``LockFileEx`` from ``pywin32``. Without the package installed you
get a descriptive ``ImportError`` rather than an obscure failure:

.. code-block:: text

    ImportError: Shared locks on Windows require the win32 extra
    (pywin32); msvcrt provides no true shared lock. Install it with:
    pip install "portalocker[win32]"

The fix is to ask for the extra:

.. code-block:: console

    pip install "portalocker[win32]"

POSIX installs are unaffected either way; the extra only declares a
dependency on Windows. See :doc:`platforms` for the rest of the
``msvcrt``/``pywin32`` split, including the second code path that needs
it -- releasing a lock. (#104)

POSIX lock exceptions changed shape
------------------------------------

This is the subtlest break in the release: no type changed, no new
exception is raised, only the text of one that already existed.

On 3.x, a POSIX lock failure passed the originating ``OSError`` as its
only positional argument and the message as a ``strerror`` *keyword*,
which `BaseLockException` never read. So ``str(exc)`` was the bare
``OSError`` text and ``exc.strerror`` was always `None`. Since 4.0.0 the
message is passed as the second *positional* argument -- the
two-argument convention `BaseLockException` already followed on Windows
-- so ``strerror`` is populated and ``str(exc)`` renders a 2-tuple repr
of both arguments:

.. code-block:: text

    portalocker 3.x
        str(exc)      '[Errno 11] Resource temporarily unavailable'
        exc.strerror  None

    portalocker 4.0.0
        str(exc)      "(BlockingIOError(11, 'Resource temporarily
                       unavailable'), '[Errno 11] Resource temporarily
                       unavailable')"
        exc.strerror  '[Errno 11] Resource temporarily unavailable'

The same shape, built by hand so it runs everywhere -- a real failure
puts the ``OSError`` that ``fcntl`` raised in the first slot, which is
why its ``[Errno N]`` prefix shows up in both halves above:

>>> from portalocker import exceptions
>>> original = OSError('Resource temporarily unavailable')
>>> exc = exceptions.AlreadyLocked(original, str(original))
>>> str(exc)
"(OSError('Resource temporarily unavailable'), 'Resource temporarily unavailable')"
>>> exc.strerror
'Resource temporarily unavailable'

**What to change:** if you parse ``str(exc)`` to recover the operating
system's message, read ``exc.strerror`` instead. It holds that message on
both platforms now, which the 3.x attribute never did on POSIX. Nothing
else about these exceptions moved -- the class hierarchy, the ``fh``
attribute, and ``except portalocker.AlreadyLocked`` all behave as before.
:doc:`troubleshooting` covers this from the symptom side.

Python 3.10 is the minimum
---------------------------

3.x still supported Python 3.9. 4.0.0 requires 3.10 or later, because 3.9
reached end of life and no longer receives fixes:

>>> import sys
>>> sys.version_info >= (3, 10)
True

If you are pinned to an older interpreter, stay on the 3.x series:

.. code-block:: console

    pip install "portalocker<4"

``LockBase`` is generic over its acquire return type
-----------------------------------------------------

`LockBase` used to be a plain ``abc.ABC``. It is now also
``typing.Generic``, parameterised by whatever its ``acquire`` returns, so
that type checkers know what a ``with`` block yields: `portalocker.Lock`
declares itself as ``LockBase[typing.IO[typing.Any]]``, and
`BoundedSemaphore` as ``LockBase['Lock | None']``.

This is a typing-only change. Subclasses keep working whether or not they
supply a parameter, and nothing about them changes at runtime:

>>> import typing
>>> from portalocker.utils import LockBase
>>> LockBase[typing.IO[typing.Any]]
portalocker.utils.LockBase[typing.IO[typing.Any]]
>>> class CountingLock(LockBase):
...     def acquire(
...         self, timeout=None, check_interval=None, fail_when_locked=None
...     ):
...         return 42
...
...     def release(self):
...         pass
>>> CountingLock().acquire()
42

To get the benefit in your own subclass, parameterise the base with what
your ``acquire`` returns -- ``class CountingLock(LockBase[int])``.

New names in the top-level namespace
-------------------------------------

`PidFileLock` is new in 4.0.0. It is a `TemporaryFileLock` that publishes
the holder's PID into the lock file, so a supervisor can read who holds
it, and its `PidFileLock.fail_closed` context manager refuses to enter
the body on contention, exposing the competing PID through
``AlreadyLocked.holder_pid`` when that is readable (#106, #118).

`NamedBoundedSemaphore` is not new -- it lived in `portalocker.utils` on
3.x -- but it was not re-exported. ``portalocker.NamedBoundedSemaphore``
now works, so the recommended semaphore no longer needs a ``utils``
import:

>>> import portalocker
>>> with portalocker.PidFileLock('worker.pid') as holder_pid:
...     holder_pid is None  # None means this process is the holder
True
>>> semaphore = portalocker.NamedBoundedSemaphore(
...     2, name='migration_demo', directory=''
... )
>>> with semaphore as slot:
...     slot is not None
True

See :doc:`lock-types` for how both sit next to the other lock classes.

Behavioural fixes worth knowing about
--------------------------------------

``Lock.release()`` no longer propagates cleanup errors
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

On 3.x, `Lock.release` unlocked and then closed, with no error handling
around either step. An unlock failure therefore propagated to the caller
*and* skipped the close, leaving the handle open and ``lock.fh`` still
set. Since 4.0.0 both steps are always attempted and ``lock.fh`` is
always cleared, and failures are swallowed by default -- which matters
most where you cannot handle them anyway, in ``__del__`` and when leaving
a ``with`` block that is already unwinding an exception:

>>> import portalocker
>>> lock = portalocker.Lock('release_demo.txt', 'w', timeout=1)
>>> fh = lock.acquire()
>>> fh.close()  # close it behind the lock's back
>>> lock.release()  # unlock and close both fail, both are swallowed
>>> lock.fh is None
True

Opt back into reporting with ``raise_on_release_error=True`` (#117):

>>> strict = portalocker.Lock(
...     'release_demo.txt', 'w', timeout=1, raise_on_release_error=True
... )
>>> fh = strict.acquire()
>>> fh.close()
>>> strict.release()
Traceback (most recent call last):
    ...
ValueError: I/O operation on closed file

``BoundedSemaphore`` recovers from non-contention errors
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

`BoundedSemaphore.try_lock` used to store its `Lock` on the instance
*before* acquiring it. A failure that was not contention -- a missing
``directory``, say -- then left that half-set lock behind, and every
later `BoundedSemaphore.acquire` tripped its ``assert not self.lock,
'Already locked'`` guard: the object stayed permanently "already locked"
even after the real cause was fixed. The lock is now recorded only once
it is actually held, and any other failure resets the attribute before
propagating:

>>> import portalocker
>>> semaphore = portalocker.NamedBoundedSemaphore(
...     2, name='recovery_demo', directory='no_such_directory'
... )
>>> try:
...     semaphore.acquire()
... except FileNotFoundError:
...     print('propagated, and the instance is still usable')
propagated, and the instance is still usable
>>> semaphore.lock is None
True

``RedisLock`` reaps crashed holders
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The liveness check published its ping before the prober's own
subscription was confirmed active, so the prober's own confirmation
satisfied the check and a dead holder was never reaped. 4.0.0 publishes
the ping only after the subscription is confirmed, stops leaking pubsub
connections on the reap path, makes ``fail_when_locked=True`` fail fast
instead of polling for the whole timeout, closes internally created
connections on release, and rolls a failed ``acquire()`` back cleanly so
the lock can be retried.

No signature changed, but code that worked around a crashed holder's lock
staying stuck can drop the workaround. See :doc:`redis` for how reaping
works, and for the shared (``LockFlags.SHARED``) Redis readers that 4.0.0
also added (#124).

Everything else
----------------

A few more fixes change behaviour without changing any signature:

- `portalocker.open_atomic` no longer silently replaces a destination
  that another process created while its context was open. Publication
  raises ``FileExistsError`` and preserves the concurrent winner (#114).
- `TemporaryFileLock` and `PidFileLock` closed an unlock-then-unlink race
  that could let two processes believe they held the same lock. Release
  now unlinks before unlocking on POSIX, and acquire re-verifies file
  identity after locking (#115). Their ``release()`` is also a no-op when
  the object does not hold the lock, so a stale object can no longer
  unlink the file out from under the current holder.
- On Windows, the ``msvcrt`` locker locked from the current file position
  instead of byte 0 for raw file descriptors, which could break mutual
  exclusion on files larger than 64KiB.
- ``python -m portalocker combine`` now inlines `portalocker.redis`
  properly, so the single-file build binds ``RedisLock`` the way the
  package does instead of always leaving it as `None`. See :doc:`cli`.

The complete list is in the :doc:`changelog`. If an upgrade still
misbehaves, :doc:`troubleshooting` groups the recurring symptoms by what
you actually see.
