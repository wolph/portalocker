"""Tests for the BoundedSemaphore helper."""

import pathlib
import random
import subprocess
import sys
import textwrap
import time

import pytest

import portalocker
from portalocker import utils


@pytest.mark.parametrize('timeout', [None, 0, 0.001])
@pytest.mark.parametrize('check_interval', [None, 0, 0.0005])
def test_bounded_semaphore(timeout, check_interval, monkeypatch):
    """Ensure that the semaphore honours *maximum*, *timeout* and
    *check_interval* and raises AlreadyLocked when exhausted.
    """
    n = 2
    name: str = str(random.random())
    monkeypatch.setattr(utils, 'DEFAULT_TIMEOUT', 0.0001)
    monkeypatch.setattr(utils, 'DEFAULT_CHECK_INTERVAL', 0.0005)

    semaphore_a = portalocker.BoundedSemaphore(n, name=name, timeout=timeout)
    semaphore_b = portalocker.BoundedSemaphore(n, name=name, timeout=timeout)
    semaphore_c = portalocker.BoundedSemaphore(n, name=name, timeout=timeout)

    semaphore_a.acquire(timeout=timeout)
    semaphore_b.acquire()
    with pytest.raises(portalocker.AlreadyLocked):
        semaphore_c.acquire(check_interval=check_interval, timeout=timeout)

    semaphore_c.acquire(
        check_interval=check_interval,
        timeout=timeout,
        fail_when_locked=False,
    )


def test_bounded_semaphore_recovers_after_acquire_error(tmp_path):
    """A3: a non-AlreadyLocked failure (e.g. a missing directory raising
    FileNotFoundError) must not leave ``self.lock`` set. Otherwise the
    ``assert not self.lock`` guard bricks the instance for every later
    acquire.
    """
    missing = tmp_path / 'missing'
    semaphore = portalocker.NamedBoundedSemaphore(
        1,
        name='recover',
        directory=str(missing),
        timeout=0,
    )

    with pytest.raises(FileNotFoundError):
        semaphore.acquire()
    assert semaphore.lock is None, 'a failed acquire must not leak self.lock'

    # Create the directory the second time around; the SAME instance must now
    # acquire cleanly instead of raising AssertionError.
    missing.mkdir()
    lock = semaphore.acquire()
    assert lock is not None
    semaphore.release()


def test_bounded_semaphore_full_waits_out_timeout_then_raises() -> None:
    """A full semaphore with ``fail_when_locked=True`` (the default) must
    retry for the whole timeout before raising ``AlreadyLocked``.

    This wait-then-raise timing has been the behaviour since 3.2.0 and
    diverges from the fail-fast handling of the other lock classes. The
    4.1.0 constructor docstring wrongly promised fail-fast, so this test
    pins the real contract with a measured lower bound.
    """
    name: str = str(random.random())
    holder_a = portalocker.BoundedSemaphore(2, name=name)
    holder_b = portalocker.BoundedSemaphore(2, name=name)
    holder_a.acquire()
    holder_b.acquire()

    contender = portalocker.BoundedSemaphore(
        2,
        name=name,
        timeout=0.3,
        check_interval=0.05,
    )
    start: float = time.perf_counter()
    with pytest.raises(portalocker.AlreadyLocked):
        contender.acquire()
    elapsed: float = time.perf_counter() - start
    assert elapsed >= 0.29, (
        'fail_when_locked=True must wait out the whole timeout first'
    )

    holder_a.release()
    holder_b.release()


def test_bounded_semaphore_returns_none_after_timeout() -> None:
    """With ``fail_when_locked=False`` a full semaphore must keep retrying
    for the whole timeout and then return ``None``, the documented legacy
    contract (every other lock raises).
    """
    name: str = str(random.random())
    holder = portalocker.BoundedSemaphore(1, name=name)
    holder.acquire()

    contender = portalocker.BoundedSemaphore(
        1,
        name=name,
        timeout=0.1,
        check_interval=0.01,
    )
    result = contender.acquire(fail_when_locked=False)
    assert result is None

    holder.release()


def test_bounded_semaphore_double_acquire_raises_and_keeps_slot() -> None:
    """Acquiring an instance that already holds a slot must raise
    ``LockException`` without consuming a second slot or dropping the
    first one.
    """
    name: str = str(random.random())
    semaphore = portalocker.BoundedSemaphore(2, name=name)
    first = semaphore.acquire()
    assert first is not None

    with pytest.raises(portalocker.LockException, match='Already locked'):
        semaphore.acquire()

    # The held slot survived the failed call untouched.
    assert semaphore.lock is first

    # Only one of the two slots is consumed: a competitor still gets one.
    competitor = portalocker.BoundedSemaphore(2, name=name, timeout=0)
    assert competitor.acquire() is not None

    competitor.release()
    semaphore.release()

    # After release the instance is usable again.
    assert semaphore.acquire() is not None
    semaphore.release()


def test_bounded_semaphore_double_acquire_guard_survives_optimization(
    tmp_path: pathlib.Path,
) -> None:
    """The double-acquire guard must hold under ``python -O`` as well: a
    plain ``assert`` would be stripped and silently leak the first slot.
    """
    script: str = textwrap.dedent(
        f"""\
        import portalocker

        semaphore = portalocker.NamedBoundedSemaphore(
            2, name='guard', directory={str(tmp_path)!r}
        )
        first = semaphore.acquire()
        if first is None:
            raise RuntimeError('first acquire failed')
        try:
            semaphore.acquire()
        except portalocker.LockException:
            pass
        else:
            raise RuntimeError('missing LockException under optimized Python')
        if semaphore.lock is not first:
            raise RuntimeError('the held slot was dropped or replaced')
        """,
    )
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, '-O', '-c', script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout == ''
    assert completed.stderr == ''


def test_bounded_semaphore_deprecation_warning_names_the_caller() -> None:
    """The default-name ``DeprecationWarning`` must point at the caller,
    not at portalocker's own source, so it deduplicates per call site.
    """
    with pytest.warns(DeprecationWarning) as records:
        portalocker.BoundedSemaphore(1)

    assert len(records) == 1
    assert records[0].filename == __file__
