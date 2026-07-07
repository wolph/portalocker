"""Tests for PidFileLock class."""

import multiprocessing
import os
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

import portalocker
from portalocker import utils


def test_pidfilelock_creation():
    """Test basic PidFileLock creation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lock_file = Path(tmpdir) / 'test_pidfilelock_creation.lock'
        lock = utils.PidFileLock(str(lock_file))
        assert lock.filename == str(lock_file)
        assert not lock._acquired_lock


def test_pidfilelock_acquire_writes_pid():
    """Test that acquiring the lock writes the current PID."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lock_file = Path(tmpdir) / 'test_pidfilelock_acquire_writes_pid.lock'
        lock = utils.PidFileLock(str(lock_file))

        try:
            lock.acquire()
            assert lock._acquired_lock

            # Check that PID was written to file
            with open(lock_file) as f:
                written_pid = int(f.read().strip())
            assert written_pid == os.getpid()
        finally:
            lock.release()


def test_pidfilelock_context_manager_success():
    """Test context manager when we successfully acquire the lock."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lock_file = (
            Path(tmpdir) / 'test_pidfilelock_context_manager_success.lock'
        )
        lock = utils.PidFileLock(str(lock_file))

        with lock as result:
            assert result is None  # We acquired the lock
            assert lock._acquired_lock

            # Verify PID was written
            with open(lock_file) as f:
                written_pid = int(f.read().strip())
            assert written_pid == os.getpid()

        # Lock should be released and file cleaned up
        # Check both conditions after context manager exit
        lock_released: bool = not lock._acquired_lock
        file_cleaned: bool = not os.path.exists(lock_file)

        assert lock_released
        assert file_cleaned


def test_pidfilelock_context_manager_already_locked():
    """Test context manager when another process holds the lock."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lock_file = (
            Path(tmpdir)
            / 'test_pidfilelock_context_manager_already_locked.lock'
        )

        # Create a lock file with a fake PID
        fake_pid = 99999
        with open(lock_file, 'w') as f:
            f.write(str(fake_pid))

        # Create another lock that tries to acquire the same file
        lock1 = utils.PidFileLock(str(lock_file))
        lock1.acquire()  # This should succeed and write our PID

        try:
            lock2 = utils.PidFileLock(str(lock_file))
            with lock2 as result:
                assert result == os.getpid()  # Should return the PID of lock1
                assert not lock2._acquired_lock
        finally:
            lock1.release()


def test_read_pid_nonexistent_file():
    """Test reading PID from non-existent file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lock_file = Path(tmpdir) / 'test_read_pid_nonexistent_file.lock'
        lock = utils.PidFileLock(str(lock_file))
        assert lock.read_pid() is None


def test_read_pid_empty_file():
    """Test reading PID from empty file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lock_file = Path(tmpdir) / 'test_read_pid_empty_file.lock'
        lock_file.touch()  # Create empty file

        lock = utils.PidFileLock(str(lock_file))
        assert lock.read_pid() is None


def test_read_pid_invalid_content():
    """Test reading PID from file with invalid content."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lock_file = Path(tmpdir) / 'test_read_pid_invalid_content.lock'
        with open(lock_file, 'w') as f:
            f.write('not_a_number')

        lock = utils.PidFileLock(str(lock_file))
        assert lock.read_pid() is None


def test_read_pid_valid_content():
    """Test reading PID from file with valid content."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lock_file = Path(tmpdir) / 'test_read_pid_valid_content.lock'
        test_pid = 12345
        with open(lock_file, 'w') as f:
            f.write(str(test_pid))

        lock = utils.PidFileLock(str(lock_file))
        assert lock.read_pid() == test_pid


@mock.patch('builtins.open', side_effect=OSError('Permission denied'))
def test_read_pid_permission_error(mock_open):
    """Test reading PID when file cannot be opened."""
    lock = utils.PidFileLock('test_read_pid_permission_error.lock')
    assert lock.read_pid() is None


def test_release_without_acquire():
    """Test releasing without acquiring first."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lock_file = Path(tmpdir) / 'test_release_without_acquire.lock'
        lock = utils.PidFileLock(str(lock_file))

        # Should not raise an error
        lock.release()
        assert not lock._acquired_lock


def test_multiple_context_manager_entries():
    """Test multiple context manager entries."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lock_file = Path(tmpdir) / 'test_multiple_context_manager_entries.lock'
        lock = utils.PidFileLock(str(lock_file))

        with lock as result1:
            assert result1 is None

            # Try to enter context again while already locked
            lock2 = utils.PidFileLock(str(lock_file))
            with lock2 as result2:
                assert result2 == os.getpid()


def test_inheritance_from_temporaryfilelock():
    """Test that PidFileLock properly inherits from TemporaryFileLock."""
    lock = utils.PidFileLock()
    assert isinstance(lock, utils.TemporaryFileLock)
    assert isinstance(lock, utils.Lock)
    assert isinstance(lock, utils.LockBase)


