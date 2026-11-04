"""RedisLock hardening from the 2026 security audit (run-2).

Every test here pins one audit finding against a channel shared with a
lower-trust Redis client, exercised against ``fakeredis`` and, when a
live server answers, against that server too:

- finding 5: an undecodable or hostile frame on the lock channel must
  not revoke the held lock;
- finding 6: a copy of a held lock must never become a second owner of
  the subscription;
- finding 9: forged probe replies make a probe inconclusive instead of
  filling the expected subscriber count;
- finding 8: an unexplained silent subscriber is reported, once per
  acquire, instead of being retried in silence;
- finding 10: the self-check only passes on an echo the held
  subscription itself delivered;
- finding 4: a fenced grant is confirmed through the held subscription
  after the token is drawn;
- finding 17: a forked child gets its own holder identity.
"""

from __future__ import annotations

import _thread
import copy
import json
import logging
import os
import pickle
import random
import threading
import time
import typing

import fakeredis
import pytest
from redis import client

import portalocker
from portalocker import redis, utils

from . import test_redis as _shared
from .test_redis import ConnectionFactory, _disable_ping_handler, _wait_for


@pytest.fixture(autouse=True)
def set_redis_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same short timeouts ``test_redis.py`` runs with."""
    monkeypatch.setattr(utils, 'DEFAULT_TIMEOUT', 0.0001)
    monkeypatch.setattr(utils, 'DEFAULT_CHECK_INTERVAL', 0.0005)
    monkeypatch.setattr(redis, 'DEFAULT_UNAVAILABLE_TIMEOUT', 0.2)
    monkeypatch.setattr(redis, 'DEFAULT_THREAD_SLEEP_TIME', 0.01)
    monkeypatch.setattr(_thread, 'interrupt_main', lambda: None)


@pytest.fixture(params=['fakeredis', 'live'])
def redis_connection(request: pytest.FixtureRequest) -> ConnectionFactory:
    """A connection factory backed by fakeredis or a live server."""
    if request.param == 'live':
        _shared._ensure_live_redis_available(_shared._LIVE_REDIS)
        return _shared._live_redis_connection

    server: fakeredis.FakeServer = fakeredis.FakeServer()
    return lambda: fakeredis.FakeStrictRedis(
        server=server,
        decode_responses=True,
    )


def _fake_server_pair() -> tuple[client.Redis, client.Redis]:
    """Return a decoding and a raw-bytes client on one fake server."""
    server: fakeredis.FakeServer = fakeredis.FakeServer()
    decoding: client.Redis = fakeredis.FakeStrictRedis(
        server=server,
        decode_responses=True,
    )
    raw: client.Redis = fakeredis.FakeStrictRedis(server=server)
    return decoding, raw


# --- finding 5: undecodable frames ---------------------------------------


def _recording_lock() -> tuple[redis.RedisLock, list[tuple[str, str]]]:
    """Return a lock whose command connection records every publish."""
    decoding, _raw = _fake_server_pair()
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=decoding,
    )
    published: list[tuple[str, str]] = []

    def record_publish(channel: typing.Any, message: typing.Any) -> int:
        published.append((str(channel), str(message)))
        return 0

    setattr(decoding, 'publish', record_publish)  # noqa: B010
    return lock, published


@pytest.mark.parametrize(
    'data',
    ['1' * 4301, '[' * 100_000, 'x' * 100_000],
    ids=['int-limit', 'deep-nesting', 'huge-garbage'],
)
def test_channel_handler_is_total(data: str) -> None:
    """No frame on the lock channel may raise out of the ping handler."""
    lock, published = _recording_lock()

    lock.channel_handler({'type': 'message', 'data': data})

    assert published == []


def test_channel_handler_echoes_only_a_string_nonce() -> None:
    """A ping is answered either way; only a string nonce is echoed."""
    lock, published = _recording_lock()
    with lock._state_lock:
        lock._expected_ping_nonce = 'abc'

    lock.channel_handler(
        {
            'type': 'message',
            'data': json.dumps({'response_channel': 'r', 'nonce': 7}),
        }
    )
    lock.channel_handler(
        {
            'type': 'message',
            'data': json.dumps({'response_channel': 'r', 'nonce': 'abc'}),
        }
    )

    assert [channel for channel, _ in published] == ['r', 'r']
    assert 'nonce' not in json.loads(published[0][1])
    assert json.loads(published[1][1])['nonce'] == 'abc'
    assert lock._seen_ping_nonce == 'abc'

    # A nonce nobody is waiting for is echoed but never recorded.
    lock.channel_handler(
        {
            'type': 'message',
            'data': json.dumps({'response_channel': 'r', 'nonce': 'other'}),
        }
    )
    assert lock._seen_ping_nonce == 'abc'


@pytest.mark.parametrize(
    'frame',
    [b'\xff\xfe', b'1' * 4301, b'[' * 100_000, b'x' * 100_000],
    ids=['non-utf8', 'int-limit', 'deep-nesting', 'huge-garbage'],
)
def test_hostile_frame_does_not_revoke_held_lock(frame: bytes) -> None:
    """A held lock survives any frame another client publishes."""
    decoding, raw = _fake_server_pair()
    lost_calls: list[redis.RedisLock] = []
    channel: str = str(random.random())
    lock: redis.RedisLock = redis.RedisLock(
        channel,
        connection=decoding,
        thread_sleep_time=0.01,
        on_lost=lost_calls.append,
        interrupt_on_lost=False,
    )
    lock.acquire()
    try:
        worker: redis.PubSubWorkerThread | None = lock.thread
        assert worker is not None
        raw.publish(channel, frame)
        # Give the worker a few read cycles to either survive or die.
        time.sleep(0.1)
        assert worker.is_alive(), 'the worker died on a hostile frame'
        assert not lock.lost
        assert lost_calls == []
        lock.ensure_held()
        # Pings are still answered after the hostile frame.
        prober: redis.RedisLock = redis.RedisLock(channel, connection=decoding)
        assert [holder.holder_id for holder in prober.probe()] == [
            lock.holder_id
        ]
    finally:
        lock.release()


def test_parse_lock_response_treats_hostile_reply_as_legacy() -> None:
    """A reply that cannot be parsed safely is a legacy exclusive holder."""
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))

    for data in ('[' * 100_000, '1' * 4301, 'x' * 100_000):
        holder: redis.RedisLockHolder = lock._parse_lock_response(data, 3)
        assert holder.legacy
        assert holder.mode is redis.RedisLockMode.EXCLUSIVE


# --- finding 6: copies ------------------------------------------------------


def test_copy_of_idle_lock_is_a_distinct_idle_holder(
    redis_connection: ConnectionFactory,
) -> None:
    """Copying an idle lock yields an independent lock with its own id."""
    connection: client.Redis = redis_connection()
    original: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=connection,
        flags=portalocker.LockFlags.SHARED,
        fencing=True,
    )

    twin: redis.RedisLock = copy.copy(original)

    assert twin is not original
    assert twin.holder_id != original.holder_id
    assert twin.channel == original.channel
    assert twin.flags == original.flags
    assert twin.fencing
    assert twin.connection is connection
    assert twin._state_lock is not original._state_lock
    assert twin._mode_lock is not original._mode_lock
    # Both can hold the (shared) channel at once, as two holders.
    original.acquire()
    try:
        twin.acquire()
        try:
            assert original._get_subscriber_count(connection) == 2
        finally:
            twin.release()
    finally:
        original.release()


def test_copy_of_held_lock_is_refused_and_original_stays_held(
    redis_connection: ConnectionFactory,
) -> None:
    """A copy of a held lock must not become a second subscription owner."""
    connection: client.Redis = redis_connection()
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=connection,
        interrupt_on_lost=False,
    )
    lock.acquire()
    try:
        with pytest.raises(TypeError):
            copy.copy(lock)
        with pytest.raises(TypeError):
            copy.deepcopy(lock)
        worker: redis.PubSubWorkerThread | None = lock.thread
        assert worker is not None and worker.is_alive()
        assert lock._get_subscriber_count(connection) == 1
        assert not lock.lost
        lock.ensure_held()
    finally:
        lock.release()


def test_lock_cannot_be_pickled() -> None:
    """Pickling a lock raises instead of smuggling a holder identity."""
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))

    with pytest.raises(TypeError):
        pickle.dumps(lock)


# --- finding 9: forged replies ---------------------------------------------


class _ScriptedPubSub:
    """A probe response pubsub that hands out prepared frames."""

    def __init__(self, frames: list[dict[str, typing.Any]]) -> None:
        self._frames: list[dict[str, typing.Any]] = list(frames)

    def subscribe(self, *_channels: str) -> None:
        self._frames.insert(0, {'type': 'subscribe'})

    def get_message(
        self,
        timeout: float = 0,
        ignore_subscribe_messages: bool = False,
    ) -> dict[str, typing.Any] | None:
        del timeout, ignore_subscribe_messages
        if self._frames:
            return self._frames.pop(0)
        return None

    def close(self) -> None:
        return None


def _record(
    holder_id: str, mode: str, elected: bool = False
) -> dict[str, typing.Any]:
    return {
        'type': 'message',
        'data': json.dumps(
            {
                'holder_id': holder_id,
                'mode': mode,
                'protocol': redis.REDIS_LOCK_PROTOCOL_VERSION,
                'elected': elected,
            }
        ),
    }


def _scripted_probe(
    lock: redis.RedisLock,
    monkeypatch: pytest.MonkeyPatch,
    frames: list[dict[str, typing.Any]],
    expected: int,
) -> list[redis.RedisLockHolder] | None:
    """Run ``_collect_lock_holders`` against prepared reply frames."""
    monkeypatch.setattr(
        lock, '_get_pubsub', lambda _c: _ScriptedPubSub(frames)
    )
    monkeypatch.setattr(lock, '_get_subscriber_count', lambda _c: expected)
    connection: typing.Any = lock.get_connection()
    monkeypatch.setattr(connection, 'publish', lambda *_a, **_k: 0)
    return lock._collect_lock_holders(connection, expected, 0.05, reap=False)


def test_probe_with_honest_replies_is_conclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decoding, _raw = _fake_server_pair()
    lock: redis.RedisLock = redis.RedisLock('c', connection=decoding)
    frames = [_record(lock.holder_id, 'pending'), _record('b' * 32, 'shared')]
    holders = _scripted_probe(lock, monkeypatch, frames, 2)

    assert holders is not None
    assert {holder.holder_id for holder in holders} == {
        lock.holder_id,
        'b' * 32,
    }


def test_probe_with_surplus_distinct_ids_is_inconclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """More distinct ids than counted subscribers cannot be attributed."""
    decoding, _raw = _fake_server_pair()
    lock: redis.RedisLock = redis.RedisLock('c', connection=decoding)
    frames = [
        _record('a' * 32, 'shared'),
        _record('b' * 32, 'shared'),
        _record(lock.holder_id, 'pending'),
    ]

    assert _scripted_probe(lock, monkeypatch, frames, 2) is None


def test_probe_with_conflicting_duplicate_is_inconclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two different records for one id are a forgery or churn, not a vote."""
    decoding, _raw = _fake_server_pair()
    lock: redis.RedisLock = redis.RedisLock('c', connection=decoding)
    frames = [
        _record(lock.holder_id, 'pending'),
        _record('b' * 32, 'exclusive'),
        _record('b' * 32, 'shared'),
    ]

    assert _scripted_probe(lock, monkeypatch, frames, 2) is None


