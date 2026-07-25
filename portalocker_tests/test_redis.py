"""RedisLock tests.

Every test runs against ``fakeredis`` (no server required) and, when a live
redis server answers on ``localhost:6379``, a second time against that
server. The ``redis_connection`` fixture provides a connection *factory* so
each lock in a test gets its own connection to the same (fake or live)
server, mirroring real usage.
"""

import _thread
import json
import os
import random
import threading
import time
import typing

import fakeredis
import pytest
from redis import client, exceptions

import portalocker
from portalocker import redis, utils

ConnectionFactory = typing.Callable[[], client.Redis]


def test_redis_lock_accepts_shared_flag() -> None:
    lock: redis.RedisLock = redis.RedisLock(
        'shared-channel',
        flags=portalocker.LockFlags.SHARED,
    )

    assert lock.flags == portalocker.LockFlags.SHARED


def test_redis_lock_uses_holder_specific_client_name() -> None:
    lock: redis.RedisLock = redis.RedisLock('named-channel')

    assert lock.client_name == f'named-channel-lock-{lock.holder_id}'
    assert lock.legacy_client_name == 'named-channel-lock'


def test_redis_lock_names_pubsub_connection(
    redis_connection: ConnectionFactory,
) -> None:
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
    )

    lock.acquire()
    try:
        connection: client.Redis = lock.get_connection()
        matching_clients: list[dict[str, str]] = [
            client_
            for client_ in connection.client_list()
            if client_.get('name') == lock.client_name
        ]
        assert len(matching_clients) == 1
        if not isinstance(connection, fakeredis.FakeStrictRedis):
            assert int(matching_clients[0]['sub']) == 1
    finally:
        lock.release()


def test_live_redis_required_fails_when_server_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('PORTALOCKER_REDIS_TESTS_REQUIRED', '1')

    with pytest.raises(pytest.UsageError, match='required live Redis server'):
        _ensure_live_redis_available(False)


@pytest.mark.parametrize(
    'flags',
    [
        portalocker.LockFlags(0),
        portalocker.LockFlags.EXCLUSIVE | portalocker.LockFlags.SHARED,
        portalocker.LockFlags.SHARED | portalocker.LockFlags.NON_BLOCKING,
    ],
)
def test_redis_lock_rejects_invalid_flags(
    flags: portalocker.LockFlags,
) -> None:
    with pytest.raises(ValueError, match='exactly one'):
        redis.RedisLock('invalid-channel', flags=flags)


def _live_redis_connection() -> client.Redis:
    host: str = os.environ.get('REDIS_HOST', 'localhost')
    port: int = int(os.environ.get('REDIS_PORT', '6379'))
    return client.Redis(
        host=host,
        port=port,
        decode_responses=True,
    )


def _live_redis_available() -> bool:
    connection: client.Redis = _live_redis_connection()
    try:
        connection.ping()
    except (exceptions.ConnectionError, ConnectionRefusedError):
        return False
    finally:
        connection.close()
    return True


def _ensure_live_redis_available(available: bool) -> None:
    if available:
        return
    if os.environ.get('PORTALOCKER_REDIS_TESTS_REQUIRED') == '1':
        raise pytest.UsageError(
            'required live Redis server is unavailable at '
            f'{os.environ.get("REDIS_HOST", "localhost")}:'
            f'{os.environ.get("REDIS_PORT", "6379")}'
        )
    pytest.skip('no live redis server')


_LIVE_REDIS: bool = _live_redis_available()


@pytest.fixture(autouse=True)
def set_redis_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(utils, 'DEFAULT_TIMEOUT', 0.0001)
    monkeypatch.setattr(utils, 'DEFAULT_CHECK_INTERVAL', 0.0005)
    # Keep these above the ~15.6ms Windows timer granularity so the real
    # ping/pong path (exercised now that check_or_kill_lock no longer returns
    # a false positive) does not flake on coarse-grained clocks.
    monkeypatch.setattr(redis, 'DEFAULT_UNAVAILABLE_TIMEOUT', 0.2)
    monkeypatch.setattr(redis, 'DEFAULT_THREAD_SLEEP_TIME', 0.01)
    monkeypatch.setattr(_thread, 'interrupt_main', lambda: None)


