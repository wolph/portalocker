"""RedisLock tests.

Every test runs against ``fakeredis`` (no server required) and, when a live
redis server answers on ``localhost:6379``, a second time against that
server. The ``redis_connection`` fixture provides a connection *factory* so
each lock in a test gets its own connection to the same (fake or live)
server, mirroring real usage.
"""

import _thread
import json
import logging
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
    # An unelected writer backs off by dropping its subscription whenever a
    # holder sample is incomplete, so the election order is only pinned down
    # once the favored writer is actually elected while the reader holds on.
    # Since #143 a writer that wins an election also keeps it instead of
    # rerunning the id sort, so on a stalled runner the second writer could
    # win a clean probe while the first is between attempts and then fairly
    # keep that election forever. The favored writer must therefore be
    # elected before the second writer may start.
    election_deadline: float = time.monotonic() + 30
    while not first.writer_elected and time.monotonic() < election_deadline:
        time.sleep(0.001)
    assert first.writer_elected
    second_thread.start()
    _wait_for_subscribers(reader, 3)

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


def test_redis_elected_writer_waits_for_shared_holders(
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

    # While the readers drain the ping reply advertises the election, so
    # later writers can defer to this incumbent instead of usurping it.
    published: list[tuple[str, str]] = []
    monkeypatch.setattr(
        connection,
        'publish',
        lambda channel, message: published.append((channel, message)),
    )
    lock.channel_handler(
        {
            'type': 'message',
            'data': json.dumps({'response_channel': 'resp'}),
        }
    )
    reply: dict[str, typing.Any] = json.loads(published[0][1])
    assert reply['elected'] is True
    assert reply['mode'] == 'pending'

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


class _IdlePubSub:
    """Stand-in pubsub that ``_unsubscribe`` can tear down quietly.

    ``connection`` is `None`, so the unsubscribe step is skipped, and
    ``close`` is a no-op, mirroring a pubsub whose worker thread already
    closed the subscription.
    """

    connection: None = None

    def close(self) -> None:
        pass


def _idle_pubsub() -> client.PubSub:
    return typing.cast('client.PubSub', _IdlePubSub())


def test_redis_nonblocking_election_winner_promotes() -> None:
    """A fail_when_locked winner takes a channel that holds no readers.

    Regression test for issue #143 defect 1: the fail check used to run
    before the promotion check, so the election winner on a channel of
    pending writers raised ``AlreadyLocked`` even though nobody held the
    lock and it could have promoted outright.
    """
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))
    lock.holder_id = 'aaa'
    holders: list[redis.RedisLockHolder] = [
        redis.RedisLockHolder(
            holder_id='aaa',
            mode=redis.RedisLockMode.PENDING,
        ),
        redis.RedisLockHolder(
            holder_id='bbb',
            mode=redis.RedisLockMode.PENDING,
        ),
    ]

    assert lock._resolve_lock_holders(holders, fail_when_locked=True)

    assert lock.mode is redis.RedisLockMode.EXCLUSIVE
    assert lock.writer_elected


def test_redis_nonblocking_election_loser_raises() -> None:
    """The election loser raises and tears down fully."""
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))
    lock.holder_id = 'bbb'
    holders: list[redis.RedisLockHolder] = [
        redis.RedisLockHolder(
            holder_id='aaa',
            mode=redis.RedisLockMode.PENDING,
        ),
        redis.RedisLockHolder(
            holder_id='bbb',
            mode=redis.RedisLockMode.PENDING,
        ),
    ]

    with pytest.raises(portalocker.AlreadyLocked):
        lock._resolve_lock_holders(holders, fail_when_locked=True)

    assert lock.pubsub is None
    assert not lock.writer_elected


