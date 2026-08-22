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
import signal
import threading
import time
import typing
import warnings

import fakeredis
import pytest
from redis import client, exceptions
from redis.connection import AbstractConnection

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
    # One count per probe round: the electing attempt sees the reader,
    # the promoting attempt is alone, and the confirm probe recounts
    # and settles on that same single subscriber.
    subscriber_counts: list[int] = [2, 1, 1]
    start_calls: list[client.Redis] = []
    sentinel_pubsub: client.PubSub = typing.cast(
        'client.PubSub',
        object(),
    )

    def start_subscription(connection_: client.Redis) -> None:
        start_calls.append(connection_)
        lock.pubsub = sentinel_pubsub
        lock.thread = _alive_worker_thread()

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
    lock.thread = None
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


class _AliveWorkerThread:
    """Stand-in worker thread that reports itself alive.

    Tests that stub ``_start_subscription`` must uphold its invariant
    that a live ``pubsub`` comes with a live worker thread, or the
    ``_confirm_held`` handshake correctly refuses the acquisition and
    ``_waiting_attempt_failed`` abandons the stubbed subscription.
    """

    ident: int | None = 1

    def is_alive(self) -> bool:
        return True

    def stop(self) -> None:
        pass

    def join(self) -> None:
        pass


def _alive_worker_thread() -> redis.PubSubWorkerThread:
    return typing.cast('redis.PubSubWorkerThread', _AliveWorkerThread())


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
        # The confirm probe after the promotion sees only this lock's
        # own freshly exclusive record.
        [
            redis.RedisLockHolder(
                holder_id='writer',
                mode=redis.RedisLockMode.EXCLUSIVE,
                elected=True,
            ),
        ],
    ]

    def start_subscription(connection_: client.Redis) -> None:
        lock.pubsub = _idle_pubsub()
        lock.thread = _alive_worker_thread()

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
    lock.thread = None
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
        lock.thread = _alive_worker_thread()

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


def _probe_reply(
    holder_id: str,
    mode: str = 'shared',
) -> dict[str, typing.Any]:
    """Build one wire-shaped probe reply message for a scripted pubsub."""
    return {
        'type': 'message',
        'data': json.dumps(
            {
                'holder_id': holder_id,
                'mode': mode,
                'protocol': redis.REDIS_LOCK_PROTOCOL_VERSION,
                'elected': False,
            }
        ),
    }


class _ScriptedPubSub:
    """Pubsub stand-in that plays back a message script.

    Records the timeout of every ``get_message`` call so a test can
    assert exactly which polls ran: the interval-long first read, the
    buffer drains at timeout zero, and the short grace polls between
    them.
    """

    messages: list[dict[str, typing.Any] | None]
    timeouts: list[float]

    def __init__(
        self,
        messages: list[dict[str, typing.Any] | None],
    ) -> None:
        self.messages = list(messages)
        self.timeouts = []

    def get_message(
        self, timeout: float = 0.0
    ) -> dict[str, typing.Any] | None:
        self.timeouts.append(timeout)
        if self.messages:
            return self.messages.pop(0)
        return None


def test_redis_drain_probe_replies_grace_poll_collects_in_flight() -> None:
    """A reply milliseconds away is collected in the same drain pass.

    Regression test for the #145 amplifier: the follow-up reads used
    only ``timeout=0``, which almost always misses a reply that is
    still in flight, so nearly every multi-holder probe slept a full
    jittered drain interval (median 116ms with three holders). After a
    ``timeout=0`` miss the drain now polls once with a short real
    timeout before giving the pass up.
    """
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))
    pubsub: _ScriptedPubSub = _ScriptedPubSub(
        [
            _probe_reply('holder-a'),
            None,
            _probe_reply('holder-b'),
            None,
            _probe_reply('holder-c'),
        ]
    )
    holders: dict[str, redis.RedisLockHolder] = {}

    lock._drain_probe_replies(
        typing.cast('client.PubSub', pubsub),
        holders,
        expected_subscribers=3,
        check_interval=0.1,
    )

    assert sorted(holders) == ['holder-a', 'holder-b', 'holder-c']
    grace: float = redis._PROBE_REPLY_GRACE
    assert pubsub.timeouts == [0.1, 0, grace, 0, grace]


def test_redis_drain_probe_replies_grace_poll_miss_ends_the_pass() -> None:
    """A missed grace poll ends the pass instead of spinning."""
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))
    pubsub: _ScriptedPubSub = _ScriptedPubSub(
        [
            _probe_reply('holder-a'),
            None,
            None,
        ]
    )
    holders: dict[str, redis.RedisLockHolder] = {}

    lock._drain_probe_replies(
        typing.cast('client.PubSub', pubsub),
        holders,
        expected_subscribers=3,
        check_interval=0.1,
    )

    assert sorted(holders) == ['holder-a']
    assert pubsub.timeouts == [0.1, 0, redis._PROBE_REPLY_GRACE]


def test_redis_drain_probe_replies_zero_interval_skips_grace_poll() -> None:
    """A zero check interval keeps the drain strictly non-blocking.

    ``probe`` and ``acquire`` never pass zero, but the guard keeps the
    grace poll from turning into a busy loop if a caller ever does.
    """
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))
    pubsub: _ScriptedPubSub = _ScriptedPubSub(
        [
            _probe_reply('holder-a'),
            None,
        ]
    )
    holders: dict[str, redis.RedisLockHolder] = {}

    lock._drain_probe_replies(
        typing.cast('client.PubSub', pubsub),
        holders,
        expected_subscribers=3,
        check_interval=0,
    )

    assert sorted(holders) == ['holder-a']
    assert pubsub.timeouts == [0, 0]