@pytest.fixture(params=['fakeredis', 'live'])
def redis_connection(request: pytest.FixtureRequest) -> ConnectionFactory:
    """Yield a connection factory backed by fakeredis or a live server."""
    if request.param == 'live':
        _ensure_live_redis_available(_LIVE_REDIS)
        return _live_redis_connection

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


def test_redis_shared_locks_coexist(
    redis_connection: ConnectionFactory,
) -> None:
    channel: str = str(random.random())
    first: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        flags=portalocker.LockFlags.SHARED,
    )
    second: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        flags=portalocker.LockFlags.SHARED,
    )

    try:
        first.acquire()
        second.acquire()
    finally:
        second.release()
        first.release()


@pytest.mark.timeout(180)
@pytest.mark.parametrize(
    ('holder_flags', 'contender_flags'),
    [
        (
            portalocker.LockFlags.SHARED,
            portalocker.LockFlags.EXCLUSIVE,
        ),
        (
            portalocker.LockFlags.EXCLUSIVE,
            portalocker.LockFlags.SHARED,
        ),
    ],
)
def test_redis_incompatible_lock_modes_contend(
    redis_connection: ConnectionFactory,
    holder_flags: portalocker.LockFlags,
    contender_flags: portalocker.LockFlags,
) -> None:
    channel: str = str(random.random())
    holder: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        flags=holder_flags,
    )
    contender: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        flags=contender_flags,
        fail_when_locked=True,
    )

    holder.acquire()
    try:
        with pytest.raises(portalocker.AlreadyLocked):
            contender.acquire()
        assert contender.pubsub is None
    finally:
        holder.release()


def _ignore_stale_cleanup(
    lock: redis.RedisLock,
    connection: client.Redis,
    responding_holders: typing.Iterable[redis.RedisLockHolder],
) -> None:
    pass


def _wait_for_subscribers(
    lock: redis.RedisLock,
    expected: int,
    timeout: float = 10,
) -> None:
    """Block until the lock channel has at least ``expected`` subscribers.

    ``RedisLock.pubsub`` is assigned before SUBSCRIBE reaches the server,
    so waiting for ``pubsub is not None`` does not guarantee that a waiter
    participates in elections yet. Contention tests must synchronize on
    the server-side subscriber count instead.
    """
    deadline: float = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if lock._get_subscriber_count(lock.get_connection()) >= expected:
            return
        time.sleep(0.001)
    raise AssertionError(f'never observed {expected} subscribers')


@pytest.mark.timeout(180)
def test_redis_pending_writer_blocks_new_readers(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel: str = str(random.random())
    reader: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        flags=portalocker.LockFlags.SHARED,
    )
    # Timeouts are sized for heavily loaded CI runners; the assertions below
    # never wait for these upper bounds on the happy path.
    writer: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        timeout=30,
        check_interval=0.02,
        unavailable_timeout=2,
    )
    if isinstance(reader.connection, fakeredis.FakeStrictRedis):
        # fakeredis does not implement CLIENT KILL. Stale-holder cleanup is
        # covered independently; this test isolates writer gating.
        monkeypatch.setattr(
            redis.RedisLock,
            '_kill_unavailable_locks',
            _ignore_stale_cleanup,
        )
    writer_errors: list[BaseException] = []
    writer_released: threading.Event = threading.Event()
    original_writer_release: typing.Callable[[], None] = writer.release

    def record_writer_release() -> None:
        original_writer_release()
        writer_released.set()

    monkeypatch.setattr(writer, 'release', record_writer_release)

    def acquire_writer() -> None:
        try:
            writer.acquire()
        except BaseException as exception:  # pragma: no cover
            writer_errors.append(exception)

    reader.acquire()
    writer_thread: threading.Thread = threading.Thread(target=acquire_writer)
    writer_thread.start()
    _wait_for_subscribers(reader, 2)
    # Before its first complete holder sample a pending writer backs off by
    # releasing its subscription, making it invisible to new readers. The
    # reader-gating guarantee only holds once the writer is elected, so the
    # test must synchronize on that state.
    election_deadline: float = time.monotonic() + 30
    while not writer.writer_elected and time.monotonic() < election_deadline:
        time.sleep(0.001)
    assert writer.writer_elected

    try:
        assert not writer_released.wait(timeout=0.4)
        late_reader: redis.RedisLock = redis.RedisLock(
            channel,
            connection=redis_connection(),
            flags=portalocker.LockFlags.SHARED,
            fail_when_locked=True,
        )
        with pytest.raises(portalocker.AlreadyLocked):
            late_reader.acquire()
        assert late_reader.pubsub is None
    finally:
        reader.release()
        writer_thread.join(timeout=60)
        writer.release()

    assert not writer_thread.is_alive()
    assert not writer_errors


