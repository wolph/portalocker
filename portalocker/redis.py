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
           foreign elected writer -> defer to it, back off and retry
           elected, no readers    -> take the lock exclusively
           anything else          -> unsubscribe and retry
      +- confirm a promotion (writers only, see
         `RedisLock._confirm_exclusive_promotion`): with the new
         exclusive record visible, count and probe once more. A rival
         that promoted on an equally stale view is resolved by holder
         id and the loser demotes

Shared readers hold the lock together, an exclusive writer holds it
alone. There is no coordinator and no lock to take before taking the
lock, so competing writers agree on a single winner by sorting the
pending holder ids they all saw; see `RedisLock._writer_is_elected`.
Subscribers that Redis still counts but that stopped answering are
crashed processes, and their connections are killed so that the channel
becomes consistent again.

Losing the connection releases the lock, and since 4.2.0 the holder is
told about it just as promptly. The subscription lives on a dedicated
connection that never reconnects (a resurrected subscription would be a
silent re-acquisition that skipped the election), so the first read
error after a revocation marks the lock as lost: `RedisLock.ensure_held`
raises `~portalocker.exceptions.LockLostError`, the ``with`` block exit
raises it too once the body finished cleanly, an ``on_lost`` callback
fires on the reader thread, and by default the main thread receives a
``KeyboardInterrupt`` (``interrupt_on_lost``, opt-in from 5.0.0
onwards). Caveats that follow from this design:

- Under redis-py's default ``socket_timeout`` of five seconds, a read
  stalled for that long raises ``TimeoutError`` and counts as a loss. A
  holder that cannot complete a read cannot confirm ownership either,
  so this is deliberate, but pathologically slow links can produce
  false losses.
- The zero-reconnect policy is applied on a RESP2 connection because
  RESP3 maintenance notifications carry their own reconnect path that
  bypasses the retry policy. Callers who need RESP3 on the subscription
  must supply ``subscription_connection_factory`` and disable
  maintenance notifications themselves.
- A holder running portalocker 4.1 or older still resubscribes silently
  after a kill, so the loss guarantee only covers channels where every
  participant runs 4.2 or later.
- Loss detection rides on the socket. A half-open link that never
  delivers a TCP reset - a hard-powered-off peer, a silently
  partitioned network - only surfaces when something writes into the
  connection, so with ``health_check_interval=0`` (redis-py's default
  for a connection you supply yourself) such a partition goes
  undetected indefinitely. The opt-in ``self_check_interval``
  parameter closes this hole above the transport: the holder
  periodically pings itself through its own channel and treats a
  missing echo as the loss it is (see `RedisLockSelfCheckError`).
- From the revocation until the holder observes it, the old and the
  new holder both run. Detection is bounded (about one worker sleep
  interval once the TCP layer notices), reaction is not, and only
  fencing at the resource itself - a token the resource checks, which
  is outside this lock's reach - closes that window. The opt-in
  ``fencing`` parameter hands every exclusive grant such a token
  (`RedisLock.fence_token`). Checking it remains the resource's job.

Set ``health_check_interval`` on the connection (it is part of
`RedisLock.DEFAULT_REDIS_KWARGS`) so that both sides notice a dead peer
promptly; the periodic ping is also what turns a half-open link into a
read error.

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
import contextlib
import enum
import json
import logging
import os
import random
import re
import threading
import time
import typing
import uuid
import warnings

import redis.backoff
import redis.client
import redis.connection
import redis.retry

# Aliased instead of `import redis.exceptions`: the single-file build
# (`python -m portalocker combine`, see `__main__._clean_line`) strips
# the qualifier of every inlined portalocker module from the output,
# and portalocker has its own `exceptions` module, so a literal
# reference to `AuthorizationError` qualified with the dotted module
# path would be rewritten to `redis.AuthorizationError`, which only
# exists for the few classes redis re-exports at top level. The
# `redis_exceptions` alias is a single word to that regex and survives
# combining unchanged.
from redis import exceptions as redis_exceptions

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
#: Seconds a probe waits for a reply that is in flight but not yet
#: buffered. After a non-blocking read comes up empty,
#: `RedisLock._drain_probe_replies` polls once with this timeout before
#: giving the pass up, so a reply a few milliseconds behind its
#: predecessor is collected in the same pass instead of after a full
#: jittered drain interval (#145).
_PROBE_REPLY_GRACE = 0.005


def _keep_first_error(
    first_error: Exception | None,
    error: Exception,
) -> Exception:
    """Pick the error a multi-step teardown should re-raise.

    `RedisLock.release` and `RedisLock._unsubscribe` run every teardown
    step even when an earlier one fails, so they can end up holding more
    than one error. The first one describes what actually went wrong,
    and the later ones are usually the same dead connection hitting the
    next step. The first error is therefore kept and any later error is
    logged instead of raised, so it cannot replace the original cause.

    Args:
        first_error: The error kept so far, or `None` when every earlier
            step succeeded.
        error: The error raised by the step that just failed.

    Returns:
        `error` when it is the first failure, otherwise `first_error`
        unchanged, with `error` logged as a suppressed secondary
        failure.
    """
    if first_error is None:
        return error
    logger.warning(
        'Suppressed secondary error while releasing redis lock: %r',
        error,
    )
    return first_error


#: Errors that mean the subscription connection itself died, as opposed
#: to a bug inside the message handler or the redis-py reader. The
#: distinction only affects how the failure is logged and reported: any
#: worker failure while the lock is held is treated as a loss, because a
#: subscription nobody services stops answering pings and gets reaped by
#: the next prober regardless of why its reader died.
_CONNECTION_LOSS_ERRORS: tuple[type[BaseException], ...] = (
    redis_exceptions.ConnectionError,
    redis_exceptions.TimeoutError,
    OSError,
)


class RedisLockSelfCheckError(redis_exceptions.ConnectionError):
    """A held lock could not deliver a message to itself in time.

    Raised on the keep-alive worker thread when the periodic self-check
    (the opt-in ``self_check_interval`` of `RedisLock`) publishes a ping
    to the lock's own channel and this holder's own reply does not come
    back through the response-channel machinery within the deadline.

    It subclasses ``redis.exceptions.ConnectionError`` deliberately: a
    holder that cannot complete its own round trip is, for locking
    purposes, disconnected. It can no longer observe revocations and it
    cannot answer another prober's ping either, so the next contended
    probe would reap its connection anyway. `RedisLock` therefore
    classifies a failed self-check as a connection loss and surfaces it
    exactly like a socket error: `RedisLock.lost` turns `True`,
    `RedisLock.ensure_held` and the ``with`` block exit raise
    `~portalocker.exceptions.LockLostError` with this error as
    ``__cause__``, ``on_lost`` fires, and ``interrupt_on_lost`` behaves
    as it would for a dead socket.

    .. versionadded:: 4.2.0
    """


class _SelfCheckAbandoned(Exception):  # noqa: N818 - control flow, no error
    """Internal control flow: the lock left ``HELD`` mid-self-check.

    Raised by `RedisLock._await_self_check_frame` when a concurrent
    `RedisLock.release` moved the state on while a self-check was still
    waiting for a frame, and caught by `RedisLock._self_check_tick`: a
    lock that stopped being held has nothing left to verify, so the
    check simply stops, declaring neither success nor loss. Never
    leaves this module.
    """


def _optional_redis_errors(name: str) -> tuple[type[BaseException], ...]:
    """Look up a redis-py exception class that may not exist yet.

    portalocker supports redis-py 5.0 and newer, while some exception
    classes only appeared later, so tables of exception types cannot
    always reference them directly. The lookup returns a tuple so a
    missing name simply contributes nothing when splatted into such a
    table.

    Args:
        name: Attribute name to look up on ``redis.exceptions``.

    Returns:
        A one-element tuple with the class, or an empty tuple when this
        redis-py release does not define it.
    """
    error_class: type[BaseException] | None = getattr(
        redis_exceptions,
        name,
        None,
    )
    if error_class is None:
        return ()
    return (error_class,)


#: ``redis_exceptions.ConnectionError`` subclasses that retrying cannot
#: cure, so `RedisLock._try_subscribe` must not classify them as
#: transient blips: burning the acquire timeout on them would bury the
#: real problem under "could not subscribe" noise and end in a
#: misleading ``AlreadyLocked``. The judgement per subclass:
#:
#: - ``AuthenticationError`` and ``AuthorizationError``: wrong or
#:   insufficient credentials repeat identically on every retry.
#: - ``MaxConnectionsError``: a factory-supplied pool at its cap is
#:   exhausted by the application itself; a tight retry loop does not
#:   free the connections the application holds, and the subscription
#:   would need one for the whole hold anyway. (The built-in derivation
#:   builds a fresh, effectively unbounded pool, so from there this
#:   cannot fire at all.)
#: - ``ExternalAuthProviderError`` (redis-py 8+): the credential
#:   provider machinery failed, which is configuration, not weather.
#:
#: Deliberately still transient: ``BusyLoadingError`` (the server is
#: loading its dataset after a restart and finishes on its own, the
#: canonical condition worth waiting out) and Sentinel's
#: ``MasterNotFoundError`` (a failover in progress resolves within
#: seconds, and Sentinel setups reach this code only through
#: ``subscription_connection_factory`` anyway).
_NON_TRANSIENT_SUBSCRIBE_ERRORS: tuple[type[BaseException], ...] = (
    redis_exceptions.AuthenticationError,
    redis_exceptions.AuthorizationError,
    redis_exceptions.MaxConnectionsError,
    *_optional_redis_errors('ExternalAuthProviderError'),
)


def _is_transient_connection_error(error: BaseException) -> bool:
    """Report whether ``error`` is a retry-worthy connection blip.

    The one classification both halves of an acquire attempt share: a
    subscribe failure in `RedisLock._try_subscribe` and a probe failure
    in `RedisLock._acquire_attempt` cost one attempt when the error is
    connection weather, and are terminal otherwise. Weather means a
    ``redis_exceptions.ConnectionError`` or ``TimeoutError`` that is not
    one of the `_NON_TRANSIENT_SUBSCRIBE_ERRORS`, which repeat
    identically on every retry.

    Args:
        error: Whatever the attempt raised.

    Returns:
        True when the caller should burn one attempt and retry, False
        when the error must propagate after a full release.
    """
    if not isinstance(
        error,
        (redis_exceptions.ConnectionError, redis_exceptions.TimeoutError),
    ):
        return False
    return not isinstance(error, _NON_TRANSIENT_SUBSCRIBE_ERRORS)


#: Signature of the exception handler a `PubSubWorkerThread` escalates
#: through. redis-py annotates the error parameter as ``Exception`` but
#: passes ``BaseException`` at runtime; this alias states the runtime
#: truth.
_WorkerExceptionHandler = typing.Callable[
    [
        BaseException,
        'redis.client.PubSub',
        'redis.client.PubSubWorkerThread',
    ],
    None,
]

#: Signature of the per-iteration hook `PubSubWorkerThread` runs after
#: every subscription read, receiving the pubsub it is reading. The
#: only registered tick is `RedisLock._self_check_tick`, and only when
#: ``self_check_interval`` is set. Anything the tick raises is routed
#: through the worker's exception handler like a read failure.
_WorkerTick = typing.Callable[['redis.client.PubSub'], None]


class _LockState(enum.Enum):
    """Lifecycle of one `RedisLock` instance.

    The state answers one question for the failure paths: when the
    keep-alive worker dies, was this lock merely trying to acquire
    (`ACQUIRING`, the failure costs one attempt) or did it own the lock
    (`HELD`, the failure is a loss the application must hear about)?
    `RedisLock._on_worker_exception` makes that call on the worker
    thread while `RedisLock.acquire` transitions on the calling thread,
    so every transition and read runs under ``RedisLock._state_lock``.

    `LOST` is deliberately sticky: `RedisLock.release` keeps it (and the
    causal error) so the loss stays observable through
    `RedisLock.lost` and the ``with`` block exit after the teardown ran.
    Only the next `RedisLock.acquire` resets a lost instance.
    """

    #: Nothing acquired and nothing in flight.
    IDLE = 'idle'
    #: `RedisLock.acquire` is running but has not confirmed ownership.
    #: A worker failure in this state fails one attempt, nothing more.
    ACQUIRING = 'acquiring'
    #: The lock is owned. Set by `RedisLock._confirm_held` only.
    HELD = 'held'
    #: The lock was owned and was revoked from outside. Set by
    #: `RedisLock._on_worker_exception` only.
    LOST = 'lost'


class _ConfirmVerdict(enum.Enum):
    """Outcome of one confirm-probe round after an exclusive promotion.

    Produced by `RedisLock._confirm_probe_verdict` and consumed by
    `RedisLock._confirm_exclusive_promotion`: one clean round confirms
    the promotion, an outranking holder demotes it, and anything
    undecided makes the round inconclusive so the confirm asks again
    instead of concluding from noise (#145).
    """

    #: No holder outranks the promotion and nobody is undecided.
    CONFIRMED = 'confirmed'
    #: A lower-id 4.2 pending peer may be promoting on a stale view of
    #: its own, so this round proves nothing either way.
    RETRY = 'retry'
    #: A holder outranks the promotion and the writer gives it up.
    DEMOTE = 'demote'


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
        >>> holder.elected is None
        True
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
    #: Whether the holder advertised a won election. True and False come
    #: from the ``elected`` field portalocker 4.2.0 added to the record,
    #: while `None` means the reply carried no readable field at all, so
    #: the holder runs a pre-4.2 release. That distinction is what the
    #: mixed-version fallback keys on: a pre-4.2 pending writer reruns
    #: the id election and cannot be told to defer, so an incumbent
    #: forfeits to it exactly as 4.1 did (see
    #: `RedisLock._must_forfeit`).
    elected: bool | None = None


