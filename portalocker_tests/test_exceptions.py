"""Exception contract tests: pickling, `strerror`, and unlock wrapping.

Lock exceptions cross process boundaries whenever a `multiprocessing`
worker raises one: the pool pickles the exception to ship it back to the
parent. These tests pin down that every construction shape the package
uses survives a pickle round-trip, that `strerror` is populated at the
`Lock` level as the migration docs promise, and that the POSIX
module-level `unlock` wraps `OSError` in `LockException` like Windows
does.

All pickling in this module round-trips objects the tests construct
themselves, so loading them back is safe.
"""

from __future__ import annotations

import errno
import multiprocessing
import os
import pickle

import pytest

import portalocker
from portalocker import exceptions

_EXCEPTION_CLASSES: list[type[exceptions.BaseLockException]] = [
    exceptions.BaseLockException,
    exceptions.LockException,
    exceptions.AlreadyLocked,
    exceptions.FileToLarge,
]


def _round_trip(
    exception: exceptions.BaseLockException,
) -> exceptions.BaseLockException:
    """Pickle and unpickle `exception`, asserting the type survives."""
    restored: exceptions.BaseLockException = pickle.loads(
        pickle.dumps(exception)
    )
    assert type(restored) is type(exception)
    return restored


@pytest.mark.parametrize('exception_class', _EXCEPTION_CLASSES)
def test_pickle_with_open_file_handle(
    exception_class: type[exceptions.BaseLockException],
    tmpfile: str,
) -> None:
    """An open file handle is dropped, but its name survives as a str."""
    with open(tmpfile, 'w') as fh:
        exception = exception_class(1, 'lock failed', fh=fh)
        restored = _round_trip(exception)

    assert restored.args == (1, 'lock failed')
    assert restored.strerror == 'lock failed'
    assert restored.fh is None
    assert restored.fh_name == tmpfile


@pytest.mark.parametrize('exception_class', _EXCEPTION_CLASSES)
def test_pickle_with_closed_file_handle(
    exception_class: type[exceptions.BaseLockException],
    tmpfile: str,
) -> None:
    """A closed handle is just as unpicklable and is dropped the same."""
    with open(tmpfile, 'w') as fh:
        pass
    restored = _round_trip(exception_class(1, 'lock failed', fh=fh))

    assert restored.fh is None
    assert restored.fh_name == tmpfile


@pytest.mark.parametrize('exception_class', _EXCEPTION_CLASSES)
def test_pickle_with_file_descriptor(
    exception_class: type[exceptions.BaseLockException],
    tmpfile: str,
) -> None:
    """A raw file descriptor is a plain int and is preserved as-is."""
    fd: int = os.open(tmpfile, os.O_WRONLY | os.O_CREAT)
    try:
        restored = _round_trip(exception_class(1, 'lock failed', fh=fd))
    finally:
        os.close(fd)

    assert restored.fh == fd
    assert restored.fh_name is None


@pytest.mark.parametrize('exception_class', _EXCEPTION_CLASSES)
def test_pickle_without_file_handle(
    exception_class: type[exceptions.BaseLockException],
) -> None:
    """`fh=None` round-trips unchanged."""
    restored = _round_trip(exception_class(1, 'lock failed'))

    assert restored.fh is None
    assert restored.fh_name is None
    assert restored.strerror == 'lock failed'


def test_pickle_posix_shape(tmpfile: str) -> None:
    """The POSIX raise shape (`OSError` first, message second) pickles."""
    original = BlockingIOError(
        errno.EAGAIN, 'Resource temporarily unavailable'
    )
    with open(tmpfile, 'w') as fh:
        exception = exceptions.AlreadyLocked(original, str(original), fh=fh)
        restored = _round_trip(exception)

    restored_inner = restored.args[0]
    assert isinstance(restored_inner, OSError)
    assert restored_inner.errno == errno.EAGAIN
    assert restored.strerror == str(original)
    assert restored.fh is None


def test_pickle_nested_wrap(tmpfile: str) -> None:
    """An `AlreadyLocked` wrapping a lock exception wrapping an `OSError`.

    This is the shape `Lock.acquire` historically produced: the inner
    exception rides along inside `args`, carrying its own filehandle. The
    whole chain has to pickle, so the inner exception's handle must be
    dropped recursively.
    """
    original = BlockingIOError(
        errno.EAGAIN, 'Resource temporarily unavailable'
    )
    with open(tmpfile, 'w') as fh:
        inner = exceptions.LockException(original, str(original), fh=fh)
        outer = exceptions.AlreadyLocked(inner)
        restored = _round_trip(outer)

    restored_inner = restored.args[0]
    assert isinstance(restored_inner, exceptions.LockException)
    assert restored_inner.fh is None
    assert restored_inner.fh_name == tmpfile
    assert restored_inner.strerror == str(original)
    assert isinstance(restored_inner.args[0], OSError)


def test_pickle_no_args() -> None:
    """The bare `AlreadyLocked()` raise from `BoundedSemaphore` pickles."""
    restored = _round_trip(exceptions.AlreadyLocked())

    assert restored.args == ()
    assert restored.strerror is None


