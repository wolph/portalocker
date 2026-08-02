Redis Lock
==========

`RedisLock` coordinates processes across machines through a Redis pubsub
channel rather than a shared filesystem; see :doc:`lock-types` for where it
fits next to the file-based locks, and :doc:`quickstart` for installing
portalocker itself. This page is the deep dive: why the lock works this
way, installing the extra it needs, everyday usage, who owns the
underlying connection, how a wedged holder gets cleaned up, and how to
exercise all of it with `fakeredis` instead of a real server.

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
released at once. There is no expiry to wait out and no heartbeat to
refresh. The trade is that nothing is stored anywhere, so every
acquisition attempt has to ask the channel who is currently there instead
of reading a key.

That ask is a ping/pong published on the channel itself: a probing lock
publishes a ping carrying a private response channel, and every
subscriber answers with its holder id and current mode. Shared readers
hold the lock together; an exclusive writer holds it alone; and competing
writers agree on a single winner by sorting the pending holder ids they
all observed, with no coordinator and no message exchange beyond that one
probe.

Installation
-------------

`RedisLock` needs the ``redis`` package, which is an optional extra:

.. code-block:: console

    pip install "portalocker[redis]"

Without it, ``portalocker.RedisLock`` is `None` rather than an import
failure: `portalocker/__init__.py` imports `portalocker.redis` inside a
``try``/``except ImportError``, so the rest of the package stays usable
without the extra. A missing ``redis`` package therefore only surfaces
when something actually tries to *use* `RedisLock` - constructing it, or
noticing ``portalocker.RedisLock is None`` - not when ``import
portalocker`` itself runs.

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

`RedisLock.check_or_kill_lock` is a separate, public method for a
standalone liveness check. It predates per-holder ids, and
`RedisLock.acquire` no longer calls it internally; it answers only the
coarser question "is anybody answering on this channel at all?". It
waits up to ``timeout`` for its own subscription to confirm, then
publishes one ping and waits up to ``timeout`` again for a reply - a
fully silent channel can therefore take up to twice ``timeout`` to
resolve. If a reply arrives it returns `True`, and if nothing replies in
time it treats the channel as dead, kills whichever ``CLIENT LIST``
entry is named after *this instance's own* connection name, and returns
`None` - it never returns `False`:

>>> holder = portalocker.RedisLock('liveness_channel', connection=connection)
>>> _ = holder.acquire()
>>> prober = portalocker.RedisLock('liveness_channel')
>>> prober.check_or_kill_lock(connection, timeout=0.5)
True
>>> holder.release()

Because that reap step only matches this instance's own connection name,
calling `RedisLock.check_or_kill_lock` does not sweep up other processes'
crashed holders the way the automatic reaping inside `RedisLock.acquire`
does; it is a narrower, single-instance check, not the mechanism behind
everyday crash recovery. `fakeredis` does not implement ``CLIENT KILL``,
so the reaping half of both code paths - the reply-timeout branch of
`RedisLock.check_or_kill_lock` above, and the internal cleanup during
`RedisLock.acquire` - is only exercised against a live server in
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
