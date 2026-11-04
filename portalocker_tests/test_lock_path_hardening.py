"""Lock paths planted by somebody else must be refused, not followed.

Lock files live in directories other writers may share. These tests pin
the hardening from the 2026 security audit:

- `Lock._get_fh` refuses a symlink, a dangling symlink, a hard link and
  a FIFO at the lock path (findings 15 and 14 of run-2), so a mode ``w``
  lock cannot truncate somebody else's file through a planted entry
  and `acquire` cannot hang on a FIFO past its timeout.
- `PidFileLock._read_pid` reads a bounded amount from a regular file
  only (finding 12).
- `PidFileLock._write_pid` publishes through an exclusively created,
  randomly named temporary file (finding 13).
"""

from __future__ import annotations

import errno
import os
import pathlib
import stat
import typing

import pytest

import portalocker
from portalocker import exceptions
from portalocker.types import Mode

posix_only = pytest.mark.skipif(
    os.name != 'posix',
    reason='symlink, hard link and FIFO refusal is a POSIX guard',
)


@posix_only
@pytest.mark.parametrize('lock_mode', ['w', 'a', 'r+', 'wb'])
def test_lock_refuses_symlink_at_lock_path(
    tmp_path: pathlib.Path,
    lock_mode: str,
) -> None:
    """A symlink planted at the lock path is refused and its target kept."""
    target: pathlib.Path = tmp_path / 'target.txt'
    target.write_text('precious')
    link: pathlib.Path = tmp_path / 'lock'
    link.symlink_to(target)

    lock: portalocker.Lock[typing.Any] = portalocker.Lock(
        link,
        typing.cast('Mode', lock_mode),
        timeout=0,
    )
    with pytest.raises(OSError) as info:
        lock.acquire()

    assert not isinstance(info.value, exceptions.LockException)
    assert info.value.errno in (errno.ELOOP, errno.EMLINK)
    assert target.read_text() == 'precious'
    assert lock.fh is None


@posix_only
def test_lock_refuses_dangling_symlink_at_lock_path(
    tmp_path: pathlib.Path,
) -> None:
    """A dangling symlink must not create its target through the lock."""
    missing: pathlib.Path = tmp_path / 'elsewhere' / 'created.txt'
    missing.parent.mkdir()
    link: pathlib.Path = tmp_path / 'lock'
    link.symlink_to(missing)

    lock: portalocker.Lock[typing.IO[str]] = portalocker.Lock(
        link,
        'w',
        timeout=0,
    )
    with pytest.raises(OSError):
        lock.acquire()

    assert not missing.exists()


@posix_only
def test_lock_refuses_hard_link_at_lock_path(tmp_path: pathlib.Path) -> None:
    """A hard link to another file is refused, so it is never truncated."""
    target: pathlib.Path = tmp_path / 'target.txt'
    target.write_text('precious')
    link: pathlib.Path = tmp_path / 'lock'
    os.link(target, link)

    lock: portalocker.Lock[typing.IO[str]] = portalocker.Lock(
        link,
        'w',
        timeout=0,
    )
    with pytest.raises(OSError) as info:
        lock.acquire()

    assert info.value.errno == errno.EMLINK
    assert target.read_text() == 'precious'


@posix_only
@pytest.mark.timeout(5)
def test_lock_refuses_fifo_at_lock_path_without_blocking(
    tmp_path: pathlib.Path,
) -> None:
    """A FIFO at the lock path fails promptly instead of blocking open."""
    fifo: pathlib.Path = tmp_path / 'lock'
    os.mkfifo(fifo)

    lock: portalocker.Lock[typing.IO[str]] = portalocker.Lock(
        fifo,
        'a',
        timeout=0,
    )
    with pytest.raises(OSError):
        lock.acquire()


@posix_only
def test_temporary_file_lock_refuses_symlink_at_lock_path(
    tmp_path: pathlib.Path,
) -> None:
    """The mode ``w`` of `TemporaryFileLock` gets the same guard."""
    target: pathlib.Path = tmp_path / 'target.txt'
    target.write_text('precious')
    link: pathlib.Path = tmp_path / 'lock'
    link.symlink_to(target)

    lock: portalocker.TemporaryFileLock = portalocker.TemporaryFileLock(
        link,
        timeout=0,
    )
    with pytest.raises(OSError):
        lock.acquire()

    assert target.read_text() == 'precious'
    assert link.is_symlink()


