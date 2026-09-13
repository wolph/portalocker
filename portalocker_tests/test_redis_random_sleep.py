"""Validate the jitter that RedisLock adds to its sleep intervals."""

import random
import time
from typing import Any

import fakeredis
import pytest
from redis import client

import portalocker
from portalocker import redis


def test_acquire_caps_jitter_at_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry sleeps end at the deadline without starting another attempt."""
    now: float = 0.0
    sleeps: list[float] = []
    attempts: list[float] = []

    def sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    def attempt(connection: client.Redis, fail_when_locked: bool) -> bool:
        attempts.append(now)
        return False

    monkeypatch.setattr(time, 'monotonic', lambda: now)
    monkeypatch.setattr(time, 'sleep', sleep)
    monkeypatch.setattr(random, 'random', lambda: 0.5)
    lock: redis.RedisLock = redis.RedisLock('deadline')
    monkeypatch.setattr(lock, '_acquire_attempt', attempt)

    with pytest.raises(portalocker.AlreadyLocked):
        lock.acquire(timeout=0.3, check_interval=2)
    assert attempts == [0.0]
    assert sleeps == [0.3]


def test_acquire_rechecks_deadline_after_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late wakeup must not start another acquisition attempt."""
    now: float = 0.0
    attempts: list[float] = []

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds + 1.0

    def attempt(connection: client.Redis, fail_when_locked: bool) -> bool:
        attempts.append(now)
        return False

    monkeypatch.setattr(time, 'monotonic', lambda: now)
    monkeypatch.setattr(time, 'sleep', sleep)
    lock: redis.RedisLock = redis.RedisLock('late-wakeup')
    monkeypatch.setattr(lock, '_acquire_attempt', attempt)

    with pytest.raises(portalocker.AlreadyLocked):
        lock.acquire(timeout=0.3, check_interval=0.01)
    assert attempts == [0.0]


def test_reply_polling_keeps_the_final_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply buffered during the final sleep still gets read by probes."""
    now: float = 0.0

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    monkeypatch.setattr(time, 'monotonic', lambda: now)
    monkeypatch.setattr(time, 'sleep', sleep)
    monkeypatch.setattr(random, 'random', lambda: 0.5)
    lock: redis.RedisLock = redis.RedisLock('final-reply')
    reads: list[float] = [now for _ in lock._timeout_generator(0.3, 2)]

    assert reads == [0.0, 0.3]


class FakeLock(redis.RedisLock):
    def __init__(
        self, thread_sleep_time: float, *args: Any, **kwargs: Any
    ) -> None:
        # Channel doesn't affect sleep behavior.
        super().__init__('test_channel', *args, **kwargs)
        self.thread_sleep_time = thread_sleep_time


def test_uncontended_acquire_skips_first_interval() -> None:
    """An uncontended acquire must not sleep out an interval up front.

    With ``check_interval=0.5`` the old pre-yield sleep cost at least
    0.25 seconds before the first attempt, so finishing in under 0.1
    seconds proves the first attempt runs immediately.
    """
    connection: fakeredis.FakeStrictRedis = fakeredis.FakeStrictRedis(
        server=fakeredis.FakeServer(),
        decode_responses=True,
    )
    lock: redis.RedisLock = redis.RedisLock(
        'uncontended_channel',
        connection=connection,
        check_interval=0.5,
    )
    start: float = time.monotonic()
    try:
        lock.acquire()
        elapsed: float = time.monotonic() - start
    finally:
        lock.release()
    assert elapsed < 0.1


def test_timeout_generator_with_positive_check_interval(monkeypatch):
    """The first yield is immediate; later yields sleep for a fraction
    of check_interval (0.5 <= factor < 1.5) when check_interval > 0.
    """
    sleep_times = []

    def fake_sleep(t):
        sleep_times.append(t)

    monkeypatch.setattr(time, 'sleep', fake_sleep)

    # For positive check_interval, effective_interval equals check_interval.
    lock = FakeLock(thread_sleep_time=0.05)
    gen = lock._timeout_generator(timeout=0.1, check_interval=0.02)
    next(gen)
    # The first attempt runs immediately, without any up-front sleep.
    assert sleep_times == []
    next(gen)
    # Expected sleep time is 0.02 * (0.5 + random_value) in [0.01, 0.03].
    assert len(sleep_times) == 1
    sleep_time = sleep_times[0]
    assert 0.01 <= sleep_time <= 0.03


def test_timeout_generator_with_zero_check_interval(monkeypatch):
    """When check_interval == 0 the sleeps between attempts must be a
    fraction of thread_sleep_time (0.5 <= factor < 1.5).
    """
    sleep_times = []

    def fake_sleep(t):
        sleep_times.append(t)

    monkeypatch.setattr(time, 'sleep', fake_sleep)

    # For zero check_interval, effective_interval is thread_sleep_time.
    lock = FakeLock(thread_sleep_time=0.05)
    gen = lock._timeout_generator(timeout=0.1, check_interval=0)
    next(gen)
    assert sleep_times == []
    next(gen)
    # Expected sleep time is 0.05 * (0.5 + random_value) in [0.025, 0.075].
    assert len(sleep_times) == 1
    sleep_time = sleep_times[0]
    assert 0.025 <= sleep_time <= 0.075


def test_timeout_generator_with_none_values(monkeypatch):
    """`None` timeout means 0.0 (one attempt) and the single attempt
    must not sleep at all.
    """
    sleep_times = []

    def fake_sleep(t):
        sleep_times.append(t)

    monkeypatch.setattr(time, 'sleep', fake_sleep)

    lock = FakeLock(thread_sleep_time=0.05)
    gen = lock._timeout_generator(timeout=None, check_interval=None)
    next(gen)
    # The one attempt a zero timeout buys runs without sleeping.
    assert sleep_times == []
    # A timeout of None coalesces to 0.0, so only one attempt is yielded.
    with pytest.raises(StopIteration):
        next(gen)
    assert sleep_times == []


def test_timeout_generator_with_negative_check_interval(monkeypatch):
    """When check_interval < 0 the sleeps between attempts must be a
    fraction of thread_sleep_time (0.5 <= factor < 1.5).
    """
    sleep_times = []

    def fake_sleep(t):
        sleep_times.append(t)

    monkeypatch.setattr(time, 'sleep', fake_sleep)

    # For negative check_interval, effective_interval is thread_sleep_time.
    lock = FakeLock(thread_sleep_time=0.05)
    gen = lock._timeout_generator(timeout=0.1, check_interval=-0.01)
    next(gen)
    assert sleep_times == []
    next(gen)
    # Expected sleep time is 0.05 * (0.5 + random_value) in [0.025, 0.075].
    assert len(sleep_times) == 1
    sleep_time = sleep_times[0]
    assert 0.025 <= sleep_time <= 0.075