def test_redis_probe_aborts_when_count_moves_mid_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An incomplete collection stops as soon as the count moves.

    A waiter that answers a ping and then backs off leaves the probe
    expecting a reply that will never come. Waiting out the whole
    reply timeout on it stalled the confirm probe for its entire
    budget (#145); the count is checked each polling interval instead,
    and a moved count ends the probe as inconclusive at once, without
    reaping anybody, since churn is not a crashed holder.
    """
    connection: fakeredis.FakeStrictRedis = fakeredis.FakeStrictRedis(
        server=fakeredis.FakeServer(),
        decode_responses=True,
    )
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=connection,
        unavailable_timeout=5,
        thread_sleep_time=0.01,
    )
    # Steady at first (the interval check passes once), then a leave.
    counts: list[int] = [2, 2, 1]
    monkeypatch.setattr(
        lock,
        '_get_subscriber_count',
        lambda connection_: counts.pop(0),
    )

    def unexpected_reap(*args: typing.Any, **kwargs: typing.Any) -> None:
        raise AssertionError('churn must not reap')

    monkeypatch.setattr(lock, '_kill_unavailable_locks', unexpected_reap)

    start: float = time.monotonic()
    holders: list[redis.RedisLockHolder] | None = lock._collect_lock_holders(
        connection,
        2,
        timeout=5,
    )
    elapsed: float = time.monotonic() - start

    assert holders is None
    assert not counts
    # Nowhere near the five second reply timeout the stall used to burn.
    assert elapsed < 2


@pytest.mark.timeout(180)
def test_redis_probe_collects_replies_within_one_interval() -> None:
    """Median conclusive-probe time with three holders stays low.

    Before the grace poll 57 of 60 such probes slept a full jittered
    drain interval (median 116ms); with it the replies still in flight
    are collected in the first pass. The bound is generous to stay
    flake proof on loaded runners: pre-fix medians sit well above it,
    post-fix medians well below.
    """
    _ensure_live_redis_available(_LIVE_REDIS)
    channel: str = str(random.random())
    readers: list[redis.RedisLock] = [
        redis.RedisLock(
            channel,
            connection=_live_redis_connection(),
            flags=portalocker.LockFlags.SHARED,
        )
        for _ in range(2)
    ]
    prober: redis.RedisLock = redis.RedisLock(
        channel,
        connection=_live_redis_connection(),
        thread_sleep_time=0.1,
        unavailable_timeout=1,
    )
    durations: list[float] = []
    try:
        for reader in readers:
            reader.acquire()
        prober._start_subscription(prober.get_connection())
        _wait_for_subscribers(prober, 3)
        time.sleep(0.2)
        for _ in range(15):
            start: float = time.perf_counter()
            holders: list[redis.RedisLockHolder] | None = (
                prober._collect_lock_holders(
                    prober.get_connection(),
                    3,
                    prober.unavailable_timeout,
                )
            )
            durations.append(time.perf_counter() - start)
            assert holders is not None
            assert len(holders) == 3
    finally:
        prober._unsubscribe()
        for reader in readers:
            reader.release()
        for lock in (*readers, prober):
            if lock.connection is not None:
                lock.connection.close()

    durations.sort()
    median: float = durations[len(durations) // 2]
    assert median < 0.06


@pytest.mark.parametrize(
    ('foreign_holders', 'expected'),
    [
        pytest.param([], 'confirmed', id='alone'),
        pytest.param(
            [
                redis.RedisLockHolder(
                    'reader',
                    redis.RedisLockMode.SHARED,
                ),
            ],
            'demote',
            id='shared-holder',
        ),
        pytest.param(
            [
                redis.RedisLockHolder(
                    'legacy-0',
                    redis.RedisLockMode.EXCLUSIVE,
                    legacy=True,
                ),
            ],
            'demote',
            id='legacy-exclusive',
        ),
        pytest.param(
            [
                redis.RedisLockHolder(
                    'z-old-rival',
                    redis.RedisLockMode.EXCLUSIVE,
                ),
            ],
            'demote',
            id='pre-42-exclusive-higher-id',
        ),
        pytest.param(
            [
                redis.RedisLockHolder(
                    'a-rival',
                    redis.RedisLockMode.EXCLUSIVE,
                    elected=True,
                ),
            ],
            'demote',
            id='exclusive-lower-id',
        ),
        pytest.param(
            [
                redis.RedisLockHolder(
                    'z-rival',
                    redis.RedisLockMode.EXCLUSIVE,
                    elected=False,
                ),
            ],
            'confirmed',
            id='exclusive-higher-id-42',
        ),
        pytest.param(
            [
                redis.RedisLockHolder(
                    'a-old-peer',
                    redis.RedisLockMode.PENDING,
                ),
            ],
            'demote',
            id='pre-42-pending-lower-id',
        ),
        pytest.param(
            [
                redis.RedisLockHolder(
                    'a-peer',
                    redis.RedisLockMode.PENDING,
                    elected=False,
                ),
            ],
            'retry',
            id='undecided-pending-lower-id',
        ),
        pytest.param(
            [
                redis.RedisLockHolder(
                    'a-peer',
                    redis.RedisLockMode.PENDING,
                    elected=True,
                ),
            ],
            'retry',
            id='elected-pending-lower-id',
        ),
        pytest.param(
            [
                redis.RedisLockHolder(
                    'z-peer',
                    redis.RedisLockMode.PENDING,
                    elected=False,
                ),
            ],
            'confirmed',
            id='pending-higher-id',
        ),
        pytest.param(
            [
                redis.RedisLockHolder(
                    'a-peer',
                    redis.RedisLockMode.PENDING,
                    elected=False,
                ),
                redis.RedisLockHolder(
                    'a-rival',
                    redis.RedisLockMode.EXCLUSIVE,
                    elected=True,
                ),
            ],
            'demote',
            id='demote-outranks-retry',
        ),
    ],
)
def test_redis_confirm_probe_verdicts(
    foreign_holders: list[redis.RedisLockHolder],
    expected: str,
) -> None:
    """The confirm probe mirrors the deterministic forfeit rules.

    A lower-id exclusive rival, any pre-4.2 exclusive rival and any
    shared holder outrank a fresh promotion (demote). A lower-id
    pending peer that speaks 4.2 is undecided, so the round is
    inconclusive (retry). A higher-id 4.2 exclusive rival demotes
    itself, and higher-id pending peers lose the sort and defer, so
    neither blocks the confirmation.
    """
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))
    lock.holder_id = 'm-just-promoted'
    holders: list[redis.RedisLockHolder] = [
        redis.RedisLockHolder(
            lock.holder_id,
            redis.RedisLockMode.EXCLUSIVE,
            elected=False,
        ),
        *foreign_holders,
    ]

    verdict: redis._ConfirmVerdict = lock._confirm_probe_verdict(holders)

    assert verdict is redis._ConfirmVerdict(expected)


def _promoted_writer(
    connection: client.Redis,
    **kwargs: typing.Any,
) -> redis.RedisLock:
    """Build a writer frozen at the instant right after its promotion."""
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=connection,
        **kwargs,
    )
    lock.holder_id = 'm-just-promoted'
    lock.mode = redis.RedisLockMode.EXCLUSIVE
    lock.pubsub = _idle_pubsub()
    return lock


def test_redis_confirm_probe_count_one_confirms_without_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alone on the channel means confirmed, at one round trip.

    Ownership is the subscription, so a rival that promoted is still
    subscribed and shows up in the count. A count of one is therefore
    conclusive on its own and the full probe is skipped, which is what
    keeps the uncontended fast path cheap.
    """
    connection: fakeredis.FakeStrictRedis = fakeredis.FakeStrictRedis(
        server=fakeredis.FakeServer(),
        decode_responses=True,
    )
    lock: redis.RedisLock = _promoted_writer(connection)
    monkeypatch.setattr(lock, '_get_subscriber_count', lambda connection_: 1)

    def unexpected_probe(*args: typing.Any, **kwargs: typing.Any) -> None:
        raise AssertionError('a count of one must not probe')

    monkeypatch.setattr(lock, '_collect_lock_holders', unexpected_probe)

    assert lock._confirm_exclusive_promotion(connection, False)
    assert lock.mode is redis.RedisLockMode.EXCLUSIVE
    assert lock.pubsub is not None


def test_redis_confirm_probe_retries_inconclusive_then_confirms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inconclusive round is noise: the confirm asks again."""
    connection: fakeredis.FakeStrictRedis = fakeredis.FakeStrictRedis(
        server=fakeredis.FakeServer(),
        decode_responses=True,
    )
    lock: redis.RedisLock = _promoted_writer(
        connection,
        unavailable_timeout=5,
        thread_sleep_time=0.001,
    )
    monkeypatch.setattr(lock, '_get_subscriber_count', lambda connection_: 2)
    probes: list[list[redis.RedisLockHolder] | None] = [
        None,
        [
            redis.RedisLockHolder(
                lock.holder_id,
                redis.RedisLockMode.EXCLUSIVE,
                elected=False,
            ),
            redis.RedisLockHolder(
                'z-deferring-peer',
                redis.RedisLockMode.PENDING,
                elected=False,
            ),
        ],
    ]
    probe_calls: list[int] = []

    def scripted_probe(
        connection_: client.Redis,
        expected_subscribers: int,
        timeout: float,
    ) -> list[redis.RedisLockHolder] | None:
        probe_calls.append(expected_subscribers)
        return probes.pop(0)

    monkeypatch.setattr(lock, '_collect_lock_holders', scripted_probe)

    assert lock._confirm_exclusive_promotion(connection, False)
    assert probe_calls == [2, 2]
    assert lock.mode is redis.RedisLockMode.EXCLUSIVE


def test_redis_confirm_probe_undecided_peer_then_rival_demotes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lower-id undecided peer holds the confirm, its promotion ends it.

    The peer's ``elected: false`` reply cannot show whether its own
    probe predates this promotion, so the confirm waits it out. One
    round later the peer is visible as exclusive, the two-incumbents
    rule applies, and the higher id demotes back to a fresh contender.
    """
    connection: fakeredis.FakeStrictRedis = fakeredis.FakeStrictRedis(
        server=fakeredis.FakeServer(),
        decode_responses=True,
    )
    lock: redis.RedisLock = _promoted_writer(
        connection,
        unavailable_timeout=5,
        thread_sleep_time=0.001,
    )
    monkeypatch.setattr(lock, '_get_subscriber_count', lambda connection_: 2)
    own_record: redis.RedisLockHolder = redis.RedisLockHolder(
        lock.holder_id,
        redis.RedisLockMode.EXCLUSIVE,
        elected=False,
    )
    probes: list[list[redis.RedisLockHolder] | None] = [
        [
            own_record,
            redis.RedisLockHolder(
                'a-rival',
                redis.RedisLockMode.PENDING,
                elected=False,
            ),
        ],
        [
            own_record,
            redis.RedisLockHolder(
                'a-rival',
                redis.RedisLockMode.EXCLUSIVE,
                elected=True,
            ),
        ],
    ]

    def scripted_probe(
        connection_: client.Redis,
        expected_subscribers: int,
        timeout: float,
    ) -> list[redis.RedisLockHolder] | None:
        return probes.pop(0)

    monkeypatch.setattr(lock, '_collect_lock_holders', scripted_probe)

    assert not lock._confirm_exclusive_promotion(connection, False)
    assert not probes
    assert lock.mode is redis.RedisLockMode.PENDING
    assert not lock.writer_elected
    assert lock.pubsub is None


def test_redis_confirm_probe_budget_exhaustion_demotes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confirm that stays inconclusive is never concluded from noise.

    When the budget runs out without one clean round the promotion is
    given up: demoting is the safe direction, and the acquire retry
    loop simply runs another full attempt against whatever the channel
    turns out to hold.
    """
    connection: fakeredis.FakeStrictRedis = fakeredis.FakeStrictRedis(
        server=fakeredis.FakeServer(),
        decode_responses=True,
    )
    lock: redis.RedisLock = _promoted_writer(
        connection,
        unavailable_timeout=0.05,
        thread_sleep_time=0.001,
    )
    monkeypatch.setattr(lock, '_get_subscriber_count', lambda connection_: 2)
    probe_calls: list[int] = []

    def inconclusive_probe(
        connection_: client.Redis,
        expected_subscribers: int,
        timeout: float,
    ) -> list[redis.RedisLockHolder] | None:
        probe_calls.append(expected_subscribers)
        return None

    monkeypatch.setattr(lock, '_collect_lock_holders', inconclusive_probe)

    assert not lock._confirm_exclusive_promotion(connection, False)
    assert probe_calls
    assert lock.mode is redis.RedisLockMode.PENDING
    assert not lock.writer_elected
    assert lock.pubsub is None


def test_redis_confirm_probe_fail_when_locked_demotion_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-blocking writer demoted by its confirm raises after release.

    ``fail_when_locked`` means not waiting for the rival to leave, so
    the demotion becomes ``AlreadyLocked``, and only after the full
    release: the instance must leave the channel and stay reusable.
    """
    connection: fakeredis.FakeStrictRedis = fakeredis.FakeStrictRedis(
        server=fakeredis.FakeServer(),
        decode_responses=True,
    )
    lock: redis.RedisLock = _promoted_writer(
        connection,
        unavailable_timeout=5,
        thread_sleep_time=0.001,
    )
    monkeypatch.setattr(lock, '_get_subscriber_count', lambda connection_: 2)

    def rival_probe(
        connection_: client.Redis,
        expected_subscribers: int,
        timeout: float,
    ) -> list[redis.RedisLockHolder] | None:
        return [
            redis.RedisLockHolder(
                lock.holder_id,
                redis.RedisLockMode.EXCLUSIVE,
                elected=False,
            ),
            redis.RedisLockHolder(
                'a-rival',
                redis.RedisLockMode.EXCLUSIVE,
                elected=True,
            ),
        ]

    monkeypatch.setattr(lock, '_collect_lock_holders', rival_probe)

    with pytest.raises(portalocker.AlreadyLocked):
        lock._confirm_exclusive_promotion(connection, True)

    assert lock.mode is redis.RedisLockMode.PENDING
    assert not lock.writer_elected
    assert lock.pubsub is None
    assert lock.thread is None


@pytest.mark.timeout(180)
def test_redis_fast_path_confirm_demotes_exactly_one_writer(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The #145 window-A schedule ends with exactly one holder.

    Staged interleaving from the issue: a writer counts a single
    subscriber and stalls between that count and its fast-path
    promotion. A lower-id rival subscribes and probes inside the
    stall, so the stalled writer's reply is snapshotted while it is
    still pending, and the rival legitimately elects itself and
    promotes. Without the confirm probe both acquires return; with it
    the stalled writer's confirm sees the rival's exclusive record,
    demotes, and acquires only after the rival releases.
    """
    channel: str = str(random.random())
    kwargs: dict[str, typing.Any] = dict(
        timeout=30,
        check_interval=0.02,
        unavailable_timeout=2,
        thread_sleep_time=0.01,
    )
    stalled: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        **kwargs,
    )
    rival: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        **kwargs,
    )
    stalled.holder_id = 'm-stalled-writer'
    rival.holder_id = 'a-rival-writer'
    if isinstance(stalled.connection, fakeredis.FakeStrictRedis):
        # fakeredis does not implement CLIENT KILL. Stale-holder cleanup
        # is covered independently; this test isolates the confirm.
        monkeypatch.setattr(
            redis.RedisLock,
            '_kill_unavailable_locks',
            _ignore_stale_cleanup,
        )
    counted_alone: threading.Event = threading.Event()
    original_count: typing.Callable[[client.Redis], int] = (
        stalled._get_subscriber_count
    )

    def stalling_count(connection_: client.Redis) -> int:
        count: int = original_count(connection_)
        if count == 1 and not counted_alone.is_set():
            counted_alone.set()
            time.sleep(0.6)
        return count

    monkeypatch.setattr(stalled, '_get_subscriber_count', stalling_count)
    acquired: list[str] = []
    errors: list[BaseException] = []

    def attempt(lock: redis.RedisLock, name: str) -> None:
        try:
            lock.acquire()
        except BaseException as exception:  # pragma: no cover
            errors.append(exception)
        else:
            acquired.append(name)

    stalled_thread: threading.Thread = threading.Thread(
        target=attempt,
        args=(stalled, 'stalled'),
        daemon=True,
    )
    stalled_thread.start()
    assert counted_alone.wait(timeout=30)
    rival_thread: threading.Thread = threading.Thread(
        target=attempt,
        args=(rival, 'rival'),
        daemon=True,
    )
    rival_thread.start()

    rival_deadline: float = time.monotonic() + 30
    while (
        'rival' not in acquired
        and not errors
        and time.monotonic() < rival_deadline
    ):
        time.sleep(0.001)
    assert not errors
    assert acquired == ['rival']

    # The stalled writer wakes at +0.6s, promotes on its stale count,
    # and must demote when its confirm probe shows the rival: while the
    # rival holds, the stalled acquire may not return.
    time.sleep(1.5)
    assert acquired == ['rival']

    rival.release()
    stalled_thread.join(timeout=60)
    assert not stalled_thread.is_alive()
    assert acquired == ['rival', 'stalled']
    stalled.release()
    rival_thread.join(timeout=10)
    assert not rival_thread.is_alive()
    assert not errors
    for lock in (stalled, rival):
        if lock.connection is not None:
            lock.connection.close()


@pytest.mark.timeout(180)
def test_redis_reader_mixed_soak_single_writer_at_a_time() -> None:
    """Live random-contention soak: writers never overlap anybody.

    The reader-mixed shape from the #145 report, where the fast-path
    staleness window fired about once per 60 to 200 acquisitions on
    the unfixed code: three writers and two readers churn one channel
    at a short check interval, and every acquisition return is checked
    against user-level accounting. The confirm probe must keep the
    violation count at zero.
    """
    _ensure_live_redis_available(_LIVE_REDIS)
    channel: str = str(random.random())
    guard: threading.Lock = threading.Lock()
    state: dict[str, int] = {'writers': 0, 'readers': 0}
    violations: list[str] = []
    errors: list[BaseException] = []
    deadline: float = time.monotonic() + 12
    kwargs: dict[str, typing.Any] = dict(
        timeout=30,
        check_interval=0.02,
        unavailable_timeout=2,
        thread_sleep_time=0.01,
    )

    def contend(flags: portalocker.LockFlags, kind: str) -> None:
        while time.monotonic() < deadline:
            lock: redis.RedisLock = redis.RedisLock(
                channel,
                connection=_live_redis_connection(),
                flags=flags,
                **kwargs,
            )
            try:
                lock.acquire()
            except portalocker.AlreadyLocked:
                continue
            except BaseException as exception:  # pragma: no cover
                errors.append(exception)
                return
            finally:
                if lock.pubsub is None and lock.connection is not None:
                    lock.connection.close()
            with guard:
                state[kind] += 1
                overlap: bool = state['writers'] > 1 or (
                    state['writers'] >= 1 and state['readers'] >= 1
                )
                if overlap:
                    violations.append(f'{kind}: {state!r}')
            time.sleep(0.01 * (0.5 + random.random()))
            with guard:
                state[kind] -= 1
            lock.release()
            if lock.connection is not None:
                lock.connection.close()
            time.sleep(random.random() * 0.01)

    threads: list[threading.Thread] = [
        threading.Thread(
            target=contend,
            args=(portalocker.LockFlags.EXCLUSIVE, 'writers'),
            daemon=True,
        )
        for _ in range(3)
    ] + [
        threading.Thread(
            target=contend,
            args=(portalocker.LockFlags.SHARED, 'readers'),
            daemon=True,
        )
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
        assert not thread.is_alive()
    assert not errors
    assert not violations


def test_redis_uncontended_acquire_stays_fast() -> None:
    """The confirm probe must not price the uncontended fast path.

    A count of one settles the confirm in a single extra round trip,
    so an uncontended acquire stays well under the latency guard. The
    bound is generous for loaded CI runners; the local median is a few
    milliseconds.
    """
    server: fakeredis.FakeServer = fakeredis.FakeServer()
    durations: list[float] = []
    for _ in range(10):
        connection: fakeredis.FakeStrictRedis = fakeredis.FakeStrictRedis(
            server=server,
            decode_responses=True,
        )
        lock: redis.RedisLock = redis.RedisLock(
            'uncontended-bench',
            connection=connection,
            check_interval=0.5,
        )
        start: float = time.perf_counter()
        lock.acquire()
        durations.append(time.perf_counter() - start)
        lock.release()
    durations.sort()
    median: float = durations[len(durations) // 2]
    assert median < 0.1


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
            exception_handler: typing.Any = None,
            tick: typing.Any = None,
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
        lock.thread = _alive_worker_thread()

    monkeypatch.setattr(lock, '_start_subscription', start_subscription)
    monkeypatch.setattr(lock, '_get_subscriber_count', lambda connection: 1)

    assert lock.acquire() is lock

    assert lock.mode is redis.RedisLockMode.EXCLUSIVE
    assert recording.entries == 2
    lock.pubsub = None
    lock.thread = None
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


def test_pubsub_worker_run_routes_escaped_error_to_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An error escaping the read loop still reaches the handler.

    The loop itself hands read errors to the exception handler, but an
    error inside the handler, or inside the ``pubsub.close()`` after
    the loop, escapes it. ``run`` routes those into the same handler as
    a last-ditch layer, ``BaseException`` included, so a worker death
    can never bypass the loss classifier (#141). The thread body runs
    directly (no thread is started) with the loop patched to raise.
    """
    failure: SystemExit = SystemExit('worker killed')

    def broken_reader(
        self: redis.PubSubWorkerThread,
        pubsub: client.PubSub,
        handler: typing.Any,
    ) -> None:
        raise failure

    monkeypatch.setattr(
        redis.PubSubWorkerThread,
        '_read_until_stopped',
        broken_reader,
    )

    handled: list[BaseException] = []
    pubsub: client.PubSub = fakeredis.FakeStrictRedis(
        decode_responses=True
    ).pubsub()  # type: ignore[no-untyped-call]
    worker: redis.PubSubWorkerThread = redis.PubSubWorkerThread(
        pubsub,
        sleep_time=0.01,
        daemon=True,
        exception_handler=lambda error, pubsub_, thread_: handled.append(
            error
        ),
    )

    worker.run()

    assert handled == [failure]


def test_pubsub_worker_read_loop_reraises_without_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read failure with no registered handler escapes the loop itself.

    The companion to the ``run``-level test below: the failure here is
    raised by ``get_message`` inside the read loop, so the loop's own
    no-handler arm re-raises it, and ``run``'s last-ditch layer then
    re-raises it again. ``RedisLock`` always registers a handler; this
    covers direct construction without one.
    """
    failure: RuntimeError = RuntimeError('connection dropped')
    pubsub: client.PubSub = fakeredis.FakeStrictRedis(
        decode_responses=True
    ).pubsub()  # type: ignore[no-untyped-call]

    def broken_get_message(
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> None:
        raise failure

    monkeypatch.setattr(pubsub, 'get_message', broken_get_message)
    worker: redis.PubSubWorkerThread = redis.PubSubWorkerThread(
        pubsub,
        sleep_time=0.01,
        daemon=True,
    )

    with pytest.raises(RuntimeError) as exc_info:
        worker.run()

    assert exc_info.value is failure


def test_pubsub_worker_run_reraises_without_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a registered handler the escaped error propagates."""
    failure: RuntimeError = RuntimeError('connection dropped')

    def broken_reader(
        self: redis.PubSubWorkerThread,
        pubsub: client.PubSub,
        handler: typing.Any,
    ) -> None:
        raise failure

    monkeypatch.setattr(
        redis.PubSubWorkerThread,
        '_read_until_stopped',
        broken_reader,
    )

    pubsub: client.PubSub = fakeredis.FakeStrictRedis(
        decode_responses=True
    ).pubsub()  # type: ignore[no-untyped-call]
    worker: redis.PubSubWorkerThread = redis.PubSubWorkerThread(
        pubsub,
        sleep_time=0.01,
        daemon=True,
    )

    with pytest.raises(RuntimeError) as exc_info:
        worker.run()

    assert exc_info.value is failure


def test_channel_handler_ignores_control_frames(
    redis_connection: ConnectionFactory,
) -> None:
    """Subscribe confirmations and other control frames are dropped.

    ``channel_handler`` only answers frames of type ``message``. The
    subscribe/unsubscribe confirmations redis-py can hand a channel
    callback must return without touching the connection at all. The
    handler is called directly with a control frame, exactly as the
    pubsub dispatch would.
    """
    lock_obj: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
    )
    # Publishes nothing and raises nothing despite the frame carrying
    # no usable payload.
    lock_obj.channel_handler({'type': 'subscribe', 'data': '1'})


# --------------------------------------------------------------------- #
#  Revocation safety and failure escalation (#137, #141)
# --------------------------------------------------------------------- #


def _wait_for(
    predicate: typing.Callable[[], bool],
    timeout: float = 5.0,
) -> bool:
    """Poll ``predicate`` until it holds or ``timeout`` passes."""
    deadline: float = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _lost(lock: redis.RedisLock) -> bool:
    """Read ``lock.lost`` opaquely.

    mypy narrows a property member expression like ``lock.lost`` on
    ``assert`` and does not invalidate the narrowing across method
    calls, so a test asserting both directions on one instance would be
    flagged as unreachable. Reading through a function keeps the
    narrowing out of the caller's scope.
    """
    return lock.lost


def _break_subscription_read(
    monkeypatch: pytest.MonkeyPatch,
    lock: redis.RedisLock,
    error: BaseException,
) -> None:
    """Make the next keep-alive read of ``lock`` raise ``error``.

    The worker thread calls ``pubsub.get_message`` once per sleep
    interval, so patching the held pubsub's read is the deterministic
    stand-in for a connection dying under the subscription.
    """
    assert lock.pubsub is not None

    def broken_get_message(
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> None:
        raise error

    monkeypatch.setattr(lock.pubsub, 'get_message', broken_get_message)


def test_redis_subscription_retry_policy(
    redis_connection: ConnectionFactory,
) -> None:
    """The subscription connection retries nothing and reconnects never.

    Structural pin for #137: redis-py's pubsub wraps every read in the
    connection's retry policy, whose failure callback reconnects even
    with a zero retry budget, and a reconnected subscription silently
    re-acquires the lock. Only ``supported_errors=()`` prevents the
    reconnect entirely, so this asserts the exact configuration rather
    than behaviour, catching any redis-py default change early. The
    command connection keeps its own (retrying) policy.
    """
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
    )

    lock.acquire()
    try:
        assert lock.pubsub is not None
        connection: typing.Any = lock.pubsub.connection
        assert connection.retry.get_retries() == 0
        assert connection.retry._supported_errors == ()
        assert connection.protocol == 2
        assert lock._subscription_client is not None
        assert lock._subscription_client is not lock.connection
    finally:
        lock.release()
    assert lock._subscription_client is None


def test_redis_subscription_inherits_health_check_interval(
    redis_connection: ConnectionFactory,
) -> None:
    """The subscription keeps the command connection's health check.

    Structural pin: the derived client must not force the lock's
    default ``health_check_interval`` onto a connection whose owner
    chose another value. The module docs tell callers to set the
    interval on their connection, and the clone is what carries that
    choice over to the subscription.
    """
    connection: client.Redis = redis_connection()
    expected: int = connection.get_connection_kwargs()['health_check_interval']
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=connection,
    )

    lock.acquire()
    try:
        assert lock.pubsub is not None
        subscription: typing.Any = lock.pubsub.connection
        assert subscription.health_check_interval == expected
    finally:
        lock.release()