@pytest.mark.timeout(180)
def test_redis_pending_writers_are_elected_by_holder_id(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel: str = str(random.random())
    reader: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        flags=portalocker.LockFlags.SHARED,
    )
    if isinstance(reader.connection, fakeredis.FakeStrictRedis):
        # fakeredis does not implement CLIENT KILL. Stale-holder cleanup is
        # covered independently; this test isolates writer election.
        monkeypatch.setattr(
            redis.RedisLock,
            '_kill_unavailable_locks',
            _ignore_stale_cleanup,
        )
    # The election result depends on every pending writer answering liveness
    # pings in time, so the unavailable window has to absorb CI scheduling
    # stalls; the happy path never waits for these upper bounds.
    first: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        timeout=30,
        check_interval=0.02,
        unavailable_timeout=5,
    )
    second: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        timeout=30,
        check_interval=0.02,
        unavailable_timeout=5,
    )
    first.holder_id = 'a-first-writer'
    second.holder_id = 'b-second-writer'
    acquired: list[str] = []
    errors: list[BaseException] = []

    def acquire(lock: redis.RedisLock, name: str) -> None:
        try:
            lock.acquire()
            acquired.append(name)
        except BaseException as exception:  # pragma: no cover
            errors.append(exception)

    reader.acquire()
    first_thread: threading.Thread = threading.Thread(
        target=acquire,
        args=(first, 'first'),
    )
    second_thread: threading.Thread = threading.Thread(
        target=acquire,
        args=(second, 'second'),
    )
    first_thread.start()
    _wait_for_subscribers(reader, 2)
    second_thread.start()
    _wait_for_subscribers(reader, 3)
    # An unelected writer backs off by dropping its subscription whenever a
    # holder sample is incomplete, so the election order is only pinned down
    # once the favored writer is actually elected while the reader holds on.
    election_deadline: float = time.monotonic() + 30
    while not first.writer_elected and time.monotonic() < election_deadline:
        time.sleep(0.001)
    assert first.writer_elected

    reader.release()
    acquired_deadline: float = time.monotonic() + 20
    while not acquired and not errors and time.monotonic() < acquired_deadline:
        time.sleep(0.001)
    if errors:
        raise errors[0]
    assert acquired == ['first']

    first.release()
    second_thread.join(timeout=60)
    assert acquired == ['first', 'second']
    second.release()
    first_thread.join(timeout=10)
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert not errors


def test_redis_elected_writer_waits_for_shared_holders() -> None:
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))
    lock.holder_id = 'writer'
    holders: list[redis.RedisLockHolder] = [
        redis.RedisLockHolder(
            holder_id=lock.holder_id,
            mode=redis.RedisLockMode.PENDING,
        ),
        redis.RedisLockHolder(
            holder_id='reader',
            mode=redis.RedisLockMode.SHARED,
        ),
    ]

    assert not lock._resolve_lock_holders(holders, fail_when_locked=False)
    assert lock.writer_elected
    assert lock.mode is redis.RedisLockMode.PENDING
    assert not lock._resolve_lock_holders(None, fail_when_locked=False)

    # Once the last shared holder is gone the elected writer acquires. In
    # the integration tests this path races the subscribers==1 fast path,
    # so it has to be covered deterministically here.
    assert lock._resolve_lock_holders([holders[0]], fail_when_locked=False)
    resolved_mode: redis.RedisLockMode = lock.mode
    assert resolved_mode is redis.RedisLockMode.EXCLUSIVE