def test_probe_with_forged_own_record_is_inconclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply claiming this lock's own id with another mode is forged."""
    decoding, _raw = _fake_server_pair()
    lock: redis.RedisLock = redis.RedisLock('c', connection=decoding)
    frames = [
        _record(lock.holder_id, 'shared'),
        _record('b' * 32, 'shared'),
    ]

    assert _scripted_probe(lock, monkeypatch, frames, 2) is None


def test_probe_with_identical_duplicate_stays_conclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retransmitted identical record is not a conflict."""
    decoding, _raw = _fake_server_pair()
    lock: redis.RedisLock = redis.RedisLock('c', connection=decoding)
    frames = [
        _record(lock.holder_id, 'pending'),
        _record('b' * 32, 'shared'),
        _record('b' * 32, 'shared'),
    ]

    assert _scripted_probe(lock, monkeypatch, frames, 2) is not None


# --- finding 8: silent foreign subscriber ----------------------------------


def test_silent_foreign_subscriber_is_reported_once_per_acquire(
    redis_connection: ConnectionFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A short probe the reaper cannot explain logs one warning."""
    channel: str = str(random.random())
    foreign: client.PubSub = typing.cast(
        'client.PubSub',
        redis_connection().pubsub(),  # type: ignore[no-untyped-call]
    )
    foreign.subscribe(channel)
    assert _wait_for(
        lambda: foreign.get_message(timeout=0.01) is not None,
        timeout=2.0,
    )
    lock: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        thread_sleep_time=0.01,
        unavailable_timeout=0.05,
        check_interval=0.01,
    )
    try:
        with (
            caplog.at_level(logging.WARNING, logger='portalocker.redis'),
            pytest.raises(portalocker.AlreadyLocked),
        ):
            lock.acquire(timeout=0.3)
    finally:
        foreign.close()
    warnings_ = [
        record
        for record in caplog.records
        if 'unidentified subscriber' in record.getMessage()
    ]
    assert len(warnings_) == 1
    assert channel in warnings_[0].getMessage()