def test_redis_subscription_worker_does_not_spin_on_fakeredis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The keep-alive worker idles between polls instead of pinging.

    Regression pin for the CI failure after #137: fakeredis never
    advances redis-py's ``next_health_check`` clock, so a non-zero
    ``health_check_interval`` forced onto the subscription made the
    worker send a health-check ``PING`` on every poll, and the spinning
    thread starved ``PUBSUB NUMSUB`` on the command connection. The
    caller's connection here has the fakeredis default of no health
    check, so a single ``PING`` from the worker is a regression.
    """
    pings: list[str] = []
    original_send: typing.Callable[..., None] = AbstractConnection.send_command

    def counting_send(
        connection: typing.Any,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> None:
        if args and args[0] == 'PING':
            pings.append(connection.client_name)
        original_send(connection, *args, **kwargs)

    monkeypatch.setattr(
        AbstractConnection,
        'send_command',
        counting_send,
    )
    connection: fakeredis.FakeStrictRedis = fakeredis.FakeStrictRedis(
        server=fakeredis.FakeServer(),
        decode_responses=True,
    )
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=connection,
        thread_sleep_time=0.01,
    )

    lock.acquire()
    try:
        time.sleep(0.1)
    finally:
        lock.release()
    assert lock.client_name not in pings


def test_redis_subscription_client_name_is_connection_level(
    redis_connection: ConnectionFactory,
) -> None:
    """The holder name is part of the subscription connection itself.

    Regression pin for #137: the name used to be sent as a one-off
    ``CLIENT SETNAME`` and died with the first reconnect, leaving a
    resubscribed holder permanently unreapable. At the connection level
    it is re-sent on every handshake instead.
    """
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
    )

    lock.acquire()
    try:
        assert lock.pubsub is not None
        connection: typing.Any = lock.pubsub.connection
        assert connection.client_name == lock.client_name
    finally:
        lock.release()


def test_redis_subscription_connection_factory_is_used(
    redis_connection: ConnectionFactory,
) -> None:
    """A caller-supplied factory replaces the derived subscription client."""
    factory_clients: list[client.Redis] = []
    command_connection: client.Redis = redis_connection()

    def factory() -> client.Redis:
        factory_clients.append(redis_connection())
        return factory_clients[-1]

    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=command_connection,
        subscription_connection_factory=factory,
    )

    lock.acquire()
    try:
        assert len(factory_clients) == 1
        assert lock._subscription_client is factory_clients[0]
    finally:
        lock.release()
    assert lock._subscription_client is None


class _ExoticPool:
    """Connection-pool stand-in whose clone attempt fails.

    Mimics a Sentinel-style pool whose constructor does not accept the
    ``(connection_class, **connection_kwargs)`` shape the derivation
    uses.
    """

    connection_class: type = object

    def __init__(self, **kwargs: typing.Any) -> None:
        self.connection_kwargs: dict[str, typing.Any] = {}
        if kwargs:
            raise TypeError('exotic pools take no keyword arguments')


def test_redis_subscription_derivation_failure_names_escape_hatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An uncloneable pool fails with a pointer at the factory parameter."""
    connection: fakeredis.FakeStrictRedis = fakeredis.FakeStrictRedis(
        server=fakeredis.FakeServer(),
        decode_responses=True,
    )
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=connection,
    )
    monkeypatch.setattr(connection, 'connection_pool', _ExoticPool())

    with pytest.raises(
        portalocker.LockException,
        match='subscription_connection_factory',
    ):
        lock.acquire()

    assert lock.pubsub is None


@pytest.mark.parametrize('interrupt_on_lost', [True, False])
def test_redis_held_worker_failure_marks_lost(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_on_lost: bool,
) -> None:
    """Losing the subscription while held surfaces on every channel.

    The injected read error stands in for a killed connection: the lock
    must flip to lost, fire ``on_lost`` exactly once, raise
    ``LockLostError`` from ``ensure_held``, and interrupt the main
    thread only when asked to (#137, #141).
    """
    interrupts: list[bool] = []
    monkeypatch.setattr(
        _thread, 'interrupt_main', lambda: interrupts.append(True)
    )
    lost_calls: list[redis.RedisLock] = []
    channel: str = str(random.random())
    lock: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        on_lost=lost_calls.append,
        interrupt_on_lost=interrupt_on_lost,
    )
    failure: exceptions.ConnectionError = exceptions.ConnectionError(
        'connection killed'
    )

    lock.acquire()
    assert not _lost(lock)
    lock.ensure_held()  # Held and healthy: returns quietly.
    worker: redis.PubSubWorkerThread | None = lock.thread
    assert worker is not None
    _break_subscription_read(monkeypatch, lock, failure)

    # The worker thread runs the whole escalation before it ends, so
    # its death means every loss side effect has landed.
    assert _wait_for(lambda: not worker.is_alive())
    assert _lost(lock)
    assert lost_calls == [lock]
    assert interrupts == ([True] if interrupt_on_lost else [])
    error: pytest.ExceptionInfo[portalocker.LockLostError]
    with pytest.raises(portalocker.LockLostError) as error:
        lock.ensure_held()
    assert error.value.channel == channel
    assert error.value.holder_id == lock.holder_id
    assert error.value.__cause__ is failure

    # release() never raises on account of the loss, and the loss stays
    # observable afterwards for bare acquire()/release() callers.
    lock.release()
    assert _lost(lock)

    # The instance stays reusable: the next acquire consumes the loss.
    lock.acquire()
    try:
        assert not _lost(lock)
        assert lost_calls == [lock]
    finally:
        lock.release()


def test_redis_base_exception_reaches_classifier(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A BaseException killing the reader is a loss, not a silent death.

    Pin for the #141 gap: the old escalation caught ``Exception`` only,
    so a ``SystemExit`` (or ``KeyboardInterrupt``) landing on the worker
    thread ended it without a trace while the process kept believing it
    held the lock.
    """
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
        interrupt_on_lost=False,
    )
    failure: SystemExit = SystemExit('worker killed')

    lock.acquire()
    _break_subscription_read(monkeypatch, lock, failure)

    assert _wait_for(lambda: lock.lost)
    error: pytest.ExceptionInfo[portalocker.LockLostError]
    with pytest.raises(portalocker.LockLostError) as error:
        lock.ensure_held()
    assert error.value.__cause__ is failure
    lock.release()


def test_redis_lost_lock_is_reusable_without_release(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """acquire() on a lost instance resets it without an explicit release."""
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
        interrupt_on_lost=False,
    )

    lock.acquire()
    _break_subscription_read(
        monkeypatch,
        lock,
        exceptions.ConnectionError('connection killed'),
    )
    assert _wait_for(lambda: lock.lost)

    lock.acquire()
    try:
        assert not lock.lost
        assert lock.thread is not None
        assert lock.thread.is_alive()
    finally:
        lock.release()


def test_redis_on_lost_exception_is_contained(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A raising on_lost callback is logged, the transition still lands."""

    def broken_callback(lock_: redis.RedisLock) -> None:
        raise RuntimeError('callback failed')

    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
        on_lost=broken_callback,
        interrupt_on_lost=False,
    )

    lock.acquire()
    with caplog.at_level(logging.ERROR, logger='portalocker.redis'):
        _break_subscription_read(
            monkeypatch,
            lock,
            exceptions.ConnectionError('connection killed'),
        )
        assert _wait_for(lambda: lock.lost)
        worker: redis.PubSubWorkerThread | None = lock.thread
        assert worker is not None
        assert _wait_for(lambda: not worker.is_alive())

    assert any(
        'on_lost callback failed' in record.message
        for record in caplog.records
    )
    lock.release()


