# PidFileLock Fail-Closed Context Implementation Plan

**For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in `PidFileLock.fail_closed()` context that enters only after ownership and reports a competing PID on `AlreadyLocked`.

**Architecture:** Keep the existing inspection context untouched. `PidFileLock.fail_closed()` returns a private `AbstractContextManager[None]` adapter that delegates acquisition and release to the existing lock. `AlreadyLocked` carries an optional `holder_pid` populated by the adapter when contention occurs.

**Tech Stack:** Python 3.10+, `contextlib`, pytest, mypy, tox, Sphinx

---

### Task 1: Successful fail-closed context and static context types

**Files:**
- Modify: `portalocker_tests/test_pidfilelock.py`
- Modify: `portalocker/utils.py:695-734`

- [ ] **Step 1: Write the failing runtime and static type tests**

Add `contextlib` to the test imports, then add this static type contract and runtime test after the existing context-manager tests:

```python
def _pidfilelock_context_types(
    lock: utils.PidFileLock,
) -> tuple[
    contextlib.AbstractContextManager[int | None],
    contextlib.AbstractContextManager[None],
]:
    return lock, lock.fail_closed()


def test_pidfilelock_fail_closed_context_manager_success(
    tmp_path: Path,
) -> None:
    pid_file: Path = tmp_path / 'fail_closed_success.pid'
    lock: utils.PidFileLock = utils.PidFileLock(str(pid_file))

    with lock.fail_closed():
        assert lock._acquired_lock
        assert lock.read_pid() == os.getpid()

    assert not lock._acquired_lock
    assert not pid_file.exists()
    assert not Path(f'{pid_file}.lock').exists()
```

- [ ] **Step 2: Run the tests and type checker to verify RED**

Run:

```bash
uv run pytest portalocker_tests/test_pidfilelock.py::test_pidfilelock_fail_closed_context_manager_success -q --no-cov
uv run mypy portalocker portalocker_tests
```

Expected: pytest fails with `AttributeError: 'PidFileLock' object has no attribute 'fail_closed'`; mypy reports the same missing attribute.

- [ ] **Step 3: Implement the minimal successful adapter**

Add this factory to `PidFileLock` after `read_pid()`:

```python
    def fail_closed(self) -> contextlib.AbstractContextManager[None]:
        """Return a context that enters only after acquiring this lock."""
        return _PidFileLockFailClosedContext(self)
```

Add the private adapter immediately after `PidFileLock`:

```python
class _PidFileLockFailClosedContext(
    contextlib.AbstractContextManager[None],
):
    """Fail-closed context adapter for :class:`PidFileLock`."""

    def __init__(self, lock: PidFileLock) -> None:
        self._lock: PidFileLock = lock

    def __enter__(self) -> None:
        self._lock.acquire()
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: typing.Any,
    ) -> bool | None:
        return self._lock.__exit__(exc_type, exc_value, traceback)
```

- [ ] **Step 4: Run the focused test and type checker to verify GREEN**

Run:

```bash
uv run pytest portalocker_tests/test_pidfilelock.py::test_pidfilelock_fail_closed_context_manager_success -q --no-cov
uv run mypy portalocker portalocker_tests
```

Expected: the focused test passes and mypy exits 0.

- [ ] **Step 5: Commit**

```bash
git add portalocker/utils.py portalocker_tests/test_pidfilelock.py
git commit -m "add fail-closed pidfile lock context"
```

### Task 2: Structured holder PID with unavailable PID content

**Files:**
- Modify: `portalocker_tests/test_pidfilelock.py`
- Modify: `portalocker/exceptions.py:31-32`

- [ ] **Step 1: Write the failing unavailable-PID test**

```python
def test_pidfilelock_fail_closed_missing_holder_pid(
    tmp_path: Path,
) -> None:
    pid_file: Path = tmp_path / 'fail_closed_missing_pid.pid'
    holder: utils.PidFileLock = utils.PidFileLock(str(pid_file))
    holder.acquire()
    pid_file.unlink()

    body_entered: bool = False
    try:
        contender: utils.PidFileLock = utils.PidFileLock(str(pid_file))
        exc_info: pytest.ExceptionInfo[portalocker.AlreadyLocked]
        with pytest.raises(portalocker.AlreadyLocked) as exc_info:
            with contender.fail_closed():
                body_entered = True

        assert not body_entered
        assert exc_info.value.holder_pid is None
    finally:
        holder.release()
```

- [ ] **Step 2: Run the test to verify RED**

Run:

```bash
uv run pytest portalocker_tests/test_pidfilelock.py::test_pidfilelock_fail_closed_missing_holder_pid -q --no-cov
```

Expected: fails because `AlreadyLocked` has no `holder_pid` attribute.

- [ ] **Step 3: Add the backward-compatible exception attribute**

Replace the empty `AlreadyLocked` class with:

```python
class AlreadyLocked(LockException):
    holder_pid: int | None

    def __init__(
        self,
        *args: typing.Any,
        holder_pid: int | None = None,
        **kwargs: typing.Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.holder_pid = holder_pid
```

