"""Replies buffered during the final retry sleep remain valid."""

import dataclasses
import json
import random
import time
import typing
from unittest import mock

import pytest
from redis import client

from portalocker import redis


@dataclasses.dataclass
class _DelayedReply:
    now: float = 0.0
    confirmation: bool = True

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def get_message(self, timeout: float) -> dict[str, object] | None:
        if self.confirmation:
            self.confirmation = False
            return {
                'type': 'subscribe',
                'pattern': None,
                'channel': 'reply-channel',
                'data': 1,
            }
        if self.now + timeout < 9.5:
            self.now += timeout
            return None
        self.now = max(self.now, 9.5)
        return {
            'type': 'message',
            'pattern': None,
            'channel': 'reply-channel',
            'data': json.dumps(
                {
                    'protocol': 1,
                    'holder_id': 'responsive-holder',
                    'mode': 'exclusive',
                }
            ),
        }


@pytest.mark.parametrize('reap', [True, False])
def test_collect_holders_reads_reply_buffered_during_final_sleep(
    monkeypatch: pytest.MonkeyPatch,
    reap: bool,
) -> None:
    """A holder answering before the deadline must not be declared absent."""
    transport: _DelayedReply = _DelayedReply()
    connection: mock.MagicMock = mock.MagicMock(spec=client.Redis)
    connection.pubsub_numsub.return_value = [('review-channel', 1)]
    connection.client_list.return_value = []
    pubsub: mock.MagicMock = mock.MagicMock(spec=client.PubSub)
    pubsub.get_message.side_effect = transport.get_message
    connection.pubsub.return_value = pubsub
    lock: redis.RedisLock = redis.RedisLock(
        'review-channel',
        connection=typing.cast('client.Redis', connection),
        thread_sleep_time=1.0,
    )
    monkeypatch.setattr(time, 'monotonic', lambda: transport.now)
    monkeypatch.setattr(time, 'sleep', transport.sleep)
    monkeypatch.setattr(random, 'random', lambda: 0.5)

    holders: list[redis.RedisLockHolder] | None = lock._collect_lock_holders(
        typing.cast('client.Redis', connection),
        expected_subscribers=1,
        timeout=10.0,
        reap=reap,
    )

    assert holders == [
        redis.RedisLockHolder(
            'responsive-holder', redis.RedisLockMode.EXCLUSIVE
        )
    ]
    connection.client_list.assert_not_called()
    pubsub.close.assert_called_once()