def test_redis_nonblocking_elected_with_readers_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fail_when_locked winner facing live readers raises.

    Non-blocking means not waiting for the readers to drain. The raise
    must never leave ``writer_elected`` set on the way out: the instance
    releases its subscription, so it must not have advertised an
    election it will not use.
    """
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))
    lock.holder_id = 'aaa'
    holders: list[redis.RedisLockHolder] = [
        redis.RedisLockHolder(
            holder_id='aaa',
            mode=redis.RedisLockMode.PENDING,
        ),
        redis.RedisLockHolder(
            holder_id='reader',
            mode=redis.RedisLockMode.SHARED,
        ),
    ]
    flags_at_release: list[bool] = []
    original_release: typing.Callable[[], None] = lock.release

    def recording_release() -> None:
        flags_at_release.append(lock.writer_elected)
        original_release()

    monkeypatch.setattr(lock, 'release', recording_release)

    with pytest.raises(portalocker.AlreadyLocked):
        lock._resolve_lock_holders(holders, fail_when_locked=True)

    assert flags_at_release == [False]
    assert not lock.writer_elected


def test_redis_writer_defers_to_elected_holder() -> None:
    """A pending writer never elects itself past an advertised incumbent.

    Regression test for issue #143 defect 2: the id sort alone let a
    lower-id newcomer usurp a writer that had already won a previous
    election and was waiting for the readers to drain.
    """
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))
    lock.holder_id = 'aaa'
    holders: list[redis.RedisLockHolder] = [
        redis.RedisLockHolder(
            holder_id='aaa',
            mode=redis.RedisLockMode.PENDING,
            elected=False,
        ),
        redis.RedisLockHolder(
            holder_id='zzz',
            mode=redis.RedisLockMode.PENDING,
            elected=True,
        ),
    ]

    assert not lock._writer_is_elected(holders)


def test_redis_incumbent_keeps_election_against_new_format_newcomer() -> None:
    """An incumbent is not usurped by a lower-id 4.2 newcomer.

    The newcomer advertises ``elected: false``, so it defers and the
    incumbent keeps its election through the reader drain and through
    the hold-off round the undecided newcomer costs, then promotes once
    the channel is clear of both.
    """
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))
    lock.holder_id = 'z-incumbent'
    lock.writer_elected = True
    holders: list[redis.RedisLockHolder] = [
        redis.RedisLockHolder(
            holder_id='z-incumbent',
            mode=redis.RedisLockMode.PENDING,
            elected=True,
        ),
        redis.RedisLockHolder(
            holder_id='a-newcomer',
            mode=redis.RedisLockMode.PENDING,
            elected=False,
        ),
        redis.RedisLockHolder(
            holder_id='reader',
            mode=redis.RedisLockMode.SHARED,
        ),
    ]

    assert not lock._resolve_lock_holders(holders, fail_when_locked=False)
    assert lock.writer_elected
    assert lock.mode is redis.RedisLockMode.PENDING

    # Without the reader the incumbent still holds off: the newcomer's
    # elected false reply cannot show whether it saw this election, so
    # promoting past it could overlap with a promotion the newcomer
    # made on its own stale view. The election itself is kept.
    assert not lock._resolve_lock_holders(holders[:2], fail_when_locked=False)
    assert lock.writer_elected
    assert lock.mode is redis.RedisLockMode.PENDING

    # A deferring newcomer unsubscribes right after its probe, so the
    # next conclusive probe is clear of it and the incumbent promotes.
    assert lock._resolve_lock_holders(holders[:1], fail_when_locked=False)
    promoted_mode: redis.RedisLockMode = lock.mode
    assert promoted_mode is redis.RedisLockMode.EXCLUSIVE
    assert lock.writer_elected


def test_redis_incumbent_holds_off_for_stale_lower_id_newcomer() -> None:
    """The reviewed double-EXCLUSIVE interleaving stays single-holder.

    Staged replay of the #143 review trace: the reader releases while
    the incumbent's probe is mid-drain, and a lower-id newcomer probes
    before the incumbent's election flag reaches the wire. Every probe
    below is exactly what each side saw in that trace, so this replay
    is deterministic where the live reproduction needed timing.
    """
    incumbent: redis.RedisLock = redis.RedisLock(str(random.random()))
    incumbent.holder_id = 'm-incumbent'
    newcomer: redis.RedisLock = redis.RedisLock(str(random.random()))
    newcomer.holder_id = 'a-newcomer'

    # The incumbent's first conclusive probe still shows the reader and
    # its own pre-election record. It elects itself and waits.
    assert not incumbent._resolve_lock_holders(
        [
            redis.RedisLockHolder(
                holder_id='m-incumbent',
                mode=redis.RedisLockMode.PENDING,
                elected=False,
            ),
            redis.RedisLockHolder(
                holder_id='reader',
                mode=redis.RedisLockMode.SHARED,
            ),
        ],
        fail_when_locked=False,
    )
    elected_after_first_probe: bool = incumbent.writer_elected
    assert elected_after_first_probe

    # The newcomer's probe raced that election: the reader is gone and
    # the incumbent's reply was snapshotted before its flag was set. On
    # that view the newcomer legitimately wins the sort and promotes.
    assert newcomer._resolve_lock_holders(
        [
            redis.RedisLockHolder(
                holder_id='m-incumbent',
                mode=redis.RedisLockMode.PENDING,
                elected=False,
            ),
            redis.RedisLockHolder(
                holder_id='a-newcomer',
                mode=redis.RedisLockMode.PENDING,
                elected=False,
            ),
        ],
        fail_when_locked=False,
    )
    assert newcomer.mode is redis.RedisLockMode.EXCLUSIVE

    # The incumbent's next probe carries the newcomer's equally stale
    # elected false reply. Promoting here is the double-EXCLUSIVE bug,
    # so the incumbent must hold off and keep its election instead.
    assert not incumbent._resolve_lock_holders(
        [
            redis.RedisLockHolder(
                holder_id='m-incumbent',
                mode=redis.RedisLockMode.PENDING,
                elected=True,
            ),
            redis.RedisLockHolder(
                holder_id='a-newcomer',
                mode=redis.RedisLockMode.PENDING,
                elected=False,
            ),
        ],
        fail_when_locked=False,
    )
    held_mode: redis.RedisLockMode = incumbent.mode
    assert held_mode is redis.RedisLockMode.PENDING
    elected_during_hold_off: bool = incumbent.writer_elected
    assert elected_during_hold_off
    incumbent_mode: redis.RedisLockMode = incumbent.mode
    newcomer_mode: redis.RedisLockMode = newcomer.mode
    assert not (
        incumbent_mode is redis.RedisLockMode.EXCLUSIVE
        and newcomer_mode is redis.RedisLockMode.EXCLUSIVE
    )

    # One round later the newcomer is visible as exclusive and the
    # forfeit rules take over: the incumbent backs off cleanly.
    incumbent.pubsub = _idle_pubsub()
    assert not incumbent._resolve_lock_holders(
        [
            redis.RedisLockHolder(
                holder_id='m-incumbent',
                mode=redis.RedisLockMode.PENDING,
                elected=True,
            ),
            redis.RedisLockHolder(
                holder_id='a-newcomer',
                mode=redis.RedisLockMode.EXCLUSIVE,
                elected=True,
            ),
        ],
        fail_when_locked=False,
    )
    assert not incumbent.writer_elected
    remaining_pubsub: client.PubSub | None = incumbent.pubsub
    assert remaining_pubsub is None


def test_redis_incumbent_promotes_past_higher_id_newcomer() -> None:
    """A higher-id undecided newcomer does not delay the promotion.

    Even on a stale view a higher id can never win the sort against
    this incumbent, so there is nothing to wait out.
    """
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))
    lock.holder_id = 'a-incumbent'
    lock.writer_elected = True

    assert lock._resolve_lock_holders(
        [
            redis.RedisLockHolder(
                holder_id='a-incumbent',
                mode=redis.RedisLockMode.PENDING,
                elected=True,
            ),
            redis.RedisLockHolder(
                holder_id='z-newcomer',
                mode=redis.RedisLockMode.PENDING,
                elected=False,
            ),
        ],
        fail_when_locked=False,
    )
    assert lock.mode is redis.RedisLockMode.EXCLUSIVE
    assert lock.writer_elected


def test_redis_incumbent_forfeits_to_old_format_lower_id() -> None:
    """An incumbent forfeits to a lower-id pre-4.2 pending writer.

    A record without the ``elected`` field comes from a 4.0 or 4.1
    holder, which runs the plain id election and cannot be told to
    defer. Forfeiting reproduces the pre-4.2 semantics exactly, so a
    mixed channel is never less safe than 4.1.
    """
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))
    lock.holder_id = 'z-incumbent'
    lock.writer_elected = True
    lock.pubsub = _idle_pubsub()
    holders: list[redis.RedisLockHolder] = [
        redis.RedisLockHolder(
            holder_id='z-incumbent',
            mode=redis.RedisLockMode.PENDING,
            elected=True,
        ),
        redis.RedisLockHolder(
            holder_id='a-old-writer',
            mode=redis.RedisLockMode.PENDING,
        ),
        redis.RedisLockHolder(
            holder_id='reader',
            mode=redis.RedisLockMode.SHARED,
        ),
    ]

    assert not lock._resolve_lock_holders(holders, fail_when_locked=False)

    assert not lock.writer_elected
    assert lock.pubsub is None


def test_redis_incumbent_forfeits_to_exclusive_holder() -> None:
    """An incumbent forfeits when anybody owns the lock exclusively.

    The probe outranks the incumbent's memory regardless of holder ids,
    and legacy holders are recorded as exclusive, so this rule also
    covers every reply the incumbent cannot reason about.
    """
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))
    lock.holder_id = 'a-incumbent'
    lock.writer_elected = True
    lock.pubsub = _idle_pubsub()
    holders: list[redis.RedisLockHolder] = [
        redis.RedisLockHolder(
            holder_id='a-incumbent',
            mode=redis.RedisLockMode.PENDING,
            elected=True,
        ),
        redis.RedisLockHolder(
            holder_id='z-owner',
            mode=redis.RedisLockMode.EXCLUSIVE,
            elected=True,
        ),
    ]

    assert not lock._resolve_lock_holders(holders, fail_when_locked=False)

    assert not lock.writer_elected
    assert lock.pubsub is None


def test_redis_two_incumbents_resolve_by_holder_id() -> None:
    """Two incumbents resolve deterministically: lower id keeps.

    Both advertise ``elected: true`` after a reply-staleness race let
    them win overlapping elections. Each computes the same answer from
    the same records, so the lower id keeps the election and the higher
    id forfeits within one probe round.
    """
    low: redis.RedisLock = redis.RedisLock(str(random.random()))
    low.holder_id = 'a-low'
    low.writer_elected = True
    high: redis.RedisLock = redis.RedisLock(str(random.random()))
    high.holder_id = 'z-high'
    high.writer_elected = True
    high.pubsub = _idle_pubsub()
    holders: list[redis.RedisLockHolder] = [
        redis.RedisLockHolder(
            holder_id='a-low',
            mode=redis.RedisLockMode.PENDING,
            elected=True,
        ),
        redis.RedisLockHolder(
            holder_id='z-high',
            mode=redis.RedisLockMode.PENDING,
            elected=True,
        ),
        redis.RedisLockHolder(
            holder_id='reader',
            mode=redis.RedisLockMode.SHARED,
        ),
    ]

    assert not low._resolve_lock_holders(holders, fail_when_locked=False)
    assert low.writer_elected

    assert not high._resolve_lock_holders(holders, fail_when_locked=False)
    assert not high.writer_elected
    assert high.pubsub is None


def test_redis_parse_lock_response_reads_elected_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The elected field parses as bool or None and rides protocol 1.

    `None` marks a record that predates the field, which is the signal
    the mixed-cluster fallback keys on, so a non-bool value degrades to
    `None` rather than to a guess.
    """
    connection: fakeredis.FakeStrictRedis = fakeredis.FakeStrictRedis(
        server=fakeredis.FakeServer(),
        decode_responses=True,
    )
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=connection,
    )
    base: dict[str, typing.Any] = {
        'holder_id': 'peer',
        'mode': 'pending',
        'protocol': 1,
    }

    assert (
        lock._parse_lock_response(
            json.dumps(dict(base, elected=True)),
            0,
        ).elected
        is True
    )
    assert (
        lock._parse_lock_response(
            json.dumps(dict(base, elected=False)),
            0,
        ).elected
        is False
    )
    assert lock._parse_lock_response(json.dumps(base), 0).elected is None
    assert (
        lock._parse_lock_response(
            json.dumps(dict(base, elected='yes')),
            0,
        ).elected
        is None
    )

    # The reply this lock publishes itself carries the field while the
    # protocol version stays 1, so 4.0 and 4.1 peers keep parsing it.
    published: list[tuple[str, str]] = []
    monkeypatch.setattr(
        connection,
        'publish',
        lambda channel, message: published.append((channel, message)),
    )
    lock.writer_elected = True
    lock.channel_handler(
        {
            'type': 'message',
            'data': json.dumps({'response_channel': 'resp'}),
        }
    )
    assert json.loads(published[0][1]) == {
        'holder_id': lock.holder_id,
        'mode': 'pending',
        'protocol': 1,
        'elected': True,
    }