class PubSubWorkerThread(redis.client.PubSubWorkerThread):
    """redis-py's pubsub reader thread, with failures routed to the lock.

    The subscription this thread services *is* the lock. While it runs,
    the holder answers liveness pings and Redis keeps counting it as a
    subscriber. If the thread were to die quietly - a dropped connection,
    a protocol error - the owning process would carry on believing it
    still holds a lock that every other process now considers released,
    which is exactly the split-brain this lock exists to avoid.

    redis-py's read loop already catches `BaseException` and hands it to
    the ``exception_handler`` passed at construction, which is
    `RedisLock._on_worker_exception` here. That handler decides whether
    the failure is a loss (the lock was held) or a failed attempt (the
    lock was still being acquired). This subclass adds two things.

    It owns the read loop instead of inheriting it, because redis-py's
    loop sets its running flag from *inside* the thread and ``stop()``
    clears that same flag: a ``stop()`` issued after ``start()`` but
    before the new thread reached ``run()`` was overwritten by the
    thread's own set, the loop then ran forever, and the ``join()`` in
    `RedisLock._unsubscribe` hung with it. `RedisLock.acquire` tears a
    fresh subscription down microseconds after starting it whenever a
    confirm is refused, an election is lost or ``fail_when_locked``
    gives up, so a CPU-starved worker thread made that ordering real.
    Here `stop` records the request in an event that `run` consults
    before every read, so a stop can never be lost, however early it
    lands. redis-py's own ``_running`` flag is left unused.

    It also adds a last-ditch layer: an exception escaping ``run``
    itself - a failure inside the handler, or inside the
    ``pubsub.close()`` that runs after the loop - is routed into the
    same handler instead of dying with the thread.
    """

    #: Set by `stop`, read by `run` before every read. An event rather
    #: than redis-py's cleared-on-stop flag so a stop that precedes the
    #: thread's first instruction still takes effect.
    _stop_requested: threading.Event
    #: Optional hook run after every read, or `None` for the plain read
    #: loop. See `_WorkerTick`.
    _tick: _WorkerTick | None

    def __init__(
        self,
        pubsub: redis.client.PubSub,
        sleep_time: float,
        daemon: bool = False,
        exception_handler: _WorkerExceptionHandler | None = None,
        tick: _WorkerTick | None = None,
    ) -> None:
        """Create the reader thread without starting it.

        Args:
            pubsub: The subscribed pubsub to read from.
            sleep_time: Seconds each ``get_message`` waits for a frame,
                which is also how long a `stop` takes to be noticed.
            daemon: Whether the thread may be abandoned at interpreter
                exit. `RedisLock` always passes `True`.
            exception_handler: Receives everything the read loop raises.
                Without one, errors propagate and end the thread.
            tick: Optional hook run once per loop iteration, after the
                read, with the pubsub as its argument. `RedisLock` uses
                it for the opt-in self-check, and `None` (the default)
                keeps the loop a plain read loop.
        """
        # redis-py annotates the handler parameter as taking `Exception`
        # while passing `BaseException` at runtime; the cast states the
        # runtime truth instead of narrowing this class's signature.
        super().__init__(
            pubsub,
            sleep_time,
            daemon=daemon,
            exception_handler=typing.cast('typing.Any', exception_handler),
        )
        self._stop_requested = threading.Event()
        self._tick = tick

    def stop(self) -> None:
        """Ask the read loop to end after its current read.

        Safe to call at any point in the thread's life, including before
        `run` started and after it ended. The loop closes the pubsub on
        its way out, which disconnects the subscription socket.
        """
        self._stop_requested.set()

    def run(self) -> None:
        """Read from the subscription, routing every failure to the lock.

        Raises:
            BaseException: Whatever escaped the underlying reader, only
                when no ``exception_handler`` was registered. With a
                handler registered nothing propagates: the handler is
                the escalation path and this thread simply ends.
        """
        # The pubsub attribute is unannotated in redis-py, and the
        # handler is stored as redis-py's narrower annotation; both
        # casts restore the real types.
        pubsub: redis.client.PubSub = typing.cast(
            'redis.client.PubSub',
            self.pubsub,
        )
        handler: _WorkerExceptionHandler | None = typing.cast(
            '_WorkerExceptionHandler | None',
            self.exception_handler,
        )
        try:
            self._read_until_stopped(pubsub, handler)
        except BaseException as error:
            if handler is None:
                raise
            handler(error, pubsub, self)

    def _read_until_stopped(
        self,
        pubsub: redis.client.PubSub,
        handler: _WorkerExceptionHandler | None,
    ) -> None:
        """Poll the subscription until `stop` was called, then close it.

        The loop body is redis-py's: one ``get_message`` per iteration,
        bounded by ``sleep_time``, with every failure handed to the
        handler (the handler stops the thread, so a dead socket ends the
        loop through it). Two things differ: the loop condition (see the
        class docstring), and an optional ``tick`` hook run after every
        read inside the same protection, so a failing tick escalates
        exactly like a failing read.

        Args:
            pubsub: The subscribed pubsub to read from.
            handler: The failure handler, or `None` to let failures
                propagate to `run`.
        """
        while not self._stop_requested.is_set():
            try:
                pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=self.sleep_time,
                )
                if self._tick is not None:
                    self._tick(pubsub)
            except BaseException as error:  # noqa: PERF203
                if handler is None:
                    raise
                handler(error, pubsub, self)
        pubsub.close()


