"""Pytest configuration shared by the package, the tests and the docs.

The doctests in ``portalocker/``, ``docs/`` and ``README.rst`` create real
lock files. Without a fixture that moves them somewhere disposable they
write into the repository root, which is where the stray ``somefile``,
``test.lock`` and ``x`` entries in earlier working trees came from.
"""

from __future__ import annotations

import pathlib

import pytest

#: Seconds allowed for a Redis doctest, overriding the global
#: ``--timeout=20``. Every ``acquire`` waits up to ``unavailable_timeout``
#: for existing holders to answer a ping, so a page with several of them
#: is legitimately slow: ~3s on Linux/macOS but over 20s on Windows, where
#: per-connection socket setup costs far more. ``portalocker_tests``
#: already grants its own contention tests ``@pytest.mark.timeout(180)``
#: for the same reason; doctests cannot carry markers, so they are granted
#: it here instead.
_REDIS_DOCTEST_TIMEOUT = 180


@pytest.fixture(autouse=True)
def _doctest_tmp_cwd(
    request: pytest.FixtureRequest,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run every doctest in a throwaway working directory.

    Only doctest items are relocated. The regular suite under
    ``portalocker_tests`` manages its own paths through the ``tmpfile``
    fixture and must keep the original working directory.
    """
    if isinstance(request.node, pytest.DoctestItem):
        monkeypatch.chdir(tmp_path)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Give the Redis doctests the timeout their contention scenarios need.

    Applies to the ``docs/redis.rst`` page and the ``portalocker/redis.py``
    module doctests only; every other doctest keeps the global timeout.
    """
    for item in items:
        if isinstance(item, pytest.DoctestItem) and 'redis' in item.nodeid:
            item.add_marker(pytest.mark.timeout(_REDIS_DOCTEST_TIMEOUT))
