"""Regression tests for concurrent and reentrant lock state transitions.

Every test here stages a historical race deterministically: the vulnerable
window is held open with events or reentrant callbacks instead of hoping a
scheduler hits it. The bugs they pin down:

- Two concurrent ``release()`` calls on one ``Lock`` both passed the
  ``self.fh`` guard, and the loser ran the OS unlock on a closed (and
  possibly reused) file descriptor, silently dropping an unrelated lock.
- A ``release()`` reentering from a signal handler between the ownership
  guard and the unlink of ``TemporaryFileLock`` / ``PidFileLock`` unlinked
  the files of whoever acquired the lock in between.
- A ``BaseException`` escaping ``fh.close()`` during release left
  ``self.fh`` set, so the next release passed the guard again.
- ``RLock``'s acquire count was a bare read-modify-write, losing updates
  when two threads shared one instance.
"""

from __future__ import annotations

import os
import signal
import threading
import time
import typing

import pytest

import portalocker
from portalocker import types, utils

posix_release_ordering = pytest.mark.skipif(
    os.name == 'nt',
    reason='stages the POSIX unlink-while-held release ordering',
)


def test_lock_concurrent_release_unlocks_once(
    tmpfile: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent ``release()`` calls must unlock and close only once.

    The first releaser is parked inside the OS unlock, holding open the
    window that is a few bytecodes wide in the wild. The second release
    must see the claimed state and return without touching the (stale)
    filehandle, instead of unlocking whatever lock the recycled file
    descriptor number points at by then.
    """
    lock = portalocker.Lock(tmpfile, timeout=0)
    lock.acquire()

    unlock_calls: list[int] = []
    parked = threading.Event()
    resume = threading.Event()
    real_unlock = portalocker.portalocker.unlock

    def gated_unlock(fh: types.IO) -> None:
        unlock_calls.append(fh.fileno())
        if len(unlock_calls) == 1:
            parked.set()
            assert resume.wait(timeout=5), 'the releaser was never resumed'
        real_unlock(fh)

    monkeypatch.setattr(portalocker.portalocker, 'unlock', gated_unlock)

    releaser = threading.Thread(target=lock.release)
    releaser.start()
    assert parked.wait(timeout=5), 'the first release never reached unlock'

    # The concurrent release must claim nothing and unlock nothing.
    lock.release()
    assert len(unlock_calls) == 1, 'the losing release unlocked a stale fd'
    assert lock.fh is None

    resume.set()
    releaser.join(timeout=5)
    assert not releaser.is_alive()
    assert len(unlock_calls) == 1


class _InterruptingClose:
    """Filehandle proxy whose first ``close()`` raises KeyboardInterrupt.

    ``BufferedWriter.flush`` checks for pending signals, so a real ^C can
    surface exactly there; this proxy makes that moment repeatable.
    """

    def __init__(self, fh: types.IO) -> None:
        self._fh = fh
        self.interrupted = False

    def __getattr__(self, name: str) -> typing.Any:
        return getattr(self._fh, name)

    def close(self) -> None:
        if not self.interrupted:
            self.interrupted = True
            raise KeyboardInterrupt
        self._fh.close()


def test_lock_release_clears_state_before_close_interrupt(
    tmpfile: str,
) -> None:
    """A ``KeyboardInterrupt`` escaping ``fh.close()`` must not leave
    ``self.fh`` set: the handle is claimed before any OS call runs, so a
    later release is a no-op instead of a second teardown.
    """
    lock = portalocker.Lock(tmpfile, timeout=0)
    fh = lock.acquire()
    proxy = _InterruptingClose(fh)
    # Annotated as optional so mypy does not narrow `lock.fh` to a plain
    # IO and declare the `is None` assertions below unreachable.
    proxy_fh: types.IO | None = typing.cast(types.IO, proxy)
    lock.fh = proxy_fh

    with pytest.raises(KeyboardInterrupt):
        lock.release()

    assert lock.fh is None, 'the interrupted release left the handle behind'
    # The second release must be a clean no-op.
    lock.release()
    fh.close()


@posix_release_ordering
def test_temporaryfilelock_close_interrupt_cannot_double_unlink(
    tmpfile: str,
) -> None:
    """After a ``KeyboardInterrupt`` inside ``close()`` the lock must not
    unlink the path again on the next release: by then the path belongs
    to a successor.
    """
    lock = portalocker.TemporaryFileLock(tmpfile)
    fh = lock.acquire()
    proxy = _InterruptingClose(fh)
    # Annotated as optional so mypy does not narrow `lock.fh` to a plain
    # IO and declare the `is None` assertions below unreachable.
    proxy_fh: types.IO | None = typing.cast(types.IO, proxy)
    lock.fh = proxy_fh

    with pytest.raises(KeyboardInterrupt):
        lock.release()

    assert lock.fh is None, 'the interrupted release left the handle behind'
    # A successor recreates the path; the stale wrapper must leave it be.
    with open(tmpfile, 'w') as successor_file:
        successor_file.write('successor')
    lock.release()
    assert os.path.exists(tmpfile), 'the successor file was unlinked'
    fh.close()
    os.unlink(tmpfile)


@posix_release_ordering
def test_temporaryfilelock_release_is_reentrancy_safe(
    tmpfile: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``release()`` reentering at the unlink point must be a no-op.

    This stages the SIGTERM-handler shape: the handler calls
    ``lock.release()`` while the outer release sits right before its
    ``os.unlink``. The reentrant call must find the state already claimed,
    releasing nothing, and the OS lock must still exclude a successor for
    the whole window.
    """
    lock = portalocker.TemporaryFileLock(tmpfile)
    lock.acquire()

    real_unlink = os.unlink
    unlink_calls: list[str] = []
    window_outcomes: list[str] = []

    def staged_unlink(
        path: typing.Any,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> None:
        unlink_calls.append(str(path))
        if len(unlink_calls) == 1:
            # The graceful-shutdown handler fires inside the window.
            lock.release()
            # A successor rushing in during the window must stay excluded:
            # the OS lock is only free once the outer release finishes.
            successor = portalocker.Lock(
                tmpfile,
                timeout=0,
                fail_when_locked=True,
            )
            try:
                successor.acquire()
            except portalocker.AlreadyLocked:
                window_outcomes.append('excluded')
            else:  # pragma: no cover - only reached when the bug is back
                window_outcomes.append('stolen')
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, 'unlink', staged_unlink)
    lock.release()
    monkeypatch.undo()

    assert unlink_calls == [lock.filename], (
        f'the reentrant release unlinked on its own: {unlink_calls}'
    )
    assert window_outcomes == ['excluded']
    assert lock.fh is None
    assert not os.path.exists(tmpfile)


@posix_release_ordering
def test_pidfilelock_release_is_reentrancy_safe(
    tmp_path: typing.Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `PidFileLock` twin of the reentrant-release test: the handler's
    reentrant ``release()`` must not unlink the PID or sidecar files, and
    the sidecar lock must keep excluding a successor until the outer
    release finishes.
    """
    pid_file = str(tmp_path / 'reentrant.pid')
    lock = portalocker.PidFileLock(pid_file)
    lock.acquire()

    real_unlink = os.unlink
    unlink_calls: list[str] = []
    window_outcomes: list[str] = []

    def staged_unlink(
        path: typing.Any,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> None:
        unlink_calls.append(str(path))
        if len(unlink_calls) == 1:
            lock.release()
            successor = portalocker.PidFileLock(
                pid_file,
                timeout=0,
                fail_when_locked=True,
            )
            try:
                successor.acquire()
            except portalocker.AlreadyLocked:
                window_outcomes.append('excluded')
            else:  # pragma: no cover - only reached when the bug is back
                window_outcomes.append('stolen')
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, 'unlink', staged_unlink)
    lock.release()
    monkeypatch.undo()

    assert unlink_calls == [pid_file, f'{pid_file}.lock'], (
        f'the reentrant release unlinked on its own: {unlink_calls}'
    )
    assert window_outcomes == ['excluded']
    assert lock._inner_lock is None

    # With the lock fully released a successor works normally again.
    successor = portalocker.PidFileLock(pid_file)
    successor.acquire()
    assert successor.read_pid() == os.getpid()
    successor.release()


class _GatedCountRLock(portalocker.RLock):
    """`RLock` whose acquire-count reads can park one designated thread.

    The second read the designated thread performs is the read half of the
    ``_acquire_count += 1`` read-modify-write (the first is the reentrancy
    guard). Parking there holds the historical lost-update window open.
    """

    def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        # Ordinary attributes, created before `super().__init__` runs the
        # first `_acquire_count = 0` assignment through the property.
        self._count_store: dict[str, int] = {}
        self._gate_thread: int | None = None
        self._gated_reads = 0
        self.parked = threading.Event()
        self.resume = threading.Event()
        super().__init__(*args, **kwargs)

    @property
    def _acquire_count(self) -> int:  # pyrefly: ignore[bad-override]
        value: int = self._count_store['value']
        if threading.get_ident() == self._gate_thread:
            self._gated_reads += 1
            if self._gated_reads == 2:
                self.parked.set()
                assert self.resume.wait(timeout=5), 'never resumed'
        return value

    @_acquire_count.setter
    def _acquire_count(self, value: int) -> None:
        self._count_store['value'] = value


def test_rlock_counter_survives_interleaved_nested_acquires(
    tmpfile: str,
) -> None:
    """Two threads nesting acquires on one held `RLock` must both count.

    One thread is parked between the read and the write of its
    ``_acquire_count += 1``. The other thread's whole increment lands
    inside that window. Without the state lock the parked thread then
    wrote its stale value back, losing the other increment, and the later
    releases closed the file while a hold was still outstanding.
    """
    lock = _GatedCountRLock(tmpfile, timeout=0)
    lock.acquire()

    def gated_nested_acquire() -> None:
        lock._gate_thread = threading.get_ident()
        lock.acquire()

    racer = threading.Thread(target=gated_nested_acquire)
    racer.start()
    assert lock.parked.wait(timeout=5), 'the racer never reached the gate'

    other = threading.Thread(target=lock.acquire)
    other.start()
    # With the fix the other thread blocks on the state lock; without it
    # this join gives its unguarded increment ample time to finish inside
    # the parked thread's read-modify-write window.
    other.join(timeout=1)

    lock.resume.set()
    racer.join(timeout=5)
    other.join(timeout=5)
    assert not racer.is_alive()
    assert not other.is_alive()

    assert lock._acquire_count == 3, 'a nested acquire was lost'
    lock.release()
    lock.release()
    lock.release()
    assert lock.fh is None


@pytest.mark.skipif(os.name == 'nt', reason='os.fork is POSIX-only')
def test_forked_child_survives_inherited_held_state_lock(tmpfile: str) -> None:
    """A child forked while another thread holds an instance state lock
    must not deadlock on it.

    The child inherits the locked ``RLock`` owned by a thread that does
    not exist in the child, so without the ``os.register_at_fork`` reinit
    every later ``release()``, ``acquire()`` or atexit-hook call in the
    child hung forever. The window is held open deterministically by a
    thread parked inside the state lock across the fork.
    """
    lock = portalocker.Lock(tmpfile, timeout=0.1)
    lock.acquire()
    inside = threading.Event()
    gate = threading.Event()

    def hold_state_lock() -> None:
        with lock._state_lock:
            inside.set()
            gate.wait(timeout=10)

    holder = threading.Thread(target=hold_state_lock, daemon=True)
    holder.start()
    assert inside.wait(timeout=5), 'the holder never took the state lock'

    pid: int = os.fork()
    if pid == 0:  # pragma: no cover - child process, exits via os._exit
        lock.release()
        os._exit(0)

    deadline: float = time.monotonic() + 5
    status: int | None = None
    while time.monotonic() < deadline:
        waited, waitstatus = os.waitpid(pid, os.WNOHANG)
        if waited:
            status = os.waitstatus_to_exitcode(waitstatus)
            break
        time.sleep(0.01)
    gate.set()
    holder.join(timeout=5)
    if status is None:  # pragma: no cover - only reached when the bug is back
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        pytest.fail('the forked child hung on the inherited state lock')
    assert status == 0
    lock.release()


def test_state_lock_reinit_hook_keeps_locks_usable(tmpfile: str) -> None:
    """The after-fork reinit hook must leave every registered lock with a
    working state lock. Called directly here, since a forked child's
    coverage never reaches the parent's report. The call must also
    succeed on Windows, where CPython's lock types lack
    ``_at_fork_reinit`` (it only exists on builds with ``fork``) and the
    hook is a documented no-op.
    """
    lock = portalocker.Lock(tmpfile, timeout=0)
    assert lock in utils._live_locks
    utils._reinit_state_locks_after_fork()
    lock.acquire()
    lock.release()
    assert lock.fh is None


def test_state_lock_reinit_hook_tolerates_missing_reinit(
    tmpfile: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reinit hook must skip state locks without ``_at_fork_reinit``.

    CPython only compiles ``_at_fork_reinit`` into its lock types on
    platforms with ``fork``, so on Windows the method does not exist at
    all. The registered hook can never fire there (nothing forks), but a
    direct call must be a quiet no-op instead of an ``AttributeError``.
    The Windows shape is staged on every platform by giving one
    registered lock a state lock without the method.
    """
    lock = portalocker.Lock(tmpfile, timeout=0)
    assert lock in utils._live_locks
    monkeypatch.setattr(lock, '_state_lock', object())
    utils._reinit_state_locks_after_fork()


def test_rlock_release_claims_handle_atomically_with_count(
    tmpfile: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final release must zero the count and claim the handle in one
    state-lock scope.

    Splitting them let a racing acquire slip between the scopes: it saw
    the count at zero, took the fast path and returned the still
    published filehandle, which the releasing thread then claimed and
    closed. The caller was left holding a closed handle while the OS
    lock was free for anybody. The releaser is parked at its claim to
    hold that window open.
    """
    lock = portalocker.RLock(tmpfile, timeout=5, check_interval=0.01)
    lock.acquire()

    parked = threading.Event()
    resume = threading.Event()
    real_claim = utils.Lock._claim_fh

    def gated_claim(self: utils.Lock) -> types.IO | None:
        if threading.current_thread().name == 'releaser':
            parked.set()
            assert resume.wait(timeout=5), 'the releaser was never resumed'
        return real_claim(self)

    monkeypatch.setattr(utils.Lock, '_claim_fh', gated_claim)

    releaser = threading.Thread(target=lock.release, name='releaser')
    releaser.start()
    assert parked.wait(timeout=5), 'the release never reached its claim'

    result: dict[str, typing.Any] = {}

    def reacquire() -> None:
        result['fh'] = lock.acquire()

    acquirer = threading.Thread(target=reacquire)
    acquirer.start()
    # With the fix the acquirer blocks on the state lock the releaser
    # still holds; without it this join gives its fast path ample time
    # to return the handle the releaser is about to close.
    acquirer.join(timeout=0.5)

    resume.set()
    releaser.join(timeout=5)
    acquirer.join(timeout=5)
    assert not releaser.is_alive()
    assert not acquirer.is_alive()

    fh = result['fh']
    assert not fh.closed, 'acquire handed out a handle the release closed'
    assert lock.fh is fh
    assert lock._acquire_count == 1

    probe = portalocker.Lock(tmpfile, timeout=0, fail_when_locked=True)
    with pytest.raises(portalocker.AlreadyLocked):
        probe.acquire()

    lock.release()
    assert fh is not None
    assert fh.closed
