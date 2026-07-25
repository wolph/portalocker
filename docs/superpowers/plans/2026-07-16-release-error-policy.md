# Release Error Policy Implementation Plan

**For agentic workers:** REQUIRED SUB-SKILL: Use
superpowers:subagent-driven-development (recommended) or
superpowers:executing-plans to implement this plan task-by-task. Steps use
checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add opt-in reporting of `Lock.release()` failures without changing
the default behavior shipped in portalocker 3.3.0.

**Architecture:** `Lock` stores a keyword-only release-error policy. Its
release path always attempts unlock and close, then either suppresses collected
ordinary exceptions or raises them according to that policy. A `Lock`-specific
context exit path preserves protected-body exceptions only when strict release
handling was explicitly enabled, leaving `LockBase` and subclass defaults
unchanged.

**Tech Stack:** Python 3.10+, pytest, uv, tox, Sphinx/reStructuredText.

---

### Task 1: Fault-injection coverage for direct release

**Files:**

- Create: `portalocker_tests/test_release_errors.py`
- Modify: `portalocker/utils.py:197-349`

- [ ] **Step 1: Write the direct-release tests**

Create `portalocker_tests/test_release_errors.py` with a small real fake handle
that records close attempts and optionally raises a supplied exception:

```python
from __future__ import annotations

import typing

import pytest

from portalocker import types, utils


class ReleaseHandle:
    def __init__(
        self,
        events: list[str],
        close_error: Exception | None = None,
    ) -> None:
        self.events = events
        self.close_error = close_error

    def close(self) -> None:
        self.events.append('close')
        if self.close_error is not None:
            raise self.close_error


def make_lock(
    handle: ReleaseHandle,
    *,
    raise_on_release_error: bool = False,
) -> utils.Lock:
    lock = utils.Lock(
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

    monkeypatch.setattr(utils.portalocker, 'unlock', unlock)


def test_release_suppresses_errors_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    unlock_error = OSError('unlock failed')
    close_error = OSError('close failed')
    handle = ReleaseHandle(events, close_error)
    lock = make_lock(handle)
    set_unlock(monkeypatch, events, unlock_error)

    lock.release()

    assert events == ['unlock', 'close']
    assert lock.fh is None


def test_strict_release_raises_unlock_error_after_closing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    unlock_error = OSError('unlock failed')
    handle = ReleaseHandle(events)
    lock = make_lock(handle, raise_on_release_error=True)
    set_unlock(monkeypatch, events, unlock_error)

    with pytest.raises(OSError) as exc_info:
        lock.release()

    assert exc_info.value is unlock_error
    assert events == ['unlock', 'close']
    assert lock.fh is None


def test_strict_release_raises_close_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    close_error = OSError('close failed')
    handle = ReleaseHandle(events, close_error)
    lock = make_lock(handle, raise_on_release_error=True)
    set_unlock(monkeypatch, events)

    with pytest.raises(OSError) as exc_info:
        lock.release()

    assert exc_info.value is close_error
    assert events == ['unlock', 'close']
    assert lock.fh is None


def test_strict_release_chains_close_error_to_unlock_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    unlock_error = OSError('unlock failed')
    close_error = OSError('close failed')
    handle = ReleaseHandle(events, close_error)
    lock = make_lock(handle, raise_on_release_error=True)
    set_unlock(monkeypatch, events, unlock_error)

    with pytest.raises(OSError) as exc_info:
        lock.release()

    assert exc_info.value is unlock_error
    assert exc_info.value.__cause__ is close_error
    assert events == ['unlock', 'close']
    assert lock.fh is None
```

- [ ] **Step 2: Run the strict tests and verify RED**

Run:

```bash
uv run pytest portalocker_tests/test_release_errors.py -q --no-cov
```

Expected: the default-compatibility test passes, while strict tests fail because
release errors are still suppressed.

- [ ] **Step 3: Add the policy and direct-release implementation**

In `portalocker/utils.py`, document and store the new keyword-only argument:

```diff
 class Lock(LockBase[typing.IO[typing.Any]]):
+    raise_on_release_error: bool

     def __init__(
         self,
         filename: Filename,
         mode: Mode = 'a',
         timeout: float | None = None,
         check_interval: float = DEFAULT_CHECK_INTERVAL,
         fail_when_locked: bool = DEFAULT_FAIL_WHEN_LOCKED,
         flags: constants.LockFlags = LOCK_METHOD,
+        *,
+        raise_on_release_error: bool = False,
         **file_open_kwargs: typing.Any,
     ) -> None:
         self.fh = None
         self.filename = str(filename)
         self.mode = mode
         self.truncate = truncate
         self.flags = flags
+        self.raise_on_release_error = raise_on_release_error
         self.file_open_kwargs = file_open_kwargs
```

Replace `Lock.release()` with collection logic that preserves current
`BaseException` behavior:

