from __future__ import annotations

import pathlib
import subprocess
import sys
import textwrap

import pytest

import portalocker


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
        with portalocker.open_atomic(target) as temporary:
            written: int = temporary.write(b'temporary payload')
            assert written == len(b'temporary payload')
            target.write_bytes(b'concurrent winner')

    assert target.read_bytes() == b'concurrent winner'
    assert set(tmp_path.iterdir()) == entries_before | {target}
