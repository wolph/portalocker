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
