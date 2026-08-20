Redis Lock
==========

`RedisLock` coordinates processes across machines through a Redis pubsub
channel rather than a shared filesystem; see :doc:`lock-types` for where it
fits next to the file-based locks, and :doc:`quickstart` for installing
portalocker itself. This page is the deep dive: why the lock works this
way, installing the extra it needs, everyday usage, who owns the
underlying connection, how a holder learns that it lost the lock, how a
wedged holder gets cleaned up, and how to exercise all of it with
`fakeredis` instead of a real server.

Why a pubsub lock
------------------

The common way to build a Redis lock is a key with a time to live: the
holder writes ``SET <name> <token> NX PX <ttl>`` and keeps refreshing it
for as long as it needs the lock. That design has one persistent problem.
When the holder crashes, its network drops, or its machine loses power,
the key outlives it, and every other contender waits out the remaining
TTL even though the holder is provably gone. Shortening the TTL narrows
that window but trades it for a different failure: a holder that is
merely slow, not dead, can lose a lock it still believes it owns.

`RedisLock` keeps the lock in a *subscription* instead of a key. A holder
subscribes to the lock channel, and a background thread keeps reading
from it, so ownership is a property of a live connection rather than a
stored value. The moment that connection drops - a clean release, a
crash, or a severed network - Redis drops the subscriber and the lock is
released at once, and since 4.2.0 the holder is told at once as well
(see `Losing a lock`_). There is no expiry to wait out and no heartbeat
to refresh. The trade is that nothing is stored anywhere, so every
acquisition attempt has to ask the channel who is currently there instead
of reading a key.

That ask is a ping/pong published on the channel itself: a probing lock
publishes a ping carrying a private response channel, and every
subscriber answers with its holder id and current mode. Shared readers
hold the lock together; an exclusive writer holds it alone; and competing
writers agree on a single winner by sorting the pending holder ids they
all observed, with no coordinator and no message exchange beyond that one
probe.

Since 4.2.0 every reply also carries an ``elected`` boolean, so a writer
that has already won the election stays visible while it waits for the
readers to drain: later writers defer to an advertised incumbent instead
of rerunning the sort against it, which used to let a lower-id newcomer
usurp a waiting writer. Because a reply is a snapshot that can predate
the election it should have reported, the incumbent also waits one extra
probe round rather than promote while a lower-id newcomer that has not
yet advertised seeing the election is still on the channel. The field is
optional on the wire and the record keeps protocol version 1, so
portalocker 4.0 and 4.1 holders parse it unchanged, simply ignore the
field, and keep the old id election on mixed channels. Full incumbency
protection holds once every writer on a channel runs 4.2 or later, with
handover delayed by that one probe round when a newcomer's reply raced
the incumbent's election.

A promotion is also verified after the fact. A reply is a snapshot, so
a probe can miss a peer's promotion by milliseconds - most easily the
uncontended fast path, which promotes on a bare subscriber count
without probing at all - and two writers could then promote on each
other's stale replies. Since 4.2.0 every promoted writer runs one
confirm probe while its new exclusive record is already visible on the
wire, so two freshly promoted rivals see each other and resolve the
conflict deterministically: the lower holder id keeps the lock, the
higher id demotes and retries. On an uncontended channel the confirm
is a single extra subscriber count, which keeps the fast path at a few
milliseconds.

Installation
-------------

`RedisLock` needs the ``redis`` package, which is an optional extra:

.. code-block:: console

    pip install "portalocker[redis]"

Without it, ``portalocker.RedisLock`` is a stub class rather than an
import failure: `portalocker/__init__.py` imports `portalocker.redis`
inside a ``try``/``except ImportError``, so the rest of the package
stays usable without the extra. A missing ``redis`` package therefore
only surfaces when something actually tries to *use* `RedisLock` -
constructing the stub raises an ``ImportError`` naming the extra - not
when ``import portalocker`` itself runs. Before 4.1.1 the fallback was
`None`, so constructing it failed with ``TypeError: 'NoneType' object
is not callable`` instead of naming the missing dependency.

