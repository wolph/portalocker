import gc
import os
import pathlib
import time

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
    """The lock file must be deleted on context exit and GC must close
    the lock gracefully.
    """
    with portalocker.TemporaryFileLock(tmpfile):
        pass

    assert not os.path.isfile(tmpfile)

    lock = portalocker.TemporaryFileLock(tmpfile)
    lock.acquire()
    del lock
    # CPython removes the file via refcount-driven `__del__`, but PyPy defers
    # finalizers, so force a collection to run them before asserting.
    gc.collect()
    assert not pathlib.Path(tmpfile).exists(), (
        'Lock file should be removed on lock object deletion'
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


@posix_release_only
def test_temporaryfilelock_unlocks_even_when_unlink_fails(
    tmpfile,
    monkeypatch,
):
    """Fix round 1: a non-FileNotFoundError unlink failure must still
    propagate, but the OS lock must be freed regardless — otherwise the
    error would leave the lock held forever.
    """
    lock = portalocker.TemporaryFileLock(tmpfile)
    lock.acquire()

    def failing_unlink(path, *args, **kwargs):
        raise PermissionError(f'unlink denied for {path!r}')

    monkeypatch.setattr(os, 'unlink', failing_unlink)
    with pytest.raises(PermissionError):
        lock.release()
    monkeypatch.undo()

    # The unlock ran: a fresh lock on the same path acquires immediately.
    fresh = portalocker.TemporaryFileLock(tmpfile, timeout=0)
    fresh.acquire()
    fresh.release()
    assert not os.path.isfile(tmpfile)


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


def test_temporaryfilelock_reacquire_while_held_is_noop(tmpfile):
    """Re-acquiring a held lock with an intact path must be an idempotent
    no-op: the same filehandle comes back, open and locked, and the lock
    is never released in between.
    """
    lock = portalocker.TemporaryFileLock(tmpfile)
    first_fh = lock.acquire()
    second_fh = lock.acquire()
    assert second_fh is first_fh
    assert not first_fh.closed
    lock.release()
    assert not os.path.isfile(tmpfile)


@posix_inode_only
@pytest.mark.parametrize('timeout', [0, None])
def test_temporaryfilelock_external_unlink_compromises_held_lock(
    tmpfile,
    timeout,
):
    """T10a/T10b: when a third party unlinked the path of a held lock
    (tmpwatch cleaning /tmp is enough), re-acquire must raise and leave the
    held filehandle untouched, for both the fail-fast (timeout=0) and the
    retrying (default timeout) forms. It must never release the held lock,
    close the caller's filehandle or silently swap to a new inode.
    """
    kwargs = {} if timeout is None else {'timeout': timeout}
    lock = portalocker.TemporaryFileLock(tmpfile, **kwargs)
    held_fh = lock.acquire()
    os.unlink(tmpfile)  # a third party cleans up the "stale" lock file

    with pytest.raises(portalocker.LockException, match='unlink'):
        lock.acquire()

    assert not held_fh.closed, 'the held filehandle was closed'
    assert lock.fh is held_fh, 'the instance dropped the lock it held'
    lock.release()


@posix_inode_only
def test_temporaryfilelock_external_replace_compromises_held_lock(tmpfile):
    """Same as the unlink case, but the path was recreated as well: the
    held lock must not silently migrate to the new inode.
    """
    lock = portalocker.TemporaryFileLock(tmpfile)
    held_fh = lock.acquire()
    os.unlink(tmpfile)
    pathlib.Path(tmpfile).write_text('')

    with pytest.raises(portalocker.LockException, match='unlink'):
        lock.acquire()

    assert not held_fh.closed
    assert lock.fh is held_fh
    lock.release()


@posix_inode_only
def test_temporaryfilelock_retry_passes_remaining_timeout(
    tmpfile,
    monkeypatch,
):
    """A fresh acquire whose verification keeps failing must fit one
    timeout budget: every retry is handed the remaining deadline instead of
    the full timeout again (which compounded to timeout^2/check_interval).
    """
    recorded: list[float | None] = []
    real_acquire = utils.Lock.acquire

    def spy_acquire(
        self,
        timeout=None,
        check_interval=None,
        fail_when_locked=None,
    ):
        recorded.append(timeout)
        return real_acquire(self, timeout, check_interval, fail_when_locked)

    monkeypatch.setattr(utils.Lock, 'acquire', spy_acquire)
    monkeypatch.setattr(utils, '_fh_matches_path', lambda fh, filename: False)

    lock = portalocker.TemporaryFileLock(
        tmpfile,
        timeout=0.5,
        check_interval=0.1,
    )
    start = time.perf_counter()
    with pytest.raises(portalocker.AlreadyLocked):
        lock.acquire()
    elapsed = time.perf_counter() - start

    assert elapsed < 1.5, f'a single 0.5s budget took {elapsed:.3f}s'
    assert len(recorded) >= 2, 'expected at least one verification retry'
    for passed_timeout in recorded[1:]:
        assert passed_timeout is not None, 'retry got the full timeout again'
        assert passed_timeout <= 0.5


def test_temporaryfilelock_accepts_pathlib_path(tmp_path):
    """`filename` is `types.Filename`: a `pathlib.Path` must work end to
    end and satisfy the type checkers.
    """
    path = tmp_path / 'pathlib.lock'
    lock = portalocker.TemporaryFileLock(path)
    lock.acquire()
    assert path.is_file()
    lock.release()
    assert not path.exists()
