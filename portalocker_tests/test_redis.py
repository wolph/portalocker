import random

import pytest
from redis import client
from redis import exceptions

import portalocker
from portalocker import redis


def xfail(function):
    # Apply both xfail decorators
    function = pytest.mark.xfail(raises=exceptions.ConnectionError)(function)
    function = pytest.mark.xfail(raises=ConnectionRefusedError)(function)
    return function


@xfail
def test_redis_lock():
    channel = str(random.random())

    lock_a = redis.RedisLock(channel)
    lock_a.acquire(fail_when_locked=True)

    lock_b = redis.RedisLock(channel)
    with pytest.raises(portalocker.AlreadyLocked):
        lock_b.acquire(fail_when_locked=True)


@pytest.mark.parametrize('timeout', [None, 0, 0.001])
@pytest.mark.parametrize('check_interval', [None, 0, 0.0005])
@xfail
def test_redis_lock_timeout(timeout, check_interval):
    connection = client.Redis()
    channel = str(random.random())
    lock_a = redis.RedisLock(channel)
    lock_a.acquire(timeout=timeout, check_interval=check_interval)

    lock_b = redis.RedisLock(channel, connection=connection)
    with pytest.raises(portalocker.AlreadyLocked):
        lock_b.acquire(timeout=timeout, check_interval=check_interval)


@xfail
def test_redis_lock_context():
    channel = str(random.random())

    lock_a = redis.RedisLock(channel, fail_when_locked=True)
    with lock_a:
        lock_b = redis.RedisLock(channel, fail_when_locked=True)
        with pytest.raises(portalocker.AlreadyLocked):
            with lock_b:
                pass


@xfail
def test_redis_relock():
    channel = str(random.random())

    lock_a = redis.RedisLock(channel, fail_when_locked=True)
    with lock_a:
        with pytest.raises(AssertionError):
            lock_a.acquire()

    lock_a.release()


if __name__ == '__main__':
    test_redis_lock()
