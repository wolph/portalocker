"""Sphinx configuration."""

from __future__ import annotations

import sys
from importlib import metadata
from pathlib import Path

# `portalocker_tests` (autodoc'd by tests.rst) is not part of the installed
# `portalocker` distribution, only the repo checkout, so it needs the repo
# root on `sys.path` to be importable here (RTD builds from a full checkout).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

project = 'portalocker'
author = 'Rick van Hattem'
copyright = f'2001-2026, {author}'
release = metadata.version('portalocker')
version = '.'.join(release.split('.')[:2])

extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.napoleon',
    'sphinx.ext.intersphinx',
    'sphinx.ext.viewcode',
]

napoleon_google_docstring = True
napoleon_numpy_docstring = False

html_theme = 'furo'

intersphinx_mapping = {
    'python': ('https://docs.python.org/3', None),
    'redis': ('https://redis.readthedocs.io/en/stable/', None),
}

autodoc_typehints = 'description'

# The docs environment only installs the `docs` extra (sphinx + furo), not
# `redis`, so the optional `redis` dependency imported by portalocker.redis
# is unavailable here. Mock it so autodoc can still document the module.
autodoc_mock_imports = ['redis']
