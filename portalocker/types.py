# noqa: A005
"""Shared type aliases and protocols used across portalocker's public API.

These are pure typing constructs with no runtime behaviour of their own;
they exist so the locking functions in `portalocker.portalocker` and
`portalocker.utils` can share consistent, precise signatures.
"""

from __future__ import annotations

import io
import pathlib
import typing

# spellchecker: off
# fmt: off
#: Every text mode string accepted by the built-in `open()`, spelled out
#: explicitly so type checkers reject a typo'd mode string instead of
#: letting it fail at runtime. Kept separate from `BinaryMode` so
#: `portalocker.Lock` can infer ``IO[str]`` filehandles from the mode.
#: The legacy universal-newline (`U`) modes are deliberately absent:
#: Python 3.11 removed them, and 3.10 only accepted them with a warning.
TextMode = typing.Literal[
    # Read text
    'r', 'rt', 'tr',
    # Write text
    'w', 'wt', 'tw',
    # Append text
    'a', 'at', 'ta',
    # Exclusive creation text
    'x', 'xt', 'tx',
    # Read and write text
    'r+', '+r', 'rt+', 'r+t', '+rt', 'tr+', 't+r', '+tr',
    # Write and read text
    'w+', '+w', 'wt+', 'w+t', '+wt', 'tw+', 't+w', '+tw',
    # Append and read text
    'a+', '+a', 'at+', 'a+t', '+at', 'ta+', 't+a', '+ta',
    # Exclusive creation and read text
    'x+', '+x', 'xt+', 'x+t', '+xt', 'tx+', 't+x', '+tx',
]
#: Every binary mode string accepted by the built-in `open()`, the
#: counterpart of `TextMode` that makes `portalocker.Lock` infer
#: ``IO[bytes]`` filehandles.
BinaryMode = typing.Literal[
    # Read binary
    'rb', 'br',
    # Write binary
    'wb', 'bw',
    # Append binary
    'ab', 'ba',
    # Exclusive creation binary
    'xb', 'bx',
    # Read and write binary
    'rb+', 'r+b', '+rb', 'br+', 'b+r', '+br',
    # Write and read binary
    'wb+', 'w+b', '+wb', 'bw+', 'b+w', '+bw',
    # Append and read binary
    'ab+', 'a+b', '+ab', 'ba+', 'b+a', '+ba',
    # Exclusive creation and read binary
    'xb+', 'x+b', '+xb', 'bx+', 'b+x', '+bx',
]
#: Every mode string accepted by the built-in `open()`, text and binary
#: combined. Spelled out flat instead of as ``TextMode | BinaryMode`` so
#: ``typing.get_args(Mode)`` keeps returning the mode strings themselves,
#: which the common runtime validation idiom
#: ``mode in typing.get_args(Mode)`` depends on; on a union of Literals
#: `typing.get_args` returns the two Literal aliases instead of their
#: members. The test suite pins this literal to be exactly the union of
#: `TextMode` and `BinaryMode`, statically and at runtime.
Mode = typing.Literal[
    # Text modes
    # Read text
    'r', 'rt', 'tr',
    # Write text
    'w', 'wt', 'tw',
    # Append text
    'a', 'at', 'ta',
    # Exclusive creation text
    'x', 'xt', 'tx',
    # Read and write text
    'r+', '+r', 'rt+', 'r+t', '+rt', 'tr+', 't+r', '+tr',
    # Write and read text
    'w+', '+w', 'wt+', 'w+t', '+wt', 'tw+', 't+w', '+tw',
    # Append and read text
    'a+', '+a', 'at+', 'a+t', '+at', 'ta+', 't+a', '+ta',
    # Exclusive creation and read text
    'x+', '+x', 'xt+', 'x+t', '+xt', 'tx+', 't+x', '+tx',

    # Binary modes
    # Read binary
    'rb', 'br',
    # Write binary
    'wb', 'bw',
    # Append binary
    'ab', 'ba',
    # Exclusive creation binary
    'xb', 'bx',
    # Read and write binary
    'rb+', 'r+b', '+rb', 'br+', 'b+r', '+br',
    # Write and read binary
    'wb+', 'w+b', '+wb', 'bw+', 'b+w', '+bw',
    # Append and read binary
    'ab+', 'a+b', '+ab', 'ba+', 'b+a', '+ba',
    # Exclusive creation and read binary
    'xb+', 'x+b', '+xb', 'bx+', 'b+x', '+bx',
]
# spellchecker: on
#: A filename argument: either a plain string path or a `pathlib.Path`.
#: Accepting both lets callers pass whichever they already have on hand
#: without converting first.
Filename = str | pathlib.Path
#: A file-like object already opened for reading or writing, in either text
#: or binary mode.
IO = typing.IO[str] | typing.IO[bytes]


class FileOpenKwargs(typing.TypedDict):
    """Keyword arguments accepted by the built-in `open()`.

    Mirrors `open()`'s signature (minus `file` and `mode`) so helpers that
    accept a filename can forward arbitrary open-related keyword arguments
    straight through to the underlying `open()` call.
    """

    # Note: Napoleon reads a leading ``something: rest`` on the first line
    # of an attribute docstring as a type declaration, so first lines here
    # deliberately avoid colons.
    buffering: int | None
    """Buffering policy. `0` disables buffering (binary mode only), `1`
    selects line buffering (text mode), and any larger integer fixes the
    buffer size in bytes.
    """

    encoding: str | None
    """Text encoding to use; ignored in binary mode."""

    errors: str | None
    """How encoding/decoding errors are handled, e.g. `'strict'` or
    `'ignore'`.
    """

    newline: str | None
    r"""Controls how universal newlines mode works, e.g. `''`, `'\\n'`,
    `'\\r'`, or `'\\r\\n'`.
    """

    closefd: bool | None
    """Whether the underlying file descriptor is closed when the file
    object is closed. Must be `True` (the default) when a filename rather
    than a file descriptor was passed to `open()`.
    """

    opener: typing.Callable[[str, int], int] | None
    """A custom opener, called as `opener(file, flags)` to obtain the
    underlying file descriptor, used instead of the default `os.open`.
    """


class HasFileno(typing.Protocol):
    """Structural protocol for objects exposing a `fileno()` method.

    Exists so functions that ultimately call `fcntl.flock` can be typed
    against anything with a file descriptor - open files, sockets, and so
    on - without requiring those objects to share a common base class.
    """

    def fileno(self) -> int:
        """Return the underlying file descriptor, as used by `fcntl.flock`."""
        ...


#: The type accepted by the module-level `lock()`/`unlock()` functions: an
#: already-open file object, anything exposing a `fileno()` method, or a
#: bare file descriptor (`int`). The `int` case exists because
#: `fcntl.flock`/`msvcrt.locking()` operate on raw file descriptors, which
#: callers may already have without an open file object wrapping them.
FileArgument = typing.IO[typing.Any] | io.TextIOWrapper | int | HasFileno
