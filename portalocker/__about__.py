"""Version and package metadata helpers for portalocker.

This module resolves the runtime version by preferring installed package
metadata and falling back to parsing the local ``pyproject.toml`` when
needed.
"""

import re
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Optional

__package_name__ = 'portalocker'
__author__ = 'Rick van Hattem'
__email__ = 'wolph@wol.ph'
__description__ = 'Wraps the portalocker recipe for easy usage'
__url__ = 'https://github.com/WoLpH/portalocker'


def _read_pyproject_version(path: Path) -> Optional[str]:  # pragma: no cover
    """Read the version from a ``pyproject.toml`` file if available.

    This uses a small regex parser that looks for the ``[project]`` table
    and extracts the ``version`` value. It's intentionally minimal to avoid
    runtime dependencies while keeping types precise.

    Args:
        path: Path to the ``pyproject.toml`` file.

    Returns:
        The version string if it could be determined, otherwise ``None``.
    """
    try:
        content = path.read_text(encoding='utf-8')
    except Exception:
        return None

    match = re.search(
        r"(?ms)^\[project\].*?^version\s*=\s*['\"]([^'\"]+)['\"]",
        content,
    )
    return match.group(1) if match else None


def get_version() -> str:  # pragma: no cover
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
