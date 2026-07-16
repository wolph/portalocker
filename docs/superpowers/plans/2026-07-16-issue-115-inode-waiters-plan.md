# Issue 115 Inode Waiters Implementation Plan

**For agentic workers:** REQUIRED SUB-SKILL: Use
superpowers:subagent-driven-development (recommended) or
superpowers:executing-plans to implement this plan task-by-task. Steps use
checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove that POSIX temporary-file and PID-file waiters cannot return
while holding an obsolete inode, while preserving portalocker 3.3.0 file
lifecycle behavior.

**Architecture:** Keep the existing post-acquisition inode validation in
`TemporaryFileLock._acquire_verified()`. Add an event-driven multiprocessing
regression that forces a waiter to open the holder's inode before release,
then verifies a third actor cannot acquire and the waiter owns the inode named
by the current path. Cover `TemporaryFileLock` and the `PidFileLock` sidecar
through one parameterized test; change production code only if this test
exposes a remaining violation.

**Tech Stack:** Python 3.10+, `multiprocessing`, pytest, portalocker's existing
`Lock`/`TemporaryFileLock`/`PidFileLock` APIs, uv, ruff, mypy, basedpyright,
pyrefly, ty, and codespell.

---

### Task 1: Add the split-inode multiprocessing regression

**Files:**

- Modify: `portalocker_tests/test_multiprocess.py`
- Inspect only unless the regression exposes a defect: `portalocker/utils.py`

- [ ] **Step 1: Add typed, spawn-safe test helpers and the parameterized regression**

Add imports and helpers to `portalocker_tests/test_multiprocess.py`:

```python
import multiprocessing.context
import multiprocessing.queues
import pathlib
import typing
from unittest import mock

from portalocker import LockFlags, utils

LockKind: typing.TypeAlias = typing.Literal['temporary', 'pid']


def make_temporary_lock(
    filename: str,
    lock_kind: LockKind,
    *,
    timeout: float,
    fail_when_locked: bool,
) -> portalocker.TemporaryFileLock:
    """Create one of the temporary-path lock implementations under test."""
    lock_type: type[portalocker.TemporaryFileLock]
    if lock_kind == 'temporary':
        lock_type = portalocker.TemporaryFileLock
    else:
        lock_type = portalocker.PidFileLock
    return lock_type(
        filename,
        timeout=timeout,
        fail_when_locked=fail_when_locked,
    )


def native_lock_path(filename: str, lock_kind: LockKind) -> str:
    """Return the pathname carrying the native OS lock."""
    if lock_kind == 'pid':
        return f'{filename}.lock'
    return filename


def hold_inode_waiter(
    filename: str,
    lock_kind: LockKind,
    native_filename: str,
    opened_event: multiprocessing.synchronize.Event,
    acquired_event: multiprocessing.synchronize.Event,
    release_event: multiprocessing.synchronize.Event,
    inode_queue: multiprocessing.queues.Queue,
) -> None:
    """Open the original inode, acquire, report it, and hold the lock."""
    original_get_fh: typing.Callable[
        [utils.Lock],
        typing.IO[typing.Any],
    ] = typing.cast(
        typing.Callable[[utils.Lock], typing.IO[typing.Any]],
        utils.Lock._get_fh,
    )

    def announce_open(lock: utils.Lock) -> typing.IO[typing.Any]:
        fh: typing.IO[typing.Any] = original_get_fh(lock)
        if lock.filename == native_filename:
            opened_event.set()
        return fh

    lock: portalocker.TemporaryFileLock = make_temporary_lock(
        filename,
        lock_kind,
        timeout=30,
        fail_when_locked=False,
    )
    try:
        with mock.patch.object(utils.Lock, '_get_fh', announce_open):
            fh: typing.IO[typing.Any] = lock.acquire()
        inode_queue.put(os.fstat(fh.fileno()).st_ino)
        acquired_event.set()
        if not release_event.wait(timeout=30):
            raise TimeoutError('parent did not release the inode waiter')
    finally:
        lock.release()
```

