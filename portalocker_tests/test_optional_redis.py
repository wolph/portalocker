"""``portalocker.RedisLock`` without the optional ``redis`` package.

The ``redis`` dependency is optional. Before 4.2.0 the fallback bound
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
# pre-4.2.0 ``None`` placeholder.
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


def test_redislock_stub_import_and_raise_in_process():
    """Execute the redis-less import fallback under coverage measurement.

    The subprocess test above proves the user-facing behaviour, but a
    subprocess leaves no trace in the coverage data, which is how the
    fallback stayed hidden behind a ``pragma: no cover`` for years. Here
    the package's ``__init__.py`` is executed a second time, in this
    process, under an alias whose ``.redis`` submodule import is blocked,
    so the ``except ImportError`` branch and the stub class body are
    measured like any other code.
    """
    import importlib.abc
    import importlib.machinery
    import importlib.util
    import types as types_module

    alias = '_portalocker_no_redis'
    package_dir = _REPO_ROOT / 'portalocker'

    class SubmoduleBlocker(importlib.abc.MetaPathFinder):
        """Make ``from .redis import ...`` fail inside the aliased package."""

        def find_spec(
            self,
            name: str,
            path: object = None,
            target: object = None,
        ) -> None:
            if name == f'{alias}.redis':
                raise ImportError('redis is hidden for this test')
            return

    spec: importlib.machinery.ModuleSpec | None = (
        importlib.util.spec_from_file_location(
            alias,
            package_dir / '__init__.py',
            submodule_search_locations=[str(package_dir)],
        )
    )
    assert spec is not None
    assert spec.loader is not None
    module: types_module.ModuleType = importlib.util.module_from_spec(spec)

    blocker = SubmoduleBlocker()
    sys.meta_path.insert(0, blocker)
    sys.modules[alias] = module
    try:
        spec.loader.exec_module(module)

        stub: type = module.RedisLock
        assert isinstance(stub, type)
        try:
            stub('some_channel')
        except ImportError as exc:
            assert 'pip install "portalocker[redis]"' in str(exc)
        else:
            raise AssertionError(
                'stub RedisLock construction did not raise ImportError'
            )
    finally:
        sys.meta_path.remove(blocker)
        for name in [n for n in sys.modules if n.split('.')[0] == alias]:
            del sys.modules[name]