@posix_only
def test_bounded_semaphore_refuses_symlink_at_slot_path(
    tmp_path: pathlib.Path,
) -> None:
    """A planted symlink at a slot path is refused rather than followed."""
    target: pathlib.Path = tmp_path / 'target.txt'
    target.write_text('precious')
    semaphore: portalocker.BoundedSemaphore = portalocker.BoundedSemaphore(
        1,
        name='planted',
        directory=str(tmp_path),
        timeout=0,
    )
    slot: pathlib.Path = pathlib.Path(semaphore.get_filenames()[0])
    slot.symlink_to(target)

    with pytest.raises(OSError):
        semaphore.acquire()

    assert target.read_text() == 'precious'


def test_lock_still_creates_and_truncates_a_regular_file(
    tmp_path: pathlib.Path,
) -> None:
    """The guard changes nothing for the ordinary regular-file case."""
    path: pathlib.Path = tmp_path / 'lock'
    path.write_text('old content')

    with portalocker.Lock(path, 'w', timeout=0) as fh:
        fh.write('new')
    assert path.read_text() == 'new'

    with portalocker.Lock(tmp_path / 'fresh', 'a', timeout=0):
        pass
    assert (tmp_path / 'fresh').is_file()


@posix_only
def test_lock_created_file_mode_matches_builtin_open(
    tmp_path: pathlib.Path,
) -> None:
    """The default opener creates with 0o666 minus umask, like `open`."""
    locked: pathlib.Path = tmp_path / 'lock'
    plain: pathlib.Path = tmp_path / 'plain'
    with portalocker.Lock(locked, 'a', timeout=0):
        pass
    with open(plain, 'a'):
        pass

    assert stat.S_IMODE(locked.stat().st_mode) == stat.S_IMODE(
        plain.stat().st_mode,
    )


@posix_only
def test_caller_opener_takes_over_the_guard(tmp_path: pathlib.Path) -> None:
    """A caller-supplied ``opener`` bypasses the guard, as documented."""
    target: pathlib.Path = tmp_path / 'target.txt'
    target.write_text('precious')
    link: pathlib.Path = tmp_path / 'lock'
    link.symlink_to(target)
    opened: list[str] = []

    def follow_links(path: str, flags: int) -> int:
        opened.append(path)
        return os.open(path, flags, 0o666)

    with portalocker.Lock(link, 'a', timeout=0, opener=follow_links):
        pass

    assert opened == [str(link)]


# --- PidFileLock._read_pid ---------------------------------------------


@posix_only
@pytest.mark.timeout(5)
def test_read_pid_returns_none_for_fifo_without_blocking(
    tmp_path: pathlib.Path,
) -> None:
    """A FIFO at the pid path reads as unreadable, promptly."""
    pid_path: pathlib.Path = tmp_path / 'daemon.pid'
    os.mkfifo(pid_path)
    lock: portalocker.PidFileLock = portalocker.PidFileLock(pid_path)

    assert lock.read_pid() is None


@posix_only
def test_read_pid_returns_none_for_symlink(tmp_path: pathlib.Path) -> None:
    """A symlink at the pid path is not followed."""
    real: pathlib.Path = tmp_path / 'real.pid'
    real.write_text('123')
    link: pathlib.Path = tmp_path / 'daemon.pid'
    link.symlink_to(real)
    lock: portalocker.PidFileLock = portalocker.PidFileLock(link)

    pid, error = lock._read_pid()

    assert pid is None
    assert isinstance(error, OSError)


def test_read_pid_bounds_the_read(tmp_path: pathlib.Path) -> None:
    """Content past the bound is junk, read only up to the bound."""
    pid_path: pathlib.Path = tmp_path / 'daemon.pid'
    pid_path.write_bytes(b'1' * 21 + b'\n' + b'x' * 10_000)
    lock: portalocker.PidFileLock = portalocker.PidFileLock(pid_path)

    assert lock.read_pid() is None

    pid_path.write_bytes(b'4242\n')
    assert lock.read_pid() == 4242


# --- PidFileLock._write_pid ---------------------------------------------


