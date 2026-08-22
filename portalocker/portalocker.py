# The msvcrt/pywin32 modules are unavailable outside Windows, and `LOCKER`
# is assigned exactly once per platform-specific branch while pyright
# analyzes all branches.
# pyright: reportUnknownMemberType=false, reportAttributeAccessIssue=false
# pyright: reportConstantRedefinition=false
"""Module portalocker.

This module provides cross-platform file locking functionality.

On POSIX systems locking is provided by ``fcntl.flock`` (or ``fcntl.lockf``
via :class:`LockfLocker`), with no extra dependencies.

On Windows the default locker is :class:`MsvcrtLocker`, which needs no
extra dependencies for *exclusive* locks (it uses the built-in ``msvcrt``
module). *Shared* locks require the Win32 API
(``win32file.LockFileEx``/``UnlockFileEx``) provided by the optional
``pywin32`` package, installable through the ``win32`` extra::

    pip install "portalocker[win32]"

Without ``pywin32``, acquiring a shared lock on Windows raises
``ImportError``. :class:`Win32Locker` can be used directly to lock through
the Win32 API exclusively; it always requires ``pywin32``.

This version uses classes to encapsulate locking logic, while maintaining
the original external API, including the LOCKER constant for specific
backwards compatibility (POSIX) and Windows behavior.
"""

import io
import os
import typing
from collections.abc import Callable
from typing import (
    Any,
    cast,
)

from . import constants, exceptions, types

# Alias for readability
LockFlags = constants.LockFlags


# Define a protocol for callable lockers
class LockCallable(typing.Protocol):
    """Call signature of the locking half of a ``LOCKER`` pair.

    `LOCKER` may be set to a ``(lock, unlock)`` tuple of two plain
    callables instead of to a `BaseLocker`; this protocol describes the
    first element. It is a typing construct only: it types `LockerType`
    and the pair returned by `_resolve_locker_pair`, and nothing checks
    it at runtime.

    The bound `lock` method of every locker in this module matches it,
    which is why ``(locker.lock, locker.unlock)`` is a valid `LOCKER`.

    See Also:
        `UnlockCallable`: the unlocking half of the same pair.
    """

    def __call__(self, file_obj: types.FileArgument, flags: LockFlags) -> None:
        """Acquire a lock on `file_obj`.

        Args:
            file_obj: An open file object, an object exposing `fileno()`,
                or a raw file descriptor.
            flags: The `LockFlags` combination describing the lock to
                take, e.g. ``EXCLUSIVE | NON_BLOCKING``.
        """
        ...


class UnlockCallable(typing.Protocol):
    """Call signature of the unlocking half of a ``LOCKER`` pair.

    The counterpart of `LockCallable`: the second element of a
    ``(lock, unlock)`` tuple assigned to `LOCKER`. It takes no flags
    because releasing a lock needs no options - the locker already knows
    what it took.

    See Also:
        `LockCallable`: the locking half of the same pair.
    """

    def __call__(self, file_obj: types.FileArgument) -> None:
        """Release a lock previously taken on `file_obj`.

        Args:
            file_obj: The same file object, `fileno()` provider or raw
                file descriptor that was passed to the lock callable.
        """
        ...


class BaseLocker:
    """Base class for locker implementations.

    A locker is the thin layer between `portalocker.lock` and the
    platform's locking system call. Each platform branch of this module
    defines its own subclasses - `Win32Locker` and `MsvcrtLocker` on
    Windows, `PosixLocker` and friends on POSIX - and `LOCKER` names the
    one the module-level `lock` / `unlock` dispatch to.

    Subclass it to plug in a custom mechanism: implement both methods,
    then assign the class or an instance of it to `LOCKER`. Both forms
    are accepted (see `_resolve_locker_pair`); the class form is
    instantiated once, on first use, and cached.
    """

    def lock(self, file_obj: types.FileArgument, flags: LockFlags) -> None:
        """Lock `file_obj` according to `flags`.

        Implementations must keep contention and real failure apart,
        because the retry machinery in `portalocker.utils.Lock.acquire`
        acts on the distinction: raise
        `~portalocker.exceptions.AlreadyLocked` when somebody else holds
        a conflicting lock, and a plain
        `~portalocker.exceptions.LockException` for any other failure.
        Only `AlreadyLocked` is retried until the timeout expires. Any
        other exception is treated as permanent and aborts the acquire
        immediately, so a custom locker that reports contention as a
        plain `LockException` loses all retrying. The bundled lockers
        follow the contract; mirror them.

        Args:
            file_obj: An open file object, an object exposing `fileno()`,
                or a raw file descriptor.
            flags: The `LockFlags` combination to apply.

        Raises:
            NotImplementedError: Always; subclasses must override this.
        """
        raise NotImplementedError

    def unlock(self, file_obj: types.FileArgument) -> None:
        """Release a lock previously taken by `lock`.

        Args:
            file_obj: The same file object, `fileno()` provider or raw
                file descriptor that was passed to `lock`.

        Raises:
            NotImplementedError: Always; subclasses must override this.
        """
        raise NotImplementedError


# Define refined LockerType with more specific types
LockerType = (
    # POSIX-style fcntl.flock callable
    Callable[[int | types.HasFileno, int], Any]
    # Tuple of lock and unlock functions
    | tuple[LockCallable, UnlockCallable]
    # BaseLocker instance
    | BaseLocker
    # BaseLocker class
    | type[BaseLocker]
)

LOCKER: LockerType

#: Cache of ``BaseLocker`` subclasses instantiated when ``LOCKER`` is a class
#: rather than an instance. Shared by the Windows and POSIX dispatchers.
_locker_instances: dict[type[BaseLocker], BaseLocker] = {}