def test_redis_del_after_loss_is_quiet(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garbage collecting a lost lock raises nothing."""
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
        interrupt_on_lost=False,
    )

    lock.acquire()
    _break_subscription_read(
        monkeypatch,
        lock,
        exceptions.ConnectionError('connection killed'),
    )
    assert _wait_for(lambda: lock.lost)

    lock.__del__()

    assert lock.pubsub is None


def test_redis_exit_raises_lock_lost_after_clean_body(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silent loss surfaces when the with block ends cleanly."""
    channel: str = str(random.random())
    lock: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        interrupt_on_lost=False,
    )

    error: pytest.ExceptionInfo[portalocker.LockLostError]
    with (  # noqa: PT012
        pytest.raises(portalocker.LockLostError) as error,
        lock,
    ):
        _break_subscription_read(
            monkeypatch,
            lock,
            exceptions.ConnectionError('connection killed'),
        )
        assert _wait_for(lambda: lock.lost)

    assert error.value.channel == channel
    assert error.value.holder_id == lock.holder_id
    # The release ran before the raise.
    assert lock.pubsub is None
    assert lock.thread is None


def test_redis_exit_does_not_mask_body_exception(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The body's own failure outranks the loss on the way out."""
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
        interrupt_on_lost=False,
    )

    with (  # noqa: PT012
        pytest.raises(ValueError, match='body failed'),
        lock,
    ):
        _break_subscription_read(
            monkeypatch,
            lock,
            exceptions.ConnectionError('connection killed'),
        )
        assert _wait_for(lambda: lock.lost)
        raise ValueError('body failed')

    # The loss stays observable even though the exit did not raise it.
    assert lock.lost


def test_redis_exit_raises_loss_landing_during_exit(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A loss racing the block exit itself still surfaces.

    ``__exit__`` used to read ``lost`` before calling ``release``, so a
    revocation landing in the gap between that read and the release's
    state transition ended the ``with`` statement looking successful.
    ``LOST`` is sticky through ``release``, so reading it afterwards
    closes the window outright: the loss is staged here at the last
    possible moment, as the first thing the exit-time release does.
    """
    channel: str = str(random.random())
    lock: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        interrupt_on_lost=False,
    )
    original_release: typing.Callable[[], None] = lock.release

    def racing_release() -> None:
        assert lock.pubsub is not None
        assert lock.thread is not None
        lock._on_worker_exception(
            exceptions.ConnectionError('revoked at block exit'),
            lock.pubsub,
            lock.thread,
        )
        original_release()

    error: pytest.ExceptionInfo[portalocker.LockLostError]
    with (  # noqa: PT012
        pytest.raises(portalocker.LockLostError) as error,
        lock,
    ):
        monkeypatch.setattr(lock, 'release', racing_release)

    assert error.value.channel == channel
    assert error.value.holder_id == lock.holder_id
    # The release ran before the raise.
    assert lock.pubsub is None
    assert lock.thread is None


def test_redis_exit_release_error_does_not_mask_body_exception(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The body's own failure outranks a release error on the way out.

    The same discipline ``Lock.__exit__`` guarantees: when the block is
    already leaving with an exception, a failure inside the exit-time
    ``release`` must not replace it. The release error is chained onto
    the body exception as its ``__context__`` with a note attached, so
    both stay visible while the body's exception is what propagates.
    """
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
        interrupt_on_lost=False,
    )

    def broken_release() -> None:
        raise _TeardownError('release failed at block exit')

    error: pytest.ExceptionInfo[ValueError]
    with (  # noqa: PT012
        pytest.raises(ValueError, match='body failed') as error,
        lock,
    ):
        monkeypatch.setattr(lock, 'release', broken_release)
        raise ValueError('body failed')

    monkeypatch.undo()
    context: BaseException | None = error.value.__context__
    assert isinstance(context, _TeardownError)
    if hasattr(error.value, 'add_note'):
        # Notes exist on 3.11+; on 3.10 the chain helper suppresses the
        # add_note call and the __context__ assertion above carries the
        # guarantee alone.
        assert 'portalocker release failed; see exception context' in getattr(
            error.value, '__notes__', []
        )
    lock.release()


def test_redis_exit_release_error_propagates_after_clean_body(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a clean body the release error is the only failure and wins."""
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
        interrupt_on_lost=False,
    )

    def broken_release() -> None:
        raise _TeardownError('release failed at block exit')

    with (  # noqa: PT012
        pytest.raises(_TeardownError),
        lock,
    ):
        monkeypatch.setattr(lock, 'release', broken_release)

    monkeypatch.undo()
    lock.release()


def test_redis_exit_propagates_body_exception_without_loss(
    redis_connection: ConnectionFactory,
) -> None:
    """A healthy lock's exit releases and lets the body error through."""
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
    )

    with pytest.raises(ValueError, match='body failed'), lock:
        raise ValueError('body failed')

    assert not lock.lost
    assert lock.pubsub is None


@pytest.mark.timeout(180)
def test_redis_waiter_worker_failure_scopes_to_attempt(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A waiter losing its subscription retries instead of escalating.

    Extends the #136 regression coverage per #141: the holder keeps the
    channel while an elected writer waits, the waiter's subscription
    read is broken, and the waiter must neither interrupt the process
    nor mark itself lost - the failure costs one attempt, and once the
    holder releases the waiter acquires normally.
    """
    interrupts: list[bool] = []
    monkeypatch.setattr(
        _thread, 'interrupt_main', lambda: interrupts.append(True)
    )
    channel: str = str(random.random())
    holder: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        flags=portalocker.LockFlags.SHARED,
        unavailable_timeout=0.5,
    )
    waiter: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        timeout=60,
        check_interval=0.02,
        unavailable_timeout=0.5,
        thread_sleep_time=0.01,
    )

    holder.acquire()
    acquired: list[bool] = []
    waiter_thread: threading.Thread = threading.Thread(
        target=lambda: acquired.append(waiter.acquire() is waiter),
        daemon=True,
    )
    waiter_thread.start()
    try:
        # Wait for the stable elected-writer state, in which the waiter
        # keeps one subscription alive between attempts.
        assert _wait_for(
            lambda: waiter.writer_elected and waiter.pubsub is not None,
            timeout=30,
        )
        broken_pubsub: client.PubSub | None = waiter.pubsub
        assert broken_pubsub is not None
        _break_subscription_read(
            monkeypatch,
            waiter,
            exceptions.ConnectionError('waiter connection blip'),
        )
        # The waiter notices, abandons the attempt and resubscribes.
        assert _wait_for(
            lambda: (
                waiter.pubsub is not None
                and waiter.pubsub is not broken_pubsub
            ),
            timeout=30,
        )
        holder.release()
        waiter_thread.join(timeout=60)
        assert not waiter_thread.is_alive()
        assert acquired == [True]
        assert waiter.mode is redis.RedisLockMode.EXCLUSIVE
        assert not waiter.lost
        assert interrupts == []
    finally:
        holder.release()
        waiter.release()


@pytest.mark.timeout(180)
def test_redis_waiter_dead_worker_without_error_retries(
    redis_connection: ConnectionFactory,
) -> None:
    """A silently dead waiter worker also only costs the attempt.

    The worker can die without its handler recording anything, so the
    retry loop checks thread liveness too, not just the recorded error.
    """
    channel: str = str(random.random())
    holder: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        flags=portalocker.LockFlags.SHARED,
        unavailable_timeout=0.5,
    )
    waiter: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        timeout=60,
        check_interval=0.02,
        unavailable_timeout=0.5,
        thread_sleep_time=0.01,
    )

    holder.acquire()
    acquired: list[bool] = []
    waiter_thread: threading.Thread = threading.Thread(
        target=lambda: acquired.append(waiter.acquire() is waiter),
        daemon=True,
    )
    waiter_thread.start()
    try:
        assert _wait_for(
            lambda: waiter.writer_elected and waiter.thread is not None,
            timeout=30,
        )
        stopped_worker: redis.PubSubWorkerThread | None = waiter.thread
        assert stopped_worker is not None
        stopped_worker.stop()
        assert _wait_for(
            lambda: (
                waiter.thread is not None
                and waiter.thread is not stopped_worker
            ),
            timeout=30,
        )
        holder.release()
        waiter_thread.join(timeout=60)
        assert not waiter_thread.is_alive()
        assert acquired == [True]
        assert not waiter.lost
    finally:
        holder.release()
        waiter.release()


def test_redis_confirm_held_race(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subscription dying between the win and the confirm costs a retry.

    The invariant under test: acquire never returns success with a dead
    worker and no notification. The worker error is injected exactly
    between the winning decision and ``_confirm_held``, the narrowest
    possible window, so the first attempt must be refused and the
    second must succeed on a fresh subscription.
    """
    interrupts: list[bool] = []
    monkeypatch.setattr(
        _thread, 'interrupt_main', lambda: interrupts.append(True)
    )
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
        timeout=30,
        check_interval=0.02,
    )
    original_confirm: typing.Callable[[], bool] = lock._confirm_held
    injected: list[bool] = []

    def racing_confirm() -> bool:
        if not injected:
            injected.append(True)
            assert lock.pubsub is not None
            assert lock.thread is not None
            lock._on_worker_exception(
                exceptions.ConnectionError('raced the confirm'),
                lock.pubsub,
                lock.thread,
            )
        return original_confirm()

    monkeypatch.setattr(lock, '_confirm_held', racing_confirm)

    assert lock.acquire() is lock
    try:
        assert injected == [True]
        assert not lock.lost
        assert lock.thread is not None
        assert lock.thread.is_alive()
        assert interrupts == []
    finally:
        lock.release()


def test_redis_shared_joiner_confirm_failure_retries(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The confirm handshake also guards the join-existing-holders path."""
    channel: str = str(random.random())
    holder: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        flags=portalocker.LockFlags.SHARED,
        unavailable_timeout=0.5,
    )
    joiner: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        flags=portalocker.LockFlags.SHARED,
        timeout=30,
        check_interval=0.02,
        unavailable_timeout=0.5,
    )
    original_confirm: typing.Callable[[], bool] = joiner._confirm_held
    injected: list[bool] = []

    def racing_confirm() -> bool:
        if not injected:
            injected.append(True)
            assert joiner.pubsub is not None
            assert joiner.thread is not None
            joiner._on_worker_exception(
                exceptions.ConnectionError('raced the confirm'),
                joiner.pubsub,
                joiner.thread,
            )
        return original_confirm()

    monkeypatch.setattr(joiner, '_confirm_held', racing_confirm)

    holder.acquire()
    try:
        assert joiner.acquire() is joiner
        assert injected == [True]
        assert not joiner.lost
        joiner.release()
    finally:
        holder.release()


def test_redis_confirm_held_requires_live_worker(
    redis_connection: ConnectionFactory,
) -> None:
    """_confirm_held refuses without a live worker thread."""
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
    )
    # No thread at all: a subscription that never started.
    assert not lock._confirm_held()
    assert lock._waiting_attempt_failed()

    lock.acquire()
    try:
        worker: redis.PubSubWorkerThread | None = lock.thread
        assert worker is not None
        worker.stop()
        assert _wait_for(lambda: not worker.is_alive())
        # A stopped worker: the handshake must refuse too.
        assert not lock._confirm_held()
        assert lock._waiting_attempt_failed()
    finally:
        lock.release()


def test_redis_worker_exception_waiting_bug_logs_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-connection worker failure while waiting logs its traceback.

    Also covers the error bookkeeping: a second failure keeps the first
    recorded error, so the eventual report names the root cause rather
    than the follow-up noise.
    """
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))
    first_failure: RuntimeError = RuntimeError('handler bug')
    second_failure: RuntimeError = RuntimeError('follow-up failure')
    worker: redis.PubSubWorkerThread = _alive_worker_thread()

    with caplog.at_level(logging.WARNING, logger='portalocker.redis'):
        lock._on_worker_exception(first_failure, _idle_pubsub(), worker)
        lock._on_worker_exception(second_failure, _idle_pubsub(), worker)

    assert not lock.lost
    assert lock._lost_error is first_failure
    assert any(
        'failed while waiting' in record.message for record in caplog.records
    )