def test_redis_nonblocking_inconclusive_probe_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With fail_when_locked an inconclusive probe retries, not raises.

    An inconclusive probe is noise, not contention: nobody demonstrably
    holds the channel, so the attempt is repeated inside the timeout and
    the second, conclusive probe wins the election and promotes.
    """
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
    probes: list[list[redis.RedisLockHolder] | None] = [
        None,
        [
            redis.RedisLockHolder(
                holder_id='writer',
                mode=redis.RedisLockMode.PENDING,
                elected=False,
            ),
        ],
    ]

    def start_subscription(connection_: client.Redis) -> None:
        lock.pubsub = _idle_pubsub()

    monkeypatch.setattr(lock, '_start_subscription', start_subscription)
    monkeypatch.setattr(lock, '_get_subscriber_count', lambda connection_: 2)
    monkeypatch.setattr(
        lock,
        '_collect_lock_holders',
        lambda connection_, expected_subscribers, timeout: probes.pop(0),
    )

    assert lock.acquire(fail_when_locked=True) is lock

    assert lock.mode is redis.RedisLockMode.EXCLUSIVE
    assert probes == []
    lock.pubsub = None
    connection.close()


def test_redis_nonblocking_zero_timeout_keeps_single_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """timeout=0 bounds a fail_when_locked acquire to one attempt.

    A permanently inconclusive channel still fails after a single probe,
    which is the knob for callers that want a hard single round trip.
    """
    connection: fakeredis.FakeStrictRedis = fakeredis.FakeStrictRedis(
        server=fakeredis.FakeServer(),
        decode_responses=True,
    )
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=connection,
        check_interval=0.001,
    )
    probe_calls: list[int] = []

    def collect_lock_holders(
        connection_: client.Redis,
        expected_subscribers: int,
        timeout: float,
    ) -> None:
        probe_calls.append(expected_subscribers)
        return

    def start_subscription(connection_: client.Redis) -> None:
        lock.pubsub = _idle_pubsub()

    monkeypatch.setattr(lock, '_start_subscription', start_subscription)
    monkeypatch.setattr(lock, '_get_subscriber_count', lambda connection_: 2)
    monkeypatch.setattr(lock, '_collect_lock_holders', collect_lock_holders)

    with pytest.raises(portalocker.AlreadyLocked):
        lock.acquire(timeout=0, fail_when_locked=True)

    assert probe_calls == [2]
    assert lock.pubsub is None
    connection.close()


@pytest.mark.timeout(180)
def test_redis_two_nonblocking_writers_exactly_one_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a free channel exactly one of two fail_when_locked writers wins.

    Regression test for issue #143 defect 1. Both contenders start
    through a barrier so the fast-path and probe-path interleavings both
    get exercised across the iterations. Before the fix both could
    raise ``AlreadyLocked`` on a channel nobody held.
    """
    server: fakeredis.FakeServer = fakeredis.FakeServer()

    def connect() -> client.Redis:
        return fakeredis.FakeStrictRedis(server=server, decode_responses=True)

    # fakeredis does not implement CLIENT KILL. Stale-holder cleanup is
    # covered independently. This test isolates the election outcome.
    monkeypatch.setattr(
        redis.RedisLock,
        '_kill_unavailable_locks',
        _ignore_stale_cleanup,
    )

    for _ in range(20):
        acquired: list[redis.RedisLock]
        failed: list[redis.RedisLock]
        errors: list[BaseException]
        acquired, failed, errors = _race_nonblocking_writers(connect)

        assert not errors
        assert len(acquired) == 1, 'exactly one contender must win'
        assert len(failed) == 1, 'exactly one contender must lose'
        assert failed[0].pubsub is None
        assert failed[0].thread is None
        acquired[0].release()
        for lock in acquired + failed:
            if lock.connection is not None:
                lock.connection.close()


def _acquire_nonblocking(
    lock: redis.RedisLock,
    barrier: threading.Barrier,
    acquired: list[redis.RedisLock],
    failed: list[redis.RedisLock],
    errors: list[BaseException],
) -> None:
    """Race one fail_when_locked acquire from behind the barrier."""
    barrier.wait()
    try:
        lock.acquire()
    except portalocker.AlreadyLocked:
        failed.append(lock)
    except BaseException as exception:  # pragma: no cover
        errors.append(exception)
    else:
        acquired.append(lock)


