import typing

from . import types


class BaseLockException(Exception):  # noqa: N818
    # Error codes:
    LOCK_FAILED: typing.Final = 1

    strerror: str | None = None  # ensure attribute always exists

    def __init__(
        self,
        *args: typing.Any,
        fh: types.IO | None | int | types.HasFileno = None,
        **kwargs: typing.Any,
    ) -> None:
        self.fh = fh
        self.strerror = (
            str(args[1])
            if len(args) > 1 and isinstance(args[1], str)
            else None
        )
        Exception.__init__(self, *args)


class LockException(BaseLockException):
    pass


class AlreadyLocked(LockException):
    holder_pid: int | None = None

    def __init__(
        self,
        *args: typing.Any,
        holder_pid: int | None = None,
        **kwargs: typing.Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.holder_pid = holder_pid


class FileToLarge(LockException):
    pass
