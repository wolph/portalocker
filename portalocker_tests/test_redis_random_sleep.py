import time

from portalocker import redis


class FakeLock(redis.RedisLock):
    def __init__(self, thread_sleep_time, *args, **kwargs):
        # Channel doesn't affect sleep behavior.
        super().__init__(channel='test_channel', *args, **kwargs)
        self.thread_sleep_time = thread_sleep_time


def test_timeout_generator_with_positive_check_interval(monkeypatch):
    sleep_times = []

    def fake_sleep(t):
        sleep_times.append(t)

    monkeypatch.setattr(time, 'sleep', fake_sleep)

    # With a positive check_interval, the effective_interval should equal check_interval.
    lock = FakeLock(thread_sleep_time=0.05)
    gen = lock._timeout_generator(timeout=0.1, check_interval=0.02)
    next(gen)
    # Expected sleep time = 0.02 * (0.5 + random_value) which lies in [0.01, 0.03].
    assert len(sleep_times) == 1
    sleep_time = sleep_times[0]
    assert 0.01 <= sleep_time <= 0.03


def test_timeout_generator_with_zero_check_interval(monkeypatch):
    sleep_times = []

    def fake_sleep(t):
        sleep_times.append(t)

    monkeypatch.setattr(time, 'sleep', fake_sleep)

    # With check_interval set to zero, effective_interval should be thread_sleep_time.
    lock = FakeLock(thread_sleep_time=0.05)
    gen = lock._timeout_generator(timeout=0.1, check_interval=0)
    next(gen)
    # Expected sleep time = 0.05 * (0.5 + random_value) which lies in [0.025, 0.075].
    assert len(sleep_times) == 1
    sleep_time = sleep_times[0]
    assert 0.025 <= sleep_time <= 0.075


def test_timeout_generator_with_negative_check_interval(monkeypatch):
    sleep_times = []

    def fake_sleep(t):
        sleep_times.append(t)

    monkeypatch.setattr(time, 'sleep', fake_sleep)

    # With a negative check_interval, effective_interval should also fall back to thread_sleep_time.
    lock = FakeLock(thread_sleep_time=0.05)
    gen = lock._timeout_generator(timeout=0.1, check_interval=-0.01)
    next(gen)
    # Expected sleep time = 0.05 * (0.5 + random_value) which lies in [0.025, 0.075].
    assert len(sleep_times) == 1
    sleep_time = sleep_times[0]
    assert 0.025 <= sleep_time <= 0.075