def _race_nonblocking_writers(
    connect: ConnectionFactory,
) -> tuple[
    list[redis.RedisLock],
    list[redis.RedisLock],
    list[BaseException],
]:
    """Race two fail_when_locked writers on one free channel.

    Returns the winners, the losers, and any unexpected errors.
    """
    channel: str = str(random.random())
    locks: list[redis.RedisLock] = []
    for holder_id in ('a-writer', 'z-writer'):
        lock: redis.RedisLock = redis.RedisLock(
            channel,
            connection=connect(),
            timeout=5,
            check_interval=0.02,
            unavailable_timeout=2,
            thread_sleep_time=0.01,
            fail_when_locked=True,
        )
        lock.holder_id = holder_id
        locks.append(lock)
    barrier: threading.Barrier = threading.Barrier(2)
    acquired: list[redis.RedisLock] = []
    failed: list[redis.RedisLock] = []
    errors: list[BaseException] = []
    threads: list[threading.Thread] = [
        threading.Thread(
            target=_acquire_nonblocking,
            args=(lock, barrier, acquired, failed, errors),
        )
        for lock in locks
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()
    return acquired, failed, errors


@pytest.mark.timeout(180)
def test_redis_elected_writer_survives_lower_id_newcomer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An elected writer is not usurped by a lower-id newcomer.

    Regression test for issue #143 defect 2. The incumbent keeps its
    election while the reader drains, takes the lock first when the
    reader releases, and only then does the newcomer get its turn.
    """
    server: fakeredis.FakeServer = fakeredis.FakeServer()

    def connect() -> client.Redis:
        return fakeredis.FakeStrictRedis(server=server, decode_responses=True)

    # fakeredis does not implement CLIENT KILL. Stale-holder cleanup is
    # covered independently. This test isolates incumbency protection.
    monkeypatch.setattr(
        redis.RedisLock,
        '_kill_unavailable_locks',
        _ignore_stale_cleanup,
    )
    channel: str = str(random.random())
    reader: redis.RedisLock = redis.RedisLock(
        channel,
        connection=connect(),
        flags=portalocker.LockFlags.SHARED,
    )
    # Timeouts are sized for heavily loaded CI runners. The assertions
    # below never wait for these upper bounds on the happy path.
    incumbent: redis.RedisLock = redis.RedisLock(
        channel,
        connection=connect(),
        timeout=60,
        check_interval=0.02,
        unavailable_timeout=5,
        thread_sleep_time=0.01,
    )
    newcomer: redis.RedisLock = redis.RedisLock(
        channel,
        connection=connect(),
        timeout=60,
        check_interval=0.02,
        unavailable_timeout=5,
        thread_sleep_time=0.01,
    )
    incumbent.holder_id = 'z-incumbent'
    newcomer.holder_id = 'a-newcomer'
    acquired: list[str] = []
    errors: list[BaseException] = []

    def acquire(lock: redis.RedisLock, name: str) -> None:
        try:
            lock.acquire()
            acquired.append(name)
        except BaseException as exception:  # pragma: no cover
            errors.append(exception)

    reader.acquire()
    incumbent_thread: threading.Thread = threading.Thread(
        target=acquire,
        args=(incumbent, 'incumbent'),
    )
    newcomer_thread: threading.Thread = threading.Thread(
        target=acquire,
        args=(newcomer, 'a-newcomer'),
    )
    incumbent_thread.start()
    _wait_for_subscribers(reader, 2)
    election_deadline: float = time.monotonic() + 30
    while (
        not incumbent.writer_elected and time.monotonic() < election_deadline
    ):
        time.sleep(0.001)
    assert incumbent.writer_elected

    newcomer_thread.start()
    _wait_for_subscribers(reader, 3)
    # Over a bounded window the incumbent keeps its election and the
    # newcomer stays out. Before the fix the newcomer's lower id won
    # the rerun election and the incumbent forfeited here.
    observation_deadline: float = time.monotonic() + 1
    while time.monotonic() < observation_deadline:
        assert incumbent.writer_elected
        assert acquired == []
        time.sleep(0.005)

    reader.release()
    acquired_deadline: float = time.monotonic() + 30
    while (
        not acquired and not errors and (time.monotonic() < acquired_deadline)
    ):
        time.sleep(0.001)
    if errors:  # pragma: no cover
        raise errors[0]
    assert acquired == ['incumbent']
    assert incumbent.mode is redis.RedisLockMode.EXCLUSIVE

    incumbent.release()
    newcomer_thread.join(timeout=60)
    assert acquired == ['incumbent', 'a-newcomer']
    newcomer.release()
    incumbent_thread.join(timeout=10)
    assert not incumbent_thread.is_alive()
    assert not newcomer_thread.is_alive()
    assert not errors


def _watch_for_exclusive_overlap(
    incumbent: redis.RedisLock,
    newcomer: redis.RedisLock,
    seconds: float,
) -> None:
    """Assert the two writers are never exclusive at the same time.

    Samples both locks for ``seconds``: a subscribed lock in
    `RedisLockMode.EXCLUSIVE` holds the channel, and two of those at
    once is the mutual exclusion break this soak hunts.
    """
    deadline: float = time.monotonic() + seconds
    while time.monotonic() < deadline:
        both_exclusive: bool = (
            incumbent.mode is redis.RedisLockMode.EXCLUSIVE
            and incumbent.pubsub is not None
            and newcomer.mode is redis.RedisLockMode.EXCLUSIVE
            and newcomer.pubsub is not None
        )
        assert not both_exclusive, 'two exclusive holders on one channel'
        time.sleep(0.0005)


def _drain_writer_threads(
    threads: list[threading.Thread],
    acquired: list[redis.RedisLock],
) -> None:
    """Release finished writers until every acquire thread has ended."""
    deadline: float = time.monotonic() + 60
    while time.monotonic() < deadline and any(
        thread.is_alive() for thread in threads
    ):
        while acquired:
            acquired.pop().release()
        time.sleep(0.002)
    while acquired:
        acquired.pop().release()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()


def _stage_stale_newcomer_round(connect: ConnectionFactory) -> None:
    """Run one round of the #143 stale-newcomer schedule.

    The reader releases while the incumbent's first probe is mid-drain
    and a lower-id newcomer starts immediately, at a check interval
    short enough to expose the reply-staleness window. This is the
    schedule that reproduced a double-EXCLUSIVE before the hold-off in
    ``_resolve_exclusive_writer`` existed.
    """
    channel: str = str(random.random())
    kwargs: dict[str, typing.Any] = dict(
        timeout=20,
        check_interval=0.02,
        unavailable_timeout=1,
        thread_sleep_time=0.1,
    )
    reader: redis.RedisLock = redis.RedisLock(
        channel,
        connection=connect(),
        flags=portalocker.LockFlags.SHARED,
        **kwargs,
    )
    incumbent: redis.RedisLock = redis.RedisLock(
        channel,
        connection=connect(),
        **kwargs,
    )
    newcomer: redis.RedisLock = redis.RedisLock(
        channel,
        connection=connect(),
        **kwargs,
    )
    incumbent.holder_id = 'm-incumbent'
    newcomer.holder_id = 'a-newcomer'
    acquired: list[redis.RedisLock] = []
    errors: list[BaseException] = []

    def attempt(lock: redis.RedisLock) -> None:
        try:
            lock.acquire()
        except BaseException as exception:  # pragma: no cover
            errors.append(exception)
        else:
            acquired.append(lock)

    reader.acquire()
    threads: list[threading.Thread] = [
        threading.Thread(target=attempt, args=(lock,), daemon=True)
        for lock in (incumbent, newcomer)
    ]
    threads[0].start()
    _wait_for_subscribers(reader, 2)
    time.sleep(0.008)
    reader.release()
    threads[1].start()

    _watch_for_exclusive_overlap(incumbent, newcomer, seconds=0.7)
    _drain_writer_threads(threads, acquired)
    assert not errors
    for lock in (reader, incumbent, newcomer):
        if lock.connection is not None:
            lock.connection.close()


@pytest.mark.timeout(180)
def test_redis_stale_newcomer_soak_never_two_exclusive() -> None:
    """Live-redis timing soak of the #143 hold-off, ten rounds.

    The deterministic staged replay lives in
    `test_redis_incumbent_holds_off_for_stale_lower_id_newcomer`. This
    soak lets real probe timing roll the same dice against a live
    server, where the pre-fix code produced roughly one double per four
    rounds at this check interval.
    """
    _ensure_live_redis_available(_LIVE_REDIS)
    for _ in range(10):
        _stage_stale_newcomer_round(_live_redis_connection)


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
        with pytest.raises(portalocker.LockException, match='already active'):
            lock_a.acquire()
    time.sleep(0.01)

    lock_a.release()


def test_redis_contended_retry_with_self_created_connection(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for issue #136.

    A waiter that creates its own connection (no ``connection=`` argument)
    used to break on its first contended retry: the release-to-retry closed
    and cleared ``self.connection`` while ``acquire`` kept resubscribing on
    a stale local reference, so ``channel_handler`` hit its
    ``assert self.connection is not None`` on the worker thread and
    ``PubSubWorkerThread.run`` escalated that to ``interrupt_main``,
    delivering a ``KeyboardInterrupt`` to the waiting main thread.
    """
    channel: str = str(random.random())

    interrupts: list[None] = []
    monkeypatch.setattr(
        _thread, 'interrupt_main', lambda: interrupts.append(None)
    )

    thread_errors: list[threading.ExceptHookArgs] = []
    monkeypatch.setattr(threading, 'excepthook', thread_errors.append)

    # The bug needs close_connection=True, so the locks must create their
    # own connections. The helper routes the lazy connection creation to
    # this test's (fake or live) server instead of passing a connection in.
    holder: redis.RedisLock = _self_connecting_lock(
        redis_connection, monkeypatch, channel, timeout=5, check_interval=0.05
    )
    waiter: redis.RedisLock = _self_connecting_lock(
        redis_connection, monkeypatch, channel, timeout=5, check_interval=0.05
    )

    holder.acquire()
    release_timer: threading.Timer = threading.Timer(0.5, holder.release)
    release_timer.start()
    try:
        # The waiter must retry against the held lock without crashing its
        # worker thread and acquire once the holder lets go.
        with waiter:
            pass
    finally:
        release_timer.join()
        holder.release()
        waiter.release()

    assert not interrupts, 'worker thread escalated a failure to main'
    assert not thread_errors, f'worker thread died: {thread_errors}'


def _self_connecting_lock(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
    channel: str,
    **kwargs: typing.Any,
) -> redis.RedisLock:
    """Build a lock that lazily creates its own connection (no
    ``connection=`` argument, so ``close_connection`` stays True) while
    still connecting to this test's fake or live server.
    """

    def get_connection(self: redis.RedisLock) -> client.Redis:
        if not self.connection:
            self.connection = redis_connection()
        return self.connection

    monkeypatch.setattr(redis.RedisLock, 'get_connection', get_connection)
    return redis.RedisLock(
        channel,
        unavailable_timeout=0.2,
        thread_sleep_time=0.01,
        **kwargs,
    )


def test_redis_fail_when_locked_closes_created_connection(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A contended fail_when_locked attempt must fully tear down: the
    lock-created connection is closed and cleared, not left idling.
    """
    channel: str = str(random.random())
    holder: redis.RedisLock = _self_connecting_lock(
        redis_connection, monkeypatch, channel, timeout=5
    )
    waiter: redis.RedisLock = _self_connecting_lock(
        redis_connection, monkeypatch, channel, fail_when_locked=True
    )

    holder.acquire()
    try:
        with pytest.raises(portalocker.AlreadyLocked):
            waiter.acquire()
    finally:
        holder.release()

    assert waiter.connection is None
    assert waiter.pubsub is None
    assert waiter.thread is None


def test_redis_timeout_expiry_closes_created_connection(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A waiter that gives up on timeout leaves no connection behind."""
    channel: str = str(random.random())
    holder: redis.RedisLock = _self_connecting_lock(
        redis_connection, monkeypatch, channel, timeout=5
    )
    waiter: redis.RedisLock = _self_connecting_lock(
        redis_connection,
        monkeypatch,
        channel,
        timeout=0.3,
        check_interval=0.05,
    )

    holder.acquire()
    try:
        with pytest.raises(portalocker.AlreadyLocked):
            waiter.acquire()
    finally:
        holder.release()

    assert waiter.connection is None
    assert waiter.pubsub is None
    assert waiter.thread is None


def test_redis_retry_keeps_caller_supplied_connection(
    redis_connection: ConnectionFactory,
) -> None:
    """Contended retries never close or replace a caller-supplied
    connection.
    """
    channel: str = str(random.random())
    holder: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        timeout=5,
        unavailable_timeout=0.2,
        thread_sleep_time=0.01,
    )
    waiter_connection: client.Redis = redis_connection()
    waiter: redis.RedisLock = redis.RedisLock(
        channel,
        connection=waiter_connection,
        timeout=0.3,
        check_interval=0.05,
        unavailable_timeout=0.2,
        thread_sleep_time=0.01,
    )

    holder.acquire()
    try:
        with pytest.raises(portalocker.AlreadyLocked):
            waiter.acquire()
    finally:
        holder.release()

    assert waiter.connection is waiter_connection
    assert waiter_connection.ping()


def test_redis_channel_handler_without_connection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ping arriving after the connection is gone is dropped with an
    error instead of raising (which the worker thread would escalate to
    ``interrupt_main``).
    """
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))
    assert lock.connection is None

    with caplog.at_level(logging.ERROR, logger='portalocker.redis'):
        lock.channel_handler(
            {
                'type': 'message',
                'data': json.dumps({'response_channel': 'somewhere'}),
            }
        )

    assert any(
        'cannot answer ping' in record.message for record in caplog.records
    )


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
            'elected': False,
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

    The count matches when the ping goes out and differs after the
    replies are in, so the churn happened while the probe was running.
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
    subscriber_counts: list[int] = [2, 1]
    monkeypatch.setattr(lock, '_get_pubsub', lambda connection: pubsub)
    monkeypatch.setattr(
        lock,
        '_get_subscriber_count',
        lambda connection: subscriber_counts.pop(0),
    )
    monkeypatch.setattr(connection, 'publish', lambda channel, message: 0)

    holders: list[redis.RedisLockHolder] | None = lock._collect_lock_holders(
        connection,
        expected_subscribers=2,
        timeout=0.01,
    )

    assert holders is None
    assert subscriber_counts == []


def test_redis_collect_holders_aborts_before_ping_on_count_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A count that moved before the ping abandons the probe unsent.

    The subscriber count is re-checked immediately before the ping is
    published. A probe whose expectation is already stale would collect
    replies describing a channel that no longer exists in that shape, so
    it is abandoned before putting any traffic on the channel.
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
    published: list[str] = []
    monkeypatch.setattr(lock, '_get_pubsub', lambda connection: pubsub)
    monkeypatch.setattr(lock, '_get_subscriber_count', lambda connection: 3)
    monkeypatch.setattr(
        connection,
        'publish',
        lambda channel, message: published.append(channel),
    )

    holders: list[redis.RedisLockHolder] | None = lock._collect_lock_holders(
        connection,
        expected_subscribers=2,
        timeout=0.01,
    )

    assert holders is None
    assert published == []


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
    responding_id: str = 'a' * 32
    stale_id: str = 'b' * 32
    response: str = json.dumps(
        {
            'holder_id': responding_id,
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
                'name': f'stale-channel-lock-{responding_id}',
            },
            {'id': 'stale-client', 'name': f'stale-channel-lock-{stale_id}'},
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


def test_redis_kill_unavailable_locks_spares_other_channels(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for issue #142.

    A holder of channel ``<base>-lock-x`` names its connection
    ``<base>-lock-x-lock-<id>``, which starts with channel ``<base>``'s
    holder prefix ``<base>-lock-``. The prefix match used by
    ``_kill_unavailable_locks`` therefore treated it as a crashed holder
    of channel ``<base>`` and killed it, even though it was healthy and
    holding a completely different lock.
    """
    base: str = str(random.random())
    neighbour: redis.RedisLock = redis.RedisLock(
        f'{base}-lock-x',
        connection=redis_connection(),
    )
    prober: redis.RedisLock = redis.RedisLock(
        base,
        connection=redis_connection(),
    )
    killed: list[str | None] = []

    neighbour.acquire()
    prober.acquire()
    try:
        connection: client.Redis = prober.get_connection()
        monkeypatch.setattr(
            connection,
            'client_kill_filter',
            lambda client_id: killed.append(client_id),
        )
        prober._kill_unavailable_locks(
            connection,
            [
                redis.RedisLockHolder(
                    holder_id=prober.holder_id,
                    mode=redis.RedisLockMode.EXCLUSIVE,
                ),
            ],
        )
    finally:
        prober.release()
        neighbour.release()

    assert killed == []


def test_redis_kill_unavailable_locks_requires_holder_id_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only names shaped like ``<channel>-lock-<32 char hex>`` are reaped.

    An unrelated client whose name merely starts with the holder prefix
    must survive, while a silent current holder and a silent legacy
    holder are still killed.
    """
    connection: fakeredis.FakeStrictRedis = fakeredis.FakeStrictRedis(
        server=fakeredis.FakeServer(),
        decode_responses=True,
    )
    lock: redis.RedisLock = redis.RedisLock(
        'stale-channel',
        connection=connection,
    )
    responding_id: str = 'a' * 32
    stale_id: str = 'b' * 32
    killed: list[str | None] = []
    monkeypatch.setattr(
        connection,
        'client_list',
        lambda: [
            {'id': '1', 'name': f'stale-channel-lock-{responding_id}'},
            {'id': '2', 'name': f'stale-channel-lock-{stale_id}'},
            {'id': '3', 'name': 'stale-channel-lock-notahexid'},
            {'id': '4', 'name': 'stale-channel-lock'},
            {'id': '5', 'name': ''},
        ],
    )
    monkeypatch.setattr(
        connection,
        'client_kill_filter',
        lambda client_id: killed.append(client_id),
    )

    lock._kill_unavailable_locks(
        connection,
        [
            redis.RedisLockHolder(
                holder_id=responding_id,
                mode=redis.RedisLockMode.SHARED,
            ),
        ],
    )

    assert killed == ['2', '4']


def test_redis_probe_drains_buffered_replies_from_many_holders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe against many holders must read every buffered reply.

    Regression test for #138. The reply loop used to read one message per
    ``_timeout_generator`` interval, which capped a probe at roughly the
    number of intervals that fit in ``unavailable_timeout``, no matter how
    fast the holders answered. With more holders than intervals every
    probe came up short and ``_kill_unavailable_locks`` killed healthy
    holders whose replies were sitting unread in the prober's own buffer.
    """
    server: fakeredis.FakeServer = fakeredis.FakeServer()

    def connect() -> client.Redis:
        return fakeredis.FakeStrictRedis(server=server, decode_responses=True)

    channel: str = str(random.random())
    holder_count: int = 25
    holders: list[redis.RedisLock] = [
        redis.RedisLock(
            channel,
            connection=connect(),
            flags=portalocker.LockFlags.SHARED,
            thread_sleep_time=0.01,
        )
        for _ in range(holder_count)
    ]
    # The default timings reproduce the bug deterministically: with
    # ``check_interval = min(0.1, 1 / 10)`` the generator yields at most
    # 21 times inside the one second timeout, so a one-message-per-yield
    # loop can never collect the 26 replies this probe needs.
    prober: redis.RedisLock = redis.RedisLock(
        channel,
        connection=connect(),
        thread_sleep_time=0.1,
        unavailable_timeout=1,
    )
    kill_calls: list[list[redis.RedisLockHolder]] = []

    def record_kill(
        connection_: client.Redis,
        responding_holders: typing.Iterable[redis.RedisLockHolder],
    ) -> None:
        kill_calls.append(list(responding_holders))

    monkeypatch.setattr(prober, '_kill_unavailable_locks', record_kill)

    try:
        for holder in holders:
            holder._start_subscription(holder.get_connection())
        prober._start_subscription(prober.get_connection())
        _wait_for_subscribers(prober, holder_count + 1)

        probe: list[redis.RedisLockHolder] | None = (
            prober._collect_lock_holders(
                prober.get_connection(),
                expected_subscribers=holder_count + 1,
                timeout=prober.unavailable_timeout,
            )
        )
    finally:
        prober.release()
        for holder in holders:
            holder.release()

    assert kill_calls == []
    assert probe is not None
    assert len(probe) == holder_count + 1


def test_redis_collect_holders_skips_control_frames_while_draining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stray control frame between replies is skipped, not counted.

    The drain loop reads every buffered frame within one polling
    interval, so it can run into control frames such as a late
    confirmation. Those must be skipped without ending the drain or
    being counted as replies.
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
    response: str = json.dumps(
        {
            'holder_id': 'responding',
            'mode': 'shared',
            'protocol': 1,
        }
    )
    pubsub: _ResponsePubSub = _ResponsePubSub(
        [response],
        confirmations=[{'type': 'subscribe'}, {'type': 'unsubscribe'}],
    )
    monkeypatch.setattr(lock, '_get_pubsub', lambda connection: pubsub)
    monkeypatch.setattr(lock, '_get_subscriber_count', lambda connection: 1)
    monkeypatch.setattr(connection, 'publish', lambda channel, message: 1)

    holders: list[redis.RedisLockHolder] | None = lock._collect_lock_holders(
        connection,
        expected_subscribers=1,
        timeout=0.01,
    )

    assert holders == [
        redis.RedisLockHolder('responding', redis.RedisLockMode.SHARED)
    ]


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

    connection: typing.Any = None

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

    If ``self.pubsub`` were left set, the already-active guard at the top
    of ``acquire`` would turn every retry into a ``LockException`` instead
    of surfacing the real error.
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

    # Retry on the *same* instance must surface the real error again, not a
    # LockException from a stale ``self.pubsub``.
    with pytest.raises(_SubscribeError):
        lock.acquire()


class _ConfirmationPubSub:
    """Stand-in pubsub for ``_start_subscription`` ordering tests.

    Records every call in order. ``get_message`` replays *confirmations*
    one frame per call and, once they run out, blocks for ``timeout`` and
    returns ``None`` like the real client does on an idle connection.
    ``subscribed`` mirrors redis-py's send-time semantics: it is set the
    moment ``subscribe`` is called, not when the server confirms.
    """

    def __init__(
        self,
        calls: list[str],
        confirmations: list[dict[str, typing.Any] | None],
    ) -> None:
        self._calls: list[str] = calls
        self._confirmations: list[dict[str, typing.Any] | None] = confirmations
        self.subscribed: bool = False

    def execute_command(self, *args: typing.Any) -> None:
        self._calls.append('execute_command')

    def parse_response(self) -> None:
        self._calls.append('parse_response')

    def subscribe(self, **channels: typing.Any) -> None:
        self._calls.append('subscribe')
        self.subscribed = True

    def get_message(self, timeout: float) -> dict[str, typing.Any] | None:
        self._calls.append('get_message')
        if self._confirmations:
            return self._confirmations.pop(0)
        time.sleep(timeout)
        return None

    def unsubscribe(self, *channels: str) -> None:
        self._calls.append('unsubscribe')

    def close(self) -> None:
        self._calls.append('close')


def _make_stub_worker_thread(calls: list[str]) -> type:
    """Build a worker-thread stand-in that records when it is started."""

    class StubWorkerThread:
        def __init__(
            self,
            pubsub: typing.Any,
            sleep_time: float,
            daemon: bool = False,
        ) -> None:
            calls.append('thread_created')

        def start(self) -> None:
            calls.append('thread_start')

        def stop(self) -> None:
            calls.append('thread_stop')

        def join(self) -> None:
            calls.append('thread_join')

    return StubWorkerThread


def test_redis_start_subscription_waits_for_subscribe_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The subscribe confirmation is drained before the worker starts.

    The worker thread consumes frames invisibly, so the confirmation must
    be read on the main thread first: processing it proves the server has
    registered the subscription, which is what makes a later ``PUBSUB
    NUMSUB`` count this holder. A bare ``time.sleep`` barrier gives no
    such guarantee, so no sleep at all may remain on this path.
    """
    connection: fakeredis.FakeStrictRedis = fakeredis.FakeStrictRedis(
        server=fakeredis.FakeServer(),
        decode_responses=True,
    )
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=connection,
        thread_sleep_time=0.001,
        unavailable_timeout=0.2,
    )
    calls: list[str] = []
    pubsub: _ConfirmationPubSub = _ConfirmationPubSub(
        calls,
        confirmations=[{'type': 'subscribe'}],
    )
    monkeypatch.setattr(lock, '_get_pubsub', lambda connection: pubsub)
    monkeypatch.setattr(
        redis,
        'PubSubWorkerThread',
        _make_stub_worker_thread(calls),
    )
    sleeps: list[float] = []
    monkeypatch.setattr(time, 'sleep', sleeps.append)

    lock._start_subscription(connection)

    assert pubsub.subscribed
    assert calls.index('subscribe') < calls.index('get_message')
    assert calls.index('get_message') < calls.index('thread_start')
    assert sleeps == []
    lock.pubsub = None
    lock.thread = None


def test_redis_start_subscription_raises_without_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No confirmation within the timeout fails the subscription.

    Returning without the confirmation would let ``acquire`` count
    subscribers before the server registered this one, which is exactly
    the fast-path race the wait exists to close. The failure must roll
    back like any other subscription error: pubsub closed and cleared,
    no worker thread started.
    """
    connection: fakeredis.FakeStrictRedis = fakeredis.FakeStrictRedis(
        server=fakeredis.FakeServer(),
        decode_responses=True,
    )
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=connection,
        thread_sleep_time=0.001,
        unavailable_timeout=0.05,
    )
    calls: list[str] = []
    pubsub: _ConfirmationPubSub = _ConfirmationPubSub(
        calls,
        confirmations=[None, {'type': 'message'}],
    )
    monkeypatch.setattr(lock, '_get_pubsub', lambda connection: pubsub)
    monkeypatch.setattr(
        redis,
        'PubSubWorkerThread',
        _make_stub_worker_thread(calls),
    )

    with pytest.raises(portalocker.LockException, match='confirm'):
        lock._start_subscription(connection)

    assert lock.pubsub is None
    assert lock.thread is None
    assert 'thread_start' not in calls
    assert 'close' in calls


