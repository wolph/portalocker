__package_name__ = 'portalocker'
__author__ = 'Rick van Hattem'
__email__ = 'wolph@wol.ph'
__description__ = """Wraps the portalocker recipe for easy usage"""
__url__ = 'https://github.com/WoLpH/portalocker'

from pathlib import Path
from typing import Optional

try:  # Python 3.8+
    import importlib.metadata as importlib_metadata
except Exception:  # pragma: no cover
    import importlib_metadata  # type: ignore[no-redef]


def _read_pyproject_version(path: Path) -> Optional[str]:  # pragma: no cover
    """Read the version from a pyproject.toml file if available.

    This is a lightweight helper used when the package is executed from a
    source checkout where distribution metadata is unavailable.

    Args:
        path: Path to the pyproject.toml file.

    Returns:
        The version string if it could be determined, otherwise None.
    """
    try:
        try:
            import tomllib  # type: ignore

            data = tomllib.loads(path.read_text(encoding='utf-8'))
            project = data.get('project', {})
            version = project.get('version')
            if isinstance(version, str):
                return version
        except Exception:
            import re

            content = path.read_text(encoding='utf-8')
            match = re.search(
                r"(?ms)\[project\].*?^version\s*=\s*['\"]([^'\"]+)['\"]",
                content,
            )
            if match:
                return match.group(1)
    except Exception:
        return None
    return None


def get_version() -> str:  # pragma: no cover
    """Return the package version at runtime.

    Prefers installed package metadata. When running from a source tree it
    falls back to parsing the pyproject.toml [project].version field.

    Returns:
        The resolved version string. Returns '0.0.0' as a last resort.
    """
    try:
        return importlib_metadata.version(__package_name__)
    except Exception:
        pass

    root = Path(__file__).resolve().parent.parent
    version = _read_pyproject_version(root / 'pyproject.toml')
    return version or '0.0.0'


__version__ = get_version()
