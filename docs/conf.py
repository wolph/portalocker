"""Sphinx configuration."""

from __future__ import annotations

import datetime
from importlib import metadata

project = 'portalocker'
author = 'Rick van Hattem'
copyright = (
    f'2001-{datetime.datetime.now(tz=datetime.timezone.utc):%Y}, {author}'
)
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
