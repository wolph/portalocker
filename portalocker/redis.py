# Several redis-py methods (`pubsub`, `unsubscribe`, `client_list`, ...) are
# unannotated or take untyped `**kwargs`, so their types are (partially)
# unknown to pyright.
# pyright: reportUnknownMemberType=false
"""Distributed locking through Redis pubsub instead of expiring keys.

The usual way to build a Redis lock is a key with a time to live: the
holder writes ``SET <name> <token> NX PX <ttl>`` and keeps refreshing it.
That design has an awkward failure mode. When a holder crashes, its
network drops, or its machine is powered off, the key outlives it and
every other process waits out the remaining TTL even though the holder is
provably gone. Shortening the TTL narrows that window but risks a merely
slow holder losing a lock it still believes it owns.

`RedisLock` keeps the lock in a *subscription* rather than in a key. A
holder subscribes to the lock channel and a background thread keeps
reading from it, so ownership is a property of a live connection. If the
process dies the socket closes, Redis drops the subscriber, and the lock
is released at once: there is no expiry to wait out and no heartbeat to
refresh. The price is that nothing is stored anywhere, so a lock attempt
has to ask the channel who is currently there.

Asking is a ping/pong on the channel itself::

    acquire()
      |
      +- subscribe to the channel, naming the connection `client_name`
      +- count subscribers; being the only one means the lock is free
      +- otherwise publish a ping carrying a private response channel,
      |  which every subscriber answers with a record of its holder id
      |  and its current mode (see `RedisLockHolder`)
      +- decide (see `RedisLock._resolve_lock_holders`):
           compatible holders     -> join them, the lock is held
           elected, no readers    -> take the lock exclusively
           anything else          -> unsubscribe and retry

Shared readers hold the lock together, an exclusive writer holds it
alone. There is no coordinator and no lock to take before taking the
lock, so competing writers agree on a single winner by sorting the
pending holder ids they all saw; see `RedisLock._writer_is_elected`.
Subscribers that Redis still counts but that stopped answering are
crashed processes, and their connections are killed so that the channel
becomes consistent again.

Set ``health_check_interval`` on the connection (it is part of
`RedisLock.DEFAULT_REDIS_KWARGS`) so that both sides notice a dead peer
promptly.

Example:
    >>> import fakeredis
    >>> import portalocker
    >>> connection = fakeredis.FakeStrictRedis(
    ...     server=fakeredis.FakeServer(), decode_responses=True
    ... )
    >>> lock = portalocker.RedisLock('some_channel', connection=connection)
    >>> with lock:
    ...     print('do something here')
    do something here
"""

from __future__ import annotations

import _thread
import enum
import json
import logging
import random
import time
import typing
import uuid

import redis.client

from . import constants, exceptions, utils

logger = logging.getLogger(__name__)

#: Seconds a probe waits for the other subscribers to answer before the
#: silent ones are treated as crashed and their connections are killed.
DEFAULT_UNAVAILABLE_TIMEOUT = 1
#: Seconds the keep-alive thread sleeps between reads. It doubles as the
#: fallback retry interval when `check_interval` is zero or negative.
DEFAULT_THREAD_SLEEP_TIME = 0.1
#: Version stamped into every holder record. A reply that does not carry
#: exactly this version is treated as coming from an older portalocker.
REDIS_LOCK_PROTOCOL_VERSION = 1


class RedisLockMode(str, enum.Enum):
    """What a participant claims to be doing with the lock.

    The member values are the strings that travel in the ``mode`` field of
    a holder record, which is why this is a `str` subclass: a member
    compares equal to its own wire value.

    `PENDING` is the member that makes the protocol work. An exclusive
    waiter announces itself on the channel before it owns anything, which
    is what lets competing writers elect a single winner among themselves
    and what stops an endless stream of readers from starving a writer.
    """

    #: The holder owns the lock alone. Nobody else may join.
    EXCLUSIVE = 'exclusive'
    #: The holder wants the lock exclusively but does not have it yet. It
    #: is competing in the election and blocking new shared holders.
    PENDING = 'pending'
    #: The holder is a reader. Any number of shared holders may hold the
    #: lock at the same time.
    SHARED = 'shared'


class RedisLockHolder(typing.NamedTuple):
    """One participant's answer to a liveness ping.

    A probe collects one of these per subscriber on the lock channel and
    the whole acquisition decision is made from the resulting list.

    Example:
        >>> from portalocker import redis
        >>> holder = redis.RedisLockHolder('a1b2', redis.RedisLockMode.SHARED)
        >>> holder.holder_id, holder.mode.value, holder.legacy
        ('a1b2', 'shared', False)
    """

    #: The answering lock's `RedisLock.holder_id`, or a synthetic
    #: ``legacy-<n>`` id for a reply that carried no identity.
    holder_id: str
    #: The mode the holder advertised at the moment it answered.
    mode: RedisLockMode
    #: True when the reply could not be read as a protocol record, so its
    #: contents were assumed rather than parsed. Such a holder is always
    #: reported as `RedisLockMode.EXCLUSIVE`, because a portalocker
    #: release old enough not to speak the protocol has no notion of
    #: shared locks and must block everyone.
    legacy: bool = False


class PubSubWorkerThread(redis.client.PubSubWorkerThread):
    """redis-py's pubsub reader thread, with failures escalated to main.

    The subscription this thread services *is* the lock. While it runs,
    the holder answers liveness pings and Redis keeps counting it as a
    subscriber. If the thread were to die quietly - a dropped connection,
    a protocol error - the owning process would carry on believing it
    still holds a lock that every other process now considers released,
    which is exactly the split-brain this lock exists to avoid.

    Interrupting the main thread turns that silent divergence into a loud
    failure the process cannot miss.
    """

    def run(self) -> None:
        """Read from the subscription, interrupting main on failure.

        Raises:
            Exception: Whatever the underlying reader raised, re-raised
                after `_thread.interrupt_main` has queued a
                `KeyboardInterrupt` in the main thread. Re-raising only
                ends this worker thread; the queued interrupt is what the
                rest of the process actually sees.
        """
        try:
            super().run()
        except Exception:  # pragma: no cover
            _thread.interrupt_main()
            raise


