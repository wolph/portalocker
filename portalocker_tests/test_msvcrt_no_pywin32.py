"""``MsvcrtLocker`` must work without the optional ``pywin32`` dependency.

Since portalocker 4.0.0 the msvcrt locker is the Windows default and
``pywin32`` is an optional (``portalocker[win32]``) extra. These tests pin
the pywin32-free behaviour: construction and exclusive locks work, while
shared locks raise an informative error pointing at the extra.

The locker classes only exist on Windows (they live inside the
``if os.name == 'nt'`` block), so the whole module skips at import time on
posix. Every nt-only name is referenced inside an ``os.name == 'nt'``
branch, matching ``conftest.py``: the strict type checkers that narrow
``os.name`` treat that branch as dead on posix and skip it, while the
module body never executes off-Windows, keeping the coverage gate honest.
"""

import importlib.util
import os
import sys

import pytest

# `win32file` is importable only when pywin32 (the `win32` extra) is
# installed. CI runs Windows cells both with and without it.
_HAS_PYWIN32: bool = importlib.util.find_spec('win32file') is not None

# Submodules the Windows lockers import lazily from pywin32.
_PYWIN32_MODULES: tuple[str, ...] = (
    'pywintypes',
    'win32file',
    'win32con',
    'winerror',
)


@pytest.fixture
def no_pywin32(monkeypatch):
    """Simulate an absent pywin32, even when it is installed.

    A ``None`` entry in ``sys.modules`` makes ``import <name>`` raise
    ``ImportError``, mirroring a pywin32-less Windows install.
    """
    for name in _PYWIN32_MODULES:
        monkeypatch.setitem(sys.modules, name, None)


if os.name == 'nt':
    from portalocker import LockFlags
    from portalocker.portalocker import MsvcrtLocker, Win32Locker

    def test_construction_without_pywin32(no_pywin32):
        locker = MsvcrtLocker()
        assert locker._win32_locker is None

    def test_exclusive_roundtrip_without_pywin32(no_pywin32, tmpfile):
        locker = MsvcrtLocker()
        with open(tmpfile, 'w') as fh:
            locker.lock(fh, LockFlags.EXCLUSIVE)
            locker.unlock(fh)

    def test_shared_without_pywin32_raises(no_pywin32, tmpfile):
        locker = MsvcrtLocker()
        with (
            open(tmpfile, 'w') as fh,
            pytest.raises(ImportError, match=r'portalocker\[win32\]'),
        ):
            locker.lock(fh, LockFlags.SHARED)

    @pytest.mark.skipif(not _HAS_PYWIN32, reason='requires pywin32 installed')
    def test_construction_with_pywin32_uses_win32_locker():
        locker = MsvcrtLocker()
        assert isinstance(locker._win32_locker, Win32Locker)

else:
    pytest.skip(
        'MsvcrtLocker is only defined on Windows (nt)',
        allow_module_level=True,
    )
