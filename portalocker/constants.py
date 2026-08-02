"""Locking constants.

Lock types:

- `EXCLUSIVE` exclusive lock
- `SHARED` shared lock. Note: on Windows this requires the optional
  ``pywin32`` package (``pip install "portalocker[win32]"``); without it,
  acquiring a shared lock raises ``ImportError``.

Lock flags:

- `NON_BLOCKING` non-blocking

Manually unlock, only needed internally

- `UNBLOCK` unlock
"""
# The platform-specific `if os.name == ...` branches each assign the module
# constants exactly once, but pyright analyzes all branches and reports the
# assignments as redefinitions.
# pyright: reportConstantRedefinition=false

import enum
import os

# The actual tests will execute the code anyhow so the following code can
# safely be ignored from the coverage tests
if os.name == 'nt':  # pragma: no cover
    import msvcrt

    #: exclusive lock
    LOCK_EX = 0x1
    #: shared lock
    LOCK_SH = 0x2
    #: non-blocking
    LOCK_NB = 0x4
    #: unlock
    LOCK_UN = msvcrt.LK_UNLCK  # type: ignore[attr-defined]

elif os.name == 'posix':  # pragma: no cover
    import fcntl

    #: exclusive lock
    LOCK_EX = fcntl.LOCK_EX
    #: shared lock
    LOCK_SH = fcntl.LOCK_SH
    #: non-blocking
    LOCK_NB = fcntl.LOCK_NB
    #: unlock
    LOCK_UN = fcntl.LOCK_UN

else:  # pragma: no cover
    raise RuntimeError('PortaLocker only defined for nt and posix platforms')


class LockFlags(enum.IntFlag):
    """Flags selecting how `portalocker.lock` acquires a file lock.

    Members combine with the bitwise ``|`` operator to build up the
    behavior you want. The library's own default, used when no `flags`
    argument is given, is ``LockFlags.EXCLUSIVE | LockFlags.NON_BLOCKING``:
    take an exclusive lock and fail immediately rather than wait.

    On POSIX platforms these map onto ``fcntl`` locks, which are
    advisory: the OS only enforces them against other processes that
    also lock the file through ``fcntl``/``flock``. A process that opens
    and writes the file without locking it is never blocked. On Windows
    they map onto ``msvcrt`` locks, except `SHARED`, which requires the
    optional ``pywin32`` package (``pip install "portalocker[win32]"``);
    without it, acquiring a shared lock raises `ImportError`.

    Example:
        >>> import portalocker
        >>> flags = (
        ...     portalocker.LockFlags.EXCLUSIVE
        ...     | portalocker.LockFlags.NON_BLOCKING
        ... )
        >>> bool(flags & portalocker.LockFlags.NON_BLOCKING)
        True
    """

    #: Request an exclusive lock. Only one process may hold `EXCLUSIVE`
    #: (or `SHARED`) on a given file at the same time; other processes
    #: attempting to lock it either block or fail, depending on whether
    #: `NON_BLOCKING` is also set.
    EXCLUSIVE = LOCK_EX
    #: Request a shared lock. Multiple processes may hold `SHARED` locks
    #: on the same file concurrently, but none may hold `EXCLUSIVE` while
    #: any `SHARED` lock is held. On Windows this requires the optional
    #: ``win32`` extra (``pip install "portalocker[win32]"``); without
    #: it, acquiring a shared lock raises `ImportError`.
    SHARED = LOCK_SH
    #: Don't wait for the lock: fail with `AlreadyLocked` immediately if
    #: it can't be acquired right away, instead of blocking until it
    #: becomes available.
    NON_BLOCKING = LOCK_NB
    #: Release a lock previously acquired on the same file. Used
    #: internally by `portalocker.unlock`; most callers use a context
    #: manager (`Lock`/`RLock`) instead of applying this flag directly.
    UNBLOCK = LOCK_UN