def test_pickle_holder_pid() -> None:
    """`holder_pid` lives in the instance dict and must survive."""
    exception = exceptions.AlreadyLocked(1, 'held')
    exception.holder_pid = 12345
    restored = _round_trip(exception)

    assert isinstance(restored, exceptions.AlreadyLocked)
    assert restored.holder_pid == 12345


def test_real_contention_pickles_and_has_strerror(tmpfile: str) -> None:
    """`fail_when_locked=True` contention: picklable, `strerror` filled.

    The migration guide tells users to read `.strerror` for the OS
    message on both platforms, so the exception `Lock.acquire` raises at
    the public surface must actually populate it.
    """
    holder = portalocker.Lock(tmpfile, timeout=0, fail_when_locked=True)
    holder.acquire()
    try:
        contender = portalocker.Lock(tmpfile, timeout=0, fail_when_locked=True)
        with pytest.raises(portalocker.AlreadyLocked) as exception_info:
            contender.acquire()
    finally:
        holder.release()

    exception = exception_info.value
    assert isinstance(exception.strerror, str)
    assert exception.strerror
    restored: exceptions.AlreadyLocked = pickle.loads(pickle.dumps(exception))
    assert isinstance(restored, portalocker.AlreadyLocked)
    assert restored.strerror == exception.strerror


def test_real_timeout_pickles_and_has_strerror(tmpfile: str) -> None:
    """The timeout re-raise is picklable and keeps `strerror` populated."""
    holder = portalocker.Lock(tmpfile, timeout=0, fail_when_locked=True)
    holder.acquire()
    try:
        contender = portalocker.Lock(
            tmpfile,
            timeout=0.01,
            check_interval=0.005,
            fail_when_locked=False,
        )
        with pytest.raises(portalocker.LockException) as exception_info:
            contender.acquire()
    finally:
        holder.release()

    exception = exception_info.value
    assert isinstance(exception.strerror, str)
    assert exception.strerror
    restored: exceptions.LockException = pickle.loads(pickle.dumps(exception))
    assert isinstance(restored, portalocker.LockException)
    assert restored.strerror == exception.strerror


@pytest.mark.skipif(os.name != 'posix', reason='POSIX raise shape')
def test_contention_args_shape_posix(tmpfile: str) -> None:
    """On POSIX the original `OSError` stays reachable in `args[0]`."""
    holder = portalocker.Lock(tmpfile, timeout=0, fail_when_locked=True)
    holder.acquire()
    try:
        contender = portalocker.Lock(tmpfile, timeout=0, fail_when_locked=True)
        with pytest.raises(portalocker.AlreadyLocked) as exception_info:
            contender.acquire()
    finally:
        holder.release()

    assert isinstance(exception_info.value.args[0], OSError)


def _acquire_contended(path: str) -> str:
    """Pool worker: acquire a lock the parent already holds."""
    lock = portalocker.Lock(path, timeout=0, fail_when_locked=True)
    lock.acquire()
    return 'acquired'  # pragma: no cover - only reached if the test fails


@pytest.mark.timeout(60)
def test_multiprocessing_pool_contention(tmpfile: str) -> None:
    """A pool worker's contention arrives in the parent as `AlreadyLocked`.

    Before the pickling fix the pool choked on the unpicklable exception
    and the parent got a `MaybeEncodingError` instead, so user code
    catching `portalocker.AlreadyLocked` never fired.
    """
    holder = portalocker.Lock(tmpfile, timeout=0, fail_when_locked=True)
    holder.acquire()
    context = multiprocessing.get_context('spawn')
    try:
        with context.Pool(1) as pool:
            result = pool.apply_async(_acquire_contended, (tmpfile,))
            with pytest.raises(portalocker.AlreadyLocked) as exception_info:
                result.get(timeout=30)
    finally:
        holder.release()

    exception = exception_info.value
    assert isinstance(exception.strerror, str)
    assert exception.strerror


@pytest.mark.skipif(os.name != 'posix', reason='POSIX unlock wrapping')
def test_posix_unlock_wraps_oserror(tmpfile: str) -> None:
    """A failing POSIX `unlock` raises `LockException`, not raw `OSError`.

    The Windows unlock has always wrapped its failures; the POSIX side
    leaked the raw `OSError`. The errno stays reachable through both
    `args[0]` and `__cause__`.
    """
    fd: int = os.open(tmpfile, os.O_WRONLY | os.O_CREAT)
    os.close(fd)
    with pytest.raises(portalocker.LockException) as exception_info:
        portalocker.unlock(fd)

    exception = exception_info.value
    cause = exception.__cause__
    assert isinstance(cause, OSError)
    assert cause.errno == errno.EBADF
    assert isinstance(exception.args[0], OSError)
    assert exception.args[0].errno == errno.EBADF
    assert isinstance(exception.strerror, str)
    assert exception.strerror


def test_file_to_large_deprecation_warning() -> None:
    """Instantiating `FileToLarge` warns: no version has ever raised it."""
    with pytest.warns(DeprecationWarning, match='FileToLarge'):
        exception = exceptions.FileToLarge(1, 'too large')

    assert isinstance(exception, exceptions.LockException)
    assert exception.strerror == 'too large'