def test_redis_channel_handler_serializes_with_promotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ping answer cannot be computed while a promotion is in flight.

    The handler snapshots ``(holder_id, mode)`` under ``_mode_lock``, the
    same lock every promotion takes, so an answer that starts after a
    promotion began reports the promoted mode. Before the fix the handler
    read ``self.mode`` unsynchronized and could answer ``pending`` for a
    writer already committed to promoting, letting a lower-id writer
    elect itself as a second exclusive holder.
    """
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
    answered: threading.Event = threading.Event()

    def handle() -> None:
        lock.channel_handler(
            {
                'type': 'message',
                'data': json.dumps({'response_channel': 'resp'}),
            }
        )
        answered.set()

    handler_thread: threading.Thread = threading.Thread(target=handle)
    lock._mode_lock.acquire()
    try:
        # The promotion is in flight: the handler must not answer yet.
        handler_thread.start()
        assert not answered.wait(timeout=0.2)
        assert published == []
        lock.mode = redis.RedisLockMode.EXCLUSIVE
    finally:
        lock._mode_lock.release()

    handler_thread.join(timeout=10)
    assert answered.is_set()
    assert len(published) == 1
    answer: dict[str, typing.Any] = json.loads(published[0][1])
    assert answer['mode'] == 'exclusive'
    assert answer['holder_id'] == lock.holder_id


class _RecordingModeLock:
    """Context-manager stand-in for ``_mode_lock`` that counts entries."""

    def __init__(self) -> None:
        self.entries: int = 0

    def __enter__(self) -> '_RecordingModeLock':
        self.entries += 1
        return self

    def __exit__(self, *exc_info: object) -> None:
        pass


def test_redis_resolve_promotion_takes_mode_lock() -> None:
    """The elected-writer promotion runs under ``_mode_lock``."""
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))
    lock.holder_id = 'writer'
    recording: _RecordingModeLock = _RecordingModeLock()
    lock._mode_lock = typing.cast('threading.Lock', recording)
    holders: list[redis.RedisLockHolder] = [
        redis.RedisLockHolder(
            holder_id=lock.holder_id,
            mode=redis.RedisLockMode.PENDING,
        ),
    ]

    assert lock._resolve_lock_holders(holders, fail_when_locked=False)

    assert lock.mode is redis.RedisLockMode.EXCLUSIVE
    assert recording.entries == 1


def test_redis_fast_path_promotion_takes_mode_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The uncontended fast-path promotion runs under ``_mode_lock``.

    Two entries are expected: the pre-loop reset of ``(mode,
    writer_elected)`` and the fast-path promotion itself.
    """
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
    recording: _RecordingModeLock = _RecordingModeLock()
    lock._mode_lock = typing.cast('threading.Lock', recording)
    sentinel_pubsub: client.PubSub = typing.cast('client.PubSub', object())

    def start_subscription(connection_: client.Redis) -> None:
        lock.pubsub = sentinel_pubsub

    monkeypatch.setattr(lock, '_start_subscription', start_subscription)
    monkeypatch.setattr(lock, '_get_subscriber_count', lambda connection: 1)

    assert lock.acquire() is lock

    assert lock.mode is redis.RedisLockMode.EXCLUSIVE
    assert recording.entries == 2
    lock.pubsub = None
    connection.close()


