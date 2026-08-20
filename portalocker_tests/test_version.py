"""Tests for version discovery logic in portalocker.__about__.

These tests verify that the runtime version is resolved from
importlib.metadata when available, and that it falls back to parsing the
pyproject.toml when metadata is unavailable. They also validate the
parser for pyproject.
"""

import importlib
from collections.abc import Callable
from pathlib import Path

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
        _self: Path, encoding: str = 'utf-8', errors: str | None = None
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


def test_read_pyproject_version_returns_none_when_unreadable(
    tmp_path: Path,
) -> None:
    """An unreadable pyproject file must yield ``None``, not an error.

    Version discovery is best-effort: a source tree without a readable
    ``pyproject.toml`` (here simply a path that does not exist) makes
    ``_read_pyproject_version`` swallow the read failure and report
    ``None`` so ``get_version`` can fall through to its last-resort
    default.
    """
    import portalocker.__about__ as about

    assert about._read_pyproject_version(tmp_path / 'missing.toml') is None


def test_get_version_last_resort_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no metadata and no pyproject, ``get_version`` returns 0.0.0.

    Both discovery mechanisms failing at once (no installed
    distribution, no parseable pyproject) still must produce a version
    string rather than an exception at import time.
    """
    import portalocker.__about__ as about

    def _raise(_: str) -> str:
        raise RuntimeError('not installed')

    monkeypatch.setattr('importlib.metadata.version', _raise, raising=True)
    monkeypatch.setattr(
        about, '_read_pyproject_version', lambda _path: None, raising=True
    )

    assert about.get_version() == '0.0.0'
