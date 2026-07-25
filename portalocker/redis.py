# Several redis-py methods (`pubsub`, `unsubscribe`, `client_list`, ...) are
# unannotated or take untyped `**kwargs`, so their types are (partially)
# unknown to pyright.
# pyright: reportUnknownMemberType=false
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

DEFAULT_UNAVAILABLE_TIMEOUT = 1
DEFAULT_THREAD_SLEEP_TIME = 0.1
REDIS_LOCK_PROTOCOL_VERSION = 1


class RedisLockMode(str, enum.Enum):
    EXCLUSIVE = 'exclusive'
    PENDING = 'pending'
    SHARED = 'shared'


class RedisLockHolder(typing.NamedTuple):
    holder_id: str
    mode: RedisLockMode
    legacy: bool = False


class PubSubWorkerThread(redis.client.PubSubWorkerThread):
    def run(self) -> None:
        try:
            super().run()
        except Exception:  # pragma: no cover
            _thread.interrupt_main()
            raise


class RedisLock(utils.LockBase['RedisLock']):
    """
    An extremely reliable Redis lock based on pubsub with a keep-alive thread

    As opposed to most Redis locking systems based on key/value pairs,
    this locking method is based on the pubsub system. The big advantage is
    that if the connection gets killed due to network issues, crashing
    processes or otherwise, it will still immediately unlock instead of
    waiting for a lock timeout.

    To make sure both sides of the lock know about the connection state it is
    recommended to set the `health_check_interval` when creating the redis
    connection..

    Args:
        channel: the redis channel to use as locking key.
        connection: an optional redis connection if you already have one
        or if you need to specify the redis connection
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
        return f'{self.legacy_client_name}-{self.holder_id}'

    @property
    def legacy_client_name(self) -> str:
        return f'{self.channel}-lock'

    def _timeout_generator(
        self, timeout: float | None, check_interval: float | None
    ) -> typing.Iterator[int]:
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
        return self.flags == constants.LockFlags.SHARED and all(
            holder.mode is RedisLockMode.SHARED for holder in holders
        )

    def _writer_is_elected(
        self,
        holders: list[RedisLockHolder],
    ) -> bool:
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
        self.release()
