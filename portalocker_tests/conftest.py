import contextlib
import logging
import multiprocessing
import os
import random

import pytest

import portalocker
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


LOCKERS: list[portalocker.portalocker.LockerType] = []
# ------------------------------------------------------------------ #
#  Locker switching helpers (used by many parametrised tests)
# ------------------------------------------------------------------ #
if os.name == 'posix':
    from fcntl import flock, lockf  # type: ignore[attr-defined]

    LOCKERS += [flock, lockf]  # type: ignore[list-item]
else:
    win_locker = portalocker.portalocker.Win32Locker()
    msvcrt_locker = portalocker.portalocker.MsvcrtLocker()

    LOCKERS += [
        (
            win_locker.lock,
            win_locker.unlock,
        ),
        (
            msvcrt_locker.lock,
            msvcrt_locker.unlock,
        ),
        portalocker.portalocker.Win32Locker,
        portalocker.portalocker.MsvcrtLocker,
        win_locker,
        msvcrt_locker,
    ]


@pytest.fixture
def locker(request, monkeypatch):
    """Patch the low-level locker that portalocker uses for this test run."""
    monkeypatch.setattr(portalocker.portalocker, 'LOCKER', request.param)
    return request.param


__all__ = ['LOCKERS']