Basic usage
------------

Every example on this page runs against `fakeredis` instead of a real
server, the same way `portalocker_tests/test_redis.py` does; see
`Testing against fakeredis`_ at the end of this page.

>>> import fakeredis
>>> import portalocker
>>> connection = fakeredis.FakeStrictRedis(
...     server=fakeredis.FakeServer(), decode_responses=True
... )
>>> with portalocker.RedisLock('some_channel', connection=connection):
...     print('do something here')
do something here

`RedisLock` is exclusive by default; pass
``flags=portalocker.LockFlags.SHARED`` for a reader that can coexist with
other readers, while an exclusive writer waits for every shared reader to
release first:

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

Set ``health_check_interval`` on the connection so that both sides notice
a dead peer promptly; it is part of `RedisLock.DEFAULT_REDIS_KWARGS`, so
it already defaults to ``10`` seconds unless a connection is supplied
directly, in which case the connection's own settings apply instead.

Connection handling
---------------------

`RedisLock` either uses a connection handed to it, or builds its own:

>>> given = portalocker.RedisLock('given_channel', connection=connection)
>>> given.connection is connection
True
>>> standalone = portalocker.RedisLock('standalone_channel')
>>> standalone.connection is None
True

The distinction matters for cleanup, and it is visible on the instance:
a connection passed to the constructor is never closed by the lock, since
the caller owns it and is expected to manage it. A connection `RedisLock`
builds for itself is owned by the lock instead: it is created lazily, on
first use, from ``redis_kwargs`` (with `RedisLock.DEFAULT_REDIS_KWARGS`
filled in for anything not overridden), and `RedisLock.release` closes and
clears it, so the following `RedisLock.acquire` builds a fresh connection
rather than reusing a closed one. Against a real server that looks like:

.. code-block:: python

    import portalocker

    lock = portalocker.RedisLock(
        'some_channel',
        redis_kwargs={'host': 'redis.internal', 'port': 6379},
    )
    lock.connection is None  # True: nothing has connected yet
    with lock:
        lock.connection is not None  # True: created on first use
    lock.connection is None  # True: release() closed and cleared it

Since 4.2.0 the connection above is only the *command* connection
(probes, pings, ``CLIENT LIST``). The subscription that actually holds
the lock lives on a dedicated client the lock builds for every
attempt, derived from the command connection's pool but configured to
never retry and never reconnect, with the holder's name set at the
connection level; see `Losing a lock`_ for why. Exotic setups whose
pools the derivation cannot clone (Sentinel, cluster, custom pool
classes) pass ``subscription_connection_factory`` to build that client
themselves.

The same lifecycle is observable end to end without a real server, by
pointing the connection `RedisLock` would normally build at a `fakeredis`
server instead of a real one:

>>> import redis
>>> pool = redis.ConnectionPool(
...     connection_class=fakeredis.FakeConnection,
...     server=fakeredis.FakeServer(),
...     decode_responses=True,
... )
>>> built = portalocker.RedisLock(
...     'built_channel', redis_kwargs={'connection_pool': pool}
... )
>>> built.connection is None
True
>>> with built:
...     built.connection is not None
True
>>> built.connection is None
True

Losing a lock
---------------

The pubsub design releases a lock the instant its connection dies, and
since 4.2.0 the holder is told just as promptly. The subscription lives
on a dedicated connection with a zero-retry, zero-reconnect policy: a
transparently resurrected subscription would be a silent re-acquisition
of a lock the holder may have lost to someone else in the gap, so the
first read error after a revocation is terminal, and the keep-alive
thread turns it into a loss the application can observe on four
channels:

- `RedisLock.lost` turns `True` and stays `True` through
  `RedisLock.release`, until the next `RedisLock.acquire` resets the
  instance.
- `RedisLock.ensure_held` raises
  :class:`~portalocker.exceptions.LockLostError` (carrying the channel,
  the holder id and the causal error as ``__cause__``). Long critical
  sections should call it periodically, since it is the only
  deterministic way a loss interrupts a running body.