# --- finding 10: self-check nonce ------------------------------------------


def test_self_check_rejects_forged_echo(
    redis_connection: ConnectionFactory,
) -> None:
    """A forged reply carrying the public holder_id must not pass the check.

    The holder's own handler is silenced, and a forger that watches
    every response channel answers each ping with this holder's public
    id. Only an echo the held subscription itself delivered counts, so
    the check must still fail and the lock must be lost.
    """
    channel: str = str(random.random())
    lost_calls: list[redis.RedisLock] = []
    lock: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        thread_sleep_time=0.01,
        unavailable_timeout=0.5,
        self_check_interval=0.1,
        on_lost=lost_calls.append,
        interrupt_on_lost=False,
    )
    forger_connection: client.Redis = redis_connection()
    forger: client.PubSub = typing.cast(
        'client.PubSub',
        forger_connection.pubsub(),  # type: ignore[no-untyped-call]
    )
    forger.psubscribe(f'{channel}')
    stop = threading.Event()

    def forge() -> None:
        while not stop.is_set():
            message: typing.Any = forger.get_message(timeout=0.01)
            if not message or message.get('type') != 'pmessage':
                continue
            try:
                ping: typing.Any = json.loads(message['data'])
                response_channel: typing.Any = ping.get('response_channel')
            except (ValueError, AttributeError):
                continue
            if isinstance(response_channel, str):
                forger_connection.publish(
                    response_channel,
                    json.dumps(
                        {
                            'holder_id': lock.holder_id,
                            'mode': 'exclusive',
                            'protocol': redis.REDIS_LOCK_PROTOCOL_VERSION,
                            'elected': True,
                            'nonce': ping.get('nonce'),
                        }
                    ),
                )

    thread = threading.Thread(target=forge, daemon=True)
    thread.start()
    lock.acquire()
    try:
        worker: redis.PubSubWorkerThread | None = lock.thread
        assert worker is not None
        _disable_ping_handler(lock)
        assert _wait_for(lambda: not worker.is_alive(), timeout=5.0), (
            'the forged echo passed the self-check'
        )
        assert lock.lost
        assert lost_calls == [lock]
    finally:
        stop.set()
        thread.join(timeout=2)
        forger.close()
        lock.release()


