# `open_atomic()` No-Replace Implementation Plan

**For agentic workers:** REQUIRED SUB-SKILL: Use
superpowers:subagent-driven-development (recommended) or
superpowers:executing-plans to implement this plan task-by-task. Steps use
checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `open_atomic()` publish atomically without replacing a
destination created before publication, while preserving its existing API and
entry-time `AssertionError` behavior.

**Architecture:** Keep the existing same-directory temporary-file workflow and
replace the final `os.rename()` with atomic hard-link publication through
`os.link()`. Turn the removable entry assertion into an explicit
`AssertionError`, retain unconditional temporary-file cleanup, and document the
no-replacement contract.

**Tech Stack:** Python 3.10+, `contextlib`, `os`, `pathlib`, `tempfile`, pytest,
uv, mypy, basedpyright, pyrefly, ty, Sphinx.

---

### Task 1: Prevent Replacement During Publication

**Files:**

- Create: `portalocker_tests/test_open_atomic.py`
- Modify: `portalocker/utils.py:108-112`

- [ ] **Step 1: Write the failing publication-race test**

Create `portalocker_tests/test_open_atomic.py` with:

```python
from __future__ import annotations

import pathlib

import pytest

import portalocker


def test_open_atomic_preserves_destination_created_before_publication(
    tmp_path: pathlib.Path,
) -> None:
    target: pathlib.Path = tmp_path / 'destination.bin'
    entries_before: set[pathlib.Path] = set(tmp_path.iterdir())

    with pytest.raises(FileExistsError):
        with portalocker.open_atomic(target) as temporary:
            written: int = temporary.write(b'temporary payload')
            assert written == len(b'temporary payload')
            target.write_bytes(b'concurrent winner')

    assert target.read_bytes() == b'concurrent winner'
    assert set(tmp_path.iterdir()) == entries_before | {target}
```

- [ ] **Step 2: Run the test and verify the race reproduces**

Run:

```bash
uv run pytest --no-cov portalocker_tests/test_open_atomic.py::test_open_atomic_preserves_destination_created_before_publication -v
```

Expected: FAIL with `Failed: DID NOT RAISE <class 'FileExistsError'>`; the
current POSIX `os.rename()` replaces the concurrent winner.

- [ ] **Step 3: Publish with an atomic no-replace hard link**

In `portalocker/utils.py`, replace the publication call only:

```python
    try:
        os.link(temp_fh.name, path)
    finally:
        with contextlib.suppress(Exception):
            os.remove(temp_fh.name)
```

- [ ] **Step 4: Run the focused test and verify it passes**

Run:

```bash
uv run pytest --no-cov portalocker_tests/test_open_atomic.py::test_open_atomic_preserves_destination_created_before_publication -v
```

Expected: PASS. The destination contains `b'concurrent winner'`, and only the
destination remains in the directory.

- [ ] **Step 5: Commit the red/green change**

```bash
git add portalocker/utils.py portalocker_tests/test_open_atomic.py
git commit -m "fix: prevent open_atomic replacing concurrent destination"
```

### Task 2: Preserve the Entry Check Under Optimized Python

**Files:**

- Modify: `portalocker_tests/test_open_atomic.py`
- Modify: `portalocker/utils.py:94`

- [ ] **Step 1: Add a failing optimized-mode compatibility test**

Add these imports to `portalocker_tests/test_open_atomic.py`:

```python
import subprocess
import sys
import textwrap
```

Then add:

```python
def test_open_atomic_existing_destination_check_survives_optimization(
    tmp_path: pathlib.Path,
) -> None:
    target: pathlib.Path = tmp_path / 'destination.bin'
    existing: bytes = b'existing destination'
    expected_message: str = f'{target!r} exists'
    target.write_bytes(existing)

    with pytest.raises(AssertionError, match='exists'):
        with portalocker.open_atomic(target):
            pass

    script: str = textwrap.dedent(
        f'''\
        import pathlib

        import portalocker

        target: pathlib.Path = pathlib.Path({str(target)!r})
        expected: bytes = {existing!r}
        expected_message: str = {expected_message!r}
        try:
            with portalocker.open_atomic(target):
                pass
        except AssertionError as error:
            if str(error) != expected_message:
                raise
        else:
            raise RuntimeError('missing AssertionError under optimized Python')
        if target.read_bytes() != expected:
            raise RuntimeError('existing destination was modified')
        ''',
    )
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, '-O', '-c', script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout == ''
    assert completed.stderr == ''
    assert target.read_bytes() == existing
```

- [ ] **Step 2: Run the optimized-mode test and verify it fails**

Run:

```bash
uv run pytest --no-cov portalocker_tests/test_open_atomic.py::test_open_atomic_existing_destination_check_survives_optimization -v
```

Expected: FAIL with `subprocess.CalledProcessError`; `python -O` removes the
current assertion, so the child process reaches publication instead of raising
the compatibility exception.

- [ ] **Step 3: Replace the removable assertion with an explicit exception**

In `portalocker/utils.py`, replace the assertion with:

```python
    if path.exists():
        raise AssertionError(f'{path!r} exists')
```

- [ ] **Step 4: Run the optimized-mode test and verify it passes**

Run:

```bash
uv run pytest --no-cov portalocker_tests/test_open_atomic.py::test_open_atomic_existing_destination_check_survives_optimization -v
```

Expected: PASS in both the pytest process and its optimized child process.

- [ ] **Step 5: Run both regression tests**

Run:

```bash
uv run pytest --no-cov portalocker_tests/test_open_atomic.py -v
```

Expected: 2 passed.

- [ ] **Step 6: Commit the compatibility fix**

```bash
git add portalocker/utils.py portalocker_tests/test_open_atomic.py
git commit -m "fix: preserve open_atomic check under optimization"
```

### Task 3: Cover Success and Non-Collision Failure Cleanup

**Files:**

- Modify: `portalocker_tests/test_open_atomic.py`

- [ ] **Step 1: Add success and publication-error characterization tests**

Add `import os` to `portalocker_tests/test_open_atomic.py`, then add:

```python
def test_open_atomic_publishes_without_leaving_temporary_file(
    tmp_path: pathlib.Path,
) -> None:
    target: pathlib.Path = tmp_path / 'destination.bin'
    entries_before: set[pathlib.Path] = set(tmp_path.iterdir())

    with portalocker.open_atomic(target) as temporary:
        written: int = temporary.write(b'published payload')
        assert written == len(b'published payload')

    assert target.read_bytes() == b'published payload'
    assert set(tmp_path.iterdir()) == entries_before | {target}


def test_open_atomic_cleans_temporary_file_after_link_error(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target: pathlib.Path = tmp_path / 'destination.bin'
    temporary_paths: list[pathlib.Path] = []

    def fail_link(source: str, destination: pathlib.Path) -> None:
        temporary_paths.append(pathlib.Path(source))
        assert destination == target
        raise OSError('link publication failed')

    monkeypatch.setattr(os, 'link', fail_link)

    with pytest.raises(OSError, match='link publication failed'):
        with portalocker.open_atomic(target) as temporary:
            written: int = temporary.write(b'unpublished payload')
            assert written == len(b'unpublished payload')

    assert len(temporary_paths) == 1
    assert not temporary_paths[0].exists()
    assert not target.exists()
    assert not any(tmp_path.iterdir())
```

- [ ] **Step 2: Run all focused tests**

Run:

```bash
uv run pytest --no-cov portalocker_tests/test_open_atomic.py -v
```

Expected: 4 passed. These tests characterize the retained success and cleanup
behavior around the new publication primitive.

- [ ] **Step 3: Commit the cleanup coverage**

```bash
git add portalocker_tests/test_open_atomic.py
git commit -m "test: cover open_atomic publication cleanup"
```

### Task 4: Document the No-Replace Contract

**Files:**

- Modify: `portalocker/utils.py:63-86`
- Modify: `CHANGELOG.rst:1-25`

- [ ] **Step 1: Update the function docstring**

Replace the opening paragraphs of `open_atomic()` with:

```python
    """Open a new file for atomic writing without replacing an existing file.

    The destination must not exist when entering or publishing the context. If
    another actor creates it while the context is open, publication raises
    :class:`FileExistsError` and leaves that destination untouched.

    The implementation writes and synchronizes a temporary file in the
    destination directory, then publishes it with an atomic hard link. The
    filesystem must support hard links.

    https://docs.python.org/3/library/os.html#os.link
```

Keep the existing doctest examples below these paragraphs.

- [ ] **Step 2: Add the changelog entry**

Add this bullet to the 4.0.0 section in `CHANGELOG.rst`:

```rst
* Fixed ``open_atomic()`` replacing a destination created while its context was
  open on POSIX; publication now raises ``FileExistsError`` and preserves the
  concurrent winner (#114)
```

- [ ] **Step 3: Run the focused tests and doctests**

Run:

```bash
uv run pytest --no-cov portalocker_tests/test_open_atomic.py portalocker/utils.py -v
```

Expected: all focused tests and `portalocker.utils` doctests pass. The full-suite
coverage gate remains in Task 5.

- [ ] **Step 4: Commit the documentation**

```bash
git add portalocker/utils.py CHANGELOG.rst
git commit -m "docs: define open_atomic no-replace contract"
```

### Task 5: Full Verification

**Files:**

- Verify only; no planned modifications.

- [ ] **Step 1: Run the complete test suite with coverage**

```bash
uv run pytest
```

Expected: exit 0, no failures, and 100% coverage.

- [ ] **Step 2: Run all configured static type checkers**

```bash
uv run mypy
uv run basedpyright
uv run pyrefly check
uv run ty check
```

Expected: each command exits 0 with no errors.

- [ ] **Step 3: Run spelling and documentation checks**

```bash
uv run codespell
mkdir -p docs/_static
uv run sphinx-build -W -b html docs docs/_build/html
```

Expected: each command exits 0 with no spelling errors or Sphinx warnings.

- [ ] **Step 4: Build both distributions**

```bash
uv build
```

Expected: exit 0 with a source distribution and wheel in `dist/`.

- [ ] **Step 5: Inspect the final repository state**

```bash
git diff --check HEAD~4..HEAD
git status --short --branch
git log --oneline --decorate -6
```

Expected: no whitespace errors; the worktree is clean on `issue-114`; the
design, plan, implementation, tests, and documentation commits are present.
