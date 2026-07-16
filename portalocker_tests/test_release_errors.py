from __future__ import annotations

import typing

import pytest

import portalocker.portalocker as portalocker_module
from portalocker import types, utils


class ReleaseHandle:
    def __init__(
        self,
        events: list[str],
        close_error: Exception | None = None,
    ) -> None:
        self.events: list[str] = events
        self.close_error: Exception | None = close_error

    def close(self) -> None:
        self.events.append('close')
        if self.close_error is not None:
            raise self.close_error


def make_lock(
    handle: ReleaseHandle,
    *,
    raise_on_release_error: bool = False,
) -> utils.Lock:
    lock: utils.Lock = utils.Lock(
        'unused.lock',
        raise_on_release_error=raise_on_release_error,
    )
    lock.fh = typing.cast(types.IO, handle)
    return lock


def set_unlock(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    error: Exception | None = None,
) -> None:
    def unlock(_fh: types.IO) -> None:
        events.append('unlock')
        if error is not None:
            raise error

    monkeypatch.setattr(portalocker_module, 'unlock', unlock)


def test_release_suppresses_errors_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    unlock_error: OSError = OSError('unlock failed')
    close_error: OSError = OSError('close failed')
    handle: ReleaseHandle = ReleaseHandle(events, close_error)
    lock: utils.Lock = make_lock(handle)
    set_unlock(monkeypatch, events, unlock_error)

    lock.release()

    assert events == ['unlock', 'close']
    assert lock.fh is None


def test_strict_release_raises_unlock_error_after_closing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    unlock_error: OSError = OSError('unlock failed')
    handle: ReleaseHandle = ReleaseHandle(events)
    lock: utils.Lock = make_lock(handle, raise_on_release_error=True)
    set_unlock(monkeypatch, events, unlock_error)

    exc_info: pytest.ExceptionInfo[OSError]
    with pytest.raises(OSError) as exc_info:
        lock.release()

    assert exc_info.value is unlock_error
    assert events == ['unlock', 'close']
    assert lock.fh is None


def test_strict_release_raises_close_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    close_error: OSError = OSError('close failed')
    handle: ReleaseHandle = ReleaseHandle(events, close_error)
    lock: utils.Lock = make_lock(handle, raise_on_release_error=True)
    set_unlock(monkeypatch, events)

    exc_info: pytest.ExceptionInfo[OSError]
    with pytest.raises(OSError) as exc_info:
        lock.release()

    assert exc_info.value is close_error
    assert events == ['unlock', 'close']
    assert lock.fh is None


def test_strict_release_chains_close_error_to_unlock_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    unlock_error: OSError = OSError('unlock failed')
    close_error: OSError = OSError('close failed')
    handle: ReleaseHandle = ReleaseHandle(events, close_error)
    lock: utils.Lock = make_lock(handle, raise_on_release_error=True)
    set_unlock(monkeypatch, events, unlock_error)

    exc_info: pytest.ExceptionInfo[OSError]
    with pytest.raises(OSError) as exc_info:
        lock.release()

    assert exc_info.value is unlock_error
    assert exc_info.value.__cause__ is close_error
    assert events == ['unlock', 'close']
    assert lock.fh is None


def test_strict_context_exit_raises_release_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    unlock_error: OSError = OSError('unlock failed')
    handle: ReleaseHandle = ReleaseHandle(events)
    lock: utils.Lock = make_lock(handle, raise_on_release_error=True)
    set_unlock(monkeypatch, events, unlock_error)

    exc_info: pytest.ExceptionInfo[OSError]
    with pytest.raises(OSError) as exc_info, lock:
        pass

    assert exc_info.value is unlock_error
    assert events == ['unlock', 'close']


def test_strict_context_preserves_body_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    body_error: ValueError = ValueError('body failed')
    unlock_error: OSError = OSError('unlock failed')
    handle: ReleaseHandle = ReleaseHandle(events)
    lock: utils.Lock = make_lock(handle, raise_on_release_error=True)
    set_unlock(monkeypatch, events, unlock_error)

    exc_info: pytest.ExceptionInfo[ValueError]
    with pytest.raises(ValueError) as exc_info, lock:
        raise body_error

    assert exc_info.value is body_error
    assert exc_info.value.__context__ is unlock_error
    exception_notes: list[str] = typing.cast(
        list[str],
        exc_info.value.__dict__['__notes__'],
    )
    assert exception_notes == [
        "portalocker release failed: OSError('unlock failed')",
    ]
    assert events == ['unlock', 'close']