- [ ] **Step 4: Run the focused test to verify GREEN**

Run:

```bash
uv run pytest portalocker_tests/test_pidfilelock.py::test_pidfilelock_fail_closed_missing_holder_pid -q --no-cov
```

Expected: passes; the body remains unexecuted and `holder_pid` is `None`.

- [ ] **Step 5: Commit**

```bash
git add portalocker/exceptions.py portalocker_tests/test_pidfilelock.py
git commit -m "add holder pid to already locked errors"
```

### Task 3: Populate the competing PID on fail-closed contention

**Files:**
- Modify: `portalocker_tests/test_pidfilelock.py`
- Modify: `portalocker/utils.py`

- [ ] **Step 1: Write the failing readable-PID contention test**

```python
def test_pidfilelock_fail_closed_reports_holder_pid(
    tmp_path: Path,
) -> None:
    pid_file: Path = tmp_path / 'fail_closed_holder_pid.pid'
    holder: utils.PidFileLock = utils.PidFileLock(str(pid_file))
    holder.acquire()

    body_entered: bool = False
    try:
        contender: utils.PidFileLock = utils.PidFileLock(str(pid_file))
        exc_info: pytest.ExceptionInfo[portalocker.AlreadyLocked]
        with pytest.raises(portalocker.AlreadyLocked) as exc_info:
            with contender.fail_closed():
                body_entered = True

        assert not body_entered
        assert exc_info.value.holder_pid == os.getpid()
    finally:
        holder.release()
```

- [ ] **Step 2: Run the test to verify RED**

Run:

```bash
uv run pytest portalocker_tests/test_pidfilelock.py::test_pidfilelock_fail_closed_reports_holder_pid -q --no-cov
```

Expected: fails because `holder_pid` remains `None`.

- [ ] **Step 3: Enrich and re-raise the same contention exception**

Replace the adapter's `__enter__` body with:

```python
    def __enter__(self) -> None:
        try:
            self._lock.acquire()
        except exceptions.AlreadyLocked as exc:
            exc.holder_pid = self._lock.read_pid()
            raise
        return None
```

- [ ] **Step 4: Run all fail-closed tests to verify GREEN**

Run:

```bash
uv run pytest portalocker_tests/test_pidfilelock.py -q --no-cov
```

Expected: all `PidFileLock` tests pass.

- [ ] **Step 5: Commit**

```bash
git add portalocker/utils.py portalocker_tests/test_pidfilelock.py
git commit -m "report holder pid on fail-closed contention"
```

### Task 4: Document inspection and fail-closed contexts

**Files:**
- Modify: `README.rst`
- Modify: `CHANGELOG.rst`

- [ ] **Step 1: Add the README examples**

Add a `PidFileLock contexts` section before `Redis Locks` showing both APIs:

```rst
PidFileLock contexts
--------------------

The default context is inspection-oriented: it enters even when another
process owns the lock and returns that process's PID. A ``None`` value means
this process acquired the lock:

.. code-block:: python

    with portalocker.PidFileLock('worker.pid') as holder_pid:
        if holder_pid is None:
            run_singleton_worker()
        else:
            print(f'worker already running as PID {holder_pid}')

Use ``fail_closed()`` when the protected body must only run after acquisition:

.. code-block:: python

    try:
        with portalocker.PidFileLock('worker.pid').fail_closed():
            run_singleton_worker()
    except portalocker.AlreadyLocked as exc:
        print(f'worker already running as PID {exc.holder_pid}')
```

- [ ] **Step 2: Add the changelog entry**

Add this bullet in the 4.0.0 section after the existing `PidFileLock` addition:

```rst
* Added ``PidFileLock.fail_closed()`` for ownership-only contexts; contention
  raises ``AlreadyLocked`` before entering the body and exposes the competing
  PID through ``AlreadyLocked.holder_pid`` when readable (#118)
```

- [ ] **Step 3: Build the documentation**

Run:

```bash
uv run tox -e docs
```

Expected: Sphinx exits 0 with warnings treated as errors.

- [ ] **Step 4: Commit**

```bash
git add README.rst CHANGELOG.rst
git commit -m "document fail-closed pidfile locks"
```

### Task 5: Full verification and diff audit

**Files:**
- Verify: all changed files

- [ ] **Step 1: Run the complete test suite**

```bash
uv run pytest
```

Expected: all tests pass with 100% branch coverage.

- [ ] **Step 2: Run lint, static analysis, metadata, spelling, and docs checks**

```bash
uv run tox -e ruff,mypy,basedpyright,pyrefly,ty,codespell,repo-review,docs
```

Expected: every tox environment exits 0.

- [ ] **Step 3: Build both distributions**

```bash
uv build
```

Expected: source distribution and wheel build successfully.

- [ ] **Step 4: Audit the final branch**

```bash
git diff --check feature/modernize-4.0.0...HEAD
git status --short --branch
git log --oneline feature/modernize-4.0.0..HEAD
```

Expected: no whitespace errors, no uncommitted files, and only the design,
implementation, tests, and documentation commits for #118.