# --- finding 4: fence token liveness ---------------------------------------


def test_fenced_grant_is_abandoned_when_subscription_dies_at_incr(
    redis_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token drawn by a holder whose subscription is dead is never used.

    The subscription's handler is silenced the moment the first INCR
    runs, so that grant can no longer prove its subscription is live.
    The attempt must be abandoned (token 1 burned) and the retry's grant
    carries the next token.
    """
    connection: client.Redis = redis_connection()
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=connection,
        thread_sleep_time=0.01,
        unavailable_timeout=0.3,
        check_interval=0.01,
        fencing=True,
        interrupt_on_lost=False,
    )
    original_incr: typing.Callable[..., typing.Any] = connection.incr
    incr_calls: list[int] = []

    def incr_then_silence(name: str, amount: int = 1) -> typing.Any:
        token: typing.Any = original_incr(name, amount)
        incr_calls.append(int(token))
        if len(incr_calls) == 1:
            _disable_ping_handler(lock)
        return token

    monkeypatch.setattr(connection, 'incr', incr_then_silence)

    lock.acquire(timeout=5)
    try:
        assert incr_calls[0] == 1
        assert lock.fence_token == incr_calls[-1]
        assert lock.fence_token is not None and lock.fence_token >= 2
    finally:
        lock.release()


# --- finding 17: forked child identity --------------------------------------


@pytest.mark.skipif(os.name == 'nt', reason='os.fork is POSIX-only')
def test_forked_child_gets_its_own_holder_id() -> None:
    """A forked child is a distinct holder on the wire."""
    decoding, _raw = _fake_server_pair()
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=decoding,
        interrupt_on_lost=False,
    )
    parent_id: str = lock.holder_id
    read_fd, write_fd = os.pipe()
    pid: int = os.fork()
    if pid == 0:  # pragma: no cover - child process, exits via os._exit
        os.close(read_fd)
        os.write(write_fd, lock.holder_id.encode())
        os._exit(0)
    os.close(write_fd)
    child_id: str = os.read(read_fd, 64).decode()
    os.close(read_fd)
    _waited, status = os.waitpid(pid, 0)

    assert os.waitstatus_to_exitcode(status) == 0
    assert len(child_id) == 32
    assert child_id != parent_id
    assert lock.holder_id == parent_id


def test_copy_of_lock_with_own_connection_does_not_share_it() -> None:
    """A twin never closes a connection the original created."""
    original: redis.RedisLock = redis.RedisLock(str(random.random()))

    twin: redis.RedisLock = copy.copy(original)

    assert original.close_connection
    assert twin.connection is None


def test_after_fork_hook_reallocates_holder_id() -> None:
    """The fork hook gives every live lock a fresh holder identity."""
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))
    before: str = lock.holder_id

    utils._reinit_state_locks_after_fork()

    assert lock.holder_id != before
    assert len(lock.holder_id) == 32


def test_fenced_grant_confirm_abandons_when_the_lock_leaves_acquiring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A release racing the fence confirm abandons the attempt quietly."""
    decoding, _raw = _fake_server_pair()
    lock: redis.RedisLock = redis.RedisLock(
        str(random.random()),
        connection=decoding,
        fencing=True,
    )

    def abandoned(*_args: typing.Any, **_kwargs: typing.Any) -> bool:
        raise redis._SelfCheckAbandoned

    monkeypatch.setattr(lock, '_await_own_echo', abandoned)

    assert lock._confirm_fenced_grant(decoding) is False


# --- review follow-ups: nonce clobbering, drain cap, response decoding -----


def _forger(
    connection_factory: ConnectionFactory,
    channel: str,
    reply: typing.Callable[[dict[str, typing.Any]], bytes | str | None],
) -> tuple[threading.Thread, threading.Event, client.PubSub]:
    """Start a thread that answers every ping on ``channel`` with ``reply``.

    ``reply`` receives the decoded ping and returns the payload to
    publish on its response channel, or ``None`` to publish nothing.
    """
    forger_connection: client.Redis = connection_factory()
    raw_connection: typing.Any = connection_factory()
    forger: client.PubSub = typing.cast(
        'client.PubSub',
        forger_connection.pubsub(),  # type: ignore[no-untyped-call]
    )
    forger.subscribe(channel)
    stop = threading.Event()

    def run() -> None:
        while not stop.is_set():
            message: typing.Any = forger.get_message(timeout=0.01)
            if not message or message.get('type') != 'message':
                continue
            ping: dict[str, typing.Any] | None = redis._decode_frame(
                message.get('data'),
            )
            if ping is None:
                continue
            response_channel: typing.Any = ping.get('response_channel')
            payload: bytes | str | None = reply(ping)
            if isinstance(response_channel, str) and payload is not None:
                raw_connection.publish(response_channel, payload)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, stop, forger


def test_self_check_survives_a_flood_of_foreign_nonces(
    redis_connection: ConnectionFactory,
) -> None:
    """Pings with other nonces must not erase the check's own evidence."""
    channel: str = str(random.random())
    lock: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        thread_sleep_time=0.01,
        unavailable_timeout=0.5,
        self_check_interval=0.2,
        interrupt_on_lost=False,
    )
    noise: client.Redis = redis_connection()
    stop = threading.Event()
    sent: list[int] = []

    def flood() -> None:
        while not stop.is_set():
            noise.publish(
                channel,
                json.dumps(
                    {
                        'message': 'ping',
                        'response_channel': f'{channel}-noise',
                        'nonce': os.urandom(16).hex(),
                    }
                ),
            )
            sent.append(1)
            time.sleep(0.005)

    thread = threading.Thread(target=flood, daemon=True)
    lock.acquire()
    try:
        thread.start()
        time.sleep(1.0)
        assert len(sent) > 20, 'the flood never ran'
        assert not lock.lost
        lock.ensure_held()
        worker: redis.PubSubWorkerThread | None = lock.thread
        assert worker is not None and worker.is_alive()
    finally:
        stop.set()
        thread.join(timeout=2)
        lock.release()