def _resolve_locker_pair(
    locker: object,
) -> tuple[LockCallable, UnlockCallable] | None:
    """Resolve the non-callable ``LockerType`` forms to a ``(lock, unlock)``
    pair.

    Handles the three high-level forms shared by every platform: a
    ``BaseLocker`` instance, a ``(lock, unlock)`` tuple, and a ``BaseLocker``
    subclass (instantiated once and cached in :data:`_locker_instances`).

    Returns ``None`` for the plain POSIX-style ``fcntl`` callable, which each
    platform resolves itself: POSIX applies its own error translation while
    Windows rejects it. ``locker`` is typed ``object`` because this is a
    runtime dispatch over the ``LockerType`` union; the type checkers cannot
    narrow the parameterised ``tuple`` / ``type`` members from ``isinstance``
    alone, so the concrete forms are recovered with explicit casts.
    """
    if isinstance(locker, BaseLocker):
        return locker.lock, locker.unlock
    if isinstance(locker, tuple):
        pair = cast('tuple[LockCallable, UnlockCallable]', locker)
        return pair[0], pair[1]
    if isinstance(locker, type):
        locker_cls = cast('type[BaseLocker]', locker)
        instance = _locker_instances.get(locker_cls)
        if instance is None:
            instance = _locker_instances[locker_cls] = locker_cls()
        return instance.lock, instance.unlock
    return None


def _validate_lock_flags(flags: LockFlags) -> None:
    """Reject flag combinations that cannot describe a lock request.

    Called by the module-level `lock` on every platform, before any
    system call, so nonsense flags fail loudly instead of doing something
    silently wrong. Three combinations are rejected:

    - Anything carrying `LockFlags.UNBLOCK`. Passing it to `lock` used to
      silently *release* a held lock on POSIX, because the bit went
      straight through to ``fcntl``. Releasing is `unlock`'s job.
    - ``SHARED | EXCLUSIVE``. The two lock types contradict each other,
      and what actually happened depended on the platform.
    - A flag set naming no lock type at all, such as ``LockFlags(0)`` or
      `LockFlags.NON_BLOCKING` on its own. `NON_BLOCKING` only says *how*
      to wait, not *what* to take.

    Note:
        On Windows ``LockFlags.UNBLOCK`` is ``msvcrt.LK_UNLCK``, which is
        0, so the UNBLOCK check cannot trigger there. A bare
        ``LockFlags.UNBLOCK`` still fails on Windows, through the
        no-lock-type check, since the flag adds no bits.

    Args:
        flags: The `LockFlags` combination passed to `lock`.

    Raises:
        RuntimeError: `flags` matches one of the rejected combinations
            described above.
    """
    if flags & LockFlags.UNBLOCK:  # pragma: not-posix
        raise RuntimeError(
            'lock() cannot release locks: LockFlags.UNBLOCK is not a valid '
            'flag for lock(). Call unlock() to release a lock.'
        )
    if flags & LockFlags.SHARED and flags & LockFlags.EXCLUSIVE:
        raise RuntimeError(
            'LockFlags.SHARED and LockFlags.EXCLUSIVE are mutually '
            'exclusive. Pass exactly one of them.'
        )
    if not flags & (LockFlags.SHARED | LockFlags.EXCLUSIVE):
        raise RuntimeError(
            'lock() needs a lock type: combine the flags with '
            'LockFlags.SHARED or LockFlags.EXCLUSIVE.'
        )


#: Fallback values for the ``msvcrt`` locking-mode constants, from the
#: ``<sys/locking.h>`` header that ``msvcrt.locking`` forwards its mode
#: argument to. Only consulted for constants the running interpreter's
#: ``msvcrt`` module does not expose itself.
_MSVCRT_LOCK_MODE_DEFAULTS: dict[str, int] = {
    'LK_UNLCK': 0,
    'LK_LOCK': 1,
    'LK_NBLCK': 2,
    'LK_RLCK': 3,
    'LK_NBRLCK': 4,
}


def _resolve_msvcrt_lock_modes(msvcrt_module: object) -> dict[str, int]:
    """Resolve the ``LK_*`` locking modes for an ``msvcrt``-like module.

    Prefers the constants the module itself defines and falls back to
    `_MSVCRT_LOCK_MODE_DEFAULTS` for any it lacks. The module is never
    mutated: before 4.2.0 the fallbacks were ``setattr``'d onto the
    shared stdlib ``msvcrt`` module, and the fallback table itself was
    wrong (``LK_LOCK`` fell back to 0, which is ``LK_UNLCK``, so a
    "blocking lock" through it would have issued an unlock).

    Args:
        msvcrt_module: The module to read ``LK_*`` constants from.
            Typed ``object`` so tests can pass a stub. On Windows this is
            the real ``msvcrt``, whose CPython builds have defined all
            five constants for as long as the module has existed, so the
            fallbacks are pure defence in depth.

    Returns:
        A mapping from constant name (``'LK_LOCK'`` etc.) to its integer
        locking mode.
    """
    return {
        name: cast('int', getattr(msvcrt_module, name, default))
        for name, default in _MSVCRT_LOCK_MODE_DEFAULTS.items()
    }


