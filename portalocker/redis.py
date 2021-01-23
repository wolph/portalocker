import typing

from redis import client

from . import exceptions
from . import utils


class RedisLock(utils.LockBase):
    '''
    An extremely reliable Redis lock based on pubsub

    As opposed to most Redis locking systems based on key/value pairs,
    this locking method is based on the pubsub system. The big advantage is
    that if the connection gets killed due to network issues, crashing
    processes or otherwise, it will still immediately unlock instead of
    waiting for a lock timeout.

    Args:
        channel: the redis channel to use as locking key.
        connection: an optional redis connection if you already have one
        or if you need to specify the redis connection
        timeout: timeout when trying to acquire a lock
        check_interval: check interval while waiting
        fail_when_locked: after the initial lock failed, return an error
            or lock the file. This does not wait for the timeout.

    '''

    channel: str
    timeout: float
    connection: typing.Optional[client.Redis]
    pubsub: typing.Optional[client.PubSub] = None
    close_connection: bool

    def __init__(
            self,
            channel: str,
            connection: typing.Optional[client.Redis] = None,
            timeout: typing.Optional[float] = None,
            check_interval: typing.Optional[float] = None,
            fail_when_locked: typing.Optional[bool] = False,
    ):
        # We don't want to close connections given as an argument
        self.close_connection = not connection

        self.channel = channel
        self.connection = connection

        super(RedisLock, self).__init__(timeout=timeout,
                                        check_interval=check_interval,
                                        fail_when_locked=fail_when_locked)

    def get_connection(self) -> client.Redis:
        if not self.connection:
            self.connection = client.Redis()

        return self.connection

    def acquire(
            self, timeout: float = None, check_interval: float = None,
            fail_when_locked: typing.Optional[bool] = True):

        assert not self.pubsub, 'This lock is already active'
        connection = self.get_connection()

        timeout_generator = self._timeout_generator(timeout, check_interval)
        for _ in timeout_generator:  # pragma: no branch
            subscribers = connection.pubsub_numsub(self.channel)[0][1]

            if not subscribers:
                self.pubsub = connection.pubsub()
                self.pubsub.subscribe(self.channel)

                subscribers = connection.pubsub_numsub(self.channel)[0][1]
                if subscribers == 1:  # pragma: no branch
                    return self
                else:  # pragma: no cover
                    # Race condition, let's try again
                    self.release()

            if fail_when_locked:  # pragma: no branch
                raise exceptions.AlreadyLocked(exceptions)

    def release(self):
        if self.pubsub:
            self.pubsub.unsubscribe(self.channel)
            self.pubsub = None