def test_redis_start_subscription_returns_subscribed(
    redis_connection: ConnectionFactory,
) -> None:
    """Against a real (fake or live) server the pubsub is subscribed."""
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
    )
    lock._start_subscription(lock.get_connection())
    try:
        assert lock.pubsub is not None
        assert lock.pubsub.subscribed
    finally:
        lock.release()


class _TeardownError(Exception):
    """Raised by teardown stubs to simulate a dead Redis connection."""


class _TeardownPubSub:
    """Stand-in pubsub for exception-safe teardown tests.

    Records ``unsubscribe`` and ``close`` calls in order and raises the
    configured errors, mimicking a pubsub whose connection died while
    the lock was held.
    """

    def __init__(
        self,
        events: list[str],
        *,
        connection: object | None = None,
        unsubscribe_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.events: list[str] = events
        self.connection: object | None = connection
        self.unsubscribe_error: Exception | None = unsubscribe_error
        self.close_error: Exception | None = close_error

    def unsubscribe(self, *channels: str) -> None:
        self.events.append('unsubscribe')
        if self.unsubscribe_error is not None:
            raise self.unsubscribe_error

    def close(self) -> None:
        self.events.append('close')
        if self.close_error is not None:
            raise self.close_error


class _BrokenThread:
    """Stand-in worker thread whose ``stop`` raises."""

    ident: int | None = None

    def __init__(self, error: Exception) -> None:
        self.error: Exception = error

    def stop(self) -> None:
        raise self.error

    def join(self) -> None:  # pragma: no cover - must not be reached
        raise AssertionError('join must not run when stop fails')


def test_redis_release_survives_unsubscribe_error(
    redis_connection: ConnectionFactory,
) -> None:
    """A failing UNSUBSCRIBE must not brick the lock instance.

    The pubsub is closed and cleared anyway, the error still propagates,
    and the same instance can acquire again once Redis is back.
    """
    events: list[str] = []
    unsubscribe_error: _TeardownError = _TeardownError('unsubscribe failed')
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
    )
    lock.pubsub = typing.cast(
        'client.PubSub | None',
        _TeardownPubSub(
            events,
            connection=object(),
            unsubscribe_error=unsubscribe_error,
        ),
    )

    exc_info: pytest.ExceptionInfo[_TeardownError]
    with pytest.raises(_TeardownError) as exc_info:
        lock.release()

    assert exc_info.value is unsubscribe_error
    assert events == ['unsubscribe', 'close']
    assert lock.pubsub is None
    assert lock.thread is None

    # The instance is not bricked: a later acquire works.
    lock.acquire()
    lock.release()


