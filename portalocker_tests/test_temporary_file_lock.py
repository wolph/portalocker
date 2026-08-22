import atexit
import contextlib
import errno
import gc
import logging
import os
import pathlib
import subprocess
import sys
import textwrap
import time
import typing

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
def test_temporaryfilelock_release_tolerates_race_vanished_file(
    tmpfile,
    monkeypatch,
):
    """A lock file that vanishes between the ownership check and the
    unlink must still release without complaint.
    """
    lock = portalocker.TemporaryFileLock(tmpfile)
    lock.acquire()
    monkeypatch.setattr(utils, '_fh_matches_path', lambda fh, path: True)
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

        # A never-acquired object (a stale release after a failed acquire)
        # must be a no-op too.
        never_acquired = portalocker.TemporaryFileLock(tmpfile)
        never_acquired.release()
        assert os.path.isfile(tmpfile), (
            'never-acquired release unlinked a held path'
        )
    finally:
        holder.release()
    assert not os.path.isfile(tmpfile)


@posix_release_only
def test_temporaryfilelock_strict_context_chain_has_no_cycle(
    tmpfile,
    monkeypatch,
):
    """Strict mode with the body, the unlink and the unlock all failing
    must build an exception chain that terminates instead of cycling.
    """
    lock = portalocker.TemporaryFileLock(tmpfile)
    lock.raise_on_release_error = True
    body_error = ValueError('the actual bug in the body')
    unlock_error = OSError('unlock failed')

    def failing_unlock(fh, *args, **kwargs):
        raise unlock_error

    with pytest.raises(ValueError) as exc_info:  # noqa: PT012, SIM117
        with lock:
            _fail_unlink(monkeypatch)
            monkeypatch.setattr(
                portalocker.portalocker,
                'unlock',
                failing_unlock,
            )
            raise body_error

    assert exc_info.value is body_error
    assert exc_info.value.__context__ is unlock_error
    assert isinstance(unlock_error.__cause__, PermissionError)

    # Walk the chain with no visited set, bounded to ten hops. A cycle
    # keeps the walker inside the chain, a healthy chain falls off the
    # end well within the bound.
    link: BaseException | None = exc_info.value
    hops: int = 0
    while link is not None and hops < 10:
        link = link.__cause__ or link.__context__
        hops += 1
    assert link is None, 'exception chain does not terminate (cycle)'


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


@posix_inode_only
def test_temporaryfilelock_compromised_release_spares_competitor_file(
    tmpfile,
    caplog,
):
    """Releasing a compromised holder must free its OS lock without
    unlinking the path: after the external swap the path belongs to the
    competitor, and unlinking it would destroy that holder's lock.
    """
    holder = portalocker.TemporaryFileLock(tmpfile)
    held_fh = holder.acquire()
    os.unlink(tmpfile)  # a third party cleans up the "stale" lock file

    competitor = portalocker.TemporaryFileLock(tmpfile)
    competitor.acquire()
    try:
        with pytest.raises(portalocker.LockException, match='unlink'):
            holder.acquire()
        with caplog.at_level(logging.WARNING, logger='portalocker.utils'):
            holder.release()
        assert os.path.isfile(tmpfile), (
            'the compromised release unlinked the competitor file'
        )
        assert held_fh.closed, 'the OS lock was not freed'
        assert holder.fh is None
        assert any(
            'not unlinking' in record.getMessage() for record in caplog.records
        ), 'expected a warning about the skipped unlink'
    finally:
        competitor.release()
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

    `LockBase` has no garbage collection finalizer (4.2.0 removed it)
    and the subprocess disables the cycle collector besides, so only
    the module level atexit hook can perform the cleanup.
    """
    lock_path: pathlib.Path = tmp_path / 'held.lock'
    script: str = textwrap.dedent(
        f"""\
        import gc

        import portalocker

        # `LockBase` has no finalizer (4.2.0 removed it) and the cycle
        # collector is off besides, so only the atexit hook can clean
        # up.
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

        lock = portalocker.TemporaryFileLock({str(lock_path)!r})
        lock.acquire()

        pid = os.fork()
        if pid == 0:
            # The child exits immediately: `LockBase` has no garbage
            # collection finalizer (4.2.0 removed it), so its normal
            # exit exercises the atexit path alone.
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


