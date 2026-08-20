import gc
import logging
import os
import pathlib

import pytest

import portalocker
from portalocker import utils

# The unlink-before-unlock ordering (and unlink errors surfacing from
# release) only applies to the POSIX release path; Windows deliberately
# unlocks first and tolerates unlink failures.
posix_release_only = pytest.mark.skipif(
    os.name == 'nt',
    reason='POSIX-only release ordering',
)

# The inode-based split-brain guard (`_fh_matches_path` and the
# unlink+recreate detection it enables) is POSIX-only: on Windows a locked
# file cannot be unlinked, so `_fh_matches_path` returns True unconditionally
# and there is no swap to detect. These tests exercise that POSIX semantics.
posix_inode_only = pytest.mark.skipif(
    os.name == 'nt',
    reason='POSIX-only inode verification; _fh_matches_path is a no-op on nt',
)


def test_temporary_file_lock(tmpfile):
    """The lock file must be deleted on context exit, and GC of a held
    lock wrapper must leave the lock file alone.
    """
    with portalocker.TemporaryFileLock(tmpfile):
        pass

    assert not os.path.isfile(tmpfile)

    lock = portalocker.TemporaryFileLock(tmpfile)
    lock.acquire()
    del lock
    # PyPy defers collection, so force one before asserting. Collection of
    # the wrapper must not tear down the held lock: the file stays until an
    # explicit release or interpreter exit (the ``atexit`` fallback).
    gc.collect()
    assert pathlib.Path(tmpfile).exists(), (
        'Lock file must survive garbage collection of the lock object'
    )


@posix_inode_only
def test_fh_matches_path_detects_swap(tmpfile):
    """A2: the inode helper must accept a live handle and reject a handle
    whose path was unlinked or recreated behind its back.
    """
    fh = open(tmpfile, 'a')  # noqa: SIM115
    try:
        assert utils._fh_matches_path(fh, tmpfile) is True
        # Unlinked: the path no longer exists.
        os.unlink(tmpfile)
        assert utils._fh_matches_path(fh, tmpfile) is False
        # Recreated: the path exists but points at a different inode.
        pathlib.Path(tmpfile).write_text('')
        assert utils._fh_matches_path(fh, tmpfile) is False
    finally:
        fh.close()


@posix_release_only
def test_temporaryfilelock_unlinks_before_unlock(tmpfile, monkeypatch):
    """A2: release must unlink the file while the lock is still held (unlink
    before unlock) to avoid a split-brain window.
    """
    events: list[str] = []

    real_unlink = os.unlink
    real_unlock = portalocker.portalocker.unlock

    def record_unlink(path, *args, **kwargs):
        events.append('unlink')
        return real_unlink(path, *args, **kwargs)

    def record_unlock(file_obj, *args, **kwargs):
        events.append('unlock')
        return real_unlock(file_obj, *args, **kwargs)

    monkeypatch.setattr(os, 'unlink', record_unlink)
    monkeypatch.setattr(portalocker.portalocker, 'unlock', record_unlock)

    lock = portalocker.TemporaryFileLock(tmpfile)
    lock.acquire()
    lock.release()

    assert events == ['unlink', 'unlock']


