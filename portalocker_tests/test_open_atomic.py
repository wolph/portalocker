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


@pytest.mark.parametrize('absolute', [False, True])
@pytest.mark.parametrize('as_string', [False, True])
@pytest.mark.parametrize('close_in_body', [False, True])
def test_open_atomic_publishes_after_chdir(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    absolute: bool,
    as_string: bool,
    close_in_body: bool,
) -> None:
    """Publication stays in the directory where the context was entered."""
    original = tmp_path / 'original'
    other = tmp_path / 'other'
    original.mkdir()
    other.mkdir()
    monkeypatch.chdir(original)
    target = pathlib.Path('result.bin')
    if absolute:
        target = original / target
    filename = str(target) if as_string else target

    with portalocker.open_atomic(filename) as file_handle:
        temporary = typing.cast(typing.BinaryIO, file_handle)
        temporary.write(b'complete payload')
        if close_in_body:
            temporary.close()
        monkeypatch.chdir(other)

    assert (original / 'result.bin').read_bytes() == b'complete payload'
    assert list(original.iterdir()) == [original / 'result.bin']
    assert not any(other.iterdir())


@pytest.mark.parametrize('absolute', [False, True])
@pytest.mark.parametrize('as_string', [False, True])
def test_open_atomic_cleans_up_after_chdir(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    absolute: bool,
    as_string: bool,
) -> None:
    """A failing body removes its temporary file after changing directory."""
    original = tmp_path / 'original'
    other = tmp_path / 'other'
    original.mkdir()
    other.mkdir()
    monkeypatch.chdir(original)
    target = pathlib.Path('result.bin')
    if absolute:
        target = original / target
    filename = str(target) if as_string else target

    with (
        pytest.raises(RuntimeError, match='body failed'),
        portalocker.open_atomic(filename) as file_handle,
    ):
        temporary = typing.cast(typing.BinaryIO, file_handle)
        temporary.write(b'incomplete payload')
        monkeypatch.chdir(other)
        raise RuntimeError('body failed')

    assert not any(original.iterdir())
    assert not any(other.iterdir())


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


@pytest.mark.skipif(os.name != 'posix', reason='entry swapping is POSIX only')
@pytest.mark.parametrize('planted', ['regular', 'symlink'])
def test_open_atomic_refuses_a_swapped_temporary_entry(
    tmp_path: pathlib.Path,
    planted: str,
) -> None:
    """The inode the body wrote is the only thing that may be published.

    Somebody who can write to the destination directory swaps the entry
    at the temporary name while the body runs. Publication must notice
    that the entry no longer names the written inode and refuse, leaving
    the destination absent and the planted target untouched.
    """
    destination: pathlib.Path = tmp_path / 'result.bin'
    victim: pathlib.Path = tmp_path / 'victim.bin'
    victim.write_bytes(b'victim payload')

    with (
        pytest.raises(OSError) as info,
        portalocker.open_atomic(destination) as file_handle,
    ):
        temporary = typing.cast(typing.BinaryIO, file_handle)
        temporary.write(b'caller payload')
        temporary.flush()
        temp_name: pathlib.Path = pathlib.Path(temporary.name)
        temp_name.unlink()
        if planted == 'regular':
            temp_name.write_bytes(b'planted payload')
        else:
            temp_name.symlink_to(victim)

    assert not isinstance(info.value, FileExistsError)
    assert not destination.exists()
    assert victim.read_bytes() == b'victim payload'


@pytest.mark.skipif(os.name != 'posix', reason='entry swapping is POSIX only')
def test_open_atomic_syncs_closed_handle_without_reopening_by_name(
    tmp_path: pathlib.Path,
) -> None:
    """A body that closed the handle is synced through the kept
    descriptor, so swapping the entry afterwards cannot redirect the
    fsync or the publication to a planted file.
    """
    destination: pathlib.Path = tmp_path / 'result.bin'
    victim: pathlib.Path = tmp_path / 'victim.bin'
    victim.write_bytes(b'victim payload')

    with (
        pytest.raises(OSError),
        portalocker.open_atomic(destination) as file_handle,
    ):
        temporary = typing.cast(typing.BinaryIO, file_handle)
        temporary.write(b'caller payload')
        temp_name = pathlib.Path(temporary.name)
        temporary.close()
        temp_name.unlink()
        temp_name.symlink_to(victim)

    assert not destination.exists()
    assert victim.read_bytes() == b'victim payload'


@pytest.mark.skipif(os.name != 'posix', reason='entry swapping is POSIX only')
def test_open_atomic_refuses_a_vanished_temporary_entry(
    tmp_path: pathlib.Path,
) -> None:
    """A temporary name that disappeared is reported as missing.

    The error type is the one 4.4.2 raised from the link call, so an
    ``except FileNotFoundError`` around ``open_atomic`` keeps working.
    """
    destination: pathlib.Path = tmp_path / 'result.bin'

    with (
        pytest.raises(OSError) as info,
        portalocker.open_atomic(destination) as file_handle,
    ):
        temporary = typing.cast(typing.BinaryIO, file_handle)
        temporary.write(b'caller payload')
        pathlib.Path(temporary.name).unlink()

    assert isinstance(info.value, FileNotFoundError)
    assert not destination.exists()


