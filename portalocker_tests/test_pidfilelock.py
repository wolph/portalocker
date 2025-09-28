"""Tests for PidFileLock class."""

import multiprocessing
import os
import tempfile
import time
from pathlib import Path
from typing import Optional
from unittest import mock

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
        result_queue: multiprocessing.Queue[tuple[str, Optional[int], int]] = (
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
