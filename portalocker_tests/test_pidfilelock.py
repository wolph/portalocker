"""Tests for PidFileLock class."""

from __future__ import annotations

import builtins
import contextlib
import errno
import multiprocessing
import os
import tempfile
import time
import typing
from pathlib import Path
from unittest import mock

import pytest

import portalocker
from portalocker import utils


class _FailingPidFile:
    def __init__(
        self,
        wrapped: typing.TextIO,
        failure_stages: set[str],
    ) -> None:
        self._wrapped: typing.TextIO = wrapped
        self._failure_stages: set[str] = failure_stages

    def _raise_if(self, stage: str) -> None:
        if stage in self._failure_stages:
            raise OSError(f'{stage} failed')

    def __enter__(self) -> _FailingPidFile:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: typing.Any,
    ) -> None:
        self.close()

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        self._raise_if('seek')
        return self._wrapped.seek(offset, whence)

    def truncate(self, size: int | None = None) -> int:
        self._raise_if('truncate')
        if size is None:
            return self._wrapped.truncate()
        return self._wrapped.truncate(size)

    def write(self, data: str) -> int:
        self._raise_if('write')
        return self._wrapped.write(data)

    def flush(self) -> None:
        self._raise_if('flush')
        self._wrapped.flush()

    def fileno(self) -> int:
        return self._wrapped.fileno()

    def close(self) -> None:
        self._wrapped.close()
        self._raise_if('close')


def _pidfilelock_context_types(
    lock: utils.PidFileLock,
) -> tuple[
    contextlib.AbstractContextManager[int | None],
    contextlib.AbstractContextManager[None],
]:
    return lock, lock.fail_closed()


def test_already_locked_holder_pid_exists_without_init() -> None:
    exc: portalocker.AlreadyLocked = portalocker.AlreadyLocked.__new__(
        portalocker.AlreadyLocked,
    )

    assert exc.holder_pid is None


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


def test_pidfilelock_fail_closed_context_manager_success(
    tmp_path: Path,
) -> None:
    pid_file: Path = tmp_path / 'fail_closed_success.pid'
    lock: utils.PidFileLock = utils.PidFileLock(str(pid_file))

    with lock.fail_closed():
        is_acquired: bool = lock._acquired_lock
        assert is_acquired
        assert lock.read_pid() == os.getpid()

    is_released: bool = not lock._acquired_lock
    assert is_released
    assert not pid_file.exists()
    assert not Path(f'{pid_file}.lock').exists()


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
        with (
            pytest.raises(portalocker.AlreadyLocked) as exc_info,
            contender.fail_closed(),
        ):
            body_entered = True

        assert not body_entered
        assert exc_info.value.holder_pid is None
    finally:
        holder.release()


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
        with (
            pytest.raises(portalocker.AlreadyLocked) as exc_info,
            contender.fail_closed(),
        ):
            body_entered = True

        assert not body_entered
        assert exc_info.value.holder_pid == os.getpid()
    finally:
        holder.release()


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


def _worker_function(
    lock_file_path,
    result_queue,
    should_succeed,
    acquired_event,
    release_event,
):
    """Worker function for multiprocessing tests."""
    try:
        lock = utils.PidFileLock(lock_file_path)
        with lock as result:
            if should_succeed:
                # We hold the lock: announce it and keep holding until the
                # parent has observed the second process being blocked. This
                # replaces a fragile ``time.sleep`` hand-off.
                result_queue.put(('success', result, os.getpid()))
                acquired_event.set()
                release_event.wait(timeout=30)
            else:
                # We expect to get the PID of the process holding the lock.
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
        acquired = multiprocessing.Event()
        release = multiprocessing.Event()

        # Start first process that should acquire the lock
        p1 = multiprocessing.Process(
            target=_worker_function,
            args=(str(lock_file), result_queue, True, acquired, release),
        )
        p1.start()

        # Wait until the first process actually holds the lock instead of
        # guessing with a sleep.
        assert acquired.wait(timeout=30), 'first process never acquired lock'

        # Start second process that should be blocked
        p2 = multiprocessing.Process(
            target=_worker_function,
            args=(str(lock_file), result_queue, False, acquired, release),
        )
        p2.start()

        try:
            # Get results from both processes
            result1 = result_queue.get(timeout=30)
            result2 = result_queue.get(timeout=30)

            # First process should succeed
            assert result1[0] == 'success'
            assert result1[1] is None  # Acquired lock successfully
            p1_pid = result1[2]

            # Second process should be blocked and get first process PID
            assert result2[0] == 'blocked'
            assert result2[1] == p1_pid  # Should get PID of first process

        finally:
            # Let the holder release, then shut both processes down.
            release.set()
            p1.join(timeout=30)
            p2.join(timeout=30)

            # Clean up any remaining processes
            if p1.is_alive():
                p1.terminate()
            if p2.is_alive():
                p2.terminate()


