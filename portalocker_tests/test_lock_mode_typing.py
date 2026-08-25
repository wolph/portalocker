"""Mode-based typing of the file locks (issue #97).

`Lock` and its subclasses hand the locked filehandle to the caller, and
the static type of that filehandle should follow the open mode: a text
mode yields ``IO[str]``, a binary mode yields ``IO[bytes]``. These tests
pin the contract twice over. The `assert_type` calls fail the type
checkers when the inference regresses, and the isinstance checks fail
the test run if the runtime behaviour ever drifts from the static story.
"""

import pathlib
import typing

from typing_extensions import assert_type

import portalocker
from portalocker import utils


def load_binary_file(path: pathlib.Path) -> bytes:
    """The exact usage from issue #97 that used to be rejected."""
    with portalocker.Lock(path, mode='rb', timeout=0.1) as fh:
        return fh.read()


def read_default_lock(lock: portalocker.Lock) -> str:
    """A bare `Lock` annotation defaults to the text filehandle."""
    fh: typing.IO[str] = lock.acquire()
    fh.seek(0)
    return fh.read()


def test_text_mode_yields_str_filehandle(tmp_path: pathlib.Path) -> None:
    path: pathlib.Path = tmp_path / 'text.lock'
    with portalocker.Lock(path, mode='a+', timeout=0.1) as fh:
        assert_type(fh, typing.IO[str])
        written: int = fh.write('spam and eggs')
        assert written > 0
        fh.seek(0)
        data = fh.read()
        assert_type(data, str)
        assert isinstance(data, str)
        assert data == 'spam and eggs'


def test_binary_mode_yields_bytes_filehandle(tmp_path: pathlib.Path) -> None:
    path: pathlib.Path = tmp_path / 'binary.lock'
    with portalocker.Lock(path, mode='ab+', timeout=0.1) as fh:
        assert_type(fh, typing.IO[bytes])
        written: int = fh.write(b'spam and eggs')
        assert written > 0
        fh.seek(0)
        data = fh.read()
        assert_type(data, bytes)
        assert isinstance(data, bytes)
        assert data == b'spam and eggs'


def test_default_mode_yields_str_filehandle(tmp_path: pathlib.Path) -> None:
    path: pathlib.Path = tmp_path / 'default.lock'
    with portalocker.Lock(path, timeout=0.1) as fh:
        assert_type(fh, typing.IO[str])
        written: int = fh.write('spam')
        assert written > 0


def test_acquire_returns_bytes_filehandle_for_binary_mode(
    tmp_path: pathlib.Path,
) -> None:
    path: pathlib.Path = tmp_path / 'acquire.lock'
    path.write_bytes(b'spam and eggs')
    lock = portalocker.Lock(path, mode='rb', timeout=0.1)
    try:
        fh = lock.acquire()
        assert_type(fh, typing.IO[bytes])
        data = fh.read()
        assert_type(data, bytes)
        assert data == b'spam and eggs'
    finally:
        lock.release()


def test_issue_97_binary_read_typechecks(tmp_path: pathlib.Path) -> None:
    path: pathlib.Path = tmp_path / 'issue97.bin'
    path.write_bytes(b'spam')
    assert load_binary_file(path) == b'spam'


def test_bare_lock_annotation_defaults_to_text(
    tmp_path: pathlib.Path,
) -> None:
    path: pathlib.Path = tmp_path / 'bare.lock'
    path.write_text('spam and eggs')
    lock = portalocker.Lock(path, mode='r+', timeout=0.1)
    try:
        assert read_default_lock(lock) == 'spam and eggs'
    finally:
        lock.release()


def test_rlock_modes_follow_the_same_contract(
    tmp_path: pathlib.Path,
) -> None:
    text_path: pathlib.Path = tmp_path / 'rlock-text.lock'
    with portalocker.RLock(text_path, mode='a+', timeout=0.1) as fh:
        assert_type(fh, typing.IO[str])
        assert fh.write('spam') > 0

    binary_path: pathlib.Path = tmp_path / 'rlock-binary.lock'
    with portalocker.RLock(binary_path, mode='ab+', timeout=0.1) as fh:
        assert_type(fh, typing.IO[bytes])
        assert fh.write(b'spam') > 0


def test_temporary_file_lock_is_a_text_lock(tmp_path: pathlib.Path) -> None:
    path: pathlib.Path = tmp_path / 'temporary.lock'
    with utils.TemporaryFileLock(path, timeout=0.1) as fh:
        assert_type(fh, typing.IO[str])
        assert fh.write('spam') > 0