```python
    def release(self) -> None:
        """Release the currently locked file handle."""
        fh = self.fh
        if fh:
            release_errors: list[Exception] = []
            try:
                try:
                    portalocker.unlock(fh)
                except Exception as exception:
                    release_errors.append(exception)
            finally:
                try:
                    fh.close()
                except Exception as exception:
                    release_errors.append(exception)
                self.fh = None

            if self.raise_on_release_error and release_errors:
                primary_error = release_errors[0]
                if len(release_errors) > 1:
                    raise primary_error from release_errors[1]
                raise primary_error
```

Add `raise_on_release_error` to the class docstring, explaining that `False`
preserves best-effort suppression and `True` raises only after unlock and close
have both been attempted.

- [ ] **Step 4: Run the direct-release tests and verify GREEN**

Run:

```bash
uv run pytest portalocker_tests/test_release_errors.py -q --no-cov
```

Expected: 4 passed.

- [ ] **Step 5: Commit the direct-release behavior**

```bash
git add portalocker/utils.py portalocker_tests/test_release_errors.py
git commit -m "add opt-in release error reporting"
```

### Task 2: Preserve protected-body exceptions in strict contexts

**Files:**

- Modify: `portalocker_tests/test_release_errors.py`
- Modify: `portalocker/utils.py:334-349`

- [ ] **Step 1: Add strict context-manager tests**

Append:

```python
def test_strict_context_exit_raises_release_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    unlock_error = OSError('unlock failed')
    handle = ReleaseHandle(events)
    lock = make_lock(handle, raise_on_release_error=True)
    set_unlock(monkeypatch, events, unlock_error)

    with pytest.raises(OSError) as exc_info:
        with lock:
            pass

    assert exc_info.value is unlock_error
    assert events == ['unlock', 'close']


def test_strict_context_preserves_body_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    body_error = ValueError('body failed')
    unlock_error = OSError('unlock failed')
    handle = ReleaseHandle(events)
    lock = make_lock(handle, raise_on_release_error=True)
    set_unlock(monkeypatch, events, unlock_error)

    with pytest.raises(ValueError) as exc_info:
        with lock:
            raise body_error

    assert exc_info.value is body_error
    assert exc_info.value.__context__ is unlock_error
    assert events == ['unlock', 'close']
```

- [ ] **Step 2: Run the body-error test and verify RED**

Run:

```bash
uv run pytest \
  portalocker_tests/test_release_errors.py::test_strict_context_preserves_body_error \
  -q --no-cov
```

Expected: FAIL because the release error replaces the protected-body error.

- [ ] **Step 3: Override `Lock.__exit__()` only for explicit strict mode**

Add after `Lock.__enter__()`:

```python
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: typing.Any,
    ) -> bool | None:
        if not self.raise_on_release_error or exc_value is None:
            self.release()
            return None

        try:
            self.release()
        except Exception as release_error:
            previous_context = exc_value.__context__
            release_error.__context__ = previous_context
            exc_value.__context__ = release_error
            with contextlib.suppress(Exception):
                exc_value.add_note(  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
                    'portalocker release failed; see exception context',
                )
        return None
```

This branch is unreachable under default settings. It also avoids changing
`LockBase.__exit__()` and the cleanup semantics of other lock types.

- [ ] **Step 4: Run focused and core locking tests**

Run:

```bash
uv run pytest \
  portalocker_tests/test_release_errors.py \
  portalocker_tests/test_core_locking.py \
  portalocker_tests/test_rlock_behaviour.py \
  portalocker_tests/test_temporary_file_lock.py \
  -q --no-cov
```

Expected: all selected tests pass. Full coverage enforcement runs in Task 3.

- [ ] **Step 5: Commit context behavior**

```bash
git add portalocker/utils.py portalocker_tests/test_release_errors.py
git commit -m "preserve body errors during strict release"
```

### Task 3: Document and verify the compatibility policy

**Files:**

- Modify: `CHANGELOG.rst:46-47`
- Verify: `portalocker/utils.py`
- Verify: `portalocker_tests/test_release_errors.py`

- [ ] **Step 1: Extend the 4.0 changelog entry**

Change the existing release bullet to:

```rst
 * ``Lock.release()`` continues suppressing unlock and close errors by default;
   closing is always attempted and the file handle reference is cleared.
   Callers can opt into reporting cleanup failures with
   ``Lock(..., raise_on_release_error=True)`` (#117)
```

- [ ] **Step 2: Run the complete project verification**

Run:

```bash
uv run pytest
uv run tox -p auto
```

Expected: all tests, type checkers, lint/format checks, spelling checks,
package checks, and documentation builds pass.

- [ ] **Step 3: Inspect the final diff for scope and compatibility**

Run:

```bash
git diff --check HEAD~2
git diff --stat HEAD~2
git status --short
```

Confirm only `portalocker/utils.py`, the focused test module, and
`CHANGELOG.rst` contain implementation changes. Confirm the worktree is clean
apart from the ignored environment/lock artifacts.

- [ ] **Step 4: Commit the documentation**

```bash
git add CHANGELOG.rst
git commit -m "document strict release error handling"
```
