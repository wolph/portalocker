"""``portalocker.RedisLock`` without the optional ``redis`` package.

The ``redis`` dependency is optional. Before 4.1.1 the fallback bound
``RedisLock`` to `None`, so constructing it failed with the baffling
``TypeError: 'NoneType' object is not callable``. The fallback is now a
stub class whose constructor raises an ``ImportError`` naming the missing
dependency and the install command.

The suite always has ``redis`` installed (it is in the ``tests`` extra),
so the redis-less import is exercised in a subprocess that hides the
module with a ``sys.meta_path`` blocker, mirroring how the
``test_msvcrt_no_pywin32`` module hides pywin32.
"""

import pathlib
import subprocess
import sys

_REPO_ROOT: pathlib.Path = pathlib.Path(__file__).parent.parent

#: Import portalocker with ``redis`` hidden and check the stub's contract.
_HIDE_REDIS_SCRIPT: str = '''
import importlib.abc
import sys


class Blocker(importlib.abc.MetaPathFinder):
    """Make ``import redis`` fail as if the package were not installed."""

    def find_spec(self, name, path=None, target=None):
        if name == 'redis' or name.startswith('redis.'):
            raise ImportError('redis is hidden for this test')
        return None


sys.meta_path.insert(0, Blocker())

import portalocker

# The attribute still imports, is truthy, and is a class rather than the
# pre-4.1.1 ``None`` placeholder.
assert portalocker.RedisLock is not None
assert isinstance(portalocker.RedisLock, type), portalocker.RedisLock

try:
    portalocker.RedisLock('some_channel')
except ImportError as exc:
    message = str(exc)
    assert 'redis' in message, message
    assert 'pip install "portalocker[redis]"' in message, message
    print('OK')
else:
    raise SystemExit('RedisLock construction without redis did not raise')
'''


def test_redislock_without_redis_raises_importerror():
    result = subprocess.run(
        [sys.executable, '-c', _HIDE_REDIS_SCRIPT],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'OK' in result.stdout