def test_redis_elected_writer_reuses_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection: fakeredis.FakeStrictRedis = fakeredis.FakeStrictRedis(
        server=fakeredis.FakeServer(),
        decode_responses=True,
    )
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=connection,
        check_interval=0.001,
        timeout=1,
    )
    lock.holder_id = 'writer'
    holders: list[redis.RedisLockHolder] = [
        redis.RedisLockHolder(
            holder_id=lock.holder_id,
            mode=redis.RedisLockMode.PENDING,
        ),
        redis.RedisLockHolder(
            holder_id='reader',
            mode=redis.RedisLockMode.SHARED,
        ),
    ]
    subscriber_counts: list[int] = [2, 1]
    start_calls: list[client.Redis] = []
    sentinel_pubsub: client.PubSub = typing.cast(
        'client.PubSub',
        object(),
    )

    def start_subscription(connection_: client.Redis) -> None:
        start_calls.append(connection_)
        lock.pubsub = sentinel_pubsub

    def get_subscriber_count(connection_: client.Redis) -> int:
        return subscriber_counts.pop(0)

    def collect_lock_holders(
        connection_: client.Redis,
        expected_subscribers: int,
        timeout: float,
    ) -> list[redis.RedisLockHolder]:
        return holders

    monkeypatch.setattr(lock, '_start_subscription', start_subscription)
    monkeypatch.setattr(lock, '_get_subscriber_count', get_subscriber_count)
    monkeypatch.setattr(lock, '_collect_lock_holders', collect_lock_holders)

    assert lock.acquire() is lock
    assert start_calls == [connection]
    assert lock.mode is redis.RedisLockMode.EXCLUSIVE
    lock.pubsub = None
    connection.close()


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

        # A ping publishes holder identity and mode on the response channel.
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
        pong_raw_data: typing.Any = pong['data']
        assert isinstance(pong_raw_data, (str, bytes, bytearray))
        pong_data: dict[str, typing.Any] = json.loads(pong_raw_data)
        assert pong_data == {
            'holder_id': lock.holder_id,
            'mode': 'exclusive',
            'protocol': 1,
        }
        pubsub.close()
    finally:
        lock.release()


@pytest.mark.parametrize(
    'data',
    [
        'not-json',
        json.dumps([]),
        json.dumps({}),
        json.dumps({'response_channel': 123}),
    ],
)
def test_redis_channel_handler_ignores_invalid_messages(
    data: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection: fakeredis.FakeStrictRedis = fakeredis.FakeStrictRedis(
        server=fakeredis.FakeServer(),
        decode_responses=True,
    )
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=connection,
    )
    published: list[tuple[str, str]] = []
    monkeypatch.setattr(
        connection,
        'publish',
        lambda channel, message: published.append((channel, message)),
    )

    lock.channel_handler({'type': 'message', 'data': data})

    assert published == []


def test_redis_parse_legacy_response_as_exclusive() -> None:
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))

    holder: redis.RedisLockHolder = lock._parse_lock_response('123.45', 7)

    assert holder == redis.RedisLockHolder(
        holder_id='legacy-7',
        mode=redis.RedisLockMode.EXCLUSIVE,
        legacy=True,
    )


@pytest.mark.parametrize(
    'response',
    [
        json.dumps(
            {
                'holder_id': 123,
                'mode': 'shared',
                'protocol': 1,
            }
        ),
        json.dumps(
            {
                'holder_id': 'holder',
                'mode': 123,
                'protocol': 1,
            }
        ),
        json.dumps(
            {
                'holder_id': 'holder',
                'mode': 'unknown',
                'protocol': 1,
            }
        ),
        json.dumps(
            {
                'holder_id': 'holder',
                'mode': 'shared',
                'protocol': 2,
            }
        ),
    ],
)
def test_redis_parse_unknown_response_as_legacy(response: str) -> None:
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))

    holder: redis.RedisLockHolder = lock._parse_lock_response(response, 0)

    assert holder.mode is redis.RedisLockMode.EXCLUSIVE
    assert holder.legacy


def test_redis_shared_lock_blocks_on_legacy_holder(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel: str = str(random.random())
    legacy_holder: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
    )

    def legacy_channel_handler(message: dict[str, str]) -> None:
        if message.get('type') != 'message':  # pragma: no cover
            return
        data: dict[str, str] = json.loads(message['data'])
        connection: client.Redis = legacy_holder.get_connection()
        connection.publish(data['response_channel'], str(time.time()))

    monkeypatch.setattr(
        legacy_holder,
        'channel_handler',
        legacy_channel_handler,
    )
    shared_contender: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        flags=portalocker.LockFlags.SHARED,
        fail_when_locked=True,
    )

    legacy_holder.acquire()
    try:
        with pytest.raises(portalocker.AlreadyLocked):
            shared_contender.acquire()
    finally:
        legacy_holder.release()