def test_custom_parameters():
    """Test PidFileLock with custom parameters."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lock_file = Path(tmpdir) / 'test_custom_parameters.lock'
        lock = utils.PidFileLock(
            filename=str(lock_file),
            timeout=10.0,
            check_interval=0.1,
            fail_when_locked=False,
        )

        assert lock.filename == str(lock_file)
        assert lock.timeout == 10.0
        assert lock.check_interval == 0.1
        assert lock.fail_when_locked is False


def _worker_function(lock_file_path, result_queue, should_succeed):
    """Worker function for multiprocessing tests."""
    try:
        lock = utils.PidFileLock(lock_file_path)
        with lock as result:
            if should_succeed:
                # We expect to acquire the lock
                result_queue.put(('success', result, os.getpid()))
                time.sleep(0.5)  # Hold the lock briefly
            else:
                # We expect to get the PID of another process
                result_queue.put(('blocked', result, os.getpid()))
    except Exception as e:
        result_queue.put(('error', str(e), os.getpid()))


def test_multiprocess_locking():
    """Test that PidFileLock works correctly across processes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lock_file = Path(tmpdir) / 'test_multiprocess_locking.lock'
        result_queue: multiprocessing.Queue[tuple[str, int | None, int]] = (
            multiprocessing.Queue()
        )

        # Start first process that should acquire the lock
        p1 = multiprocessing.Process(
            target=_worker_function, args=(str(lock_file), result_queue, True)
        )
        p1.start()

        # Give first process time to acquire lock
        time.sleep(0.1)

        # Start second process that should be blocked
        p2 = multiprocessing.Process(
            target=_worker_function, args=(str(lock_file), result_queue, False)
        )
        p2.start()

        try:
            # Get results from both processes
            result1 = result_queue.get(timeout=2)
            result2 = result_queue.get(timeout=2)

            # First process should succeed
            assert result1[0] == 'success'
            assert result1[1] is None  # Acquired lock successfully
            p1_pid = result1[2]

            # Second process should be blocked and get first process PID
            assert result2[0] == 'blocked'
            assert result2[1] == p1_pid  # Should get PID of first process

        finally:
            p1.join(timeout=2)
            p2.join(timeout=2)

            # Clean up any remaining processes
            if p1.is_alive():
                p1.terminate()
            if p2.is_alive():
                p2.terminate()


def test_pidfilelock_timeout_waits_when_not_fail_when_locked(tmp_path):
    """A1: with ``fail_when_locked=False`` a contended acquire must honour
    the timeout (block, then raise ``AlreadyLocked``); with
    ``fail_when_locked=True`` it must fail fast."""
    lock_file = tmp_path / 'pidfilelock_timeout.pid'
    holder = utils.PidFileLock(str(lock_file))
    holder.acquire()
    try:
        contender = utils.PidFileLock(str(lock_file))

        # fail_when_locked=False must actually wait out the timeout.
        start = time.perf_counter()
        with pytest.raises(portalocker.AlreadyLocked):
            contender.acquire(fail_when_locked=False, timeout=0.5)
        waited = time.perf_counter() - start
        assert waited >= 0.3, f'expected a ~0.5s wait, waited {waited:.3f}s'

        # fail_when_locked=True must fail (almost) immediately.
        start = time.perf_counter()
        with pytest.raises(portalocker.AlreadyLocked):
            contender.acquire(fail_when_locked=True, timeout=0.5)
        fast = time.perf_counter() - start
        assert fast < 0.2, f'expected a fast failure, took {fast:.3f}s'
    finally:
        holder.release()


def test_pidfilelock_normalizes_plain_lockexception(tmp_path, monkeypatch):
    """A1: when a timed-out sidecar acquire re-raises a plain ``LockException``
    (rather than ``AlreadyLocked``, as can happen on Windows), ``acquire`` must
    normalize it to ``AlreadyLocked`` so ``__enter__`` and callers see one
    predictable surface."""
    lock_file = tmp_path / 'pidfilelock_normalize.pid'

    def boom(self, *args, **kwargs):
        raise portalocker.LockException('boom')

    # Patch the sidecar ``Lock.acquire`` (not ``PidFileLock.acquire``) so the
    # inner lock raises a non-``AlreadyLocked`` ``LockException``.
    monkeypatch.setattr(utils.Lock, 'acquire', boom)

    lock = utils.PidFileLock(str(lock_file))
    with pytest.raises(portalocker.AlreadyLocked):
        lock.acquire()
    # The failed sidecar reference must not leak.
    assert lock._inner_lock is None


def test_pidfilelock_unlinks_sidecar_before_unlock(tmp_path, monkeypatch):
    """A2: release must unlink the sidecar lock file while the sidecar lock is
    still held (unlink before unlock) to avoid a split-brain window."""
    lock_file = tmp_path / 'pidfilelock_order.pid'
    sidecar = f'{lock_file}.lock'
    events: list[tuple[str, str]] = []

    real_unlink = os.unlink
    real_unlock = portalocker.portalocker.unlock

    def record_unlink(path, *args, **kwargs):
        events.append(('unlink', str(path)))
        return real_unlink(path, *args, **kwargs)

    def record_unlock(file_obj, *args, **kwargs):
        events.append(('unlock', ''))
        return real_unlock(file_obj, *args, **kwargs)

    monkeypatch.setattr(os, 'unlink', record_unlink)
    monkeypatch.setattr(portalocker.portalocker, 'unlock', record_unlock)

    lock = utils.PidFileLock(str(lock_file))
    lock.acquire()
    lock.release()

    kinds = [kind for kind, _ in events]
    assert 'unlock' in kinds, 'sidecar lock should be unlocked on release'
    assert ('unlink', sidecar) in events, 'sidecar file should be unlinked'
    # The sidecar file must be unlinked before it is unlocked.
    assert events.index(('unlink', sidecar)) < kinds.index('unlock')


def test_pidfilelock_release_without_acquire(tmp_path):
    """A2: releasing a never-acquired PidFileLock must be a safe no-op even
    though no sidecar lock is held."""
    lock_file = tmp_path / 'pidfilelock_no_acquire.pid'
    lock = utils.PidFileLock(str(lock_file))
    # No acquire: ``_inner_lock`` is None and neither file exists.
    lock.release()
    assert lock._inner_lock is None
    assert not os.path.isfile(lock_file)
    assert not os.path.isfile(f'{lock_file}.lock')
