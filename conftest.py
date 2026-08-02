"""Pytest configuration shared by the package, the tests and the docs.

The doctests in ``portalocker/``, ``docs/`` and ``README.rst`` create real
lock files. Without a fixture that moves them somewhere disposable they
write into the repository root, which is where the stray ``somefile``,
``test.lock`` and ``x`` entries in earlier working trees came from.
"""

from __future__ import annotations

import pathlib

import pytest


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
