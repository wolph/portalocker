from __future__ import annotations

import pathlib
import typing
from typing import Union

Mode = typing.Literal[
    # Text modes
    'r',
    'rt',
    'tr',  # Read text
    'w',
    'wt',
    'tw',  # Write text
    'a',
    'at',
    'ta',  # Append text
    'x',
    'xt',
    'tx',  # Exclusive creation text
    'r+',
    '+r',
    'rt+',
    'r+t',
    '+rt',
    'tr+',
    't+r',
    '+tr',  # Read and write text
    'w+',
    '+w',
    'wt+',
    'w+t',
    '+wt',
    'tw+',
    't+w',
    '+tw',  # Write and read text
    'a+',
    '+a',
    'at+',
    'a+t',
    '+at',
    'ta+',
    't+a',
    '+ta',  # Append and read text
    'x+',
    '+x',
    'xt+',
    'x+t',
    '+xt',
    'tx+',
    't+x',
    '+tx',  # Exclusive creation and read text
    'U',
    'rU',
    'Ur',
    'rtU',
    'rUt',
    'Urt',
    'trU',
    'tUr',
    'Utr',  # Universal newline support
    # Binary modes
    'rb',
    'br',  # Read binary
    'wb',
    'bw',  # Write binary
    'ab',
    'ba',  # Append binary
    'xb',
    'bx',  # Exclusive creation binary
    'rb+',
    'r+b',
    '+rb',
    'br+',
    'b+r',
    '+br',  # Read and write binary
    'wb+',
    'w+b',
    '+wb',
    'bw+',
    'b+w',
    '+bw',  # Write and read binary
    'ab+',
    'a+b',
    '+ab',
    'ba+',
    'b+a',
    '+ba',  # Append and read binary
    'xb+',
    'x+b',
    '+xb',
    'bx+',
    'b+x',
    '+bx',  # Exclusive creation and read binary
    'rbU',
    'rUb',
    'Urb',
    'brU',
    'bUr',
    'Ubr',
    # Universal newline support in binary mode
]
Filename = Union[str, pathlib.Path]
IO: typing.TypeAlias = Union[typing.IO[str], typing.IO[bytes]]


class FileOpenKwargs(typing.TypedDict):
    buffering: int | None
    encoding: str | None
    errors: str | None
    newline: str | None
    closefd: bool | None
    opener: typing.Callable[[str, int], int] | None
