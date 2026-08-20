"""Lifecycle semantics of the lock wrapper objects.

Garbage collection of a lock wrapper must never tear down a held lock.
Portalocker 4.0.0 introduced a ``LockBase.__del__`` that released the OS
lock, closed the filehandle and (for `TemporaryFileLock`) unlinked the
lock file the moment the wrapper was collected, so the throwaway idiom
``fh = Lock(path).acquire()`` lost mutual exclusion instantly. These
tests pin the 3.2.0 semantics: collection of the wrapper is a no-op,
cleanup at interpreter exit still works through ``atexit``, and the
descriptor protocol releases the lock itself rather than the owner.
"""

from __future__ import annotations

import gc
import os
import pathlib
import subprocess
import sys
import typing

import pytest

import portalocker
from portalocker import utils

#: Exit code the contender subprocess uses to signal `AlreadyLocked`.
_CONTENDER_BLOCKED: int = 3

#: Tries to take the lock named by ``argv[1]`` from a separate process.
_CONTENDER_CODE: str = """
import sys

import portalocker

try:
    portalocker.Lock(sys.argv[1], timeout=0, fail_when_locked=True).acquire()
except portalocker.AlreadyLocked:
    sys.exit(3)
sys.exit(0)
"""

#: Acquires a `TemporaryFileLock` on ``argv[1]`` and exits holding it,
#: keeping the wrapper referenced so the ``atexit`` handler can run.
_ATEXIT_HOLDER_CODE: str = """
import os
import sys

import portalocker

lock = portalocker.TemporaryFileLock(sys.argv[1], timeout=0)
lock.acquire()
print(int(os.path.isfile(sys.argv[1])))
"""


def _contend_from_subprocess(path: str) -> int:
    """Run the contender subprocess and return its exit code."""
    result: subprocess.CompletedProcess[bytes] = subprocess.run(
        [sys.executable, '-c', _CONTENDER_CODE, path],
        check=False,
        timeout=15,
    )
    return result.returncode


def test_gc_of_lock_wrapper_keeps_lock_held(tmpfile: str) -> None:
    """The throwaway idiom must keep the lock after the wrapper is GCed."""
    fh: typing.IO[typing.Any] = portalocker.Lock(tmpfile, timeout=0).acquire()
    gc.collect()

    assert not fh.closed, 'GC of the lock wrapper closed the filehandle'
    fh.write('still open after gc')
    fh.flush()

    # An external contender must still be locked out.
    assert _contend_from_subprocess(tmpfile) == _CONTENDER_BLOCKED, (
        'GC of the lock wrapper released the OS lock'
    )
    fh.close()


def test_del_of_held_lock_wrapper_is_a_noop(tmpfile: str) -> None:
    """Explicitly deleting a held lock wrapper must not release it."""
    lock: portalocker.Lock = portalocker.Lock(tmpfile, timeout=0)
    fh: typing.IO[typing.Any] = lock.acquire()
    del lock
    gc.collect()

    assert not fh.closed, 'GC of the lock wrapper closed the filehandle'
    fh.close()


def test_gc_of_temporary_file_lock_keeps_file_and_lock(tmpfile: str) -> None:
    """GC of a `TemporaryFileLock` wrapper must not unlink a held file."""
    fh: typing.IO[typing.Any] = portalocker.TemporaryFileLock(
        tmpfile,
        timeout=0,
    ).acquire()
    gc.collect()

    assert not fh.closed, 'GC of the lock wrapper closed the filehandle'
    assert os.path.isfile(tmpfile), 'GC of the lock wrapper unlinked the file'

    # A second locker (fresh filehandle, same process) must still lose.
    with pytest.raises(portalocker.AlreadyLocked):
        portalocker.TemporaryFileLock(tmpfile, timeout=0).acquire()

    fh.close()