@posix_only
def test_write_pid_uses_exclusive_random_temp_name(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A planted entry at the only temp name the helper can pick is refused.

    With `os.urandom` pinned the helper has exactly one candidate name.
    A symlink pre-placed there must make the acquire fail without the
    symlink target being written to and without a pid file appearing.
    """
    monkeypatch.setattr(os, 'urandom', lambda n: b'\x42' * n)
    pid_path: pathlib.Path = tmp_path / 'daemon.pid'
    target: pathlib.Path = tmp_path / 'target.txt'
    target.write_text('precious')
    planted: pathlib.Path = tmp_path / f'.portalocker.{"42" * 8}.tmp'
    planted.symlink_to(target)
    lock: portalocker.PidFileLock = portalocker.PidFileLock(pid_path)

    with pytest.raises(OSError):
        lock.acquire()

    assert target.read_text() == 'precious'
    assert not pid_path.exists()
    assert planted.is_symlink()
    assert lock.read_pid() is None


def test_write_pid_leaves_no_predictable_temp_file(
    tmp_path: pathlib.Path,
) -> None:
    """The old ``<filename>.<pid>.tmp`` name is never used any more."""
    pid_path: pathlib.Path = tmp_path / 'daemon.pid'
    seen: list[str] = []
    original_replace: typing.Callable[..., None] = os.replace

    def record(src: typing.Any, dst: typing.Any, **kwargs: typing.Any) -> None:
        seen.append(os.fspath(src))
        original_replace(src, dst, **kwargs)

    lock: portalocker.PidFileLock = portalocker.PidFileLock(pid_path)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, 'replace', record)
        with lock.fail_closed():
            assert lock.read_pid() == os.getpid()

    assert seen
    assert all(
        pathlib.Path(name).name.startswith('.portalocker.') for name in seen
    )
    assert not list(tmp_path.glob('*.tmp'))
    assert not list(tmp_path.glob('.portalocker.*'))


@posix_only
def test_lock_refuses_a_fifo_opened_read_write(tmp_path: pathlib.Path) -> None:
    """A FIFO that opens without blocking is still refused after fstat."""
    fifo: pathlib.Path = tmp_path / 'lock'
    os.mkfifo(fifo)

    lock: portalocker.Lock[typing.IO[str]] = portalocker.Lock(
        fifo,
        'r+',
        timeout=0,
    )
    with pytest.raises(OSError) as info:
        lock.acquire()

    assert info.value.errno == errno.EINVAL


@posix_only
def test_read_pid_returns_none_for_a_directory(tmp_path: pathlib.Path) -> None:
    """A directory at the pid path is unreadable, with an EINVAL error."""
    lock: portalocker.PidFileLock = portalocker.PidFileLock(tmp_path)

    pid, error = lock._read_pid()

    assert pid is None
    assert isinstance(error, OSError)
    assert error.errno == errno.EINVAL


def test_read_pid_rejects_more_than_twenty_digits(
    tmp_path: pathlib.Path,
) -> None:
    """Twenty-one digits are junk even though they fit the read bound."""
    pid_path: pathlib.Path = tmp_path / 'daemon.pid'
    pid_path.write_bytes(b'1' * 21)
    lock: portalocker.PidFileLock = portalocker.PidFileLock(pid_path)

    assert lock.read_pid() is None


@posix_only
def test_explicit_opener_none_keeps_the_guard(tmp_path: pathlib.Path) -> None:
    """``opener=None`` is the builtin default, not a custom opener."""
    target: pathlib.Path = tmp_path / 'target.txt'
    target.write_text('precious')
    link: pathlib.Path = tmp_path / 'lock'
    link.symlink_to(target)

    lock: portalocker.Lock[typing.IO[str]] = portalocker.Lock(
        link,
        'a',
        timeout=0,
        opener=None,
    )
    with pytest.raises(OSError) as info:
        lock.acquire()

    assert info.value.errno == errno.ELOOP


@posix_only
def test_directory_at_lock_path_raises_is_a_directory_error(
    tmp_path: pathlib.Path,
) -> None:
    """A directory keeps raising ``IsADirectoryError`` in read modes too."""
    lock: portalocker.Lock[typing.IO[str]] = portalocker.Lock(
        tmp_path,
        'r',
        timeout=0,
    )
    with pytest.raises(IsADirectoryError):
        lock.acquire()