- A ``with`` block whose body finishes cleanly raises
  :class:`~portalocker.exceptions.LockLostError` on exit, after
  releasing. A body exception is never masked by it.
- An ``on_lost`` callback passed to the constructor fires exactly once
  per loss, on the keep-alive thread. Keep it short, do not take
  application locks inside it, and expect anything it raises to be
  logged rather than propagated. Calling ``release()`` on the lost
  lock inside the callback is fine: the teardown skips joining the
  worker thread it runs on, and that thread exits on its own right
  after the callback returns.

By default a loss additionally interrupts the main thread with a
`KeyboardInterrupt`, which is the historical behaviour and every bit as
best-effort as it sounds: a custom ``SIGINT`` disposition, a main
thread blocked in a C call, or a broad ``except`` all swallow it.
portalocker 5.0.0 flips the ``interrupt_on_lost`` default to `False`,
and until then a loss under the implicit default emits a
`DeprecationWarning` at the moment it interrupts; pass
``interrupt_on_lost`` explicitly to choose your side early.

Injecting a read failure into the keep-alive thread stands in for a
killed connection well enough to show the whole surface without a real
server:

>>> import redis.exceptions
>>> lost_locks = []
>>> lock = portalocker.RedisLock(
...     'doomed_channel',
...     connection=connection,
...     on_lost=lost_locks.append,
...     interrupt_on_lost=False,
... )
>>> _ = lock.acquire()
>>> lock.ensure_held()  # held and healthy: returns quietly
>>> def broken_read(*args, **kwargs):
...     raise redis.exceptions.ConnectionError('connection killed')
>>> lock.pubsub.get_message = broken_read
>>> import time
>>> while not lost_locks:  # the worker notices within its sleep interval
...     time.sleep(0.01)
>>> lost_locks == [lock]
True
>>> lock.lost  # recorded before the callback ran, so already true here
True
>>> try:
...     lock.ensure_held()
... except portalocker.LockLostError as error:
...     print(error.channel, type(error.__cause__).__name__)
doomed_channel ConnectionError
>>> lock.release()  # never raises on account of the loss
>>> lock.lost  # still observable after release
True
>>> _ = lock.acquire()  # a fresh acquire resets the instance
>>> lock.lost
False
>>> lock.release()

The caveats, stated plainly rather than hidden:

- Under redis-py's default ``socket_timeout`` of five seconds, a read
  stalled that long counts as a loss. A holder that cannot complete a
  read cannot confirm ownership either, so this is deliberate, but a
  pathologically slow link can produce a false loss.
- The dedicated subscription connection speaks RESP2, because RESP3
  maintenance notifications drive a reconnect path in redis-py that
  bypasses the retry policy. If you need RESP3 on the subscription,
  supply ``subscription_connection_factory`` and disable maintenance
  notifications yourself; the factory must yield a client whose
  connections do not retry or reconnect.
- A holder running portalocker 4.1 or older still resubscribes
  silently after a kill, so the loss guarantee covers a channel only
  once every participant on it runs 4.2 or later.
- Loss detection rides on the socket. A half-open link that never
  delivers a TCP reset - a hard-powered-off peer, a silently
  partitioned network - only surfaces when something writes into the
  connection, so with ``health_check_interval=0`` (redis-py's default
  for a connection you supply yourself) such a partition goes
  undetected indefinitely. Set the interval on your connection so the
  periodic health-check ping turns the partition into a read error.
- A forked child inherits the lock object and the parent's sockets.
  The child's ``release`` (explicit or via garbage collection) only
  drops the child's local references; the network teardown is skipped
  outside the subscribing process, because an UNSUBSCRIBE over the
  inherited socket would silently revoke the parent's lock. A child
  that needs the lock must build its own instance.

From the revocation until the holder observes it, both the new and the
old holder run: detection is bounded (about one worker sleep interval
after the TCP layer notices), reaction is not. Only resource-side
fencing closes that window, and that is outside the lock's reach.