def test_probe_flood_past_the_drain_cap_is_inconclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flood of duplicates cannot hide a forged surplus behind the cap."""
    decoding, _raw = _fake_server_pair()
    lock: redis.RedisLock = redis.RedisLock('c', connection=decoding)
    frames = [_record(lock.holder_id, 'pending'), _record('b' * 32, 'shared')]
    frames += [_record('b' * 32, 'shared')] * (redis._SURPLUS_DRAIN_FRAMES + 5)
    frames.append(_record('c' * 32, 'shared'))

    assert _scripted_probe(lock, monkeypatch, frames, 2) is None


def test_probe_survives_undecodable_frames_on_its_response_channel(
    redis_connection: ConnectionFactory,
) -> None:
    """Garbage bytes on a response channel do not abort the acquire."""
    channel: str = str(random.random())
    holder: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        thread_sleep_time=0.01,
        interrupt_on_lost=False,
    )
    holder.acquire()
    thread, stop, forger = _forger(
        redis_connection, channel, lambda _ping: b'\xff\xfe\xfd'
    )
    contender: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        thread_sleep_time=0.01,
        unavailable_timeout=0.3,
    )
    try:
        with pytest.raises(portalocker.AlreadyLocked):
            contender.acquire(timeout=0.5, fail_when_locked=True)
        assert not holder.lost
    finally:
        stop.set()
        thread.join(timeout=2)
        forger.close()
        contender.release()
        holder.release()


def test_self_check_survives_undecodable_frames_on_its_response_channel(
    redis_connection: ConnectionFactory,
) -> None:
    """Garbage bytes on the self-check's response channel are not a loss."""
    channel: str = str(random.random())
    lock: redis.RedisLock = redis.RedisLock(
        channel,
        connection=redis_connection(),
        thread_sleep_time=0.01,
        unavailable_timeout=0.5,
        self_check_interval=0.05,
        interrupt_on_lost=False,
    )
    lock.acquire()
    thread, stop, forger = _forger(
        redis_connection, channel, lambda _ping: b'\xff\xfe\xfd'
    )
    try:
        time.sleep(0.5)
        assert not lock.lost
        lock.ensure_held()
    finally:
        stop.set()
        thread.join(timeout=2)
        forger.close()
        lock.release()


