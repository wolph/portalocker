:tocdepth: 2

.. raw:: html

    <nav class="portalocker-links" aria-label="Project links">
      <a href="https://pypi.org/project/portalocker/">PyPI</a>
      <a href="https://github.com/wolph/portalocker">Source code</a>
      <a href="changelog.html">Changelog</a>
    </nav>
    <p class="portalocker-eyebrow">Cross-platform Python locking</p>

Coordinate access. Keep your code simple.
=========================================

.. container:: portalocker-hero

    File locks for Python on Linux, macOS and Windows. Use a context manager
    to coordinate access between processes, or Redis locks across machines.

    .. raw:: html

        <div class="portalocker-actions">
          <a class="portalocker-button" href="quickstart.html">Get started <span aria-hidden="true">&rarr;</span></a>
          <a class="portalocker-button portalocker-button-secondary" href="lock-types.html">Choose a lock</a>
        </div>

Start with a file lock
------------------------

Install portalocker on Python 3.10 or later:

.. code-block:: console

    python -m pip install portalocker

Use the file handle inside the context:

.. code-block:: python

    import portalocker

    with portalocker.Lock('report.txt', 'a', timeout=5) as fh:
        fh.write('Report complete.\n')

``Lock`` waits up to five seconds to acquire the file lock. Leaving the
context releases the lock and closes the file, including when the body raises
an exception. Give every participating process the same file path.

Choose the lock for your task
--------------------------------

.. raw:: html

    <div class="portalocker-cards">
      <a class="portalocker-card" href="quickstart.html">
        <h3>File locks</h3>
        <p>Coordinate readers and writers on a shared filesystem.</p>
        <span>File locking guide <span aria-hidden="true">&rarr;</span></span>
      </a>
      <a class="portalocker-card" href="redis.html">
        <h3>Redis locks</h3>
        <p>Coordinate processes across machines using Redis pub/sub.</p>
        <span>Redis guide <span aria-hidden="true">&rarr;</span></span>
      </a>
      <a class="portalocker-card" href="lock-types.html">
        <h3>Workers and slots</h3>
        <p>Run one worker with a PID file, or limit concurrency with a semaphore.</p>
        <span>Lock types <span aria-hidden="true">&rarr;</span></span>
      </a>
    </div>

.. note::

    Unix file locks are advisory, so every participating process must lock.
    Shared file locks on Windows require the optional ``win32`` extra.
    Check :doc:`platforms` for filesystem behaviour and network storage caveats.

.. raw:: html

    <nav class="portalocker-footer-links" aria-label="More documentation">
      <a href="api/index.html">API reference</a>
      <a href="migration.html">Migration guide</a>
      <a href="troubleshooting.html">Troubleshooting</a>
      <a href="https://github.com/wolph/portalocker/issues">Report an issue</a>
    </nav>

.. toctree::
    :hidden:
    :caption: Get started

    quickstart
    lock-types

.. toctree::
    :hidden:
    :caption: Guides

    platforms
    redis
    cli
    troubleshooting

.. toctree::
    :hidden:
    :caption: Reference

    api/index
    migration
    changelog
    license
