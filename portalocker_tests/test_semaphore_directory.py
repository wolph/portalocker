"""The default semaphore directory is private to the user."""

from __future__ import annotations

import errno
import os
import pathlib
import pickle
import stat
import tempfile
import typing

import pytest

import portalocker
from portalocker import utils

posix_only = pytest.mark.skipif(
    os.name != 'posix', reason='ownership and modes are POSIX concepts'
)


@pytest.fixture
def temp_root(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> pathlib.Path:
    """Point the system temporary directory at a fresh tree."""
    monkeypatch.setattr(tempfile, 'gettempdir', lambda: str(tmp_path))
    return tmp_path


def _expected_default(root: pathlib.Path) -> pathlib.Path:
    return root / f'portalocker-{os.getuid()}'


@posix_only
@pytest.mark.parametrize(
    'factory',
    [
        lambda: portalocker.NamedBoundedSemaphore(1, name='spam', timeout=0),
        lambda: portalocker.BoundedSemaphore(1, name='spam', timeout=0),
    ],
    ids=['named', 'plain'],
)
def test_default_directory_is_private_per_user(
    temp_root: pathlib.Path,
    factory: typing.Callable[[], portalocker.BoundedSemaphore],
) -> None:
    """The slots live in a 0700 directory owned by the current user."""
    semaphore: portalocker.BoundedSemaphore = factory()
    expected: pathlib.Path = _expected_default(temp_root)

    assert semaphore.directory == str(expected)
    assert not expected.exists()
    with semaphore:
        entry: os.stat_result = os.lstat(expected)
        assert stat.S_ISDIR(entry.st_mode)
        assert stat.S_IMODE(entry.st_mode) == 0o700
        assert entry.st_uid == os.getuid()
        assert (expected / 'spam.00.lock').is_file()


@posix_only
def test_default_directory_refuses_a_planted_symlink(
    temp_root: pathlib.Path,
) -> None:
    """A symlink at the default path is refused, not followed."""
    elsewhere: pathlib.Path = temp_root / 'elsewhere'
    elsewhere.mkdir()
    _expected_default(temp_root).symlink_to(elsewhere)
    semaphore: portalocker.NamedBoundedSemaphore = (
        portalocker.NamedBoundedSemaphore(1, name='spam', timeout=0)
    )

    with pytest.raises(OSError) as info:
        semaphore.acquire()

    assert info.value.errno == errno.ENOTDIR
    assert not list(elsewhere.iterdir())
    assert semaphore.lock is None


@posix_only
def test_default_directory_refuses_a_foreign_owner(
    temp_root: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory another user planted at the default path is refused."""
    _expected_default(temp_root).mkdir(mode=0o700)
    semaphore: portalocker.NamedBoundedSemaphore = (
        portalocker.NamedBoundedSemaphore(1, name='spam', timeout=0)
    )
    real_uid: int = os.getuid()
    monkeypatch.setattr(os, 'getuid', lambda: real_uid + 1)

    with pytest.raises(PermissionError):
        semaphore.acquire()

    assert semaphore.lock is None


@posix_only
def test_default_directory_mode_is_tightened(temp_root: pathlib.Path) -> None:
    """An own directory left open to others is closed before use."""
    expected: pathlib.Path = _expected_default(temp_root)
    expected.mkdir(mode=0o755)
    os.chmod(expected, 0o755)
    semaphore: portalocker.NamedBoundedSemaphore = (
        portalocker.NamedBoundedSemaphore(1, name='spam', timeout=0)
    )

    with semaphore:
        assert stat.S_IMODE(os.lstat(expected).st_mode) == 0o700


@posix_only
def test_explicit_directory_is_left_alone(temp_root: pathlib.Path) -> None:
    """An explicit directory is used as given, mode and all."""
    shared: pathlib.Path = temp_root / 'shared'
    shared.mkdir(mode=0o755)
    os.chmod(shared, 0o755)
    semaphore: portalocker.NamedBoundedSemaphore = (
        portalocker.NamedBoundedSemaphore(
            1, name='spam', directory=str(shared), timeout=0
        )
    )

    with semaphore:
        assert (shared / 'spam.00.lock').is_file()
    assert stat.S_IMODE(os.lstat(shared).st_mode) == 0o755
    assert not _expected_default(temp_root).exists()


def test_default_directory_on_windows_is_the_temp_dir(
    temp_root: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows temp dirs are per user already, so they stay the default."""
    monkeypatch.setattr(os, 'name', 'nt')

    assert utils._default_semaphore_directory() == str(temp_root)
    utils._ensure_private_directory(str(temp_root))
    assert not list(temp_root.glob('portalocker-*'))


@posix_only
def test_default_directory_survives_pickling(temp_root: pathlib.Path) -> None:
    """A transferred semaphore keeps using the private default."""
    semaphore: portalocker.NamedBoundedSemaphore = (
        portalocker.NamedBoundedSemaphore(1, name='spam', timeout=0)
    )
    twin: portalocker.NamedBoundedSemaphore = pickle.loads(  # noqa: S301
        pickle.dumps(semaphore)
    )

    assert twin.directory == semaphore.directory
    with twin:
        assert (_expected_default(temp_root) / 'spam.00.lock').is_file()