def test_strict_factory_client_survives_a_non_utf8_frame() -> None:
    """A strictly decoding factory client drops the frame instead of dying."""
    server: fakeredis.FakeServer = fakeredis.FakeServer()
    channel: str = str(random.random())
    lock: redis.RedisLock = redis.RedisLock(
        channel,
        connection=fakeredis.FakeStrictRedis(
            server=server, decode_responses=True
        ),
        thread_sleep_time=0.01,
        interrupt_on_lost=False,
        subscription_connection_factory=lambda: fakeredis.FakeStrictRedis(
            server=server, decode_responses=True
        ),
    )
    raw: client.Redis = fakeredis.FakeStrictRedis(server=server)
    lock.acquire()
    try:
        raw.publish(channel, b'\xff\xfe')
        time.sleep(0.1)
        assert not lock.lost
        worker: redis.PubSubWorkerThread | None = lock.thread
        assert worker is not None and worker.is_alive()
    finally:
        lock.release()


def test_held_subscription_service_is_capped_per_poll() -> None:
    """A held subscription that never empties is serviced a bounded amount.

    Without the cap a flood on the lock channel would keep the check
    reading the held side forever and never poll its response channel.
    """
    lock: redis.RedisLock = redis.RedisLock(str(random.random()))
    with lock._state_lock:
        lock._lock_state = redis._LockState.HELD
    response: _ScriptedPubSub = _ScriptedPubSub([])
    reads: list[int] = []

    class EndlessHeld:
        def get_message(
            self,
            timeout: float = 0.0,
            ignore_subscribe_messages: bool = False,
        ) -> dict[str, typing.Any]:
            reads.append(1)
            return {'type': 'message', 'data': 'noise'}

    deadline: float = time.monotonic() + 0.05
    replied: bool = lock._await_self_check_frame(
        typing.cast('client.PubSub', response),
        typing.cast('client.PubSub', EndlessHeld()),
        deadline,
        0.001,
        lambda _frame: False,
    )
    with lock._state_lock:
        lock._lock_state = redis._LockState.IDLE

    assert replied is False
    assert len(reads) % redis._HELD_SERVICE_FRAMES == 0
    assert len(reads) >= redis._HELD_SERVICE_FRAMES