# The Windows implementation is measured on the Windows CI cells and only
# excluded where it cannot run (POSIX, via the `not-nt` plugin rule).
# The pywin32-only method bodies inside carry `nt-no-pywin32` so the
# pywin32-less Windows cell is not failed on code it cannot reach.
if os.name == 'nt':  # pragma: not-nt
    # ``msvcrt`` ships with every Windows Python build, so inside this
    # branch the import cannot fail and needs no guard.
    import msvcrt

    # Windows-specific helper functions
    def _prepare_windows_file(
        file_obj: types.FileArgument,
    ) -> tuple[int, typing.IO[Any] | None, int | None]:
        """Prepare file for Windows: get fd, seek to 0 and save prior pos.

        ``msvcrt.locking`` and ``LockFileEx`` lock a byte range relative to
        the *current* file position, so every path must seek to byte 0 first
        for consistent mutual exclusion (otherwise two locks taken at
        different positions on files larger than the lock length do not
        conflict). Full IO objects are seeked/restored via ``seek``/``tell``;
        raw descriptors (``int`` / ``HasFileno``) via ``os.lseek``.

        Args:
            file_obj: An open file object, an object exposing `fileno()`,
                or a raw file descriptor.

        Returns:
            A ``(fd, io_obj, original_pos)`` triple to hand straight to
            `_restore_windows_file_pos` once the lock or unlock call has
            returned. ``io_obj`` is the file object when there was one and
            `None` for raw descriptors; ``original_pos`` is `None` when the
            descriptor is not seekable (a pipe, socket or standard stream)
            and therefore has no position worth restoring.
        """
        # Full IO objects (have tell/seek) -> preserve and restore position
        original_pos: int | None
        if isinstance(file_obj, io.IOBase):
            fd: int = file_obj.fileno()
            original_pos = file_obj.tell()
            if original_pos != 0:
                file_obj.seek(0)
            return fd, typing.cast(typing.IO[Any], file_obj), original_pos
            # cast satisfies mypy: IOBase -> IO[Any]

        # Raw descriptor (int) or an object that only implements fileno()
        # (HasFileno). There is no Python-level file object to seek, so use
        # the fd directly and let the caller restore it with os.lseek.
        if isinstance(file_obj, int):
            fd = file_obj
        else:
            fd = typing.cast(types.HasFileno, file_obj).fileno()  # type: ignore[redundant-cast]
        # A raw fd may be non-seekable (a pipe, socket or standard stream),
        # where os.lseek raises OSError ("Illegal seek"). Such fds have no
        # meaningful position to normalize, so skip the seek and record no
        # position to restore.
        try:
            original_pos = os.lseek(fd, 0, os.SEEK_CUR)
            if original_pos != 0:
                os.lseek(fd, 0, os.SEEK_SET)
        except OSError:
            original_pos = None
        return fd, None, original_pos

    def _restore_windows_file_pos(
        fd: int,
        file_io_obj: typing.IO[Any] | None,
        original_pos: int | None,
    ) -> None:
        """Restore a saved file position after a lock/unlock operation.

        IO objects are restored via ``seek``; raw descriptors (no IO object)
        via ``os.lseek`` on ``fd``.

        Args:
            fd: The file descriptor `_prepare_windows_file` returned.
            file_io_obj: The file object it returned, or `None` when the
                caller passed a raw descriptor.
            original_pos: The position it returned. `None` (non-seekable)
                and ``0`` (already at the start) are both no-ops.
        """
        if original_pos is None or original_pos == 0:
            return
        if file_io_obj is not None:
            file_io_obj.seek(original_pos)
        else:
            os.lseek(fd, original_pos, os.SEEK_SET)

    class Win32Locker(BaseLocker):
        """Locker using Win32 API (LockFileEx/UnlockFileEx).

        This is the only locker on Windows that can take a *shared* lock:
        ``LockFileEx`` locks shared unless ``LOCKFILE_EXCLUSIVE_LOCK`` is
        requested, whereas ``msvcrt.locking`` has no shared mode at all.
        `MsvcrtLocker`, the default, delegates its shared locks here.

        It is not the default because it needs the optional ``pywin32``
        package. Since 4.0.0 that package is no longer installed with
        portalocker; ``pip install "portalocker[win32]"`` adds it, and
        without it merely constructing this class raises `ImportError`.

        Example:
            .. code-block:: python

                from portalocker import LockFlags
                from portalocker.portalocker import Win32Locker

                locker = Win32Locker()
                with open('example.txt', 'w') as fh:
                    locker.lock(fh, LockFlags.SHARED)
                    locker.unlock(fh)

        See Also:
            `MsvcrtLocker`: the dependency-free Windows default.
        """

        _lock_bytes_low: int = -0x10000

        def __init__(self) -> None:
            """Verify that ``pywin32`` is importable.

            Nothing is cached here. In particular the ``OVERLAPPED``
            structure handed to ``LockFileEx``/``UnlockFileEx`` is
            created fresh for every call (see `lock` / `unlock`): the
            Win32 API forbids sharing one ``OVERLAPPED`` between
            concurrent calls, which a cached instance would do the moment
            two threads lock through the same locker.

            Raises:
                ImportError: ``pywin32`` is not installed. The message
                    names the ``win32`` extra that provides it.
            """
            try:
                # Imported purely as an availability probe.
                import pywintypes  # noqa: F401  # pyright: ignore[reportUnusedImport]
            except ImportError as e:
                raise ImportError(
                    'Win32Locker requires the win32 extra (pywin32). '
                    'Install it with: pip install "portalocker[win32]"'
                ) from e

        def _get_os_handle(self, fd: int) -> int:
            """Translate a C runtime descriptor into a Win32 file handle.

            ``LockFileEx`` and ``UnlockFileEx`` operate on OS handles
            rather than on the descriptors Python hands out, so both go
            through ``msvcrt.get_osfhandle`` first.

            Args:
                fd: The file descriptor to translate.

            Returns:
                The Win32 file handle owning `fd`.
            """
            return cast(int, msvcrt.get_osfhandle(fd))  # type: ignore[attr-defined]

        def lock(  # pragma: nt-no-pywin32 - the body needs pywin32
            self,
            file_obj: types.FileArgument,
            flags: LockFlags,
        ) -> None:
            """Lock `file_obj` through ``win32file.LockFileEx``.

            The file position is normalized to byte 0 before the call and
            restored afterwards (see `_prepare_windows_file`), so that two
            processes holding the file at different offsets still contend
            for the same range.

            Args:
                file_obj: An open file object, an object exposing
                    `fileno()`, or a raw file descriptor.
                flags: `LockFlags.EXCLUSIVE` adds
                    ``LOCKFILE_EXCLUSIVE_LOCK`` and
                    `LockFlags.NON_BLOCKING` adds
                    ``LOCKFILE_FAIL_IMMEDIATELY``. `LockFlags.SHARED` sets
                    neither, which is ``LockFileEx``'s own default and is
                    how a shared lock is expressed.

            Raises:
                ~portalocker.exceptions.AlreadyLocked: Windows reported
                    ``ERROR_LOCK_VIOLATION``, i.e. someone else holds a
                    conflicting lock on the range.
                ~portalocker.exceptions.LockException: Any other Win32 error,
                    or an ``OSError`` such as ``msvcrt.get_osfhandle``
                    rejecting a stale file descriptor. Neither a raw
                    ``pywintypes.error`` nor a raw ``OSError`` escapes this
                    method.
            """
            import pywintypes
            import win32con
            import win32file
            import winerror

            fd, io_obj_ctx, pos_ctx = _prepare_windows_file(file_obj)

            mode = 0
            if flags & LockFlags.NON_BLOCKING:
                mode |= win32con.LOCKFILE_FAIL_IMMEDIATELY
            if flags & LockFlags.EXCLUSIVE:
                mode |= win32con.LOCKFILE_EXCLUSIVE_LOCK

            try:
                os_fh = self._get_os_handle(fd)
                # A fresh OVERLAPPED per call: the Win32 API forbids
                # sharing one instance between concurrent calls.
                overlapped = pywintypes.OVERLAPPED()
                win32file.LockFileEx(
                    os_fh, mode, 0, self._lock_bytes_low, overlapped
                )
            except pywintypes.error as exc_value:
                if exc_value.winerror == winerror.ERROR_LOCK_VIOLATION:
                    raise exceptions.AlreadyLocked(
                        exceptions.LockException.LOCK_FAILED,
                        exc_value.strerror,
                        fh=file_obj,  # Pass original file_obj
                    ) from exc_value
                else:
                    # Any other Win32 error must still surface as a
                    # LockException per the documented contract, not as a
                    # raw pywintypes.error.
                    raise exceptions.LockException(
                        exceptions.LockException.LOCK_FAILED,
                        exc_value.strerror,
                        fh=file_obj,  # Pass original file_obj
                    ) from exc_value
            except OSError as exc_value:
                # Mirror unlock(): a stale or invalid descriptor makes
                # msvcrt.get_osfhandle raise OSError, which must not
                # escape lock() raw while unlock() wraps it.
                raise exceptions.LockException(
                    exceptions.LockException.LOCK_FAILED,
                    exc_value.strerror,
                    fh=file_obj,  # Pass original file_obj
                ) from exc_value
            finally:
                _restore_windows_file_pos(fd, io_obj_ctx, pos_ctx)

        def unlock(  # pragma: nt-no-pywin32 - the body needs pywin32
            self,
            file_obj: types.FileArgument,
        ) -> None:
            """Release a lock through ``win32file.UnlockFileEx``.

            ``ERROR_NOT_LOCKED`` is swallowed, so unlocking a range that
            is not locked - releasing twice, or releasing a lock Windows
            already dropped - is harmless.

            Args:
                file_obj: The same file object, `fileno()` provider or
                    raw file descriptor that was passed to `lock`.

            Raises:
                ~portalocker.exceptions.LockException: Any Win32 error other
                    than ``ERROR_NOT_LOCKED``, or any `OSError` raised while
                    unlocking.
            """
            import pywintypes
            import win32file
            import winerror

            fd, io_obj_ctx, pos_ctx = _prepare_windows_file(file_obj)

            try:
                os_fh = self._get_os_handle(fd)
                # A fresh OVERLAPPED per call, as in lock().
                overlapped = pywintypes.OVERLAPPED()
                win32file.UnlockFileEx(
                    os_fh, 0, self._lock_bytes_low, overlapped
                )
            except pywintypes.error as exc:
                if exc.winerror != winerror.ERROR_NOT_LOCKED:
                    raise exceptions.LockException(
                        exceptions.LockException.LOCK_FAILED,
                        exc.strerror,
                        fh=file_obj,  # Pass original file_obj
                    ) from exc
            except OSError as exc:
                raise exceptions.LockException(
                    exceptions.LockException.LOCK_FAILED,
                    exc.strerror,
                    fh=file_obj,  # Pass original file_obj
                ) from exc
            finally:
                _restore_windows_file_pos(fd, io_obj_ctx, pos_ctx)

    class MsvcrtLocker(BaseLocker):
        """Default Windows locker, based on ``msvcrt.locking``.

        Exclusive locks work without any extra dependencies. Shared locks
        are delegated to :class:`Win32Locker` and therefore require the
        optional ``pywin32`` package (``pip install "portalocker[win32]"``);
        without it, acquiring a shared lock raises ``ImportError``.
        """

        _win32_locker: Win32Locker | None
        _lock_modes: dict[str, int]
        _msvcrt_lock_length: int = 0x10000

        def __init__(self) -> None:
            """Set up the msvcrt locker and its optional win32 fallback.

            A `Win32Locker` is built eagerly, because two paths need the
            Win32 API: shared locks in `lock`, and the ``EACCES`` retry in
            `unlock`. ``pywin32`` is optional since 4.0.0, so its absence
            is recorded as `None` here rather than raised, and is only
            reported if one of those two paths is actually taken.

            The ``LK_*`` locking modes are resolved once, via
            `_resolve_msvcrt_lock_modes`, and stored on the instance.
            Before 4.2.0 any missing constant was ``setattr``'d onto the
            shared stdlib ``msvcrt`` module instead, with a fallback
            table whose values were wrong: ``LK_LOCK`` fell back to 0,
            which is ``LK_UNLCK``, so a "blocking lock" through the
            fallback would have issued an unlock.
            """
            try:
                self._win32_locker = Win32Locker()
            except ImportError:
                # pywin32 is an optional extra since 4.0.0. Without it,
                # exclusive locks still work via the pure-msvcrt path
                # below; shared locks and the unlock() fallback raise
                # informative errors instead of crashing here.
                self._win32_locker = None
            self._lock_modes = _resolve_msvcrt_lock_modes(msvcrt)

        def lock(self, file_obj: types.FileArgument, flags: LockFlags) -> None:
            """Lock `file_obj`, handing shared locks to `Win32Locker`.

            Exclusive locks go through ``msvcrt.locking`` and need no
            extra dependency. `LockFlags.SHARED` has no ``msvcrt``
            equivalent, so it is forwarded to the `Win32Locker` built in
            `__init__` with only `LockFlags.NON_BLOCKING` carried across.

            The msvcrt path locks ``_msvcrt_lock_length`` bytes (64 KiB)
            starting at byte 0; `_prepare_windows_file` normalizes and
            restores the position around the call because
            ``msvcrt.locking`` works relative to the current one. Before
            4.0.0 raw file descriptors were locked from wherever they
            happened to be positioned, which could leave two holders
            locking different ranges of a file larger than 64 KiB and so
            not excluding each other at all.

            Args:
                file_obj: An open file object, an object exposing
                    `fileno()`, or a raw file descriptor.
                flags: `LockFlags.NON_BLOCKING` selects ``LK_NBLCK``
                    instead of ``LK_LOCK``; `LockFlags.SHARED` switches to
                    the `Win32Locker` path entirely.

            Raises:
                ImportError: `LockFlags.SHARED` was requested but
                    ``pywin32`` is not installed. The message names the
                    ``win32`` extra that provides it.
                ~portalocker.exceptions.AlreadyLocked: ``msvcrt.locking``
                    failed with an errno that means contention (13, 16, 33 or
                    36).
                ~portalocker.exceptions.LockException: ``msvcrt.locking``
                    failed for any other reason.

            Example:
                .. code-block:: python

                    from portalocker import LockFlags
                    from portalocker.portalocker import MsvcrtLocker

                    locker = MsvcrtLocker()
                    with open('example.txt', 'w') as fh:
                        # No pywin32 needed for an exclusive lock.
                        locker.lock(fh, LockFlags.EXCLUSIVE)
                        locker.unlock(fh)
            """
            if flags & LockFlags.SHARED:
                win32_locker = self._win32_locker
                if win32_locker is None:
                    raise ImportError(
                        'Shared locks on Windows require the win32 extra '
                        '(pywin32); msvcrt provides no true shared lock. '
                        'Install it with: pip install "portalocker[win32]"'
                    )
                win32_api_flags = LockFlags(0)
                if flags & LockFlags.NON_BLOCKING:
                    win32_api_flags |= LockFlags.NON_BLOCKING
                win32_locker.lock(file_obj, win32_api_flags)
                return

            fd, io_obj_ctx, pos_ctx = _prepare_windows_file(file_obj)
            mode = (
                self._lock_modes['LK_NBLCK']
                if flags & LockFlags.NON_BLOCKING
                else self._lock_modes['LK_LOCK']
            )

            try:
                msvcrt.locking(  # type: ignore[attr-defined]
                    fd,
                    mode,
                    self._msvcrt_lock_length,
                )
            except OSError as exc_value:
                if exc_value.errno in (13, 16, 33, 36):
                    raise exceptions.AlreadyLocked(
                        exceptions.LockException.LOCK_FAILED,
                        str(exc_value),
                        fh=file_obj,  # Pass original file_obj
                    ) from exc_value
                raise exceptions.LockException(
                    exceptions.LockException.LOCK_FAILED,
                    str(exc_value),
                    fh=file_obj,  # Pass original file_obj
                ) from exc_value
            finally:
                _restore_windows_file_pos(fd, io_obj_ctx, pos_ctx)

        def unlock(self, file_obj: types.FileArgument) -> None:
            """Release a lock through ``msvcrt.locking`` with ``LK_UNLCK``.

            If that fails with ``EACCES`` the unlock is retried through
            `Win32Locker.unlock`, which is how a lock taken by the shared
            path in `lock` gets released. When ``pywin32`` is not
            installed there is no fallback to retry with, so the original
            msvcrt failure is raised with a note naming the missing extra.

            Args:
                file_obj: The same file object, `fileno()` provider or
                    raw file descriptor that was passed to `lock`.

            Raises:
                ~portalocker.exceptions.LockException: ``msvcrt.locking``
                    failed with something other than ``EACCES``; or it failed
                    with ``EACCES`` and no fallback was available; or the
                    fallback itself failed, in which case the message reports
                    both failures.
            """
            fd, io_obj_ctx, pos_ctx = _prepare_windows_file(file_obj)
            took_fallback_path = False

            try:
                msvcrt.locking(  # type: ignore[attr-defined]
                    fd,
                    self._lock_modes['LK_UNLCK'],
                    self._msvcrt_lock_length,
                )
            except OSError as exc:
                if exc.errno == 13:  # EACCES (Permission denied)
                    win32_locker = self._win32_locker
                    if win32_locker is None:
                        # No pywin32 to fall back to; surface the
                        # original msvcrt unlock failure instead.
                        raise exceptions.LockException(
                            exceptions.LockException.LOCK_FAILED,
                            f'{exc.strerror} (the win32 unlock fallback '
                            f'is unavailable without pywin32; install it '
                            f'with: pip install "portalocker[win32]")',
                            fh=file_obj,
                        ) from exc
                    took_fallback_path = True
                    # Restore position before calling win32_locker,
                    # as it will re-prepare.
                    _restore_windows_file_pos(fd, io_obj_ctx, pos_ctx)
                    try:
                        win32_locker.unlock(
                            file_obj
                        )  # win32_locker handles its own seeking
                    except exceptions.LockException as win32_exc:
                        raise exceptions.LockException(
                            exceptions.LockException.LOCK_FAILED,
                            f'msvcrt unlock failed ({exc.strerror}), and '
                            f'win32 fallback failed ({win32_exc.strerror})',
                            fh=file_obj,
                        ) from win32_exc
                    except Exception as final_exc:
                        raise exceptions.LockException(
                            exceptions.LockException.LOCK_FAILED,
                            f'msvcrt unlock failed ({exc.strerror}), and '
                            f'win32 fallback failed with unexpected error: '
                            f'{final_exc!s}',
                            fh=file_obj,
                        ) from final_exc
                else:
                    raise exceptions.LockException(
                        exceptions.LockException.LOCK_FAILED,
                        exc.strerror,
                        fh=file_obj,
                    ) from exc
            finally:
                if not took_fallback_path:
                    _restore_windows_file_pos(fd, io_obj_ctx, pos_ctx)

    LOCKER = MsvcrtLocker

    def lock(file: types.FileArgument, flags: LockFlags) -> None:
        """Lock `file` with the locker named by the module-level `LOCKER`.

        This is the Windows implementation of `portalocker.lock`; the
        POSIX branch of this module defines a separate one. `LOCKER`
        defaults to the `MsvcrtLocker` class and may be replaced with a
        `BaseLocker` instance, a `BaseLocker` subclass, or a
        ``(lock, unlock)`` tuple. The bare-callable form that POSIX
        accepts has no counterpart here, because there is no ``fcntl``.

        Args:
            file: An open file object, an object exposing `fileno()`, or a
                raw file descriptor.
            flags: The `LockFlags` combination to apply. Note that
                `LockFlags.SHARED` needs ``pywin32``; see `MsvcrtLocker`.

        Raises:
            RuntimeError: `flags` is a rejected combination - it carries
                `LockFlags.UNBLOCK`, combines `LockFlags.SHARED` with
                `LockFlags.EXCLUSIVE`, or names no lock type at all. See
                `_validate_lock_flags`.
            TypeError: `LOCKER` holds none of the supported forms - most
                likely a plain callable copied from POSIX code.
            ~portalocker.exceptions.AlreadyLocked: The lock is held by someone
                else and `LockFlags.NON_BLOCKING` was set.
            ~portalocker.exceptions.LockException: The locking call failed for
                any other reason.

        Example:
            .. code-block:: python

                import portalocker

                with open('example.txt', 'w') as fh:
                    portalocker.lock(fh, portalocker.LockFlags.EXCLUSIVE)
                    portalocker.unlock(fh)
        """
        _validate_lock_flags(flags)
        pair = _resolve_locker_pair(LOCKER)
        if pair is None:
            # Windows has no ``fcntl``-style callable locker.
            raise TypeError(
                f'LOCKER must be a BaseLocker instance, a tuple of lock and '
                f'unlock functions, or a subclass of BaseLocker, '
                f'got {type(LOCKER)}.'
            )
        pair[0](file, flags)

    def unlock(file: types.FileArgument) -> None:
        """Release a lock taken by the Windows `lock`.

        Resolves `LOCKER` the same way `lock` does, so a lock and its
        unlock always go to the same implementation as long as `LOCKER`
        is not reassigned in between.

        Args:
            file: The same file object, `fileno()` provider or raw file
                descriptor that was passed to `lock`.

        Raises:
            TypeError: `LOCKER` holds none of the supported forms.
            ~portalocker.exceptions.LockException: The unlocking call failed.
        """
        pair = _resolve_locker_pair(LOCKER)
        if pair is None:
            raise TypeError(
                f'LOCKER must be a BaseLocker instance, a tuple of lock and '
                f'unlock functions, or a subclass of BaseLocker, '
                f'got {type(LOCKER)}.'
            )
        pair[1](file)