def test_redis_release_prefers_first_teardown_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The first teardown error is raised, later ones are only logged."""
    events: list[str] = []
    unsubscribe_error: _TeardownError = _TeardownError('unsubscribe failed')
    close_error: _TeardownError = _TeardownError('close failed')
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))
    lock.pubsub = typing.cast(
        'client.PubSub | None',
        _TeardownPubSub(
            events,
            connection=object(),
            unsubscribe_error=unsubscribe_error,
            close_error=close_error,
        ),
    )

    exc_info: pytest.ExceptionInfo[_TeardownError]
    with (
        caplog.at_level(logging.WARNING, logger='portalocker.redis'),
        pytest.raises(_TeardownError) as exc_info,
    ):
        lock.release()

    assert exc_info.value is unsubscribe_error
    assert events == ['unsubscribe', 'close']
    assert lock.pubsub is None
    assert any(
        'Suppressed secondary' in record.message for record in caplog.records
    )


def test_redis_release_survives_thread_stop_error() -> None:
    """A worker thread that fails to stop must not block the teardown.

    The pubsub is still closed and both ``thread`` and ``pubsub`` are
    cleared before the error propagates.
    """
    events: list[str] = []
    stop_error: _TeardownError = _TeardownError('stop failed')
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))
    lock.thread = typing.cast(
        'redis.PubSubWorkerThread | None',
        _BrokenThread(stop_error),
    )
    lock.pubsub = typing.cast(
        'client.PubSub | None',
        _TeardownPubSub(events),
    )

    exc_info: pytest.ExceptionInfo[_TeardownError]
    with pytest.raises(_TeardownError) as exc_info:
        lock.release()

    assert exc_info.value is stop_error
    # No unsubscribe: the stub pubsub has no connection left. The close
    # still ran and the state is cleared.
    assert events == ['close']
    assert lock.thread is None
    assert lock.pubsub is None


def test_redis_acquire_thread_start_failure_propagates(
    redis_connection: ConnectionFactory,
) -> None:
    """When the worker thread cannot start the original error propagates.

    The rollback used to join the never-started thread, replacing the
    real error with ``RuntimeError: cannot join thread before it is
    started`` and leaking a subscribed pubsub that kept this process
    counted as a holder.
    """
    connection: client.Redis = redis_connection()
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=connection,
    )
    start_error: RuntimeError = RuntimeError("can't start new thread")

    def broken_start(self: redis.PubSubWorkerThread) -> None:
        raise start_error

    exc_info: pytest.ExceptionInfo[RuntimeError]
    with pytest.MonkeyPatch.context() as thread_patch:
        thread_patch.setattr(redis.PubSubWorkerThread, 'start', broken_start)
        with pytest.raises(RuntimeError) as exc_info:
            lock.acquire()

    assert exc_info.value is start_error
    assert lock.pubsub is None
    assert lock.thread is None
    # The rollback really unsubscribed: nobody is left on the channel.
    subscriber_count: int = lock._get_subscriber_count(connection)
    deadline: float = time.monotonic() + 5
    while subscriber_count and time.monotonic() < deadline:
        time.sleep(0.01)
        subscriber_count = lock._get_subscriber_count(connection)
    assert subscriber_count == 0

    # With threads available again the same instance acquires normally.
    lock.acquire()
    lock.release()


def test_redis_release_skips_unsubscribe_for_closed_pubsub(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Release must not reconnect purely to send a pointless UNSUBSCRIBE.

    The worker thread closes the pubsub when it stops, so after a normal
    hold the subscription is already gone. Sending UNSUBSCRIBE anyway
    would check a fresh connection out of the pool and reconnect, which
    is what used to blow up during interpreter shutdown.
    """
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
    )
    lock.acquire()
    pubsub: client.PubSub | None = lock.pubsub
    assert pubsub is not None
    commands: list[str] = []
    original_execute: typing.Callable[..., typing.Any] = pubsub.execute_command

    def recording_execute(*args: typing.Any) -> typing.Any:
        commands.append(str(args[0]))
        return original_execute(*args)

    monkeypatch.setattr(pubsub, 'execute_command', recording_execute)

    lock.release()

    assert lock.pubsub is None
    assert lock.thread is None
    assert pubsub.connection is None
    assert 'UNSUBSCRIBE' not in commands