def test_redis_worker_exception_secondary_error_after_loss(
    redis_connection: ConnectionFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A repeat failure after the loss keeps the first error and on_lost.

    redis-py can run the teardown into the same dead socket that caused
    the loss; the second error must neither replace the recorded cause
    nor fire the callback again.
    """
    lost_calls: list[redis.RedisLock] = []
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
        on_lost=lost_calls.append,
        interrupt_on_lost=False,
    )
    first_failure: exceptions.ConnectionError = exceptions.ConnectionError(
        'connection killed'
    )
    second_failure: exceptions.ConnectionError = exceptions.ConnectionError(
        'close failed on the same dead socket'
    )

    lock.acquire()
    try:
        assert lock.pubsub is not None
        assert lock.thread is not None
        with caplog.at_level(logging.DEBUG, logger='portalocker.redis'):
            lock._on_worker_exception(first_failure, lock.pubsub, lock.thread)
            lock._on_worker_exception(second_failure, lock.pubsub, lock.thread)

        assert lock.lost
        assert lock._lost_error is first_failure
        assert lost_calls == [lock]
        assert any(
            'raised again after the loss' in record.message
            for record in caplog.records
        )
    finally:
        lock.release()


def test_redis_implicit_interrupt_on_lost_warns_at_loss(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leaving interrupt_on_lost unset warns when a loss interrupts.

    The 4.2.0 default keeps the historical interrupt, and the warning
    announcing the 5.0.0 flip fires at loss time - when it is relevant -
    rather than at construction time. The handler runs on the calling
    thread here so the warning is caught deterministically.
    """
    interrupts: list[bool] = []
    monkeypatch.setattr(
        _thread, 'interrupt_main', lambda: interrupts.append(True)
    )
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
    )
    assert lock.interrupt_on_lost

    lock.acquire()
    try:
        assert lock.pubsub is not None
        assert lock.thread is not None
        with pytest.warns(DeprecationWarning, match='interrupt_on_lost'):
            lock._on_worker_exception(
                exceptions.ConnectionError('connection killed'),
                lock.pubsub,
                lock.thread,
            )
        assert interrupts == [True]
        assert lock.lost
    finally:
        lock.release()


class _BrokenPool:
    """Pool stand-in whose disconnect raises."""

    def __init__(self, events: list[str], error: Exception) -> None:
        self._events: list[str] = events
        self._error: Exception = error

    def disconnect(self) -> None:
        self._events.append('disconnect')
        raise self._error


class _BrokenSubscriptionClient:
    """Subscription-client stand-in whose whole teardown raises."""

    def __init__(
        self,
        events: list[str],
        close_error: Exception,
        disconnect_error: Exception,
    ) -> None:
        self.events: list[str] = events
        self._close_error: Exception = close_error
        self.connection_pool: _BrokenPool = _BrokenPool(
            events,
            disconnect_error,
        )

    def close(self) -> None:
        self.events.append('close')
        raise self._close_error


def test_redis_unsubscribe_survives_subscription_client_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A dead subscription client's teardown keeps the first error.

    Both teardown steps run even when the first raises, the reference
    is cleared regardless, the first failure propagates and the second
    is logged as suppressed, matching the discipline of the rest of the
    teardown.
    """
    events: list[str] = []
    close_error: _TeardownError = _TeardownError('client close failed')
    disconnect_error: _TeardownError = _TeardownError('pool detach failed')
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))
    lock._subscription_client = typing.cast(
        'client.Redis | None',
        _BrokenSubscriptionClient(events, close_error, disconnect_error),
    )

    exc_info: pytest.ExceptionInfo[_TeardownError]
    with (
        caplog.at_level(logging.WARNING, logger='portalocker.redis'),
        pytest.raises(_TeardownError) as exc_info,
    ):
        lock._unsubscribe()

    assert exc_info.value is close_error
    assert events == ['close', 'disconnect']
    assert lock._subscription_client is None
    assert any(
        'Suppressed secondary' in record.message for record in caplog.records
    )


def test_redis_abandon_failed_attempt_survives_teardown_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A dead attempt's teardown failure is logged, not raised.

    The abandoned subscription usually died with the very connection
    the teardown then trips over, and the retry loop exists to survive
    exactly that, so the error may not abort it.
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

    with caplog.at_level(logging.WARNING, logger='portalocker.redis'):
        lock._abandon_failed_attempt()

    assert lock.pubsub is None
    assert events == ['unsubscribe', 'close']
    assert any(
        'dead subscription attempt' in record.message
        for record in caplog.records
    )


def test_redis_probe_reports_holders(
    redis_connection: ConnectionFactory,
) -> None:
    """probe() answers who is on the channel without touching anything."""
    channel: str = str(random.random())
    holder: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        flags=portalocker.LockFlags.SHARED,
        unavailable_timeout=0.5,
    )
    prober: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        unavailable_timeout=0.5,
    )

    assert prober.probe() == []

    holder.acquire()
    try:
        holders: list[redis.RedisLockHolder] = prober.probe()
        assert [holder_.holder_id for holder_ in holders] == [holder.holder_id]
        assert holders[0].mode is redis.RedisLockMode.SHARED
        # Probing is read-only: the holder still holds, unbothered.
        assert not holder.lost
        assert prober.pubsub is None
    finally:
        holder.release()

    assert prober.probe() == []


def test_redis_probe_does_not_reap(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unanswered probe raises instead of killing or guessing.

    A wedged holder is exactly what ``acquire`` may reap; ``probe`` may
    neither reap it nor report the channel as free, because both would
    hand the caller a conclusion the probe did not earn.
    """
    channel: str = str(random.random())
    holder: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        flags=portalocker.LockFlags.SHARED,
        unavailable_timeout=0.5,
    )
    # Wedge the holder before it subscribes: it stays counted but stops
    # answering pings.
    monkeypatch.setattr(holder, 'channel_handler', lambda message: None)
    prober: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        unavailable_timeout=0.5,
    )
    kills: list[typing.Any] = []
    monkeypatch.setattr(
        prober,
        '_kill_unavailable_locks',
        lambda connection, responding_holders: kills.append(
            responding_holders
        ),
    )

    holder.acquire()
    try:
        with pytest.raises(portalocker.LockException, match='conclusiv'):
            prober.probe(timeout=0.3)
        assert kills == []
        # The wedged holder is still counted: nothing was reaped.
        connection: client.Redis = holder.get_connection()
        assert holder._get_subscriber_count(connection) == 1
    finally:
        holder.release()


def test_redis_check_or_kill_lock_is_deprecated(
    redis_connection: ConnectionFactory,
) -> None:
    """check_or_kill_lock warns and points at probe()."""
    channel: str = str(random.random())
    connection: client.Redis = redis_connection()
    holder: redis.RedisLock = redis.RedisLock(
        channel,
        connection=connection,
        unavailable_timeout=0.5,
    )

    holder.acquire()
    try:
        with pytest.deprecated_call(match='probe'):
            assert holder.check_or_kill_lock(connection, timeout=0.5)
    finally:
        holder.release()


@pytest.mark.parametrize(
    'error_class',
    [exceptions.ConnectionError, exceptions.TimeoutError],
)
def test_redis_transient_probe_error_fails_attempt_cleanly(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
    error_class: type[Exception],
) -> None:
    """A command-connection blip after the subscribe burns one attempt.

    The probe half of an attempt (the subscriber count and the holder
    collection) runs on the command connection *after* the subscription
    went live. An error there used to propagate with the subscription
    still standing: worker alive, state ACQUIRING, a zombie pending
    record blocking every other writer, and the instance itself
    refusing its next ``acquire`` with "already active" until an
    explicit release. The same abandon discipline as a transient
    subscribe failure applies now: with ``timeout=0`` the single
    attempt is consumed, ``AlreadyLocked`` reports the burnt budget,
    and nothing stays behind on the channel or the instance.
    """
    channel: str = str(random.random())
    lock: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        interrupt_on_lost=False,
    )

    def broken_count(connection: client.Redis) -> int:
        raise error_class('command connection failed mid-probe')

    monkeypatch.setattr(lock, '_get_subscriber_count', broken_count)

    with pytest.raises(portalocker.AlreadyLocked):
        lock.acquire(timeout=0)

    assert lock.pubsub is None
    assert lock.thread is None
    assert not lock.lost

    # The channel carries no zombie record: a fresh writer acquires it
    # without waiting anybody out.
    other: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        timeout=5,
        check_interval=0.05,
        unavailable_timeout=0.5,
        interrupt_on_lost=False,
    )
    other.acquire()
    other.release()

    # The failed instance itself recovered too.
    monkeypatch.undo()
    assert lock.acquire(timeout=5) is lock
    lock.release()


def test_redis_transient_probe_error_retries_within_timeout(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One probe blip costs one attempt, exactly like a subscribe blip.

    The #141 scoping promise covers the whole waiter side: a command
    connection that times out under the subscriber count is retried
    within the acquire budget on a fresh subscription, and the failed
    attempt's own subscription is gone, so the eventual hold is the
    only record on the channel.
    """
    admin: client.Redis = redis_connection()
    channel: str = str(random.random())
    lock: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        timeout=30,
        check_interval=0.02,
        interrupt_on_lost=False,
    )
    original_count: typing.Callable[[client.Redis], int] = (
        lock._get_subscriber_count
    )
    failures: list[bool] = []

    def flaky_count(connection: client.Redis) -> int:
        if not failures:
            failures.append(True)
            raise exceptions.TimeoutError('NUMSUB timed out')
        return original_count(connection)

    monkeypatch.setattr(lock, '_get_subscriber_count', flaky_count)

    with caplog.at_level(logging.WARNING, logger='portalocker.redis'):
        assert lock.acquire() is lock
    try:
        assert failures == [True]
        assert any(
            'retried within the timeout' in record.message
            for record in caplog.records
        )
        assert admin.pubsub_numsub(channel)[0][1] == 1
        assert not lock.lost
    finally:
        lock.release()


@pytest.mark.parametrize(
    'error_class',
    [exceptions.AuthenticationError, _TeardownError, KeyboardInterrupt],
)
def test_redis_terminal_probe_error_releases_before_propagating(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
    error_class: type[BaseException],
) -> None:
    """A terminal probe failure releases everything, then propagates.

    Anything that is not a connection blip - bad credentials, a library
    bug, an interrupt landing mid-probe - must leave ``acquire`` the
    way a terminal subscribe failure does: subscription gone, owned
    command connection closed, the instance immediately reusable, and
    the channel free for the next writer. Before the rollback the error
    propagated over a live subscription that blocked the channel until
    someone remembered to call ``release`` on the failed instance.
    """
    seed_pool: typing.Any = redis_connection().connection_pool
    channel: str = str(random.random())
    lock: redis.RedisLock = redis.RedisLock(
        channel,
        redis_kwargs={'connection_pool': seed_pool},
        timeout=5,
        check_interval=0.02,
        interrupt_on_lost=False,
    )

    def broken_count(connection: client.Redis) -> int:
        raise error_class('terminal probe failure')

    monkeypatch.setattr(lock, '_get_subscriber_count', broken_count)

    with pytest.raises(error_class):
        lock.acquire()

    assert lock.pubsub is None
    assert lock.thread is None
    assert lock.connection is None

    other: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        timeout=5,
        check_interval=0.05,
        unavailable_timeout=0.5,
        interrupt_on_lost=False,
    )
    other.acquire()
    other.release()

    monkeypatch.undo()
    assert lock.acquire(timeout=5) is lock
    lock.release()


@pytest.mark.parametrize(
    'error_class',
    [exceptions.AuthenticationError, exceptions.AuthorizationError],
)
def test_redis_non_transient_subscribe_error_takes_terminal_path(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
    error_class: type[Exception],
) -> None:
    """Credential errors from the subscribe are terminal on any backend.

    The fakeredis counterpart of the live wrong-password test: the
    classification itself needs no server, so a subscribe raising a
    non-transient ``ConnectionError`` subclass must take the terminal
    path - one attempt, full release, owned connection closed, error
    propagated - on every CI cell, not only the ones with a live Redis
    to reject a password.
    """
    seed_pool: typing.Any = redis_connection().connection_pool
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        redis_kwargs={'connection_pool': seed_pool},
        timeout=30,
        check_interval=0.02,
    )
    attempts: list[bool] = []

    def rejecting_subscribe(connection: client.Redis) -> None:
        attempts.append(True)
        raise error_class('credentials rejected')

    monkeypatch.setattr(lock, '_start_subscription', rejecting_subscribe)

    with pytest.raises(error_class):
        lock.acquire()

    assert attempts == [True]
    assert lock.pubsub is None
    assert lock.thread is None
    assert lock.connection is None

    # Reusable: the next acquire fails the same clean way instead of
    # tripping the already-active guard.
    with pytest.raises(error_class):
        lock.acquire()
    assert attempts == [True, True]
    assert lock.connection is None


@pytest.mark.parametrize(
    'error_class',
    [exceptions.ConnectionError, exceptions.TimeoutError],
)
def test_redis_acquire_retries_transient_subscribe_failure(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error_class: type[Exception],
) -> None:
    """A confirmation blip on a self-created connection stays serviceable.

    Regression pin for the rollback defect the verifier found: the
    transient-failure rollback used to run through ``release()``, which
    closed and cleared a lock-created command connection. The retry
    then subscribed anyway, but ``channel_handler`` had no connection
    left to answer pings on, so the "held" lock was unanswerable and
    the next prober reaped it within a second. The real
    ``_start_subscription`` must run here (only the confirmation wait
    is made flaky), and after the blip the holder must still answer a
    probe over its own, still-open command connection.
    """
    seed_pool: typing.Any = redis_connection().connection_pool
    channel: str = str(random.random())
    lock: redis.RedisLock = redis.RedisLock(
        channel,
        redis_kwargs={'connection_pool': seed_pool},
        timeout=30,
        check_interval=0.02,
        interrupt_on_lost=False,
    )
    original_wait: typing.Callable[[client.PubSub], None] = (
        lock._wait_for_subscribe_confirmation
    )
    failures: list[bool] = []

    def flaky_wait(pubsub: client.PubSub) -> None:
        if not failures:
            failures.append(True)
            raise error_class('transient confirmation failure')
        original_wait(pubsub)

    monkeypatch.setattr(lock, '_wait_for_subscribe_confirmation', flaky_wait)

    with caplog.at_level(logging.WARNING, logger='portalocker.redis'):
        assert lock.acquire() is lock
    try:
        assert failures == [True]
        assert any(
            'could not subscribe' in record.message
            for record in caplog.records
        )
        # The blip must not have consumed the self-created command
        # connection the ping handler answers on.
        assert lock.connection is not None
        prober: redis.RedisLock = redis.RedisLock(
            channel,
            connection=redis_connection(),
            unavailable_timeout=1,
        )
        holders: list[redis.RedisLockHolder] = prober.probe(timeout=5)
        assert [holder.holder_id for holder in holders] == [lock.holder_id]
        assert not lock.lost
    finally:
        lock.release()
    assert lock.connection is None


def test_redis_terminal_subscribe_failure_closes_created_connection(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-transient subscribe failure still closes an owned connection.

    Transient blips keep the self-created command connection alive for
    the retry, so the terminal path has to pick up the close that used
    to happen inside the rollback: when the error propagates out of
    ``acquire`` the lock must be fully inactive, owned connection
    included.
    """
    seed_pool: typing.Any = redis_connection().connection_pool
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        redis_kwargs={'connection_pool': seed_pool},
        timeout=1,
        check_interval=0.02,
    )

    def broken_wait(pubsub: client.PubSub) -> None:
        raise _SubscribeError('subscription permanently broken')

    monkeypatch.setattr(lock, '_wait_for_subscribe_confirmation', broken_wait)

    with pytest.raises(_SubscribeError):
        lock.acquire()

    assert lock.pubsub is None
    assert lock.thread is None
    assert lock.connection is None


