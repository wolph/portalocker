"""Write semantics of ``Lock`` modes containing ``w``.

``Lock`` rewrites mode ``w`` to ``a`` so that opening cannot truncate a
file another holder is still using. On POSIX the kernel append flag is
cleared again once the lock is taken and the file truncated, so the
handle honours seek positions exactly like a plain ``open(mode='w')``
would. On Windows the handle keeps append semantics; that platform
difference is documented rather than patched over.
"""

import os
import pathlib

import pytest

import portalocker

posix_only = pytest.mark.skipif(
    os.name != 'posix',
    reason='clearing the kernel append flag is POSIX only',
)


@posix_only
def test_write_mode_positioned_writes(tmpfile: str) -> None:
    """Mode ``w`` honours seek positions like a plain ``open(mode='w')``."""
    with portalocker.Lock(tmpfile, 'w', timeout=0) as fh:
        fh.write('x')
        fh.seek(0)
        fh.write('y')

    assert pathlib.Path(tmpfile).read_text() == 'y'


@posix_only
def test_write_plus_mode_positioned_writes(tmpfile: str) -> None:
    """Mode ``w+`` truncates, then reads and writes at the seek position."""
    pathlib.Path(tmpfile).write_text('stale data from a previous run')

    with portalocker.Lock(tmpfile, 'w+', timeout=0) as fh:
        assert fh.read() == ''
        fh.write('spam')
        fh.seek(0)
        fh.write('eggs')

    assert pathlib.Path(tmpfile).read_text() == 'eggs'


@posix_only
def test_binary_write_mode_positioned_writes(tmpfile: str) -> None:
    """Mode ``wb`` honours seek positions as well."""
    with portalocker.Lock(tmpfile, 'wb', timeout=0) as fh:
        fh.write(b'x')
        fh.seek(0)
        fh.write(b'y')

    assert pathlib.Path(tmpfile).read_bytes() == b'y'


def test_append_mode_keeps_appending(tmpfile: str) -> None:
    """A genuine ``a`` mode keeps kernel append semantics untouched."""
    pathlib.Path(tmpfile).write_text('abc')

    with portalocker.Lock(tmpfile, 'a', timeout=0) as fh:
        fh.write('x')
        fh.seek(0)
        fh.write('y')

    assert pathlib.Path(tmpfile).read_text() == 'abcxy'
