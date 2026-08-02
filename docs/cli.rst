Command-Line Interface
=======================

``portalocker`` ships a small command-line interface, invoked as
``python -m portalocker``. Its only subcommand, ``combine``, bundles the
package into a single vendorable ``portalocker.py`` file for projects
that cannot add ``portalocker`` as a dependency -- a vendoring or
single-file-deployment constraint, for example -- and instead copy one
generated file with the same public API into their own tree.

Running combine
----------------

.. code-block:: console

    $ python -m portalocker combine

This writes the combined file to ``dist/portalocker.py`` by default.
Pick a different destination with ``--output-file``/``-o``:

.. code-block:: console

    $ python -m portalocker combine --output-file vendor/portalocker.py
    $ python -m portalocker combine -o vendor/portalocker.py

If the ``ruff`` command is available, the output is formatted and
lint-fixed in place; if not, that step is skipped with a warning. Either
way, the generated file is then executed once as a smoke test, so a
broken combine surfaces immediately rather than only when a downstream
project imports the file.

What the transform does
-------------------------

Starting from ``portalocker/__init__.py``, ``combine`` walks the
package's modules and inlines every relative import it finds --
``from .foo import bar`` and its parenthesised multi-line form -- with
the source of the referenced module, recursively, so the result has no
relative imports left. A module referenced from more than one place is
only inlined once.

``if TYPE_CHECKING:`` blocks are the one exception: they are copied
verbatim rather than inlined, preserving the runtime guard their
else-branch relies on. That includes the optional
``try: from .redis import RedisLock`` guard in ``__init__.py``:
``redis.py`` is inlined like any other sibling module, re-indented to
stay inside the ``try``, so the combined file binds ``RedisLock`` the
same way the package does -- to ``None`` if importing ``redis`` fails.

Qualified references left behind by inlining (``constants.LockFlags``
becoming just ``LockFlags``, for instance) are cleaned up, and useless
self-assignments the cleanup can produce (``spam = spam``) are stripped
entirely.

``README.rst`` and ``LICENSE`` are also copied near the top of the
output, each wrapped in a triple-quoted string, for reference.

Limitations
------------

The combined file still needs the ``redis`` package installed for
``RedisLock`` to work: combining inlines the code that binds to
``redis``, not ``redis`` itself.

Every file ``combine`` reads -- the package's own modules, plus
``README.rst`` and ``LICENSE`` -- is opened with ``encoding='ascii'``.
That means the entire ``portalocker`` source tree, along with
``README.rst`` and ``LICENSE``, must stay ASCII-only: an em dash, curly
quote, box-drawing character, or any other non-ASCII byte anywhere in
those files makes ``combine`` raise ``UnicodeDecodeError`` instead of
silently mis-encoding the result. ``portalocker_tests/test_combined.py``
asserts the combined output can still be produced, which is how a stray
non-ASCII character elsewhere in the package gets caught by the test
suite. Keep contributions to those files ASCII-only for this reason.
