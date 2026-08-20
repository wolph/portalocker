import atexit
import gc
import os
import pathlib
import subprocess
import sys
import textwrap

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


def test_lock_construction_registers_no_atexit_callbacks(
    tmpfile: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Constructing locks must not register per-instance atexit callbacks.

    Every construction used to call ``atexit.register`` with a fresh
    closure that was never unregistered, so a daemon churning through
    short-lived locks accumulated one dead callback per lock forever.
    """
    registered: list[object] = []

    def record_register(
        func: object,
        /,
        *args: object,
        **kwargs: object,
    ) -> object:
        registered.append(func)
        return func

    monkeypatch.setattr(atexit, 'register', record_register)

    for _ in range(10):
        portalocker.TemporaryFileLock(tmpfile)
        portalocker.PidFileLock(f'{tmpfile}.pid')

    assert registered == []


@pytest.mark.skipif(
    not hasattr(atexit, '_ncallbacks'),
    reason='atexit._ncallbacks is CPython specific',
)
def test_atexit_callback_count_stays_flat(tmpfile: str) -> None:
    """N acquire/release cycles must leave the atexit callback count as is."""
    baseline: int = atexit._ncallbacks()

    for _ in range(25):
        lock = portalocker.TemporaryFileLock(tmpfile)
        lock.acquire()
        lock.release()

    assert atexit._ncallbacks() == baseline


def test_atexit_hook_releases_lock_held_at_interpreter_exit(
    tmp_path: pathlib.Path,
) -> None:
    """A lock still held at interpreter exit must have its file unlinked.

    The garbage collection fallback is neutralized inside the child, so
    only the module level atexit hook can perform the cleanup.
    """
    lock_path: pathlib.Path = tmp_path / 'held.lock'
    script: str = textwrap.dedent(
        f"""\
        import gc

        import portalocker
        from portalocker import utils

        # Neutralize the garbage collection fallback so that only the
        # atexit hook can clean up.
        utils.LockBase.__del__ = lambda self: None
        gc.disable()

        lock = portalocker.TemporaryFileLock({str(lock_path)!r})
        lock.acquire()
        if lock.fh is None:
            raise RuntimeError('acquire did not take the lock')
        """,
    )
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, '-c', script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout == ''
    assert completed.stderr == ''
    assert not lock_path.exists(), 'atexit hook did not release the lock'


@pytest.mark.skipif(os.name == 'nt', reason='os.fork is POSIX-only')
def test_atexit_hook_ignores_inherited_locks_in_forked_child(
    tmp_path: pathlib.Path,
) -> None:
    """A forked child exiting normally must not release the parent's lock.

    The child inherits the live lock objects and the atexit machinery, so
    its normal exit used to unlink the parent's lock file and drop the OS
    lock (which lives on the shared open file description). The classic
    daemonize sequence of acquire-then-fork lost its lock the moment
    either side exited. The exit hook must skip locks another process
    constructed.
    """
    lock_path: pathlib.Path = tmp_path / 'daemon.lock'
    script: str = textwrap.dedent(
        f"""\
        import os
        import sys

        import portalocker
        from portalocker import utils

        lock = portalocker.TemporaryFileLock({str(lock_path)!r})
        lock.acquire()

        pid = os.fork()
        if pid == 0:
            # The garbage collection fallback is neutralized in the child
            # only, so this test isolates the atexit path.
            utils.LockBase.__del__ = lambda self: None
            sys.exit(0)

        os.waitpid(pid, 0)
        if not os.path.exists({str(lock_path)!r}):
            raise RuntimeError("child's exit unlinked the parent's lock")

        contender = portalocker.TemporaryFileLock(
            {str(lock_path)!r}, timeout=0
        )
        try:
            contender.acquire()
        except portalocker.AlreadyLocked:
            pass
        else:
            raise RuntimeError("child's exit released the parent's lock")

        lock.release()
        if os.path.exists({str(lock_path)!r}):
            raise RuntimeError('parent release left the lock file behind')
        """,
    )
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, '-c', script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout == ''
    assert completed.stderr == ''
    assert not lock_path.exists()


def test_atexit_hook_skips_collected_locks(tmpfile: str) -> None:
    """The exit hook tracks instances weakly: a collected lock drops out.

    This mirrors the old weakref based behaviour: registration must never
    keep a lock alive, and garbage collection remains responsible for
    locks that die before the interpreter does.
    """
    lock = portalocker.TemporaryFileLock(tmpfile)
    assert lock in utils._exit_releases

    del lock
    gc.collect()
    assert all(item.filename != tmpfile for item in utils._exit_releases)
