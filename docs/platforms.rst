Platform Behaviour
==================

portalocker presents a single API over three quite different locking
systems: ``fcntl`` on POSIX, ``msvcrt`` on Windows, and the Win32 API
(``LockFileEx``/``UnlockFileEx``) when the optional ``pywin32`` package
is installed. The API hides the system calls; it cannot hide their
semantics, and the semantics are where the surprises live.

This page describes what the operating system actually does underneath.
For choosing a lock class, see :doc:`lock-types`. For installing the
optional extras, see :doc:`quickstart`. For symptom-first fixes, see
:doc:`troubleshooting`.

Advisory versus mandatory locking
---------------------------------

**On POSIX, file locks are advisory.** The kernel only enforces them
between processes that also ask for a lock. A process that simply opens
the file and writes to it is never blocked, never fails, and never
learns that a lock existed:

.. code-block:: python

    # Process A, holding an exclusive lock
    import fcntl

    fh = open('data.txt', 'r+')
    fcntl.flock(fh, fcntl.LOCK_EX)

    # Process B, which never locks. This succeeds immediately.
    with open('data.txt', 'r+') as other:
        other.write('clobbered')

A lock is therefore a convention, not a guard: it protects the file only
if *every* participant honours it. This is the single most common reason
for "portalocker does not seem to lock" reports.

**On Windows, file locks are mandatory.** ``msvcrt.locking`` and
``LockFileEx`` are enforced by the OS, so an unrelated process that reads
or writes an overlapping range of the file fails with a
``PermissionError`` even though it never asked for a lock:

.. code-block:: python

    # Process A (Windows), holding an exclusive lock
    import portalocker

    fh = open('data.txt', 'r+')
    portalocker.lock(fh, portalocker.LockFlags.EXCLUSIVE)

    # Process B, which never locks:
    with open('data.txt', 'r+') as other:
        other.write('clobbered')  # PermissionError

This asymmetry is not a portability wart you can ignore; it changes what
your program is allowed to do. `portalocker.PidFileLock`, for instance,
puts the OS lock on a sidecar ``<filename>.lock`` file precisely so that
the PID file itself stays readable on Windows (see :doc:`lock-types`).

Linux can be told to enforce locks, by mounting a filesystem with
``-o mand`` and marking individual files set-group-ID with the group
execute bit cleared. Don't. It is racy by design, it was deprecated in
Linux 4.5, and support was removed from the kernel in 5.15, so any code
relying on it stops working on a modern distribution. Treat POSIX locks
as advisory and make every participant lock.

Further reading:

* https://en.wikipedia.org/wiki/File_locking
* http://stackoverflow.com/questions/39292051/portalocker-does-not-seem-to-lock
* https://stackoverflow.com/questions/12062466/mandatory-file-lock-on-linux

``flock`` versus ``lockf`` on POSIX
------------------------------------

POSIX offers two unrelated locking mechanisms, and Python exposes both:
``fcntl.flock`` wraps ``flock(2)``, while ``fcntl.lockf`` wraps the
``fcntl(2)`` record locks. portalocker defaults to ``fcntl.flock``.

.. list-table::
    :header-rows: 1

    * -
      - ``fcntl.flock``
      - ``fcntl.lockf``
    * - Lock is owned by
      - the open file description
      - the process, per file
    * - Two ``open()`` calls in one process
      - conflict with each other
      - do **not** conflict; the second call replaces the first lock
    * - Descriptors shared via ``dup()`` or ``fork()``
      - share one lock; it is released when the last of them closes
      - a ``fork()`` child does not inherit the parent's locks
    * - Closing an unrelated descriptor for the same file
      - harmless
      - drops all of that process's locks on the file
    * - Byte ranges
      - not supported; always the whole file
      - supported (portalocker does not use them)
    * - Upgrading shared to exclusive
      - not atomic; the old lock is dropped first, so another waiter can
        slip in
      - atomic

The first two rows matter most in practice. portalocker's `Lock` opens
its own filehandle, so with the default ``flock`` two `Lock` objects on
the same path *in the same process* still exclude each other. Switch to
``lockf`` and that in-process exclusion silently disappears, because the
kernel considers both locks to belong to the same owner.

On Linux the two mechanisms are independent: a ``flock`` lock does not
block a ``lockf`` lock on the same file, or the reverse. Other systems
may implement one in terms of the other, so mixing them is not portable.
The practical rule is that every process sharing a file has to use the
same primitive — which is the usual reason to switch portalocker away
from its default: some other program already picked ``lockf``.

Selecting the primitive
~~~~~~~~~~~~~~~~~~~~~~~~

The module-level ``LOCKER`` in `portalocker.portalocker` decides what the
default dispatch uses. It lives on the submodule, not on the top-level
package:

.. code-block:: python

    import fcntl

    import portalocker.portalocker

    portalocker.portalocker.LOCKER = fcntl.lockf

``LOCKER`` accepts four forms. On POSIX:

* a bare ``fcntl``-style callable taking ``(fd, operation)`` — the
  default, ``fcntl.flock``. It is routed through a shared `PosixLocker`,
  so it still gets descriptor extraction, flag validation and the
  translation of ``OSError`` into ``AlreadyLocked``/``LockException``;
* a `BaseLocker` subclass, instantiated once and cached;
* a `BaseLocker` instance;
* a ``(lock, unlock)`` tuple of two callables.

On Windows the same forms are accepted except the bare callable, which
has no meaning without ``fcntl`` and raises ``TypeError``. There
``LOCKER`` defaults to the `MsvcrtLocker` class.

Honouring all four forms in the module-level ``lock()``/``unlock()`` on
POSIX is a 4.0.0 fix; earlier versions only honoured the bare callable
there.

The class form is the tidier way to pin a primitive, because
`FlockLocker` and `LockfLocker` bind their syscall at class level and use
it regardless of what ``LOCKER`` happens to be:

.. code-block:: python

    import portalocker.portalocker
    from portalocker.portalocker import LockfLocker

    portalocker.portalocker.LOCKER = LockfLocker

    # Or use one directly, leaving the global default alone:
    locker = LockfLocker()
    with open('data.txt', 'w') as fh:
        locker.lock(fh, portalocker.LockFlags.EXCLUSIVE)
        locker.unlock(fh)

That the subclasses honour their own callable is also a 4.0.0 fix: they
previously fell through to the global ``LOCKER``, so a `LockfLocker`
quietly called ``flock``. A plain `PosixLocker`, by contrast, still
follows ``LOCKER`` deliberately, re-reading it on every access, so
reassigning ``LOCKER`` redirects instances that already exist.

Plugging in your own locker works on every platform, since `BaseLocker`
is platform-independent:

>>> import portalocker
>>> from portalocker.portalocker import BaseLocker
>>> class RecordingLocker(BaseLocker):
...     def lock(self, file_obj, flags):
...         print('lock', portalocker.LockFlags(flags).name)
...     def unlock(self, file_obj):
...         print('unlock')
>>> previous = portalocker.portalocker.LOCKER
>>> portalocker.portalocker.LOCKER = RecordingLocker
>>> try:
...     with open('example.txt', 'w') as fh:
...         portalocker.lock(fh, portalocker.LockFlags.EXCLUSIVE)
...         portalocker.unlock(fh)
... finally:
...     portalocker.portalocker.LOCKER = previous
lock EXCLUSIVE
unlock

One POSIX-only validation is worth knowing about: `LockFlags.NON_BLOCKING`
only says *how* to wait, so passing it on its own raises ``RuntimeError``
there. Combine it with `LockFlags.SHARED` or `LockFlags.EXCLUSIVE`.

Windows: ``msvcrt`` versus ``pywin32``
---------------------------------------

Windows has two locking APIs, and portalocker's default uses the one that
costs nothing to install.

`MsvcrtLocker` is the default. It calls ``msvcrt.locking`` from the
standard library, so **exclusive** locks work on a bare
``pip install portalocker`` with no extra dependencies.

``msvcrt.locking`` has no shared mode at all. `LockFlags.SHARED`
therefore delegates to `Win32Locker`, which calls ``LockFileEx`` from
``pywin32``. Since 4.0.0 ``pywin32`` is no longer installed with
portalocker on Windows, so a shared lock without it raises a descriptive
``ImportError`` rather than failing obscurely:

.. code-block:: text

    ImportError: Shared locks on Windows require the win32 extra
    (pywin32); msvcrt provides no true shared lock. Install it with:
    pip install "portalocker[win32]"

The fix is the ``win32`` extra:

.. code-block:: console

    pip install "portalocker[win32]"

`Win32Locker` can also be used directly, for instance to route every lock
through ``LockFileEx``. It always needs ``pywin32``: without it, merely
constructing the class raises ``ImportError``.

Two more consequences of the split:

* `MsvcrtLocker` builds its `Win32Locker` eagerly and remembers whether
  that succeeded. Besides shared locks, the second path that needs it is
  ``unlock()``: an ``EACCES`` from ``msvcrt.locking`` is retried through
  ``UnlockFileEx``, which is how a lock taken by the shared path gets
  released. Without ``pywin32`` there is nothing to retry with, so the
  original msvcrt failure surfaces as a ``LockException`` whose message
  names the missing extra.
* Because Windows locking is mandatory (see above), a shared lock is not
  merely an optimisation there — without one, readers that would happily
  coexist on POSIX are locked out.

