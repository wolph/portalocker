############################################
portalocker - Cross-platform locking library
############################################

.. image:: https://github.com/wolph/portalocker/actions/workflows/ci.yml/badge.svg?branch=develop
    :alt: CI Test Status
    :target: https://github.com/wolph/portalocker/actions/workflows/ci.yml

Overview
--------

Portalocker is a library to provide an easy API to file locking.

Portalocker requires Python 3.10 or later.

An important detail to note is that on Linux and Unix systems the locks are
advisory by default. By specifying the `-o mand` option to the mount command it
is possible to enable mandatory file locking on Linux. This is generally not
recommended however. For more information about the subject:

 - https://en.wikipedia.org/wiki/File_locking
 - http://stackoverflow.com/questions/39292051/portalocker-does-not-seem-to-lock
 - https://stackoverflow.com/questions/12062466/mandatory-file-lock-on-linux

Windows and the ``pywin32`` dependency
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Since version 4.0.0, ``pywin32`` is no longer installed automatically on
Windows. Exclusive locks work out of the box without any extra
dependencies using the built-in `msvcrt` module. Shared locks
(``LockFlags.SHARED``) on Windows require the optional `pywin32`
dependency, which can be installed through the ``win32`` extra:

::

    pip install "portalocker[win32]"

Attempting to acquire a shared lock on Windows without `pywin32` raises an
``ImportError`` explaining this requirement.

The module is currently maintained by Rick van Hattem <Wolph@wol.ph>.
The project resides at https://github.com/WoLpH/portalocker . Bugs and feature
requests can be submitted there. Patches are also very welcome.

Security contact information
------------------------------------------------------------------------------

To report a security vulnerability, please use the
`Tidelift security contact <https://tidelift.com/security>`_.
Tidelift will coordinate the fix and disclosure.

PidFileLock contexts
--------------------

The default context is inspection-oriented: it enters even when another
process owns the lock and returns that process's PID. A ``None`` value means
this process acquired the lock:

.. code-block:: python

    import portalocker

    with portalocker.PidFileLock('worker.pid') as holder_pid:
        if holder_pid is None:
            run_singleton_worker()
        else:
            print(f'worker already running as PID {holder_pid}')

Use ``fail_closed()`` when the protected body must only run after acquisition:

.. code-block:: python

    import portalocker

    try:
        with portalocker.PidFileLock('worker.pid').fail_closed():
            run_singleton_worker()
    except portalocker.AlreadyLocked as exc:
        print(f'worker already running as PID {exc.holder_pid}')

Redis Locks
-----------

This library now features a lock based on Redis which allows for locks across
multiple threads, processes and even distributed locks across multiple
computers.

It is an extremely reliable Redis lock that is based on pubsub.

As opposed to most Redis locking systems based on key/value pairs,
this locking method is based on the pubsub system. The big advantage is
that if the connection gets killed due to network issues, crashing
processes or otherwise, it will still immediately unlock instead of
waiting for a lock timeout.

First make sure you have everything installed correctly:

::

    pip install "portalocker[redis]"

Usage is really easy:

::

    import portalocker

    lock = portalocker.RedisLock('some_lock_channel_name')

    with lock:
        print('do something here')

Shared locks allow multiple readers while remaining mutually exclusive with
writers:

::

    read_lock = portalocker.RedisLock(
        'some_lock_channel_name',
        flags=portalocker.LockFlags.SHARED,
    )

    with read_lock:
        print('read shared state')

Waiting writers prevent new readers from entering, so a continuous stream of
readers cannot starve an exclusive lock. New shared locks also interoperate with
older portalocker clients: legacy Redis locks are treated as exclusive.

The API is essentially identical to the other ``Lock`` classes so in addition
to the ``with`` statement you can also use ``lock.acquire(...)``.

The normal test suite uses ``fakeredis``. To optionally repeat the Redis tests
against a locally running server:

::

    redis-server
    tox -e redis-live

Set ``REDIS_HOST`` or ``REDIS_PORT`` when the server does not use
``localhost:6379``. The ``redis-live`` environment fails instead of skipping
when it cannot connect.

Python 2
--------

Python 2 was supported in versions before Portalocker 2.0. If you are still
using
Python 2,
you can run this to install:

::

    pip install "portalocker<2"

Tips
----

On some networked filesystems it might be needed to force a `os.fsync()` before
closing the file so it's actually written before another client reads the file.
Effectively this comes down to:

::

   with portalocker.Lock('some_file', 'rb+', timeout=60) as fh:
       # do what you need to do
       ...

       # flush and sync to filesystem
       fh.flush()
       os.fsync(fh.fileno())

Links
-----

* Documentation
    - http://portalocker.readthedocs.org/en/latest/
* Source
    - https://github.com/WoLpH/portalocker
* Bug reports
    - https://github.com/WoLpH/portalocker/issues
* Package homepage
    - https://pypi.python.org/pypi/portalocker
* My blog
    - http://w.wol.ph/

Examples
--------

To make sure your cache generation scripts don't race, use the `Lock` class:

>>> import portalocker
>>> with portalocker.Lock('somefile', timeout=1) as fh:
...     print('writing some stuff to my cache...', file=fh)

To customize the opening and locking a manual approach is also possible:

>>> import portalocker
>>> file = open('somefile', 'r+')
>>> portalocker.lock(file, portalocker.LockFlags.EXCLUSIVE)
>>> file.seek(12)
>>> file.write('foo')
>>> file.close()

Explicitly unlocking is not needed in most cases but omitting it has been known
to cause issues:
https://github.com/AzureAD/microsoft-authentication-extensions-for-python/issues/42#issuecomment-601108266

If needed, it can be done through:

>>> portalocker.unlock(file)

Do note that your data might still be in a buffer so it is possible that your
data is not available until you `flush()` or `close()`.

To create a cross platform bounded semaphore across multiple processes you can
use the `BoundedSemaphore` class which functions somewhat similar to
`threading.BoundedSemaphore`:

>>> import portalocker
>>> n = 2
>>> timeout = 0.1

>>> semaphore_a = portalocker.BoundedSemaphore(n, timeout=timeout)
>>> semaphore_b = portalocker.BoundedSemaphore(n, timeout=timeout)
>>> semaphore_c = portalocker.BoundedSemaphore(n, timeout=timeout)

>>> semaphore_a.acquire()
<portalocker.utils.Lock object at ...>
>>> semaphore_b.acquire()
<portalocker.utils.Lock object at ...>
>>> semaphore_c.acquire()
Traceback (most recent call last):
  ...
portalocker.exceptions.AlreadyLocked


More examples can be found in the
`tests <http://portalocker.readthedocs.io/en/latest/_modules/tests/tests.html>`_.


Versioning
----------

This library follows `Semantic Versioning <http://semver.org/>`_.


Changelog
---------

Every release has a ``git tag`` with a commit message for the tag
explaining what was added and/or changed. The list of tags/releases
including the commit messages can be found here:
https://github.com/WoLpH/portalocker/releases

License
-------

See the `LICENSE <https://github.com/WoLpH/portalocker/blob/develop/LICENSE>`_ file.