def test_atexit_unlinks_held_temporary_file_lock(tmpfile: str) -> None:
    """Interpreter exit must still clean up a held `TemporaryFileLock`."""
    result: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, '-c', _ATEXIT_HOLDER_CODE, tmpfile],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '1', 'lock file was never created'
    assert not os.path.isfile(tmpfile), (
        'atexit cleanup left the lock file behind'
    )


def test_descriptor_delete_releases_class_attribute_lock(
    tmpfile: str,
) -> None:
    """``del owner.attribute`` must release the lock stored on the class."""
    shared_lock: utils.Lock = utils.Lock(tmpfile, timeout=0)

    class Owner:
        lock: typing.ClassVar[utils.Lock] = shared_lock

    owner: Owner = Owner()
    fh: typing.IO[typing.Any] = Owner.lock.acquire()

    # Runtime-legal through the descriptor's `__delete__`; pyrefly types
    # the class attribute as read-only from an instance.
    del owner.lock  # pyrefly: ignore[read-only]

    assert shared_lock.fh is None, 'descriptor deletion left the lock held'
    assert fh.closed, 'descriptor deletion left the filehandle open'

    # The lock is free again: a fresh contender acquires immediately.
    contender: portalocker.Lock = portalocker.Lock(
        tmpfile,
        timeout=0,
        fail_when_locked=True,
    )
    contender.acquire()
    contender.release()


def test_release_locks_at_exit_releases_only_owned_locks(
    tmp_path: pathlib.Path,
) -> None:
    """The ``atexit`` hook releases this process's locks and only those.

    ``_release_locks_at_exit`` normally runs while the interpreter shuts
    down, which is why it went unmeasured for a long time. It is a plain
    function, though, so this test drives it directly: a lock recorded
    under the current pid is released (its file unlinked), while a lock
    recorded under a foreign pid - the state a forked child inherits
    from its parent - is skipped and stays held.
    """
    owned_path: str = str(tmp_path / 'owned.lock')
    foreign_path: str = str(tmp_path / 'foreign.lock')

    owned = portalocker.TemporaryFileLock(owned_path, timeout=0)
    owned.acquire()
    foreign = portalocker.TemporaryFileLock(foreign_path, timeout=0)
    foreign.acquire()
    # Pretend the foreign lock was taken by the (fictional) parent.
    utils._exit_releases[foreign] = os.getpid() + 12345

    try:
        assert os.path.isfile(owned_path)
        assert os.path.isfile(foreign_path)

        utils._release_locks_at_exit()

        # The owned lock was released and its file removed. The foreign
        # lock was left alone for its owning process to release.
        assert not os.path.isfile(owned_path)
        assert owned.fh is None
        assert os.path.isfile(foreign_path)
        assert foreign.fh is not None
    finally:
        utils._exit_releases.pop(foreign, None)
        foreign.release()


def test_release_locks_at_exit_suppresses_release_errors(
    tmp_path: pathlib.Path,
) -> None:
    """A failing ``release`` cannot break the interpreter-exit sweep.

    Errors are suppressed because nobody is left to handle them at exit,
    and one broken lock must not stop the remaining locks from being
    released.
    """
    broken_path: str = str(tmp_path / 'broken.lock')
    healthy_path: str = str(tmp_path / 'healthy.lock')

    broken = portalocker.TemporaryFileLock(broken_path, timeout=0)
    broken.acquire()
    healthy = portalocker.TemporaryFileLock(healthy_path, timeout=0)
    healthy.acquire()

    def exploding_release() -> None:
        raise RuntimeError('release failed at exit')

    # Instance attribute shadows the method for this object only.
    broken.release = exploding_release  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]  # noqa: E501

    try:
        utils._release_locks_at_exit()
        # The broken lock's failure was swallowed and the healthy lock
        # was still released.
        assert not os.path.isfile(healthy_path)
        assert healthy.fh is None
    finally:
        del broken.release
        utils._exit_releases.pop(broken, None)
        broken.release()
