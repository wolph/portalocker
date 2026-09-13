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
html_title: str = 'Portalocker'
exclude_patterns: list[str] = ['_build', 'superpowers']
html_static_path: list[str] = ['_static']
html_favicon: str = '_static/file-lock.svg'
html_css_files: list[str] = ['portalocker.css']
html_theme_options: dict[str, object] = {
    'light_logo': 'portalocker-light.svg',
    'dark_logo': 'portalocker-dark.svg',
    'sidebar_hide_name': True,
    'light_css_variables': {
        'color-brand-primary': '#087869',
        'color-brand-content': '#087869',
    },
    'dark_css_variables': {
        'color-brand-primary': '#70ddbf',
        'color-brand-content': '#70ddbf',
    },
}

intersphinx_mapping = {
    'python': ('https://docs.python.org/3', None),
    'redis': ('https://redis.readthedocs.io/en/stable/', None),
}

autodoc_typehints = 'description'

# The docs environment only installs the `docs` extra (sphinx + furo), not
# `redis`, so the optional `redis` dependency imported by portalocker.redis
# is unavailable here. Mock it so autodoc can still document the module.
autodoc_mock_imports = ['redis']
