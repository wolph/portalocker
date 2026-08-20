"""The "timeout has no effect in blocking mode" warning contract.

The warning fires at most once per lock instance, points at the
caller's own file for every entry point (the stacklevel is computed by
walking past portalocker's internal frames), and never fires for
subclasses that were constructed without any timeout argument. The
whole suite runs with ``filterwarnings = error``, so a stray warning in
these constructors would already fail unrelated user suites configured
the same way.
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


@pytest.mark.parametrize(
    'lock_class',
    [
        portalocker.RLock,
        portalocker.TemporaryFileLock,
        portalocker.PidFileLock,
    ],
)
def test_explicit_timeout_still_warns_in_subclass(
    lock_class: type,
    tmpfile: str,
) -> None:
    """A caller-provided timeout warns once and names the caller's file.

    Each subclass constructor stacks its own frames on top of
    ``Lock.__init__``, so a fixed stacklevel would blame portalocker's
    own source here. The computed stacklevel must walk past all of them.
    """
    with pytest.warns(UserWarning, match='timeout has no effect') as record:
        lock_class(tmpfile, timeout=1, flags=BLOCKING)
    assert len(record) == 1
    assert record[0].filename == __file__


def test_rlock_acquire_timeout_warning_names_caller(tmpfile: str) -> None:
    """The ``RLock.acquire`` chain also attributes the warning correctly.

    ``RLock.acquire`` delegates to ``Lock.acquire``, adding one more
    internal frame between the caller and the warning.
    """
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        lock = portalocker.RLock(tmpfile, flags=BLOCKING)

    with pytest.warns(UserWarning, match='timeout has no effect') as record:
        lock.acquire(timeout=0.1)
    assert len(record) == 1
    assert record[0].filename == __file__
    lock.release()


def test_stacklevel_falls_back_without_frame_introspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without frame introspection the stacklevel falls back to ``1``.

    ``inspect.currentframe()`` may return ``None`` on interpreters that
    do not expose Python stack frames. CPython and PyPy always do, so
    the fallback cannot be reached by walking a real stack. The patched
    ``currentframe`` stands in for such an interpreter and the helper
    must degrade to stacklevel ``1`` (blaming the caller) instead of
    crashing.
    """
    import inspect

    from portalocker import utils

    monkeypatch.setattr(inspect, 'currentframe', lambda: None)
    assert utils._stacklevel_beyond_module() == 1
