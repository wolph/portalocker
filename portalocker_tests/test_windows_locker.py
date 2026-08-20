"""Windows-only locker regressions for ``portalocker.portalocker``.

Covers two Windows-specific bugs:

* ``Win32Locker.lock`` must translate *every* ``pywintypes.error`` into a
  ``LockException`` (not just ``ERROR_LOCK_VIOLATION``); other Win32 errors
  used to escape as the raw pywintypes exception.
* ``_prepare_windows_file`` must seek raw descriptors (int / HasFileno) to
  byte 0 before locking, otherwise ``msvcrt.locking`` locks a range relative
  to the current position and two "exclusive" locks on files larger than the
  lock length do not conflict.

The lockers only exist on Windows (inside the ``if os.name == 'nt'`` block of
``portalocker.portalocker``), so the whole module skips at import time on
posix, matching ``conftest.py`` / ``test_msvcrt_no_pywin32.py``.
"""

import importlib.util
import os

import pytest

import portalocker
from portalocker import LockFlags

# `win32file` is importable only when pywin32 (the `win32` extra) is
# installed. CI runs Windows cells both with and without it.
_HAS_PYWIN32: bool = importlib.util.find_spec('win32file') is not None


@pytest.fixture
def tmpfile(tmp_path):
    return str(tmp_path / 'windows_locker.lock')


if os.name == 'nt':
    from portalocker.portalocker import Win32Locker

    # --- B3: non-LOCK_VIOLATION errors become LockException --------------

    @pytest.mark.skipif(not _HAS_PYWIN32, reason='requires pywin32 installed')
    def test_win32_lock_wraps_non_lock_violation(monkeypatch, tmpfile):
        import pywintypes
        import win32file

        locker = Win32Locker()

        # ERROR_ACCESS_DENIED (5) != ERROR_LOCK_VIOLATION (33).
        denied = pywintypes.error(5, 'LockFileEx', 'Access is denied.')

        def boom(*args: object, **kwargs: object) -> None:
            raise denied

        monkeypatch.setattr(win32file, 'LockFileEx', boom)

        with (
            open(tmpfile, 'w') as fh,  # noqa: PTH123
            pytest.raises(portalocker.LockException) as exc_info,
        ):
            locker.lock(fh, LockFlags.EXCLUSIVE)

        # It must be a plain LockException, not the AlreadyLocked subtype
        # (which is reserved for genuine lock contention), and the original
        # pywintypes.error must be chained as the cause.
        assert not isinstance(exc_info.value, portalocker.AlreadyLocked)
        assert exc_info.value.__cause__ is denied

    @pytest.mark.skipif(not _HAS_PYWIN32, reason='requires pywin32 installed')
    def test_win32_lock_wraps_oserror_from_stale_fd(monkeypatch, tmpfile):
        """An ``OSError`` from ``msvcrt.get_osfhandle`` (stale fd) must
        surface as ``LockException``, matching the unlock path, instead of
        escaping ``lock()`` raw.
        """
        import msvcrt

        locker = Win32Locker()
        stale = OSError(9, 'The handle is invalid')

        def boom(fd: int) -> int:
            raise stale

        monkeypatch.setattr(msvcrt, 'get_osfhandle', boom)

        with (
            open(tmpfile, 'w') as fh,  # noqa: PTH123
            pytest.raises(portalocker.LockException) as exc_info,
        ):
            locker.lock(fh, LockFlags.EXCLUSIVE)

        assert exc_info.value.__cause__ is stale

    @pytest.mark.skipif(not _HAS_PYWIN32, reason='requires pywin32 installed')
    def test_win32_overlapped_created_per_call(monkeypatch, tmpfile):
        """Every ``LockFileEx``/``UnlockFileEx`` call must get its own
        ``OVERLAPPED``: the Win32 API forbids sharing one instance between
        calls, which the old cached ``self._overlapped`` did across threads.
        """
        import win32file

        locker = Win32Locker()
        overlappeds: list[object] = []

        def lock_spy(*args: object, **kwargs: object) -> None:
            overlappeds.append(args[4])

        def unlock_spy(*args: object, **kwargs: object) -> None:
            overlappeds.append(args[3])

        monkeypatch.setattr(win32file, 'LockFileEx', lock_spy)
        monkeypatch.setattr(win32file, 'UnlockFileEx', unlock_spy)

        with open(tmpfile, 'w') as fh:  # noqa: PTH123
            locker.lock(fh, LockFlags.EXCLUSIVE)
            locker.lock(fh, LockFlags.EXCLUSIVE)
            locker.unlock(fh)

        assert len(overlappeds) == 3
        assert len(set(map(id, overlappeds))) == 3, (
            'OVERLAPPED instances must not be shared between calls'
        )
        # And no instance-cached OVERLAPPED exists to share in the first
        # place.
        assert not hasattr(locker, '_overlapped')

    # --- LK_* fallbacks live on the instance, not on the module ---------

    def test_msvcrt_locker_does_not_mutate_msvcrt(monkeypatch):
        """Constructing ``MsvcrtLocker`` must not setattr fallback LK_*
        constants onto the shared stdlib ``msvcrt`` module.
        """
        import msvcrt

        from portalocker.portalocker import MsvcrtLocker

        monkeypatch.delattr(msvcrt, 'LK_NBRLCK', raising=False)

        locker = MsvcrtLocker()

        # The instance resolved the documented fallback for the missing
        # constant without writing it back to the module.
        assert locker._lock_modes['LK_NBRLCK'] == 4
        assert not hasattr(msvcrt, 'LK_NBRLCK')
        # The constants the module does expose are used as-is. (The
        # attr-defined ignores match the ones in portalocker.portalocker:
        # posix typeshed has no msvcrt attributes.)
        assert locker._lock_modes['LK_LOCK'] == msvcrt.LK_LOCK  # type: ignore[attr-defined]
        assert locker._lock_modes['LK_NBLCK'] == msvcrt.LK_NBLCK  # type: ignore[attr-defined]
        assert locker._lock_modes['LK_UNLCK'] == msvcrt.LK_UNLCK  # type: ignore[attr-defined]

    # --- B4: raw descriptors lock from byte 0 ---------------------------

    def test_int_fd_locks_from_byte_zero(tmpfile):
        # File larger than the msvcrt lock length (0x10000) so that a lock
        # taken away from byte 0 would not overlap one taken at byte 0.
        payload = b'\0' * (128 * 1024)
        # ``os.O_BINARY`` is Windows-only; ``getattr`` keeps the module
        # importable/type-checkable off-Windows where this test is skipped.
        o_binary: int = getattr(os, 'O_BINARY', 0)
        fd = os.open(tmpfile, os.O_RDWR | os.O_CREAT | o_binary)
        try:
            os.write(fd, payload)
            # Move well away from the start before locking with the int fd.
            os.lseek(fd, 100 * 1024, os.SEEK_SET)
            portalocker.lock(fd, LockFlags.EXCLUSIVE | LockFlags.NON_BLOCKING)

            # A second exclusive lock from the same process via a file
            # object must conflict, because both now target [0, 0x10000).
            with (
                open(tmpfile, 'r+b') as fh,  # noqa: PTH123
                pytest.raises(portalocker.AlreadyLocked),
            ):
                portalocker.lock(
                    fh, LockFlags.EXCLUSIVE | LockFlags.NON_BLOCKING
                )

            portalocker.unlock(fd)
        finally:
            os.close(fd)

else:
    pytest.skip(
        'Windows lockers are only defined on nt',
        allow_module_level=True,
    )