def test_redis_del_suppresses_teardown_errors() -> None:
    """``__del__`` is best effort and must stay quiet.

    A broken connection during garbage collection must not surface as an
    interpreter-level "Exception ignored in" message.
    """
    events: list[str] = []
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))
    lock.pubsub = typing.cast(
        'client.PubSub | None',
        _TeardownPubSub(
            events,
            connection=object(),
            unsubscribe_error=_TeardownError('unsubscribe failed'),
        ),
    )

    lock.__del__()

    assert events == ['unsubscribe', 'close']
    assert lock.pubsub is None


class _BoomTeardownPubSub(_BoomPubSub):
    """Pubsub whose ``subscribe`` and rollback ``unsubscribe`` both raise."""

    def __init__(self) -> None:
        self.connection: object | None = object()

    def unsubscribe(self, *channels: str) -> None:
        raise _TeardownError('unsubscribe failed')


def test_redis_rollback_failure_keeps_original_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rollback that fails too is logged, the original error propagates.

    If the rollback error replaced the original one the caller would see
    the release failure instead of what actually broke the acquire.
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
    monkeypatch.setattr(
        lock, '_get_pubsub', lambda conn: _BoomTeardownPubSub()
    )

    with (
        caplog.at_level(logging.WARNING, logger='portalocker.redis'),
        pytest.raises(_SubscribeError),
    ):
        lock.acquire()

    assert lock.pubsub is None
    assert lock.thread is None
    assert any('roll back' in record.message for record in caplog.records)


def test_redis_release_clears_broken_created_connection(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broken self-created connection is cleared, first error wins.

    Both the pubsub teardown and the connection close fail here. The
    unsubscribe error propagates, the close error is logged, and the
    connection is cleared so a later acquire builds a fresh one.
    """
    events: list[str] = []
    unsubscribe_error: _TeardownError = _TeardownError('unsubscribe failed')
    close_error: _TeardownError = _TeardownError('close failed')
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))
    assert lock.close_connection is True
    connection: fakeredis.FakeStrictRedis = fakeredis.FakeStrictRedis(
        server=fakeredis.FakeServer(),
        decode_responses=True,
    )

    def broken_close() -> None:
        raise close_error

    monkeypatch.setattr(connection, 'close', broken_close)
    lock.connection = typing.cast('client.Redis | None', connection)
    lock.pubsub = typing.cast(
        'client.PubSub | None',
        _TeardownPubSub(
            events,
            connection=object(),
            unsubscribe_error=unsubscribe_error,
        ),
    )

    exc_info: pytest.ExceptionInfo[_TeardownError]
    with (
        caplog.at_level(logging.WARNING, logger='portalocker.redis'),
        pytest.raises(_TeardownError) as exc_info,
    ):
        lock.release()

    assert exc_info.value is unsubscribe_error
    assert events == ['unsubscribe', 'close']
    assert lock.connection is None
    assert lock.pubsub is None
    assert any(
        'Suppressed secondary' in record.message for record in caplog.records
    )


def test_redis_release_raises_connection_close_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing connection close propagates but still clears the state."""
    close_error: _TeardownError = _TeardownError('close failed')
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))
    assert lock.close_connection is True
    connection: fakeredis.FakeStrictRedis = fakeredis.FakeStrictRedis(
        server=fakeredis.FakeServer(),
        decode_responses=True,
    )

    def broken_close() -> None:
        raise close_error

    monkeypatch.setattr(connection, 'close', broken_close)
    lock.connection = typing.cast('client.Redis | None', connection)

    exc_info: pytest.ExceptionInfo[_TeardownError]
    with pytest.raises(_TeardownError) as exc_info:
        lock.release()

    assert exc_info.value is close_error
    assert lock.connection is None