Crashed holders
-----------------

The connection-as-ownership design covers the common case on its own: a
clean release, a crash, or a dropped network all close the socket, and
Redis drops the subscriber immediately - no reaping needed. What is left
to handle is a subscriber Redis still *counts* but that has stopped
answering: wedged rather than gone, which would otherwise leave the
channel permanently inconsistent, since the subscriber count would never
again match the number of holders willing to answer a probe.

During a normal, contended `RedisLock.acquire`, that reaping is automatic
and internal. When a probe collects fewer holder replies than there are
counted subscribers, portalocker matches every ``CLIENT LIST`` entry
against the holder ids that did answer, kills (``CLIENT KILL``) any
connection that is named like a holder of this channel but is not among
them, and reports the probe as inconclusive so the caller retries against
the now-cleaned-up channel. Because ownership lives in the connection,
killing it is what releases that holder's lock.

For a standalone look at a channel, `RedisLock.probe` publishes the
same liveness ping a real acquisition would and returns one
`RedisLockHolder` per subscriber, without killing anything, without
leaving a subscription behind, and without touching the lock's own
state. An unanswered probe raises rather than reporting the channel as
free, because "nobody answered" and "nobody is there" are different
statements and confusing them is how double locks happen:

>>> holder = portalocker.RedisLock('liveness_channel', connection=connection)
>>> _ = holder.acquire()
>>> prober = portalocker.RedisLock('liveness_channel', connection=connection)
>>> [h.mode.value for h in prober.probe()]
['exclusive']
>>> holder.release()
>>> prober.probe()
[]

`RedisLock.check_or_kill_lock`, the liveness check from before 4.0.0,
is deprecated since 4.2.0 and will be removed in 5.0.0: its reap arm
kills connections based on a caller-chosen timeout with none of the
protocol discipline that protects live-but-slow holders inside
`RedisLock.acquire`, and since 4.0.0 it can only match this instance's
own connection name anyway. Use `RedisLock.probe` for the read-only
question and leave the reaping to `RedisLock.acquire`.

`fakeredis` does not implement ``CLIENT KILL``, so the reaping inside
`RedisLock.acquire` is only exercised against a live server in
`portalocker_tests/test_redis.py`; against `fakeredis`, the internal
cleanup helper is monkeypatched to a no-op so the rest of the contention
logic can still be tested without it.

Testing against fakeredis
----------------------------

Every example on this page uses `fakeredis` in place of a real Redis
server, matching `portalocker_tests/test_redis.py`:

>>> connection = fakeredis.FakeStrictRedis(
...     server=fakeredis.FakeServer(), decode_responses=True
... )
>>> lock = portalocker.RedisLock('test_channel', connection=connection)

A single ``FakeServer`` stands in for a real Redis instance, and separate
``FakeStrictRedis`` connections attached to the *same* server behave like
separate processes talking to the same server, which is what makes
contention testable without a network:

>>> server = fakeredis.FakeServer()
>>> conn_a = fakeredis.FakeStrictRedis(server=server, decode_responses=True)
>>> conn_b = fakeredis.FakeStrictRedis(server=server, decode_responses=True)
>>> first = portalocker.RedisLock('contended_channel', connection=conn_a)
>>> second = portalocker.RedisLock(
...     'contended_channel', connection=conn_b, fail_when_locked=True
... )
>>> _ = first.acquire()
>>> try:
...     second.acquire()
... except portalocker.AlreadyLocked:
...     print('contended, as expected')
contended, as expected
>>> first.release()

`portalocker_tests/test_redis.py` runs its suite against `fakeredis`
always, and against a live server too whenever one is reachable, through a
fixture that hands out a fresh connection *factory* rather than one shared
connection, so each test gets independent connections the way the
examples above do. The one gap `fakeredis` leaves is ``CLIENT KILL``, as
noted in `Crashed holders`_ above: tests that depend on it either run
against a live server only, or monkeypatch the reaping helper to a no-op
so the rest of a contention scenario is still covered without it.