def test_redis_terminal_rollback_failure_keeps_original_error(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing terminal cleanup is logged, the subscribe error wins."""
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
        timeout=1,
        check_interval=0.02,
    )

    def broken_wait(pubsub: client.PubSub) -> None:
        raise _SubscribeError('subscription permanently broken')

    def broken_release() -> None:
        raise _TeardownError('terminal cleanup failed')

    monkeypatch.setattr(lock, '_wait_for_subscribe_confirmation', broken_wait)
    monkeypatch.setattr(lock, 'release', broken_release)

    with (
        caplog.at_level(logging.WARNING, logger='portalocker.redis'),
        pytest.raises(_SubscribeError),
    ):
        lock.acquire()

    assert any('roll back' in record.message for record in caplog.records)


def test_live_redis_wrong_password_raises_promptly(
    redis_connection: ConnectionFactory,
) -> None:
    """Bad credentials raise AuthenticationError, never AlreadyLocked.

    ``AuthenticationError`` subclasses ``ConnectionError`` in redis-py,
    so the transient-blip tolerance used to retry it for the whole
    timeout and then raise ``AlreadyLocked``, burying the actual
    problem. Credentials do not heal on retry: the error must take the
    terminal path promptly, with the owned connection closed and the
    instance still reusable. Live only: fakeredis accepts any password.
    """
    if isinstance(redis_connection(), fakeredis.FakeStrictRedis):
        pytest.skip('fakeredis accepts any password')
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        redis_kwargs={
            'host': os.environ.get('REDIS_HOST', 'localhost'),
            'port': int(os.environ.get('REDIS_PORT', '6379')),
            'password': 'definitely-wrong-password',
        },
        timeout=30,
        check_interval=0.02,
    )

    started: float = time.monotonic()
    with pytest.raises(exceptions.AuthenticationError):
        lock.acquire()
    elapsed: float = time.monotonic() - started

    # Prompt: nowhere near the 30 second retry budget.
    assert elapsed < 5
    assert lock.pubsub is None
    assert lock.thread is None
    assert lock.connection is None
    # Reusable: the next acquire fails the same clean way instead of
    # tripping the already-active guard.
    with pytest.raises(exceptions.AuthenticationError):
        lock.acquire()
    assert lock.connection is None


def test_redis_optional_error_lookup_skips_missing_names() -> None:
    """The optional-exception lookup tolerates older redis-py releases.

    ``ExternalAuthProviderError`` only exists from redis-py 8 onwards
    while portalocker supports redis-py 5, so the non-transient table
    is built through a lookup that must simply skip names an older
    release does not define.
    """
    assert redis._optional_redis_errors('ExternalAuthProviderError') != ()
    assert redis._optional_redis_errors('NoSuchExceptionAnywhere') == ()


def test_redis_interrupt_survives_escalated_deprecation_warning(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DeprecationWarning-as-error filter cannot eat the interrupt.

    Users who run with ``-W error::DeprecationWarning`` make the
    loss-time warning raise inside the handler. The interrupt is the
    documented 4.2 default behaviour and must already have fired by
    then, so the escalated filter costs only the warning, never the
    interrupt.
    """
    interrupts: list[bool] = []
    monkeypatch.setattr(
        _thread, 'interrupt_main', lambda: interrupts.append(True)
    )
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
    )

    lock.acquire()
    try:
        assert lock.pubsub is not None
        assert lock.thread is not None
        with warnings.catch_warnings():
            warnings.simplefilter('error', DeprecationWarning)
            with pytest.raises(DeprecationWarning):
                lock._on_worker_exception(
                    exceptions.ConnectionError('connection killed'),
                    lock.pubsub,
                    lock.thread,
                )
        assert interrupts == [True]
        assert lock.lost
    finally:
        lock.release()


def test_redis_fork_reinit_covers_mode_lock() -> None:
    """`RedisLock` registers its mode lock for the after-fork reinit.

    The fork hook resets every lock an instance reports through
    ``_fork_reinit_locks``. The base class reports the state lock; a
    `RedisLock` must add ``_mode_lock``, because the worker thread
    holds it for every ping snapshot, so a child forked inside such a
    snapshot inherits it locked and its first ``release()`` (the
    documented child action) or garbage collection hangs on it forever.
    """
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))
    fork_locks: list[object] = list(lock._fork_reinit_locks())

    assert lock._state_lock in fork_locks
    assert lock._mode_lock in fork_locks


@pytest.mark.skipif(os.name == 'nt', reason='os.fork is POSIX-only')
def test_redis_forked_child_survives_inherited_held_mode_lock() -> None:
    """A child forked during a ping snapshot must not hang on release.

    ``channel_handler`` takes ``_mode_lock`` for every ping answer, so
    an ``os.fork`` from another thread can capture it locked, owned by
    a worker thread that does not exist in the child. The window is
    held open deterministically by a thread parked inside the mode
    lock across the fork; the child's ``release()`` must return
    promptly instead of deadlocking on the inherited lock, exactly as
    the state lock has been guaranteed since 4.2.0.
    """
    server: fakeredis.FakeServer = fakeredis.FakeServer()
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=fakeredis.FakeStrictRedis(
            server=server,
            decode_responses=True,
        ),
        interrupt_on_lost=False,
    )
    lock.acquire()
    inside = threading.Event()
    gate = threading.Event()

    def hold_mode_lock() -> None:
        with lock._mode_lock:
            inside.set()
            gate.wait(timeout=10)

    holder = threading.Thread(target=hold_mode_lock, daemon=True)
    holder.start()
    assert inside.wait(timeout=5), 'the holder never took the mode lock'

    pid: int = os.fork()
    if pid == 0:  # pragma: no cover - child process, exits via os._exit
        lock.release()
        os._exit(0)

    deadline: float = time.monotonic() + 5
    status: int | None = None
    while time.monotonic() < deadline:
        waited, waitstatus = os.waitpid(pid, os.WNOHANG)
        if waited:
            status = os.waitstatus_to_exitcode(waitstatus)
            break
        time.sleep(0.01)
    gate.set()
    holder.join(timeout=5)
    if status is None:  # pragma: no cover - only reached when the bug is back
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        pytest.fail('the forked child hung on the inherited mode lock')
    assert status == 0
    lock.release()


def test_redis_release_in_forked_child_leaves_parent_lock_alone(
    redis_connection: ConnectionFactory,
) -> None:
    """A forked child's release must not revoke the parent's lock.

    A forked child inherits the lock object and its sockets. Its
    ``release`` (explicit or via garbage collection) used to send
    UNSUBSCRIBE over the inherited socket, silently releasing the
    parent's lock while the parent's connection stayed alive, so
    neither the loss machinery nor a probe would ever tell the parent.
    The teardown must recognise the foreign process (simulated here by
    patching ``os.getpid``) and only drop the child's local
    references. The parent-side worker, subscription and connection
    stay untouched.
    """
    seed_pool: typing.Any = redis_connection().connection_pool
    admin: client.Redis = redis_connection()
    channel: str = str(random.random())
    lock: redis.RedisLock = redis.RedisLock(
        channel,
        redis_kwargs={'connection_pool': seed_pool},
        interrupt_on_lost=False,
    )

    lock.acquire()
    worker: redis.PubSubWorkerThread | None = lock.thread
    pubsub: client.PubSub | None = lock.pubsub
    command_connection: client.Redis | None = lock.connection
    subscription_client: client.Redis | None = lock._subscription_client
    assert worker is not None
    assert pubsub is not None
    assert command_connection is not None
    assert subscription_client is not None
    assert admin.pubsub_numsub(channel)[0][1] == 1

    parent_pid: int = os.getpid()
    with pytest.MonkeyPatch.context() as fork_patch:
        fork_patch.setattr(os, 'getpid', lambda: parent_pid + 1)
        lock.release()

    # The child's references are gone, without touching the sockets.
    assert lock.thread is None
    assert lock.pubsub is None
    assert lock._subscription_client is None
    assert lock.connection is None
    assert not lock.lost
    # The parent's lock is untouched: worker alive, still subscribed.
    assert worker.is_alive()
    assert admin.pubsub_numsub(channel)[0][1] == 1

    # Parent-side cleanup, now genuinely in the owning process.
    worker.stop()
    worker.join()
    pubsub.close()
    subscription_client.close()
    subscription_client.connection_pool.disconnect()
    command_connection.close()


def test_redis_release_from_on_lost_callback_succeeds(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``on_lost`` callback may release the lock it is told about.

    The callback runs on the keep-alive worker thread, and ``release``
    joins that thread during teardown. Joining yourself raises
    ``RuntimeError: cannot join current thread``, so a callback that
    reacted to the loss with the obvious ``lock.release()`` blew up
    after most of the teardown had already run. The join is skipped on
    the worker thread now: the callback's release completes quietly,
    the worker exits on its own right after, and the instance is fully
    torn down and reusable.
    """
    events: list[str] = []

    def releasing_callback(lost_lock: redis.RedisLock) -> None:
        try:
            lost_lock.release()
        except BaseException as error:  # noqa: BLE001
            events.append(f'release raised {type(error).__name__}')
        else:
            events.append('release ok')

    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
        on_lost=releasing_callback,
        interrupt_on_lost=False,
    )

    lock.acquire()
    worker: redis.PubSubWorkerThread | None = lock.thread
    assert worker is not None
    _break_subscription_read(
        monkeypatch,
        lock,
        exceptions.ConnectionError('connection killed'),
    )

    assert _wait_for(lambda: bool(events))
    assert events == ['release ok']
    # The worker was not joined, so it winds down on its own.
    assert _wait_for(lambda: not worker.is_alive())
    assert lock.pubsub is None
    assert lock.thread is None
    assert lock._subscription_client is None
    # The loss stays observable through the release, as always.
    assert lock.lost

    # A released-from-callback instance is a normal released instance:
    # a later main-thread release is a quiet no-op and a fresh acquire
    # resets and takes the lock again.
    lock.release()
    assert lock.acquire(timeout=5) is lock
    # Read through a local: mypy narrows the property to True from the
    # assert above and would call a direct re-check unreachable.
    lost_after_reacquire: bool = lock.lost
    assert not lost_after_reacquire
    lock.release()


def test_redis_on_lost_base_exception_is_contained(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An on_lost raising SystemExit cannot skip interrupt or teardown.

    ``BaseException`` from the callback used to escape the handler,
    skipping the main-thread interrupt and redis-py's post-loop
    ``pubsub.close()``. The containment must cover ``BaseException``,
    matching what the documentation promises.
    """
    interrupts: list[bool] = []
    monkeypatch.setattr(
        _thread, 'interrupt_main', lambda: interrupts.append(True)
    )

    def bailing_callback(lock_: redis.RedisLock) -> None:
        raise SystemExit('callback bail-out')

    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
        on_lost=bailing_callback,
        interrupt_on_lost=True,
    )

    lock.acquire()
    worker: redis.PubSubWorkerThread | None = lock.thread
    assert worker is not None
    with caplog.at_level(logging.ERROR, logger='portalocker.redis'):
        _break_subscription_read(
            monkeypatch,
            lock,
            exceptions.ConnectionError('connection killed'),
        )
        assert _wait_for(lambda: not worker.is_alive())

    assert lock.lost
    assert interrupts == [True]
    # redis-py's post-loop close ran: the pubsub gave its connection up.
    assert lock.pubsub is not None
    assert lock.pubsub.connection is None
    assert any(
        'on_lost callback failed' in record.message
        for record in caplog.records
    )
    lock.release()


