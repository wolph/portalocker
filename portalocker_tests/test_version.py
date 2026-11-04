"""Tests for version discovery logic in portalocker.__about__.

These tests verify that the runtime version is resolved from
importlib.metadata when available, and that it falls back to parsing the
pyproject.toml when metadata is unavailable. They also validate the
parser for pyproject.
"""

import importlib
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import portalocker


@pytest.fixture()
def reload_about(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[], None]]:
    """Return a function to reload portalocker.__about__ cleanly.

    The module is reloaded once more at teardown, after the test's
    patches are undone, so a ``__version__`` computed under a patch
    does not leak into later tests that read the real version.

    Yields:
        A function to call which reloads portalocker.__about__.
    """
    import portalocker.__about__ as about

    def _reload() -> None:
        importlib.reload(about)

    yield _reload
    monkeypatch.undo()
    importlib.reload(about)


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
    monkeypatch: pytest.MonkeyPatch,
    reload_about: Callable[[], None],
    tmp_path: Path,
) -> None:
    """get_version should fall back to reading pyproject.toml.

    This test patches importlib's metadata.version to raise and points
    the package's resolved location at a temporary tree whose
    ``pyproject.toml`` names this package with a specific version. It
    then verifies both get_version() and module import-time __version__
    resolve to the expected fallback version through the real bounded
    reader.
    """
    import portalocker.__about__ as about

    def _raise(_: str) -> str:
        raise RuntimeError('not installed')

    # Patch upstream importlib.metadata so the module alias picks it up
    monkeypatch.setattr('importlib.metadata.version', _raise, raising=True)

    (tmp_path / 'pyproject.toml').write_text(
        "[project]\nname = 'portalocker'\nversion = '1.2.3'\n",
        encoding='utf-8',
    )
    fake_location: Path = tmp_path / 'portalocker' / '__about__.py'
    monkeypatch.setattr(
        Path, 'resolve', lambda _self, strict=False: fake_location
    )

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


def test_read_pyproject_version_is_bounded_and_linear(tmp_path: Path) -> None:
    """A huge file of ``[project]`` headers must not take quadratic time.

    The old lazy multiline regex re-scanned the rest of the file from
    every header, so a planted 1 MiB file above the package stalled the
    import. The parser now reads a bounded prefix and scans it once.
    """
    import portalocker.__about__ as about

    path = tmp_path / 'pyproject.toml'
    path.write_text('[project]\n' * (1024 * 1024 // 10), encoding='utf-8')

    started = time.perf_counter()
    assert about._read_pyproject_version(path) is None
    assert time.perf_counter() - started < 1.0


def test_read_pyproject_version_ignores_other_tables(tmp_path: Path) -> None:
    """A ``version`` under another table is not the project version."""
    import portalocker.__about__ as about

    path = tmp_path / 'pyproject.toml'
    path.write_text(
        "[tool.something]\nversion = '9.9.9'\n"
        "[project]\nname = 'portalocker'\n",
        encoding='utf-8',
    )

    assert about._read_pyproject_version(path) is None


def test_read_pyproject_version_requires_the_portalocker_name(
    tmp_path: Path,
) -> None:
    """Another project's ``pyproject.toml`` above the package is ignored."""
    import portalocker.__about__ as about

    path = tmp_path / 'pyproject.toml'
    path.write_text(
        "[project]\nname = 'somethingelse'\nversion = '9.9.9'\n",
        encoding='utf-8',
    )

    assert about._read_pyproject_version(path) is None


def test_read_pyproject_version_reads_the_real_pyproject() -> None:
    """The repository's own file still resolves, name check included."""
    import portalocker.__about__ as about

    root = Path(about.__file__).resolve().parent.parent
    assert about._read_pyproject_version(root / 'pyproject.toml') == (
        portalocker.__version__
    )


def test_read_prefix_returns_none_for_a_directory(tmp_path: Path) -> None:
    """A directory (not a regular file) reads as nothing."""
    import portalocker.__about__ as about

    assert about._read_prefix(tmp_path, 10) is None


def test_read_prefix_returns_none_when_the_read_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read error after the open is swallowed like an open error."""
    import os

    import portalocker.__about__ as about

    path = tmp_path / 'pyproject.toml'
    path.write_text("[project]\nname = 'portalocker'\n", encoding='utf-8')

    def failing_read(_fd: int, _limit: int) -> bytes:
        raise OSError('read failed')

    monkeypatch.setattr(os, 'read', failing_read)

    assert about._read_prefix(path, 10) is None


def test_read_pyproject_version_ignores_an_unquoted_version(
    tmp_path: Path,
) -> None:
    """Only a quoted TOML string is a version."""
    import portalocker.__about__ as about

    path = tmp_path / 'pyproject.toml'
    path.write_text(
        "[project]\nname = 'portalocker'\nversion = 1.2\n",
        encoding='utf-8',
    )

    assert about._read_pyproject_version(path) is None
