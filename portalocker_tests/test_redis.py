"""RedisLock tests.

Every test runs against ``fakeredis`` (no server required) and, when a live
redis server answers on ``localhost:6379``, a second time against that
server. The ``redis_connection`` fixture provides a connection *factory* so
each lock in a test gets its own connection to the same (fake or live)
server, mirroring real usage.
"""

import _thread
import json
import random
import time
import typing

import fakeredis
import pytest
from redis import client, exceptions

import portalocker
from portalocker import redis, utils

ConnectionFactory = typing.Callable[[], client.Redis]


def _live_redis_available() -> bool:
    try:
        client.Redis().ping()
    except (exceptions.ConnectionError, ConnectionRefusedError):
        return False
    return True


_LIVE_REDIS: bool = _live_redis_available()


@pytest.fixture(autouse=True)
def set_redis_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(utils, 'DEFAULT_TIMEOUT', 0.0001)
    monkeypatch.setattr(utils, 'DEFAULT_CHECK_INTERVAL', 0.0005)
    monkeypatch.setattr(redis, 'DEFAULT_UNAVAILABLE_TIMEOUT', 0.01)
    monkeypatch.setattr(redis, 'DEFAULT_THREAD_SLEEP_TIME', 0.001)
    monkeypatch.setattr(_thread, 'interrupt_main', lambda: None)


@pytest.fixture(params=['fakeredis', 'live'])
def redis_connection(request: pytest.FixtureRequest) -> ConnectionFactory:
    """Yield a connection factory backed by fakeredis or a live server."""
    if request.param == 'live':
        if not _LIVE_REDIS:
            pytest.skip('no live redis server on localhost:6379')
        return lambda: client.Redis(decode_responses=True)

    server: fakeredis.FakeServer = fakeredis.FakeServer()
    return lambda: fakeredis.FakeStrictRedis(
        server=server,
        decode_responses=True,
    )


def test_redis_lock(redis_connection: ConnectionFactory) -> None:
    channel: str = str(random.random())

    lock_a: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
    )
    lock_a.acquire(fail_when_locked=True)
    time.sleep(0.01)

    lock_b: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
    )
    try:
        with pytest.raises(portalocker.AlreadyLocked):
            lock_b.acquire(fail_when_locked=True)
    finally:
        lock_a.release()
        if lock_a.connection is not None:
            lock_a.connection.close()


@pytest.mark.parametrize('timeout', [None, 0, 0.001])
@pytest.mark.parametrize('check_interval', [None, 0, 0.0005])
def test_redis_lock_timeout(
    timeout: float | None,
    check_interval: float | None,
    redis_connection: ConnectionFactory,
) -> None:
    channel: str = str(random.random())
    lock_a: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
    )
    lock_a.acquire(timeout=timeout, check_interval=check_interval)

    lock_b: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
    )
    with pytest.raises(portalocker.AlreadyLocked):
        try:
            lock_b.acquire(timeout=timeout, check_interval=check_interval)
        finally:
            lock_a.release()
            if lock_a.connection is not None:
                lock_a.connection.close()


def test_redis_lock_context(redis_connection: ConnectionFactory) -> None:
    channel: str = str(random.random())

    lock_a: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        fail_when_locked=True,
    )
    with lock_a:
        time.sleep(0.01)
        lock_b: redis.RedisLock = redis.RedisLock(
            channel,
            connection=redis_connection(),
            fail_when_locked=True,
        )
        with pytest.raises(portalocker.AlreadyLocked), lock_b:
            pass


def test_redis_relock(redis_connection: ConnectionFactory) -> None:
    channel: str = str(random.random())

    lock_a: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        fail_when_locked=True,
    )
    with lock_a:
        time.sleep(0.01)
        with pytest.raises(AssertionError):
            lock_a.acquire()
    time.sleep(0.01)

    lock_a.release()


def test_redis_get_connection_creates_and_caches() -> None:
    """Without an explicit connection one is created lazily and reused."""
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))
    assert lock.connection is None
    connection_a: client.Redis = lock.get_connection()
    connection_b: client.Redis = lock.get_connection()
    assert connection_a is connection_b
    assert lock.close_connection


