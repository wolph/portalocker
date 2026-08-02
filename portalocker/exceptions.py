"""Exceptions raised when acquiring, holding, or releasing a lock fails.

Hierarchy::

    BaseLockException
      LockException
        AlreadyLocked
        FileToLarge

`BaseLockException` is the shared base and is rarely raised directly;
catch `LockException` to handle any locking failure, or one of its two
subclasses to handle a specific cause.
"""

import typing

from . import types


class BaseLockException(Exception):  # noqa: N818
    """Common base for every exception this module defines.

    Not raised directly; `LockException` and its subclasses are. Beyond
    the standard `Exception` payload, instances carry:

    - `fh`: the filehandle (or file descriptor, or file-like object)
        that was being locked when the failure happened, if one was
        available yet. `None` when the failure happened before a handle
        existed, e.g. while opening the file.
    - `strerror`: the OS error message, when the failure came from a
        system call. `None` otherwise.

    Example:
        >>> from portalocker import exceptions
        >>> try:
        ...     raise exceptions.LockException(
        ...         exceptions.LockException.LOCK_FAILED,
        ...         'Resource temporarily unavailable',
        ...     )
        ... except exceptions.LockException as exc:
        ...     exc.strerror
        'Resource temporarily unavailable'
    """

    LOCK_FAILED: typing.Final = 1
    """The only error code this package has ever raised.

    Callers within this package pass it as the first positional argument
    for every raise; it does not distinguish between causes and exists
    only for backwards compatibility with code that inspects
    `exc.args[0]`.
    """

    strerror: str | None = None  # ensure attribute always exists

    def __init__(
        self,
        *args: typing.Any,
        fh: types.IO | None | int | types.HasFileno = None,
        **kwargs: typing.Any,
    ) -> None:
        """Store `fh` and extract `strerror` from `args[1]`.

        Args:
            *args: Forwarded to `Exception.__init__`. If a second
                positional argument is present and is a `str`, it becomes
                `strerror`; this mirrors the two-argument `OSError`
                convention (an error code, then a message) so a locking
                failure looks the same whether it originated on POSIX or
                on Windows.
            fh: The filehandle involved in the failure, if any. Stored
                unchanged on `self.fh`.
            **kwargs: Ignored here; accepted so that subclasses which add
                their own keyword-only arguments (e.g. `AlreadyLocked`'s
                `holder_pid`) can forward the rest of their keyword
                arguments through `super().__init__(*args, **kwargs)`
                without this initialiser rejecting them.
        """
        self.fh = fh
        self.strerror = (
            str(args[1])
            if len(args) > 1 and isinstance(args[1], str)
            else None
        )
        Exception.__init__(self, *args)


class LockException(BaseLockException):
    """Raised when acquiring or releasing a lock fails.

    This is the general-purpose locking failure and the type to catch
    when any locking error is acceptable to handle uniformly:
    `AlreadyLocked` and `FileToLarge` both derive from it, so `except
    LockException` also catches those.

    .. versionchanged:: 4.0.0
        On POSIX, lock failures now populate `strerror` and pass the
        OS error message as the second positional argument, matching the
        contract this module already followed on Windows. Previously,
        `str(exc)` on POSIX returned the bare underlying `OSError` text;
        it now returns the 2-argument exception repr instead (for
        example ``(1, 'Resource temporarily unavailable')``). Code that
        parsed `str(exc)` on POSIX should read `.strerror` instead, which
        has held the message consistently on both platforms since 4.0.0.
    """


class AlreadyLocked(LockException):
    """Raised when a lock is held elsewhere and cannot be acquired.

    This is what `Lock.acquire` raises immediately when
    `fail_when_locked=True` and the first attempt finds the lock already
    held, and also what it raises when `timeout` expires while waiting
    for the lock to become free.
    """

    holder_pid: int | None = None
    """The PID of the process already holding the lock, when known.

    `None` unless a caller has populated it explicitly; acquiring a lock
    does not discover or fill this in by itself.
    """

    def __init__(
        self,
        *args: typing.Any,
        holder_pid: int | None = None,
        **kwargs: typing.Any,
    ) -> None:
        """Initialise like `BaseLockException`, plus record `holder_pid`.

        Args:
            *args: Forwarded to `BaseLockException.__init__`.
            holder_pid: The PID of the process already holding the lock,
                if known. Stored on `self.holder_pid`; unlike `fh` and
                `strerror`, `BaseLockException` has no equivalent
                attribute, so this is the one addition `AlreadyLocked`
                makes over its base initialiser.
            **kwargs: Forwarded to `BaseLockException.__init__`.
        """
        super().__init__(*args, **kwargs)
        self.holder_pid = holder_pid


class FileToLarge(LockException):
    """Raised when a file is too large for the locking call to handle.

    The misspelling in the name (`FileToLarge`, rather than
    `FileTooLarge`) is a long-standing typo in the public API. It is kept
    exactly as-is for backwards compatibility instead of being silently
    renamed, which would break `except FileToLarge` in existing code.
    """