Networked filesystems
---------------------

Locking over a network filesystem is best-effort. It depends on the
protocol, the server, the client implementation and the mount options,
and none of that is visible from Python. Prefer a lock that does not
depend on the filesystem — `portalocker.RedisLock`, see :doc:`redis` —
when correctness across machines actually matters.

If you must lock on a network mount:

**NFS.** On Linux, ``flock()`` on an NFS file has been emulated with
whole-file POSIX byte-range locks since kernel 2.6.12, so it does reach
the server; older kernels kept it client-local. NFSv2 and NFSv3 carry
locking in a separate protocol that needs ``rpc.statd``/``lockd``
running on both ends, while NFSv4 carries it in the main protocol. The
``local_lock`` mount option turns locking back into a client-local
operation, which silently removes all cross-client exclusion — check it
before trusting a mount. After a server or client restart there is a
recovery grace period during which locks may be lost. Some NFS setups
also make ``fcntl`` raise ``EOFError``; portalocker translates that into
``LockException`` so it is at least catchable alongside every other lock
failure.

**SMB/CIFS.** Byte-range locks are supported by the Linux ``cifs``
client and enforced by the server, but the ``nobrl`` mount option — used
to make some database files usable — stops the client from sending them,
again leaving locking client-local. Windows clients talking to a Windows
or Samba server get the usual mandatory semantics.

Flush before you release
~~~~~~~~~~~~~~~~~~~~~~~~~

A lock serialises *access*. It says nothing about when your bytes become
visible to the next holder, and on a network filesystem two layers of
buffering sit in between:

#. Python's own buffer, which ``fh.write()`` may not have left at all.
   ``fh.flush()`` pushes it down to the operating system.
#. The client's page cache, which the OS may not have sent to the
   server. `os.fsync` on the descriptor forces that.

NFS's close-to-open consistency means a reader is only guaranteed to see
your data after you closed the file and it opened the file afresh.
`portalocker.Lock` does close its handle on release, which usually
covers it, but nothing forces a flush if you hold the handle open across
the unlock — with `portalocker.RLock`, or by calling `portalocker.lock`
and `portalocker.unlock` on a handle you manage yourself. Doing both
explicitly costs one line and removes the question:

>>> import os
>>> import portalocker
>>> with portalocker.Lock('shared.txt', 'w', timeout=5) as fh:
...     _ = fh.write('visible to the next holder')
...     fh.flush()
...     os.fsync(fh.fileno())

Byte ranges versus whole files
-------------------------------

portalocker locks whole files. There is no public API for locking a byte
range, and the ranges the implementations use internally are all
anchored at byte 0 so that any two holders of the same file always
contend:

* **POSIX.** ``fcntl.flock`` has no concept of a range. portalocker calls
  its locker with ``(fd, operation)`` only, so when ``fcntl.lockf`` is
  selected instead, that function's defaults apply — ``start=0``,
  ``whence=SEEK_SET`` and ``len=0``, which means "to the end of the
  file", including bytes appended later.
* **Windows, msvcrt.** ``msvcrt.locking`` locks a fixed number of bytes
  *starting at the current file position*. portalocker locks 64 KiB
  (``0x10000``) and seeks to byte 0 first, restoring the previous
  position afterwards.
* **Windows, Win32.** ``LockFileEx`` takes its offset from an
  ``OVERLAPPED`` structure, which portalocker leaves at 0, and locks a
  fixed large range from there.

Seeking to byte 0 on the msvcrt path is a 4.0.0 fix. Before it, raw file
descriptors — an ``int``, or an object exposing only ``fileno()`` —
were locked from wherever the descriptor happened to be positioned. Two
processes working at different offsets in a file larger than 64 KiB
would then lock disjoint ranges and fail to exclude each other at all,
with no error to show for it. Passing a real file object was unaffected,
because its position was already normalised.

The practical consequences:

* Two locks on the same file always contend, whatever either side had
  seeked to. This is portable behaviour you can rely on:

  >>> import portalocker
  >>> first = portalocker.Lock('example.lock', 'a', fail_when_locked=True)
  >>> _ = first.acquire()
  >>> second = portalocker.Lock('example.lock', 'a', fail_when_locked=True)
  >>> try:
  ...     _ = second.acquire()
  ... except portalocker.AlreadyLocked:
  ...     print('contended, as expected')
  contended, as expected
  >>> first.release()

* You cannot use portalocker to let two processes work on different
  regions of one large file concurrently. If you need that, call
  ``fcntl.lockf`` with explicit ``start``/``len`` arguments yourself, and
  accept that it is POSIX-only.
* Treat the lock as a token that names the file, not as something that
  protects a particular range of bytes.
