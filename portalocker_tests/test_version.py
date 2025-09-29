"""Tests for version discovery logic in portalocker.__about__.

These tests verify that the runtime version is resolved from
importlib.metadata when available, and that it falls back to parsing the
pyproject.toml when metadata is unavailable. They also validate the
parser for pyproject.
"""

import importlib
from pathlib import Path
from typing import Callable, Optional

import pytest

import portalocker


@pytest.fixture()
def reload_about() -> Callable[[], None]:
    """Return a function to reload portalocker.__about__ cleanly.

    Returns:
        A function to call which reloads portalocker.__about__.
    """

    def _reload() -> None:
        import portalocker.__about__ as about

        importlib.reload(about)

    return _reload


def test_get_version_prefers_importlib_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_version should prefer importlib metadata, when available."""
    import portalocker.__about__ as about

    # Patch the version function via string path to avoid mypy export checks
    monkeypatch.setattr(
        'portalocker.__about__.importlib_metadata.version',
        lambda _name: '9.9.9',
        raising=True,
    )

    assert about.get_version() == '9.9.9'


def test_get_version_fallback_pyproject(
    monkeypatch: pytest.MonkeyPatch, reload_about: Callable[[], None]
) -> None:
    """get_version should fall back to reading pyproject.toml.

    This test patches importlib's metadata.version to raise and patches
    pathlib.Path.read_text to return a minimal pyproject.toml with a
    specific version. It then verifies both get_version() and module
    import-time __version__ resolve to the expected fallback version.
    """
    import portalocker.__about__ as about

    def _raise(_: str) -> str:
        raise RuntimeError('not installed')

    # Patch upstream importlib.metadata so the module alias picks it up
    monkeypatch.setattr('importlib.metadata.version', _raise, raising=True)

    def fake_read_text(
        _self: Path, encoding: str = 'utf-8', errors: Optional[str] = None
    ) -> str:
        return "[project]\nname = 'portalocker'\nversion = '1.2.3'\n"

    monkeypatch.setattr('pathlib.Path.read_text', fake_read_text, raising=True)

    # get_version should now read the fallback version
    assert about.get_version() == '1.2.3'

    # Reload the module so __version__ is recomputed at import time
    reload_about()
    import portalocker.__about__ as about2

    assert about2.__version__ == '1.2.3'


def test_read_pyproject_version_parses_value(tmp_path: Path) -> None:
    """_read_pyproject_version must parse [project].version value."""
    toml = "[project]\nname = 'portalocker'\nversion = '4.5.6'\n"
    path = tmp_path / 'pyproject.toml'
    path.write_text(toml, encoding='utf-8')

    import portalocker.__about__ as about

    assert about._read_pyproject_version(path) == '4.5.6'


def test_dunder_version_is_string() -> None:
    """portalocker.__version__ should be a non-empty string."""
    assert isinstance(portalocker.__version__, str)
    assert len(portalocker.__version__) > 0
