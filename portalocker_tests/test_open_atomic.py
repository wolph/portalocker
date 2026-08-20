from __future__ import annotations

import errno
import os
import pathlib
import stat
import subprocess
import sys
import textwrap
import typing

import pytest

import portalocker

# The hard link publication (and therefore its rename fallback for
# filesystems without hard links) only exists on the POSIX code path.
# Windows always publishes with an atomic rename.
posix_hard_link_only = pytest.mark.skipif(
    os.name == 'nt',
    reason='POSIX-only hard link publication',
)


def test_open_atomic_publishes_without_leaving_temporary_file(
    tmp_path: pathlib.Path,
) -> None:
    target: pathlib.Path = tmp_path / 'destination.bin'
    entries_before: set[pathlib.Path] = set(tmp_path.iterdir())

    with portalocker.open_atomic(target) as file_handle:
        temporary: typing.BinaryIO = typing.cast(typing.BinaryIO, file_handle)
        written: int = temporary.write(b'published payload')
        assert written == len(b'published payload')

    assert target.read_bytes() == b'published payload'
    assert set(tmp_path.iterdir()) == entries_before | {target}


def test_open_atomic_uses_platform_publication_primitive(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target: pathlib.Path = tmp_path / 'destination.bin'
    publication_calls: list[tuple[pathlib.Path, pathlib.Path]] = []
    real_replace: typing.Callable[[str, pathlib.Path], None] = typing.cast(
        typing.Callable[[str, pathlib.Path], None],
        os.replace,
    )

    def fake_publication(source: str, destination: pathlib.Path) -> None:
        publication_calls.append((pathlib.Path(source), destination))
        real_replace(source, destination)

    def fail_unused_publication(
        source: str,
        destination: pathlib.Path,
    ) -> None:
        raise AssertionError(
            f'unexpected publication call: {source!r} -> {destination!r}',
        )

    publication_function: str = 'rename' if os.name == 'nt' else 'link'
    unused_publication_function: str = 'link' if os.name == 'nt' else 'rename'
    monkeypatch.setattr(os, publication_function, fake_publication)
    monkeypatch.setattr(
        os,
        unused_publication_function,
        fail_unused_publication,
    )

    with portalocker.open_atomic(target) as file_handle:
        temporary: typing.BinaryIO = typing.cast(typing.BinaryIO, file_handle)
        written: int = temporary.write(b'published payload')
        assert written == len(b'published payload')

    assert target.read_bytes() == b'published payload'
    assert len(publication_calls) == 1
    assert publication_calls[0][1] == target
    assert not publication_calls[0][0].exists()


def test_open_atomic_preserves_payload_after_publication_error(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed publication must keep the temporary file and say where.

    Deleting the temporary file after a failed publication (as 4.0.0 did)
    destroys the caller's payload with no way to recover it.

    The location note is asserted against the exception's ``args``, not
    its ``str()``: the staged error is a bare `OSError` without a
    ``strerror``, so the note is appended as an extra argument and a
    multi-argument exception stringifies as the repr of its args tuple.
    On Windows that repr escapes every path backslash, so substring
    matching the raw path against ``str()`` fails there while the note
    itself is present and correct.
    """
    target: pathlib.Path = tmp_path / 'destination.bin'
    temporary_paths: list[pathlib.Path] = []

    def fail_publication(source: str, destination: pathlib.Path) -> None:
        temporary_paths.append(pathlib.Path(source))
        assert destination == target
        raise OSError('publication failed')

    publication_function: str = 'rename' if os.name == 'nt' else 'link'
    monkeypatch.setattr(os, publication_function, fail_publication)

    with (
        pytest.raises(OSError, match='publication failed') as exc_info,
        portalocker.open_atomic(target) as file_handle,
    ):
        temporary: typing.BinaryIO = typing.cast(
            typing.BinaryIO,
            file_handle,
        )
        written: int = temporary.write(b'unpublished payload')
        assert written == len(b'unpublished payload')

    assert len(temporary_paths) == 1
    assert temporary_paths[0].exists(), 'the payload must not be destroyed'
    assert temporary_paths[0].read_bytes() == b'unpublished payload'
    note: str = f'payload preserved at {temporary_paths[0]}'
    assert note in exc_info.value.args
    assert not target.exists()


def test_open_atomic_existing_destination_check_survives_optimization(
    tmp_path: pathlib.Path,
) -> None:
    target: pathlib.Path = tmp_path / 'destination.bin'
    existing: bytes = b'existing destination'
    target.write_bytes(existing)

    with (
        pytest.raises(FileExistsError) as exc_info,
        portalocker.open_atomic(target),
    ):
        pass

    assert exc_info.value.filename == str(target)

    script: str = textwrap.dedent(
        f"""\
        import pathlib

        import portalocker

        target: pathlib.Path = pathlib.Path({str(target)!r})
        expected: bytes = {existing!r}
        try:
            with portalocker.open_atomic(target):
                pass
        except FileExistsError as error:
            if error.filename != str(target):
                raise
        else:
            raise RuntimeError('missing FileExistsError under optimized '
                               'Python')
        if target.read_bytes() != expected:
            raise RuntimeError('existing destination was modified')
        """,
    )
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, '-O', '-c', script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout == ''
    assert completed.stderr == ''
    assert target.read_bytes() == existing


def test_open_atomic_preserves_destination_created_before_publication(
    tmp_path: pathlib.Path,
) -> None:
    target: pathlib.Path = tmp_path / 'destination.bin'
    entries_before: set[pathlib.Path] = set(tmp_path.iterdir())

    with (
        pytest.raises(FileExistsError) as exc_info,
        portalocker.open_atomic(target) as file_handle,
    ):
        temporary: typing.BinaryIO = typing.cast(
            typing.BinaryIO,
            file_handle,
        )
        written: int = temporary.write(b'temporary payload')
        assert written == len(b'temporary payload')
        target.write_bytes(b'concurrent winner')

    assert target.read_bytes() == b'concurrent winner'

    # The losing payload survives in the temporary file, and the raised
    # exception says where to find it.
    extras: set[pathlib.Path] = (
        set(tmp_path.iterdir()) - entries_before - {target}
    )
    assert len(extras) == 1
    preserved: pathlib.Path = extras.pop()
    assert preserved.read_bytes() == b'temporary payload'
    assert str(preserved) in str(exc_info.value)


@posix_hard_link_only
@pytest.mark.parametrize(
    'errno_code',
    [errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK],
)
def test_open_atomic_falls_back_to_rename_without_hard_links(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    errno_code: int,
) -> None:
    """Filesystems without hard links (exFAT, some SMB/NFS/FUSE mounts)
    must still publish, through the rename fallback.
    """
    target: pathlib.Path = tmp_path / 'destination.bin'
    entries_before: set[pathlib.Path] = set(tmp_path.iterdir())

    def fail_link(source: str, destination: pathlib.Path) -> None:
        raise OSError(errno_code, 'hard links unsupported')

    monkeypatch.setattr(os, 'link', fail_link)

    with portalocker.open_atomic(target) as file_handle:
        temporary: typing.BinaryIO = typing.cast(typing.BinaryIO, file_handle)
        written: int = temporary.write(b'fallback payload')
        assert written == len(b'fallback payload')

    assert target.read_bytes() == b'fallback payload'
    assert set(tmp_path.iterdir()) == entries_before | {target}


@posix_hard_link_only
def test_open_atomic_fallback_refuses_existing_destination(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rename fallback must keep the no-replace guarantee: an existing
    destination raises ``FileExistsError`` and stays untouched.
    """
    target: pathlib.Path = tmp_path / 'destination.bin'

    def fail_link(source: str, destination: pathlib.Path) -> None:
        raise OSError(errno.ENOTSUP, 'hard links unsupported')

    monkeypatch.setattr(os, 'link', fail_link)

    with (
        pytest.raises(FileExistsError) as exc_info,
        portalocker.open_atomic(target) as file_handle,
    ):
        temporary: typing.BinaryIO = typing.cast(typing.BinaryIO, file_handle)
        written: int = temporary.write(b'losing payload')
        assert written == len(b'losing payload')
        target.write_bytes(b'concurrent winner')

    assert exc_info.value.filename == str(target)
    assert target.read_bytes() == b'concurrent winner'
    assert str(exc_info.value.__cause__) == (
        f'[Errno {errno.ENOTSUP}] hard links unsupported'
    )


@posix_hard_link_only
def test_open_atomic_fallback_refuses_dangling_symlink_destination(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rename fallback must refuse a dangling symlink destination.

    The hard link refuses it (the symlink itself occupies the name), so
    the fallback's existence check has to use ``lexists``. A plain
    ``exists`` follows the symlink, reports the name as free and lets
    the rename silently replace the symlink.
    """
    target: pathlib.Path = tmp_path / 'destination.bin'

    def fail_link(source: str, destination: pathlib.Path) -> None:
        raise OSError(errno.ENOTSUP, 'hard links unsupported')

    monkeypatch.setattr(os, 'link', fail_link)

    with (
        pytest.raises(FileExistsError) as exc_info,
        portalocker.open_atomic(target) as file_handle,
    ):
        temporary: typing.BinaryIO = typing.cast(typing.BinaryIO, file_handle)
        written: int = temporary.write(b'losing payload')
        assert written == len(b'losing payload')
        target.symlink_to(tmp_path / 'nowhere')

    assert exc_info.value.filename == str(target)
    assert target.is_symlink(), 'the fallback replaced the symlink'
    assert not (tmp_path / 'nowhere').exists()


@posix_hard_link_only
def test_open_atomic_fallback_failure_preserves_payload(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the fallback rename fails as well, the payload survives in the
    temporary file and the raised exception names its path.
    """
    target: pathlib.Path = tmp_path / 'destination.bin'
    temporary_paths: list[pathlib.Path] = []

    def fail_link(source: str, destination: pathlib.Path) -> None:
        raise OSError(errno.ENOTSUP, 'hard links unsupported')

    def fail_rename(source: str, destination: pathlib.Path) -> None:
        temporary_paths.append(pathlib.Path(source))
        raise OSError(errno.EIO, 'device failed mid-rename')

    monkeypatch.setattr(os, 'link', fail_link)
    monkeypatch.setattr(os, 'rename', fail_rename)

    with (
        pytest.raises(OSError, match='device failed') as exc_info,
        portalocker.open_atomic(target) as file_handle,
    ):
        temporary: typing.BinaryIO = typing.cast(typing.BinaryIO, file_handle)
        written: int = temporary.write(b'surviving payload')
        assert written == len(b'surviving payload')

    assert len(temporary_paths) == 1
    assert temporary_paths[0].exists(), 'the payload must not be destroyed'
    assert temporary_paths[0].read_bytes() == b'surviving payload'
    assert str(temporary_paths[0]) in str(exc_info.value)
    assert not target.exists()


def test_open_atomic_removes_temporary_file_when_body_raises(
    tmp_path: pathlib.Path,
) -> None:
    """An exception in the caller's body must not leak the temporary file.

    The exception propagates through the yield, so the publication code
    never runs. Without explicit cleanup every failed attempt would leave
    one orphaned temporary file behind.
    """
    target: pathlib.Path = tmp_path / 'destination.bin'

    with (
        pytest.raises(RuntimeError, match='body failed'),
        portalocker.open_atomic(target) as file_handle,
    ):
        temporary: typing.BinaryIO = typing.cast(typing.BinaryIO, file_handle)
        written: int = temporary.write(b'partial payload')
        assert written == len(b'partial payload')
        raise RuntimeError('body failed')

    assert not target.exists()
    assert not any(tmp_path.iterdir()), 'body failure leaked a temp file'


def test_open_atomic_tolerates_handle_closed_in_body(
    tmp_path: pathlib.Path,
) -> None:
    """A body that closes the handle itself must still publish cleanly.

    Flushing a closed handle raises ``ValueError``, so the closed case
    has to be detected and synchronized through a fresh descriptor.
    """
    target: pathlib.Path = tmp_path / 'destination.bin'

    with portalocker.open_atomic(target) as file_handle:
        temporary: typing.BinaryIO = typing.cast(typing.BinaryIO, file_handle)
        written: int = temporary.write(b'closed early')
        assert written == len(b'closed early')
        temporary.close()

    assert target.read_bytes() == b'closed early'
    assert set(tmp_path.iterdir()) == {target}


def test_open_atomic_retries_a_colliding_temporary_name(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A random temporary name that already exists must be rolled again.

    The exclusive create refuses the occupied name instead of truncating
    it, and the occupant must survive untouched.
    """
    target: pathlib.Path = tmp_path / 'destination.bin'
    tokens: list[bytes] = [b'\x00' * 8, b'\xff' * 8]

    def fake_urandom(count: int) -> bytes:
        assert count == 8
        return tokens.pop(0)

    first_token_hex: str = tokens[0].hex()
    colliding: pathlib.Path = tmp_path / f'.portalocker.{first_token_hex}.tmp'
    colliding.write_bytes(b'occupied')
    monkeypatch.setattr(os, 'urandom', fake_urandom)

    with portalocker.open_atomic(target) as file_handle:
        temporary: typing.BinaryIO = typing.cast(typing.BinaryIO, file_handle)
        written: int = temporary.write(b'payload')
        assert written == len(b'payload')

    assert tokens == [], 'expected exactly one retry'
    assert target.read_bytes() == b'payload'
    assert colliding.read_bytes() == b'occupied'


def test_open_atomic_bounds_the_temporary_name_retries(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken entropy source must surface as OSError, not a hang.

    With every random name colliding, the bounded retry gives up after
    ``_TEMP_NAME_ATTEMPTS`` attempts. The error is a plain ``OSError``
    rather than ``FileExistsError``, so it cannot be mistaken for the
    destination already existing.
    """
    target: pathlib.Path = tmp_path / 'destination.bin'
    urandom_calls: list[int] = []

    def constant_urandom(count: int) -> bytes:
        urandom_calls.append(count)
        return b'\x00' * count

    colliding: pathlib.Path = tmp_path / f'.portalocker.{"00" * 8}.tmp'
    colliding.write_bytes(b'occupied')
    monkeypatch.setattr(os, 'urandom', constant_urandom)
    monkeypatch.setattr(portalocker.utils, '_TEMP_NAME_ATTEMPTS', 3)

    with (
        pytest.raises(OSError, match='no usable temporary file name') as (
            exc_info
        ),
        portalocker.open_atomic(target),
    ):
        pass

    assert type(exc_info.value) is OSError
    assert len(urandom_calls) == 3
    assert not target.exists()
    assert colliding.read_bytes() == b'occupied'


@pytest.mark.skipif(
    os.name == 'nt',
    reason='Windows MAX_PATH limits are unrelated to the temp name pattern',
)
@pytest.mark.parametrize('use_fallback', [False, True])
def test_open_atomic_supports_maximum_length_basenames(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    use_fallback: bool,
) -> None:
    """A 255 byte destination basename must publish successfully.

    The temporary name is a fixed 33 bytes and must not embed the
    destination's basename: a pattern that prepends the destination name
    pushes any basename of 234+ bytes over the filesystem's 255 byte
    limit and fails with ``ENAMETOOLONG``, where 3.2.0 published fine.
    Covers both the hard link path and the rename fallback.
    """
    target: pathlib.Path = tmp_path / ('a' * 255)
    if use_fallback:

        def fail_link(source: str, destination: pathlib.Path) -> None:
            raise OSError(errno.ENOTSUP, 'hard links unsupported')

        monkeypatch.setattr(os, 'link', fail_link)

    with portalocker.open_atomic(target) as file_handle:
        temporary: typing.BinaryIO = typing.cast(typing.BinaryIO, file_handle)
        written: int = temporary.write(b'long name payload')
        assert written == len(b'long name payload')

    assert target.read_bytes() == b'long name payload'
    assert set(tmp_path.iterdir()) == {target}


def test_open_atomic_never_touches_the_process_umask(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The umask must never be modified, not even briefly.

    ``os.umask`` is process-global: a round-trip to read it opens a
    window in which every other thread creates world-writable files.
    The permissions have to come from the kernel applying the umask at
    creation instead.
    """
    target: pathlib.Path = tmp_path / 'destination.bin'

    def forbidden_umask(mask: int) -> int:
        raise AssertionError(f'os.umask({mask:#o}) called during publish')

    monkeypatch.setattr(os, 'umask', forbidden_umask)

    with portalocker.open_atomic(target) as file_handle:
        temporary: typing.BinaryIO = typing.cast(typing.BinaryIO, file_handle)
        written: int = temporary.write(b'payload')
        assert written == len(b'payload')

    assert target.read_bytes() == b'payload'


def test_open_atomic_publishes_with_plain_open_permissions(
    tmp_path: pathlib.Path,
) -> None:
    """The published file must carry the permissions a plain ``open``
    gives, not the private ``0o600`` of ``NamedTemporaryFile``.
    """
    atomic_target: pathlib.Path = tmp_path / 'atomic.bin'
    plain_target: pathlib.Path = tmp_path / 'plain.bin'

    with portalocker.open_atomic(atomic_target) as file_handle:
        temporary: typing.BinaryIO = typing.cast(typing.BinaryIO, file_handle)
        written: int = temporary.write(b'payload')
        assert written == len(b'payload')
    plain_target.write_bytes(b'payload')

    atomic_mode: int = stat.S_IMODE(atomic_target.stat().st_mode)
    plain_mode: int = stat.S_IMODE(plain_target.stat().st_mode)
    assert atomic_mode == plain_mode


def test_open_atomic_removes_temporary_file_on_keyboard_interrupt(
    tmp_path: pathlib.Path,
) -> None:
    """A ``KeyboardInterrupt`` in the body must clean up like any other
    body failure: the temporary file is removed and nothing is published.
    ``except Exception`` would miss it, so this pins the ``BaseException``
    handling.
    """
    target: pathlib.Path = tmp_path / 'destination.bin'

    with (
        pytest.raises(KeyboardInterrupt),
        portalocker.open_atomic(target) as file_handle,
    ):
        temporary: typing.BinaryIO = typing.cast(typing.BinaryIO, file_handle)
        written: int = temporary.write(b'partial payload')
        assert written == len(b'partial payload')
        raise KeyboardInterrupt

    assert not target.exists()
    assert not any(tmp_path.iterdir()), 'the interrupt leaked a temp file'