def test_redis_subscription_derivation_without_pool_names_escape_hatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client without a connection pool fails with the factory hint.

    Cluster clients have no ``connection_pool`` attribute at all, so
    the derivation failure is an ``AttributeError`` rather than a
    ``TypeError``. Both must land in the same ``LockException`` naming
    ``subscription_connection_factory``.
    """
    connection: fakeredis.FakeStrictRedis = fakeredis.FakeStrictRedis(
        server=fakeredis.FakeServer(),
        decode_responses=True,
    )
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=connection,
    )
    monkeypatch.delattr(connection, 'connection_pool')

    with pytest.raises(
        portalocker.LockException,
        match='subscription_connection_factory',
    ):
        lock.acquire()

    assert lock.pubsub is None


def test_redis_fresh_subscription_clears_stale_worker_error(
    redis_connection: ConnectionFactory,
) -> None:
    """A stale error from a dead prior attempt cannot refuse the next.

    A worker can die and record its error during the between-attempts
    teardown, after ``_abandon_failed_attempt`` already consumed the
    previous one. Starting a fresh subscription must clear that
    leftover, or ``_confirm_held`` refuses a perfectly healthy attempt
    once before converging.
    """
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
    )
    connection: client.Redis = lock.get_connection()
    with lock._state_lock:
        lock._lock_state = redis._LockState.ACQUIRING
        lock._lost_error = RuntimeError('stale straggler error')

    lock._start_subscription(connection)
    try:
        assert lock._confirm_held()
    finally:
        lock.release()


def _skip_without_client_kill(connection: client.Redis) -> None:
    """Skip on fakeredis, which does not implement CLIENT KILL."""
    if isinstance(connection, fakeredis.FakeStrictRedis):
        pytest.skip('fakeredis does not implement CLIENT KILL')


def _clients_named(
    connection: client.Redis,
    client_name: str,
) -> list[dict[str, str]]:
    """Return the CLIENT LIST entries carrying ``client_name``."""
    return [
        client_
        for client_ in connection.client_list()
        if client_.get('name') == client_name
    ]


@pytest.mark.timeout(180)
def test_live_redis_client_kill_revokes_lock_loudly(
    redis_connection: ConnectionFactory,
) -> None:
    """CLIENT KILL revokes a held lock loudly, end to end (#137 pin).

    On portalocker 4.1 this fails by construction: the killed holder's
    connection reconnected and resubscribed without a name, the holder
    kept believing it held the lock, the subscriber count stayed
    inflated, and the nameless ghost could never be reaped again. Now
    the holder observes the loss, the channel drains to zero, no
    connection under the holder's name survives, and a waiter acquires
    exclusively.
    """
    holder_connection: client.Redis = redis_connection()
    _skip_without_client_kill(holder_connection)
    admin: client.Redis = redis_connection()
    channel: str = str(random.random())
    lost_calls: list[redis.RedisLock] = []
    holder: redis.RedisLock = redis.RedisLock(
        channel,
        connection=holder_connection,
        on_lost=lost_calls.append,
        interrupt_on_lost=False,
        unavailable_timeout=0.5,
        thread_sleep_time=0.01,
    )
    waiter: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        timeout=30,
        check_interval=0.02,
        unavailable_timeout=0.5,
    )

    holder.acquire()
    try:
        worker: redis.PubSubWorkerThread | None = holder.thread
        assert worker is not None
        named_clients: list[dict[str, str]] = _clients_named(
            admin,
            holder.client_name,
        )
        assert len(named_clients) == 1
        admin.client_kill_filter(named_clients[0].get('id'))

        # The worker thread runs the whole escalation before it ends,
        # so its death means every loss side effect has landed.
        assert _wait_for(lambda: not worker.is_alive())
        assert holder.lost
        assert lost_calls == [holder]
        with pytest.raises(portalocker.LockLostError):
            holder.ensure_held()

        # No silent resubscribe: the channel drains to zero and stays
        # there past several worker wake-ups.
        assert _wait_for(
            lambda: admin.pubsub_numsub(channel)[0][1] == 0,
        )
        time.sleep(0.2)
        assert admin.pubsub_numsub(channel)[0][1] == 0
        assert _clients_named(admin, holder.client_name) == []

        # The channel is genuinely free again: a waiter takes it.
        waiter.acquire()
        assert waiter.mode is redis.RedisLockMode.EXCLUSIVE
    finally:
        waiter.release()
        holder.release()
        admin.close()


def _reap_wedged_holder(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[redis.RedisLock, redis.RedisLock, float]:
    """Wedge a holder, let a waiter reap it, and time the takeover.

    Returns the wedged holder, the waiter now holding the channel, and
    the ``time.monotonic`` timestamp at which the waiter's acquire
    returned (the takeover instant the loss latency is measured from).
    """
    channel: str = str(random.random())
    holder: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        flags=portalocker.LockFlags.SHARED,
        unavailable_timeout=0.5,
        thread_sleep_time=0.01,
    )
    # Wedged: still counted by Redis, never answers another ping.
    monkeypatch.setattr(holder, 'channel_handler', lambda message: None)
    waiter: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        timeout=30,
        check_interval=0.02,
        unavailable_timeout=0.5,
    )

    holder.acquire()
    waiter.acquire()
    return holder, waiter, time.monotonic()


@pytest.mark.timeout(180)
def test_live_redis_reaped_wedged_holder_observes_loss(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reaped holder observes its loss within a bounded delay.

    The split-brain window is the detection latency, not zero, so this
    pins the bound instead of pretending: the wedged holder's worker
    reads every ``thread_sleep_time`` (10ms here) and the kill closes
    its socket, so the loss must land well within the two-second
    ceiling this asserts (generous for CI, still a world away from the
    old behaviour of never noticing at all).
    """
    _skip_without_client_kill(redis_connection())
    holder, waiter, taken_over_at = _reap_wedged_holder(
        redis_connection,
        monkeypatch,
    )
    try:
        assert waiter.mode is redis.RedisLockMode.EXCLUSIVE
        assert _wait_for(lambda: holder.lost, timeout=2.0)
        loss_latency: float = time.monotonic() - taken_over_at
        assert loss_latency <= 2.0
        with pytest.raises(portalocker.LockLostError):
            holder.ensure_held()
    finally:
        waiter.release()
        holder.release()


@pytest.mark.timeout(180)
def test_live_redis_no_unreapable_ghost(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reaping a wedged holder leaves no ghost subscriber behind (#137).

    The denial-of-lock corollary of #137: a killed holder used to
    resubscribe namelessly, inflating the subscriber count forever and
    making every later exclusive acquire impossible. After the reap the
    channel must count exactly the one true holder and carry no
    connection named after the reaped one.
    """
    admin: client.Redis = redis_connection()
    _skip_without_client_kill(admin)
    holder, waiter, _taken_over_at = _reap_wedged_holder(
        redis_connection,
        monkeypatch,
    )
    try:
        assert _wait_for(lambda: holder.lost, timeout=2.0)
        channel: str = waiter.channel
        assert admin.pubsub_numsub(channel)[0][1] == 1
        assert _clients_named(admin, holder.client_name) == []
        assert len(_clients_named(admin, waiter.client_name)) == 1
    finally:
        waiter.release()
        holder.release()
        admin.close()


@pytest.mark.timeout(60)
def test_redis_worker_stop_before_run_is_not_lost(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``stop()`` that lands before the worker ran must still stop it.

    redis-py's ``PubSubWorkerThread.run`` sets its running flag from
    inside the thread and ``stop()`` clears it, so a stop issued after
    ``start()`` but before the new thread reached ``run()`` was
    overwritten by the thread's own set, and the read loop then ran
    forever while ``_unsubscribe`` sat in ``join()``. Production
    reaches that ordering whenever a CPU-starved worker thread has not
    been scheduled yet by the time a refused confirm, a lost election
    or ``fail_when_locked`` tears the fresh subscription down, and the
    confirm-race test hit it on a loaded CI runner. The ordering is
    staged exactly: the worker's ``run`` waits on a gate that only
    opens once ``_unsubscribe`` has issued its stop and is about to
    join.
    """
    interrupts: list[bool] = []
    monkeypatch.setattr(
        _thread, 'interrupt_main', lambda: interrupts.append(True)
    )
    gate = threading.Event()
    original_run: typing.Callable[[redis.PubSubWorkerThread], None] = (
        redis.PubSubWorkerThread.run
    )

    def gated_run(self: redis.PubSubWorkerThread) -> None:
        assert gate.wait(timeout=30), 'the worker gate never opened'
        original_run(self)

    monkeypatch.setattr(redis.PubSubWorkerThread, 'run', gated_run)
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
        timeout=30,
        check_interval=0.02,
    )
    original_confirm: typing.Callable[[], bool] = lock._confirm_held
    injected: list[bool] = []

    def racing_confirm() -> bool:
        if not injected:
            injected.append(True)
            worker: redis.PubSubWorkerThread | None = lock.thread
            assert lock.pubsub is not None
            assert worker is not None
            # The worker has been started but is parked before `run`.
            assert worker.is_alive()
            lock._on_worker_exception(
                exceptions.ConnectionError('raced the confirm'),
                lock.pubsub,
                worker,
            )
            original_join: typing.Callable[..., None] = worker.join

            def join_after_opening_gate(
                *args: typing.Any,
                **kwargs: typing.Any,
            ) -> None:
                # `_unsubscribe` has issued its stop by now: let the
                # worker enter `run` only afterwards.
                gate.set()
                original_join(*args, **kwargs)

            monkeypatch.setattr(worker, 'join', join_after_opening_gate)
        else:
            gate.set()
        return original_confirm()

    monkeypatch.setattr(lock, '_confirm_held', racing_confirm)

    assert lock.acquire() is lock
    try:
        assert injected == [True]
        assert not lock.lost
        assert interrupts == []
    finally:
        lock.release()


# --------------------------------------------------------------------- #
#  Opt-in self-check heartbeat (#146)
# --------------------------------------------------------------------- #


def _record_self_check_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    lock: redis.RedisLock,
    entered: threading.Event | None = None,
) -> list[str]:
    """Record how every self-check ``lock`` runs from now on ends.

    Each completed check appends ``'passed'``, each failed one the name
    of the exception type it raised. ``entered``, when given, is set the
    moment a check starts, so a test can hold a check mid-flight.
    """
    outcomes: list[str] = []
    original: typing.Callable[[client.PubSub], None] = lock._run_self_check

    def recording(held_pubsub: client.PubSub) -> None:
        if entered is not None:
            entered.set()
        try:
            original(held_pubsub)
        except BaseException as error:
            outcomes.append(type(error).__name__)
            raise
        outcomes.append('passed')

    monkeypatch.setattr(lock, '_run_self_check', recording)
    return outcomes


def _disable_ping_handler(lock: redis.RedisLock) -> None:
    """Silence the held subscription's handler, keeping delivery alive.

    The staged silent failure: frames still arrive and are read, but
    the holder no longer reacts to them, so its own self-check ping
    goes unanswered while no read ever raises.
    """
    pubsub: client.PubSub | None = lock.pubsub
    assert pubsub is not None
    for key in list(pubsub.channels):
        pubsub.channels[key] = lambda message: None


@pytest.mark.parametrize('interval', [0.0, -1.0])
def test_redis_self_check_interval_must_be_positive(interval: float) -> None:
    """A non-positive self-check interval is a configuration error."""
    with pytest.raises(ValueError, match='self_check_interval'):
        redis.RedisLock(str(random.random()), self_check_interval=interval)


def test_redis_self_check_passes_while_healthy(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A healthy held lock passes check after check and stays held."""
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
        thread_sleep_time=0.01,
        # A generous interval: the echo deadline is min(interval,
        # unavailable_timeout), and a loaded CI runner can miss a tight
        # one, failing a perfectly healthy check.
        self_check_interval=0.25,
        interrupt_on_lost=False,
    )
    outcomes: list[str] = _record_self_check_outcomes(monkeypatch, lock)

    lock.acquire()
    try:
        assert _wait_for(lambda: len(outcomes) >= 2)
        assert set(outcomes) == {'passed'}
        assert not _lost(lock)
        lock.ensure_held()
    finally:
        lock.release()


@pytest.mark.parametrize('interrupt_on_lost', [True, False])
def test_redis_self_check_detects_silent_delivery_failure(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_on_lost: bool,
) -> None:
    """A subscription that delivers nothing flips to LOST in one interval.

    The handler is disabled rather than the socket broken, so no read
    ever raises: only the end-to-end self-check can notice that the
    holder's own ping no longer comes back, and its failure must run
    the identical loss escalation a socket error runs (#146).
    """
    interrupts: list[bool] = []
    monkeypatch.setattr(
        _thread, 'interrupt_main', lambda: interrupts.append(True)
    )
    lost_calls: list[redis.RedisLock] = []
    channel: str = str(random.random())
    lock: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        thread_sleep_time=0.01,
        unavailable_timeout=0.5,
        self_check_interval=0.1,
        on_lost=lost_calls.append,
        interrupt_on_lost=interrupt_on_lost,
    )

    lock.acquire()
    assert not _lost(lock)
    worker: redis.PubSubWorkerThread | None = lock.thread
    assert worker is not None
    _disable_ping_handler(lock)

    # The worker runs the whole escalation before it ends, so its death
    # means every loss side effect has landed.
    assert _wait_for(lambda: not worker.is_alive())
    assert _lost(lock)
    assert lost_calls == [lock]
    assert interrupts == ([True] if interrupt_on_lost else [])
    error: pytest.ExceptionInfo[portalocker.LockLostError]
    with pytest.raises(portalocker.LockLostError) as error:
        lock.ensure_held()
    assert error.value.channel == channel
    assert isinstance(error.value.__cause__, redis.RedisLockSelfCheckError)
    lock.release()
    assert _lost(lock)


def test_redis_self_check_no_traffic_when_unset(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an interval the lock publishes nothing while held."""
    connection: client.Redis = redis_connection()
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=connection,
        thread_sleep_time=0.01,
    )
    outcomes: list[str] = _record_self_check_outcomes(monkeypatch, lock)
    published: list[str] = []
    original_publish: typing.Callable[..., typing.Any] = connection.publish

    def counting_publish(
        channel: str,
        message: str,
    ) -> typing.Any:
        published.append(channel)
        return original_publish(channel, message)

    monkeypatch.setattr(connection, 'publish', counting_publish)

    lock.acquire()
    try:
        time.sleep(0.2)  # roughly twenty worker read cycles
        assert published == []
        assert outcomes == []
        worker: redis.PubSubWorkerThread | None = lock.thread
        assert worker is not None
        assert worker._tick is None
    finally:
        lock.release()


def test_redis_self_check_noop_while_waiting(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A waiter's ticks run but never start a check while ACQUIRING."""
    channel: str = str(random.random())
    holder: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        thread_sleep_time=0.01,
    )
    waiter: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        thread_sleep_time=0.01,
        unavailable_timeout=0.3,
        self_check_interval=0.01,
    )
    ticks: list[bool] = []
    original_tick: typing.Callable[[client.PubSub], None] = (
        waiter._self_check_tick
    )

    def counting_tick(held_pubsub: client.PubSub) -> None:
        ticks.append(True)
        original_tick(held_pubsub)

    monkeypatch.setattr(waiter, '_self_check_tick', counting_tick)
    outcomes: list[str] = _record_self_check_outcomes(monkeypatch, waiter)

    holder.acquire()
    try:
        with pytest.raises(portalocker.AlreadyLocked):
            waiter.acquire(timeout=0.3)
        assert ticks
        assert outcomes == []
    finally:
        holder.release()


def test_redis_self_check_aborts_quietly_when_released_mid_check(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A release landing mid-check abandons the check, not the release.

    The check is pinned in flight (its reply can never arrive and its
    deadline is far away), then the lock is released from the main
    thread. The check must notice the state change within one poll and
    stop without declaring a loss or delaying the release by anything
    near its deadline.
    """
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
        thread_sleep_time=0.01,
        unavailable_timeout=5,
        self_check_interval=5,
        interrupt_on_lost=False,
    )
    entered: threading.Event = threading.Event()
    outcomes: list[str] = _record_self_check_outcomes(
        monkeypatch,
        lock,
        entered,
    )

    lock.acquire()
    _disable_ping_handler(lock)  # the check can never complete on its own
    with lock._state_lock:
        lock._next_self_check = 0.0  # the next tick starts a check now
    assert entered.wait(timeout=5)

    started: float = time.monotonic()
    lock.release()
    elapsed: float = time.monotonic() - started

    assert elapsed < 2
    assert not _lost(lock)
    assert outcomes == ['_SelfCheckAbandoned']


def test_redis_self_check_fails_without_subscribe_confirmation(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A response channel that never confirms is a failed check too.

    The reply deadline covers the whole round trip, the response
    channel's own subscribe confirmation included: a holder whose
    command path cannot even set up the return channel could not answer
    another prober's ping either, so it must not keep believing it
    holds the lock.
    """
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
        thread_sleep_time=0.01,
        unavailable_timeout=0.3,
        self_check_interval=0.05,
        interrupt_on_lost=False,
    )
    lock.acquire()
    original_get_pubsub: typing.Callable[[client.Redis], client.PubSub] = (
        lock._get_pubsub
    )

    def deaf_pubsub(connection: client.Redis) -> client.PubSub:
        pubsub: client.PubSub = original_get_pubsub(connection)
        monkeypatch.setattr(
            pubsub,
            'get_message',
            lambda *args, **kwargs: None,
        )
        return pubsub

    monkeypatch.setattr(lock, '_get_pubsub', deaf_pubsub)

    assert _wait_for(lambda: lock.lost)
    error: pytest.ExceptionInfo[portalocker.LockLostError]
    with pytest.raises(portalocker.LockLostError) as error:
        lock.ensure_held()
    assert isinstance(error.value.__cause__, redis.RedisLockSelfCheckError)
    lock.release()


class _ServicedHeldPubSub:
    """Held-subscription stand-in that counts its service reads."""

    reads: int

    def __init__(self) -> None:
        self.reads = 0

    def get_message(
        self,
        timeout: float = 0.0,
        ignore_subscribe_messages: bool = False,
    ) -> None:
        self.reads += 1
        return


def test_redis_self_check_skips_other_holders_replies() -> None:
    """The reply wait ignores everything that is not this holder's echo.

    A self-check ping is an ordinary probe ping, so every other holder
    on the channel answers it too, and unparsable noise can land on
    the response channel as well. Only this holder's own record may
    conclude the check, and the held subscription is serviced while
    the wait polls.
    """
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        self_check_interval=1,
    )
    with lock._state_lock:
        lock._lock_state = redis._LockState.HELD
    response: _ScriptedPubSub = _ScriptedPubSub(
        [
            None,  # first poll comes up empty: the held side is serviced
            _probe_reply('0' * 32),  # another holder's answer
            {'type': 'message', 'data': 'junk'},  # unparsable noise
            _probe_reply(lock.holder_id),  # this holder's own echo
        ]
    )
    held: _ServicedHeldPubSub = _ServicedHeldPubSub()

    replied: bool = lock._await_self_check_frame(
        typing.cast('client.PubSub', response),
        typing.cast('client.PubSub', held),
        time.monotonic() + 5,
        0.001,
        lock._is_own_probe_reply,
    )

    assert replied is True
    assert held.reads >= 1
    assert response.messages == []
    with lock._state_lock:
        lock._lock_state = redis._LockState.IDLE