else:  # pragma: not-posix
    import errno
    import fcntl

    # PosixLocker methods accept FileArgument | HasFileno
    PosixFileArgument = types.FileArgument | types.HasFileno

    class PosixLocker(BaseLocker):
        """Locker implementation using the `LOCKER` constant.

        Wraps a ``fcntl``-style callable with the parts every POSIX lock
        needs: extracting the file descriptor, rejecting a non-blocking
        request that names no lock type, and translating ``OSError`` into
        `AlreadyLocked` / `LockException`.

        Which callable it wraps depends on the class. `FlockLocker` and
        `LockfLocker` bind ``fcntl.flock`` and ``fcntl.lockf``
        respectively, while a plain `PosixLocker` follows the module-level
        `LOCKER` - so `LOCKER` selects the primitive for the default
        dispatch, and the subclasses pin one regardless of it. Both
        primitives are exposed because a program usually has to match
        whatever the other processes sharing the file already use. The
        subclasses honouring their own callable is a 4.0.0 fix; before
        that they silently used the global `LOCKER` too.

        Example:
            >>> import fcntl
            >>> from portalocker.portalocker import (
            ...     FlockLocker,
            ...     LockfLocker,
            ...     PosixLocker,
            ... )
            >>> PosixLocker().locker is fcntl.flock
            True
            >>> FlockLocker().locker is fcntl.flock
            True
            >>> LockfLocker().locker is fcntl.lockf
            True
        """

        _locker: Callable[[int | types.HasFileno, int], Any] | None = None

        @property
        def locker(self) -> Callable[[int | types.HasFileno, int], Any]:
            """The ``fcntl``-style callable this locker locks with.

            Subclasses set `_locker` at class level and always return
            that. An unbound `PosixLocker` returns the module-level
            `LOCKER`, read on every access, so reassigning `LOCKER`
            redirects existing instances too.

            Returns:
                A callable taking ``(fd, operation)``, normally
                ``fcntl.flock`` or ``fcntl.lockf``.

            Example:
                >>> import fcntl
                >>> from portalocker.portalocker import LockfLocker
                >>> LockfLocker().locker is fcntl.lockf
                True
            """
            if self._locker is None:
                # On POSIX systems ``LOCKER`` is a callable (fcntl.flock) but
                # mypy also sees the Windows-only tuple assignment.  Explicitly
                # cast so mypy knows we are returning the callable variant
                # here.
                return cast(
                    Callable[[int | types.HasFileno, int], Any], LOCKER
                )  # pyright: ignore[reportUnnecessaryCast]

            # mypy does not realise ``self._locker`` is non-None after the
            # check
            assert self._locker is not None
            return self._locker

        def _get_fd(self, file_obj: PosixFileArgument) -> int:
            """Extract the file descriptor the ``fcntl`` call needs.

            Args:
                file_obj: A raw file descriptor, returned unchanged, or
                    any object with a callable `fileno()`.

            Returns:
                The integer file descriptor to lock.

            Raises:
                TypeError: `file_obj` is neither an `int` nor exposes a
                    callable `fileno()`.

            Example:
                >>> from portalocker.portalocker import PosixLocker
                >>> locker = PosixLocker()
                >>> locker._get_fd(0)
                0
                >>> with open('example.txt', 'w') as fh:
                ...     locker._get_fd(fh) == fh.fileno()
                True
            """
            if isinstance(file_obj, int):
                return file_obj
            # Check for fileno() method; covers typing.IO and HasFileno
            elif hasattr(file_obj, 'fileno') and callable(file_obj.fileno):
                return file_obj.fileno()
            else:
                # Should not be reached if PosixFileArgument is correct.
                # isinstance(file_obj, io.IOBase) could be an
                # alternative check
                # but hasattr is more general for HasFileno.
                raise TypeError(
                    "Argument 'file_obj' must be an int, an IO object "
                    'with fileno(), or implement HasFileno.'
                )

        def lock(self, file_obj: PosixFileArgument, flags: LockFlags) -> None:
            """Lock `file_obj` by calling `locker` with `flags`.

            Args:
                file_obj: An open file object, an object exposing
                    `fileno()`, or a raw file descriptor.
                flags: The `LockFlags` combination to apply.
                    `LockFlags.NON_BLOCKING` only says *how* to wait, so
                    it has to be combined with `LockFlags.SHARED` or
                    `LockFlags.EXCLUSIVE` to say what to take.

            Raises:
                RuntimeError: `LockFlags.NON_BLOCKING` was passed on its
                    own, without `LockFlags.SHARED` or
                    `LockFlags.EXCLUSIVE`.
                ~portalocker.exceptions.AlreadyLocked: ``fcntl`` reported
                    ``EACCES`` or ``EAGAIN``, i.e. someone else holds a
                    conflicting lock.
                ~portalocker.exceptions.LockException: Any other ``OSError``,
                    or the ``EOFError`` seen on some network filesystems.

            Note:
                The full flag validation (rejecting UNBLOCK-bearing
                combinations, ``SHARED | EXCLUSIVE`` and flag sets naming
                no lock type) lives in the module-level `portalocker.lock`
                by design. Calling this method directly skips that guard,
                so ``lock(fh, LockFlags.UNBLOCK)`` on a bare locker still
                reaches ``fcntl`` and silently releases the lock.

            Example:
                >>> from portalocker import LockFlags
                >>> from portalocker.portalocker import PosixLocker
                >>> locker = PosixLocker()
                >>> with open('example.txt', 'w') as fh:
                ...     locker.lock(
                ...         fh, LockFlags.EXCLUSIVE | LockFlags.NON_BLOCKING
                ...     )
                ...     locker.unlock(fh)
            """
            if (flags & LockFlags.NON_BLOCKING) and not flags & (
                LockFlags.SHARED | LockFlags.EXCLUSIVE
            ):
                raise RuntimeError(
                    'When locking in non-blocking mode on POSIX, '
                    'the SHARED or EXCLUSIVE flag must be specified as well.'
                )

            fd = self._get_fd(file_obj)
            try:
                self.locker(fd, flags)
            except OSError as exc_value:
                if exc_value.errno in (errno.EACCES, errno.EAGAIN):
                    raise exceptions.AlreadyLocked(
                        exc_value,
                        str(exc_value),
                        fh=file_obj,  # Pass original file_obj
                    ) from exc_value
                else:
                    raise exceptions.LockException(
                        exc_value,
                        str(exc_value),
                        fh=file_obj,  # Pass original file_obj
                    ) from exc_value
            except EOFError as exc_value:
                raise exceptions.LockException(
                    exc_value,
                    str(exc_value),
                    fh=file_obj,  # Pass original file_obj
                ) from exc_value

        def unlock(self, file_obj: PosixFileArgument) -> None:
            """Release a lock by calling `locker` with `LockFlags.UNBLOCK`.

            Failures are translated the same way `lock` translates them,
            matching the Windows lockers: the original ``OSError`` goes
            into ``args[0]`` and onto ``__cause__``, so its ``errno``
            stays reachable.

            Args:
                file_obj: The same file object, `fileno()` provider or raw
                    file descriptor that was passed to `lock`.

            Raises:
                ~portalocker.exceptions.LockException: The unlock call
                    failed, for example with ``EBADF`` when the
                    descriptor was already closed. Wraps the ``OSError``
                    that ``fcntl`` raised, or the ``EOFError`` some NFS
                    setups produce, just like `lock` does.

            .. versionchanged:: 4.2.0
                Previously the raw ``OSError`` propagated unchanged,
                unlike on Windows where unlock failures have always been
                wrapped in `LockException`.
            """
            fd = self._get_fd(file_obj)
            try:
                self.locker(fd, LockFlags.UNBLOCK)
            except OSError as exc_value:
                raise exceptions.LockException(
                    exc_value,
                    str(exc_value),
                    fh=file_obj,  # Pass original file_obj
                ) from exc_value
            except EOFError as exc_value:
                raise exceptions.LockException(
                    exc_value,
                    str(exc_value),
                    fh=file_obj,  # Pass original file_obj
                ) from exc_value

    class FlockLocker(PosixLocker):
        """FlockLocker is a PosixLocker implementation using fcntl.flock."""

        # Bind the callable so this locker uses flock regardless of the
        # module-level ``LOCKER`` fallback. (``fcntl.flock`` is a builtin and
        # therefore is not bound as a descriptor on attribute access.) The
        # explicit annotation keeps the type checkers from treating the
        # class-level callable as a method (which would bind ``self``).
        _locker: Callable[[int | types.HasFileno, int], Any] | None = (
            fcntl.flock
        )

    class LockfLocker(PosixLocker):
        """LockfLocker is a PosixLocker implementation using fcntl.lockf."""

        _locker: Callable[[int | types.HasFileno, int], Any] | None = (
            fcntl.lockf
        )

    # LOCKER constant for POSIX is fcntl.flock for backward compatibility.
    # Type matches: Callable[[int | HasFileno, int], Any]
    LOCKER = fcntl.flock

    _posix_locker_instance = PosixLocker()

    # Public API for POSIX supports every ``LockerType`` form. The plain
    # ``fcntl`` callable is routed through ``_posix_locker_instance`` (fd
    # extraction, non-blocking validation and error translation); the tuple,
    # instance and subclass forms are dispatched via ``_resolve_locker_pair``.
    def lock(file: types.FileArgument, flags: LockFlags) -> None:
        """Lock `file` with the locker named by the module-level `LOCKER`.

        This is the POSIX implementation of `portalocker.lock`; the
        Windows branch of this module defines a separate one. Every
        `LockerType` form is accepted:

        - a bare ``fcntl``-style callable, the default (``fcntl.flock``),
          which is routed through a shared `PosixLocker` so that it still
          gets descriptor extraction, flag validation and error
          translation;
        - a ``(lock, unlock)`` tuple, a `BaseLocker` instance, or a
          `BaseLocker` subclass, all resolved by `_resolve_locker_pair`.

        Honouring all of those forms is a 4.0.0 fix; earlier versions only
        honoured the bare callable here.

        Args:
            file: An open file object, an object exposing `fileno()`, or a
                raw file descriptor.
            flags: The `LockFlags` combination to apply.
                `LockFlags.NON_BLOCKING` must be combined with
                `LockFlags.SHARED` or `LockFlags.EXCLUSIVE`.

        Raises:
            RuntimeError: `flags` is a rejected combination - it carries
                `LockFlags.UNBLOCK`, combines `LockFlags.SHARED` with
                `LockFlags.EXCLUSIVE`, or names no lock type at all. See
                `_validate_lock_flags`.
            ~portalocker.exceptions.AlreadyLocked: The lock is held elsewhere
                and `LockFlags.NON_BLOCKING` was set.
            ~portalocker.exceptions.LockException: The locking call failed for
                another reason.
            RuntimeError: `LockFlags.NON_BLOCKING` was passed on its own,
                without `LockFlags.SHARED` or `LockFlags.EXCLUSIVE`. The
                built-in lockers reject this before touching the file,
                because on POSIX the flag only says *how* to wait and not
                what kind of lock to take.

        The exception translation above is provided by the built-in
        lockers. A raw ``(lock, unlock)`` callable tuple assigned to
        `LOCKER` is invoked as-is and owns its own error translation, so
        an untranslated ``OSError`` can escape it.

        Example:
            >>> import portalocker
            >>> with open('example.txt', 'w') as fh:
            ...     portalocker.lock(fh, portalocker.LockFlags.EXCLUSIVE)
            ...     portalocker.unlock(fh)
        """
        _validate_lock_flags(flags)
        pair = _resolve_locker_pair(LOCKER)
        if pair is None:
            _posix_locker_instance.lock(file, flags)
        else:
            pair[0](file, flags)

    def unlock(file: types.FileArgument) -> None:
        """Release a lock taken by the POSIX `lock`.

        Resolves `LOCKER` the same way `lock` does, so a lock and its
        unlock always go to the same implementation as long as `LOCKER`
        is not reassigned in between.

        Args:
            file: The same file object, `fileno()` provider or raw file
                descriptor that was passed to `lock`.

        Raises:
            ~portalocker.exceptions.LockException: The unlock call failed,
                wrapping the original ``OSError`` (reachable through
                ``args[0]`` and ``__cause__``), matching what the
                Windows unlock has always raised. As with `lock`, a raw
                ``(lock, unlock)`` callable tuple assigned to `LOCKER`
                owns its own error translation and can leak an
                untranslated ``OSError`` instead.

        .. versionchanged:: 4.2.0
            Previously a failing unlock raised the raw ``OSError``.

        Example:
            >>> import portalocker
            >>> with open('example.txt', 'w') as fh:
            ...     portalocker.lock(fh, portalocker.LockFlags.EXCLUSIVE)
            ...     portalocker.unlock(fh)
        """
        pair = _resolve_locker_pair(LOCKER)
        if pair is None:
            _posix_locker_instance.unlock(file)
        else:
            pair[1](file)