def test_legacy_probe_recognizes_new_shared_holder(
    redis_connection: ConnectionFactory,
) -> None:
    channel: str = str(random.random())
    shared_holder: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        flags=portalocker.LockFlags.SHARED,
    )
    legacy_probe: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
    )

    shared_holder.acquire()
    try:
        connection: client.Redis = legacy_probe.get_connection()
        assert legacy_probe.check_or_kill_lock(connection, timeout=0.2)
    finally:
        shared_holder.release()


def test_live_redis_reaps_unresponsive_shared_holder(
    redis_connection: ConnectionFactory,
) -> None:
    holder_connection: client.Redis = redis_connection()
    if isinstance(holder_connection, fakeredis.FakeStrictRedis):
        pytest.skip('fakeredis does not implement CLIENT KILL')
    contender_connection: client.Redis = redis_connection()
    channel: str = str(random.random())
    holder: redis.RedisLock = redis.RedisLock(
        channel,
        connection=holder_connection,
        flags=portalocker.LockFlags.SHARED,
        unavailable_timeout=0.2,
    )
    contender: redis.RedisLock = redis.RedisLock(
        channel,
        connection=contender_connection,
        timeout=2,
        check_interval=0.02,
        unavailable_timeout=0.2,
    )

    holder.acquire()
    assert holder.thread is not None
    holder.thread.stop()
    holder.thread.join()
    holder.thread = None
    try:
        contender.acquire()
        assert contender.mode is redis.RedisLockMode.EXCLUSIVE
    finally:
        contender.release()
        holder.release()
        holder_connection.close()
        contender_connection.close()


class _SilentPubSub:
    """Stand-in pubsub whose lock holder never answers."""

    def subscribe(self, *channels: str) -> None:
        pass

    def get_message(self, timeout: float) -> None:
        return None

    def close(self) -> None:
        pass


class _ResponsePubSub:
    def __init__(
        self,
        responses: list[str],
        confirmations: list[dict[str, typing.Any] | None] | None = None,
    ) -> None:
        self._responses: list[str] = responses
        self._confirmations: list[dict[str, typing.Any] | None] = (
            confirmations
            if confirmations is not None
            else [{'type': 'subscribe'}]
        )

    def subscribe(self, *channels: str) -> None:
        pass

    def get_message(self, timeout: float) -> dict[str, typing.Any] | None:
        if self._confirmations:
            return self._confirmations.pop(0)
        if self._responses:
            return {'type': 'message', 'data': self._responses.pop(0)}
        return None

    def close(self) -> None:
        pass