def test_redis_channel_handler(redis_connection: ConnectionFactory) -> None:
    """The lock holder answers pings and ignores messages without data."""
    channel: str = str(random.random())
    lock: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
    )
    lock.acquire()
    try:
        response_channel: str = f'{channel}-response'
        connection: client.Redis = lock.get_connection()
        pubsub: client.PubSub = lock._get_pubsub(connection)
        pubsub.subscribe(response_channel)

        # A message without data is ignored: only the subscribe
        # confirmation reaches us, never a pong.
        lock.channel_handler({'type': 'message', 'data': ''})
        while (message := pubsub.get_message(timeout=0.1)) is not None:
            assert message.get('type') != 'message'

        # A ping publishes a pong (timestamp) on the response channel.
        lock.channel_handler(
            {
                'type': 'message',
                'data': json.dumps(
                    {
                        'response_channel': response_channel,
                        'message': 'ping',
                    }
                ),
            }
        )
        pong: dict[str, typing.Any] | None = None
        for _ in range(50):
            message = pubsub.get_message(timeout=0.1)
            if message is not None and message.get('type') == 'message':
                pong = message
                break
        assert pong is not None
        pong_data: typing.Any = pong['data']
        assert pong_data is not None
        assert float(pong_data) > 0
        pubsub.close()
    finally:
        lock.release()


class _SilentPubSub:
    """Stand-in pubsub whose lock holder never answers."""

    def subscribe(self, *channels: str) -> None:
        pass

    def get_message(self, timeout: float) -> None:
        return None

    def close(self) -> None:
        pass


def test_redis_check_or_kill_lock_kills_unresponsive_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unresponsive lock holder gets killed through the client list.

    Neither fakeredis nor a healthy live server can reach this path
    end-to-end (the response-channel subscribe confirmation always
    satisfies ``get_message``), so the collaborators are stubbed: the
    pubsub never yields a message and the client list reports one
    matching and one unrelated client.
    """
    connection: fakeredis.FakeStrictRedis = fakeredis.FakeStrictRedis(
        server=fakeredis.FakeServer(),
        decode_responses=True,
    )
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=connection,
        thread_sleep_time=0.001,
    )

    def silent_pubsub(connection: client.Redis) -> _SilentPubSub:
        return _SilentPubSub()

    killed: list[str | None] = []

    def client_list(client_type: str) -> list[dict[str, str]]:
        assert client_type == 'pubsub'
        return [
            {'id': '42', 'name': lock.client_name},
            {'id': '43', 'name': 'unrelated-client'},
        ]

    def client_kill_filter(client_id: str | None) -> None:
        killed.append(client_id)

    monkeypatch.setattr(lock, '_get_pubsub', silent_pubsub)
    monkeypatch.setattr(connection, 'client_list', client_list)
    monkeypatch.setattr(connection, 'client_kill_filter', client_kill_filter)

    assert lock.check_or_kill_lock(connection, timeout=0.01) is None
    assert killed == ['42']


class _RecordingPubSub:
    """Stand-in pubsub that records the order of calls.

    ``get_message`` returns the subscribe confirmation Redis queues on
    ``subscribe`` (``type='subscribe'``) while *confirm* is set, then a single
    pong (``type='message'``) while *pong* is set, then ``None`` forever.
    """

    def __init__(
        self,
        calls: list[str],
        *,
        confirm: bool = True,
        pong: bool = False,
    ) -> None:
        self._calls = calls
        self._confirm = confirm
        self._pong = pong
        self._confirmed = False
        self._ponged = False

    def subscribe(self, *channels: str) -> None:
        self._calls.append('subscribe')

    def get_message(self, timeout: float) -> dict[str, typing.Any] | None:
        self._calls.append('get_message')
        if self._confirm and not self._confirmed:
            self._confirmed = True
            return {'type': 'subscribe', 'channel': 'c', 'data': 1}
        if self._pong and not self._ponged:
            self._ponged = True
            return {'type': 'message', 'channel': 'c', 'data': '1.0'}
        return None

    def close(self) -> None:
        self._calls.append('close')


def test_redis_check_or_kill_lock_pings_after_subscribe_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The subscribe confirmation is consumed before the ping is sent.

    The stub yields the subscribe confirmation first and then stays silent.
    The confirmation must not be counted as a pong (so an unresponsive holder
    is reaped instead of reported alive) and the ping must only be published
    once the subscription has been confirmed active.
    """
    connection: fakeredis.FakeStrictRedis = fakeredis.FakeStrictRedis(
        server=fakeredis.FakeServer(),
        decode_responses=True,
    )
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=connection,
        thread_sleep_time=0.001,
    )
    calls: list[str] = []
    killed: list[str | None] = []

    def recording_pubsub(connection: client.Redis) -> _RecordingPubSub:
        return _RecordingPubSub(calls, confirm=True, pong=False)

    def client_list(client_type: str) -> list[dict[str, str]]:
        assert client_type == 'pubsub'
        return [
            {'id': '42', 'name': lock.client_name},
            {'id': '43', 'name': 'unrelated-client'},
        ]

    def client_kill_filter(client_id: str | None) -> None:
        killed.append(client_id)

    def publish(channel: str, message: str) -> int:
        calls.append('publish')
        return 0

    monkeypatch.setattr(lock, '_get_pubsub', recording_pubsub)
    monkeypatch.setattr(connection, 'client_list', client_list)
    monkeypatch.setattr(connection, 'client_kill_filter', client_kill_filter)
    monkeypatch.setattr(connection, 'publish', publish)

    assert lock.check_or_kill_lock(connection, timeout=0.01) is None
    assert killed == ['42']
    # Ping published only after the subscribe confirmation was consumed.
    assert calls.index('subscribe') < calls.index('publish')
    assert calls.index('publish') > calls.index('get_message')
    # Pubsub is closed even on the reap branch.
    assert 'close' in calls


