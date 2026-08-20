from __future__ import annotations

import logging
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


class BrokenReprError(OSError):
    def __repr__(self) -> str:
        raise RuntimeError('repr failed')


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
    if hasattr(exc_info.value, 'add_note'):
        assert getattr(exc_info.value, '__notes__', []) == [
            'portalocker release failed; see exception context',
        ]
    else:
        assert not hasattr(exc_info.value, '__notes__')
    assert events == ['unlock', 'close']


def test_strict_context_preserves_body_when_release_repr_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    body_error: ValueError = ValueError('body failed')
    unlock_error: BrokenReprError = BrokenReprError('unlock failed')
    handle: ReleaseHandle = ReleaseHandle(events)
    lock: utils.Lock = make_lock(handle, raise_on_release_error=True)
    set_unlock(monkeypatch, events, unlock_error)

    exc_info: pytest.ExceptionInfo[ValueError]
    with pytest.raises(ValueError) as exc_info, lock:
        raise body_error

    assert exc_info.value is body_error
    assert exc_info.value.__context__ is unlock_error


def test_strict_context_preserves_body_when_notes_are_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    body_error: ValueError = ValueError('body failed')
    body_error.__dict__['__notes__'] = 'invalid'
    unlock_error: OSError = OSError('unlock failed')
    handle: ReleaseHandle = ReleaseHandle(events)
    lock: utils.Lock = make_lock(handle, raise_on_release_error=True)
    set_unlock(monkeypatch, events, unlock_error)

    exc_info: pytest.ExceptionInfo[ValueError]
    with pytest.raises(ValueError) as exc_info, lock:
        raise body_error

    assert exc_info.value is body_error
    assert exc_info.value.__context__ is unlock_error


def test_repeated_release_is_noop_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    handle: ReleaseHandle = ReleaseHandle(events)
    lock: utils.Lock = make_lock(handle, raise_on_release_error=True)
    set_unlock(monkeypatch, events)

    lock.release()
    lock.release()

    assert events == ['unlock', 'close']


def test_repeated_release_is_noop_after_strict_failure(
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
    lock.release()

    assert exc_info.value is unlock_error
    assert events == ['unlock', 'close']


def test_strict_context_retains_unlock_and_close_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    body_error: ValueError = ValueError('body failed')
    unlock_error: OSError = OSError('unlock failed')
    close_error: OSError = OSError('close failed')
    handle: ReleaseHandle = ReleaseHandle(events, close_error)
    lock: utils.Lock = make_lock(handle, raise_on_release_error=True)
    set_unlock(monkeypatch, events, unlock_error)

    exc_info: pytest.ExceptionInfo[ValueError]
    with pytest.raises(ValueError) as exc_info, lock:
        raise body_error

    assert exc_info.value is body_error
    assert exc_info.value.__context__ is unlock_error
    assert unlock_error.__cause__ is close_error
    assert events == ['unlock', 'close']


def test_release_logs_suppressed_errors_by_default(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []
    unlock_error: OSError = OSError('unlock failed')
    close_error: OSError = OSError('close failed')
    handle: ReleaseHandle = ReleaseHandle(events, close_error)
    lock: utils.Lock = make_lock(handle)
    set_unlock(monkeypatch, events, unlock_error)

    with caplog.at_level(logging.WARNING, logger='portalocker.utils'):
        lock.release()

    messages: list[str] = [record.getMessage() for record in caplog.records]
    assert len(messages) == 2, 'expected one warning per suppressed error'
    assert all('suppressed error' in message for message in messages)
    assert events == ['unlock', 'close']
    assert lock.fh is None


def test_default_context_preserves_body_error_from_misbehaving_release() -> (
    None
):
    """A subclass whose `release` raises despite the default
    ``raise_on_release_error=False`` still cannot mask the body error.
    """
    release_error: OSError = OSError('release failed')

    class MisbehavingLock(utils.Lock):
        def release(self) -> None:
            raise release_error

    lock: MisbehavingLock = MisbehavingLock('unused.lock')
    lock.fh = typing.cast(types.IO, ReleaseHandle([]))
    body_error: ValueError = ValueError('body failed')

    exc_info: pytest.ExceptionInfo[ValueError]
    with pytest.raises(ValueError) as exc_info, lock:
        raise body_error

    assert exc_info.value is body_error
    assert exc_info.value.__context__ is release_error
