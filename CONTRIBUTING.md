# Contributing to Portalocker

Install the development tools and run the checks from a checkout:

```console
uv sync --group dev --python 3.13
uv run pytest
uv run tox -m check
uv run tox -e docs,distribution
```

The test suite runs file locks against the local platform and Redis locks
against fakeredis. It also tests a live Redis server when one is available.
Tests require 100% branch coverage for code reachable on the current platform.
Ruff checks formatting and lint rules. Mypy, basedpyright, pyrefly and ty check
the package and tests. Tool versions remain unpinned so new releases are tested.

To require live Redis coverage, start a disposable Redis server and run:

```console
REDIS_HOST=localhost REDIS_PORT=6379 uv run tox -e redis-live
```

Use a dedicated test server. The tests simulate connection loss and kill lock
subscriptions. The command fails if that server is unavailable.

Run `uv run tox` for the available Python and PyPy environments. Missing local
interpreters are skipped. CI checks Python 3.10 through 3.14 on Linux, macOS and
Windows, including Windows with and without the optional `pywin32` dependency.

## Documentation

The README is Markdown. The guides and API reference use Sphinx with RST and
the Furo theme. Run the documentation build after editing them:

```console
uv run tox -e docs
uv run python -m http.server --directory docs/_build/html 8000
```

Open `http://localhost:8000`. Check changed pages in light and dark themes at
desktop, tablet and mobile widths. Check full pages, component details,
keyboard focus, links and the browser console.

Python fences in the README are executed directly by the test suite in
temporary directories. RST doctests run alongside the package tests. Keep
examples runnable, use British spelling and ASCII punctuation, and use absolute
image URLs in the README so images also work on PyPI.

## Preparing a release

Update the version and changelog together. `uv run tox -e distribution` builds
the wheel and sdist, checks the PyPI description and installs both archives
in isolated environments. The publish workflow repeats distribution validation
before uploading anything to PyPI.

A complete release includes the version and changelog, a pushed tag, the PyPI
publication, a GitHub release and `master` fast-forwarded to that tag. Verify
all five. A green build alone is not a published release.
