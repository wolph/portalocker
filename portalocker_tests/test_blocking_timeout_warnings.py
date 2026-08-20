"""The "timeout has no effect in blocking mode" warning contract.

The warning fires at most once per lock instance, points at the caller
(``stacklevel=2``), and never fires for subclasses that were constructed
without any timeout argument. The whole suite runs with
``filterwarnings = error``, so a stray warning in these constructors
would already fail unrelated user suites configured the same way.
"""

import warnings

import pytest

import portalocker

#: Blocking flags: EXCLUSIVE without NON_BLOCKING.
BLOCKING: portalocker.LockFlags = portalocker.LockFlags.EXCLUSIVE


def test_constructor_timeout_blocking_warns_once(tmpfile: str) -> None:
    """An explicit timeout with blocking flags warns at construction only."""
    with pytest.warns(UserWarning, match='timeout has no effect') as record:
        lock = portalocker.Lock(tmpfile, timeout=0.1, flags=BLOCKING)
    assert len(record) == 1
    # The warning points at this test, not at portalocker's own source.
    assert record[0].filename == __file__

    # Acquiring with a per-call timeout must not warn a second time.
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        lock.acquire(timeout=0.1)
    lock.release()


def test_acquire_timeout_blocking_warns_once(tmpfile: str) -> None:
    """Without a constructor timeout, the first acquire warns exactly once."""
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        lock = portalocker.Lock(tmpfile, flags=BLOCKING)

    with pytest.warns(UserWarning, match='timeout has no effect') as record:
        lock.acquire(timeout=0.1)
    assert len(record) == 1
    assert record[0].filename == __file__
    lock.release()

    # Later acquires with a per-call timeout stay silent.
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        lock.acquire(timeout=0.1)
    lock.release()


def test_acquire_without_timeout_never_warns(tmpfile: str) -> None:
    """Blocking flags without any timeout are a valid, silent combination."""
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        lock = portalocker.Lock(tmpfile, flags=BLOCKING)
        lock.acquire()
    lock.release()


def test_rlock_without_timeout_is_silent(tmpfile: str) -> None:
    """RLock without a timeout argument must not inherit a fake one."""
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        portalocker.RLock(tmpfile, flags=BLOCKING)


def test_temporary_file_lock_without_timeout_is_silent(tmpfile: str) -> None:
    """TemporaryFileLock without a timeout argument stays silent."""
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        portalocker.TemporaryFileLock(tmpfile, flags=BLOCKING)


def test_pid_file_lock_without_timeout_is_silent(tmpfile: str) -> None:
    """PidFileLock without a timeout argument stays silent."""
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        portalocker.PidFileLock(tmpfile, flags=BLOCKING)


def test_explicit_timeout_still_warns_in_subclass(tmpfile: str) -> None:
    """A real, caller-provided timeout still triggers the warning."""
    with pytest.warns(UserWarning, match='timeout has no effect'):
        portalocker.RLock(tmpfile, timeout=1, flags=BLOCKING)
