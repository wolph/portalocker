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
from portalocker import exceptions


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
    not hasattr(os, 'chflags'),
    reason='os.chflags is not available on this platform',
)
def test_prepare_fh_failure_append_only_file(tmpfile: str) -> None:
    """The real trigger: mode ``w`` on an append-only (uappnd) file.

    ``open(mode='a')`` and the lock both succeed, then the deferred
    truncate fails with ``EPERM``. The failed acquire must not leave the
    file locked behind the escaping traceback.
    """
    pathlib.Path(tmpfile).write_text('precious append-only data')
    try:
        os.chflags(tmpfile, stat.UF_APPEND)
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
        os.chflags(tmpfile, 0)


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
