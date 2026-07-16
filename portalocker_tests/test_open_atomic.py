from __future__ import annotations

import pathlib

import pytest

import portalocker


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
