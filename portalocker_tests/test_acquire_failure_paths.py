"""Failure paths of ``Lock.acquire``: cleanup and error classification.

Two clusters of behaviour are pinned down here:

- A failure *after* the lock was taken (``_prepare_fh`` raising) must
  unlock and close the filehandle before the error escapes, so a
  traceback that keeps the exception alive cannot pin an OS lock.
- Only contention (``AlreadyLocked``) is worth retrying. A plain
  ``LockException`` means the backend cannot lock this file at all
  (unsupported filesystem, ``ENOLCK``), so it must fail fast and must
  not be misreported as "someone holds this lock".
"""

import os
import pathlib
import stat
import time
import typing

import pytest

import portalocker
from portalocker import exceptions, utils

# `os.chflags` only exists on BSD-family platforms, and recent typeshed
# only declares it there, so a direct `os.chflags(...)` call fails the
# type checkers on the Linux CI runners. The `getattr` alias mirrors the
# runtime `skipif` guard at the type level.
_chflags: typing.Callable[[str, int], None] | None = getattr(
    os,
    'chflags',
    None,
)


def test_prepare_fh_failure_releases_lock(
    tmpfile: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``_prepare_fh`` error must unlock and close the filehandle.

    The captured exception (via ``pytest.raises``) keeps the traceback
    and therefore the filehandle alive, exactly like a real caller's
    error handling would. The contender below only succeeds when the
    failed acquire explicitly unlocked and closed the handle.
    """
    lock = portalocker.Lock(tmpfile, mode='w', timeout=0)

    def broken_prepare(fh: typing.IO[typing.Any]) -> typing.IO[typing.Any]:
        raise PermissionError('simulated append-only file')

    monkeypatch.setattr(lock, '_prepare_fh', broken_prepare)

    with pytest.raises(PermissionError) as exc_info:
        lock.acquire()

    # The original error escapes unwrapped and the lock holds nothing.
    assert not isinstance(exc_info.value, exceptions.LockException)
    assert lock.fh is None

    # A second contender must acquire immediately, while the traceback
    # above still pins the failed handle in memory.
    contender = portalocker.Lock(
        tmpfile,
        mode='a',
        timeout=0,
        fail_when_locked=True,
    )
    contender.acquire()
    contender.release()


@pytest.mark.skipif(
    _chflags is None,
    reason='os.chflags is not available on this platform',
)
def test_prepare_fh_failure_append_only_file(tmpfile: str) -> None:
    """The real trigger: mode ``w`` on an append-only (uappnd) file.

    ``open(mode='a')`` and the lock both succeed, then the deferred
    truncate fails with ``EPERM``. The failed acquire must not leave the
    file locked behind the escaping traceback.
    """
    assert _chflags is not None  # the skipif above guarantees it
    pathlib.Path(tmpfile).write_text('precious append-only data')
    try:
        _chflags(tmpfile, stat.UF_APPEND)
    except OSError:  # pragma: no cover - filesystem dependent
        pytest.skip('filesystem does not support chflags uappnd')

    try:
        lock = portalocker.Lock(tmpfile, mode='w', timeout=0)
        with pytest.raises(PermissionError) as exc_info:
            lock.acquire()

        assert exc_info.value.errno == 1  # EPERM
        assert lock.fh is None

        # Appending is still allowed, so a contender in append mode must
        # acquire immediately after the failure.
        contender = portalocker.Lock(
            tmpfile,
            mode='a',
            timeout=0,
            fail_when_locked=True,
        )
        contender.acquire()
        contender.release()
    finally:
        _chflags(tmpfile, 0)


def test_non_contention_error_fails_fast(
    tmpfile: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain ``LockException`` is permanent: no retrying, no wrapping."""
    attempts: list[int] = []

    def unsupported_lock(
        fh: typing.IO[typing.Any],
        flags: portalocker.LockFlags,
    ) -> None:
        attempts.append(1)
        raise exceptions.LockException(
            exceptions.LockException.LOCK_FAILED,
            'Operation not supported',
        )

    monkeypatch.setattr('portalocker.utils.portalocker.lock', unsupported_lock)

    lock = portalocker.Lock(tmpfile, timeout=2, check_interval=0.5)
    start: float = time.perf_counter()
    with pytest.raises(exceptions.LockException) as exc_info:
        lock.acquire()
    elapsed: float = time.perf_counter() - start

    assert elapsed < 1.0, 'permanent lock errors must not burn the timeout'
    assert len(attempts) == 1
    assert type(exc_info.value) is exceptions.LockException
    assert lock.fh is None


def test_non_contention_error_is_not_already_locked(
    tmpfile: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``fail_when_locked`` must not dress a backend failure as contention."""

    def unsupported_lock(
        fh: typing.IO[typing.Any],
        flags: portalocker.LockFlags,
    ) -> None:
        raise exceptions.LockException(
            exceptions.LockException.LOCK_FAILED,
            'No locks available',
        )

    monkeypatch.setattr('portalocker.utils.portalocker.lock', unsupported_lock)

    lock = portalocker.Lock(tmpfile, timeout=0, fail_when_locked=True)
    with pytest.raises(exceptions.LockException) as exc_info:
        lock.acquire()

    assert not isinstance(exc_info.value, exceptions.AlreadyLocked)
    assert lock.fh is None


def test_contention_still_retries(
    tmpfile: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real contention (``AlreadyLocked``) keeps retrying until timeout."""
    attempts: list[int] = []

    def contended_lock(
        fh: typing.IO[typing.Any],
        flags: portalocker.LockFlags,
    ) -> None:
        attempts.append(1)
        raise exceptions.AlreadyLocked(
            exceptions.LockException.LOCK_FAILED,
            'Resource temporarily unavailable',
        )

    monkeypatch.setattr('portalocker.utils.portalocker.lock', contended_lock)

    lock = portalocker.Lock(tmpfile, timeout=0.2, check_interval=0.01)
    with pytest.raises(exceptions.AlreadyLocked):
        lock.acquire()

    assert len(attempts) > 1, 'contention must still be retried'
    assert lock.fh is None


def test_acquire_interrupt_in_retry_sleep_closes_fd(
    tmpfile: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``KeyboardInterrupt`` in the retry sleep (^C while waiting on a
    contended lock) must close the descriptor the acquire opened, instead
    of leaking it for as long as the traceback stays referenced.
    """
    holder = portalocker.Lock(tmpfile, timeout=0)
    holder.acquire()

    waiter = portalocker.Lock(tmpfile, timeout=10, check_interval=0.01)
    opened: list[typing.IO[typing.Any]] = []
    real_get_fh = portalocker.Lock._get_fh

    def recording_get_fh(self: portalocker.Lock) -> typing.IO[typing.Any]:
        fh = real_get_fh(self)
        opened.append(fh)
        return fh

    def interrupting_sleep(seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(portalocker.Lock, '_get_fh', recording_get_fh)
    monkeypatch.setattr(time, 'sleep', interrupting_sleep)

    with pytest.raises(KeyboardInterrupt):
        waiter.acquire()

    assert len(opened) == 1
    assert opened[0].closed, 'the interrupted acquire leaked its descriptor'
    assert waiter.fh is None
    holder.release()


def test_acquire_interrupt_in_prepare_unlocks_and_closes(
    tmpfile: str,
) -> None:
    """A ``KeyboardInterrupt`` between a successful lock and the
    publication on ``self.fh`` (staged inside ``_prepare_fh``) must give
    the OS lock back and close the descriptor. It used to leave the lock
    held by an untracked descriptor, with ``release`` a silent no-op.
    """
    opened: list[typing.IO[typing.Any]] = []

    class InterruptedPrepare(portalocker.Lock):
        def _prepare_fh(
            self,
            fh: typing.IO[typing.Any],
        ) -> typing.IO[typing.Any]:
            opened.append(fh)
            raise KeyboardInterrupt

    waiter = InterruptedPrepare(tmpfile, timeout=0)
    with pytest.raises(KeyboardInterrupt):
        waiter.acquire()

    assert waiter.fh is None
    assert len(opened) == 1
    assert opened[0].closed, 'the interrupted acquire leaked its descriptor'

    # The OS lock must be free again: an untracked holder would make this
    # probe raise AlreadyLocked.
    probe = portalocker.Lock(tmpfile, timeout=0, fail_when_locked=True)
    probe.acquire()
    probe.release()


def test_pidfilelock_interrupt_after_sidecar_lock_rolls_back(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``KeyboardInterrupt`` landing inside the verified sidecar acquire
    after the OS lock was taken must roll the sidecar back before the
    interrupt propagates: a pinned traceback keeps the frame (and with it
    the sidecar lock object) alive, so refcounting would never free the
    lock and every contender would stay blocked.
    """
    pid_file = str(tmp_path / 'interrupted.pid')
    real_verified = utils.TemporaryFileLock._acquire_verified

    def interrupted_verified(
        lock: utils.Lock,
        filename: str,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> typing.IO[typing.Any]:
        real_verified(lock, filename, *args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        utils.PidFileLock,
        '_acquire_verified',
        staticmethod(interrupted_verified),
    )
    lock = utils.PidFileLock(pid_file)
    with pytest.raises(KeyboardInterrupt) as interrupt_info:
        lock.acquire()

    assert lock._inner_lock is None
    assert lock._acquired_lock is False

    # `interrupt_info` pins the traceback, and with it the acquire frame
    # and the local sidecar lock object, exactly like an error handler
    # that stores the exception. Refcounting therefore cannot free the
    # sidecar behind our back: only an explicit rollback can.
    assert interrupt_info.value.__traceback__ is not None
    monkeypatch.undo()
    successor = utils.PidFileLock(pid_file)
    successor.acquire()
    assert successor.read_pid() == os.getpid()
    successor.release()
