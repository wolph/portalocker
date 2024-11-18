import contextlib
import logging
import multiprocessing
import random

import pytest

from portalocker import utils

logger = logging.getLogger(__name__)


@pytest.fixture(scope='function')
def tmpfile(tmp_path):
    filename = tmp_path / str(random.random())[2:]
    yield str(filename)
    with contextlib.suppress(PermissionError):
        filename.unlink(missing_ok=True)


def pytest_sessionstart(session):
    # Force spawning the process so we don't accidentally inherit locks.
    # I'm not a 100% certain this will work correctly unfortunately... there
    # is some potential for breaking tests
    multiprocessing.set_start_method('spawn')


@pytest.fixture(autouse=True)
def reduce_timeouts(monkeypatch):
    "For faster testing we reduce the timeouts."
    monkeypatch.setattr(utils, 'DEFAULT_TIMEOUT', 0.1)
    monkeypatch.setattr(utils, 'DEFAULT_CHECK_INTERVAL', 0.05)
