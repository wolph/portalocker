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


def test_open_atomic_cleans_temporary_file_after_publication_error(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target: pathlib.Path = tmp_path / 'destination.bin'
    temporary_paths: list[pathlib.Path] = []

    def fail_publication(source: str, destination: pathlib.Path) -> None:
        temporary_paths.append(pathlib.Path(source))
        assert destination == target
        raise OSError('publication failed')

    publication_function: str = 'rename' if os.name == 'nt' else 'link'
    monkeypatch.setattr(os, publication_function, fail_publication)

    with (
        pytest.raises(OSError, match='publication failed'),
        portalocker.open_atomic(target) as file_handle,
    ):
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

    with (
        pytest.raises(AssertionError, match='exists'),
        portalocker.open_atomic(target),
    ):
        pass

    script: str = textwrap.dedent(
        f"""\
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
        pytest.raises(FileExistsError),
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
    assert set(tmp_path.iterdir()) == entries_before | {target}
