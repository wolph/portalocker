"""Tests for version discovery logic in portalocker.__about__.

These tests verify that the runtime version is resolved from
importlib.metadata when available, and that it falls back to parsing the
pyproject.toml when metadata is unavailable. They also validate the
parser for pyproject.
"""

from __future__ import annotations

from pathlib import Path

import portalocker


def test_get_version_prefers_importlib_metadata(monkeypatch) -> None:
    """get_version should prefer importlib metadata, when available."""
    import portalocker.__about__ as about

    monkeypatch.setattr(
        about.importlib_metadata,
        'version',
        lambda _: '9.9.9',
        raising=True,
    )

    assert about.get_version() == '9.9.9'


def test_get_version_fallback_pyproject(monkeypatch) -> None:
    """get_version should fall back to reading pyproject.toml."""
    import portalocker.__about__ as about

    def _raise(_: str) -> str:
        raise RuntimeError('not installed')

    monkeypatch.setattr(about.importlib_metadata, 'version', _raise)
    monkeypatch.setattr(
        about, '_read_pyproject_version', lambda _: '1.2.3', raising=True
    )

    assert about.get_version() == '1.2.3'


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
