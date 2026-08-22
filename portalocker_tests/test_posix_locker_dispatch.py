"""POSIX ``LOCKER`` dispatch and subclass binding.

Exercises the module-level ``portalocker.lock`` / ``portalocker.unlock``
dispatch on POSIX for every ``LockerType`` form (plain callable, tuple,
``BaseLocker`` instance, ``BaseLocker`` subclass) plus the
``FlockLocker`` / ``LockfLocker`` callable binding.

POSIX-only: the ``PosixLocker`` family lives in the ``else`` (non-nt) branch
of ``portalocker.portalocker``.
"""

from __future__ import annotations

import os

import pytest

import portalocker
from portalocker import LockFlags

if os.name != 'posix':
    pytest.skip(
        'PosixLocker family is only defined on posix',
        allow_module_level=True,
    )

import fcntl  # noqa: E402

from portalocker.portalocker import (  # noqa: E402
    FlockLocker,
    LockfLocker,
    PosixLocker,
)


@pytest.fixture
def set_locker(monkeypatch):
    """Return a helper that swaps ``portalocker.portalocker.LOCKER``."""

    def _set(value: object) -> object:
        monkeypatch.setattr(portalocker.portalocker, 'LOCKER', value)
        return value

    return _set


def _assert_exclusive_conflict(tmpfile: str) -> None:
    """A second non-blocking exclusive lock on ``tmpfile`` must fail."""
    with open(tmpfile, 'a+') as a, open(tmpfile, 'a+') as b:
        portalocker.lock(a, LockFlags.EXCLUSIVE)
        try:
            with pytest.raises(portalocker.LockException):
                portalocker.lock(
                    b, LockFlags.EXCLUSIVE | LockFlags.NON_BLOCKING
                )
        finally:
            portalocker.unlock(a)


# --- B1: subclasses bind their own callable, not the module global -------


def test_flocklocker_binds_flock(set_locker):
    # Even with the module-level LOCKER monkeypatched to lockf, FlockLocker
    # must resolve to fcntl.flock rather than the global fallback.
    set_locker(fcntl.lockf)
    assert FlockLocker().locker is fcntl.flock


def test_lockflocker_binds_lockf(set_locker):
    set_locker(fcntl.flock)
    assert LockfLocker().locker is fcntl.lockf


def test_plain_posixlocker_uses_global(set_locker):
    # A bare PosixLocker (no bound callable) still follows the global LOCKER.
    set_locker(fcntl.flock)
    assert PosixLocker().locker is fcntl.flock
    set_locker(fcntl.lockf)
    assert PosixLocker().locker is fcntl.lockf


# --- B2: module-level dispatch supports every LockerType form ------------


def test_dispatch_callable(set_locker, tmpfile):
    set_locker(fcntl.flock)
    _assert_exclusive_conflict(tmpfile)


def test_dispatch_tuple(set_locker, tmpfile):
    flock_locker = FlockLocker()
    set_locker((flock_locker.lock, flock_locker.unlock))
    _assert_exclusive_conflict(tmpfile)


def test_dispatch_instance(set_locker, tmpfile):
    set_locker(FlockLocker())
    _assert_exclusive_conflict(tmpfile)


def test_posix_lockexception_has_strerror(set_locker, tmpfile):
    # B5: the message passed to the exception must populate ``strerror``.
    set_locker(fcntl.flock)
    with open(tmpfile, 'a+') as a, open(tmpfile, 'a+') as b:
        portalocker.lock(a, LockFlags.EXCLUSIVE)
        try:
            with pytest.raises(portalocker.AlreadyLocked) as exc_info:
                portalocker.lock(
                    b, LockFlags.EXCLUSIVE | LockFlags.NON_BLOCKING
                )
            assert isinstance(exc_info.value.strerror, str)
            assert exc_info.value.strerror
        finally:
            portalocker.unlock(a)


def test_dispatch_class(set_locker, tmpfile):
    # The class form instantiates lazily and caches: lock() creates the
    # instance, unlock() reuses it (covers both cache branches).
    from portalocker.portalocker import _locker_instances

    _locker_instances.pop(FlockLocker, None)
    set_locker(FlockLocker)
    with open(tmpfile, 'a+') as a:
        portalocker.lock(a, LockFlags.EXCLUSIVE)
        portalocker.unlock(a)
    assert isinstance(_locker_instances.get(FlockLocker), FlockLocker)


def test_direct_posix_locker_rejects_nonblocking_alone(tmpfile):
    """``PosixLocker.lock`` keeps its own NON_BLOCKING-alone guard.

    The module-level ``portalocker.lock`` validates flags before
    dispatching (since 4.2.0), but a ``PosixLocker`` can also be used
    directly, and NON_BLOCKING without a lock type must still fail with
    a clear ``RuntimeError`` there instead of an opaque ``fcntl`` error.
    """
    locker = PosixLocker()
    with open(tmpfile, 'a+') as fh, pytest.raises(RuntimeError):
        locker.lock(fh, LockFlags.NON_BLOCKING)


# --- error translation coverage ------------------------------------------


def test_get_fd_rejects_unsupported_object():
    """``PosixLocker._get_fd`` must reject objects with no descriptor.

    The typed signature promises an ``int``, an IO object, or a
    ``HasFileno`` implementation, but the method is plain runtime code
    and can be handed anything. The guard raise is part of the contract
    and therefore measured and tested rather than excluded from
    coverage.
    """
    locker = PosixLocker()
    with pytest.raises(TypeError, match='fileno'):
        locker._get_fd(object())  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]  # noqa: E501


def test_lock_wraps_non_contention_oserror(set_locker, tmpfile):
    """A non-contention ``OSError`` becomes a plain ``LockException``.

    ``EACCES`` / ``EAGAIN`` mean somebody else holds the lock and map to
    ``AlreadyLocked``. Any other errno (``ENOLCK`` here, the classic
    "no locks available" NFS failure) is a real error, must not be
    retried, and surfaces as the base ``LockException`` with the
    original ``OSError`` chained as the cause.
    """
    import errno

    failure = OSError(errno.ENOLCK, 'No locks available')

    def broken_locker(fd: int, flags: int) -> None:
        raise failure

    set_locker(broken_locker)
    locker = PosixLocker()
    with (
        open(tmpfile, 'a+') as fh,
        pytest.raises(portalocker.LockException) as exc_info,
    ):
        locker.lock(fh, LockFlags.EXCLUSIVE)
    assert not isinstance(exc_info.value, portalocker.AlreadyLocked)
    assert exc_info.value.__cause__ is failure


def test_lock_wraps_eoferror(set_locker, tmpfile):
    """The ``EOFError`` some NFS setups raise becomes ``LockException``.

    ``fcntl`` on a broken NFS mount can fail with a bare ``EOFError``
    instead of an ``OSError``. The locker must translate it exactly like
    any other failure instead of leaking it raw. The NFS condition
    itself cannot be staged in a unit test, so the raising locker
    callable stands in for the broken mount.
    """
    failure = EOFError('lost communication with locking daemon')

    def broken_locker(fd: int, flags: int) -> None:
        raise failure

    set_locker(broken_locker)
    locker = PosixLocker()
    with (
        open(tmpfile, 'a+') as fh,
        pytest.raises(portalocker.LockException) as exc_info,
    ):
        locker.lock(fh, LockFlags.EXCLUSIVE)
    assert exc_info.value.__cause__ is failure
