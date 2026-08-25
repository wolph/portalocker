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

import copy
import errno
import io
import multiprocessing
import os
import pickle
import typing

import pytest

import portalocker
from portalocker import exceptions, types

# `FileToLarge` is deliberately absent: instantiating it emits a
# `DeprecationWarning`, so it gets its own tests below with the warning
# asserted instead of leaking into every parametrised run.
_EXCEPTION_CLASSES: list[type[exceptions.BaseLockException]] = [
    exceptions.BaseLockException,
    exceptions.LockException,
    exceptions.AlreadyLocked,
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


def test_copy_preserves_file_handle(tmpfile: str) -> None:
    """`copy.copy` keeps `fh`; `copy.deepcopy` drops it like pickling.

    A shallow copy stays in the process where the handle is still
    usable, so `__copy__` preserves it. A deep copy has to duplicate the
    handle, which is as impossible as pickling it, so the pickle
    reduction applies and `fh_name` carries the identification instead.
    """
    with open(tmpfile, 'w') as fh:
        exception = exceptions.AlreadyLocked(1, 'lock failed', fh=fh)
        exception.holder_pid = 4242

        shallow = copy.copy(exception)
        assert shallow is not exception
        assert type(shallow) is exceptions.AlreadyLocked
        assert shallow.fh is fh
        assert shallow.args == exception.args
        assert shallow.fh_name == tmpfile
        assert shallow.strerror == 'lock failed'
        assert shallow.holder_pid == 4242

        deep = copy.deepcopy(exception)
        assert deep.fh is None
        assert deep.fh_name == tmpfile
        assert deep.holder_pid == 4242


def test_detached_wrapper_does_not_break_construction() -> None:
    """A handle whose ``name`` lookup raises must not break `__init__`.

    A detached `io.TextIOWrapper` raises `ValueError` from its ``name``
    property, which `getattr` with a default does not swallow. The
    constructor must survive that and fall back to `fh_name=None`.
    """
    wrapper = io.TextIOWrapper(io.BytesIO(), encoding='utf-8')
    wrapper.detach()
    exception = exceptions.LockException(1, 'lock failed', fh=wrapper)

    assert exception.fh is wrapper
    assert exception.fh_name is None


class _HolderPidLock(portalocker.Lock):
    """Lock whose locking step reports contention with a holder PID."""

    def _get_lock(self, fh: typing.IO[str]) -> typing.IO[str]:
        raise exceptions.AlreadyLocked(
            1,
            'held elsewhere',
            fh=fh,
            holder_pid=4242,
        )


def test_lock_surface_forwards_fh_name_and_holder_pid(tmpfile: str) -> None:
    """The `Lock.acquire` wrap forwards `fh` and `holder_pid`.

    Pickling drops both `fh` and `__cause__`, so without the forwarding
    a multiprocessing user could not tell which file was contended or
    who held it. `fh_name` and `holder_pid` must survive the pool
    boundary on the Lock-surface exception itself.
    """
    lock = _HolderPidLock(tmpfile, timeout=0, fail_when_locked=True)
    with pytest.raises(portalocker.AlreadyLocked) as exception_info:
        lock.acquire()

    exception = exception_info.value
    assert exception.fh_name == tmpfile
    assert exception.holder_pid == 4242

    restored: exceptions.AlreadyLocked = pickle.loads(pickle.dumps(exception))
    assert restored.fh is None
    assert restored.fh_name == tmpfile
    assert restored.holder_pid == 4242
    assert restored.strerror == 'held elsewhere'


def test_real_contention_fh_name_survives_pickle(tmpfile: str) -> None:
    """Real contention: the contended file's name survives the pickle."""
    holder = portalocker.Lock(tmpfile, timeout=0, fail_when_locked=True)
    holder.acquire()
    try:
        contender = portalocker.Lock(tmpfile, timeout=0, fail_when_locked=True)
        with pytest.raises(portalocker.AlreadyLocked) as exception_info:
            contender.acquire()
    finally:
        holder.release()

    restored: exceptions.AlreadyLocked = pickle.loads(
        pickle.dumps(exception_info.value)
    )
    assert restored.fh_name == tmpfile


@pytest.mark.skipif(os.name != 'posix', reason='POSIX unlock wrapping')
def test_posix_unlock_wraps_eoferror(
    tmpfile: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`unlock` wraps `EOFError` in `LockException`, matching `lock`.

    ``fcntl.lockf`` can raise `EOFError` on some NFS setups. The lock
    side has always translated it; the unlock side must too.
    """

    def _raise_eof(fd: int | types.HasFileno, flags: int) -> None:
        raise EOFError('lockf gave up on NFS')

    monkeypatch.setattr(portalocker.portalocker, 'LOCKER', _raise_eof)
    fd: int = os.open(tmpfile, os.O_WRONLY | os.O_CREAT)
    try:
        with pytest.raises(portalocker.LockException) as exception_info:
            portalocker.unlock(fd)
    finally:
        os.close(fd)

    exception = exception_info.value
    assert isinstance(exception.__cause__, EOFError)
    assert isinstance(exception.args[0], EOFError)
    assert isinstance(exception.strerror, str)
    assert exception.strerror


def test_file_to_large_pickle(tmpfile: str) -> None:
    """`FileToLarge` pickles like the rest, warning on each construction.

    Unpickling reconstructs the exception through `__init__`, so both
    the original construction and the round trip emit the deprecation
    warning.
    """
    with open(tmpfile, 'w') as fh:
        with pytest.warns(DeprecationWarning, match='FileToLarge'):
            exception = exceptions.FileToLarge(1, 'lock failed', fh=fh)
        with pytest.warns(DeprecationWarning, match='FileToLarge'):
            restored: exceptions.FileToLarge = pickle.loads(
                pickle.dumps(exception)
            )

    assert type(restored) is exceptions.FileToLarge
    assert restored.args == (1, 'lock failed')
    assert restored.strerror == 'lock failed'
    assert restored.fh is None
    assert restored.fh_name == tmpfile


def test_file_to_large_deprecation_warning() -> None:
    """Instantiating `FileToLarge` warns: no version has ever raised it."""
    with pytest.warns(DeprecationWarning, match='FileToLarge'):
        exception = exceptions.FileToLarge(1, 'too large')

    assert isinstance(exception, exceptions.LockException)
    assert exception.strerror == 'too large'