@pytest.mark.parametrize(
    'confirmations',
    [
        [None],
        [{'type': 'message'}, {'type': 'subscribe'}],
    ],
)
def test_redis_collect_holders_tolerates_confirmation_delays(
    confirmations: list[dict[str, typing.Any] | None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection: fakeredis.FakeStrictRedis = fakeredis.FakeStrictRedis(
        server=fakeredis.FakeServer(),
        decode_responses=True,
    )
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=connection,
        thread_sleep_time=0.001,
    )
    pubsub: _ResponsePubSub = _ResponsePubSub(
        [],
        confirmations=confirmations,
    )
    monkeypatch.setattr(lock, '_get_pubsub', lambda connection: pubsub)
    monkeypatch.setattr(lock, '_get_subscriber_count', lambda connection: 0)
    monkeypatch.setattr(connection, 'publish', lambda channel, message: 0)

    holders: list[redis.RedisLockHolder] | None = lock._collect_lock_holders(
        connection,
        expected_subscribers=0,
        timeout=0.01,
    )

    assert holders == []


def test_redis_collect_holders_detects_subscriber_churn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A holder set that changes mid-probe invalidates the sample.

    In the integration tests this only happens when a competing waiter
    resubscribes at exactly the wrong moment, so it has to be covered
    deterministically here.
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
    pubsub: _ResponsePubSub = _ResponsePubSub([])
    monkeypatch.setattr(lock, '_get_pubsub', lambda connection: pubsub)
    monkeypatch.setattr(lock, '_get_subscriber_count', lambda connection: 1)
    monkeypatch.setattr(connection, 'publish', lambda channel, message: 0)

    holders: list[redis.RedisLockHolder] | None = lock._collect_lock_holders(
        connection,
        expected_subscribers=2,
        timeout=0.01,
    )

    assert holders is None


def test_redis_collect_holders_kills_only_unresponsive_holder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection: fakeredis.FakeStrictRedis = fakeredis.FakeStrictRedis(
        server=fakeredis.FakeServer(),
        decode_responses=True,
    )
    lock: redis.RedisLock = redis.RedisLock(
        'stale-channel',
        connection=connection,
        thread_sleep_time=0.001,
    )
    response: str = json.dumps(
        {
            'holder_id': 'responding',
            'mode': 'shared',
            'protocol': 1,
        }
    )
    pubsub: _ResponsePubSub = _ResponsePubSub([response])
    killed: list[str | None] = []
    monkeypatch.setattr(lock, '_get_pubsub', lambda connection: pubsub)
    monkeypatch.setattr(lock, '_get_subscriber_count', lambda connection: 2)
    monkeypatch.setattr(connection, 'publish', lambda channel, message: 2)
    monkeypatch.setattr(
        connection,
        'client_list',
        lambda: [
            {
                'id': 'responding-client',
                'name': 'stale-channel-lock-responding',
            },
            {'id': 'stale-client', 'name': 'stale-channel-lock-stale'},
        ],
    )
    monkeypatch.setattr(
        connection,
        'client_kill_filter',
        lambda client_id: killed.append(client_id),
    )

    holders: list[redis.RedisLockHolder] | None = lock._collect_lock_holders(
        connection,
        expected_subscribers=2,
        timeout=0.01,
    )

    assert holders is None
    assert killed == ['stale-client']


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


@pytest.mark.timeout(60)
def test_redis_acquire_fail_when_locked_fails_fast() -> None:
    """``fail_when_locked`` raises immediately when the holder is alive.

    It must not keep polling until the timeout expires.
    """
    server: fakeredis.FakeServer = fakeredis.FakeServer()
    holder_connection: fakeredis.FakeStrictRedis = fakeredis.FakeStrictRedis(
        server=server,
        decode_responses=True,
    )
    contender_connection: fakeredis.FakeStrictRedis = (
        fakeredis.FakeStrictRedis(
            server=server,
            decode_responses=True,
        )
    )
    channel: str = str(random.random())
    holder: redis.RedisLock = redis.RedisLock(
        channel,
        connection=holder_connection,
        thread_sleep_time=0.001,
    )
    # The generous timeout is the point of the regression: failing fast must
    # not depend on the timeout, so the elapsed assertion below proves the
    # contender never polled anywhere near it even on slow CI runners.
    contender: redis.RedisLock = redis.RedisLock(
        channel,
        connection=contender_connection,
        timeout=30,
        fail_when_locked=True,
        thread_sleep_time=0.001,
        unavailable_timeout=2,
    )
    holder.acquire()

    start: float = time.monotonic()
    with pytest.raises(portalocker.AlreadyLocked):
        contender.acquire()
    elapsed: float = time.monotonic() - start

    assert elapsed < 10
    holder.release()


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


class _SubscribeError(Exception):
    """Raised by the stub pubsub to simulate a failing subscribe."""


class _BoomPubSub:
    """Pubsub whose ``subscribe`` always raises."""

    def execute_command(self, *args: typing.Any) -> None:
        pass

    def parse_response(self) -> None:
        pass

    def subscribe(self, **channels: typing.Any) -> None:
        raise _SubscribeError('subscribe failed')

    def unsubscribe(self, *channels: str) -> None:
        pass

    def close(self) -> None:
        pass


def test_redis_acquire_rolls_back_pubsub_on_subscribe_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing subscribe must not leave the lock half-initialised.

    If ``self.pubsub`` were left set, the ``assert not self.pubsub`` guard at
    the top of ``acquire`` would turn every retry into an ``AssertionError``
    instead of surfacing the real error.
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

    monkeypatch.setattr(lock, '_get_pubsub', lambda conn: _BoomPubSub())

    with pytest.raises(_SubscribeError):
        lock.acquire()
    assert lock.pubsub is None
    assert lock.thread is None

    # Retry on the *same* instance must surface the real error again, not an
    # AssertionError from a stale ``self.pubsub``.
    with pytest.raises(_SubscribeError):
        lock.acquire()
