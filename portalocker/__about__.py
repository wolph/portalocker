"""Version and package metadata helpers for portalocker.

This module resolves the runtime version by preferring installed package
metadata and falling back to parsing the local ``pyproject.toml`` when
needed.
"""

import os
import re
import stat
from importlib import metadata as importlib_metadata
from pathlib import Path

__package_name__ = 'portalocker'
__author__ = 'Rick van Hattem'
__email__ = 'wolph@wol.ph'
__description__ = 'Wraps the portalocker recipe for easy usage'
__url__ = 'https://github.com/WoLpH/portalocker'

#: Bytes of ``pyproject.toml`` the fallback is prepared to read. The
#: ``[project]`` table of this package sits in the first few kilobytes
#: of its own file; anything larger than this is not that file.
_PYPROJECT_READ_LIMIT: int = 64 * 1024


def _read_prefix(path: Path, limit: int) -> str | None:
    """Read at most ``limit`` bytes from the regular file at ``path``.

    The probe runs at import time against a directory the package does
    not own (the one above it), so it never reads anything but a regular
    file, never blocks on a FIFO, and never reads more than ``limit``
    bytes. A symlinked file is followed, as it was before the bound
    existed, since some checkout layouts link their ``pyproject.toml``.
    A file that is not UTF-8 reads as nothing, as before.

    Args:
        path: The file to read.
        limit: The number of bytes to read at most.

    Returns:
        The decoded prefix, or ``None`` when the file could not be read
        or is not a regular file.
    """
    flags: int = (
        os.O_RDONLY
        | getattr(os, 'O_NONBLOCK', 0)
        | getattr(os, 'O_CLOEXEC', 0)
        | getattr(os, 'O_BINARY', 0)
    )
    try:
        fd: int = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, limit).decode('utf-8')
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        os.close(fd)


def _unquote(value: str) -> str | None:
    """Return the content of a basic or literal single-line TOML string.

    A trailing comment after the closing quote is allowed, a
    triple-quoted string, an unterminated one or anything but a
    comment after the closing quote is not a version.
    """
    value = value.strip()
    if len(value) < 2 or value[0] not in '\'"':
        return None
    quote: str = value[0]
    if value.startswith(quote * 3):
        return None
    closing: int = value.find(quote, 1)
    if closing == -1:
        return None
    rest: str = value[closing + 1 :].strip()
    if rest and not rest.startswith('#'):
        return None
    inner: str = value[1:closing]
    return inner or None


def _normalise_name(name: str | None) -> str | None:
    """Normalise a project name the way PEP 503 compares them."""
    if name is None:
        return None
    return re.sub(r'[-_.]+', '-', name).lower()


def _read_pyproject_version(path: Path) -> str | None:
    """Read the version from a ``pyproject.toml`` file if available.

    A single linear pass over a bounded prefix of the file: only a
    ``version`` key inside the ``[project]`` table counts, and only
    when that table's ``name`` is this package. A ``pyproject.toml`` of
    some other project above a vendored copy therefore never supplies
    the version, and a huge planted file cannot stall the import. It is
    intentionally minimal to avoid runtime dependencies while keeping
    types precise.

    Args:
        path: Path to the ``pyproject.toml`` file.

    Returns:
        The version string if it could be determined, otherwise ``None``.
    """
    content: str | None = _read_prefix(path, _PYPROJECT_READ_LIMIT)
    if content is None:
        return None

    in_project: bool = False
    name: str | None = None
    version: str | None = None
    for raw_line in content.splitlines():
        line: str = raw_line.strip()
        if line.startswith('['):
            if in_project:
                break
            in_project = line.rstrip() == '[project]'
            continue
        if not in_project or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        if key == 'name':
            name = _unquote(value)
        elif key == 'version':
            version = _unquote(value)
    if _normalise_name(name) != _normalise_name(__package_name__):
        return None
    return version


def get_version() -> str:
    """Return the package version at runtime.

    Prefers installed package metadata. When running from a source tree it
    falls back to parsing the ``pyproject.toml`` ``[project].version``
    field.

    Returns:
        The resolved version string. Returns ``'0.0.0'`` as a last resort.
    """
    try:
        return importlib_metadata.version(__package_name__)
    except Exception:
        pass

    root = Path(__file__).resolve().parent.parent
    version = _read_pyproject_version(root / 'pyproject.toml')
    return version or '0.0.0'


__version__ = get_version()
