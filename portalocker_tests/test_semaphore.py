"""Tests for the BoundedSemaphore helper."""

import random

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
