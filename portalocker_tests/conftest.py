import contextlib
import importlib.util
import logging
import multiprocessing
import os
import random

import pytest

import portalocker
from portalocker import utils

logger = logging.getLogger(__name__)

# `win32file` is only importable when the `pywin32` package (the `win32`
# extra) is installed. Since 4.0.0 that package is optional, so CI runs
# Windows cells both with and without it (msvcrt-only). Guard every
# Windows-specific locker below on its availability so collection succeeds
# either way.
_HAS_PYWIN32: bool = importlib.util.find_spec('win32file') is not None


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
    from fcntl import flock, lockf

    LOCKERS += [flock, lockf]
elif _HAS_PYWIN32:
    # `MsvcrtLocker.__init__` unconditionally constructs a `Win32Locker`
    # (for its shared-lock delegation and unlock() fallback), and
    # `Win32Locker.__init__` unconditionally imports `pywintypes`. That
    # means *both* the pure win32 entries and the msvcrt hybrid entries
    # below require pywin32 just to construct, not only when the unlock
    # fallback path is actually exercised. So the whole block is gated,
    # not only the win32-only entries.
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
