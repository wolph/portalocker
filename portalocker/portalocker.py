# portalocker.py - Cross-platform (posix/nt) API for flock-style file locking.
#                  Requires python 1.5.2 or better.
'''Cross-platform (posix/nt) API for flock-style file locking.

Synopsis:

   import portalocker
   file = open('somefile', 'r+')
   portalocker.lock(file, portalocker.LOCK_EX)
   file.seek(12)
   file.write('foo')
   file.close()

If you know what you're doing, you may choose to

   portalocker.unlock(file)

before closing the file, but why?

Methods:

   lock( file, flags )
   unlock( file )

Constants:

   LOCK_EX
   LOCK_SH
   LOCK_NB

Exceptions:

    LockException

Notes:

For the 'nt' platform, this module requires the Python Extensions for Windows.
Be aware that this may not work as expected on Windows 95/98/ME.

History:

I learned the win32 technique for locking files from sample code
provided by John Nielsen <nielsenjf@my-deja.com> in the documentation
that accompanies the win32 modules.

Author: Jonathan Feinberg <jdf@pobox.com>,
        Lowell Alleman <lalleman@mfps.com>
Version: $Id: portalocker.py 5474 2008-05-16 20:53:50Z lowell $

'''

import os


__all__ = [
    'lock',
    'unlock',
    'LOCK_EX',
    'LOCK_SH',
    'LOCK_NB',
    'LockException',
]


class LockException(Exception):
    # Error codes:
    LOCK_FAILED = 1

if os.name == 'nt':  # pragma: no cover
    import msvcrt
    LOCK_EX = 0x1      # exclusive - msvcrt.LK_LOCK or msvcrt.LK_NBLCK
    LOCK_SH = 0x2      # shared    - msvcrt.LK_RLOCK or msvcrt.LK_NBRLCK
    LOCK_NB = 0x4      # 
elif os.name == 'posix':
    import fcntl
    LOCK_EX = fcntl.LOCK_EX
    LOCK_SH = fcntl.LOCK_SH
    LOCK_NB = fcntl.LOCK_NB
else:  # pragma: no cover
    raise RuntimeError('PortaLocker only defined for nt and posix platforms')


def nt_lock(file_, flags):  # pragma: no cover
    if flags & LOCK_SH:
        mode = msvcrt.LK_NBRLCK if (flags & LOCK_NB) else msvcrt.LK_RLOCK
    else:
        mode = msvcrt.LK_NBLCK if (flags & LOCK_NB) else msvcrt.LK_LOCK

    # windows locks byte ranges, so make sure to lock from file start
    try:
        savepos = file_.tell()
        if savepos:
            # [ ] test exclusive lock fails on seek here
            # [ ] test if shared lock passes this point
            file_.seek(0)
        # [x] check if 0 param locks entire file (not documented in Python)
	#   [x] just fails with "IOError: [Errno 13] Permission denied",
        #        but -1 seems to do the trick
        try:
            msvcrt.locking(file_.fileno(), mode, -1)
        except IOError as exc_value:
            # [ ] be more specific here
            raise LockException(LockException.LOCK_FAILED, exc_value.strerror)
        finally:
            if savepos:
                file_.seek(savepos)
    except IOError as exc_value:
        raise LockException(LockException.LOCK_FAILED, exc_value.strerror)


def nt_unlock(file_):  # pragma: no cover
    try:
        savepos = file_.tell()
        if savepos:
            file_.seek(0)
        try:
            msvcrt.locking(file_.fileno(), msvcrt.LK_UNLCK, -1)
        except IOError as exc_value:
            raise LockException(LockException.LOCK_FAILED, exc_value.strerror)
        finally:
            if savepos:
                file_.seek(savepos)
    except IOError as exc_value:
        raise LockException(LockException.LOCK_FAILED, exc_value.strerror)


def posix_lock(file_, flags):
    try:
        fcntl.flock(file_.fileno(), flags)
    except IOError as exc_value:
        # The exception code varies on different systems so we'll catch
        # every IO error
        raise LockException(exc_value)


def posix_unlock(file_):
    fcntl.flock(file_.fileno(), fcntl.LOCK_UN)

if os.name == 'nt':  # pragma: no cover
    lock = nt_lock
    unlock = nt_unlock
elif os.name == 'posix':
    lock = posix_lock
    unlock = posix_unlock
else:  # pragma: no cover
    raise RuntimeError('Your os %r is unsupported.' % os.name)