def test_pidfilelock_timeout_waits_when_not_fail_when_locked(tmp_path):
    """A1: with ``fail_when_locked=False`` a contended acquire must honour
    the timeout (block, then raise ``AlreadyLocked``); with
    ``fail_when_locked=True`` it must fail fast.
    """
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
        # Well under the 0.5s timeout, with headroom for a starved CI runner.
        assert fast < 0.4, f'expected a fast failure, took {fast:.3f}s'
    finally:
        holder.release()


def test_pidfilelock_normalizes_plain_lockexception(tmp_path, monkeypatch):
    """A1: when a timed-out sidecar acquire re-raises a plain ``LockException``
    (rather than ``AlreadyLocked``, as can happen on Windows), ``acquire`` must
    normalize it to ``AlreadyLocked`` so ``__enter__`` and callers see one
    predictable surface.
    """
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


@pytest.mark.skipif(
    os.name == 'nt',
    reason='POSIX-only release ordering',
)
def test_pidfilelock_unlinks_sidecar_before_unlock(tmp_path, monkeypatch):
    """A2: release must unlink the sidecar lock file while the sidecar lock is
    still held (unlink before unlock) to avoid a split-brain window.
    """
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


@pytest.mark.skipif(
    os.name == 'nt',
    reason='POSIX-only release ordering',
)
def test_pidfilelock_unlocks_even_when_unlink_fails(tmp_path, monkeypatch):
    """Fix round 1: a non-FileNotFoundError unlink failure must still
    propagate, but the sidecar lock must be freed regardless — otherwise the
    error would leave the sidecar held forever.
    """
    lock_file = tmp_path / 'pidfilelock_unlink_fail.pid'
    lock = utils.PidFileLock(str(lock_file))
    lock.acquire()

    def failing_unlink(path, *args, **kwargs):
        raise PermissionError(f'unlink denied for {path!r}')

    monkeypatch.setattr(os, 'unlink', failing_unlink)
    # Keep ``excinfo`` (and with it the traceback) alive: in real usage the
    # propagating exception pins the frame that references the sidecar lock,
    # preventing a garbage-collection release from masking the leak.
    with pytest.raises(PermissionError) as excinfo:
        lock.release()
    monkeypatch.undo()
    assert excinfo.value is not None

    # The unlock ran and the reference is gone.
    assert lock._inner_lock is None

    # A fresh lock on the same path must acquire immediately.
    fresh = utils.PidFileLock(str(lock_file))
    fresh.acquire(timeout=0)
    try:
        assert fresh.read_pid() == os.getpid()
    finally:
        fresh.release()


def test_pidfilelock_release_without_acquire(tmp_path):
    """A2: releasing a never-acquired PidFileLock must be a safe no-op even
    though no sidecar lock is held.
    """
    lock_file = tmp_path / 'pidfilelock_no_acquire.pid'
    lock = utils.PidFileLock(str(lock_file))
    # No acquire: ``_inner_lock`` is None and neither file exists.
    lock.release()
    assert lock._inner_lock is None
    assert not os.path.isfile(lock_file)
    assert not os.path.isfile(f'{lock_file}.lock')