def _fail_unlink(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``os.unlink`` raise `PermissionError` for every path."""

    def failing_unlink(target: str, *args: object, **kwargs: object) -> None:
        raise PermissionError(f'unlink denied for {target!r}')

    monkeypatch.setattr(os, 'unlink', failing_unlink)


@posix_release_only
def test_temporaryfilelock_release_suppresses_unlink_error_by_default(
    tmpfile,
    monkeypatch,
    caplog,
):
    """With ``raise_on_release_error`` unset an unlink failure must be
    suppressed and logged, and the OS lock must be freed regardless.
    Anything else would leave the lock held forever.
    """
    lock = portalocker.TemporaryFileLock(tmpfile)
    lock.acquire()

    _fail_unlink(monkeypatch)
    with caplog.at_level(logging.WARNING, logger='portalocker.utils'):
        lock.release()
    monkeypatch.undo()

    assert lock.fh is None, 'release left the instance holding a handle'
    assert any(
        'suppressed error' in record.getMessage() for record in caplog.records
    ), 'suppressed unlink error was not logged'

    # The unlock ran: a fresh lock on the same path acquires immediately.
    fresh = portalocker.TemporaryFileLock(tmpfile, timeout=0)
    fresh.acquire()
    fresh.release()
    assert not os.path.isfile(tmpfile)


@posix_release_only
def test_temporaryfilelock_strict_release_raises_unlink_error(
    tmpfile,
    monkeypatch,
):
    """With ``raise_on_release_error`` set an unlink failure must
    propagate, but the OS lock must still be freed first.
    """
    lock = portalocker.TemporaryFileLock(tmpfile)
    lock.raise_on_release_error = True
    lock.acquire()

    _fail_unlink(monkeypatch)
    with pytest.raises(PermissionError):
        lock.release()
    monkeypatch.undo()

    # The unlock ran: a fresh lock on the same path acquires immediately.
    fresh = portalocker.TemporaryFileLock(tmpfile, timeout=0)
    fresh.acquire()
    fresh.release()
    assert not os.path.isfile(tmpfile)


@posix_release_only
def test_temporaryfilelock_body_exception_wins_by_default(
    tmpfile,
    monkeypatch,
):
    """An exception from the ``with`` body must propagate unchanged even
    when the unlink in `release` fails on the way out.
    """
    lock = portalocker.TemporaryFileLock(tmpfile)
    body_error = ValueError('the actual bug in the body')

    with pytest.raises(ValueError) as exc_info:  # noqa: PT012, SIM117
        with lock:
            _fail_unlink(monkeypatch)
            raise body_error

    assert exc_info.value is body_error


@posix_release_only
def test_temporaryfilelock_body_exception_wins_when_strict(
    tmpfile,
    monkeypatch,
):
    """With ``raise_on_release_error`` set the body exception still wins,
    with the unlink failure chained on as its ``__context__``.
    """
    lock = portalocker.TemporaryFileLock(tmpfile)
    lock.raise_on_release_error = True
    body_error = ValueError('the actual bug in the body')

    with pytest.raises(ValueError) as exc_info:  # noqa: PT012, SIM117
        with lock:
            _fail_unlink(monkeypatch)
            raise body_error

    assert exc_info.value is body_error
    assert isinstance(exc_info.value.__context__, PermissionError)


@posix_release_only
def test_temporaryfilelock_release_tolerates_vanished_file(tmpfile):
    """A held lock file that a third party already unlinked must release
    without complaint.
    """
    lock = portalocker.TemporaryFileLock(tmpfile)
    lock.acquire()
    os.unlink(tmpfile)

    lock.release()

    assert lock.fh is None


@posix_release_only
def test_temporaryfilelock_strict_unlock_error_wins_over_unlink_error(
    tmpfile,
    monkeypatch,
):
    """With ``raise_on_release_error`` set and both the unlink and the
    unlock failing, the unlock error propagates with the unlink error
    chained on as its ``__cause__``.
    """
    lock = portalocker.TemporaryFileLock(tmpfile)
    lock.raise_on_release_error = True
    lock.acquire()

    unlock_error = OSError('unlock failed')

    def failing_unlock(fh, *args, **kwargs):
        raise unlock_error

    _fail_unlink(monkeypatch)
    monkeypatch.setattr(portalocker.portalocker, 'unlock', failing_unlock)

    with pytest.raises(OSError) as exc_info:
        lock.release()
    monkeypatch.undo()

    assert exc_info.value is unlock_error
    assert isinstance(exc_info.value.__cause__, PermissionError)
    assert lock.fh is None
    os.unlink(tmpfile)


@posix_release_only
def test_temporaryfilelock_strict_unlock_error_propagates_alone(
    tmpfile,
    monkeypatch,
):
    """With ``raise_on_release_error`` set and only the unlock failing,
    that error propagates without an artificial ``__cause__``.
    """
    lock = portalocker.TemporaryFileLock(tmpfile)
    lock.raise_on_release_error = True
    lock.acquire()

    unlock_error = OSError('unlock failed')

    def failing_unlock(fh, *args, **kwargs):
        raise unlock_error

    monkeypatch.setattr(portalocker.portalocker, 'unlock', failing_unlock)

    with pytest.raises(OSError) as exc_info:
        lock.release()
    monkeypatch.undo()

    assert exc_info.value is unlock_error
    assert exc_info.value.__cause__ is None
    assert lock.fh is None


@posix_inode_only
def test_temporaryfilelock_recovers_from_stale_handle(tmpfile, monkeypatch):
    """A2: if the locked handle no longer names the current path, acquire must
    drop it and re-acquire within the timeout.
    """
    calls: list[str] = []
    real_matches = utils._fh_matches_path

    def flaky(fh, filename):
        calls.append(filename)
        # The first acquired handle looks stale, the retry is honoured.
        if len(calls) == 1:
            return False
        return real_matches(fh, filename)

    monkeypatch.setattr(utils, '_fh_matches_path', flaky)

    lock = portalocker.TemporaryFileLock(tmpfile, timeout=1.0)
    fh = lock.acquire()
    try:
        assert fh is not None
        assert len(calls) == 2, 'expected exactly one stale detection + retry'
        assert os.path.isfile(tmpfile)
    finally:
        lock.release()
    assert not os.path.isfile(tmpfile)


@posix_inode_only
def test_temporaryfilelock_gives_up_on_persistent_swap(tmpfile, monkeypatch):
    """A2: a path that keeps being replaced must surface as AlreadyLocked
    within the timeout rather than spinning forever.
    """
    monkeypatch.setattr(utils, '_fh_matches_path', lambda fh, filename: False)

    lock = portalocker.TemporaryFileLock(tmpfile, timeout=0)
    with pytest.raises(portalocker.AlreadyLocked):
        lock.acquire()


def test_temporaryfilelock_sequential_cycles(tmpfile):
    """A2: two lock/release cycles on the same path must both succeed and
    clean up the file each time.
    """
    for _ in range(2):
        lock = portalocker.TemporaryFileLock(tmpfile)
        lock.acquire()
        assert os.path.isfile(tmpfile)
        lock.release()
        assert not os.path.isfile(tmpfile)


def test_temporaryfilelock_release_without_ownership_keeps_file(tmpfile):
    """Releasing a lock object that holds nothing must not unlink the path.

    A stale object (double release or GC of a failed acquire) would otherwise
    destroy the current holder's lock file.
    """
    stale = portalocker.TemporaryFileLock(tmpfile)
    stale.acquire()
    stale.release()

    holder = portalocker.TemporaryFileLock(tmpfile)
    holder.acquire()
    try:
        # Double release of the stale object must be a no-op.
        stale.release()
        assert os.path.isfile(tmpfile), 'stale release unlinked a held path'

        # A never-acquired object (the __del__-after-failed-acquire path)
        # must be a no-op too.
        never_acquired = portalocker.TemporaryFileLock(tmpfile)
        never_acquired.release()
        assert os.path.isfile(tmpfile), (
            'never-acquired release unlinked a held path'
        )
    finally:
        holder.release()
    assert not os.path.isfile(tmpfile)