Add the regression after the existing multiprocessing tests:

```python
@pytest.mark.parametrize('lock_kind', ['temporary', 'pid'])
@pytest.mark.skipif(
    os.name == 'nt',
    reason='POSIX-only inode replacement race',
)
@pytest.mark.skipif(
    'pypy' in platform.python_implementation().lower(),
    reason='pypy3 does not support the multiprocessing test',
)
def test_temporary_lock_waiters_converge_on_current_inode(
    tmp_path: pathlib.Path,
    lock_kind: LockKind,
) -> None:
    """#115: waiters must reject an obsolete unlinked lock inode."""
    context: multiprocessing.context.BaseContext = multiprocessing.get_context(
        'spawn'
    )
    filename: str = str(tmp_path / f'{lock_kind}.lock')
    native_filename: str = native_lock_path(filename, lock_kind)
    holder: portalocker.TemporaryFileLock = make_temporary_lock(
        filename,
        lock_kind,
        timeout=30,
        fail_when_locked=False,
    )
    opened_event: multiprocessing.synchronize.Event = context.Event()
    acquired_event: multiprocessing.synchronize.Event = context.Event()
    release_event: multiprocessing.synchronize.Event = context.Event()
    inode_queue: multiprocessing.queues.Queue = context.Queue()
    waiter: multiprocessing.Process = context.Process(
        target=hold_inode_waiter,
        args=(
            filename,
            lock_kind,
            native_filename,
            opened_event,
            acquired_event,
            release_event,
            inode_queue,
        ),
    )
    holder_released: bool = False

    holder.acquire()
    waiter.start()
    try:
        assert opened_event.wait(timeout=30), (
            'waiter never opened the holder inode'
        )
        holder.release()
        holder_released = True
        assert acquired_event.wait(timeout=30), 'waiter never acquired'

        waiter_inode: int = inode_queue.get(timeout=30)
        contender: portalocker.TemporaryFileLock = make_temporary_lock(
            filename,
            lock_kind,
            timeout=0,
            fail_when_locked=True,
        )
        try:
            with pytest.raises(portalocker.AlreadyLocked):
                contender.acquire()
        finally:
            contender.release()

        current_inode: int = os.stat(native_filename).st_ino
        assert waiter_inode == current_inode
    finally:
        if not holder_released:
            holder.release()
        release_event.set()
        waiter.join(timeout=30)
        if waiter.is_alive():
            waiter.terminate()
            waiter.join(timeout=30)
        inode_queue.close()
        inode_queue.join_thread()

    assert waiter.exitcode == 0
```

- [ ] **Step 2: Verify the regression detects portalocker 3.3.0 behavior**

Temporarily replace only the body of
`TemporaryFileLock._acquire_verified()` in `portalocker/utils.py` with the
pre-fix behavior below. Do not stage or commit this temporary edit:

```python
        return Lock.acquire(
            lock,
            timeout,
            check_interval,
            fail_when_locked,
        )
```

Run:

```bash
uv run pytest \
  portalocker_tests/test_multiprocess.py::test_temporary_lock_waiters_converge_on_current_inode \
  --no-cov -q
```

Expected: both parameter cases fail because the third acquisition succeeds,
so `pytest.raises(portalocker.AlreadyLocked)` reports `DID NOT RAISE`.

- [ ] **Step 3: Restore the existing inode-verification implementation**

Restore the method body exactly:

```python
        for _ in lock._timeout_generator(timeout, check_interval):
            fh = Lock.acquire(lock, timeout, check_interval, fail_when_locked)
            if os.name == 'nt':  # Windows: a locked file can't be swapped.
                return fh  # pragma: not-nt
            if _fh_matches_path(fh, filename):  # pragma: not-posix
                return fh  # pragma: not-posix
            # Stale handle: the path was unlinked+recreated behind our back.
            Lock.release(lock)  # pragma: not-posix
        raise exceptions.AlreadyLocked(  # pragma: not-posix
            exceptions.LockException.LOCK_FAILED,
            f'{filename!r} kept being replaced while locking (split-brain)',
        )
```

