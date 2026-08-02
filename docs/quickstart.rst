Quickstart
==========

This page gets you from an empty environment to a working file lock. For
choosing between the different lock classes, see :doc:`lock-types`. For
platform-specific behaviour (advisory vs. mandatory locking, ``msvcrt`` vs.
``pywin32``, NFS caveats), see :doc:`platforms`.

Installation
------------

.. code-block:: console

    pip install portalocker

Two optional extras pull in dependencies only some users need:

.. code-block:: console

    pip install "portalocker[redis]"
    pip install "portalocker[win32]"

Install ``redis`` when you use `portalocker.RedisLock` for cross-machine
locking. Install ``win32`` on Windows when you need a shared
(``LockFlags.SHARED``) lock; exclusive locks work on Windows without it.

Your first lock
----------------

`portalocker.Lock` opens a file, locks it, and hands you the filehandle:

>>> import portalocker
>>> with portalocker.Lock('example.lock', 'w', timeout=1) as fh:
...     _ = fh.write('hello, portalocker')

The lock is released automatically when the ``with`` block exits. If another
process is already holding it, `portalocker.Lock` retries until ``timeout``
seconds pass, then raises ``AlreadyLocked``.

Reading and writing under a lock
---------------------------------

The same class works for reading; open in a read mode instead:

>>> with portalocker.Lock('example.lock', 'r', timeout=1) as fh:
...     print(fh.read())
hello, portalocker

Use a mode with both ``+`` to read and write through the same lock:

>>> with portalocker.Lock('example.lock', 'r+', timeout=1) as fh:
...     data = fh.read()
...     _ = fh.seek(0)
...     _ = fh.write(data.upper())
>>> with portalocker.Lock('example.lock', 'r', timeout=1) as fh:
...     print(fh.read())
HELLO, PORTALOCKER

Flushing on networked filesystems
-----------------------------------

On some networked filesystems, another client may not see your writes until
they are forced out of buffers. Call ``flush()`` and `os.fsync` before the
lock is released:

>>> import os
>>> with portalocker.Lock('some_file', 'w+', timeout=60) as fh:
...     _ = fh.write('some data')
...     fh.flush()
...     os.fsync(fh.fileno())
