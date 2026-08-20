"""Regression tests for resolving lock paths at construction time.

A lock built with a relative path used to store it as given and resolve
it against the *current* working directory on every later OS call. An
``os.chdir`` between acquire and release (the daemonize idiom does
``chdir('/')``) then made release and the atexit hook unlink lock files
belonging to whatever process owns the equally-named files at the new
working directory. The path is now resolved once, in ``Lock.__init__``.
"""

from __future__ import annotations

import os
import pathlib

import pytest

import portalocker


def test_lock_filename_resolves_relative_path_at_construction(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    lock = portalocker.Lock('relative.lock', timeout=0)
    assert lock.filename == str(tmp_path / 'relative.lock')
    assert os.path.isabs(lock.filename)


def test_lock_filename_accepts_pathlib_and_absolute_paths(
    tmp_path: pathlib.Path,
) -> None:
    absolute = tmp_path / 'absolute.lock'
    lock = portalocker.Lock(absolute, timeout=0)
    assert lock.filename == str(absolute)


def test_temporaryfilelock_release_after_chdir_unlinks_original(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Release after ``os.chdir`` must remove the file the lock was
    acquired on, not consult (or unlink) the path relative to the new
    working directory where an unrelated holder may keep its own file.
    """
    dir_a = tmp_path / 'a'
    dir_b = tmp_path / 'b'
    dir_a.mkdir()
    dir_b.mkdir()
    victim = dir_b / '.lock'
    victim.write_text('another service holds this')

    monkeypatch.chdir(dir_a)
    lock = portalocker.TemporaryFileLock('.lock')
    lock.acquire()
    assert (dir_a / '.lock').exists()

    monkeypatch.chdir(dir_b)
    lock.release()

    assert not (dir_a / '.lock').exists(), 'the own lock file was left behind'
    assert victim.read_text() == 'another service holds this'


def test_pidfilelock_release_after_chdir_removes_own_files(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `PidFileLock` twin: both the PID file and the sidecar must be
    resolved once, so a daemon-style ``chdir`` cannot redirect the
    release at another service's pidfile pair.
    """
    dir_a = tmp_path / 'a'
    dir_b = tmp_path / 'b'
    dir_a.mkdir()
    dir_b.mkdir()
    victim_pid = dir_b / '.pid'
    victim_sidecar = dir_b / '.pid.lock'
    victim_pid.write_text('54321')
    victim_sidecar.write_text('')

    monkeypatch.chdir(dir_a)
    lock = portalocker.PidFileLock('.pid')
    lock.acquire()
    assert lock._lockfile == str(dir_a / '.pid.lock')
    assert (dir_a / '.pid').exists()
    assert (dir_a / '.pid.lock').exists()

    monkeypatch.chdir(dir_b)
    lock.release()

    assert not (dir_a / '.pid').exists(), 'the own PID file was left behind'
    assert not (dir_a / '.pid.lock').exists(), 'the sidecar was left behind'
    assert victim_pid.read_text() == '54321'
    assert victim_sidecar.exists()