@pytest.mark.timeout(60)
def test_live_redis_self_check_detects_half_open_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live link that silently stops delivering is detected (#146).

    As close to a real half-open link as honest staging gets without
    netem: the subscription connection's ``can_read`` is pinned False,
    so every read sees nothing, nothing errors, and the socket stays
    open - which is exactly what a partition with no RST looks like
    from the reader. The server keeps counting the holder the whole
    time, and only the end-to-end self-check can notice.
    """
    _ensure_live_redis_available(_LIVE_REDIS)
    admin: client.Redis = _live_redis_connection()
    lost_calls: list[redis.RedisLock] = []
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=_live_redis_connection(),
        thread_sleep_time=0.01,
        unavailable_timeout=0.5,
        self_check_interval=0.2,
        on_lost=lost_calls.append,
        interrupt_on_lost=False,
    )
    outcomes: list[str] = _record_self_check_outcomes(monkeypatch, lock)

    lock.acquire()
    try:
        # Healthy first: at least one full round trip passes.
        assert _wait_for(lambda: 'passed' in outcomes)
        assert not _lost(lock)

        pubsub: client.PubSub | None = lock.pubsub
        assert pubsub is not None
        subscription_connection: typing.Any = pubsub.connection
        assert subscription_connection is not None
        monkeypatch.setattr(
            subscription_connection,
            'can_read',
            lambda timeout=0: False,
        )
        # The server was never told anything: it still counts the
        # holder while delivery is already dead.
        assert len(_clients_named(admin, lock.client_name)) == 1

        assert _wait_for(lambda: lock.lost, timeout=10)
        assert lost_calls == [lock]
        error: pytest.ExceptionInfo[portalocker.LockLostError]
        with pytest.raises(portalocker.LockLostError) as error:
            lock.ensure_held()
        assert isinstance(
            error.value.__cause__,
            redis.RedisLockSelfCheckError,
        )
        # The loss teardown closes the socket, so the ghost the server
        # kept counting disappears too.
        assert _wait_for(
            lambda: _clients_named(admin, lock.client_name) == [],
            timeout=10,
        )
    finally:
        lock.release()
        if lock.connection is not None:
            lock.connection.close()
        admin.close()


# --------------------------------------------------------------------- #
#  Opt-in fencing tokens (#146)
# --------------------------------------------------------------------- #


def test_redis_fence_token_increments_across_grants(
    redis_connection: ConnectionFactory,
) -> None:
    """Alternating exclusive grants draw strictly increasing tokens."""
    channel: str = str(random.random())
    connection: client.Redis = redis_connection()
    first: redis.RedisLock = redis.RedisLock(
        channel,
        connection=connection,
        thread_sleep_time=0.01,
        fencing=True,
    )
    second: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        thread_sleep_time=0.01,
        fencing=True,
    )

    first.acquire(timeout=5)
    assert first.fence_token == 1
    first.release()
    second.acquire(timeout=5)
    assert second.fence_token == 2
    second.release()
    first.acquire(timeout=5)
    assert first.fence_token == 3
    first.release()

    # Monotonicity is the key's job, so release leaves it behind.
    assert connection.get(f'{channel}-fence') == '3'


def test_redis_fence_token_none_while_unheld_shared_or_disabled(
    redis_connection: ConnectionFactory,
) -> None:
    """No token without fencing, without a grant, or for a reader."""
    connection: client.Redis = redis_connection()
    plain: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=connection,
        thread_sleep_time=0.01,
    )
    assert plain.fence_token is None  # fencing disabled
    plain.acquire(timeout=5)
    assert plain.fence_token is None  # even while held
    plain.release()

    shared: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
        thread_sleep_time=0.01,
        flags=portalocker.LockFlags.SHARED,
        fencing=True,
    )
    assert shared.fence_token is None
    shared.acquire(timeout=5)
    assert shared.fence_token is None  # shared grants draw no token
    shared.release()
    # A shared channel never grows a fence key either.
    assert connection.exists(f'{shared.channel}-fence') == 0

    fresh: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        fencing=True,
    )
    assert fresh.fence_token is None  # never acquired


def test_redis_fence_token_available_inside_with_block(
    redis_connection: ConnectionFactory,
) -> None:
    """The token is readable inside the block and survives the exit."""
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
        thread_sleep_time=0.01,
        fencing=True,
    )

    with lock:
        assert lock.fence_token == 1
    assert lock.fence_token == 1  # survives until the next acquire


def test_redis_fence_token_reset_by_the_next_acquire(
    redis_connection: ConnectionFactory,
) -> None:
    """The next acquire consumes the previous grant's token."""
    channel: str = str(random.random())
    lock: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        thread_sleep_time=0.01,
        fencing=True,
    )
    holder: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        thread_sleep_time=0.01,
    )

    lock.acquire(timeout=5)
    assert lock.fence_token == 1
    lock.release()
    holder.acquire(timeout=5)
    try:
        with pytest.raises(portalocker.AlreadyLocked):
            lock.acquire(timeout=0, fail_when_locked=True)
        assert lock.fence_token is None  # the failed acquire reset it
    finally:
        holder.release()


def test_redis_fence_wrong_typed_key_fails_acquire_terminally(
    redis_connection: ConnectionFactory,
) -> None:
    """A fence key of the wrong type fails the acquire, fully released.

    ``INCR`` on a non-integer value repeats identically on every retry,
    so burning the timeout on it would bury the configuration problem
    under a misleading ``AlreadyLocked``. The error propagates, and the
    lock must be off the channel: held-with-fencing always implies a
    token, so a grant that cannot draw one may not stand.
    """
    channel: str = str(random.random())
    connection: client.Redis = redis_connection()
    connection.set(f'{channel}-fence', 'not-a-counter')
    lock: redis.RedisLock = redis.RedisLock(
        channel,
        connection=connection,
        thread_sleep_time=0.01,
        fencing=True,
    )

    with pytest.raises(exceptions.ResponseError):
        lock.acquire(timeout=1)

    assert lock.fence_token is None
    assert lock.pubsub is None  # fully released, no zombie subscription
    assert not _lost(lock)
    assert connection.pubsub_numsub(channel)[0][1] == 0

    # The instance stays usable once the key is fixed.
    connection.delete(f'{channel}-fence')
    lock.acquire(timeout=5)
    try:
        assert lock.fence_token == 1
    finally:
        lock.release()


def test_redis_fence_transient_incr_failure_burns_one_attempt(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection blip during INCR costs one attempt, not the acquire."""
    channel: str = str(random.random())
    connection: client.Redis = redis_connection()
    lock: redis.RedisLock = redis.RedisLock(
        channel,
        connection=connection,
        thread_sleep_time=0.01,
        fencing=True,
    )
    attempts: list[str] = []
    original_incr: typing.Callable[..., typing.Any] = connection.incr

    def flaky_incr(key: str) -> typing.Any:
        attempts.append(key)
        if len(attempts) == 1:
            raise exceptions.ConnectionError('fence INCR hit a blip')
        return original_incr(key)

    monkeypatch.setattr(connection, 'incr', flaky_incr)

    lock.acquire(timeout=5)
    try:
        assert lock.fence_token == 1
        assert attempts == [f'{channel}-fence'] * 2
    finally:
        lock.release()


def test_redis_fence_token_survives_loss_until_next_acquire(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost hold keeps its token readable for forensic use."""
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=redis_connection(),
        thread_sleep_time=0.01,
        fencing=True,
        interrupt_on_lost=False,
    )

    lock.acquire(timeout=5)
    assert lock.fence_token == 1
    _break_subscription_read(
        monkeypatch,
        lock,
        exceptions.ConnectionError('connection killed'),
    )
    assert _wait_for(lambda: lock.lost)
    assert lock.fence_token == 1  # the token the lost hold carried
    lock.release()
    assert lock.fence_token == 1
    lock.acquire(timeout=5)
    try:
        assert not _lost(lock)
        assert lock.fence_token == 2
    finally:
        lock.release()