@pytest.mark.parametrize(
    'failure_stage',
    ('open', 'seek', 'truncate', 'write', 'flush', 'fsync', 'close'),
)
def test_pidfilelock_releases_sidecar_on_publication_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    """#116: every PID-publication failure rolls back the sidecar."""
    pid_file: Path = tmp_path / 'pidfilelock_writefail.pid'
    real_open: typing.Callable[..., typing.TextIO] = typing.cast(
        typing.Callable[..., typing.TextIO],
        builtins.open,
    )
    real_fsync: typing.Callable[[int], None] = os.fsync
    failure_stages: set[str] = {failure_stage}

    def failing_open(
        file: typing.Any,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> typing.TextIO:
        if str(file) != str(pid_file):
            return real_open(file, *args, **kwargs)
        if failure_stage == 'open':
            raise OSError('open failed')
        wrapped: typing.TextIO = real_open(file, *args, **kwargs)
        return typing.cast(
            typing.TextIO,
            _FailingPidFile(wrapped, failure_stages),
        )

    def failing_fsync(fd: int) -> None:
        if failure_stage == 'fsync':
            raise OSError('fsync failed')
        real_fsync(fd)

    monkeypatch.setattr(builtins, 'open', failing_open)
    monkeypatch.setattr(os, 'fsync', failing_fsync)

    failing_lock: utils.PidFileLock = utils.PidFileLock(str(pid_file))
    with pytest.raises(OSError, match=rf'^{failure_stage} failed$'):
        failing_lock.acquire()

    assert failing_lock._inner_lock is None
    assert not failing_lock._acquired_lock

    monkeypatch.undo()
    recovered: utils.PidFileLock = utils.PidFileLock(str(pid_file))
    recovered.acquire(timeout=0)
    try:
        assert recovered.read_pid() == os.getpid()
    finally:
        recovered.release()


@pytest.mark.parametrize('error_number', (errno.EINVAL, errno.ENOTSUP))
def test_pidfilelock_tolerates_unsupported_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    """Unsupported fsync must not regress otherwise valid PID publication."""
    pid_file: Path = tmp_path / 'pidfilelock_unsupported_fsync.pid'

    def unsupported_fsync(fd: int) -> None:
        raise OSError(error_number, os.strerror(error_number))

    monkeypatch.setattr(os, 'fsync', unsupported_fsync)

    lock: utils.PidFileLock = utils.PidFileLock(str(pid_file))
    lock.acquire(timeout=0)
    try:
        assert lock._acquired_lock
        assert lock.read_pid() == os.getpid()
    finally:
        lock.release()


def test_pidfilelock_preserves_write_error_when_pid_close_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#116: PID close failure is secondary to the publication failure."""
    pid_file: Path = tmp_path / 'pidfilelock_writeclosefail.pid'
    real_open: typing.Callable[..., typing.TextIO] = typing.cast(
        typing.Callable[..., typing.TextIO],
        builtins.open,
    )

    def failing_open(
        file: typing.Any,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> typing.TextIO:
        wrapped: typing.TextIO = real_open(file, *args, **kwargs)
        if str(file) != str(pid_file):
            return wrapped
        return typing.cast(
            typing.TextIO,
            _FailingPidFile(wrapped, {'write', 'close'}),
        )

    monkeypatch.setattr(builtins, 'open', failing_open)

    failing_lock: utils.PidFileLock = utils.PidFileLock(str(pid_file))
    with pytest.raises(OSError, match=r'^write failed$') as exc_info:
        failing_lock.acquire()

    assert isinstance(exc_info.value.__cause__, OSError)
    assert str(exc_info.value.__cause__) == 'close failed'
    assert failing_lock._inner_lock is None
    assert not failing_lock._acquired_lock

    monkeypatch.undo()
    recovered: utils.PidFileLock = utils.PidFileLock(str(pid_file))
    recovered.acquire(timeout=0)
    recovered.release()


def test_pidfilelock_preserves_publication_error_when_rollback_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#116: rollback failure is secondary and the handle is force-closed."""
    pid_file: Path = tmp_path / 'pidfilelock_rollbackfail.pid'
    publication_error: OSError = OSError('publication failed')
    cleanup_error: RuntimeError = RuntimeError('cleanup failed')
    captured_handles: list[typing.IO[typing.Any]] = []

    def failing_write_pid(lock: utils.PidFileLock) -> None:
        raise publication_error

    def failing_release(lock: utils.Lock) -> None:
        assert lock.fh is not None
        captured_handles.append(lock.fh)
        raise cleanup_error

    monkeypatch.setattr(utils.PidFileLock, '_write_pid', failing_write_pid)
    monkeypatch.setattr(utils.Lock, 'release', failing_release)

    failing_lock: utils.PidFileLock = utils.PidFileLock(str(pid_file))
    with pytest.raises(OSError) as exc_info:
        failing_lock.acquire()

    assert exc_info.value is publication_error
    assert exc_info.value.__cause__ is cleanup_error
    assert captured_handles
    assert captured_handles[0].closed
    assert failing_lock._inner_lock is None
    assert not failing_lock._acquired_lock

    monkeypatch.undo()
    recovered: utils.PidFileLock = utils.PidFileLock(str(pid_file))
    recovered.acquire(timeout=0)
    recovered.release()


def test_pidfilelock_chains_emergency_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#116: force-close failure remains behind the release failure."""
    pid_file: Path = tmp_path / 'pidfilelock_forceclosefail.pid'
    publication_error: OSError = OSError('publication failed')
    cleanup_error: RuntimeError = RuntimeError('cleanup failed')
    wrapped_handles: list[typing.IO[typing.Any]] = []

    def failing_write_pid(lock: utils.PidFileLock) -> None:
        assert lock._inner_lock is not None
        assert lock._inner_lock.fh is not None
        wrapped: typing.IO[typing.Any] = lock._inner_lock.fh
        wrapped_handles.append(wrapped)
        lock._inner_lock.fh = typing.cast(
            typing.TextIO,
            _FailingPidFile(
                typing.cast(typing.TextIO, wrapped),
                {'close'},
            ),
        )
        raise publication_error

    def failing_release(lock: utils.Lock) -> None:
        raise cleanup_error

    monkeypatch.setattr(utils.PidFileLock, '_write_pid', failing_write_pid)
    monkeypatch.setattr(utils.Lock, 'release', failing_release)

    failing_lock: utils.PidFileLock = utils.PidFileLock(str(pid_file))
    with pytest.raises(OSError) as exc_info:
        failing_lock.acquire()

    assert exc_info.value is publication_error
    assert exc_info.value.__cause__ is cleanup_error
    assert isinstance(cleanup_error.__cause__, OSError)
    assert str(cleanup_error.__cause__) == 'close failed'
    assert wrapped_handles[0].closed
    assert failing_lock._inner_lock is None
    assert not failing_lock._acquired_lock

    monkeypatch.undo()
    recovered: utils.PidFileLock = utils.PidFileLock(str(pid_file))
    recovered.acquire(timeout=0)
    recovered.release()


def test_pidfilelock_uses_emergency_close_error_when_release_leaves_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#116: emergency-close failure is reported after incomplete release."""
    pid_file: Path = tmp_path / 'pidfilelock_incomplete_release.pid'
    publication_error: OSError = OSError('publication failed')
    wrapped_handles: list[typing.IO[typing.Any]] = []

    def failing_write_pid(lock: utils.PidFileLock) -> None:
        assert lock._inner_lock is not None
        assert lock._inner_lock.fh is not None
        wrapped: typing.IO[typing.Any] = lock._inner_lock.fh
        wrapped_handles.append(wrapped)
        lock._inner_lock.fh = typing.cast(
            typing.TextIO,
            _FailingPidFile(
                typing.cast(typing.TextIO, wrapped),
                {'close'},
            ),
        )
        raise publication_error

    def incomplete_release(lock: utils.Lock) -> None:
        assert lock.fh is not None

    monkeypatch.setattr(utils.PidFileLock, '_write_pid', failing_write_pid)
    monkeypatch.setattr(utils.Lock, 'release', incomplete_release)

    failing_lock: utils.PidFileLock = utils.PidFileLock(str(pid_file))
    with pytest.raises(OSError) as exc_info:
        failing_lock.acquire()

    assert exc_info.value is publication_error
    assert isinstance(exc_info.value.__cause__, OSError)
    assert str(exc_info.value.__cause__) == 'close failed'
    assert wrapped_handles[0].closed
    assert failing_lock._inner_lock is None
    assert not failing_lock._acquired_lock

    monkeypatch.undo()
    recovered: utils.PidFileLock = utils.PidFileLock(str(pid_file))
    recovered.acquire(timeout=0)
    recovered.release()


def test_pidfilelock_preserves_pid_close_error_after_rollback_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#116: rollback causes retain the earlier PID-close failure."""
    pid_file: Path = tmp_path / 'pidfilelock_all_cleanup_failures.pid'
    real_open: typing.Callable[..., typing.TextIO] = typing.cast(
        typing.Callable[..., typing.TextIO],
        builtins.open,
    )
    cleanup_error: RuntimeError = RuntimeError('cleanup failed')
    sidecar_handles: list[typing.IO[typing.Any]] = []

    def failing_open(
        file: typing.Any,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> typing.TextIO:
        wrapped: typing.TextIO = real_open(file, *args, **kwargs)
        if str(file) != str(pid_file):
            return wrapped
        return typing.cast(
            typing.TextIO,
            _FailingPidFile(wrapped, {'write', 'close'}),
        )

    def failing_release(lock: utils.Lock) -> None:
        assert lock.fh is not None
        wrapped: typing.IO[typing.Any] = lock.fh
        sidecar_handles.append(wrapped)
        lock.fh = typing.cast(
            typing.TextIO,
            _FailingPidFile(
                typing.cast(typing.TextIO, wrapped),
                {'close'},
            ),
        )
        raise cleanup_error

    monkeypatch.setattr(builtins, 'open', failing_open)
    monkeypatch.setattr(utils.Lock, 'release', failing_release)

    failing_lock: utils.PidFileLock = utils.PidFileLock(str(pid_file))
    with pytest.raises(OSError, match=r'^write failed$') as exc_info:
        failing_lock.acquire()

    assert exc_info.value.__cause__ is cleanup_error
    sidecar_close_error: BaseException | None = cleanup_error.__cause__
    assert isinstance(sidecar_close_error, OSError)
    assert str(sidecar_close_error) == 'close failed'
    pid_close_error: BaseException | None = sidecar_close_error.__cause__
    assert isinstance(pid_close_error, OSError)
    assert str(pid_close_error) == 'close failed'
    assert sidecar_handles[0].closed
    assert failing_lock._inner_lock is None
    assert not failing_lock._acquired_lock

    monkeypatch.undo()
    recovered: utils.PidFileLock = utils.PidFileLock(str(pid_file))
    recovered.acquire(timeout=0)
    recovered.release()


def test_pidfilelock_release_without_ownership_keeps_files(tmp_path):
    """A stale PidFileLock must not unlink a current holder's files.

    This covers double release and garbage collection after a failed acquire.
    """
    pid_file = str(tmp_path / 'stale.pid')

    stale = utils.PidFileLock(pid_file)
    stale.acquire()
    stale.release()

    holder = utils.PidFileLock(pid_file)
    holder.acquire()
    try:
        stale.release()
        assert os.path.isfile(pid_file), 'stale release unlinked the pid file'

        never_acquired = utils.PidFileLock(pid_file)
        never_acquired.release()
        assert os.path.isfile(pid_file), (
            'never-acquired release unlinked the pid file'
        )
        assert holder.read_pid() == os.getpid()
    finally:
        holder.release()
    assert not os.path.isfile(pid_file)
