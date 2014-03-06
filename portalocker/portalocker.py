#!/usr/bin/env python
#
# portalocker.py - Cross-platform (posix/nt) API for flock-style file locking.
#
# http://code.activestate.com/recipes/65203-portalocker-cross-platform-posixnt-api-for-flock-s/
#
"""
Cross-platform (posix/nt) API for flock-style file locking.

Synopsis::

   import portalocker
   file = open("somefile", "r+")
   portalocker.lock(file, portalocker.LOCK_EX)
   file.seek(12)
   file.write("foo")
   file.close()

If you know what you're doing, you may choose to::

   portalocker.unlock(file)

before closing the file, but why?

Methods::

   lock( file, flags )
   unlock( file )

Constants::

   LOCK_EX
   LOCK_SH
   LOCK_NB

I learned the win32 technique for locking files from sample code
provided by John Nielsen <nielsenjf@my-deja.com> in the documentation
that accompanies the win32 modules.

:Author: Jonathan Feinberg <jdf@pobox.com>

Roundup Changes
---------------
2012-11-28 (anatoly techtonik)
   - Ported to ctypes
   - Dropped support for Win95, Win98 and WinME
   - Added return result
"""

__docformat__ = 'restructuredtext'

import os

if os.name == 'nt':
    import msvcrt
    from ctypes import *
    from ctypes.wintypes import BOOL, DWORD, HANDLE

    LOCK_SH = 0    # the default
    LOCK_NB = 0x1  # LOCKFILE_FAIL_IMMEDIATELY
    LOCK_EX = 0x2  # LOCKFILE_EXCLUSIVE_LOCK

    # --- the code is taken from pyserial project ---
    #
    # detect size of ULONG_PTR 
    def is_64bit():
        return sizeof(c_ulong) != sizeof(c_void_p)
    if is_64bit():
        ULONG_PTR = c_int64
    else:
        ULONG_PTR = c_ulong
    PVOID = c_void_p

    # --- Union inside Structure by stackoverflow:3480240 ---
    class _OFFSET(Structure):
        _fields_ = [
            ('Offset', DWORD),
            ('OffsetHigh', DWORD)]

    class _OFFSET_UNION(Union):
        _anonymous_ = ['_offset']
        _fields_ = [
            ('_offset', _OFFSET),
            ('Pointer', PVOID)]

    class OVERLAPPED(Structure):
        _anonymous_ = ['_offset_union']
        _fields_ = [
            ('Internal', ULONG_PTR),
            ('InternalHigh', ULONG_PTR),
            ('_offset_union', _OFFSET_UNION),
            ('hEvent', HANDLE)]

    LPOVERLAPPED = POINTER(OVERLAPPED)

    # --- Define function prototypes for extra safety ---
    LockFileEx = windll.kernel32.LockFileEx
    LockFileEx.restype = BOOL
    LockFileEx.argtypes = [HANDLE, DWORD, DWORD, DWORD, DWORD, LPOVERLAPPED]
    UnlockFileEx = windll.kernel32.UnlockFileEx
    UnlockFileEx.restype = BOOL
    UnlockFileEx.argtypes = [HANDLE, DWORD, DWORD, DWORD, LPOVERLAPPED]
            
elif os.name == 'posix':
    import fcntl
    LOCK_SH = fcntl.LOCK_SH  # shared lock
    LOCK_NB = fcntl.LOCK_NB  # non-blocking
    LOCK_EX = fcntl.LOCK_EX
else:
    raise RuntimeError("PortaLocker only defined for nt and posix platforms")

if os.name == 'nt':
    def lock(file, flags):
        """ Return True on success, False otherwise """
        hfile = msvcrt.get_osfhandle(file.fileno())
        overlapped = OVERLAPPED()
        if LockFileEx(hfile, flags, 0, 0, 0xFFFF0000, byref(overlapped)):
            return True
        else:
            return False

    def unlock(file):
        hfile = msvcrt.get_osfhandle(file.fileno())
        overlapped = OVERLAPPED()
        if UnlockFileEx(hfile, 0, 0, 0xFFFF0000, byref(overlapped)):
            return True
        else:
            return False

elif os.name =='posix':
    def lock(file, flags):
        if fcntl.flock(file.fileno(), flags) == 0:
            return True
        else:
            return False

    def unlock(file):
        if fcntl.flock(file.fileno(), fcntl.LOCK_UN) == 0:
            return True
        else:
            return False

if __name__ == '__main__':
    from time import time, strftime, localtime
    import sys

    log = open('log.txt', "a+")
    lock(log, LOCK_EX)

    timestamp = strftime("%m/%d/%Y %H:%M:%S\n", localtime(time()))
    log.write( timestamp )

    print "Wrote lines. Hit enter to release lock."
    dummy = sys.stdin.readline()

    log.close()