Confirm `git diff -- portalocker/utils.py` is empty.

- [ ] **Step 4: Run the focused regression against the selected implementation**

Run:

```bash
uv run pytest \
  portalocker_tests/test_multiprocess.py::test_temporary_lock_waiters_converge_on_current_inode \
  --no-cov -q
```

Expected: `2 passed`.

- [ ] **Step 5: Check the changed test file**

Run:

```bash
uv run ruff check portalocker_tests/test_multiprocess.py
uv run ruff format --check portalocker_tests/test_multiprocess.py
uv run mypy portalocker_tests/test_multiprocess.py
```

Expected: all commands exit 0 without diagnostics.

- [ ] **Step 6: Commit the regression**

```bash
git add portalocker_tests/test_multiprocess.py
git commit -m 'test #115 split-inode waiters across processes'
```

### Task 2: Correct issue attribution

**Files:**

- Modify: `CHANGELOG.rst`
- Modify: `portalocker_tests/test_temporary_file_lock.py`
- Modify: `portalocker_tests/test_pidfilelock.py`

- [ ] **Step 1: Attribute #115 to the split-inode fix**

Change the split-brain changelog bullet to end with:

```rst
after locking (#115)
```

Remove `(#115)` from the separate stale-object release bullet. Preserve the
rest of both bullets unchanged.

Change the two stale-release regression docstrings so they no longer claim to
cover #115:

```python
def test_temporaryfilelock_release_without_ownership_keeps_file(tmpfile):
    """Releasing a lock object that holds nothing must not unlink the path.

    A stale object (double release or GC of a failed acquire) would otherwise
    destroy the current holder's lock file.
    """
```

```python
def test_pidfilelock_release_without_ownership_keeps_files(tmp_path):
    """A stale PidFileLock must not unlink a current holder's files.

    This covers double release and garbage collection after a failed acquire.
    """
```

- [ ] **Step 2: Verify the attribution-only changes**

Run:

```bash
uv run pytest \
  portalocker_tests/test_temporary_file_lock.py::test_temporaryfilelock_release_without_ownership_keeps_file \
  portalocker_tests/test_pidfilelock.py::test_pidfilelock_release_without_ownership_keeps_files \
  --no-cov -q
uv run codespell \
  CHANGELOG.rst \
  portalocker_tests/test_temporary_file_lock.py \
  portalocker_tests/test_pidfilelock.py
git diff --check
```

Expected: `2 passed`; codespell and `git diff --check` exit 0.

- [ ] **Step 3: Commit the attribution correction**

```bash
git add \
  CHANGELOG.rst \
  portalocker_tests/test_temporary_file_lock.py \
  portalocker_tests/test_pidfilelock.py
git commit -m 'correct #115 split-inode attribution'
```

### Task 3: Run complete verification

**Files:**

- Verify: all tracked changes on branch `issue-115`

- [ ] **Step 1: Run the full test suite**

Run:

```bash
uv run pytest
```

Expected on POSIX: `115 passed, 17 skipped`, 100% statement and branch
coverage, exit 0.

- [ ] **Step 2: Run all configured static checks**

Run:

```bash
uv run ruff check
uv run ruff format --check
uv run mypy
uv run basedpyright
uv run pyrefly check
uv run ty check
uv run codespell
```

Expected: every command exits 0 without errors.

- [ ] **Step 3: Verify branch scope and cleanliness**

Run:

```bash
git diff --check afbf995...HEAD
git diff --stat afbf995...HEAD
git status --short --branch
git log --oneline afbf995..HEAD
```

Expected: only the design, plan, issue regression, changelog attribution, and
stale-test docstrings differ; the worktree is clean.
