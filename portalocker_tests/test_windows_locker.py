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

    # --- Windows helpers and dispatch, measured since the nt block ------
    # --- lost its blanket "pragma: no cover" ----------------------------

    from portalocker.portalocker import (
        MsvcrtLocker,
        _prepare_windows_file,
    )

    def _open_raw(path: str) -> int:
        """Open ``path`` read-write as a raw binary descriptor."""
        o_binary: int = getattr(os, 'O_BINARY', 0)
        return os.open(path, os.O_RDWR | os.O_CREAT | o_binary)

    class _FilenoOnly:
        """Wrapper exposing only ``fileno()``, the ``HasFileno`` shape."""

        def __init__(self, fd: int) -> None:
            self._fd: int = fd

        def fileno(self) -> int:
            return self._fd

    class _RecordingWin32Stub:
        """Stands in for ``Win32Locker`` so the delegation paths of
        ``MsvcrtLocker`` run without pywin32 installed.
        """

        def __init__(self, unlock_error: Exception | None = None) -> None:
            self.lock_calls: list[LockFlags] = []
            self.unlock_calls: int = 0
            self.unlock_error: Exception | None = unlock_error

        def lock(self, file_obj: object, flags: LockFlags) -> None:
            self.lock_calls.append(flags)

        def unlock(self, file_obj: object) -> None:
            self.unlock_calls += 1
            if self.unlock_error is not None:
                raise self.unlock_error

    def test_prepare_windows_file_int_fd_at_start(tmpfile):
        """A raw descriptor at byte 0 needs no repositioning."""
        fd: int = _open_raw(tmpfile)
        try:
            assert _prepare_windows_file(fd) == (fd, None, 0)
        finally:
            os.close(fd)

    def test_prepare_windows_file_fileno_wrapper(tmpfile):
        """An object exposing only ``fileno()`` resolves to its fd."""
        fd: int = _open_raw(tmpfile)
        try:
            assert _prepare_windows_file(_FilenoOnly(fd)) == (fd, None, 0)
        finally:
            os.close(fd)

    def test_prepare_windows_file_non_seekable_fd(monkeypatch, tmpfile):
        """A non-seekable descriptor records no position to restore.

        Pipes, sockets and standard streams make ``os.lseek`` raise.
        The patched ``lseek`` stands in for such a descriptor
        deterministically.
        """
        fd: int = _open_raw(tmpfile)

        def broken_lseek(fd_: int, pos: int, how: int) -> int:
            raise OSError(29, 'Illegal seek')

        monkeypatch.setattr(os, 'lseek', broken_lseek)
        try:
            assert _prepare_windows_file(fd) == (fd, None, None)
        finally:
            monkeypatch.undo()
            os.close(fd)

    def test_file_object_lock_restores_position(tmpfile):
        """Locking a file object away from byte 0 restores its position.

        The lock itself is taken at byte 0 for consistent mutual
        exclusion, but the caller's read/write position must survive
        both the lock and the unlock.
        """
        with open(tmpfile, 'wb') as fh:  # noqa: PTH123
            fh.write(b'x' * 1024)
        with open(tmpfile, 'r+b') as fh:  # noqa: PTH123
            fh.seek(512)
            portalocker.lock(fh, LockFlags.EXCLUSIVE | LockFlags.NON_BLOCKING)
            assert fh.tell() == 512
            portalocker.unlock(fh)
            assert fh.tell() == 512

    def test_nt_dispatch_rejects_plain_callable(monkeypatch, tmpfile):
        """The bare-callable ``LOCKER`` form is POSIX-only.

        On POSIX a plain callable is handed ``fcntl``-style arguments.
        Windows has no equivalent, so both ``lock`` and ``unlock`` must
        reject it with a ``TypeError`` naming the supported forms.
        """

        def plain(fd: int, flags: int) -> None:
            raise AssertionError('the plain callable must never be called')

        monkeypatch.setattr(portalocker.portalocker, 'LOCKER', plain)
        with open(tmpfile, 'w') as fh:  # noqa: PTH123
            with pytest.raises(TypeError, match='BaseLocker'):
                portalocker.lock(fh, LockFlags.EXCLUSIVE)
            with pytest.raises(TypeError, match='BaseLocker'):
                portalocker.unlock(fh)

    def test_nt_dispatch_instance_and_tuple(monkeypatch, tmpfile):
        """Instance and tuple ``LOCKER`` forms dispatch on Windows too.

        Mirrors ``test_posix_locker_dispatch``: the resolver's instance
        and tuple branches are shared by both platforms and must be
        exercised on each. ``MsvcrtLocker`` works without pywin32, so
        this runs on every Windows cell.
        """
        locker = MsvcrtLocker()
        forms: list[object] = [locker, (locker.lock, locker.unlock)]
        for form in forms:
            monkeypatch.setattr(portalocker.portalocker, 'LOCKER', form)
            with open(tmpfile, 'w') as fh:  # noqa: PTH123
                portalocker.lock(
                    fh, LockFlags.EXCLUSIVE | LockFlags.NON_BLOCKING
                )
                portalocker.unlock(fh)

    def test_msvcrt_shared_delegation_flag_translation(tmpfile):
        """Shared locks forward only ``NON_BLOCKING`` to the win32 locker.

        The stub records what the delegation hands over, so this runs
        with and without pywin32: plain ``SHARED`` arrives as an empty
        flag set (``LockFileEx``'s own shared default) and the
        non-blocking variant carries exactly ``NON_BLOCKING``.
        """
        locker = MsvcrtLocker()
        stub = _RecordingWin32Stub()
        locker._win32_locker = stub  # type: ignore[assignment]
        with open(tmpfile, 'w') as fh:  # noqa: PTH123
            locker.lock(fh, LockFlags.SHARED)
            locker.lock(fh, LockFlags.SHARED | LockFlags.NON_BLOCKING)
        assert stub.lock_calls == [LockFlags(0), LockFlags.NON_BLOCKING]

    def test_msvcrt_lock_wraps_non_contention_oserror(monkeypatch, tmpfile):
        """A non-contention ``OSError`` becomes a plain ``LockException``.

        Errnos 13/16/33/36 mean contention and map to ``AlreadyLocked``.
        Anything else (``EINVAL`` here) is a real failure and must
        surface as the base ``LockException`` with the cause chained.
        """
        import msvcrt

        locker = MsvcrtLocker()
        failure = OSError(22, 'Invalid argument')

        def broken_locking(fd: int, mode: int, length: int) -> None:
            raise failure

        monkeypatch.setattr(msvcrt, 'locking', broken_locking)
        with (
            open(tmpfile, 'w') as fh,  # noqa: PTH123
            pytest.raises(portalocker.LockException) as exc_info,
        ):
            locker.lock(fh, LockFlags.EXCLUSIVE)
        assert not isinstance(exc_info.value, portalocker.AlreadyLocked)
        assert exc_info.value.__cause__ is failure

    @pytest.fixture
    def eacces_locking(monkeypatch):
        """Make ``msvcrt.locking`` fail with ``EACCES`` (errno 13)."""
        import msvcrt

        def denied(fd: int, mode: int, length: int) -> None:
            raise OSError(13, 'Permission denied')

        monkeypatch.setattr(msvcrt, 'locking', denied)

    def test_msvcrt_unlock_eacces_falls_back_to_win32(eacces_locking, tmpfile):
        """``EACCES`` on unlock retries through the win32 locker.

        That retry is how a lock taken by the shared (win32) path is
        released. The stub records the fallback call, so the path is
        exercised on every Windows cell.
        """
        locker = MsvcrtLocker()
        stub = _RecordingWin32Stub()
        locker._win32_locker = stub  # type: ignore[assignment]
        with open(tmpfile, 'w') as fh:  # noqa: PTH123
            locker.unlock(fh)
        assert stub.unlock_calls == 1

    def test_msvcrt_unlock_fallback_lock_exception_reports_both(
        eacces_locking, tmpfile
    ):
        """A failing win32 fallback reports both failures in one message."""
        locker = MsvcrtLocker()
        stub = _RecordingWin32Stub(
            unlock_error=portalocker.LockException(
                portalocker.LockException.LOCK_FAILED,
                'win32 fallback exploded',
            )
        )
        locker._win32_locker = stub  # type: ignore[assignment]
        with (
            open(tmpfile, 'w') as fh,  # noqa: PTH123
            pytest.raises(
                portalocker.LockException,
                match='win32 fallback failed',
            ),
        ):
            locker.unlock(fh)

    def test_msvcrt_unlock_fallback_unexpected_error_is_wrapped(
        eacces_locking, tmpfile
    ):
        """A non-portalocker fallback error is wrapped, not leaked raw."""
        locker = MsvcrtLocker()
        stub = _RecordingWin32Stub(
            unlock_error=RuntimeError('stub blew a fuse')
        )
        locker._win32_locker = stub  # type: ignore[assignment]
        with (
            open(tmpfile, 'w') as fh,  # noqa: PTH123
            pytest.raises(
                portalocker.LockException,
                match='unexpected error',
            ),
        ):
            locker.unlock(fh)

    def test_msvcrt_unlock_eacces_without_pywin32_names_extra(
        eacces_locking, tmpfile
    ):
        """With no win32 fallback available, the error names the extra."""
        locker = MsvcrtLocker()
        locker._win32_locker = None
        with (
            open(tmpfile, 'w') as fh,  # noqa: PTH123
            pytest.raises(
                portalocker.LockException,
                match=r'portalocker\[win32\]',
            ),
        ):
            locker.unlock(fh)

    def test_msvcrt_unlock_wraps_non_eacces_oserror(monkeypatch, tmpfile):
        """Any other unlock ``OSError`` surfaces as ``LockException``."""
        import msvcrt

        locker = MsvcrtLocker()
        failure = OSError(22, 'Invalid argument')

        def broken_locking(fd: int, mode: int, length: int) -> None:
            raise failure

        monkeypatch.setattr(msvcrt, 'locking', broken_locking)
        with (
            open(tmpfile, 'w') as fh,  # noqa: PTH123
            pytest.raises(portalocker.LockException) as exc_info,
        ):
            locker.unlock(fh)
        assert exc_info.value.__cause__ is failure

    def test_win32_get_os_handle_translates_fd(tmpfile):
        """``_get_os_handle`` needs only ``msvcrt``, not pywin32.

        The instance is created with ``__new__`` to skip the pywin32
        availability probe in ``__init__``, so this runs on every
        Windows cell.
        """
        locker = Win32Locker.__new__(Win32Locker)
        with open(tmpfile, 'w') as fh:  # noqa: PTH123
            handle: int = locker._get_os_handle(fh.fileno())
        assert isinstance(handle, int)

    @pytest.mark.skipif(not _HAS_PYWIN32, reason='requires pywin32 installed')
    def test_win32_unlock_swallows_not_locked(monkeypatch, tmpfile):
        """``ERROR_NOT_LOCKED`` on unlock is harmless and swallowed."""
        import pywintypes
        import win32file
        import winerror

        locker = Win32Locker()
        err = pywintypes.error(
            winerror.ERROR_NOT_LOCKED,
            'UnlockFileEx',
            'The segment is already unlocked.',
        )

        def boom(*args: object, **kwargs: object) -> None:
            raise err

        monkeypatch.setattr(win32file, 'UnlockFileEx', boom)
        with open(tmpfile, 'w') as fh:  # noqa: PTH123
            locker.unlock(fh)

    @pytest.mark.skipif(not _HAS_PYWIN32, reason='requires pywin32 installed')
    def test_win32_unlock_wraps_other_win32_error(monkeypatch, tmpfile):
        """Any other Win32 unlock error becomes a ``LockException``."""
        import pywintypes
        import win32file

        locker = Win32Locker()
        # ERROR_ACCESS_DENIED (5) != ERROR_NOT_LOCKED (158).
        err = pywintypes.error(5, 'UnlockFileEx', 'Access is denied.')

        def boom(*args: object, **kwargs: object) -> None:
            raise err

        monkeypatch.setattr(win32file, 'UnlockFileEx', boom)
        with (
            open(tmpfile, 'w') as fh,  # noqa: PTH123
            pytest.raises(portalocker.LockException) as exc_info,
        ):
            locker.unlock(fh)
        assert exc_info.value.__cause__ is err

    @pytest.mark.skipif(not _HAS_PYWIN32, reason='requires pywin32 installed')
    def test_win32_unlock_wraps_oserror_from_stale_fd(monkeypatch, tmpfile):
        """A stale-fd ``OSError`` on unlock is wrapped like on lock."""
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
            locker.unlock(fh)
        assert exc_info.value.__cause__ is stale

else:
    pytest.skip(
        'Windows lockers are only defined on nt',
        allow_module_level=True,
    )
