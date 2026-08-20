import time

import pytest

import portalocker


def test_with_timeout(tmpdir):
    """Test that AlreadyLocked is raised when a lock is already held."""
    tmpfile = tmpdir.join('test_with_timeout.lock')
    # Open the file 2 times
    with pytest.raises(portalocker.AlreadyLocked):  # noqa: SIM117
        with portalocker.Lock(tmpfile, timeout=0.1) as fh:
            print('writing some stuff to my cache...', file=fh)
            with portalocker.Lock(
                tmpfile,
                timeout=0.1,
                mode='wb',
                fail_when_locked=True,
            ):
                pass
            print('writing more stuff to my cache...', file=fh)


def test_without_timeout(tmpdir):
    """
    Test that LockException is raised when a file is locked without a
    timeout.
    """
    tmpfile = tmpdir.join('test_without_timeout.lock')
    # Open the file 2 times
    with pytest.raises(portalocker.LockException):  # noqa: SIM117
        with portalocker.Lock(tmpfile, timeout=None) as fh:
            print('writing some stuff to my cache...', file=fh)
            with portalocker.Lock(tmpfile, timeout=None, mode='w'):
                pass
            print('writing more stuff to my cache...', file=fh)


def test_without_fail(tmpdir):
    """Test that LockException is raised when fail_when_locked is False."""
    tmpfile = tmpdir.join('test_without_fail.lock')
    # Open the file 2 times
    with pytest.raises(portalocker.LockException):  # noqa: SIM117
        with portalocker.Lock(tmpfile, timeout=0.1) as fh:
            print('writing some stuff to my cache...', file=fh)
            lock = portalocker.Lock(tmpfile, timeout=0.1)
            lock.acquire(check_interval=0.05, fail_when_locked=False)


def test_timeout_generator_caps_sleep_at_deadline(tmpfile):
    """The generator gives up at the timeout, not one interval later."""
    lock = portalocker.Lock(tmpfile)
    start: float = time.perf_counter()
    attempts: list[int] = list(lock._timeout_generator(0.5, 3))
    elapsed: float = time.perf_counter() - start

    assert attempts[0] == 0
    assert elapsed < 1.0, 'sleep must be capped at the remaining deadline'


def test_contended_lock_gives_up_at_timeout(tmpfile):
    """A contended acquire returns at the timeout despite a huge interval."""
    holder = portalocker.Lock(tmpfile, timeout=0)
    holder.acquire()
    try:
        waiter = portalocker.Lock(tmpfile, timeout=0.5, check_interval=3)
        start: float = time.perf_counter()
        with pytest.raises(portalocker.LockException):
            waiter.acquire()
        elapsed: float = time.perf_counter() - start
        assert elapsed < 1.0, 'timeout=0.5 must not stretch to 3s'
    finally:
        holder.release()


def test_timeout_generator_zero_timeout_single_attempt(tmpfile):
    """A timeout of zero still buys exactly one attempt."""
    lock = portalocker.Lock(tmpfile)
    assert list(lock._timeout_generator(0, 0.25)) == [0]


def test_timeout_generator_negative_timeout_single_attempt(tmpfile):
    """Even a negative timeout yields the guaranteed first attempt."""
    lock = portalocker.Lock(tmpfile)
    assert list(lock._timeout_generator(-1, 0.25)) == [0]


def test_timeout_generator_zero_interval_keeps_floor(tmpfile):
    """A zero check_interval must not busy-spin the CPU."""
    lock = portalocker.Lock(tmpfile)
    attempts: int = sum(1 for _ in lock._timeout_generator(0.05, 0))
    # The 1ms sleep floor allows roughly 50 attempts in 50ms. Hundreds
    # would mean the floor is gone and the generator spins flat out.
    assert attempts < 100
