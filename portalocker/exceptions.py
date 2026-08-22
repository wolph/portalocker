"""Exceptions raised when acquiring, holding, or releasing a lock fails.

Hierarchy::

    BaseLockException
      LockException
        AlreadyLocked
        LockLostError
        FileToLarge  (deprecated, never raised)

`BaseLockException` is the shared base and is rarely raised directly;
catch `LockException` to handle any locking failure, `AlreadyLocked` to
handle contention specifically, or `LockLostError` to handle a
distributed lock that was revoked while held.
"""

import contextlib
import typing
import warnings

from . import types


class BaseLockException(Exception):  # noqa: N818
    """Common base for every exception this module defines.

    Not raised directly; `LockException` and its subclasses are. Beyond
    the standard `Exception` payload, instances carry:

    - `fh`: the filehandle (or file descriptor, or file-like object)
        that was being locked when the failure happened, if one was
        available yet. `None` when the failure happened before a handle
        existed, e.g. while opening the file.
    - `fh_name`: the name of that file as a plain string, when the
        handle had one. `None` otherwise.
    - `strerror`: the OS error message, when the failure came from a
        system call. `None` otherwise.

    Instances pickle, so they survive the trip out of a
    `multiprocessing` worker back to the parent process. An open file
    object cannot be pickled, so `fh` is dropped (replaced by `None`)
    during pickling; `fh_name` still identifies the file afterwards, and
    an integer file descriptor is kept as-is. This applies recursively
    when one lock exception wraps another in its ``args``.
    `copy.deepcopy` behaves like pickling and drops the handle too,
    while `copy.copy` keeps `fh` shared with the original, since a
    shallow copy never leaves the process where the handle is valid.

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

    The Windows lockers pass it as the first positional argument, with
    the OS message second, so ``exc.args`` is ``(1, message)`` there. The
    POSIX lockers put the original `OSError` in the first slot instead,
    with ``str`` of it second, so code that inspects ``exc.args[0]``
    sees this constant only on Windows. It does not distinguish between
    causes and exists for backwards compatibility.
    """

    strerror: str | None = None  # ensure attribute always exists

    fh_name: str | None = None
    """Name of the file behind `fh` as a plain string, when it had one.

    Unlike `fh` itself, this survives pickling, so an exception that
    crossed a process boundary still says which file was involved.
    """

    def __init__(
        self,
        *args: typing.Any,
        fh: types.IO | int | types.HasFileno | None = None,
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
        # A broken handle may raise from its `name` property (a detached
        # `io.TextIOWrapper` raises `ValueError`), which `getattr` with a
        # default does not swallow. The name is a debugging nicety, so any
        # failure to read it simply leaves `fh_name` as `None`.
        name: typing.Any = None
        with contextlib.suppress(Exception):
            name = getattr(fh, 'name', None)
        self.fh_name = name if isinstance(name, str) else None
        self.strerror = (
            str(args[1])
            if len(args) > 1 and isinstance(args[1], str)
            else None
        )
        Exception.__init__(self, *args)

    def __reduce__(
        self,
    ) -> tuple[
        type['BaseLockException'],
        tuple[typing.Any, ...],
        dict[str, typing.Any],
    ]:
        """Pickle without the filehandle, which cannot be pickled.

        `BaseException.__reduce__` includes the full instance ``__dict__``
        in the pickle payload (and bypasses ``__getstate__``, which is
        why this override targets ``__reduce__`` itself). With an open
        file object on `fh` that made every lock exception unpicklable,
        so a `multiprocessing` worker hitting contention crashed the
        result pipe with a ``MaybeEncodingError`` instead of delivering
        `AlreadyLocked` to the parent.

        Returns:
            The standard ``(callable, args, state)`` reduction triple:
            the class, the ``args`` tuple, and a copy of the instance
            dict in which a non-integer `fh` is replaced by `None`. An
            integer file descriptor pickles fine and is kept, and
            `fh_name` keeps identifying the file either way. Exceptions
            nested inside ``args`` are reduced recursively by pickle
            itself, so a wrapped lock exception sheds its own handle the
            same way.
        """
        state: dict[str, typing.Any] = dict(self.__dict__)
        if state.get('fh') is not None and not isinstance(state['fh'], int):
            state['fh'] = None
        return (self.__class__, self.args, state)

    def __copy__(self) -> 'BaseLockException':
        """Shallow-copy the exception, keeping the filehandle.

        `copy.copy` falls back to ``__reduce_ex__`` when no ``__copy__``
        is defined, and the pickle reduction above deliberately drops
        `fh`. That is right for pickling and for `copy.deepcopy`, where
        the handle would have to be serialised or duplicated, but a
        shallow copy stays inside the process where the handle is still
        perfectly usable, so dropping it there would lose information
        for no safety gain. This override keeps `fh` shared between the
        original and the copy, which is exactly what a shallow copy
        means.

        Returns:
            A new instance of the same class with the same ``args`` and
            a shallow copy of the instance attributes, `fh` included.
            ``__init__`` is bypassed, so copying a deprecated subclass
            does not re-emit its construction warning.
        """
        new_exception: BaseLockException = self.__class__.__new__(
            self.__class__
        )
        new_exception.args = self.args
        new_exception.__dict__.update(self.__dict__)
        return new_exception


class LockException(BaseLockException):
    """Raised when acquiring or releasing a lock fails.

    This is the general-purpose locking failure and the type to catch
    when any locking error is acceptable to handle uniformly:
    `AlreadyLocked` derives from it, so `except LockException` also
    catches contention (as does the deprecated, never-raised
    `FileToLarge`).

    .. versionchanged:: 4.0.0
        On POSIX, lock failures now populate `strerror` and pass the
        OS error message as the second positional argument, matching the
        contract this module already followed on Windows. The first
        positional argument on POSIX stays the original `OSError`, where
        Windows passes the `LOCK_FAILED` code. Previously, `str(exc)` on
        POSIX returned the bare underlying `OSError` text; it now
        returns the 2-argument exception repr instead, for example
        ``(BlockingIOError(11, 'Resource temporarily unavailable'),
        '[Errno 11] Resource temporarily unavailable')``. Code that
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


class LockLostError(LockException):
    """Raised when a lock that was successfully acquired is lost again.

    `portalocker.RedisLock` keeps its lock in a live pubsub
    subscription, so the lock can be revoked from outside the holding
    process: a severed network connection, a ``CLIENT KILL`` issued by
    an administrator, or a reap by another contender that saw this
    holder stop answering pings. This exception is how that revocation
    reaches the code that thought it still held the lock. It is raised
    by `portalocker.RedisLock.ensure_held` and by the ``with`` block
    exit of a lock that was lost while the block ran; the underlying
    cause (usually a ``redis.exceptions.ConnectionError``) is attached
    as ``__cause__``.

    Beyond the `LockException` payload, instances carry `channel` and
    `holder_id` so a handler that manages several locks can tell which
    one died.

    Example:
        >>> from portalocker import exceptions
        >>> error = exceptions.LockLostError(
        ...     exceptions.LockException.LOCK_FAILED,
        ...     'lock lost',
        ...     channel='jobs',
        ...     holder_id='a1b2',
        ... )
        >>> error.channel, error.holder_id
        ('jobs', 'a1b2')

    .. versionadded:: 4.2.0
    """

    channel: str | None = None
    """The pubsub channel the lost lock lived on, when known."""

    holder_id: str | None = None
    """The `RedisLock.holder_id` of the holder that lost the lock."""

    def __init__(
        self,
        *args: typing.Any,
        channel: str | None = None,
        holder_id: str | None = None,
        **kwargs: typing.Any,
    ) -> None:
        """Initialise like `LockException`, plus record the lock identity.

        Args:
            *args: Forwarded to `LockException.__init__`.
            channel: The pubsub channel the lost lock lived on. Stored
                on `self.channel`.
            holder_id: The holder id of the lock instance that lost the
                lock. Stored on `self.holder_id`.
            **kwargs: Forwarded to `LockException.__init__`.
        """
        super().__init__(*args, **kwargs)
        self.channel = channel
        self.holder_id = holder_id


class FileToLarge(LockException):
    """Deprecated and never raised; kept only for backwards compatibility.

    No version of this package has ever raised it: it was defined for a
    file-too-large failure mode that the locking backends never
    reported. Code catching it catches nothing, so it is deprecated and
    instantiating it emits a `DeprecationWarning`. Catch `LockException`
    instead.

    The misspelling in the name (`FileToLarge`, rather than
    `FileTooLarge`) is a long-standing typo in the public API. It is kept
    exactly as-is instead of being silently renamed, which would break
    `except FileToLarge` in existing code.

    .. deprecated:: 4.2.0
        Will be removed in a future major release.
    """

    def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        """Initialise like `LockException`, plus warn about deprecation.

        Args:
            *args: Forwarded to `LockException.__init__`.
            **kwargs: Forwarded to `LockException.__init__`.

        Warns:
            DeprecationWarning: Always. The warning points at the caller
                (``stacklevel=2``) so the deprecated construction site
                shows up in the report, not this initialiser.
        """
        warnings.warn(
            'FileToLarge is deprecated: portalocker has never raised it '
            'and it will be removed in a future major release. Catch '
            'LockException instead.',
            DeprecationWarning,
            stacklevel=2,
        )
        super().__init__(*args, **kwargs)