@pytest.mark.skipif(os.name == 'nt', reason='os.fork is POSIX-only')
def test_atexit_hook_releases_lock_acquired_in_forked_child(
    tmp_path: pathlib.Path,
) -> None:
    """A child that acquires a parent-constructed lock owns it at exit.

    The worker-pool idiom constructs the lock before forking and acquires
    it inside the child. The exit hook used to record the owning pid at
    construction time, so the child's normal exit skipped the lock it
    legitimately held and left the lock file behind. Ownership is now
    recorded at acquire time.
    """
    lock_path: pathlib.Path = tmp_path / 'worker.lock'
    script: str = textwrap.dedent(
        f"""\
        import gc
        import os
        import sys

        import portalocker

        # Constructed in the parent, before the fork.
        lock = portalocker.TemporaryFileLock({str(lock_path)!r})

        pid = os.fork()
        if pid == 0:
            # `LockBase` has no finalizer (4.2.0 removed it) and the
            # cycle collector is off besides: the atexit path alone
            # must clean up after the child.
            gc.disable()
            lock.acquire()
            sys.exit(0)

        os.waitpid(pid, 0)
        if os.path.exists({str(lock_path)!r}):
            raise RuntimeError(
                "the child's exit left its own lock file behind"
            )

        contender = portalocker.TemporaryFileLock(
            {str(lock_path)!r}, timeout=0
        )
        contender.acquire()
        contender.release()
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


def test_temporaryfilelock_constructor_accepts_raise_on_release_error(
    tmpfile: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The constructor must accept and forward ``raise_on_release_error``
    instead of raising `TypeError`; strict mode used to require poking
    the attribute after construction.
    """
    lock = portalocker.TemporaryFileLock(
        tmpfile,
        raise_on_release_error=True,
    )
    assert lock.raise_on_release_error is True
    lock.acquire()
    _fail_unlink(monkeypatch)
    with pytest.raises(PermissionError):
        lock.release()
    monkeypatch.undo()
    assert lock.fh is None
    os.unlink(tmpfile)


def _patch_nt_release(
    monkeypatch: pytest.MonkeyPatch,
    unlink_errors: list[Exception],
) -> tuple[list[str], list[float]]:
    """Route release through the Windows path with scripted unlink errors.

    Returns the list of attempted unlink paths and the recorded sleeps.
    ``unlink_errors`` is consumed one error per attempt; once exhausted
    the real ``os.unlink`` runs.
    """
    attempts: list[str] = []
    sleeps: list[float] = []
    real_unlink = os.unlink

    def scripted_unlink(
        target: typing.Any,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> None:
        attempts.append(str(target))
        if unlink_errors:
            raise unlink_errors.pop(0)
        real_unlink(target, *args, **kwargs)

    monkeypatch.setattr(os, 'unlink', scripted_unlink)
    monkeypatch.setattr(time, 'sleep', sleeps.append)
    monkeypatch.setattr(os, 'name', 'nt')
    return attempts, sleeps


def test_temporaryfilelock_nt_release_retries_transient_denial(
    tmpfile: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Windows unlink retry must ride out a scanner holding the file
    briefly (two denials here) and still remove it.
    """
    lock = portalocker.TemporaryFileLock(tmpfile)
    lock.acquire()
    attempts, sleeps = _patch_nt_release(
        monkeypatch,
        [PermissionError('scanner holds it'), PermissionError('still held')],
    )
    lock.release()
    monkeypatch.undo()
    assert len(attempts) == 3
    assert len(sleeps) == 2
    assert not os.path.exists(tmpfile)
    assert lock.fh is None


def test_temporaryfilelock_nt_release_gives_up_without_final_sleep(
    tmpfile: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When every attempt is denied the last failure surfaces (strict
    mode) and no sleep follows the final attempt: sleeping after giving
    up only delays the caller for nothing.
    """
    lock = portalocker.TemporaryFileLock(
        tmpfile,
        raise_on_release_error=True,
    )
    lock.acquire()
    attempts, sleeps = _patch_nt_release(
        monkeypatch,
        [PermissionError(f'denied {n}') for n in range(5)],
    )
    with pytest.raises(PermissionError, match='denied 4'):
        lock.release()
    monkeypatch.undo()
    assert len(attempts) == 5
    assert len(sleeps) == 4, 'the release slept after its final attempt'
    assert lock.fh is None
    os.unlink(tmpfile)


def test_temporaryfilelock_nt_release_captures_other_oserror(
    tmpfile: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-retryable unlink failure must follow the flag contract like
    the POSIX path (suppressed and logged by default) instead of escaping
    ``release`` regardless of the flag as it did before 4.2.0. It is also
    not worth retrying, so one attempt suffices.
    """
    lock = portalocker.TemporaryFileLock(tmpfile)
    lock.acquire()
    attempts, sleeps = _patch_nt_release(
        monkeypatch,
        [IsADirectoryError('surprise directory')],
    )
    with caplog.at_level(logging.WARNING, logger=utils.logger.name):
        lock.release()
    monkeypatch.undo()
    assert len(attempts) == 1
    assert sleeps == []
    assert any(
        'suppressed error while removing' in record.message
        for record in caplog.records
    )
    assert lock.fh is None
    os.unlink(tmpfile)


def test_temporaryfilelock_nt_release_tolerates_vanished_file(
    tmpfile: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lock file that is already gone is fine on the Windows path too.

    The vanished file is staged as a scripted `FileNotFoundError`
    instead of a real pre-release unlink: on Windows the lock file is
    held open by the acquire and cannot be unlinked here (WinError 32),
    and the release meets the same exception either way. The genuinely
    unlinked file is covered by the POSIX-only variants above.
    """
    lock = portalocker.TemporaryFileLock(tmpfile)
    lock.acquire()
    attempts, sleeps = _patch_nt_release(
        monkeypatch,
        [FileNotFoundError(errno.ENOENT, 'already gone', tmpfile)],
    )
    lock.release()
    monkeypatch.undo()
    assert len(attempts) == 1
    assert sleeps == []
    assert lock.fh is None
    os.unlink(tmpfile)


def test_temporaryfilelock_acquire_retries_closed_handle(
    tmpfile: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reentrant release that claims and closes the freshly locked
    handle before the inode verification must make the verified acquire
    retry within its remaining budget, not leak a ``ValueError`` from
    ``fstat`` on a closed file.
    """
    real_acquire = utils.Lock.acquire
    handles: list[typing.IO[typing.Any]] = []

    def sabotaged_acquire(
        self: utils.Lock,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> typing.IO[typing.Any]:
        fh = real_acquire(self, *args, **kwargs)
        handles.append(fh)
        if len(handles) == 1:
            # The SIGTERM-handler shape: a reentrant release claims and
            # closes the handle right after the acquire returns it.
            utils.Lock.release(self)
        return fh

    monkeypatch.setattr(utils.Lock, 'acquire', sabotaged_acquire)
    lock = portalocker.TemporaryFileLock(
        tmpfile,
        timeout=1,
        check_interval=0.01,
        fail_when_locked=False,
    )
    fh = lock.acquire()
    monkeypatch.undo()

    assert len(handles) >= 2, 'the closed handle was not retried'
    assert not fh.closed
    assert lock.fh is fh
    lock.release()
    assert not os.path.exists(tmpfile)


@posix_inode_only
def test_temporaryfilelock_reacquire_with_closed_handle_is_compromised(
    tmpfile: str,
) -> None:
    """Re-acquiring while the held handle is closed must report the
    documented compromised-lock ``LockException``: a closed handle
    certainly no longer guards the path, and the inode comparison used
    to leak a raw ``ValueError`` from ``fileno()`` instead.
    """
    lock = portalocker.TemporaryFileLock(tmpfile)
    fh = lock.acquire()
    fh.close()  # a stray close: the OS lock died with the descriptor
    with pytest.raises(portalocker.LockException, match='compromised'):
        lock.acquire()
    lock.release()


@posix_inode_only
def test_fh_matches_path_reports_dead_descriptor(
    tmp_path: pathlib.Path,
) -> None:
    """A descriptor closed at the OS level under a still-open file object
    (``fileno()`` succeeds, ``fstat`` raises ``EBADF``) certainly no
    longer guards the path, so the comparison must say so instead of
    leaking the raw ``OSError``.
    """
    path = str(tmp_path / 'dead.fd')
    fd: int = os.open(path, os.O_RDWR | os.O_CREAT)
    fh = os.fdopen(fd, 'w')
    os.close(fh.fileno())  # the descriptor dies under the object
    assert utils._fh_matches_path(typing.cast(typing.Any, fh), path) is False
    with contextlib.suppress(OSError):
        fh.close()


@posix_inode_only
def test_fh_matches_path_propagates_other_oserrors(
    tmpfile: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the dead-descriptor errno is translated: an I/O error from
    the stat itself is real news and must propagate.
    """
    with open(tmpfile, 'w') as fh:

        def failing_fstat(fd: int) -> os.stat_result:
            raise OSError(errno.EIO, 'disk on fire')

        monkeypatch.setattr(os, 'fstat', failing_fstat)
        with pytest.raises(OSError, match='disk on fire'):
            utils._fh_matches_path(typing.cast(typing.Any, fh), tmpfile)
        monkeypatch.undo()