class RedisLock(utils.LockBase['RedisLock']):
    """An extremely reliable Redis lock based on pubsub.

    The lock is held by a subscription kept open by a keep-alive thread.

    As opposed to most Redis locking systems based on key/value pairs,
    this locking method is based on the pubsub system. The big advantage is
    that if the connection gets killed due to network issues, crashing
    processes or otherwise, it will still immediately unlock instead of
    waiting for a lock timeout.

    The flip side of that immediacy is handled too: the *holder* learns
    about a revocation as soon as its keep-alive thread observes the
    dead connection. `lost` turns True, `ensure_held` and the ``with``
    block exit raise `~portalocker.exceptions.LockLostError`, an
    optional `on_lost` callback fires, and (by default in 4.2, opt-in
    from 5.0.0) the main thread is interrupted. The subscription lives
    on a dedicated connection that never retries or reconnects, because
    a transparently resurrected subscription would be a silent
    re-acquisition; one consequence worth knowing is that redis-py's
    default ``socket_timeout`` turns a read stalled for five seconds
    into a loss.

    A note on ``os.fork``: a forked child inherits the lock object and
    the parent's sockets. The child's `release` (or garbage collection
    of its copy) only drops the child's local references; the network
    teardown is skipped in any process other than the one that
    subscribed, because an UNSUBSCRIBE over the inherited socket would
    silently revoke the *parent's* lock without the parent ever being
    told. A child that needs the lock must construct its own instance.

    To make sure both sides of the lock know about the connection state it is
    recommended to set the `health_check_interval` when creating the redis
    connection.

    Mixing versions on one channel has a known limitation: portalocker
    3.2.0 and older holders all share one connection name, so one live
    plus one crashed legacy holder cannot be told apart and the channel
    stays blocked until the crashed holder's TCP connection dies on its
    own (see `legacy_client_name`).

    The lock requires a single standalone Redis endpoint. Every
    acquisition decision starts from ``PUBSUB NUMSUB``, which is
    node-local in Redis Cluster and replica setups while message delivery
    is cluster-wide, so two writers subscribed through different nodes
    would each count one subscriber and both take the uncontended fast
    path. Point every participant at the same standalone server.

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
        on_lost: Callback invoked exactly once when a held lock is lost,
            with this lock as its only argument. It runs on the
            keep-alive worker thread, so keep it short, do not block in
            it, and do not take application locks inside it; anything it
            raises is caught and logged rather than propagated.
            Calling `release` on the lost lock inside the callback is
            allowed: the teardown skips joining the worker thread it is
            running on, and that thread exits on its own right after
            the callback returns. The
            loss is recorded first and the callback runs afterwards, so
            `lost` can already be `True` while the callback has not run
            yet: code that needs the callback to have completed must
            wait on the callback, not on `lost`. `None` (the default)
            disables the callback.
        interrupt_on_lost: Whether losing a held lock also interrupts
            the main thread with a `KeyboardInterrupt`. The default
            (`None`) currently behaves as `True` and emits a
            `DeprecationWarning` at the moment a loss actually triggers
            the interrupt: portalocker 5.0.0 flips the default to
            `False`, surfacing losses only through
            `~portalocker.exceptions.LockLostError`, `ensure_held`,
            `lost`, the ``with`` block exit and `on_lost`. Pass an
            explicit `True` or `False` to opt out of the warning.
            Delivery of the interrupt is best effort either way: it is
            a no-op under a custom ``SIGINT`` disposition, deferred
            while the main thread blocks in a C call, and catchable as
            an ordinary `KeyboardInterrupt`.
        subscription_connection_factory: Escape hatch for connection
            setups the built-in derivation cannot reproduce (Sentinel,
            cluster, custom pools). When given, every subscription
            attempt calls it for a fresh client instead of deriving one
            from the command connection, and the lock closes that
            client again when the attempt ends. The returned client
            must yield connections that do not retry or reconnect, or
            the loss guarantee above silently disappears.
        self_check_interval: Seconds between opt-in end-to-end
            self-checks of a held lock, or `None` (the default) for
            none. Socket-level loss detection cannot see a half-open
            link - a partition that never delivers a TCP reset - so
            when set, the holder periodically publishes a liveness ping
            to its own channel and requires its own reply back through
            the response-channel machinery within
            ``min(self_check_interval, unavailable_timeout)`` seconds.
            A failed check is a loss, classified and surfaced exactly
            like a socket error (`RedisLockSelfCheckError` as the
            ``__cause__``). Costs one publish, one short-lived
            response subscription and one channel-wide reply round per
            interval per holder, which is why it is opt-in. Note the
            check verifies the whole loop through the command
            connection too, so a holder whose command path is down
            also loses the lock - deliberately, since such a holder
            cannot answer other probers either.
        fencing: When `True`, every exclusive grant draws a
            monotonically increasing fencing token (``INCR`` on
            `fence_key`, stored in `fence_token`) that resources able
            to check fences can use to reject writes from a stale
            holder. Off by default because it reintroduces key state:
            the counter key never expires, by design, since
            monotonicity must survive idle periods. Shared grants
            never draw a token, and only grants from locks with
            fencing enabled bump the counter, so the ordering
            guarantee covers exactly the exclusive grants of
            fencing-enabled 4.2+ writers on the channel.

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
    #: Callback fired once per loss on the worker thread, or `None`.
    on_lost: typing.Callable[[RedisLock], None] | None
    #: Whether a loss also interrupts the main thread. Defaults to True
    #: in 4.2.0; the default flips to False in 5.0.0.
    interrupt_on_lost: bool
    #: Factory for the dedicated subscription client, or `None` to
    #: derive one from the command connection.
    subscription_connection_factory: (
        typing.Callable[[], redis.client.Redis] | None
    )
    #: Seconds between end-to-end self-checks of a held subscription,
    #: or `None` (the default) to run none. See `_self_check_tick`.
    self_check_interval: float | None
    #: Whether every exclusive grant draws a fencing token. See
    #: `fence_token`.
    fencing: bool
    #: Whether the caller chose `interrupt_on_lost` explicitly. When
    #: False, the 4.2.0 default of True is in effect and a loss that
    #: interrupts also announces the 5.0.0 default change.
    _interrupt_on_lost_set: bool
    #: The client owning the zero-retry subscription connection, built
    #: per attempt by `_start_subscription` and closed with it. Always
    #: owned by the lock, also when it came out of
    #: `subscription_connection_factory`.
    _subscription_client: redis.client.Redis | None
    #: The pid `_start_subscription` last ran in, or `None` without a
    #: subscription. A forked child inherits the lock object and its
    #: sockets, and a teardown running in a pid other than this one
    #: must only drop its local references: any socket operation - the
    #: UNSUBSCRIBE most of all - would act on the *parent's* live
    #: subscription, silently releasing a lock the parent still
    #: believes it holds (the same hazard redis-py's pools guard with
    #: ``_checkpid``). See `_in_subscribing_process`.
    _subscription_pid: int | None
    #: Where this instance is in its lifecycle. See `_LockState`.
    #: Together with `_lost_error` it is guarded by the ``_state_lock``
    #: every `utils.LockBase` instance owns (documented there as the
    #: guard for acquire/release state transitions, which is exactly
    #: what this is, and reinitialized in forked children so a fork
    #: taken while the worker thread holds it cannot deadlock the
    #: child). The worker thread records failures under it while
    #: `acquire` confirms ownership under it, which is what makes the
    #: loss-versus-confirm race safe (see `_confirm_held`).
    #: Deliberately separate from `_mode_lock`: that lock serializes
    #: the ``(mode, elected)`` snapshot taken for every ping answer, a
    #: hot path that must not contend with lifecycle transitions, and
    #: no code path ever holds both locks at once, so no lock ordering
    #: needs to exist between them.
    _lock_state: _LockState
    #: The error that killed the keep-alive worker, kept until the next
    #: `acquire` so `~portalocker.exceptions.LockLostError` can carry it
    #: as ``__cause__``. Guarded by `_state_lock`.
    _lost_error: BaseException | None
    #: Monotonic instant the next self-check is due. Meaningful only
    #: while the lock is held: armed by `_confirm_held`, re-armed by
    #: `_self_check_tick` after every passed check. Guarded by
    #: `_state_lock`.
    _next_self_check: float
    #: Token of the current or most recent fenced grant, or `None`.
    #: Guarded by `_state_lock`. See `fence_token`.
    _fence_token: int | None
    #: Serializes `mode` and `writer_elected` transitions with
    #: `channel_handler`'s snapshot of ``(holder_id, mode, elected)``.
    #: The handler runs on the worker thread while `acquire` promotes on
    #: the main thread. Without this lock a probe could read ``pending``
    #: from a writer already committed to promoting itself, or a torn
    #: ``(mode, elected)`` pair from the middle of a transition. Like
    #: the state lock it is reinitialized in forked children (reported
    #: through `_fork_reinit_locks`): the worker holds it for every ping
    #: answer, so a child forked inside that snapshot would otherwise
    #: hang forever on its first `release`.
    _mode_lock: threading.Lock

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
        on_lost: typing.Callable[[RedisLock], None] | None = None,
        interrupt_on_lost: bool | None = None,
        subscription_connection_factory: (
            typing.Callable[[], redis.client.Redis] | None
        ) = None,
        self_check_interval: float | None = None,
        fencing: bool = False,
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
                `fail_when_locked` for non-blocking behaviour. Also
                raised for a ``self_check_interval`` that is neither
                `None` nor positive.
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
        if self_check_interval is not None and self_check_interval <= 0:
            raise ValueError(
                'self_check_interval must be positive or None, got '
                f'{self_check_interval!r}'
            )
        self.self_check_interval = self_check_interval
        self.fencing = fencing
        self.holder_id = uuid.uuid4().hex
        self.writer_elected = False
        self.on_lost = on_lost
        # `None` means "the caller left the choice to the library": the
        # 4.2.0 default of True applies, and the loss that actually
        # triggers an interrupt announces the 5.0.0 default flip. An
        # explicit True or False opts out of that warning.
        self.interrupt_on_lost = (
            True if interrupt_on_lost is None else interrupt_on_lost
        )
        self._interrupt_on_lost_set = interrupt_on_lost is not None
        self.subscription_connection_factory = subscription_connection_factory
        self._subscription_client = None
        self._subscription_pid = None
        # Guarded by the base class's `_state_lock` (created in
        # `super().__init__` below) from the moment a worker thread can
        # exist; only the constructor may assign without holding it.
        self._lock_state = _LockState.IDLE
        self._lost_error = None
        self._next_self_check = 0.0
        self._fence_token = None
        # Guards every `mode` and `writer_elected` transition made once
        # a subscription can exist. Only the constructor runs strictly
        # before any worker thread, so only these two assignments above
        # and below skip the lock.
        self._mode_lock = threading.Lock()
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

    def _fork_reinit_locks(
        self,
    ) -> tuple[threading.Lock | threading.RLock, ...]:
        """Report the Python locks a forked child must reinitialize.

        Adds `_mode_lock` to the base class's state lock: the worker
        thread takes it for every ping snapshot and `acquire` for every
        promotion, so a fork landing inside either scope hands the
        child a lock owned by a thread that does not exist there, and
        the child's `release` (or garbage collection) would block on it
        forever. The after-fork hook resets each lock separately and
        never acquires them, so the no-path-holds-both-locks invariant
        between the state lock and the mode lock is untouched.

        Returns:
            The state lock and the mode lock.
        """
        return (*super()._fork_reinit_locks(), self._mode_lock)

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
        asked for and carries `holder_id`, the *current* `mode`,
        `REDIS_LOCK_PROTOCOL_VERSION` and an ``elected`` boolean
        mirroring `writer_elected`. Answering with the live mode rather
        than a stored one is what makes the protocol truthful: a writer
        that is still `RedisLockMode.PENDING` says so, and a probe
        therefore learns the state as it was at the moment it asked. The
        ``elected`` field is how an incumbent stays visible while it
        waits for readers to drain: later writers defer to an advertised
        election instead of rerunning the id sort against it (see
        `_writer_is_elected`). Records still carry protocol version 1,
        so portalocker 4.0 and 4.1 parse them unchanged and simply
        ignore the extra key.

        The ``(holder_id, mode, elected)`` triple is snapshotted under
        `_mode_lock`, the lock every promotion takes, and published
        outside it so no network I/O runs under the lock. That
        serializes each answer against promotions: an answer either
        completes its snapshot before a promotion starts or observes the
        promoted state, and it can never read `mode` halfway through a
        transition. An answer snapshotted just before a promotion still
        truthfully reports the older mode. The prober's own count checks
        are what invalidate a decision built on such an answer.

        A probing lock is subscribed to its own channel, so it answers
        its own ping and appears in its own holder list.

        Args:
            message: A redis-py pubsub message. Only messages of type
                ``message`` are answered; subscribe confirmations and
                other control frames are dropped.
        """
        if message.get('type') != 'message':
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

        connection: redis.client.Redis | None = self.connection
        if connection is None:
            # The connection must outlive every worker thread, so this is
            # unreachable unless a teardown raced the handler. Dropping
            # the ping makes this holder look unavailable to the prober,
            # which is recoverable; raising here would land in
            # `_on_worker_exception`, which classifies it as a loss for
            # a held lock (killing this holder's own subscription over a
            # ping it could simply have dropped) or as a burnt attempt
            # for a waiter. An `assert` would also be stripped under -O
            # and degrade into an `AttributeError` with the same
            # escalation.
            logger.error(
                'Redis lock %s cannot answer ping: connection is closed',
                self.holder_id,
            )
            return
        with self._mode_lock:
            holder_id: str = self.holder_id
            mode: RedisLockMode = self.mode
            elected: bool = self.writer_elected
        connection.publish(
            response_channel,
            json.dumps(
                {
                    'holder_id': holder_id,
                    'mode': mode.value,
                    'protocol': REDIS_LOCK_PROTOCOL_VERSION,
                    'elected': elected,
                }
            ),
        )

    @property
    def client_name(self) -> str:
        """Name given to this holder's subscriber connection.

        `_make_subscription_client` sets this as the connection-level
        ``client_name`` of the dedicated subscription client, so the
        name is part of the handshake of the connection that actually
        holds the subscription and shows up against it in ``CLIENT
        LIST``. `_kill_unavailable_locks` reads it back the other way
        around: a listed connection whose name carries a `holder_id`
        that did not answer the last ping belongs to a crashed holder,
        and killing it releases the lock.

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

        One legacy mix stays unresolvable, because legacy holders are
        indistinguishable by name: with one live and one crashed 3.2.0
        holder on the same channel, the live reply spares every legacy
        connection from reaping, so waiters stay blocked until the
        crashed holder's TCP connection dies on its own.

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

        - The first yield is immediate, so an uncontended acquire makes
          its first attempt without waiting out an interval. The poll
          loops driven by this generator do not busy-spin as a result:
          their ``get_message(timeout=...)`` calls already block for
          the check interval when no message is waiting.
        - Every interval between attempts is scaled by a random factor
          in ``[0.5, 1.5)``. Contenders that started together would
          otherwise retry in lockstep and keep colliding round after
          round; the jitter spreads them out so one of them gets a
          clean probe.
        - The deadline is taken from `time.monotonic`, so adjusting the
          system clock mid-wait cannot stretch or cut short a timeout.

        Like the base class it always yields at least once, even with a
        zero or negative timeout, so ``fail_when_locked`` still makes one
        real attempt rather than failing without asking.

        Args:
            timeout: Seconds to keep yielding for. `None` means zero,
                which still yields exactly once.
            check_interval: Base seconds to sleep between attempts.
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
        effective_interval: float = (
            check_interval if check_interval > 0 else self.thread_sleep_time
        )
        deadline: float = time.monotonic() + timeout
        yield 0
        while time.monotonic() < deadline:
            time.sleep(effective_interval * (0.5 + random.random()))
            yield 0

    def _make_subscription_client(
        self,
        connection: redis.client.Redis,
    ) -> redis.client.Redis:
        """Build the dedicated client the subscription will live on.

        The subscription is the lock, so its connection follows a
        stricter policy than the command connection (#137):

        - ``retry=Retry(NoBackoff(), 0, supported_errors=())``. redis-py
          wraps every pubsub read in the connection's retry policy, and
          its failure callback reconnects *before* the retry budget is
          checked, so even ``retries=0`` resurrects the connection once.
          A reconnected pubsub resubscribes with its handlers intact,
          which silently re-acquires a lock this holder may have lost to
          somebody else in the meantime. Only an empty
          ``supported_errors`` tuple makes the retry machinery catch
          nothing at all, so a dead socket kills the subscription with
          zero reconnects and the worker reports the loss instead.
          ``retry_on_error`` and ``retry_on_timeout`` are cleared too,
          because redis-py merges them back into the retry policy's
          supported errors.
        - ``client_name`` at the connection level, so the name is part
          of the handshake rather than a separately sent command.
        - ``protocol=2`` with the maintenance-notification kwargs
          stripped, because RESP3 maintenance notifications drive a
          second reconnect path in redis-py's pubsub that ignores the
          retry policy entirely.
        - Decoded responses, because `channel_handler` compares the
          decoded channel name and payload.

        ``health_check_interval`` is deliberately *not* overridden: the
        subscription inherits whatever the command connection uses, as
        it did when both lived on one connection. A lock-created
        connection carries the `DEFAULT_REDIS_KWARGS` value of ten
        seconds through the clone, and a caller-supplied connection
        keeps the caller's choice, which is what the module docs ask
        them to set. Forcing the lock's default onto a connection whose
        owner chose otherwise also has a nasty failure mode on
        fakeredis: its ``read_response`` never advances redis-py's
        ``next_health_check`` clock, so any non-zero interval makes the
        worker send a health-check ``PING`` on every ``get_message``
        poll (on the order of 100k per second), and the spinning worker
        starves the command connection's ``PUBSUB NUMSUB`` for tens of
        milliseconds.

        The client is built on a fresh connection pool cloned from the
        command connection's pool (same connection class, same
        connection arguments, the overrides above applied), never by
        mutating a pooled connection: a mutated connection would go back
        into the caller's pool on release and hand some later, unrelated
        command a zero-retry connection named like a lock holder, which
        the reaper would then kill. Cloning the pool works for both a
        connection the lock created itself and one the caller supplied,
        including one built around a custom pool.

        Args:
            connection: The command connection to derive the
                subscription client from. Only its pool's class and
                connection arguments are read; the connection itself is
                not touched.

        Returns:
            A client owned by this lock, with
            `subscription_connection_factory` taking precedence over the
            derivation when the caller supplied one.

        Raises:
            ~portalocker.exceptions.LockException: The pool could not be
                cloned, typically because an exotic pool class takes
                constructor arguments this derivation does not know
                about, or because a cluster-style client carries no
                connection pool at all. The message points at
                `subscription_connection_factory`, which exists for
                exactly that situation.
        """
        if self.subscription_connection_factory is not None:
            return self.subscription_connection_factory()

        try:
            pool: redis.connection.ConnectionPool = connection.connection_pool
            # `get_connection_kwargs` is annotated as a bare Dict in
            # redis-py; the cast restores the real shape.
            connection_kwargs: dict[str, typing.Any] = typing.cast(
                'dict[str, typing.Any]',
                connection.get_connection_kwargs(),
            )
            subscription_kwargs: dict[str, typing.Any] = {
                key: value
                for key, value in connection_kwargs.items()
                # The maintenance-notification machinery is RESP3-only
                # and rejects (or bypasses) the RESP2 zero-retry setup
                # below.
                if 'maint' not in key and key != 'connection_class'
            }
            subscription_kwargs.update(
                retry=redis.retry.Retry(
                    redis.backoff.NoBackoff(),
                    retries=0,
                    supported_errors=(),
                ),
                retry_on_error=[],
                retry_on_timeout=False,
                client_name=self.client_name,
                protocol=2,
                decode_responses=True,
            )
            subscription_pool: redis.connection.ConnectionPool = type(pool)(
                connection_class=pool.connection_class,
                **subscription_kwargs,
            )
        except (TypeError, AttributeError) as error:
            # TypeError: an exotic pool class rejects the clone kwargs.
            # AttributeError: a cluster-style client carries no
            # connection pool at all.
            raise exceptions.LockException(
                exceptions.LockException.LOCK_FAILED,
                'RedisLock could not derive a subscription client from '
                f'a {type(connection).__name__} connection; pass '
                'subscription_connection_factory to build one yourself',
            ) from error
        return redis.client.Redis(connection_pool=subscription_pool)

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

        1. The subscription gets its own client (see
           `_make_subscription_client`): connection-level
           ``client_name`` so the connection that actually holds the
           subscription is the one `_kill_unavailable_locks` finds in
           ``CLIENT LIST``, and a zero-reconnect retry policy so a
           revoked subscription dies loudly instead of resurrecting
           itself. The command connection is left completely alone.
        2. The subscription is registered with `channel_handler` as its
           callback, so pings are answered from now on.
        3. The server's subscribe confirmation is drained here, on the
           calling thread, before the worker thread exists (see
           `_wait_for_subscribe_confirmation`). Processing it proves the
           server registered the subscription, so the ``PUBSUB NUMSUB``
           that `acquire` runs next is guaranteed to count this holder.
           Without that proof a delayed ``SUBSCRIBE`` would let `acquire`
           read ``subscribers == 1`` while another holder exists and take
           the uncontended fast path against a contended channel.
        4. A `PubSubWorkerThread` starts reading, with
           `_on_worker_exception` registered as its exception handler,
           so any failure of the reader - `BaseException` included -
           lands in the loss classifier instead of dying with the
           thread. It is a daemon thread: an unreleased lock must never
           keep the interpreter alive, and since losing the connection
           *is* releasing the lock, dying at process exit is the
           correct behaviour rather than a leak.

        Any failure rolls the whole thing back through `_unsubscribe`
        before re-raising, leaving `pubsub` as `None`. Without that
        rollback a failed `acquire` would leave half a subscription
        behind and the already-active guard at the top of `acquire`
        would refuse every later retry on the same object. The rollback
        deliberately keeps the command connection: `acquire` retries a
        transient failure on that same connection, and closing a
        lock-created connection here would leave the retry holding a
        subscription whose ping handler has nothing left to answer on
        (the terminal cleanup for errors that do propagate lives in
        `_try_subscribe`). A rollback that fails as well - usually the
        same dead Redis that broke the subscribe - is logged rather
        than raised, so the original error is what propagates.

        A leftover `_lost_error` from a worker that died during the
        previous teardown is cleared before subscribing, so the fresh
        attempt cannot be refused by `_confirm_held` over a failure
        that belonged to a subscription which no longer exists. The
        current pid is recorded as `_subscription_pid`, which is what
        lets a later teardown detect that it runs in a forked child.

        Args:
            connection: The command connection the subscription client
                is derived from.

        Raises:
            ~portalocker.exceptions.LockException: The server did not
                confirm the subscription within `unavailable_timeout`
                seconds, raised after the rollback.
            Exception: Anything the Redis client raises while
                connecting, subscribing or starting the thread,
                re-raised unchanged after the rollback. `acquire`
                treats a ``redis_exceptions.ConnectionError`` or
                ``redis_exceptions.TimeoutError`` from here as one
                failed attempt and retries within its timeout budget.
        """
        with self._state_lock:
            self._lost_error = None
        subscription_client: redis.client.Redis = (
            self._make_subscription_client(connection)
        )
        self._subscription_client = subscription_client
        self._subscription_pid = os.getpid()
        pubsub: redis.client.PubSub = self._get_pubsub(subscription_client)
        self.pubsub = pubsub
        try:
            pubsub.subscribe(**{self.channel: self.channel_handler})
            self._wait_for_subscribe_confirmation(pubsub)
            # A daemon thread so an unreleased lock can never block
            # interpreter exit; losing the connection releases the lock by
            # design, which is exactly what process exit should do.
            self.thread = PubSubWorkerThread(
                pubsub,
                sleep_time=self.thread_sleep_time,
                daemon=True,
                exception_handler=self._on_worker_exception,
                # No tick at all without an interval, so the default
                # read loop stays byte-for-byte what it always was.
                tick=(
                    self._self_check_tick
                    if self.self_check_interval is not None
                    else None
                ),
            )
            self.thread.start()
        except Exception:
            # The subscribe or thread start failure is the error worth
            # reporting. A rollback that fails as well is logged so it
            # cannot replace the original cause.
            try:
                self._unsubscribe()
            except Exception:
                logger.warning(
                    'Redis lock %s failed to roll back a broken subscription',
                    self.holder_id,
                    exc_info=True,
                )
            raise

    def _wait_for_subscribe_confirmation(
        self,
        pubsub: redis.client.PubSub,
    ) -> None:
        """Block until the server confirms the channel subscription.

        Redis sends a ``subscribe`` frame after it has registered the
        subscription, so reading that frame here establishes a
        happens-before edge: any command issued afterwards - in
        particular the ``PUBSUB NUMSUB`` in `acquire` - runs against a
        server that already counts this holder. This wait replaces the
        ``time.sleep(0.01)`` that used to stand in for it, which bounded
        nothing: a single TCP retransmit delays a ``SUBSCRIBE`` far
        longer than 10ms.

        Note that redis-py's ``PubSub.subscribed`` is no substitute: it
        is set the moment the ``SUBSCRIBE`` command is *sent*, not when
        the server confirms it.

        A ping that arrives while draining is not lost: ``get_message``
        dispatches it to `channel_handler` and returns `None`, so the
        loop keeps waiting for the confirmation frame while the ping is
        answered as usual.

        Args:
            pubsub: The freshly subscribed pubsub to read frames from,
                before any worker thread consumes them invisibly.

        Raises:
            ~portalocker.exceptions.LockException: No confirmation
                arrived within `unavailable_timeout` seconds. Proceeding
                without it would reopen the miscount this wait exists to
                prevent, so the subscription attempt fails instead.
        """
        check_interval: float = min(
            self.thread_sleep_time,
            self.unavailable_timeout / 10,
        )
        deadline: float = time.monotonic() + self.unavailable_timeout
        first: bool = True
        while first or time.monotonic() < deadline:
            first = False
            confirmation: dict[str, typing.Any] | None = typing.cast(
                'dict[str, typing.Any] | None',
                pubsub.get_message(timeout=check_interval),
            )
            if confirmation and confirmation.get('type') == 'subscribe':
                return
        raise exceptions.LockException(
            exceptions.LockException.LOCK_FAILED,
            'Redis did not confirm the lock channel subscription within '
            f'{self.unavailable_timeout} seconds',
        )

    def _on_worker_exception(
        self,
        error: BaseException,
        pubsub: redis.client.PubSub,
        worker: redis.client.PubSubWorkerThread,
    ) -> None:
        """Classify a keep-alive worker failure and escalate a loss.

        Registered as the redis-py ``exception_handler`` of the worker
        thread, so it runs *on* that thread for anything the reader
        raises, `BaseException` included (redis-py catches that wide
        deliberately, and a `KeyboardInterrupt` landing on the worker
        must not vanish either). `PubSubWorkerThread.run` additionally
        routes anything that escapes the handler itself back in here, so
        a bug in this method still ends in the classifier on its second
        pass instead of dying silently (#141).

        What happens depends on where the lock is in its lifecycle,
        decided under `_state_lock`:

        - `_LockState.HELD`: the lock is lost. The state moves to
          `_LockState.LOST`, the error is recorded for
          `~portalocker.exceptions.LockLostError`, `on_lost` fires, and
          when `interrupt_on_lost` is set the main thread is
          interrupted. Connection errors log as an error without a
          traceback (an expected lifecycle event, just a bad one),
          anything else logs with the traceback because it is a handler
          or library bug.
        - Any other state: the failure is scoped to the running acquire
          attempt. It is recorded so `acquire` notices, logged as a
          warning, and nothing escalates - a waiter that loses its
          subscription simply retries (#141).
        - `_LockState.LOST` already: a second failure from the same
          teardown (usually the ``pubsub.close()`` after the loop dying
          on the same dead socket). The first error is kept and the
          repeat is logged at debug level.

        The worker is always told to stop, which makes redis-py's read
        loop exit and close the pubsub.

        Args:
            error: What the reader (or a previous pass of this handler)
                raised.
            pubsub: The pubsub the worker was reading; unused, part of
                the redis-py handler signature.
            worker: The worker thread to stop.
        """
        del pubsub  # Part of the redis-py handler signature only.
        with self._state_lock:
            already_lost: bool = self._lock_state is _LockState.LOST
            if self._lost_error is None:
                self._lost_error = error
            was_held: bool = self._lock_state is _LockState.HELD
            if was_held:
                self._lock_state = _LockState.LOST
        worker.stop()
        if already_lost:
            logger.debug(
                'Redis lock %s worker raised again after the loss was '
                'recorded: %r',
                self.holder_id,
                error,
            )
            return
        connection_lost: bool = isinstance(error, _CONNECTION_LOSS_ERRORS)
        if was_held:
            if connection_lost:
                logger.error(
                    'Redis lock %s lost its subscription connection while '
                    'holding channel %r: %r',
                    self.holder_id,
                    self.channel,
                    error,
                )
            else:
                logger.error(
                    'Redis lock %s worker failed while holding channel %r, '
                    'the lock is lost',
                    self.holder_id,
                    self.channel,
                    exc_info=error,
                )
            self._fire_on_lost()
            if self.interrupt_on_lost:
                # The interrupt goes first: users who escalate
                # DeprecationWarning to an error would otherwise lose
                # the documented interrupt to their warning filter.
                _thread.interrupt_main()
                if not self._interrupt_on_lost_set:
                    # stacklevel 1 on purpose: this runs on the worker
                    # thread, where the caller frames belong to
                    # redis-py's read loop, so pointing higher would
                    # attribute the warning to redis-py. The message
                    # names the lock and channel instead.
                    warnings.warn(
                        f'portalocker.RedisLock on channel '
                        f'{self.channel!r} lost a held lock and '
                        'interrupted the main thread because '
                        'interrupt_on_lost defaults to True in '
                        'portalocker 4.2. portalocker 5.0.0 flips that '
                        'default to False; pass interrupt_on_lost '
                        'explicitly to keep or drop the interrupt.',
                        DeprecationWarning,
                        stacklevel=1,
                    )
        elif connection_lost:
            logger.warning(
                'Redis lock %s lost its subscription while waiting for '
                'channel %r, the attempt is retried: %r',
                self.holder_id,
                self.channel,
                error,
            )
        else:
            logger.warning(
                'Redis lock %s worker failed while waiting for channel %r, '
                'the attempt is retried',
                self.holder_id,
                self.channel,
                exc_info=error,
            )

    def _fire_on_lost(self) -> None:
        """Invoke the `on_lost` callback, containing whatever it raises.

        Runs on the worker thread as part of the HELD to LOST
        transition. A callback failure must not break that transition,
        the main-thread interrupt that follows it, or the thread
        teardown around it, so anything the callback raises - including
        a `BaseException` such as `SystemExit`, which would otherwise
        skip the interrupt and redis-py's post-loop ``pubsub.close()`` -
        is logged with its traceback and swallowed.
        """
        callback: typing.Callable[[RedisLock], None] | None = self.on_lost
        if callback is None:
            return
        try:
            callback(self)
        except BaseException:  # noqa: BLE001
            logger.exception(
                'Redis lock %s on_lost callback failed',
                self.holder_id,
            )

    def _self_check_tick(self, held_pubsub: redis.client.PubSub) -> None:
        """Run one due self-check, or nothing at all.

        Registered as the keep-alive worker's per-iteration tick, and
        only when ``self_check_interval`` is set, so it runs on the
        worker thread between subscription reads: the natural cadence
        for a periodic holder-side check, and the one place that needs
        no third thread. While the lock is anything but
        `_LockState.HELD` this is a no-op - an idle, acquiring or lost
        lock has no held subscription whose delivery could be
        verified - and while held, a check runs once the gate armed by
        `_confirm_held` expires, re-arming it after every passed check.

        A failed check raises out of this method into the worker's
        read loop, which routes it through `_on_worker_exception`
        exactly like a socket error: `RedisLockSelfCheckError` is a
        ``redis.exceptions.ConnectionError``, so the loss is
        classified as a connection loss and every loss channel behaves
        identically to a socket-detected revocation.

        Both reads of the gate run under `_state_lock`, the same lock
        `_confirm_held` arms it under. `_mode_lock` is never taken
        here, so the no-path-holds-both-locks invariant between the
        two locks stands.

        Args:
            held_pubsub: The held subscription the worker is reading.
                Passed through to `_run_self_check`, which keeps
                servicing it so pings - the check's own included - are
                still answered while the check waits for its reply.
        """
        # The tick is only registered when the interval is set, so the
        # cast states an invariant rather than an assumption.
        interval: float = typing.cast('float', self.self_check_interval)
        with self._state_lock:
            if self._lock_state is not _LockState.HELD:
                return
            if time.monotonic() < self._next_self_check:
                return
        try:
            self._run_self_check(held_pubsub)
        except _SelfCheckAbandoned:
            # The lock was released mid-check. There is nothing left
            # to verify and nothing to escalate.
            return
        with self._state_lock:
            self._next_self_check = time.monotonic() + interval

    def _run_self_check(self, held_pubsub: redis.client.PubSub) -> None:
        """Verify the held subscription end to end, or raise.

        Publishes an ordinary liveness ping to the lock's own channel -
        the same wire shape `_collect_lock_holders` sends, carrying a
        private single-use response channel - and requires this
        holder's *own* reply to arrive through that response channel in
        time. The reply can only arrive when the whole loop works: the
        publish reached the server, the server delivered the ping to
        the held subscription, `channel_handler` answered it, and the
        answer travelled back through a fresh subscription. That is
        what closes the half-open-link hole socket-level detection
        leaves open: a subscription whose socket is open but silently
        delivers nothing fails this check within one interval instead
        of surviving until the kernel gives up on retransmits.

        The reply deadline is ``min(self_check_interval,
        unavailable_timeout)``: `unavailable_timeout` is already the
        protocol's bound on how long a healthy holder may take to
        answer a ping - any contended prober reaps this holder after
        that long - and capping at the interval keeps at most one
        check in flight per interval. The deadline covers the response
        channel's subscribe confirmation too, so a command path that
        cannot even set up the return channel fails the check as well.
        Deliberate, because such a holder cannot answer other probers
        either, though it does mean a command-connection outage can
        cost a hold whose subscription was actually fine.

        The check never disturbs the channel: it reads no subscriber
        count, reaps nobody, and leaves the held subscription alone
        apart from servicing it. Other holders answer the ping like
        any probe ping and their replies are ignored.

        Args:
            held_pubsub: The held subscription to keep servicing while
                the check waits.

        Raises:
            RedisLockSelfCheckError: The subscribe confirmation or this
                holder's own reply did not arrive within the deadline.
            _SelfCheckAbandoned: The lock stopped being held mid-check
                (propagated from `_await_self_check_frame`).
        """
        budget: float = min(
            typing.cast('float', self.self_check_interval),
            self.unavailable_timeout,
        )
        deadline: float = time.monotonic() + budget
        poll: float = min(self.thread_sleep_time, budget / 10)
        connection: redis.client.Redis = self.get_connection()
        response_channel: str = f'{self.channel}-{uuid.uuid4().hex}'
        response_pubsub: redis.client.PubSub = self._get_pubsub(connection)
        try:
            response_pubsub.subscribe(response_channel)
            if self._await_self_check_frame(
                response_pubsub,
                held_pubsub,
                deadline,
                poll,
                # The confirmation proves the server registered the
                # response subscription, so the reply to the ping
                # published next cannot be dropped for want of a
                # listener (the same discipline as
                # `_collect_lock_holders`).
                lambda frame: frame.get('type') == 'subscribe',
            ):
                connection.publish(
                    self.channel,
                    json.dumps(
                        {
                            'message': 'ping',
                            'response_channel': response_channel,
                        }
                    ),
                )
                if self._await_self_check_frame(
                    response_pubsub,
                    held_pubsub,
                    deadline,
                    poll,
                    self._is_own_probe_reply,
                ):
                    return
            raise RedisLockSelfCheckError(
                f'Redis lock {self.holder_id} did not receive its own '
                f'self-check reply on channel {self.channel!r} within '
                f'{budget} seconds'
            )
        finally:
            response_pubsub.close()

    def _await_self_check_frame(
        self,
        response_pubsub: redis.client.PubSub,
        held_pubsub: redis.client.PubSub,
        deadline: float,
        poll: float,
        predicate: typing.Callable[[dict[str, typing.Any]], bool],
    ) -> bool:
        """Wait for a response-channel frame matching ``predicate``.

        Each polling interval drains every buffered response frame and
        then services the held subscription with one non-blocking
        read. The servicing is what makes the self-check sound rather
        than self-defeating: this method runs on the worker thread,
        whose ordinary read loop is paused for the duration, and the
        check's own ping arrives through exactly that held
        subscription - without the read here, `channel_handler` would
        never see the ping and every check would time out. Other
        probers' pings keep being answered through the same read.

        Args:
            response_pubsub: The check's own response subscription.
            held_pubsub: The held subscription to service every
                interval.
            deadline: Monotonic instant at which the wait gives up.
            poll: Seconds each response read may block, pacing the
                loop.
            predicate: Decides whether a frame is the one awaited.

        Returns:
            True when a matching frame arrived, False when the
            deadline passed first.

        Raises:
            _SelfCheckAbandoned: The lock left `_LockState.HELD`,
                which a concurrent `release` does before it stops the
                worker. The check is moot then and must declare
                neither success nor loss.
        """
        while time.monotonic() < deadline:
            with self._state_lock:
                if self._lock_state is not _LockState.HELD:
                    raise _SelfCheckAbandoned
            frame: dict[str, typing.Any] | None = typing.cast(
                'dict[str, typing.Any] | None',
                response_pubsub.get_message(timeout=poll),
            )
            while frame is not None:
                if predicate(frame):
                    return True
                frame = typing.cast(
                    'dict[str, typing.Any] | None',
                    response_pubsub.get_message(timeout=0),
                )
            held_pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=0,
            )
        return False

    def _is_own_probe_reply(self, frame: dict[str, typing.Any]) -> bool:
        """Report whether ``frame`` carries this holder's own record.

        A self-check ping is an ordinary probe ping, so every holder
        on the channel answers it, and only the echo of this lock's
        own `channel_handler` proves the delivery path. Foreign replies
        parse to their own ids, and a frame that does not parse as a
        protocol record at all parses to a synthetic ``legacy-<n>``
        id, which can never equal a uuid4 hex `holder_id`, so both are
        ignored by the caller.

        Args:
            frame: A pubsub frame from the response channel.

        Returns:
            True when the frame is this holder's reply.
        """
        holder: RedisLockHolder = self._parse_lock_response(
            frame.get('data'),
            0,
        )
        return holder.holder_id == self.holder_id

    def _confirm_held(self) -> bool:
        """Promote a won acquisition to `_LockState.HELD`, or refuse.

        The confirm handshake that closes the flag-transition race: the
        subscription connection can die in the microseconds between the
        probe result that decided the acquisition and the state update
        that records it. Both `acquire` success sites call this under
        `_state_lock`, the same lock `_on_worker_exception` takes, so
        exactly two interleavings exist. Handler first: the error is
        recorded while the state is still `_LockState.ACQUIRING`, the
        handler takes its quiet attempt-scoped path, and this method
        sees the error and refuses. Confirm first: the state is
        `_LockState.HELD` by the time the handler runs, so the loud
        LOST path fires. In neither ordering does `acquire` return
        success with a dead worker and no notification.

        The liveness check additionally covers a worker that died
        without the handler running at all, which redis-py permits when
        ``pubsub.close()`` raises after a clean stop.

        Returns:
            True when the lock is now held. False when the attempt must
            be treated as failed because the worker already died or
            recorded an error.
        """
        thread: PubSubWorkerThread | None = self.thread
        with self._state_lock:
            if self._lost_error is not None:
                return False
            if thread is None or not thread.is_alive():
                return False
            self._lock_state = _LockState.HELD
            if self.self_check_interval is not None:
                # Arm the self-check gate: the first check runs one
                # full interval into the hold, not at the instant of
                # the grant the confirm probe just verified.
                self._next_self_check = (
                    time.monotonic() + self.self_check_interval
                )
            return True

    def _waiting_attempt_failed(self) -> bool:
        """Report whether the current attempt's worker is already dead.

        Checked by `acquire` before reusing an existing subscription for
        the next attempt. A recorded error or a worker thread that is no
        longer alive both mean the subscription backing this attempt is
        gone: Redis dropped the subscriber with the connection, so
        continuing to probe on top of it would wait on a lock this
        process is no longer counted for.

        Returns:
            True when the attempt must be abandoned and the next one
            should subscribe from scratch.
        """
        with self._state_lock:
            if self._lost_error is not None:
                return True
        thread: PubSubWorkerThread | None = self.thread
        return thread is None or not thread.is_alive()

    def _abandon_failed_attempt(self) -> None:
        """Consume a failed attempt so the next one starts from scratch.

        Clears the recorded worker error, resets the lifecycle to
        `_LockState.ACQUIRING`, rolls an exclusive lock's mode back to
        `RedisLockMode.PENDING` (forgetting any election, which a lock
        without a live subscription may not advertise), and tears the
        dead subscription down. The teardown is best effort: it usually
        runs against the same dead connection that killed the attempt,
        so a teardown failure is logged rather than allowed to abort
        the retry loop that exists to survive exactly these failures.

        Also used by `acquire` to reset an instance whose previous hold
        ended in `_LockState.LOST`, which is what makes lost instances
        reusable.
        """
        with self._state_lock:
            self._lost_error = None
            self._lock_state = _LockState.ACQUIRING
            # A token drawn for a grant whose confirm was then refused
            # belongs to no hold. The INCR it burned is a harmless gap
            # in the counter.
            self._fence_token = None
        if self.flags == constants.LockFlags.EXCLUSIVE:
            with self._mode_lock:
                self.mode = RedisLockMode.PENDING
                self.writer_elected = False
        try:
            self._unsubscribe()
        except Exception:
            logger.warning(
                'Redis lock %s failed to tear down a dead subscription '
                'attempt',
                self.holder_id,
                exc_info=True,
            )

    @property
    def lost(self) -> bool:
        """Whether a held lock was revoked and the loss is unhandled.

        True from the moment the keep-alive worker observed the
        revocation until the next `acquire` resets the instance.
        Deliberately still True after `release`, so code using bare
        ``acquire()``/``release()`` can check it afterwards; the raising
        counterpart is `ensure_held`.

        Returns:
            True when the lock is in the lost state.
        """
        with self._state_lock:
            return self._lock_state is _LockState.LOST

    def ensure_held(self) -> None:
        """Raise if the lock was lost, return quietly otherwise.

        The check to sprinkle through a long critical section: cheap
        (one mutex acquisition, no network traffic), and the only way a
        loss interrupts a running body deterministically, since the
        optional main-thread interrupt is best effort by nature.

        This reports revocation, not acquisition: it also returns
        quietly on a lock that is idle or still acquiring, so calling
        it only makes sense between a successful `acquire` and the
        matching `release`.

        Raises:
            ~portalocker.exceptions.LockLostError: The lock was revoked
                while held. The error that killed the subscription is
                attached as ``__cause__``.
        """
        if self.lost:
            raise self._lock_lost_error()

    def _lock_lost_error(self) -> exceptions.LockLostError:
        """Build the `LockLostError` describing this lock's loss.

        Returns:
            The error, carrying `channel` and `holder_id`, with the
            exception that killed the keep-alive worker attached as
            ``__cause__`` so tracebacks show the revocation and its
            cause as one chain.
        """
        with self._state_lock:
            cause: BaseException | None = self._lost_error
        error: exceptions.LockLostError = exceptions.LockLostError(
            exceptions.LockException.LOCK_FAILED,
            f'Redis lock {self.holder_id} on channel {self.channel!r} was '
            'revoked while held',
            channel=self.channel,
            holder_id=self.holder_id,
        )
        error.__cause__ = cause
        return error

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: typing.Any,
    ) -> bool | None:
        """Release the lock, raising `LockLostError` for a silent loss.

        A lock that was lost mid-block and whose body nevertheless
        finished cleanly would otherwise end the ``with`` statement
        looking successful, which is exactly the silent divergence a
        revoked lock must not produce. The loss is therefore re-raised
        here - after the release, so the teardown always runs - but only
        when no exception is already propagating out of the body: the
        body's own failure is the more specific signal and must not be
        masked (the loss stays observable through `lost` either way).
        `lost` is read *after* the release: the LOST state is sticky
        through `release`, so reading it afterwards also catches a
        revocation that lands in the instant the block is already
        exiting.

        A release failure follows `Lock.__exit__`'s discipline: with an
        exception already leaving the body it is chained onto that
        exception as its ``__context__`` (with a note attached) instead
        of replacing it, and only with a clean body does the release
        error itself propagate.

        Args:
            exc_type: Type of the exception leaving the block, if any.
            exc_value: The exception instance, if any.
            traceback: The traceback of that exception, if any.

        Returns:
            `None`, so a body exception keeps propagating once the lock
            has been released.

        Raises:
            ~portalocker.exceptions.LockLostError: The lock was revoked
                while the block ran and the block raised nothing itself.
            Exception: Whatever `release` raises, but only when the
                block itself ended without an exception.
        """
        try:
            self.release()
        except Exception as release_error:
            if exc_value is None:
                # Nothing to mask, the release error is the only
                # failure. A loss stays observable through `lost`.
                raise
            utils._chain_release_error(  # pyright: ignore[reportPrivateUsage]
                exc_value,
                release_error,
            )
            return None
        if exc_type is None and self.lost:
            raise self._lock_lost_error()
        return None

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

        The optional ``elected`` key is read as a strict boolean.
        Anything else, including its absence, parses as `None`, which
        `RedisLockHolder.elected` documents as "the holder predates the
        field". Records from portalocker 4.0 and 4.1 lack the key, so
        they land on `None` here without falling back to a legacy
        holder: the record itself is still a perfectly valid protocol
        version 1 record.

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
                elected_value: typing.Any = data.get('elected')
                elected: bool | None = (
                    elected_value if isinstance(elected_value, bool) else None
                )
                return RedisLockHolder(
                    holder_id=holder_id,
                    mode=RedisLockMode(mode),
                    elected=elected,
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
        - `legacy_client_name` plus a `holder_id` suffix. The suffix must
          be exactly 32 lowercase hex characters, the shape of the uuid4
          hex `holder_id` every lock allocates, and the holder is killed
          unless that exact id replied.

        The suffix shape is validated because channel names may
        themselves contain ``-lock-``. A holder of channel ``a-lock-b``
        is named ``a-lock-b-lock-<id>``, which starts with channel
        ``a``'s prefix but does not parse as one of its holders. A bare
        prefix match here used to kill those healthy neighbours, along
        with any unrelated client that coincidentally shared the prefix
        (#142).

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
        # fakeredis rejects `client_list('pubsub')`, so the list stays
        # unfiltered and the name shape does all the narrowing.
        holder_name_pattern: re.Pattern[str] = re.compile(
            re.escape(self.legacy_client_name) + '-(?P<holder_id>[0-9a-f]{32})'
        )
        clients: list[dict[str, str]] = connection.client_list()
        for client_ in clients:
            client_name: str = client_.get('name', '')
            match: re.Match[str] | None = holder_name_pattern.fullmatch(
                client_name,
            )
            unavailable: bool = (
                client_name == self.legacy_client_name and not legacy_responded
            ) or (
                match is not None
                and match.group('holder_id') not in responding_holder_ids
            )
            if unavailable:
                logger.warning(
                    'Killing unavailable redis client: %r',
                    client_,
                )
                connection.client_kill_filter(client_.get('id'))

    def _drain_probe_replies(
        self,
        pubsub: redis.client.PubSub,
        holders: dict[str, RedisLockHolder],
        expected_subscribers: int,
        check_interval: float,
    ) -> None:
        """Read every buffered reply within one polling interval.

        Reading a single message per interval would cap the throughput
        at one reply per interval, and a channel with more holders than
        intervals could then never be probed conclusively, so every
        probe would kill healthy holders (#138). The first read waits up
        to `check_interval` for a reply, the follow-up reads first empty
        the local buffer at ``timeout=0`` and then, when that comes up
        empty, poll once with the short `_PROBE_REPLY_GRACE` timeout.
        Holders answer one after another, so the next reply is usually a
        few milliseconds behind the previous one: without the grace poll
        the ``timeout=0`` read missed it almost every time and the probe
        slept a full jittered drain interval per reply, which is what
        stretched every reply-staleness window in this protocol to
        around a hundred milliseconds (#145). Draining stops early once
        `expected_subscribers` distinct holders have replied.

        Args:
            pubsub: The probe's own subscription to read replies from.
            holders: The replies collected so far, keyed by holder id
                and extended in place. The number of legacy entries in
                it doubles as the index for the next synthetic
                ``legacy-<n>`` id.
            expected_subscribers: Reply count at which draining stops.
            check_interval: Seconds the first read may wait for a reply.
                Also caps the grace poll, so a zero interval keeps the
                drain strictly non-blocking after the first read.
        """
        grace: float = min(_PROBE_REPLY_GRACE, check_interval)
        message: dict[str, typing.Any] | None = typing.cast(
            'dict[str, typing.Any] | None',
            pubsub.get_message(timeout=check_interval),
        )
        while message is not None:
            if message.get('type') == 'message':
                legacy_index: int = sum(
                    holder.legacy for holder in holders.values()
                )
                holder_record: RedisLockHolder = self._parse_lock_response(
                    message.get('data'),
                    legacy_index,
                )
                holders[holder_record.holder_id] = holder_record
                if len(holders) >= expected_subscribers:
                    return
            message = typing.cast(
                'dict[str, typing.Any] | None',
                pubsub.get_message(timeout=0),
            )
            if message is None and grace > 0:
                # The buffer is empty but the next reply may be in
                # flight, and one short real wait collects it in this
                # pass.
                message = typing.cast(
                    'dict[str, typing.Any] | None',
                    pubsub.get_message(timeout=grace),
                )

    def _collect_lock_holders(
        self,
        connection: redis.client.Redis,
        expected_subscribers: int,
        timeout: float,
        reap: bool = True,
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
        `expected_subscribers` distinct holders have replied. Every
        polling interval drains all the replies that are already buffered
        instead of reading a single one, so the interval paces the
        polling rather than capping the reply throughput. A channel with
        more holders than the timeout has intervals can therefore still
        be probed conclusively.

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

        A probe is inconclusive in three ways:

        1. The subscriber count no longer matched ``expected_subscribers``
           immediately before the ping went out. The expectation was
           already stale, so the probe is abandoned before it puts any
           traffic on the channel.
        2. The count changed while the replies were being collected,
           checked each polling interval while replies are still
           outstanding and once more after collection. Someone joined
           or left, so the replies describe a channel that no longer
           exists in that shape, and a decision made from them could be
           wrong for either party. The interval check also stops an
           incomplete collection from waiting out the full timeout for
           a reply whose sender already left the channel.
        3. Fewer distinct holder ids replied than there were subscribers.
           Somebody counted by Redis is not answering, which normally
           means it crashed, though a holder too slow to answer inside
           ``timeout`` is treated the same way.
           `_kill_unavailable_locks` reaps those connections and the
           probe still reports inconclusive so that the caller retries
           against the cleaned-up channel.

        The two count checks bracket the collection window, but ``PUBSUB
        NUMSUB`` reports how many subscribers there are, not who they
        are, so count-preserving churn between the two checks - one
        holder leaving while another joins - remains undetectable and
        such a probe passes as conclusive. Losing waiters produce exactly
        that churn routinely, since they unsubscribe between attempts. A
        single probe can therefore still be built on a stale sample, and
        what contains the damage is what happens after. A writer that
        promoted
        on such a sample is visible as `RedisLockMode.EXCLUSIVE` to every
        later probe, so retrying losers back off, and its own promotion
        does not stand on this one probe either: the confirm probe in
        `_confirm_exclusive_promotion` re-checks the channel while the
        promotion is already on the wire and demotes when a churn-hidden
        holder surfaces (#145). Only a rival hidden from *that* probe as
        well - a second count-preserving join landing in the instant
        between its count pre-check and its ping - stays unseen, on both
        sides at once for an actual double.

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
            reap: Whether inconclusive case 3 above may kill the silent
                subscribers. `acquire` reaps; the read-only `probe`
                passes False so observing a channel can never modify
                it.

        Returns:
            The holders that answered, or `None` when the probe was
            inconclusive.
        """
        response_channel: str = f'{self.channel}-{uuid.uuid4().hex}'
        check_interval: float = min(self.thread_sleep_time, timeout / 10)
        pubsub: redis.client.PubSub = self._get_pubsub(connection)
        holders: dict[str, RedisLockHolder] = {}
        try:
            pubsub.subscribe(response_channel)
            for _ in self._timeout_generator(timeout, check_interval):
                confirmation: dict[str, typing.Any] | None = typing.cast(
                    'dict[str, typing.Any] | None',
                    pubsub.get_message(timeout=check_interval),
                )
                if confirmation and confirmation.get('type') == 'subscribe':
                    break

            # First half of the count bracket: a probe whose expectation
            # is already stale is abandoned before the ping goes out.
            precheck_subscribers: int = self._get_subscriber_count(
                connection,
            )
            if precheck_subscribers != expected_subscribers:
                return None

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
                self._drain_probe_replies(
                    pubsub,
                    holders,
                    expected_subscribers,
                    check_interval,
                )
                if len(holders) >= expected_subscribers:
                    break
                # Somebody joined or left while the replies were coming
                # in: the probe is inconclusive either way (case 2), so
                # stop waiting for replies that may never come instead
                # of burning the whole timeout on them. A waiter that
                # backs off right after answering used to cost exactly
                # that stall (#145). Returning here skips the reap on
                # purpose: a changed count is churn, not a crashed
                # holder, and reaping is only safe when the count held
                # steady while a subscriber stayed silent.
                if (
                    self._get_subscriber_count(connection)
                    != expected_subscribers
                ):
                    return None

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
                if reap:
                    self._kill_unavailable_locks(
                        connection,
                        holders.values(),
                    )
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
        `RedisLockMode.EXCLUSIVE`: the lock is owned, so there is
        nothing to elect. It also returns False when any other holder
        advertises ``elected``: that peer won a previous election and is
        waiting for the readers to drain, so a fresh contender defers to
        it instead of rerunning the sort against it (#143). Otherwise
        the ids of every `RedisLockMode.PENDING` holder are sorted and
        the lowest one wins.

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

        Winning here is also not the last word. A reply is a snapshot,
        so a contender can win this sort from a reply that predates a
        peer's own promotion - most easily the uncontended fast path,
        which never probes at all. The promotion that follows a win is
        therefore verified by `_confirm_exclusive_promotion` before
        `acquire` reports success, and a winner whose view turns out
        stale demotes there instead of standing on it (#145).

        The deference rule is what makes an election stick. Winning is
        remembered in `writer_elected` and advertised on every ping
        reply, so later writers see the incumbent and stand down no
        matter how their ids compare. Without it a lower-id newcomer
        would win the rerun sort and usurp a writer that had already
        been waiting (#143). Among fresh contenders the sort stays what
        it always was: holder ids are uuid4 hex strings, so the order is
        arbitrary but stable, a tie-break rather than a priority, and a
        contender that loses it simply retries.

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
            holder id, nobody holds the lock exclusively, and nobody
            else advertises a won election.

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

            Even the lowest id defers to an advertised incumbent:

            >>> lock._writer_is_elected(
            ...     [
            ...         redis.RedisLockHolder('bbb', pending),
            ...         redis.RedisLockHolder('zzz', pending, elected=True),
            ...     ]
            ... )
            False
        """
        if any(holder.mode is RedisLockMode.EXCLUSIVE for holder in holders):
            return False
        if any(
            holder.elected and holder.holder_id != self.holder_id
            for holder in holders
        ):
            return False
        pending_holder_ids: list[str] = sorted(
            holder.holder_id
            for holder in holders
            if holder.mode is RedisLockMode.PENDING
        )
        return bool(
            pending_holder_ids and pending_holder_ids[0] == self.holder_id
        )

    def _must_forfeit(
        self,
        holders: list[RedisLockHolder],
    ) -> bool:
        """Report whether an elected writer must give its election up.

        An election is worthless the moment somebody else demonstrably
        outranks it, and this is the complete list of who does. Every
        holder in a conclusive probe, excluding this lock's own record,
        is checked against three rules:

        1. A `RedisLockMode.EXCLUSIVE` holder. Somebody owns the lock,
           and legacy replies are recorded as exclusive too, so this
           rule also covers every peer the incumbent cannot reason
           about. The probe outranks our memory.
        2. A holder advertising ``elected`` with a lower holder id. Two
           incumbents can arise when a ping was answered just before the
           winner's flag became visible, so both sides resolve the
           conflict from the same records: the lower id keeps the
           election, the higher id forfeits. That settles it once both
           flags are visible on the wire. For the round in which a
           stale reply still hides one of them, the hold-off in
           `_resolve_exclusive_writer` keeps either side from
           promoting past the other.
        3. A `RedisLockMode.PENDING` holder whose record lacks the
           ``elected`` field (`RedisLockHolder.elected` is `None`) and
           whose id sorts below ours. That peer runs portalocker 4.0 or
           4.1, will elect itself by the id sort, and cannot be told to
           defer. Forfeiting reproduces the pre-4.2 semantics exactly,
           so a mixed channel is never less safe than 4.1, merely as
           unfair as it always was.

        A 4.2 pending writer with a lower id and ``elected: false``
        matches none of the rules, so the election is kept, but that
        record alone does not make promotion safe: it cannot show
        whether the peer probed before or after this election reached
        the wire, so the peer may be electing itself from a stale view
        right now. `_resolve_exclusive_writer` therefore keeps the
        election without promoting while such a record is present, and
        the next probe round tells a deferring peer (gone from the
        channel) apart from a competing one (visible as elected or
        exclusive, rules 1 and 2 above) (#143).

        Args:
            holders: The holders returned by a conclusive probe,
                including this lock's own record.

        Returns:
            True when the election must be forfeited and this writer
            should retry as a fresh contender.
        """
        for holder in holders:
            if holder.holder_id == self.holder_id:
                continue
            if holder.mode is RedisLockMode.EXCLUSIVE:
                return True
            lower: bool = holder.holder_id < self.holder_id
            if holder.elected and lower:
                return True
            if (
                holder.mode is RedisLockMode.PENDING
                and holder.elected is None
                and lower
            ):
                return True
        return False

    def _resolve_lock_holders(
        self,
        holders: list[RedisLockHolder] | None,
        fail_when_locked: bool,
    ) -> bool:
        """Decide what to do with the result of one probe.

        This is the only place where an observation becomes an action.
        Every branch is guarded on ``holders is not None``, because an
        inconclusive probe must never be read as "the lock is free". The
        election branches themselves live in `_resolve_exclusive_writer`
        and its `None` result routes into the give-up branch here.

        The outcomes, in the order they are tested:

        - **Compatible holders** (see `_holders_are_compatible`): the
          lock is now held. The subscription opened for the probe is
          exactly the subscription that holds it, so nothing more is
          needed.
        - **Already-elected writer, conclusive probe**: the election is
          not rerun against newcomers. Unless `_must_forfeit` says the
          probe outranks it, the incumbent keeps waiting while
          `RedisLockMode.SHARED` holders drain and while any undecided
          lower-id peer is still visible, and promotes `mode` to
          `RedisLockMode.EXCLUSIVE` once the probe is clear of both
          (see `_resolve_exclusive_writer` for the hold-off). A forfeit
          falls through to the give-up branch below.
        - **Fresh election won, no readers left** (see
          `_writer_is_elected`): `mode` is promoted to
          `RedisLockMode.EXCLUSIVE` and the lock is held. This runs
          before the `fail_when_locked` check, so the winner of two
          non-blocking contenders takes a reader-free channel instead of
          raising. The loser's probe shows the winner as pending with a
          winning id or as already exclusive, and it raises, so exactly
          one of the two succeeds (#143).
        - **Fresh election won, readers still holding**: with
          `fail_when_locked` the lock genuinely cannot be taken without
          waiting for the readers, so everything is released and
          `AlreadyLocked` is raised, without ever setting
          `writer_elected`: this instance is about to leave the channel
          and must not advertise an election it will not use. A blocking
          caller instead remembers the win in `writer_elected` and keeps
          the subscription, so its `RedisLockMode.PENDING` record keeps
          new readers out while the existing ones drain, and its ping
          replies advertise the election so later writers defer.
        - **Inconclusive probe while already elected**: the subscription
          is kept and the caller retries. An elected writer must not
          drop its pending record over a single noisy probe. Doing so
          would let new readers in and could forfeit an election it had
          already won, since `release` clears `writer_elected`.
        - **Anything else**: somebody incompatible is there, the
          election was lost or forfeited, or the probe was inconclusive
          for an unelected lock. The subscription is dropped before
          retrying (`_unsubscribe`, which keeps the connection for the
          next attempt). That unsubscribe is the point rather than a
          detail: a waiter that stayed subscribed would keep inflating
          the subscriber count that everybody else's probe has to match
          exactly.

        With `fail_when_locked` the caller does not want to wait for a
        held channel, so a conclusive probe that reaches the give-up
        branch releases everything and raises. An inconclusive probe is
        different: it is noise, not contention, because nobody
        demonstrably holds the channel, so the attempt returns False and
        retries within the caller's timeout. That also lets a
        non-blocking acquire succeed right after reaping a crashed
        holder instead of failing spuriously. Callers that want a hard
        single attempt pass ``timeout=0``, which bounds the retry loop
        in `acquire` to one iteration.

        Args:
            holders: A conclusive probe result, or `None` when the probe
                could not determine the state of the channel.
            fail_when_locked: Give up on conclusive contention instead
                of waiting for the holders to leave.

        Returns:
            True when the lock is now held and `acquire` may return;
            False when the caller should retry.

        Raises:
            AlreadyLocked: `fail_when_locked` was set and a conclusive
                probe showed the channel actually held.
        """
        if holders is not None and self._holders_are_compatible(holders):
            return True

        if holders is not None and self.flags == constants.LockFlags.EXCLUSIVE:
            resolution: bool | None = self._resolve_exclusive_writer(
                holders,
                fail_when_locked,
            )
            if resolution is not None:
                return resolution

        if holders is None and self.writer_elected:
            return False

        with self._mode_lock:
            self.writer_elected = False
        self._unsubscribe()
        logger.debug('Redis lock %s unsubscribed to retry', self.holder_id)
        if fail_when_locked:
            if holders is None:
                return False
            self.release()
            raise exceptions.AlreadyLocked()
        return False

    def _resolve_exclusive_writer(
        self,
        holders: list[RedisLockHolder],
        fail_when_locked: bool,
    ) -> bool | None:
        """Decide for an exclusive writer holding a conclusive probe.

        The election half of `_resolve_lock_holders`, split out so each
        method carries one decision. An incumbent (``writer_elected``
        set) never reruns the election: it keeps its win unless
        `_must_forfeit` says the probe outranks it. A fresh contender
        runs `_writer_is_elected`, and a winner promotes before the
        `fail_when_locked` check so a non-blocking writer can take a
        reader-free channel (#143).

        The incumbent promotes only once the probe is clear of both
        shared holders and undecided lower-id peers. A pending record
        carrying ``elected: false`` from a lower id is undecided
        because a ping reply is a snapshot that can be a full drain
        interval old: it cannot show whether that peer probed before or
        after this election became visible, so the peer may be about to
        promote on its own stale view. Holding the promotion for one
        round separates the cases, since a deferring peer has left the
        channel by the next probe and a promoting one shows up as
        exclusive or elected. Rerunning the election instead would
        reintroduce the usurpation this branch exists to prevent, and
        promoting anyway is exactly the double-holder interleaving the
        #143 review reproduced.

        The reply-staleness window around the uncontended fast path -
        a contender promoting on a stale reply answered between another
        writer's subscriber count and its fast-path promotion - is not
        this method's to close: every promotion this method makes, the
        `fail_when_locked` winners included, is verified against the
        live channel by `_confirm_exclusive_promotion` before `acquire`
        reports success, and the writer whose view was stale demotes
        there (#145). What the confirm does and does not cover is
        documented on that method.

        Both promotion sites take `_mode_lock` and `writer_elected` is
        assigned under it as well, so a ping answered by the worker
        thread can never observe the ``(mode, elected)`` pair halfway
        through a transition (see `channel_handler`).

        Args:
            holders: The holders returned by a conclusive probe,
                including this lock's own record.
            fail_when_locked: Give up on conclusive contention instead
                of waiting for the holders to leave.

        Returns:
            True when the lock is now held, False when the caller
            should keep the subscription and retry as an elected
            writer, or `None` when the election is lost or forfeited
            and `_resolve_lock_holders` should run its give-up branch.

        Raises:
            AlreadyLocked: `fail_when_locked` was set, the election was
                won, and shared holders still hold the lock, raised
                after a full `release` and without ever setting
                `writer_elected`.
        """
        shared_present: bool = any(
            holder.mode is RedisLockMode.SHARED for holder in holders
        )
        if self.writer_elected:
            if self._must_forfeit(holders):
                return None
            if shared_present:
                return False
            # A lower-id pending peer answering ``elected: false`` is
            # undecided: its reply cannot show whether it probed before
            # or after this election reached the wire, and a reply is
            # stale by up to a full drain interval. Promoting past it
            # could overlap with a promotion it made on its own stale
            # view, so hold the promotion for a round instead. A peer
            # that deferred unsubscribes right after its probe and is
            # gone from the next one, while a peer that elected or
            # promoted shows up as elected or exclusive and
            # `_must_forfeit` fires. Higher ids never win the sort, so
            # only lower ids need the wait.
            undecided_lower_id: bool = any(
                holder.holder_id != self.holder_id
                and holder.mode is RedisLockMode.PENDING
                and holder.elected is False
                and holder.holder_id < self.holder_id
                for holder in holders
            )
            if undecided_lower_id:
                return False
            with self._mode_lock:
                self.mode = RedisLockMode.EXCLUSIVE
            return True

        if not self._writer_is_elected(holders):
            return None
        if not shared_present:
            with self._mode_lock:
                self.writer_elected = True
                self.mode = RedisLockMode.EXCLUSIVE
            return True
        if fail_when_locked:
            self.release()
            raise exceptions.AlreadyLocked()
        with self._mode_lock:
            self.writer_elected = True
        return False

    def _confirm_probe_verdict(
        self,
        holders: list[RedisLockHolder],
    ) -> _ConfirmVerdict:
        """Judge one conclusive confirm probe of a freshly promoted writer.

        Runs with this lock already advertising
        `RedisLockMode.EXCLUSIVE` on the wire, so every rule reasons
        about what a *rival* record means next to a promotion that is
        now visible. The rules mirror `_must_forfeit`, applied to the
        post-promotion shape of the same conflicts:

        - A `RedisLockMode.SHARED` holder outranks the promotion. A
          reader can only coexist with an exclusive writer through a
          stale or churned sample on one of the two sides, and the
          reader holds without probing again, so the writer is the side
          that can still yield.
        - A `RedisLockMode.EXCLUSIVE` rival that is legacy or pre-4.2
          (`RedisLockHolder.elected` is `None`) outranks the promotion
          regardless of id: such a rival never runs a confirm probe, so
          it will not demote itself.
        - A 4.2 `RedisLockMode.EXCLUSIVE` rival resolves by the same
          deterministic rule two elected incumbents use: the lower id
          keeps, the higher id demotes. Seeing a lower id is a demote.
          A higher id is left to demote itself, which it does, because
          its own confirm cannot conclude while this lock is visible as
          pending (retry below) or as exclusive (this rule).
        - A `RedisLockMode.PENDING` peer with a lower id and no
          ``elected`` field runs portalocker 4.0 or 4.1: it wins the id
          sort, cannot be told to defer and will promote, so the
          promotion is given up at once, exactly like `_must_forfeit`
          rule 3.
        - A `RedisLockMode.PENDING` peer with a lower id that does
          speak 4.2 is undecided, the same ambiguity the #143 hold-off
          waits out: its reply cannot show whether its current probe
          predates this promotion, so it may be about to promote on a
          stale view. The round is inconclusive and the confirm asks
          again. By the next round the peer has either deferred (gone
          from the channel) or promoted (the exclusive rules above
          settle it).
        - Higher-id pending peers lose the sort against a visible
          exclusive record and defer on their own, so they do not block
          the confirmation.

        Args:
            holders: The holders returned by a conclusive probe,
                including this lock's own record.

        Returns:
            The verdict, with `_ConfirmVerdict.DEMOTE` taking
            precedence over `_ConfirmVerdict.RETRY` when both apply.
        """
        verdict: _ConfirmVerdict = _ConfirmVerdict.CONFIRMED
        for holder in holders:
            if holder.holder_id == self.holder_id:
                continue
            if holder.mode is RedisLockMode.SHARED:
                return _ConfirmVerdict.DEMOTE
            lower: bool = holder.holder_id < self.holder_id
            if holder.mode is RedisLockMode.EXCLUSIVE:
                if holder.legacy or holder.elected is None or lower:
                    return _ConfirmVerdict.DEMOTE
                continue
            if not lower:
                continue
            if holder.elected is None:
                return _ConfirmVerdict.DEMOTE
            verdict = _ConfirmVerdict.RETRY
        return verdict

    def _confirm_exclusive_promotion(
        self,
        connection: redis.client.Redis,
        fail_when_locked: bool,
    ) -> bool:
        """Verify a fresh exclusive promotion against the live channel.

        The promotion that led here was decided from ping replies, and
        a reply is a snapshot that can predate the peer's own fast-path
        promotion: the peer counted ``subscribers == 1`` moments
        earlier and promoted without ever probing, while its worker
        still answered this lock's ping as pending. Deciding from such
        a reply is how two writers end up exclusive at once (#145).
        This confirm runs after *every* promotion, fast path and
        election alike, with the new `RedisLockMode.EXCLUSIVE` record
        already visible on the wire: that visibility is what breaks the
        symmetry, because from now on every fresh probe any peer takes
        shows this lock as exclusive, and two freshly promoted rivals
        each see the other and resolve deterministically by holder id
        through `_confirm_probe_verdict`.

        A subscriber count of one confirms without probing: ownership
        *is* the subscription, so a rival that promoted is necessarily
        still subscribed and counted, and being alone proves there is
        nobody to conflict with. That single ``PUBSUB NUMSUB`` round
        trip is the entire cost on the uncontended fast path, which is
        why the confirm is unconditional rather than gated on a
        count-moved heuristic: the recount is the heuristic, and unlike
        one it is conclusive when it comes back one.

        Inconclusive rounds (a churning count, a silent subscriber, an
        undecided lower-id peer) are retried within an
        `unavailable_timeout` budget, never concluded from. A confirm
        that cannot reach one clean round demotes when the budget runs
        out: giving up a promotion that could not be verified is the
        safe direction, and costs one attempt in `acquire`'s retry
        loop. Under `fail_when_locked` that exhausted budget is
        terminal instead: the demotion releases fully and raises
        `AlreadyLocked`, even when every round was inconclusive noise
        rather than contention.

        This is a channel-level check and deliberately distinct from
        `_confirm_held`, the worker-death handshake: this method asks
        "is my promotion consistent with everybody else", the state
        handshake asks "is my own subscription still alive". The caller
        runs them in that order, so a subscription dying mid-confirm is
        still caught before `acquire` reports success.

        Two residues stay open, and they bound what "confirmed" means.
        Count-preserving churn can still hide a subscribed rival from a
        single conclusive probe, this one included, but only when a
        join lands in the instant between the probe's count pre-check
        and its ping. Producing two confirmed holders needs that join
        to hide the rival from the one confirm that would otherwise
        demote, on top of the double promotion itself, so it is the
        product of two rare events. And a pre-4.2 rival that promotes
        on a stale view *after* this confirm concluded is not seen by
        anybody, since it never confirms. Among 4.2+ writers
        that ordering is impossible, because a rival mid-decision is
        still subscribed and therefore visible to this confirm as
        pending or exclusive.

        Args:
            connection: The command connection to count and probe on.
            fail_when_locked: Forwarded to the demotion: a non-blocking
                writer raises instead of retrying.

        Returns:
            True when the promotion stands. False when the writer
            demoted and the attempt should be retried.

        Raises:
            AlreadyLocked: `fail_when_locked` was set and the writer
                demoted, raised after a full `release`.
        """
        for _ in self._timeout_generator(self.unavailable_timeout, None):
            subscribers: int = self._get_subscriber_count(connection)
            if subscribers <= 1:
                return True
            holders: list[RedisLockHolder] | None = self._collect_lock_holders(
                connection,
                subscribers,
                self.unavailable_timeout,
            )
            if holders is None:
                continue
            verdict: _ConfirmVerdict = self._confirm_probe_verdict(holders)
            logger.debug(
                'Redis lock %s confirm probe verdict=%s holders=%r',
                self.holder_id,
                verdict.value,
                holders,
            )
            if verdict is _ConfirmVerdict.CONFIRMED:
                return True
            if verdict is _ConfirmVerdict.DEMOTE:
                break
        self._demote_unconfirmed_promotion(fail_when_locked)
        return False

    def _demote_unconfirmed_promotion(self, fail_when_locked: bool) -> None:
        """Give up a promotion the confirm probe could not stand behind.

        The mirror of the give-up branch in `_resolve_lock_holders`,
        with the mode rolled back first: the lock is advertising
        `RedisLockMode.EXCLUSIVE`, so the record returns to
        `RedisLockMode.PENDING` (forgetting any election, exactly like
        a forfeit) before the subscription is dropped. The next attempt
        then rejoins as a fresh contender, and the rival that outranked
        this promotion is visible to that attempt's ordinary probe.

        Args:
            fail_when_locked: When set, the demotion is terminal for
                this acquisition: everything is released and
                `AlreadyLocked` is raised, mirroring how a conclusive
                lost election ends a non-blocking acquire.

        Raises:
            AlreadyLocked: `fail_when_locked` was set, raised after the
                full release.
        """
        with self._mode_lock:
            self.mode = RedisLockMode.PENDING
            self.writer_elected = False
        self._unsubscribe()
        logger.debug(
            'Redis lock %s demoted an unconfirmed promotion to retry',
            self.holder_id,
        )
        if fail_when_locked:
            self.release()
            raise exceptions.AlreadyLocked()

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
        3. A writer that promoted - on either path - verifies the
           promotion against the live channel with
           `_confirm_exclusive_promotion` and demotes when a rival
           promoted on an equally stale view and outranks it. On the
           fast path this costs one extra subscriber count.

        A failed attempt normally unsubscribes again, so the next
        iteration subscribes from scratch; an elected writer is the
        exception and holds on to its subscription between attempts.

        Either way, success is only reported after `_confirm_held`
        verified - under the state lock the worker's failure handler
        also takes, and after the channel-level confirm above - that
        the keep-alive worker is still alive and recorded no error. A
        subscription that died in the microseconds after the winning
        probe therefore costs one attempt instead of producing a lock
        that is held in this process's imagination only.

        `fail_when_locked` means the caller will not wait for a held
        channel: the first conclusive probe showing the lock actually
        held raises `AlreadyLocked` instead of polling the holder until
        the timeout expires. A writer that wins the election on a
        channel with no readers left is not blocked by anybody, so it
        takes the lock rather than raising, and exactly one of two
        non-blocking contenders succeeds on a free channel (#143).
        Inconclusive probes are noise rather than contention and are
        retried within the timeout. Pass ``timeout=0`` to bound a
        non-blocking acquisition to exactly one attempt.

        Transient connection trouble while merely *waiting* is scoped
        to the attempt (#141): a subscribe or probe that fails with a
        ``redis_exceptions.ConnectionError`` or ``TimeoutError``, and a
        keep-alive worker that dies before the lock is held, all count
        as one failed attempt and are retried within the timeout
        budget. Any other subscribe or probe failure releases
        everything first and then propagates, with the lock left
        inactive, off the channel and usable again - an error may
        never strand a live subscription behind a failed ``acquire``,
        because such a zombie record would block every other writer
        until someone released this instance by hand.

        Calling this on an instance whose previous hold ended in a loss
        resets it: the recorded error is consumed, the dead
        subscription is torn down, and the acquisition proceeds from
        scratch.

        Args:
            timeout: Seconds to keep retrying. Defaults to the instance's
                `timeout`, itself defaulting to `utils.DEFAULT_TIMEOUT`.
                Zero still makes exactly one attempt.
            check_interval: Base seconds to wait between attempts,
                jittered by `_timeout_generator`. Defaults to the
                instance's `check_interval`.
            fail_when_locked: Raise `AlreadyLocked` on the first
                conclusive probe that shows the channel held, instead of
                waiting for the holders to leave. Defaults to the
                instance's `fail_when_locked`.

        Returns:
            This lock, so ``with RedisLock(...) as lock`` binds the lock
            itself rather than a file handle.

        Raises:
            ~portalocker.exceptions.AlreadyLocked: The timeout expired
                without acquiring the lock, or `fail_when_locked` was set
                and a conclusive probe showed the channel held.
            ~portalocker.exceptions.LockException: This instance is
                already holding a lock. A `RedisLock` is not reentrant
                and holds at most one lock at a time; use a second
                instance, which gets its own `holder_id`. A single
                instance is not thread-safe either: two threads racing
                `acquire` on one instance can both pass this guard.
                Before 4.2.0 this misuse raised `AssertionError`, which
                ``python -O`` strips.

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

        if self.lost:
            # The previous hold ended in a revocation; consume it so the
            # instance is reusable, as documented above.
            self._abandon_failed_attempt()
        if self.pubsub is not None:
            raise exceptions.LockException('This lock is already active')
        with self._state_lock:
            self._lost_error = None
            self._lock_state = _LockState.ACQUIRING
            # A fresh acquisition consumes the previous grant's fencing
            # token. The token of the grant this acquire produces is
            # drawn in `_draw_fence_token`.
            self._fence_token = None
        if self.flags == constants.LockFlags.EXCLUSIVE:
            with self._mode_lock:
                self.mode = RedisLockMode.PENDING
                self.writer_elected = False
        connection: redis.client.Redis = self.get_connection()

        for _ in self._timeout_generator(
            effective_timeout,
            effective_check_interval,
        ):
            if self._acquire_attempt(connection, effective_fail_when_locked):
                return self

        self.release()
        raise exceptions.AlreadyLocked()

    def _acquire_attempt(
        self,
        connection: redis.client.Redis,
        fail_when_locked: bool,
    ) -> bool:
        """Run one iteration of `acquire`'s retry loop.

        Ensures a live subscription exists (abandoning one whose worker
        died while waiting, see `_waiting_attempt_failed`), then runs
        the probe half through `_probe_and_decide`.

        Once the subscription is live, this instance is a counted,
        ping-answering participant on the channel, so an error leaving
        this method must never strand that subscription: a stranded one
        is a zombie pending record that blocks every other writer while
        this instance refuses its next ``acquire`` as already active.
        A probe error that is mere connection weather therefore burns
        this attempt (`_abandon_failed_attempt`, same as a transient
        subscribe failure), and everything else - `AlreadyLocked` from
        `_resolve_lock_holders` excepted, which already released - runs
        a full `release` before propagating, interrupts included.

        Args:
            connection: The command connection of this acquisition.
            fail_when_locked: Forwarded to `_resolve_lock_holders`.

        Returns:
            True when the lock is now held and confirmed. False when
            this attempt failed and `acquire` should retry within its
            timeout budget.

        Raises:
            AlreadyLocked: Propagated from `_resolve_lock_holders` when
                `fail_when_locked` is set and the channel is
                conclusively held, after its own release.
            BaseException: A non-transient subscription or probe
                failure, re-raised after the terminal rollback.
        """
        if self.pubsub is not None and self._waiting_attempt_failed():
            # The worker backing the previous attempt died while we
            # were merely waiting; that attempt is over, nothing
            # more (#141).
            self._abandon_failed_attempt()
        if self.pubsub is None and not self._try_subscribe(connection):
            return False
        try:
            return self._probe_and_decide(connection, fail_when_locked)
        except exceptions.AlreadyLocked:
            # `_resolve_lock_holders` ran the terminal release before
            # raising; there is nothing left to roll back.
            raise
        except BaseException as error:
            if _is_transient_connection_error(error):
                logger.warning(
                    'Redis lock %s lost its command connection while '
                    'probing channel %r, the attempt is retried within '
                    'the timeout',
                    self.holder_id,
                    self.channel,
                    exc_info=True,
                )
                self._abandon_failed_attempt()
                return False
            self._roll_back_terminal_acquire_failure()
            raise

    def _probe_and_decide(
        self,
        connection: redis.client.Redis,
        fail_when_locked: bool,
    ) -> bool:
        """Probe the channel and turn the result into an attempt outcome.

        The decision half of `_acquire_attempt`, run under its rollback
        protection: takes the uncontended fast path or hands a probe to
        `_resolve_lock_holders`, verifies an exclusive promotion
        against the live channel through
        `_confirm_exclusive_promotion`, draws the optional fencing
        token through `_draw_fence_token`, and finally confirms the
        win through `_confirm_or_abandon`. The confirm probe runs for
        every exclusive promotion - the fast path and both election
        paths - because each of them decides from information that can
        be a reply-staleness window old. A shared join needs no
        confirm, since readers admit each other and a conclusive probe
        of pure readers has no promotion in it to race, and it draws
        no fencing token either (see `fence_token`).

        Args:
            connection: The command connection of this acquisition.
            fail_when_locked: Forwarded to `_resolve_lock_holders` and
                to the confirm probe's demotion.

        Returns:
            True when the lock is now held and confirmed, False when
            the attempt failed.

        Raises:
            AlreadyLocked: Propagated from `_resolve_lock_holders` or
                from a `fail_when_locked` confirm demotion when the
                channel is conclusively held.
        """
        subscribers: int = self._get_subscriber_count(connection)
        logger.debug(
            'Redis lock %s mode=%s observed %d subscribers',
            self.holder_id,
            self.mode.value,
            subscribers,
        )
        if subscribers == 1:
            if self.flags == constants.LockFlags.EXCLUSIVE:
                with self._mode_lock:
                    self.mode = RedisLockMode.EXCLUSIVE
        else:
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
            if not self._resolve_lock_holders(holders, fail_when_locked):
                return False
        if self.flags == constants.LockFlags.EXCLUSIVE:
            if not self._confirm_exclusive_promotion(
                connection,
                fail_when_locked,
            ):
                return False
            self._draw_fence_token(connection)
        return self._confirm_or_abandon()

    def _try_subscribe(self, connection: redis.client.Redis) -> bool:
        """Subscribe for one attempt, absorbing transient failures.

        A subscribe that dies with a connection or timeout error is the
        waiter-side blip `acquire` promises to tolerate: the rollback
        inside `_start_subscription` already ran (keeping the command
        connection alive for the retry, which is what keeps the ping
        handler answerable after the blip), so this only logs and
        reports the attempt as failed. Every other error propagates,
        because an unexpected failure should surface rather than burn
        the whole timeout retrying it, and on that terminal path a full
        `release` runs first so a lock-created command connection is
        closed exactly when nobody is going to retry on it.

        Not every ``ConnectionError`` is a blip: redis-py derives its
        credential and pool-exhaustion failures from it, and those
        repeat identically on every retry, so
        `_is_transient_connection_error` routes them onto the same
        terminal path. A wrong password therefore raises
        ``AuthenticationError`` promptly instead of burning the whole
        timeout and ending in a misleading ``AlreadyLocked``.

        Args:
            connection: The command connection to derive the
                subscription client from.

        Returns:
            True when the subscription is live, False when the attempt
            failed transiently and should be retried.

        Raises:
            Exception: The non-transient subscription failure, re-raised
                after the terminal cleanup. A cleanup failure is logged
                so it cannot replace the original error.
        """
        try:
            self._start_subscription(connection)
        except Exception as error:
            if not _is_transient_connection_error(error):
                self._roll_back_terminal_acquire_failure()
                raise
            logger.warning(
                'Redis lock %s could not subscribe, retrying within the '
                'timeout',
                self.holder_id,
                exc_info=True,
            )
            return False
        return True

    def _roll_back_terminal_acquire_failure(self) -> None:
        """Restore the fully inactive state before an error propagates.

        The terminal arm shared by `_try_subscribe` and
        `_acquire_attempt`: when a subscription or probe failure is
        about to leave `acquire`, a full `release` runs so the
        subscription stops being counted and a lock-created command
        connection is closed exactly when nobody is going to retry on
        it. A cleanup failure is logged so it cannot replace the
        original error, which the caller re-raises.
        """
        try:
            self.release()
        except Exception:
            logger.warning(
                'Redis lock %s failed to roll back after a failed '
                'subscription',
                self.holder_id,
                exc_info=True,
            )

    def _confirm_or_abandon(self) -> bool:
        """Confirm a won attempt, or abandon it for the next round.

        Wraps `_confirm_held` so every `acquire` success site treats a
        refused confirmation the same way: the attempt is consumed by
        `_abandon_failed_attempt` and the retry loop carries on.

        Returns:
            True when the lock is held, False when the attempt failed at
            the last moment and was cleaned up.
        """
        if self._confirm_held():
            return True
        self._abandon_failed_attempt()
        return False

    @property
    def fence_key(self) -> str:
        """Name of the key the fencing counter lives in.

        One counter per channel, shared by every fencing-enabled
        writer on it, so tokens drawn by different processes are
        ordered by the same ``INCR``. The key never expires, by
        design: monotonicity has to survive idle periods, so `release`
        deliberately leaves it behind and nothing in this class ever
        deletes it. Delete it by hand only when no fenced resource
        remembers a token from it any more, because a reset counter
        hands out tokens that stale resources would consider current.

        Returns:
            ``<channel>-fence``.

        Example:
            >>> from portalocker import redis
            >>> redis.RedisLock('some_channel', fencing=True).fence_key
            'some_channel-fence'

        .. versionadded:: 4.2.0
        """
        return f'{self.channel}-fence'

    @property
    def fence_token(self) -> int | None:
        """Token of the current fenced grant, or the most recent one.

        `None` until this lock - constructed with ``fencing=True`` and
        exclusive flags - completes an exclusive grant, and from then
        on the token drawn for that grant. The value deliberately
        survives a loss and a `release`, so bare
        ``acquire()``/``release()`` callers and forensic logging can
        still read which token the hold carried. The next `acquire`
        resets it to `None`. With fencing enabled the lock is never
        reported held without a token, because the token is drawn
        before `_confirm_held` runs.

        Shared holders never carry a token, ``fencing=True`` or not:
        shared grants coexist, so a per-grant monotonic token would
        order nothing. Fencing is a writer-side guarantee.

        The guarantee is also only as wide as the writers that
        participate: a holder running portalocker 4.1 or older, or a
        4.2+ writer constructed without ``fencing=True``, takes the
        channel without bumping the counter, so the tokens order
        exactly the exclusive grants of fencing-enabled writers and
        nothing else. Mixed channels degrade silently, which is one of
        the reasons fencing is opt-in.

        Returns:
            The token, or `None` when no fenced grant happened since
            the last `acquire` started.

        Example:
            >>> import fakeredis
            >>> import portalocker
            >>> connection = fakeredis.FakeStrictRedis(
            ...     server=fakeredis.FakeServer(), decode_responses=True
            ... )
            >>> lock = portalocker.RedisLock(
            ...     'fenced_channel', connection=connection, fencing=True
            ... )
            >>> lock.fence_token is None
            True
            >>> with lock:
            ...     lock.fence_token
            1
            >>> lock.fence_token  # survives release until the next acquire
            1

        .. versionadded:: 4.2.0
        """
        with self._state_lock:
            return self._fence_token

    def _draw_fence_token(self, connection: redis.client.Redis) -> None:
        """Draw the fencing token for a just-confirmed exclusive grant.

        Runs after `_confirm_exclusive_promotion` and before
        `_confirm_held`, which is what upholds the fencing invariant:
        with fencing enabled the lock can never report held without a
        token, because an ``INCR`` that fails leaves the state at
        `_LockState.ACQUIRING` and the attempt never confirms.

        The failure discipline is `_acquire_attempt`'s, shared with
        every other command on the acquire path: a transient
        connection error burns this attempt and the retry draws a
        fresh token (a token possibly burned by the failed attempt is
        a gap in the counter, which monotonicity does not mind), while
        anything else - most likely a ``WRONGTYPE`` reply because
        something unrelated wrote to `fence_key` - releases everything
        and propagates, since it would repeat identically on every
        retry and burning the timeout on it would end in a misleading
        ``AlreadyLocked``.

        A no-op when `fencing` is disabled. Only exclusive grants
        reach this method, since shared joins return earlier in
        `_probe_and_decide`.

        Args:
            connection: The command connection to run ``INCR`` on.

        Raises:
            Exception: Whatever ``INCR`` raised, unhandled here.
        """
        if not self.fencing:
            return
        token: int = int(connection.incr(self.fence_key))
        with self._state_lock:
            self._fence_token = token
        logger.debug(
            'Redis lock %s drew fence token %d on channel %r',
            self.holder_id,
            token,
            self.channel,
        )

    def probe(
        self,
        timeout: float | None = None,
    ) -> list[RedisLockHolder]:
        """Report who currently holds the channel, changing nothing.

        The read-only companion to `acquire` and the replacement for the
        deprecated `check_or_kill_lock`: it publishes the same liveness
        ping a real acquisition would, collects the answers, and stops
        there. No connection is killed, no subscription outlives the
        call, and the lock's own state does not change, so this is safe
        to run against a channel in production to see who is on it.

        When this lock currently holds the channel its own record is
        part of the answer, since its keep-alive worker answers the ping
        like any other holder's.

        Args:
            timeout: Seconds to wait for the holders to answer,
                defaulting to `unavailable_timeout`. Inconclusive probes
                are retried within this budget.

        Returns:
            One `RedisLockHolder` per subscriber, or an empty list when
            nobody is subscribed to the channel.

        Raises:
            ~portalocker.exceptions.LockException: The channel stayed
                inconclusive for the whole timeout: subscribers kept
                joining or leaving mid-probe, or a counted subscriber
                never answered. An unanswered probe is deliberately not
                reported as an empty channel, because treating it as
                one is exactly the misreading that hands out double
                locks; `acquire` is the code path that may reap such a
                silent subscriber.

        Example:
            >>> import fakeredis
            >>> import portalocker
            >>> connection = fakeredis.FakeStrictRedis(
            ...     server=fakeredis.FakeServer(), decode_responses=True
            ... )
            >>> lock = portalocker.RedisLock(
            ...     'probed_channel', connection=connection
            ... )
            >>> lock.probe()
            []
            >>> _ = lock.acquire()
            >>> [holder.mode.value for holder in lock.probe()]
            ['exclusive']
            >>> lock.release()

        .. versionadded:: 4.2.0
        """
        effective_timeout: float = (
            timeout if timeout is not None else self.unavailable_timeout
        )
        connection: redis.client.Redis = self.get_connection()
        for _ in self._timeout_generator(effective_timeout, None):
            subscribers: int = self._get_subscriber_count(connection)
            if subscribers == 0:
                return []
            holders: list[RedisLockHolder] | None = self._collect_lock_holders(
                connection,
                subscribers,
                effective_timeout,
                reap=False,
            )
            if holders is not None:
                return holders
        raise exceptions.LockException(
            exceptions.LockException.LOCK_FAILED,
            f'Redis lock channel {self.channel!r} could not be probed '
            f'conclusively within {effective_timeout} seconds',
        )

    def check_or_kill_lock(
        self,
        connection: redis.client.Redis,
        timeout: float,
    ) -> bool | None:
        """Ask whether anyone is alive on the channel, and reap if not.

        .. deprecated:: 4.2.0
            Use `probe` for a read-only view of the channel; the
            reaping of crashed holders happens inside `acquire`, where
            the protocol's timeout discipline protects live-but-slow
            holders from a caller-chosen timeout. This method will be
            removed in portalocker 5.0.0.

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

        Warns:
            DeprecationWarning: Always, naming `probe` as the
                replacement.
        """
        warnings.warn(
            'check_or_kill_lock is deprecated and will be removed in '
            'portalocker 5.0.0; use probe() for a read-only view of the '
            'channel. Crashed holders are reaped inside acquire().',
            DeprecationWarning,
            stacklevel=2,
        )
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

    def _in_subscribing_process(self) -> bool:
        """Report whether this process created the current subscription.

        A forked child inherits the lock object together with the
        parent's sockets, so a teardown running in the child must not
        talk to them: an UNSUBSCRIBE sent over the inherited socket
        releases the *parent's* lock, and the parent - whose connection
        stays perfectly healthy - is never told (redis-py's connection
        pools guard the same hazard with their ``_checkpid`` dance).
        `_unsubscribe` and `release` therefore only drop local
        references when this returns False.

        Returns:
            True when no subscription exists or the current pid is the
            one `_start_subscription` recorded, so socket teardown is
            safe. False in a process that inherited the subscription
            through ``fork``.
        """
        subscription_pid: int | None = self._subscription_pid
        return subscription_pid is None or subscription_pid == os.getpid()

    def _unsubscribe(self) -> None:
        """Drop the subscription but keep the command connection.

        Stops and joins the keep-alive thread, closes the pubsub
        (unsubscribing first when the pubsub still owns a connection),
        and closes the dedicated subscription client and its pool.
        This is the back-off between attempts: `_resolve_lock_holders`
        calls it after an unsuccessful probe so that a waiting lock
        stops being counted as a subscriber, and the next attempt
        subscribes from scratch, on a fresh subscription client but the
        same command connection.

        In a forked child (see `_in_subscribing_process`) all of that
        shrinks to dropping the references: the thread does not exist
        in the child, and every socket in sight is the parent's live
        subscription, which the child may not touch.

        The teardown is exception safe. `thread` and `pubsub` are
        cleared before their cleanup steps run, every step runs even
        when an earlier one fails, and only the first failure is
        re-raised once the rest has run (see `_keep_first_error`). One
        failed unsubscribe therefore cannot leave a stale `pubsub`
        behind that would trip the ``assert not self.pubsub`` guard on
        every later `acquire`.

        The unsubscribe is skipped when the pubsub no longer owns a
        connection. The worker thread closes the pubsub itself when it
        is stopped (redis-py's ``PubSubWorkerThread.run`` behaviour), so
        in the normal release path the subscription is already gone by
        the time this method looks at it. Unsubscribing then would check
        a fresh connection out of the pool and reconnect purely to
        unsubscribe from a subscription the server already discarded.
        Only a pubsub that still owns a connection - the
        `_start_subscription` rollback path, where the thread never
        ran - has anything left to unsubscribe.

        A thread is only joined when its `start` succeeded. A thread
        whose `start` raised has no ``ident`` yet, and joining it would
        raise ``RuntimeError`` instead of the error that actually broke
        the acquire. ``is_alive`` would be the wrong test: a thread that
        already finished is no longer alive but can still be joined.

        The join is also skipped when this method runs *on* the worker
        thread itself, because joining the current thread raises
        ``RuntimeError`` too. That is a supported path, not an anomaly:
        an ``on_lost`` callback calling `release` executes on the
        worker thread, and the worker is already stopping by then, so
        skipping the join only means the thread finishes on its own a
        moment after the release returns.

        Keeping the connection alive here is not an optimisation but a
        correctness requirement. `channel_handler` answers pings over
        `connection` from the worker thread, and `acquire` keeps working
        with the connection it fetched before its retry loop. Closing
        and clearing the connection between attempts is exactly the bug
        that used to interrupt the whole process: the next attempt
        resubscribed on the stale reference while ``self.connection``
        was `None`, the handler's assert fired on the worker thread, and
        `PubSubWorkerThread.run` escalated it to the main thread.

        Raises:
            Exception: The first error any teardown step raised,
                re-raised after the remaining steps have run.
        """
        first_error: Exception | None = None
        same_process: bool = self._in_subscribing_process()
        self._subscription_pid = None

        thread: PubSubWorkerThread | None = self.thread
        self.thread = None
        # In a forked child the thread object is a ghost: fork clones
        # only the calling thread, and joining a thread that never ran
        # in this process can hang on its inherited internal lock.
        if thread is not None and same_process:
            try:
                thread.stop()
                if (
                    thread.ident is not None
                    and thread is not threading.current_thread()
                ):
                    thread.join()
                    time.sleep(0.01)
            except Exception as error:
                first_error = _keep_first_error(first_error, error)

        pubsub: redis.client.PubSub | None = self.pubsub
        self.pubsub = None
        if pubsub is not None and same_process:
            try:
                # redis-py does not annotate `PubSub.connection` (the
                # constructor assigns a plain `None`), so mypy infers the
                # attribute as always-`None` and basedpyright sees an
                # unknown. The cast gives both the real optional type.
                pubsub_connection: object | None = typing.cast(
                    'object | None',
                    pubsub.connection,
                )
                if pubsub_connection is not None:
                    # `PubSub.unsubscribe()` is unannotated in redis-py
                    pubsub.unsubscribe(  # type: ignore[no-untyped-call]
                        self.channel,
                    )
            except Exception as error:
                first_error = _keep_first_error(first_error, error)
            try:
                pubsub.close()
            except Exception as error:
                first_error = _keep_first_error(first_error, error)

        first_error = self._close_subscription_client(
            first_error,
            teardown=same_process,
        )

        if first_error is not None:
            raise first_error

    def _close_subscription_client(
        self,
        first_error: Exception | None,
        teardown: bool = True,
    ) -> Exception | None:
        """Close the dedicated subscription client and its pool.

        The client is per attempt and always owned by this lock, also
        when it came from `subscription_connection_factory`, so it goes
        down with the subscription. Its pool is disconnected explicitly
        because ``Redis.close`` leaves an externally supplied pool
        alone, and the pubsub connection was checked out of exactly
        that pool. Follows the same exception-safe teardown discipline
        as the rest of `_unsubscribe`: the reference is cleared first,
        both steps always run, failures are folded through
        `_keep_first_error`.

        Args:
            first_error: The error the surrounding teardown kept so
                far, or `None`.
            teardown: Whether the client's sockets may be touched at
                all. `_unsubscribe` passes False in a forked child,
                where the reference is dropped but the connections
                belong to the parent.

        Returns:
            The error the caller should keep: `first_error` when it was
            already set, otherwise the first failure raised here, or
            `None` when everything succeeded.
        """
        subscription_client: redis.client.Redis | None = (
            self._subscription_client
        )
        self._subscription_client = None
        if subscription_client is not None and teardown:
            try:
                subscription_client.close()
            except Exception as error:
                first_error = _keep_first_error(first_error, error)
            try:
                subscription_client.connection_pool.disconnect()
            except Exception as error:
                first_error = _keep_first_error(first_error, error)
        return first_error

    def release(self) -> None:
        """Give up the lock and undo everything `acquire` set up.

        Stops and joins the keep-alive thread, closes the pubsub (see
        `_unsubscribe`), and forgets any election this lock had won. A
        connection the lock created itself is closed and cleared, so the
        next `get_connection` builds a fresh one. A connection supplied
        by the caller is left alone.

        Dropping the subscription is not merely cleanup, it *is* the
        release: other processes learn the lock is free by no longer
        seeing this subscriber, with no key to delete and no expiry to
        wait for.

        The teardown is exception safe: every step runs even when an
        earlier one fails, `thread`, `pubsub` and a self-created
        `connection` are cleared regardless, and only the first failure
        is re-raised once everything has run (see `_keep_first_error`).
        A release interrupted by a dead Redis therefore still leaves the
        instance ready for a later `acquire` instead of permanently
        tripping its ``assert not self.pubsub`` guard.

        This is the terminal teardown. The back-off between attempts is
        `_unsubscribe`, which keeps the connection so the retry loop and
        the ping handler can keep using it; this method is for when the
        lock is done, either released by the caller or giving up with
        `AlreadyLocked`. `_try_subscribe` also calls it when a
        subscription failure is about to propagate out of `acquire`,
        which is terminal too.

        Calling this when nothing was acquired is harmless - it still
        closes a self-created connection if one exists - which is what
        makes both that rollback and `__del__` safe.

        In a forked child every socket in sight belongs to the parent,
        so the teardown only drops this process's references (see
        `_in_subscribing_process`): the parent keeps its lock, and the
        child's `release` (or garbage collection) cannot silently
        revoke it. The child must not otherwise use an inherited lock
        instance; it should build its own.

        A loss is *not* erased here: releasing a lock that was revoked
        while held leaves `lost` True (and the causal error in place)
        until the next `acquire`, so the loss stays observable after
        the teardown - the ``with`` block exit and code checking `lost`
        after a bare `release` both rely on that. Releasing never
        raises on account of a loss; only genuine teardown failures
        propagate.

        Raises:
            Exception: The first error any teardown step raised,
                re-raised after the remaining steps have run.
        """
        first_error: Exception | None = None
        # Snapshot before _unsubscribe clears the recorded pid: the
        # command connection was created alongside the subscription, so
        # the same fork test governs whether closing it is safe.
        same_process: bool = self._in_subscribing_process()
        with self._mode_lock:
            self.writer_elected = False
        with self._state_lock:
            if self._lock_state is not _LockState.LOST:
                self._lock_state = _LockState.IDLE
                self._lost_error = None
        try:
            self._unsubscribe()
        except Exception as error:
            first_error = error

        # Only close connections we created ourselves; caller-supplied ones
        # are left untouched. Clear it even when closing fails so a later
        # acquire recreates the connection instead of reusing a broken one.
        # A forked child clears without closing: the sockets are the
        # parent's.
        if self.close_connection and self.connection is not None:
            connection: redis.client.Redis = self.connection
            self.connection = None
            if same_process:
                try:
                    connection.close()
                except Exception as error:
                    first_error = _keep_first_error(first_error, error)

        if first_error is not None:
            raise first_error

    def __del__(self) -> None:
        """Release the lock when the object is garbage collected.

        A safety net for a lock that was never released explicitly, not a
        substitute for `release` or a ``with`` block. CPython's reference
        counting usually runs this promptly, but PyPy collects whenever
        it sees fit, and nothing runs at all if the interpreter exits
        while a reference is still alive.

        That last case is harmless here, which is the advantage of
        holding a lock in a connection rather than in a key: the process
        exits, the daemon reader thread goes with it, the socket closes
        and Redis releases the lock.

        Every error is suppressed here, deliberately. A
        finalizer often runs during interpreter shutdown, where even the
        import a reconnect attempt triggers can fail (``ImportError:
        sys.meta_path is None``), and raising from here only produces an
        "Exception ignored in" message that the code dropping the
        reference can never catch.
        """
        with contextlib.suppress(Exception):
            self.release()