@pytest.mark.skipif(os.name != 'posix', reason='hard links are POSIX only')
def test_open_atomic_unlinks_a_destination_that_links_a_swapped_inode(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A swap that beats the pre-link check is caught after the link.

    ``os.link`` is replaced by one that links a decoy instead of the
    written inode, standing in for a swap landing between the identity
    check and the link call. The destination entry that resulted must
    be removed again and the publication must fail.
    """
    destination: pathlib.Path = tmp_path / 'result.bin'
    decoy: pathlib.Path = tmp_path / 'decoy.bin'
    decoy.write_bytes(b'decoy')
    real_link = os.link

    def link_decoy(
        src: typing.Any, dst: typing.Any, **kwargs: typing.Any
    ) -> None:
        real_link(decoy, dst, **kwargs)

    monkeypatch.setattr(portalocker.utils, '_PROC_FD_LINKS', False)
    monkeypatch.setattr(os, 'link', link_decoy)
    with (
        pytest.raises(OSError) as info,
        portalocker.open_atomic(destination) as file_handle,
    ):
        typing.cast(typing.BinaryIO, file_handle).write(b'caller payload')

    assert info.value.errno == portalocker.utils._STALE_ERRNO
    assert not destination.exists()
    assert decoy.read_bytes() == b'decoy'


@pytest.mark.skipif(os.name != 'posix', reason='hard links are POSIX only')
def test_open_atomic_rename_fallback_refuses_a_swapped_inode(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a filesystem without hard links the rename is checked too."""
    destination: pathlib.Path = tmp_path / 'result.bin'
    decoy: pathlib.Path = tmp_path / 'decoy.bin'
    decoy.write_bytes(b'decoy')
    real_rename = os.rename

    def refuse_link(*_args: typing.Any, **_kwargs: typing.Any) -> None:
        raise OSError(errno.ENOTSUP, 'no hard links here')

    def rename_decoy(
        src: typing.Any, dst: typing.Any, **kwargs: typing.Any
    ) -> None:
        real_rename(decoy, dst, **kwargs)

    monkeypatch.setattr(portalocker.utils, '_PROC_FD_LINKS', False)
    monkeypatch.setattr(os, 'link', refuse_link)
    monkeypatch.setattr(os, 'rename', rename_decoy)
    with (
        pytest.raises(OSError) as info,
        portalocker.open_atomic(destination) as file_handle,
    ):
        typing.cast(typing.BinaryIO, file_handle).write(b'caller payload')

    assert info.value.errno == portalocker.utils._STALE_ERRNO
    assert not destination.exists()


@pytest.mark.skipif(os.name != 'posix', reason='hard links are POSIX only')
def test_open_atomic_links_from_the_descriptor_where_supported(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``/proc/self/fd`` links the temporary name is never used."""
    destination: pathlib.Path = tmp_path / 'result.bin'
    sources: list[str] = []
    real_link = os.link
    temp_names: list[str] = []
    real_open_exclusive = portalocker.utils._open_exclusive_temp

    def recording_open_exclusive(
        path: pathlib.Path, binary: bool
    ) -> tuple[typing.IO[typing.Any], str]:
        handle, name = real_open_exclusive(path, binary)
        temp_names.append(name)
        return handle, name

    def link_from_descriptor(
        src: typing.Any, dst: typing.Any, **kwargs: typing.Any
    ) -> None:
        sources.append(str(src))
        # macOS has no /proc: resolve the descriptor link by hand.
        real_link(temp_names[-1], dst)

    monkeypatch.setattr(portalocker.utils, '_PROC_FD_LINKS', True)
    monkeypatch.setattr(
        portalocker.utils, '_open_exclusive_temp', recording_open_exclusive
    )
    monkeypatch.setattr(os, 'link', link_from_descriptor)
    with portalocker.open_atomic(destination) as file_handle:
        typing.cast(typing.BinaryIO, file_handle).write(b'payload')

    assert sources and sources[0].startswith('/proc/self/fd/')
    assert destination.read_bytes() == b'payload'


@pytest.mark.skipif(os.name != 'posix', reason='hard links are POSIX only')
@pytest.mark.parametrize('proc_present', [False, True])
def test_open_atomic_descriptor_link_failure_handling(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    proc_present: bool,
) -> None:
    """ENOENT without ``/proc`` falls back by name; otherwise it raises."""
    destination: pathlib.Path = tmp_path / 'result.bin'
    real_link = os.link
    real_exists = os.path.exists

    def failing_descriptor_link(
        src: typing.Any, dst: typing.Any, **kwargs: typing.Any
    ) -> None:
        if str(src).startswith('/proc/self/fd/'):
            raise OSError(errno.ENOENT, 'no /proc here')
        real_link(src, dst, **kwargs)

    monkeypatch.setattr(portalocker.utils, '_PROC_FD_LINKS', True)
    monkeypatch.setattr(os, 'link', failing_descriptor_link)
    monkeypatch.setattr(
        os.path,
        'exists',
        lambda p: (
            proc_present if str(p) == '/proc/self/fd' else real_exists(p)
        ),
    )
    if proc_present:
        with (
            pytest.raises(OSError) as info,
            portalocker.open_atomic(destination) as file_handle,
        ):
            typing.cast(typing.BinaryIO, file_handle).write(b'payload')
        assert info.value.errno == errno.ENOENT
        assert not destination.exists()
    else:
        with portalocker.open_atomic(destination) as file_handle:
            typing.cast(typing.BinaryIO, file_handle).write(b'payload')
        assert destination.read_bytes() == b'payload'