class RedisLock(utils.LockBase['RedisLock']):
    """An extremely reliable Redis lock based on pubsub.

    The lock is held by a subscription kept open by a keep-alive thread.

    As opposed to most Redis locking systems based on key/value pairs,
    this locking method is based on the pubsub system. The big advantage is
    that if the connection gets killed due to network issues, crashing
    processes or otherwise, it will still immediately unlock instead of
    waiting for a lock timeout.

    To make sure both sides of the lock know about the connection state it is
    recommended to set the `health_check_interval` when creating the redis
    connection.

    Args:
        channel: the redis channel to use as locking key.
        connection: an optional redis connection if you already have one
            or if you need to specify the redis connection. A connection
            given here is never closed by the lock; one created by the
            lock itself is closed on release.
        timeout: timeout when trying to acquire a lock
        check_interval: check interval while waiting
        fail_when_locked: after the initial lock failed, return an error
            or lock the file. This does not wait for the timeout.
        thread_sleep_time: sleep time between fetching messages from redis to
            prevent a busy/wait loop. In the case of lock conflicts this
            increases the time it takes to resolve the conflict. This should
            be smaller than the `check_interval` to be useful.
        unavailable_timeout: If the conflicting lock is properly connected
            this should never exceed twice your redis latency. Note that this
            will increase the wait time possibly beyond your `timeout` and is
            always executed if a conflict arises.
        redis_kwargs: The redis connection arguments if no connection is
            given. The `DEFAULT_REDIS_KWARGS` are used as default, if you want
            to override these you need to explicitly specify a value (e.g.
            `health_check_interval=0`)
        flags: `LockFlags.EXCLUSIVE` (the default) or `LockFlags.SHARED`.
            Shared holders may coexist, while an exclusive holder waits for
            all shared holders to release. Other flag combinations are
            rejected; use `fail_when_locked` for non-blocking acquisition.

    Example:
        Two readers can hold the same channel at the same time, while a
        writer would have to wait for both of them:

        >>> import fakeredis
        >>> import portalocker
        >>> connection = fakeredis.FakeStrictRedis(
        ...     server=fakeredis.FakeServer(), decode_responses=True
        ... )
        >>> reader = portalocker.RedisLock(
        ...     'shared_channel',
        ...     connection=connection,
        ...     flags=portalocker.LockFlags.SHARED,
        ... )
        >>> other_reader = portalocker.RedisLock(
        ...     'shared_channel',
        ...     connection=connection,
        ...     flags=portalocker.LockFlags.SHARED,
        ... )
        >>> with reader, other_reader:
        ...     print('both readers are in')
        both readers are in
    """

    redis_kwargs: dict[str, typing.Any]
    thread: PubSubWorkerThread | None
    channel: str
    timeout: float
    connection: redis.client.Redis | None
    pubsub: redis.client.PubSub | None = None
    close_connection: bool
    flags: constants.LockFlags
    holder_id: str
    mode: RedisLockMode
    writer_elected: bool

    DEFAULT_REDIS_KWARGS: typing.ClassVar[dict[str, typing.Any]] = dict(
        health_check_interval=10,
        decode_responses=True,
    )

    def __init__(
        self,
        channel: str,
        connection: redis.client.Redis | None = None,
        timeout: float | None = None,
        check_interval: float | None = None,
        fail_when_locked: bool | None = False,
        thread_sleep_time: float = DEFAULT_THREAD_SLEEP_TIME,
        unavailable_timeout: float = DEFAULT_UNAVAILABLE_TIMEOUT,
        redis_kwargs: dict[str, typing.Any] | None = None,
        flags: constants.LockFlags = constants.LockFlags.EXCLUSIVE,
    ) -> None:
        """Configure the lock without touching Redis.

        Nothing is connected, subscribed or published here. The
        constructor only records the configuration, allocates the
        `holder_id` that identifies this instance on the wire, and picks
        the starting `mode`: `RedisLockMode.SHARED` for a reader, or
        `RedisLockMode.PENDING` for a writer, which only becomes
        `RedisLockMode.EXCLUSIVE` once `acquire` succeeds. The first
        contact with Redis happens in `acquire`.

        Every instance gets its own random `holder_id`, so two locks in
        the same process are two independent holders and neither can
        satisfy the other's lock.

        See the class docstring for the argument reference.

        Raises:
            ValueError: `flags` was neither exactly
                `constants.LockFlags.EXCLUSIVE` nor exactly
                `constants.LockFlags.SHARED`. Combinations such as
                ``EXCLUSIVE | NON_BLOCKING`` have no meaning here; use
                `fail_when_locked` for non-blocking behaviour.
        """
        # We don't want to close connections given as an argument
        self.close_connection = not connection

        self.thread = None
        self.channel = channel
        self.connection = connection
        self.thread_sleep_time = thread_sleep_time
        self.unavailable_timeout = unavailable_timeout
        self.redis_kwargs = redis_kwargs or dict()
        if flags not in (
            constants.LockFlags.EXCLUSIVE,
            constants.LockFlags.SHARED,
        ):
            raise ValueError(
                'RedisLock flags must contain exactly one of '
                'LockFlags.EXCLUSIVE or LockFlags.SHARED'
            )
        self.flags = flags
        self.holder_id = uuid.uuid4().hex
        self.writer_elected = False
        self.mode = (
            RedisLockMode.SHARED
            if flags == constants.LockFlags.SHARED
            else RedisLockMode.PENDING
        )

        for key, value in self.DEFAULT_REDIS_KWARGS.items():
            self.redis_kwargs.setdefault(key, value)

        super().__init__(
            timeout=timeout,
            check_interval=check_interval,
            fail_when_locked=fail_when_locked,
        )

    def get_connection(self) -> redis.client.Redis:
        """Return the Redis connection, creating one on first use.

        A connection handed to the constructor is returned unchanged and
        is never closed by this class; the caller owns it. A connection
        created here is owned by the lock, is built from `redis_kwargs`
        (with `DEFAULT_REDIS_KWARGS` filled in), and is closed by
        `release`, after which the next call creates a fresh one.

        Returns:
            The connection every command from this lock is issued on.
        """
        if not self.connection:
            self.connection = redis.client.Redis(**self.redis_kwargs)

        return self.connection

    def _get_pubsub(
        self,
        connection: redis.client.Redis,
    ) -> redis.client.PubSub:
        """Typed wrapper, `Redis.pubsub()` is unannotated in redis-py."""
        return typing.cast(
            'redis.client.PubSub',
            connection.pubsub(),  # type: ignore[no-untyped-call]
        )

    def _get_subscriber_count(self, connection: redis.client.Redis) -> int:
        """Get the subscriber count for our channel."""
        return connection.pubsub_numsub(self.channel)[0][1]

    def channel_handler(self, message: dict[str, str]) -> None:
        """Answer a liveness ping with this holder's record.

        Registered as the subscription callback in `_start_subscription`,
        so it runs on the `PubSubWorkerThread` for every message
        published to `channel`. Anything that is not a JSON object with a
        non-empty ``response_channel`` string is ignored, which keeps
        unrelated traffic on the channel harmless.

        The reply is published on the private response channel the prober
        asked for and carries `holder_id`, the *current* `mode` and
        `REDIS_LOCK_PROTOCOL_VERSION`. Answering with the live mode
        rather than a stored one is what makes the protocol truthful: a
        writer that is still `RedisLockMode.PENDING` says so, and a probe
        therefore learns the state as it was at the moment it asked.

        A probing lock is subscribed to its own channel, so it answers
        its own ping and appears in its own holder list.

        Args:
            message: A redis-py pubsub message. Only messages of type
                ``message`` are answered; subscribe confirmations and
                other control frames are dropped.
        """
        if message.get('type') != 'message':  # pragma: no cover
            return

        raw_data: str | None = message.get('data')
        if not raw_data:
            return

        try:
            data: typing.Any = json.loads(raw_data)
        except (json.JSONDecodeError, TypeError):
            logger.debug('Invalid Redis lock message: %r', message)
            return
        if not isinstance(data, dict):
            return
        data_dict: dict[str, typing.Any] = typing.cast(
            'dict[str, typing.Any]',
            data,
        )
        response_channel: typing.Any = data_dict.get('response_channel')
        if not isinstance(response_channel, str) or not response_channel:
            return

        assert self.connection is not None
        self.connection.publish(
            response_channel,
            json.dumps(
                {
                    'holder_id': self.holder_id,
                    'mode': self.mode.value,
                    'protocol': REDIS_LOCK_PROTOCOL_VERSION,
                }
            ),
        )

    @property
    def client_name(self) -> str:
        """Name given to this holder's subscriber connection.

        `_start_subscription` sends ``CLIENT SETNAME`` over the pubsub
        connection itself, so the name lands on the connection that
        actually holds the subscription and shows up against it in
        ``CLIENT LIST``. `_kill_unavailable_locks` reads it back the
        other way around: a listed connection whose name carries a
        `holder_id` that did not answer the last ping belongs to a
        crashed holder, and killing it releases the lock.

        Returns:
            `legacy_client_name` with this instance's `holder_id`
            appended.

        Example:
            >>> from portalocker import redis
            >>> lock = redis.RedisLock('some_channel')
            >>> lock.client_name == f'some_channel-lock-{lock.holder_id}'
            True
        """
        return f'{self.legacy_client_name}-{self.holder_id}'

    @property
    def legacy_client_name(self) -> str:
        """Connection name used by portalocker 3.2.0 and older.

        Up to and including portalocker 3.2.0 every holder on a channel
        named its connection ``<channel>-lock``, with no per-holder
        suffix, and answered a ping with a bare timestamp string instead
        of a record. Shared locks need holders to be individually
        identifiable, so 4.0.0 appended `holder_id` to the name (see
        `client_name`) and replaced the timestamp with a JSON record.

        The old name is still recognised so that a 4.0.0 holder sharing a
        channel with an older one stays correct: an old reply is recorded
        as a single legacy `RedisLockMode.EXCLUSIVE` holder, which blocks
        readers and writers alike, and an old connection that stops
        answering is still reaped by name.

        Returns:
            The unsuffixed ``<channel>-lock`` name.

        Example:
            >>> from portalocker import redis
            >>> redis.RedisLock('some_channel').legacy_client_name
            'some_channel-lock'
        """
        return f'{self.channel}-lock'

    def _timeout_generator(
        self, timeout: float | None, check_interval: float | None
    ) -> typing.Iterator[int]:
        """Yield once per attempt until the deadline passes.

        Overrides `utils.LockBase._timeout_generator` because a Redis
        retry loop has different needs from a filesystem one:

        - Every interval is scaled by a random factor in ``[0.5, 1.5)``.
          Contenders that started together would otherwise retry in
          lockstep and keep colliding round after round; the jitter
          spreads them out so one of them gets a clean probe.
        - The sleep happens *before* the yield, so a caller waits out an
          interval before its first attempt as well as between attempts.
          The loops driven by this generator poll a subscription, and
          polling one that was created microseconds ago only wastes a
          round trip.
        - The deadline is taken from `time.monotonic`, so adjusting the
          system clock mid-wait cannot stretch or cut short a timeout.

        Like the base class it always yields at least once, even with a
        zero or negative timeout, so ``fail_when_locked`` still makes one
        real attempt rather than failing without asking.

        Args:
            timeout: Seconds to keep yielding for. `None` means zero,
                which still yields exactly once.
            check_interval: Base seconds to sleep before each attempt.
                `None` or a non-positive value falls back to
                `thread_sleep_time`.

        Yields:
            A constant ``0``. Unlike the base class this is not an
            attempt counter; callers iterate for the timing alone.
        """
        if timeout is None:
            timeout = 0.0
        if check_interval is None:
            check_interval = self.thread_sleep_time
        deadline = time.monotonic() + timeout
        first = True
        while first or time.monotonic() < deadline:
            first = False
            effective_interval = (
                check_interval
                if check_interval > 0
                else self.thread_sleep_time
            )
            sleep_time = effective_interval * (0.5 + random.random())
            time.sleep(sleep_time)
            yield 0

    def _start_subscription(
        self,
        connection: redis.client.Redis,
    ) -> None:
        """Subscribe to the lock channel and start the keep-alive thread.

        This is where the lock comes into existence. Once the
        subscription is live this process is counted by ``PUBSUB
        NUMSUB``, answers pings through `channel_handler`, and stays a
        holder for exactly as long as the connection survives.

        The order of operations matters:

        1. ``CLIENT SETNAME`` is sent over the pubsub connection rather
           than the command connection, so the name identifies the
           connection that actually holds the subscription - the one
           `_kill_unavailable_locks` looks for in ``CLIENT LIST``. Its
           reply is consumed with ``parse_response`` so it cannot later
           be mistaken for a pubsub message.
        2. The subscription is registered with `channel_handler` as its
           callback, so pings are answered from now on.
        3. A `PubSubWorkerThread` starts reading. It is a daemon thread:
           an unreleased lock must never keep the interpreter alive, and
           since losing the connection *is* releasing the lock, dying at
           process exit is the correct behaviour rather than a leak.

        Any failure rolls the whole thing back through `release` before
        re-raising, leaving `pubsub` as `None`. Without that rollback a
        failed `acquire` would leave half a subscription behind and the
        ``assert not self.pubsub`` at the top of `acquire` would refuse
        every later retry on the same object.

        Args:
            connection: The connection to subscribe on.

        Raises:
            Exception: Anything the Redis client raises while naming,
                subscribing or starting the thread, re-raised unchanged
                after the rollback.
        """
        pubsub: redis.client.PubSub = self._get_pubsub(connection)
        self.pubsub = pubsub
        try:
            pubsub.execute_command(  # type: ignore[no-untyped-call]
                'CLIENT',
                'SETNAME',
                self.client_name,
            )
            pubsub.parse_response()  # type: ignore[no-untyped-call]
            pubsub.subscribe(**{self.channel: self.channel_handler})
            # A daemon thread so an unreleased lock can never block
            # interpreter exit; losing the connection releases the lock by
            # design, which is exactly what process exit should do.
            self.thread = PubSubWorkerThread(
                pubsub,
                sleep_time=self.thread_sleep_time,
                daemon=True,
            )
            self.thread.start()
            time.sleep(0.01)
        except Exception:
            self.release()
            raise

    def _parse_lock_response(
        self,
        raw_data: typing.Any,
        legacy_index: int,
    ) -> RedisLockHolder:
        """Turn one raw ping reply into a `RedisLockHolder`.

        A reply is accepted as a protocol record only if it decodes to a
        mapping whose ``protocol`` equals `REDIS_LOCK_PROTOCOL_VERSION`
        and whose ``holder_id`` and ``mode`` are both strings, with
        ``mode`` naming a known `RedisLockMode`. Everything else - a bare
        timestamp from portalocker 3.2.0, a truncated payload, a mode
        some future version invents - falls back to a synthetic legacy
        holder.

        That fallback is deliberately pessimistic. It reports
        `RedisLockMode.EXCLUSIVE`, so anything unrecognised blocks
        readers and writers alike. Assuming `RedisLockMode.SHARED` for a
        participant whose intentions could not be read would hand out
        overlapping locks, which is the one outcome a lock may never
        produce.

        Legacy replies carry no identity, so they cannot be de-duplicated
        by holder id the way protocol replies are. `legacy_index` gives
        each one a distinct ``legacy-<n>`` id, so two silent old holders
        count as two holders instead of collapsing into one and leaving
        the caller short of the subscriber count it expected.

        Args:
            raw_data: The message payload exactly as received, normally a
                JSON string but not required to be anything in
                particular.
            legacy_index: How many legacy replies this probe has already
                seen. Used only to build a unique fallback holder id.

        Returns:
            The parsed holder, or a legacy exclusive holder when the
            reply could not be interpreted.
        """
        try:
            data: typing.Any = json.loads(raw_data)
            holder_id: typing.Any = data.get('holder_id')
            mode: typing.Any = data.get('mode')
            protocol: typing.Any = data.get('protocol')
            if (
                protocol == REDIS_LOCK_PROTOCOL_VERSION
                and isinstance(holder_id, str)
                and isinstance(mode, str)
            ):
                return RedisLockHolder(
                    holder_id=holder_id,
                    mode=RedisLockMode(mode),
                )
        except (AttributeError, TypeError, ValueError):
            pass

        return RedisLockHolder(
            holder_id=f'legacy-{legacy_index}',
            mode=RedisLockMode.EXCLUSIVE,
            legacy=True,
        )

    def _kill_unavailable_locks(
        self,
        connection: redis.client.Redis,
        responding_holders: typing.Iterable[RedisLockHolder],
    ) -> None:
        """Kill the connections of holders that did not answer the ping.

        Called only when a probe collected fewer replies than there are
        subscribers on the channel. A subscriber Redis still counts but
        that no longer answers is a crashed or wedged process; because
        ownership lives in the connection, killing that connection is
        precisely what releases its lock, and the next probe then sees a
        consistent channel again.

        Every entry in ``CLIENT LIST`` is matched against the two naming
        schemes this package has used:

        - exactly `legacy_client_name`: a portalocker 3.2.0 or older
          holder. Killed unless some legacy reply arrived in this probe,
          since old holders are indistinguishable from one another.
        - `legacy_client_name` plus a `holder_id` suffix: a current
          holder. Killed unless that exact holder id replied.

        This lock names its own connection the same way, so the caller
        must include its own reply in `responding_holders` or it will
        kill itself. It does: a probing lock is subscribed to its own
        channel and answers its own ping.

        Args:
            connection: The connection to run ``CLIENT LIST`` and
                ``CLIENT KILL`` on.
            responding_holders: The holders that did answer this probe.
                Anything named like a holder of this channel but absent
                from this list is considered crashed.
        """
        holders: list[RedisLockHolder] = list(responding_holders)
        responding_holder_ids: set[str] = {
            holder.holder_id for holder in holders if not holder.legacy
        }
        legacy_responded: bool = any(holder.legacy for holder in holders)
        client_name_prefix: str = f'{self.legacy_client_name}-'
        clients: list[dict[str, str]] = connection.client_list()
        for client_ in clients:
            client_name: str = client_.get('name', '')
            unavailable: bool = (
                client_name == self.legacy_client_name and not legacy_responded
            ) or (
                client_name.startswith(client_name_prefix)
                and client_name.removeprefix(client_name_prefix)
                not in responding_holder_ids
            )
            if unavailable:
                logger.warning(
                    'Killing unavailable redis client: %r',
                    client_,
                )
                connection.client_kill_filter(client_.get('id'))

    def _collect_lock_holders(
        self,
        connection: redis.client.Redis,
        expected_subscribers: int,
        timeout: float,
    ) -> list[RedisLockHolder] | None:
        """Probe the channel and report who is holding the lock.

        Publishes a ping on the lock channel carrying a private,
        single-use response channel (``<channel>-<uuid4>``) and gathers
        the answers. That channel is subscribed *and its subscription
        confirmed* before the ping goes out, so a fast holder cannot
        reply into a subscription that is not listening yet; the
        confirmation frame is drained here rather than counted as a
        reply. That wait is bounded by ``timeout``: if no confirmation
        arrives in time the ping is published anyway, and replies that
        beat the subscription are missed, which shows up as an
        inconclusive probe. The probe uses its own short-lived pubsub,
        which is always closed again, so the subscription that holds the
        lock is never disturbed.

        Answers are keyed by holder id, so a holder that answers twice is
        counted once, and collection stops as soon as
        `expected_subscribers` distinct holders have replied.

        Returning `None` is not the same as returning an empty list, and
        the difference is the reason `_resolve_lock_holders` guards every
        branch on ``holders is not None``:

        - `None` means *could not determine*. The probe is unusable and
          the only safe response is to try again. Reading it as "nobody
          holds the lock" would hand out a lock that somebody else may
          well be holding.
        - A list is a positive statement about who is there, and can be
          acted on. It always contains this lock itself, because a
          probing lock is subscribed to its own channel and answers its
          own ping. An empty list would mean nobody at all is there, but
          `acquire` never asks in that situation: it short-circuits when
          it is the only subscriber.

        A probe is inconclusive in two ways:

        1. The subscriber count changed while the probe was running
           (``current != expected``). Someone joined or left, so the
           replies describe a channel that no longer exists in that
           shape, and a decision made from them could be wrong for
           either party.
        2. Fewer holders replied than there were subscribers. Somebody
           counted by Redis is not answering, which normally means it
           crashed, though a holder too slow to answer inside ``timeout``
           is treated the same way;
           `_kill_unavailable_locks` reaps those connections and the
           probe still reports inconclusive so that the caller retries
           against the cleaned-up channel.

        Args:
            connection: The connection to publish the ping on and to run
                the subscriber count against.
            expected_subscribers: How many subscribers the caller counted
                just before probing. This is both the target number of
                replies and the value the count is re-checked against.
            timeout: Seconds to spend waiting for the subscribe
                confirmation and, separately, for the replies. Polling
                uses `thread_sleep_time` or a tenth of this timeout,
                whichever is smaller.

        Returns:
            The holders that answered, or `None` when the probe was
            inconclusive.
        """
        response_channel: str = f'{self.channel}-{uuid.uuid4().hex}'
        check_interval: float = min(self.thread_sleep_time, timeout / 10)
        pubsub: redis.client.PubSub = self._get_pubsub(connection)
        holders: dict[str, RedisLockHolder] = {}
        legacy_index: int = 0
        try:
            pubsub.subscribe(response_channel)
            for _ in self._timeout_generator(timeout, check_interval):
                confirmation: dict[str, typing.Any] | None = typing.cast(
                    'dict[str, typing.Any] | None',
                    pubsub.get_message(timeout=check_interval),
                )
                if confirmation and confirmation.get('type') == 'subscribe':
                    break

            connection.publish(
                self.channel,
                json.dumps(
                    {
                        'message': 'ping',
                        'response_channel': response_channel,
                    }
                ),
            )

            for _ in self._timeout_generator(timeout, check_interval):
                message: dict[str, typing.Any] | None = typing.cast(
                    'dict[str, typing.Any] | None',
                    pubsub.get_message(timeout=check_interval),
                )
                if message and message.get('type') == 'message':
                    holder: RedisLockHolder = self._parse_lock_response(
                        message.get('data'),
                        legacy_index,
                    )
                    holders[holder.holder_id] = holder
                    legacy_index += int(holder.legacy)
                    if len(holders) >= expected_subscribers:
                        break

            current_subscribers: int = self._get_subscriber_count(connection)
            logger.debug(
                'Redis lock %s probe expected=%d received=%d current=%d',
                self.holder_id,
                expected_subscribers,
                len(holders),
                current_subscribers,
            )
            if current_subscribers != expected_subscribers:
                return None
            if len(holders) < expected_subscribers:
                self._kill_unavailable_locks(connection, holders.values())
                return None
            return list(holders.values())
        finally:
            pubsub.close()

    def _holders_are_compatible(
        self,
        holders: list[RedisLockHolder],
    ) -> bool:
        """Report whether this lock may join the holders already present.

        The whole compatibility matrix fits in one expression because
        only one combination is allowed::

            our flags   the holders present       result
            ---------   -------------------       ------
            SHARED      all SHARED                compatible
            SHARED      any PENDING or EXCLUSIVE  not compatible
            EXCLUSIVE   anything at all           not compatible

        An exclusive lock never joins anybody; it goes through the
        election in `_writer_is_elected` instead. A reader joins only a
        set of pure readers, and a single `RedisLockMode.PENDING` holder
        is enough to keep it out - that is the rule that stops an endless
        stream of readers from starving a waiting writer. Legacy holders
        are recorded as `RedisLockMode.EXCLUSIVE`, so an old participant
        blocks readers too.

        This lock's own reply is part of `holders`, and a shared lock
        advertises `RedisLockMode.SHARED` from construction onwards, so
        seeing itself in the list never makes it incompatible with
        itself.

        Args:
            holders: The holders returned by a conclusive probe.

        Returns:
            True when this lock can be added to the existing holders
            without violating exclusivity.
        """
        return self.flags == constants.LockFlags.SHARED and all(
            holder.mode is RedisLockMode.SHARED for holder in holders
        )

    def _writer_is_elected(
        self,
        holders: list[RedisLockHolder],
    ) -> bool:
        """Report whether this lock is the writer elected to go next.

        Returns False at once if any holder is already
        `RedisLockMode.EXCLUSIVE`: the lock is owned, so there is nothing
        to elect. Otherwise the ids of every `RedisLockMode.PENDING`
        holder are sorted and the lowest one wins.

        Sorting is the entire trick, and it is worth spelling out why it
        works. There is no coordinator here, and no lock to take before
        taking the lock. What every contender does have is the result of
        its own probe, and each probe asked the same channel at
        approximately the same time, so they all see the same set of
        pending ids. Sorting that set is a pure function of its contents,
        so every contender independently computes the *same* winner, and
        only the one whose own `holder_id` sorts first concludes that it
        won. No message needs to be exchanged to agree.

        Contenders whose probes disagree - someone joined halfway
        through - do not corrupt the outcome, because a probe taken
        during a membership change is discarded upstream:
        `_collect_lock_holders` returns `None` when the subscriber count
        moved. The losers simply retry, and a loser that keeps losing is
        bounded by the caller's timeout, not by the election.

        Holder ids are uuid4 hex strings, so the resulting order is
        arbitrary but stable. It is a tie-break, not a priority: this is
        not a fair queue and a writer is not guaranteed to be served
        before writers that arrive later.

        `RedisLockMode.SHARED` holders do not block the election, so a
        writer can be elected while readers are still draining. Being
        elected is not the same as holding the lock; only
        `_resolve_lock_holders` converts election into ownership, and
        only once no shared holder is left.

        Args:
            holders: The holders returned by a conclusive probe,
                including this lock's own record.

        Returns:
            True when this lock is the pending writer with the lowest
            holder id and nobody holds the lock exclusively.

        Example:
            >>> from portalocker import redis
            >>> lock = redis.RedisLock('some_channel')
            >>> lock.holder_id = 'bbb'
            >>> pending = redis.RedisLockMode.PENDING
            >>> lock._writer_is_elected(
            ...     [
            ...         redis.RedisLockHolder('aaa', pending),
            ...         redis.RedisLockHolder('bbb', pending),
            ...     ]
            ... )
            False
            >>> lock._writer_is_elected(
            ...     [
            ...         redis.RedisLockHolder('bbb', pending),
            ...         redis.RedisLockHolder('ccc', pending),
            ...     ]
            ... )
            True
        """
        if any(holder.mode is RedisLockMode.EXCLUSIVE for holder in holders):
            return False
        pending_holder_ids: list[str] = sorted(
            holder.holder_id
            for holder in holders
            if holder.mode is RedisLockMode.PENDING
        )
        return bool(
            pending_holder_ids and pending_holder_ids[0] == self.holder_id
        )

    def _resolve_lock_holders(
        self,
        holders: list[RedisLockHolder] | None,
        fail_when_locked: bool,
    ) -> bool:
        """Decide what to do with the result of one probe.

        This is the only place where an observation becomes an action.
        Every branch is guarded on ``holders is not None``, because an
        inconclusive probe must never be read as "the lock is free".

        The outcomes, in the order they are tested:

        - **Compatible holders** (see `_holders_are_compatible`): the
          lock is now held. The subscription opened for the probe is
          exactly the subscription that holds it, so nothing more is
          needed.
        - **Elected writer, no readers left** (see `_writer_is_elected`):
          `mode` is promoted from `RedisLockMode.PENDING` to
          `RedisLockMode.EXCLUSIVE`, which is what every later probe by
          anybody else will see, and the lock is held.
        - **Elected writer, readers still holding**: the election is
          remembered in `writer_elected` and the subscription is kept, so
          this lock's `RedisLockMode.PENDING` record keeps new readers
          out while the existing ones drain. The caller retries.
        - **Inconclusive probe while already elected**: the subscription
          is kept and the caller retries. An elected writer must not drop
          its pending record over a single noisy probe; doing so would
          let new readers in and could forfeit an election it had already
          won, since `release` clears `writer_elected`.
        - **Anything else**: somebody incompatible is there, or the probe
          was inconclusive and this lock is not elected. The
          subscription is released before retrying. That release is the
          point rather than a detail: a waiter that stayed subscribed
          would keep inflating the subscriber count that everybody
          else's probe has to match exactly.

        With `fail_when_locked` the caller does not want to wait, so the
        first attempt that does not end in ownership raises instead of
        returning. An elected writer always raises here, even when no
        readers remain and the lock could have been taken outright,
        because that check runs before the promotion to
        `RedisLockMode.EXCLUSIVE`; reaching this method at all means the
        channel is contended. The one exception is an already-elected
        writer facing
        an inconclusive probe, which keeps retrying because it is not
        contention that stopped it.

        Args:
            holders: A conclusive probe result, or `None` when the probe
                could not determine the state of the channel.
            fail_when_locked: Give up immediately instead of retrying.

        Returns:
            True when the lock is now held and `acquire` may return;
            False when the caller should retry.

        Raises:
            AlreadyLocked: `fail_when_locked` was set and this attempt
                did not end in ownership.
        """
        if holders is not None and self._holders_are_compatible(holders):
            return True

        writer_is_elected: bool = (
            holders is not None
            and self.flags == constants.LockFlags.EXCLUSIVE
            and self._writer_is_elected(holders)
        )
        if writer_is_elected:
            self.writer_elected = True
            if fail_when_locked:
                self.release()
                raise exceptions.AlreadyLocked()
            if holders is not None and not any(
                holder.mode is RedisLockMode.SHARED for holder in holders
            ):
                self.mode = RedisLockMode.EXCLUSIVE
                return True
            return False

        if holders is None and self.writer_elected:
            return False

        self.release()
        logger.debug('Redis lock %s released to retry', self.holder_id)
        if fail_when_locked:
            raise exceptions.AlreadyLocked()
        return False

    def acquire(
        self,
        timeout: float | None = None,
        check_interval: float | None = None,
        fail_when_locked: bool | None = None,
    ) -> RedisLock:
        """Acquire the lock, retrying until it is free or time runs out.

        Every attempt subscribes to the channel if this lock is not
        subscribed already, then either takes the lock outright or hands
        a probe result to `_resolve_lock_holders`:

        1. Count the subscribers. Being the only one means nobody else is
           on the channel, so the lock is free and is taken immediately;
           an exclusive lock promotes itself from `RedisLockMode.PENDING`
           to `RedisLockMode.EXCLUSIVE` here. This is the uncontended
           fast path and costs one round trip.
        2. Otherwise probe the channel with `_collect_lock_holders` and
           let `_resolve_lock_holders` decide.

        A failed attempt normally unsubscribes again, so the next
        iteration subscribes from scratch; an elected writer is the
        exception and holds on to its subscription between attempts.

        `fail_when_locked` turns the retry loop into a single attempt
        against a contended channel: rather than polling until the
        timeout expires against a holder that has demonstrably answered a
        ping, the first attempt that does not end in ownership raises
        `AlreadyLocked`. An elected writer always raises here too, even
        when no readers remain and the lock could have been taken
        outright.

        If subscribing itself fails, `_start_subscription` rolls back
        before re-raising, so the original error propagates with the lock
        left inactive and the same object can be used again.

        Args:
            timeout: Seconds to keep retrying. Defaults to the instance's
                `timeout`, itself defaulting to `utils.DEFAULT_TIMEOUT`.
                Zero still makes exactly one attempt.
            check_interval: Base seconds to wait between attempts,
                jittered by `_timeout_generator`. Defaults to the
                instance's `check_interval`.
            fail_when_locked: Raise `AlreadyLocked` on the first
                unsuccessful attempt instead of retrying. Defaults to the
                instance's `fail_when_locked`.

        Returns:
            This lock, so ``with RedisLock(...) as lock`` binds the lock
            itself rather than a file handle.

        Raises:
            ~portalocker.exceptions.AlreadyLocked: The timeout expired
                without acquiring the lock, or `fail_when_locked` was set
                and the first attempt did not succeed.
            AssertionError: This instance is already holding a lock. A
                `RedisLock` is not reentrant and holds at most one lock
                at a time; use a second instance, which gets its own
                `holder_id`.

        Example:
            >>> import fakeredis
            >>> import portalocker
            >>> connection = fakeredis.FakeStrictRedis(
            ...     server=fakeredis.FakeServer(), decode_responses=True
            ... )
            >>> lock = portalocker.RedisLock(
            ...     'some_channel', connection=connection
            ... )
            >>> lock.acquire(timeout=1) is lock
            True
            >>> lock.release()
        """
        effective_timeout: float = typing.cast(
            'float',
            utils.coalesce(timeout, self.timeout, 0.0),
        )
        effective_check_interval: float = typing.cast(
            'float',
            utils.coalesce(check_interval, self.check_interval, 0.0),
        )
        effective_fail_when_locked: bool = typing.cast(
            'bool',
            utils.coalesce(fail_when_locked, self.fail_when_locked, False),
        )

        assert not self.pubsub, 'This lock is already active'
        if self.flags == constants.LockFlags.EXCLUSIVE:
            self.mode = RedisLockMode.PENDING
            self.writer_elected = False
        connection: redis.client.Redis = self.get_connection()

        for _ in self._timeout_generator(
            effective_timeout,
            effective_check_interval,
        ):
            if self.pubsub is None:
                self._start_subscription(connection)
            subscribers: int = self._get_subscriber_count(connection)
            logger.debug(
                'Redis lock %s mode=%s observed %d subscribers',
                self.holder_id,
                self.mode.value,
                subscribers,
            )
            if subscribers == 1:
                if self.flags == constants.LockFlags.EXCLUSIVE:
                    self.mode = RedisLockMode.EXCLUSIVE
                return self

            holders: list[RedisLockHolder] | None = self._collect_lock_holders(
                connection,
                subscribers,
                self.unavailable_timeout,
            )
            logger.debug(
                'Redis lock %s observed holders=%r',
                self.holder_id,
                holders,
            )
            if self._resolve_lock_holders(
                holders,
                effective_fail_when_locked,
            ):
                return self

        self.release()
        raise exceptions.AlreadyLocked()

    def check_or_kill_lock(
        self,
        connection: redis.client.Redis,
        timeout: float,
    ) -> bool | None:
        """Ask whether anyone is alive on the channel, and reap if not.

        The public liveness check from before 4.0.0. `acquire` no longer
        uses it: it probes with `_collect_lock_holders` and reaps with
        `_kill_unavailable_locks`, which understand individual holders
        and lock modes. This method predates holder ids and answers only
        the coarser question "is anybody answering on this channel?".

        The ping is published only after the subscription's own
        confirmation frame has been consumed, or the wait for it times
        out. Redis queues a ``subscribe`` confirmation the moment a
        subscription is made; before 4.0.0 the reply poll accepted any
        message, so that confirmation was read as a reply, a crashed
        holder was reported as alive and stale locks were never reaped.
        The poll below now also requires ``type == 'message'``, and
        draining the confirmation first guarantees that the subscription
        is active before the ping goes out, so a real reply cannot be
        published into a subscription that is not listening yet.

        Note:
            The reap step matches ``CLIENT LIST`` entries against
            `client_name`, which since 4.0.0 carries this instance's own
            `holder_id`. On a live server that name identifies only this
            lock's own connection, so another process's crashed holder is
            not matched here; reaping across holders is what
            `_kill_unavailable_locks` does during `acquire`.

        Args:
            connection: The connection to probe and to reap on.
            timeout: Seconds to wait for the subscribe confirmation and,
                separately, for a reply, so a fully silent channel can
                take up to twice this long.

        Returns:
            True as soon as any reply arrives. `None` when nothing
            answered in time, after killing the matching pubsub
            connections. False is never returned.
        """
        # Random channel name to get messages back from the lock
        response_channel = f'{self.channel}-{random.random()}'
        check_interval = min(self.thread_sleep_time, timeout / 10)

        pubsub = self._get_pubsub(connection)
        try:
            pubsub.subscribe(response_channel)

            # Consume the subscribe-confirmation message *before* pinging.
            # Redis queues a confirmation the moment we subscribe; if it were
            # left in the buffer the poll below would treat it as a pong and
            # wrongly report the holder as alive. Waiting for it here also
            # guarantees the subscription is active before we publish, so the
            # pong sent in response to our ping cannot be dropped.
            for _ in self._timeout_generator(timeout, check_interval):
                confirmation = typing.cast(
                    'dict[str, typing.Any] | None',
                    pubsub.get_message(timeout=check_interval),
                )
                if confirmation and confirmation.get('type') == 'subscribe':
                    break

            connection.publish(
                self.channel,
                json.dumps(
                    dict(
                        response_channel=response_channel,
                        message='ping',
                    ),
                ),
            )

            for _ in self._timeout_generator(timeout, check_interval):
                message = typing.cast(
                    'dict[str, typing.Any] | None',
                    pubsub.get_message(timeout=check_interval),
                )
                if message and message.get('type') == 'message':
                    return True

            clients: list[dict[str, str]] = connection.client_list('pubsub')
            for client_ in clients:
                if client_.get('name') == self.client_name:
                    logger.warning(
                        'Killing unavailable redis client: %r',
                        client_,
                    )
                    connection.client_kill_filter(client_.get('id'))
            return None
        finally:
            pubsub.close()

    def release(self) -> None:
        """Give up the lock and undo everything `acquire` set up.

        Stops and joins the keep-alive thread, unsubscribes and closes
        the pubsub connection, and forgets any election this lock had
        won. A connection the lock created itself is closed and cleared,
        so the next `get_connection` builds a fresh one; a connection
        supplied by the caller is left alone.

        Dropping the subscription is not merely cleanup, it *is* the
        release: other processes learn the lock is free by no longer
        seeing this subscriber, with no key to delete and no expiry to
        wait for.

        The same method doubles as the back-off between attempts.
        `_resolve_lock_holders` calls it after an unsuccessful probe so
        that a waiting lock stops being counted as a subscriber, and
        `_start_subscription` calls it to roll back a subscribe that
        failed halfway.

        Calling this when nothing was acquired is harmless - it still
        closes a self-created connection if one exists - which is what
        makes both that rollback and `__del__` safe.
        """
        self.writer_elected = False
        if self.thread:  # pragma: no branch
            self.thread.stop()
            self.thread.join()
            self.thread = None
            time.sleep(0.01)

        if self.pubsub:  # pragma: no branch
            # `PubSub.unsubscribe()` is unannotated in redis-py
            self.pubsub.unsubscribe(  # type: ignore[no-untyped-call]
                self.channel,
            )
            self.pubsub.close()
            self.pubsub = None

        # Only close connections we created ourselves; caller-supplied ones
        # are left untouched. Clear it so a later acquire recreates it.
        if self.close_connection and self.connection is not None:
            self.connection.close()
            self.connection = None

    def __del__(self) -> None:  # pragma: no cover - best effort cleanup
        """Release the lock when the object is garbage collected.

        A safety net for a lock that was never released explicitly, not a
        substitute for `release` or a ``with`` block. CPython's reference
        counting usually runs this promptly, but PyPy collects whenever
        it sees fit, and nothing runs at all if the interpreter exits
        while a reference is still alive.

        That last case is harmless here, which is the advantage of
        holding a lock in a connection rather than in a key: the process
        exits, the daemon reader thread goes with it, the socket closes
        and Redis releases the lock. Unlike
        `utils.LockBase.__del__` this does not suppress errors, so a
        failure during collection surfaces as the interpreter's
        "Exception ignored in" message and cannot be caught by the code
        that dropped the reference.
        """
        self.release()