@pytest.mark.parametrize(
    ('confirm', 'pong', 'expected'),
    [(True, True, True), (False, False, None)],
)
def test_redis_check_or_kill_lock_always_closes_pubsub(
    confirm: bool,
    pong: bool,
    expected: bool | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``pubsub.close()`` runs on both the alive and the reap branch."""
    connection: fakeredis.FakeStrictRedis = fakeredis.FakeStrictRedis(
        server=fakeredis.FakeServer(),
        decode_responses=True,
    )
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=connection,
        thread_sleep_time=0.001,
    )
    calls: list[str] = []

    def recording_pubsub(connection: client.Redis) -> _RecordingPubSub:
        return _RecordingPubSub(calls, confirm=confirm, pong=pong)

    def publish(channel: str, message: str) -> int:
        return 0

    monkeypatch.setattr(lock, '_get_pubsub', recording_pubsub)
    monkeypatch.setattr(connection, 'client_list', lambda client_type: [])
    monkeypatch.setattr(connection, 'publish', publish)

    assert lock.check_or_kill_lock(connection, timeout=0.01) is expected
    assert calls.count('close') == 1


def test_redis_acquire_fail_when_locked_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``fail_when_locked`` raises immediately when the holder is alive.

    It must not keep polling until the timeout expires. ``check_or_kill_lock``
    is stubbed to report the holder alive and is expected to be consulted
    exactly once before ``AlreadyLocked`` is raised.
    """
    connection: fakeredis.FakeStrictRedis = fakeredis.FakeStrictRedis(
        server=fakeredis.FakeServer(),
        decode_responses=True,
    )
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=connection,
        thread_sleep_time=0.001,
    )
    calls: list[float] = []

    def check_or_kill_lock(conn: client.Redis, timeout: float) -> bool:
        calls.append(timeout)
        return True

    monkeypatch.setattr(lock, '_get_subscriber_count', lambda conn: 1)
    monkeypatch.setattr(lock, 'check_or_kill_lock', check_or_kill_lock)

    start: float = time.monotonic()
    with pytest.raises(portalocker.AlreadyLocked):
        lock.acquire(timeout=1, fail_when_locked=True)
    elapsed: float = time.monotonic() - start

    # Raised after a single liveness check, not after polling the timeout.
    assert calls == [lock.unavailable_timeout]
    assert elapsed < 0.5


def test_redis_release_closes_auto_created_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection the lock created itself is closed on release."""
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))
    assert lock.close_connection is True

    connection: fakeredis.FakeStrictRedis = fakeredis.FakeStrictRedis(
        server=fakeredis.FakeServer(),
        decode_responses=True,
    )
    closed: list[bool] = []
    monkeypatch.setattr(connection, 'close', lambda: closed.append(True))
    lock.connection = connection

    lock.release()

    assert closed == [True]
    # Cleared so a later acquire recreates the connection.
    assert lock.connection is None


def test_redis_release_keeps_caller_supplied_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller-supplied connection is never closed by the lock."""
    connection: fakeredis.FakeStrictRedis = fakeredis.FakeStrictRedis(
        server=fakeredis.FakeServer(),
        decode_responses=True,
    )
    closed: list[bool] = []
    monkeypatch.setattr(connection, 'close', lambda: closed.append(True))
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=connection,
    )
    assert lock.close_connection is False

    lock.release()

    assert closed == []
    assert lock.connection is connection
