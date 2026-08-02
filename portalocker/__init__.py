"""Cross-platform file locking, with optional Redis-backed distributed locks.

This is the public entry point of the ``portalocker`` package. Most of what
you need lives directly on this module:

- `Lock` and `RLock` open a file and hold an OS-level advisory lock on it
  for the lifetime of the ``with`` block; `RLock` additionally allows the
  same thread to re-enter the lock.
- `BoundedSemaphore` and `NamedBoundedSemaphore` cap the number of
  processes that may hold a lock concurrently, using a directory of lock
  files rather than a single one.
- `PidFileLock` writes the current process ID into the lock file, so a
  stale lock left behind by a crashed process can be recognised.
- `TemporaryFileLock` is a short-lived variant of `Lock` intended for
  small, throwaway critical sections.
- `RedisLock` is a distributed lock built on Redis pubsub, for
  coordinating processes that do not share a filesystem. It requires the
  optional ``redis`` dependency; when that package is not installed,
  `RedisLock` is still importable as ``None`` so that
  ``import portalocker`` never fails, but constructing it raises at that
  point rather than at import time. Install it with
  ``pip install "portalocker[redis]"``.
- `lock` and `unlock` are the low-level, platform-specific primitives that
  the classes above are built on; reach for them only if the context
  managers do not fit your use case.

Example:
    >>> import portalocker
    >>> with portalocker.Lock('somefile', timeout=1) as fh:
    ...     _ = fh.write('writing some stuff to my cache')
"""

from . import __about__, constants, exceptions, portalocker
from .utils import (
    BoundedSemaphore,
    Lock,
    NamedBoundedSemaphore,
    PidFileLock,
    RLock,
    TemporaryFileLock,
    open_atomic,
)

try:
    from .redis import RedisLock
except ImportError:  # pragma: no cover
    # `redis` is an optional dependency; keep the attribute importable so
    # `portalocker.RedisLock` fails at use time, not import time.
    RedisLock = None  # type: ignore[assignment,misc]  # ty: ignore[invalid-assignment]


#: The package name on Pypi
__package_name__ = __about__.__package_name__
#: Current author and maintainer, view the git history for the previous ones
__author__ = __about__.__author__
#: Current author's email address
__email__ = __about__.__email__
#: Version number
__version__ = __about__.__version__
#: Package description for Pypi
__description__ = __about__.__description__
#: Package homepage
__url__ = __about__.__url__


#: Exception thrown when the file is already locked by someone else
AlreadyLocked = exceptions.AlreadyLocked
#: Exception thrown if an error occurred during locking
LockException = exceptions.LockException


#: Lock a file. Note that this is an advisory lock on Linux/Unix systems
lock = portalocker.lock
#: Unlock a file
unlock = portalocker.unlock

#: Place an exclusive lock.
#: Only one process may hold an exclusive lock for a given file at a given
#: time.
LOCK_EX: constants.LockFlags = constants.LockFlags.EXCLUSIVE

#: Place a shared lock.
#: More than one process may hold a shared lock for a given file at a given
#: time.
LOCK_SH: constants.LockFlags = constants.LockFlags.SHARED

#: Acquire the lock in a non-blocking fashion.
LOCK_NB: constants.LockFlags = constants.LockFlags.NON_BLOCKING

#: Remove an existing lock held by this process.
LOCK_UN: constants.LockFlags = constants.LockFlags.UNBLOCK

#: Locking flags enum
LockFlags = constants.LockFlags

#: Locking utility class to automatically handle opening with timeouts and
#: context wrappers

__all__ = [
    'LOCK_EX',
    'LOCK_NB',
    'LOCK_SH',
    'LOCK_UN',
    'AlreadyLocked',
    'BoundedSemaphore',
    'Lock',
    'LockException',
    'LockFlags',
    'NamedBoundedSemaphore',
    'PidFileLock',
    'RLock',
    'RedisLock',
    'TemporaryFileLock',
    'lock',
    'open_atomic',
    'unlock',
]
