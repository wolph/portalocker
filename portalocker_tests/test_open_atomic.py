from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import textwrap
import typing

import pytest

import portalocker


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


def test_open_atomic_cleans_temporary_file_after_link_error(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target: pathlib.Path = tmp_path / 'destination.bin'
    temporary_paths: list[pathlib.Path] = []

    def fail_link(source: str, destination: pathlib.Path) -> None:
        temporary_paths.append(pathlib.Path(source))
        assert destination == target
        raise OSError('link publication failed')

    monkeypatch.setattr(os, 'link', fail_link)

    with pytest.raises(OSError, match='link publication failed'):
        with portalocker.open_atomic(target) as file_handle:
            temporary: typing.BinaryIO = typing.cast(
                typing.BinaryIO,
                file_handle,
            )
            written: int = temporary.write(b'unpublished payload')
            assert written == len(b'unpublished payload')

    assert len(temporary_paths) == 1
    assert not temporary_paths[0].exists()
    assert not target.exists()
    assert not any(tmp_path.iterdir())


def test_open_atomic_existing_destination_check_survives_optimization(
    tmp_path: pathlib.Path,
) -> None:
    target: pathlib.Path = tmp_path / 'destination.bin'
    existing: bytes = b'existing destination'
    expected_message: str = f'{target!r} exists'
    target.write_bytes(existing)

    with pytest.raises(AssertionError, match='exists'):
        with portalocker.open_atomic(target):
            pass

    script: str = textwrap.dedent(
        f'''\
        import pathlib

        import portalocker

        target: pathlib.Path = pathlib.Path({str(target)!r})
        expected: bytes = {existing!r}
        expected_message: str = {expected_message!r}
        try:
            with portalocker.open_atomic(target):
                pass
        except AssertionError as error:
            if str(error) != expected_message:
                raise
        else:
            raise RuntimeError('missing AssertionError under optimized Python')
        if target.read_bytes() != expected:
            raise RuntimeError('existing destination was modified')
        ''',
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

    with pytest.raises(FileExistsError):
        with portalocker.open_atomic(target) as file_handle:
            temporary: typing.BinaryIO = typing.cast(
                typing.BinaryIO,
                file_handle,
            )
            written: int = temporary.write(b'temporary payload')
            assert written == len(b'temporary payload')
            target.write_bytes(b'concurrent winner')

    assert target.read_bytes() == b'concurrent winner'
    assert set(tmp_path.iterdir()) == entries_before | {target}
