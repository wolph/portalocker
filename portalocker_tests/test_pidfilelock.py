"""Tests for PidFileLock class."""

from __future__ import annotations

import builtins
import contextlib
import errno
import gc
import itertools
import logging
import multiprocessing
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import typing
import weakref
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


def test_pidfilelock_terminal_lockexception_propagates(tmp_path, monkeypatch):
    """A plain ``LockException`` from the sidecar is terminal (a backend
    that cannot lock at all: ``ENOLCK``, an unsupported filesystem), not
    contention, so ``acquire`` must let it propagate as itself. It used
    to be normalized to ``AlreadyLocked``, which told callers to retry a
    failure retrying cannot fix and contradicted the retry contract
    (``AlreadyLocked`` means contention, a plain ``LockException`` is
    permanent).
    """
    lock_file = tmp_path / 'pidfilelock_terminal.pid'

    def boom(self, *args, **kwargs):
        raise portalocker.LockException('boom')

    # Patch the sidecar ``Lock.acquire`` (not ``PidFileLock.acquire``) so the
    # inner lock raises a non-``AlreadyLocked`` ``LockException``.
    monkeypatch.setattr(utils.Lock, 'acquire', boom)

    lock = utils.PidFileLock(str(lock_file))
    with pytest.raises(portalocker.LockException, match='boom') as exc_info:
        lock.acquire()
    assert not isinstance(exc_info.value, portalocker.AlreadyLocked)
    # The failed sidecar reference must not leak.
    assert lock._inner_lock is None


def test_pidfilelock_enter_propagates_terminal_lockexception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``__enter__`` turns readable contention into a returned PID, but a
    terminal ``LockException`` is not contention and must propagate: there
    is no holder to report and running the block would be wrong.
    """
    lock_file = tmp_path / 'pidfilelock_terminal_enter.pid'

    def boom(self, *args, **kwargs):
        raise portalocker.LockException('boom')

    monkeypatch.setattr(utils.Lock, 'acquire', boom)

    lock = utils.PidFileLock(str(lock_file))
    with pytest.raises(portalocker.LockException, match='boom') as exc_info:
        lock.__enter__()
    assert not isinstance(exc_info.value, portalocker.AlreadyLocked)


def test_pidfilelock_contention_still_raises_already_locked(tmp_path):
    """Genuine contention keeps its ``AlreadyLocked`` surface after the
    terminal ``LockException`` change.
    """
    lock_file = str(tmp_path / 'pidfilelock_contention.pid')
    holder = utils.PidFileLock(lock_file)
    holder.acquire()
    contender = utils.PidFileLock(lock_file, timeout=0)
    try:
        with pytest.raises(portalocker.AlreadyLocked):
            contender.acquire()
    finally:
        holder.release()


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
def test_pidfilelock_unlocks_even_when_unlink_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-FileNotFoundError unlink failure must free the sidecar lock
    regardless, and with ``raise_on_release_error`` unset it is
    suppressed and logged rather than raised, matching
    `TemporaryFileLock.release`. The raising strict-mode side is covered
    by `test_pidfilelock_constructor_accepts_raise_on_release_error`.
    """
    lock_file = tmp_path / 'pidfilelock_unlink_fail.pid'
    lock = utils.PidFileLock(str(lock_file))
    lock.acquire()

    def failing_unlink(path, *args, **kwargs):
        raise PermissionError(f'unlink denied for {path!r}')

    monkeypatch.setattr(os, 'unlink', failing_unlink)
    with caplog.at_level(logging.WARNING, logger=utils.logger.name):
        lock.release()
    monkeypatch.undo()
    assert any(
        'suppressed error while removing' in record.message
        for record in caplog.records
    )

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
    ('open', 'write', 'flush', 'fsync', 'close', 'replace'),
)
def test_pidfilelock_releases_sidecar_on_publication_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    """#116: every PID-publication failure rolls back the sidecar."""
    pid_file: Path = tmp_path / 'pidfilelock_writefail.pid'
    # The PID is published through a temporary file that `os.replace`
    # moves over the PID file, so the failures are injected on that
    # temporary path rather than on the PID file itself.
    pid_temp_file: str = f'{pid_file}.{os.getpid()}.tmp'
    real_open: typing.Callable[..., typing.TextIO] = typing.cast(
        typing.Callable[..., typing.TextIO],
        builtins.open,
    )
    real_fsync: typing.Callable[[int], None] = os.fsync
    real_replace: typing.Callable[..., None] = typing.cast(
        typing.Callable[..., None],
        os.replace,
    )
    failure_stages: set[str] = {failure_stage}

    def failing_open(
        file: typing.Any,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> typing.TextIO:
        if str(file) != pid_temp_file:
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

    def failing_replace(
        src: typing.Any,
        dst: typing.Any,
        **kwargs: typing.Any,
    ) -> None:
        if failure_stage == 'replace' and str(dst) == str(pid_file):
            raise OSError('replace failed')
        real_replace(src, dst, **kwargs)

    monkeypatch.setattr(builtins, 'open', failing_open)
    monkeypatch.setattr(os, 'fsync', failing_fsync)
    monkeypatch.setattr(os, 'replace', failing_replace)

    failing_lock: utils.PidFileLock = utils.PidFileLock(str(pid_file))
    with pytest.raises(OSError, match=rf'^{failure_stage} failed$'):
        failing_lock.acquire()

    assert failing_lock._inner_lock is None
    assert not failing_lock._acquired_lock
    assert not os.path.exists(pid_temp_file), (
        'the failed publication left its temporary file behind'
    )

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
    pid_temp_file: str = f'{pid_file}.{os.getpid()}.tmp'
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
        if str(file) != pid_temp_file:
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
    sidecar_locks: list[utils.Lock] = []
    real_verified: typing.Callable[..., typing.IO[typing.Any]] = (
        utils.PidFileLock._acquire_verified
    )

    def capturing_verified(
        lock: utils.Lock,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> typing.IO[typing.Any]:
        sidecar_locks.append(lock)
        return real_verified(lock, *args, **kwargs)

    def failing_write_pid(lock: utils.PidFileLock) -> None:
        # `_inner_lock` is only published after a fully successful
        # acquire, so the sidecar is reached through the captured local.
        inner: utils.Lock = sidecar_locks[-1]
        assert inner.fh is not None
        wrapped: typing.IO[typing.Any] = inner.fh
        wrapped_handles.append(wrapped)
        inner.fh = typing.cast(
            typing.TextIO,
            _FailingPidFile(
                typing.cast(typing.TextIO, wrapped),
                {'close'},
            ),
        )
        raise publication_error

    def failing_release(lock: utils.Lock) -> None:
        raise cleanup_error

    monkeypatch.setattr(
        utils.PidFileLock,
        '_acquire_verified',
        staticmethod(capturing_verified),
    )
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
    sidecar_locks: list[utils.Lock] = []
    real_verified: typing.Callable[..., typing.IO[typing.Any]] = (
        utils.PidFileLock._acquire_verified
    )

    def capturing_verified(
        lock: utils.Lock,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> typing.IO[typing.Any]:
        sidecar_locks.append(lock)
        return real_verified(lock, *args, **kwargs)

    def failing_write_pid(lock: utils.PidFileLock) -> None:
        # `_inner_lock` is only published after a fully successful
        # acquire, so the sidecar is reached through the captured local.
        inner: utils.Lock = sidecar_locks[-1]
        assert inner.fh is not None
        wrapped: typing.IO[typing.Any] = inner.fh
        wrapped_handles.append(wrapped)
        inner.fh = typing.cast(
            typing.TextIO,
            _FailingPidFile(
                typing.cast(typing.TextIO, wrapped),
                {'close'},
            ),
        )
        raise publication_error

    def incomplete_release(lock: utils.Lock) -> None:
        assert lock.fh is not None

    monkeypatch.setattr(
        utils.PidFileLock,
        '_acquire_verified',
        staticmethod(capturing_verified),
    )
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
    pid_temp_file: str = f'{pid_file}.{os.getpid()}.tmp'
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
        if str(file) != pid_temp_file:
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


def test_pidfilelock_reacquire_is_noop(tmp_path, monkeypatch):
    """T7: acquire() on an instance that already holds the lock must be an
    idempotent no-op. The buggy version built a new sidecar `Lock` and
    overwrote `_inner_lock`, and the discarded object's teardown released
    the held OS lock mid-call, opening a theft window.
    """
    lock = utils.PidFileLock(str(tmp_path / 'reacquire.pid'))
    first_fh = lock.acquire()
    # A weak reference on purpose: a strong one would keep the discarded
    # sidecar Lock alive and hide the buggy teardown release.
    assert lock._inner_lock is not None
    inner_before = weakref.ref(lock._inner_lock)

    released: list[str] = []
    real_release = utils.Lock.release

    def spy_release(self: utils.Lock) -> None:
        if self.fh is not None:
            released.append('released-a-held-lock')
        real_release(self)

    monkeypatch.setattr(utils.Lock, 'release', spy_release)
    pid_writes: list[str] = []
    monkeypatch.setattr(
        lock,
        '_write_pid',
        lambda: pid_writes.append('pid-write'),
    )

    second_fh = lock.acquire()
    # Drive the finalizer of a discarded sidecar Lock, if one was created.
    gc.collect()

    assert released == [], 'the second acquire released the held OS lock'
    assert second_fh is first_fh
    assert lock._inner_lock is inner_before()
    assert pid_writes == [], 'the second acquire rewrote the PID file'

    monkeypatch.undo()
    lock.release()
    assert not (tmp_path / 'reacquire.pid').exists()


def test_pidfilelock_instance_timeout_honoured(tmp_path):
    """T8: `PidFileLock(path, timeout=0.4, fail_when_locked=False)` must
    wait out roughly 0.4 seconds. The buggy version built the sidecar
    `Lock` from the acquire() parameter (None), which `Lock.__init__`
    silently turned into the module default timeout.
    """
    pid_file = str(tmp_path / 'instance_timeout.pid')
    holder = utils.PidFileLock(pid_file)
    holder.acquire()
    try:
        contender = utils.PidFileLock(
            pid_file,
            timeout=0.4,
            check_interval=0.05,
            fail_when_locked=False,
        )
        start = time.perf_counter()
        with pytest.raises(portalocker.AlreadyLocked):
            contender.acquire()
        waited = time.perf_counter() - start
        assert 0.25 <= waited < 2.0, (
            f'instance timeout of 0.4s waited {waited:.3f}s'
        )
    finally:
        holder.release()


def test_pidfilelock_percall_timeout_overrides_instance(tmp_path):
    """The per-call timeout must win over the instance attribute, per the
    `LockBase` contract: the argument wins when it is not None.
    """
    pid_file = str(tmp_path / 'percall_timeout.pid')
    holder = utils.PidFileLock(pid_file)
    holder.acquire()
    try:
        contender = utils.PidFileLock(
            pid_file,
            timeout=5.0,
            check_interval=0.05,
            fail_when_locked=False,
        )
        start = time.perf_counter()
        with pytest.raises(portalocker.AlreadyLocked):
            contender.acquire(timeout=0.3)
        waited = time.perf_counter() - start
        assert 0.2 <= waited < 1.5, (
            f'per-call timeout of 0.3s waited {waited:.3f}s'
        )
    finally:
        holder.release()


def test_pidfilelock_instance_check_interval_honoured(tmp_path, monkeypatch):
    """The instance check_interval must pace the sidecar lock attempts.

    With check_interval=0.2 the retries are roughly 0.2 seconds apart and a
    0.5 second timeout buys about four attempts. The buggy version paced
    the sidecar at the module default interval instead.
    """
    pid_file = str(tmp_path / 'check_interval.pid')
    holder = utils.PidFileLock(pid_file)
    holder.acquire()

    attempts: list[float] = []
    real_lock = portalocker.portalocker.lock

    def spy_lock(fh, flags):
        attempts.append(time.perf_counter())
        return real_lock(fh, flags)

    monkeypatch.setattr(portalocker.portalocker, 'lock', spy_lock)
    try:
        contender = utils.PidFileLock(
            pid_file,
            timeout=0.5,
            check_interval=0.2,
            fail_when_locked=False,
        )
        with pytest.raises(portalocker.AlreadyLocked):
            contender.acquire()
    finally:
        monkeypatch.undo()
        holder.release()

    gaps = [b - a for a, b in itertools.pairwise(attempts)]
    assert 3 <= len(attempts) <= 6, f'{len(attempts)} attempts: {gaps}'
    assert max(gaps) >= 0.15, f'attempts paced too tightly: {gaps}'


def test_pidfilelock_accepts_pathlib_path(tmp_path):
    """`filename` is `types.Filename`: a `pathlib.Path` must work end to
    end and satisfy the type checkers.
    """
    path = tmp_path / 'pathlib.pid'
    lock = utils.PidFileLock(path)
    lock.acquire()
    assert path.is_file()
    assert lock.read_pid() == os.getpid()
    assert (tmp_path / 'pathlib.pid.lock').is_file()
    lock.release()
    assert not path.exists()
    assert not (tmp_path / 'pathlib.pid.lock').exists()


@pytest.mark.parametrize('interrupt', [KeyboardInterrupt, SystemExit])
def test_pidfilelock_interrupted_contender_leaves_holder_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt: type[BaseException],
) -> None:
    """R2: a contender interrupted while waiting for the sidecar lock (a
    SIGINT, or a SIGTERM handler calling sys.exit) must not consider itself
    a holder. Its release must leave the live holder's PID and sidecar
    files alone, otherwise the next acquirer wins and two processes hold
    the lock at once.
    """
    pid_file = str(tmp_path / 'interrupted.pid')
    holder = utils.PidFileLock(pid_file)
    holder.acquire()

    def interrupting_lock(fh, flags):
        raise interrupt('signal while waiting for the sidecar lock')

    monkeypatch.setattr(portalocker.portalocker, 'lock', interrupting_lock)
    contender = utils.PidFileLock(
        pid_file,
        timeout=0.5,
        fail_when_locked=False,
    )
    with pytest.raises(interrupt):
        contender.acquire()
    monkeypatch.undo()

    # The interrupted contender holds nothing, so releasing it must be a
    # no-op instead of unlinking the holder's files.
    contender.release()
    assert os.path.isfile(pid_file), (
        'the interrupted contender unlinked the holder PID file'
    )
    assert holder.read_pid() == os.getpid()

    third = utils.PidFileLock(pid_file)
    with pytest.raises(portalocker.AlreadyLocked):
        third.acquire()

    holder.release()
    assert not os.path.isfile(pid_file)


def test_pidfilelock_release_with_lost_sidecar_lock_keeps_files(tmp_path):
    """A PidFileLock whose sidecar OS lock is already gone (fh is None)
    must not unlink the PID and sidecar files: they may belong to a new
    holder by now.
    """
    pid_file = str(tmp_path / 'lost.pid')
    first = utils.PidFileLock(pid_file)
    first.acquire()
    # Drop the OS lock behind the instance's back, standing in for any
    # path that releases the sidecar without going through
    # `PidFileLock.release`.
    assert first._inner_lock is not None
    utils.Lock.release(first._inner_lock)

    second = utils.PidFileLock(pid_file)
    second.acquire()
    try:
        first.release()
        assert os.path.isfile(pid_file), (
            'a lockless release unlinked the new holder PID file'
        )
        assert second.read_pid() == os.getpid()
    finally:
        second.release()
    assert not os.path.isfile(pid_file)


@pytest.mark.parametrize('sabotage', ['missing', 'empty', 'garbage'])
def test_pidfilelock_enter_raises_when_holder_pid_unreadable(
    tmp_path: Path,
    sabotage: str,
) -> None:
    """R6: on contention with an unreadable holder PID, `__enter__` must
    raise instead of returning the `None` we-are-the-holder sentinel. The
    buggy version made a chmod'ed, deleted or garbage PID file run the
    caller's exclusive block next to a live holder.
    """
    pid_file = tmp_path / 'unreadable.pid'
    holder = utils.PidFileLock(str(pid_file))
    holder.acquire()
    try:
        if sabotage == 'missing':
            os.unlink(pid_file)
        elif sabotage == 'empty':
            pid_file.write_text('')
        else:
            pid_file.write_text('-1')

        contender = utils.PidFileLock(str(pid_file))
        with pytest.raises(portalocker.AlreadyLocked) as excinfo, contender:
            pytest.fail('the exclusive block ran without ownership')
        assert excinfo.value.holder_pid is None
        # The cause chain names the real problem: the read error when the
        # PID file could not be read at all, the contention otherwise.
        if sabotage == 'missing':
            assert isinstance(excinfo.value.__cause__, FileNotFoundError)
        else:
            assert isinstance(
                excinfo.value.__cause__,
                portalocker.AlreadyLocked,
            )
    finally:
        holder.release()


@pytest.mark.parametrize(
    'content',
    ['-1', '0', '+7', '1_000', '\uff11\uff12\uff13', '0x10', '12abc', ''],
)
def test_read_pid_rejects_unsafe_content(tmp_path, content):
    """R6: `read_pid` must only accept plain positive ASCII decimals.

    Signs, underscores, fullwidth digits, zero and negatives all parse
    through a bare `int()` and the obvious consumer feeds the result to
    `os.kill`, where -1 signals everything the user owns. The write
    pins its encoding to UTF-8 because the fullwidth-digit case cannot
    be encoded by the cp1252 locale of a stock Windows runner.
    """
    pid_file = tmp_path / 'strict.pid'
    pid_file.write_text(content, encoding='utf-8')
    lock = utils.PidFileLock(str(pid_file))
    assert lock.read_pid() is None


def test_read_pid_rejects_undecodable_bytes(tmp_path):
    """R6: bytes no text codec accepts must read as `None`, not raise.

    `read_pid` promises `None` for unreadable content, but reading the
    file as locale-encoded text let a `UnicodeDecodeError` escape for
    byte junk (invalid UTF-8 on POSIX, undefined cp1252 bytes such as
    0x81 on Windows). The file is now read as bytes and validated as
    ASCII, so the junk is rejected like any other non-decimal content.
    """
    pid_file = tmp_path / 'binary.pid'
    pid_file.write_bytes(b'\xff\xfe\x81123')
    lock = utils.PidFileLock(str(pid_file))
    assert lock.read_pid() is None


def test_read_pid_rejects_over_long_digit_strings(tmp_path):
    """R6: a preposterous digit run must read as `None`, not raise.

    CPython caps `int(str)` conversion at 4300 digits (see
    `sys.set_int_max_str_digits`) and raises `ValueError` beyond it, so
    a junk PID file of 5000 digits escaped `read_pid` as an exception
    where the contract promises `None`. No real PID needs more than 20
    digits (a 64-bit ``pid_max`` is 20), so longer content is now
    rejected before the conversion is attempted.
    """
    pid_file = tmp_path / 'oversized.pid'
    pid_file.write_text('9' * 5000, encoding='ascii')
    lock = utils.PidFileLock(str(pid_file))
    assert lock.read_pid() is None


def test_read_pid_accepts_surrounding_whitespace(tmp_path):
    """A trailing newline from `echo $$ > file` style writers stays valid."""
    pid_file = tmp_path / 'whitespace.pid'
    pid_file.write_text(' 42\n')
    lock = utils.PidFileLock(str(pid_file))
    assert lock.read_pid() == 42


class _ObservingWriter:
    """Wrap a writable file, calling `observe` before every mutation."""

    def __init__(
        self,
        wrapped: typing.IO[str],
        observe: typing.Callable[[], None],
    ) -> None:
        self._wrapped = wrapped
        self._observe = observe

    def write(self, data: str) -> int:
        self._observe()
        return self._wrapped.write(data)

    def truncate(self, size: int | None = None) -> int:
        self._observe()
        if size is None:
            return self._wrapped.truncate()
        return self._wrapped.truncate(size)

    def __getattr__(self, name: str) -> typing.Any:
        return getattr(self._wrapped, name)


def test_pidfilelock_publication_is_atomic(tmp_path, monkeypatch):
    """R8: a reader must never observe a truncated or empty PID file.

    The buggy version truncated the PID file in place, so between the
    truncate and the write a concurrent `read_pid` saw an empty file, and
    before the truncate it saw the previous (possibly dead) holder's PID.
    The write now goes through a temporary file and `os.replace`, so a
    reader sees either the old complete PID or the new complete PID.
    """
    pid_file = tmp_path / 'atomic.pid'
    pid_file.write_text('54321')  # a crashed previous holder's PID

    observed: list[str] = []
    real_open = builtins.open

    def observe() -> None:
        try:
            with real_open(pid_file) as fh:
                observed.append(fh.read())
        except FileNotFoundError:
            observed.append('<missing>')

    def observing_open(file, *args, **kwargs):
        fh = real_open(file, *args, **kwargs)
        mode = kwargs.get('mode', args[0] if args else 'r')
        writable = isinstance(mode, str) and ('w' in mode or 'a' in mode)
        if writable and str(file).startswith(str(tmp_path)):
            return _ObservingWriter(fh, observe)
        return fh

    monkeypatch.setattr(builtins, 'open', observing_open)
    lock = utils.PidFileLock(str(pid_file))
    lock.acquire()
    monkeypatch.undo()

    assert observed, 'expected the PID write to be observed'
    allowed = {'54321', str(os.getpid())}
    for snapshot in observed:
        assert snapshot in allowed, (
            f'a reader could observe {snapshot!r} instead of a complete PID'
        )
    assert lock.read_pid() == os.getpid()
    lock.release()


def test_pidfilelock_nt_release_unlinks_pidfile_before_sidecar_unlock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Windows release path must unlink the PID file while the sidecar
    lock is still held, mirroring the POSIX order. Unlinking after the
    sidecar release deletes the PID file a fast successor just published.
    The branch is exercised here by patching `os.name`: the PID file
    carries no OS lock on any platform, so every step runs fine on POSIX.
    """
    pid_file = str(tmp_path / 'nt_order.pid')
    lock = utils.PidFileLock(pid_file)
    lock.acquire()

    events: list[str] = []
    real_unlink = os.unlink
    # The sidecar teardown claims the filehandle first and unlocks it via
    # `_release_claimed_fh`, so that is the choke point to observe.
    real_release_claimed = utils.Lock._release_claimed_fh

    def recording_unlink(path, *args, **kwargs):
        events.append(f'unlink:{os.path.basename(str(path))}')
        return real_unlink(path, *args, **kwargs)

    def recording_release(self: utils.Lock, fh: typing.Any) -> None:
        events.append('sidecar-release')
        real_release_claimed(self, fh)

    monkeypatch.setattr(os, 'unlink', recording_unlink)
    monkeypatch.setattr(utils.Lock, '_release_claimed_fh', recording_release)
    monkeypatch.setattr(os, 'name', 'nt')
    try:
        lock.release()
    finally:
        monkeypatch.undo()

    assert 'unlink:nt_order.pid' in events, events
    assert 'sidecar-release' in events, events
    assert events.index('unlink:nt_order.pid') < events.index(
        'sidecar-release'
    ), f'PID file unlinked after the sidecar release: {events}'
    assert not os.path.isfile(pid_file)


def test_pidfilelock_nt_release_tolerates_missing_sidecar_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Windows release path must skip the sidecar unlink when the file
    is already gone and still finish the rest of the teardown.

    The cleaner's removal is staged as a scripted `FileNotFoundError`
    instead of a real pre-release unlink: on Windows the sidecar is held
    open by the acquire and cannot be unlinked here (WinError 32), and
    the release path meets the same exception either way. A really
    removed sidecar is exercised by the POSIX-only compromised-sidecar
    tests in this module.
    """
    pid_file = str(tmp_path / 'nt_missing.pid')
    lock = utils.PidFileLock(pid_file)
    lock.acquire()
    sidecar = f'{pid_file}.lock'
    real_unlink = os.unlink

    def unlink_vanished_sidecar(
        target: typing.Any,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> None:
        if str(target) == sidecar:
            # A cleaner already removed the sidecar.
            raise FileNotFoundError(
                errno.ENOENT, 'already cleaned up', sidecar
            )
        real_unlink(target, *args, **kwargs)

    monkeypatch.setattr(os, 'unlink', unlink_vanished_sidecar)
    monkeypatch.setattr(os, 'name', 'nt')
    try:
        lock.release()
    finally:
        monkeypatch.undo()
    assert not os.path.isfile(pid_file)
    assert lock._inner_lock is None


posix_sidecar_only = pytest.mark.skipif(
    os.name == 'nt',
    reason='POSIX-only inode verification, a locked file cannot be '
    'swapped on nt',
)


@posix_sidecar_only
def test_pidfilelock_reacquire_raises_when_sidecar_compromised(tmp_path):
    """Re-acquire on a holding instance whose sidecar a third party
    unlinked must raise exactly like `TemporaryFileLock` does, instead of
    silently returning a filehandle whose lock no longer guards the path
    a competitor now owns.
    """
    pid_file = str(tmp_path / 'compromised.pid')
    holder = utils.PidFileLock(pid_file)
    held_fh = holder.acquire()
    os.unlink(f'{pid_file}.lock')  # a cleaner sweeps both files
    os.unlink(pid_file)

    competitor = utils.PidFileLock(pid_file)
    competitor.acquire()
    try:
        with pytest.raises(portalocker.LockException, match='unlink'):
            holder.acquire()
        assert not held_fh.closed, 'the held filehandle was closed'
        assert holder._inner_lock is not None
        assert holder._inner_lock.fh is held_fh
        assert competitor.read_pid() == os.getpid()
    finally:
        competitor.release()


@posix_sidecar_only
def test_pidfilelock_compromised_release_spares_competitor_files(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Releasing a compromised holder must free its OS lock without
    unlinking the PID and sidecar files a competitor now owns.
    """
    pid_file = str(tmp_path / 'swept.pid')
    holder = utils.PidFileLock(pid_file)
    holder.acquire()
    os.unlink(f'{pid_file}.lock')  # a cleaner sweeps both files
    os.unlink(pid_file)

    competitor = utils.PidFileLock(pid_file)
    competitor.acquire()
    try:
        with caplog.at_level(logging.WARNING, logger='portalocker.utils'):
            holder.release()
        assert os.path.isfile(pid_file), (
            'the compromised release unlinked the competitor PID file'
        )
        assert os.path.isfile(f'{pid_file}.lock'), (
            'the compromised release unlinked the competitor sidecar'
        )
        assert competitor.read_pid() == os.getpid()
        assert holder._inner_lock is None
        assert any(
            'not unlinking' in record.getMessage() for record in caplog.records
        ), 'expected a warning about the skipped unlink'
    finally:
        competitor.release()
    assert not os.path.isfile(pid_file)


@pytest.mark.parametrize('interrupt', [KeyboardInterrupt, SystemExit])
def test_pidfilelock_interrupt_during_publication_releases_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt: type[BaseException],
) -> None:
    """An interrupt between taking the sidecar lock and publishing the
    instance state must roll the sidecar back. Without the rollback the
    OS lock is stranded on a local that only refcount garbage collection
    releases, and a pinned traceback (this test keeps the ExceptionInfo
    alive) blocks every contender indefinitely.
    """
    pid_file = str(tmp_path / 'pub_interrupt.pid')
    lock = utils.PidFileLock(pid_file)

    def interrupting_write_pid(self: utils.PidFileLock) -> None:
        raise interrupt('signal during PID publication')

    monkeypatch.setattr(
        utils.PidFileLock,
        '_write_pid',
        interrupting_write_pid,
    )
    with pytest.raises(interrupt) as excinfo:
        lock.acquire()
    monkeypatch.undo()

    # The pinned exception keeps the acquire frame, and with it the local
    # sidecar Lock, alive: refcount collection cannot help here.
    assert excinfo.traceback is not None
    assert lock._inner_lock is None

    contender = utils.PidFileLock(pid_file)
    contender.acquire(timeout=0)
    contender.release()


@pytest.mark.skipif(os.name == 'nt', reason='os.fork is POSIX-only')
def test_pidfilelock_atexit_releases_lock_acquired_in_forked_child(
    tmp_path: Path,
) -> None:
    """The `PidFileLock` twin of the acquired-in-child exit test: the
    child's normal exit must remove the PID file it published and the
    sidecar, instead of leaving its stale PID behind for `read_pid` to
    hand to ``os.kill`` after the pid gets recycled.
    """
    pid_path = tmp_path / 'worker.pid'
    script = textwrap.dedent(
        f"""\
        import gc
        import os
        import sys

        import portalocker

        # Constructed in the parent, before the fork.
        lock = portalocker.PidFileLock({str(pid_path)!r})

        pid = os.fork()
        if pid == 0:
            # `LockBase` has no finalizer (4.2.0 removed it) and the
            # cycle collector is off besides: the atexit path alone
            # must clean up after the child.
            gc.disable()
            lock.acquire()
            sys.exit(0)

        os.waitpid(pid, 0)
        for leftover in ({str(pid_path)!r}, {str(pid_path) + '.lock'!r}):
            if os.path.exists(leftover):
                raise RuntimeError(
                    "the child's exit left %r behind" % leftover
                )

        with portalocker.PidFileLock({str(pid_path)!r}) as holder_pid:
            if holder_pid is not None:
                raise RuntimeError(
                    'a stale holder %r survived the child' % holder_pid
                )
        """,
    )
    completed = subprocess.run(
        [sys.executable, '-c', script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout == ''
    assert completed.stderr == ''
    assert not pid_path.exists()


def _fail_pid_unlink(
    monkeypatch: pytest.MonkeyPatch,
    pid_file: str,
    lockfile_too: bool = False,
) -> None:
    """Make ``os.unlink`` raise `PermissionError` for the PID file, and
    optionally for the sidecar as well.
    """
    real_unlink = os.unlink

    def failing_unlink(
        target: typing.Any,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> None:
        if str(target) == pid_file or (
            lockfile_too and str(target) == f'{pid_file}.lock'
        ):
            raise PermissionError(f'unlink denied for {target!r}')
        real_unlink(target, *args, **kwargs)

    monkeypatch.setattr(os, 'unlink', failing_unlink)


def test_pidfilelock_exit_preserves_body_exception_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A release failure in ``__exit__`` must not replace the body's own
    exception. With ``raise_on_release_error`` unset the unlink failure
    is suppressed entirely, exactly like `Lock.__exit__`; the historical
    ``__exit__`` called ``release()`` bare and the `PermissionError`
    replaced the ``ValueError``.
    """
    pid_file = str(tmp_path / 'mask_default.pid')
    lock = utils.PidFileLock(pid_file)
    with (
        pytest.raises(ValueError, match='body failed'),
        lock as holder_pid,
    ):
        assert holder_pid is None
        _fail_pid_unlink(monkeypatch, pid_file)
        raise ValueError('body failed')
    monkeypatch.undo()
    assert lock._inner_lock is None


def test_pidfilelock_exit_strict_chains_release_error_onto_body_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``raise_on_release_error`` set the body exception still wins
    and the release failure is attached as its ``__context__``, matching
    the `Lock.__exit__` masking guarantee.

    The explanatory note only exists where ``add_note`` does (3.11+);
    on 3.10 the release code quietly skips it, so the assertion follows
    the same fork, mirroring the strict-context tests in
    `test_release_errors`.
    """
    pid_file = str(tmp_path / 'mask_strict.pid')
    lock = utils.PidFileLock(pid_file)
    lock.raise_on_release_error = True
    with (
        pytest.raises(ValueError, match='body failed') as exc_info,
        lock as holder_pid,
    ):
        assert holder_pid is None
        _fail_pid_unlink(monkeypatch, pid_file)
        raise ValueError('body failed')
    monkeypatch.undo()
    assert isinstance(exc_info.value.__context__, PermissionError)
    if hasattr(exc_info.value, 'add_note'):
        notes: list[str] = getattr(exc_info.value, '__notes__', [])
        assert any('release failed' in note for note in notes)
    else:
        assert not hasattr(exc_info.value, '__notes__')


def test_pidfilelock_exit_strict_raises_release_error_with_clean_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a clean body and ``raise_on_release_error`` set the unlink
    failure surfaces from ``__exit__``, as `Lock.__exit__` promises.
    """
    pid_file = str(tmp_path / 'strict_clean.pid')
    lock = utils.PidFileLock(pid_file)
    lock.raise_on_release_error = True
    with (
        pytest.raises(PermissionError, match='unlink denied'),
        lock as holder_pid,
    ):
        assert holder_pid is None
        _fail_pid_unlink(monkeypatch, pid_file)
    monkeypatch.undo()
    assert lock._inner_lock is None
    os.unlink(pid_file)


def test_pidfilelock_exit_default_suppresses_release_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With ``raise_on_release_error`` unset a clean body exits cleanly
    even when the unlink fails; the failure is logged instead.
    """
    pid_file = str(tmp_path / 'default_clean.pid')
    lock = utils.PidFileLock(pid_file)
    with (
        caplog.at_level(logging.WARNING, logger=utils.logger.name),
        lock as holder_pid,
    ):
        assert holder_pid is None
        _fail_pid_unlink(monkeypatch, pid_file)
    monkeypatch.undo()
    assert lock._inner_lock is None
    assert any(
        'suppressed error while removing' in record.message
        for record in caplog.records
    )
    os.unlink(pid_file)


def test_pidfilelock_fail_closed_preserves_body_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fail-closed adapter routes through the same ``__exit__``, so
    the masking guarantee holds there as well.
    """
    pid_file = str(tmp_path / 'mask_fail_closed.pid')
    lock = utils.PidFileLock(pid_file)
    lock.raise_on_release_error = True
    with (
        pytest.raises(ValueError, match='body failed') as exc_info,
        lock.fail_closed(),
    ):
        _fail_pid_unlink(monkeypatch, pid_file)
        raise ValueError('body failed')
    monkeypatch.undo()
    assert isinstance(exc_info.value.__context__, PermissionError)


def test_pidfilelock_constructor_accepts_raise_on_release_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The constructor must accept and forward ``raise_on_release_error``
    instead of raising `TypeError`; the docs have described strict mode
    for this class all along.
    """
    pid_file = str(tmp_path / 'ctor_strict.pid')
    lock = utils.PidFileLock(pid_file, raise_on_release_error=True)
    assert lock.raise_on_release_error is True
    lock.acquire()
    _fail_pid_unlink(monkeypatch, pid_file)
    with pytest.raises(PermissionError, match='unlink denied'):
        lock.release()
    monkeypatch.undo()
    assert lock._inner_lock is None
    os.unlink(pid_file)


def test_pidfilelock_posix_strict_reports_both_failing_unlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When both the PID file and the sidecar refuse to unlink, strict
    mode raises the first failure and the second is logged, so neither
    error disappears and the second removal was still attempted.
    """
    pid_file = str(tmp_path / 'both_fail.pid')
    lock = utils.PidFileLock(pid_file, raise_on_release_error=True)
    lock.acquire()
    _fail_pid_unlink(monkeypatch, pid_file, lockfile_too=True)
    with (
        caplog.at_level(logging.WARNING, logger=utils.logger.name),
        pytest.raises(PermissionError, match='both_fail'),
    ):
        lock.release()
    monkeypatch.undo()
    assert any(
        'suppressed additional error' in record.message
        for record in caplog.records
    )
    assert lock._inner_lock is None
    os.unlink(pid_file)
    os.unlink(f'{pid_file}.lock')


def test_pidfilelock_posix_strict_unlock_error_propagates_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A strict release whose unlinks succeed but whose sidecar unlock
    fails must raise the unlock error on its own.
    """
    pid_file = str(tmp_path / 'unlock_fail.pid')
    lock = utils.PidFileLock(pid_file, raise_on_release_error=True)
    lock.acquire()

    def failing_unlock(fh, *args, **kwargs):
        raise portalocker.LockException('unlock refused')

    monkeypatch.setattr(portalocker.portalocker, 'unlock', failing_unlock)
    with pytest.raises(portalocker.LockException, match='unlock refused'):
        lock.release()
    monkeypatch.undo()
    assert lock._inner_lock is None


def test_pidfilelock_posix_strict_unlock_error_chains_unlink_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A strict release where the unlink and the sidecar unlock both fail
    must raise the unlock error chained from the unlink error, so neither
    failure disappears.
    """
    pid_file = str(tmp_path / 'chain_fail.pid')
    lock = utils.PidFileLock(pid_file, raise_on_release_error=True)
    lock.acquire()
    _fail_pid_unlink(monkeypatch, pid_file)

    def failing_unlock(fh, *args, **kwargs):
        raise portalocker.LockException('unlock refused')

    monkeypatch.setattr(portalocker.portalocker, 'unlock', failing_unlock)
    with pytest.raises(
        portalocker.LockException,
        match='unlock refused',
    ) as exc_info:
        lock.release()
    monkeypatch.undo()
    assert isinstance(exc_info.value.__cause__, PermissionError)
    assert lock._inner_lock is None
    os.unlink(pid_file)


def test_pidfilelock_nt_release_tolerates_missing_pid_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Windows release path must treat an already-removed PID file as
    fine and still finish the rest of the teardown.
    """
    pid_file = str(tmp_path / 'nt_missing_pid.pid')
    lock = utils.PidFileLock(pid_file)
    lock.acquire()
    os.unlink(pid_file)  # a cleaner already removed the PID file
    monkeypatch.setattr(os, 'name', 'nt')
    try:
        lock.release()
    finally:
        monkeypatch.undo()
    assert not os.path.isfile(f'{pid_file}.lock')
    assert lock._inner_lock is None


def test_pidfilelock_nt_strict_release_raises_pid_unlink_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Windows release path must report a failing PID file unlink
    through the ``raise_on_release_error`` contract instead of silently
    swallowing it as it did before 4.2.0.
    """
    pid_file = str(tmp_path / 'nt_strict.pid')
    lock = utils.PidFileLock(pid_file, raise_on_release_error=True)
    lock.acquire()
    _fail_pid_unlink(monkeypatch, pid_file)
    monkeypatch.setattr(os, 'name', 'nt')
    with pytest.raises(PermissionError, match='nt_strict'):
        lock.release()
    monkeypatch.undo()
    assert lock._inner_lock is None
    os.unlink(pid_file)


def test_pidfilelock_nt_default_release_logs_sidecar_unlink_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The Windows release path must capture a failing sidecar unlink
    like the POSIX path does, logging it under the default flag.
    """
    pid_file = str(tmp_path / 'nt_sidecar_fail.pid')
    lock = utils.PidFileLock(pid_file)
    lock.acquire()
    real_unlink = os.unlink

    def failing_sidecar_unlink(target, *args, **kwargs):
        if str(target) == f'{pid_file}.lock':
            raise PermissionError(f'unlink denied for {target!r}')
        real_unlink(target, *args, **kwargs)

    monkeypatch.setattr(os, 'unlink', failing_sidecar_unlink)
    monkeypatch.setattr(os, 'name', 'nt')
    with caplog.at_level(logging.WARNING, logger=utils.logger.name):
        lock.release()
    monkeypatch.undo()
    assert any(
        'suppressed error while removing' in record.message
        for record in caplog.records
    )
    assert lock._inner_lock is None
    os.unlink(f'{pid_file}.lock')


def test_pidfilelock_nt_release_logs_second_unlink_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the PID file and the sidecar both refuse to unlink on the
    Windows path, the first failure is reported and the second is logged.
    """
    pid_file = str(tmp_path / 'nt_both_fail.pid')
    lock = utils.PidFileLock(pid_file, raise_on_release_error=True)
    lock.acquire()
    _fail_pid_unlink(monkeypatch, pid_file, lockfile_too=True)
    monkeypatch.setattr(os, 'name', 'nt')
    with (
        caplog.at_level(logging.WARNING, logger=utils.logger.name),
        pytest.raises(PermissionError, match='nt_both_fail'),
    ):
        lock.release()
    monkeypatch.undo()
    assert any(
        'suppressed additional error' in record.message
        for record in caplog.records
    )
    assert lock._inner_lock is None
    os.unlink(pid_file)
    os.unlink(f'{pid_file}.lock')


def test_pidfilelock_nt_strict_unlock_error_propagates_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A strict Windows release whose PID unlink succeeds but whose
    sidecar unlock fails must raise the unlock error on its own.
    """
    pid_file = str(tmp_path / 'nt_unlock_fail.pid')
    lock = utils.PidFileLock(pid_file, raise_on_release_error=True)
    lock.acquire()

    def failing_unlock(fh, *args, **kwargs):
        raise portalocker.LockException('unlock refused')

    monkeypatch.setattr(portalocker.portalocker, 'unlock', failing_unlock)
    monkeypatch.setattr(os, 'name', 'nt')
    with pytest.raises(portalocker.LockException, match='unlock refused'):
        lock.release()
    monkeypatch.undo()
    assert lock._inner_lock is None
    os.unlink(f'{pid_file}.lock')


def test_pidfilelock_nt_strict_unlock_error_chains_unlink_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A strict Windows release where the PID unlink and the sidecar
    unlock both fail must raise the unlock error chained from the unlink
    error.
    """
    pid_file = str(tmp_path / 'nt_chain_fail.pid')
    lock = utils.PidFileLock(pid_file, raise_on_release_error=True)
    lock.acquire()
    _fail_pid_unlink(monkeypatch, pid_file)

    def failing_unlock(fh, *args, **kwargs):
        raise portalocker.LockException('unlock refused')

    monkeypatch.setattr(portalocker.portalocker, 'unlock', failing_unlock)
    monkeypatch.setattr(os, 'name', 'nt')
    with pytest.raises(
        portalocker.LockException,
        match='unlock refused',
    ) as exc_info:
        lock.release()
    monkeypatch.undo()
    assert isinstance(exc_info.value.__cause__, PermissionError)
    assert lock._inner_lock is None
    os.unlink(pid_file)
    os.unlink(f'{pid_file}.lock')


def test_pidfilelock_losing_rollback_spares_winning_thread_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A contender's rollback must not erase another thread's win.

    Two threads fresh-acquire one instance: A takes the sidecar, B fails
    with contention and rolls back. B's rollback used to wipe
    ``_inner_lock`` and ``_acquired_lock`` unconditionally, so A's
    published state vanished, its ``__exit__`` no-oped and garbage
    collection of the orphaned sidecar freed the OS lock mid-block. The
    gates park B inside its rollback until A has published.

    Who wins is pinned down, not raced: the loser only starts once the
    winner signals from inside ``_write_pid``, which runs with the
    sidecar already held. The earlier version gave the winner a 10 ms
    head start and lost it on a slow Windows runner, where the winner
    thread had not even been scheduled yet and the whole choreography
    deadlocked into leaked threads failing later tests.
    """
    pid_file = str(tmp_path / 'rollback_wipe.pid')
    lock = utils.PidFileLock(pid_file, fail_when_locked=True)

    a_holds_sidecar = threading.Event()
    b_in_rollback = threading.Event()
    a_published = threading.Event()
    real_rollback = utils.PidFileLock._rollback_failed_acquire
    real_write_pid = utils.PidFileLock._write_pid

    def gated_rollback(
        self: utils.PidFileLock,
        inner_lock: utils.Lock,
    ) -> Exception | None:
        b_in_rollback.set()
        assert a_published.wait(timeout=10), 'the winner never published'
        return real_rollback(self, inner_lock)

    def gated_write_pid(self: utils.PidFileLock) -> None:
        if threading.current_thread().name == 'winner':
            a_holds_sidecar.set()
            assert b_in_rollback.wait(timeout=10), 'the loser never failed'
        real_write_pid(self)

    monkeypatch.setattr(
        utils.PidFileLock,
        '_rollback_failed_acquire',
        gated_rollback,
    )
    monkeypatch.setattr(utils.PidFileLock, '_write_pid', gated_write_pid)

    results: dict[str, str] = {}

    def winner() -> None:
        lock.acquire()
        results['winner'] = 'acquired'
        a_published.set()

    def loser() -> None:
        try:
            lock.acquire()
        except portalocker.AlreadyLocked:
            results['loser'] = 'AlreadyLocked'

    winner_thread = threading.Thread(target=winner, name='winner')
    loser_thread = threading.Thread(target=loser, name='loser')
    winner_thread.start()
    assert a_holds_sidecar.wait(timeout=10), 'the winner never took the lock'
    loser_thread.start()
    winner_thread.join(timeout=10)
    loser_thread.join(timeout=10)
    assert not winner_thread.is_alive()
    assert not loser_thread.is_alive()
    monkeypatch.undo()

    assert results == {'winner': 'acquired', 'loser': 'AlreadyLocked'}
    assert lock._acquired_lock is True, 'the rollback erased the win'
    assert lock._inner_lock is not None, 'the rollback erased the win'

    gc.collect()
    probe = utils.Lock(lock._lockfile, timeout=0, fail_when_locked=True)
    with pytest.raises(portalocker.AlreadyLocked):
        probe.acquire()

    lock.release()
    assert not os.path.exists(pid_file)


def test_pidfilelock_rollback_clears_state_it_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the failure strikes after publication (the exit-hook
    registration raising), the rollback owns the published state and must
    clear it while rolling the sidecar back.
    """
    pid_file = str(tmp_path / 'rollback_published.pid')
    lock = utils.PidFileLock(pid_file)

    class ExplodingRegistry:
        def __setitem__(
            self,
            key: utils.TemporaryFileLock,
            value: int,
        ) -> None:
            raise RuntimeError('registry refused the lock')

    monkeypatch.setattr(utils, '_exit_releases', ExplodingRegistry())
    with pytest.raises(RuntimeError, match='registry refused'):
        lock.acquire()
    monkeypatch.undo()

    assert lock._inner_lock is None
    assert lock._acquired_lock is False
    successor = utils.PidFileLock(pid_file)
    successor.acquire()
    assert successor.read_pid() == os.getpid()
    successor.release()


def test_pidfilelock_reentrant_release_during_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handler's ``release()`` between publication and return must not
    crash the acquire.

    The exit-hook registration runs right after publication, so a
    reentrant release there claims the freshly published state; the old
    ``assert inner_lock.fh is not None`` then fired (and ``python -O``
    returned ``None`` instead). The acquire must return its own locally
    bound filehandle and leave the instance released.
    """
    pid_file = str(tmp_path / 'reentrant_registration.pid')
    lock = utils.PidFileLock(pid_file)

    class ReleasingRegistry:
        def __setitem__(
            self,
            key: utils.TemporaryFileLock,
            value: int,
        ) -> None:
            # The SIGTERM-handler shape: a release lands right after the
            # publication and claims everything.
            lock.release()

    monkeypatch.setattr(utils, '_exit_releases', ReleasingRegistry())
    fh = lock.acquire()
    monkeypatch.undo()

    assert fh is not None
    assert fh.closed, 'the reentrant release should have torn the fh down'
    assert lock._inner_lock is None
    assert lock._acquired_lock is False

    successor = utils.PidFileLock(pid_file)
    successor.acquire()
    assert successor.read_pid() == os.getpid()
    successor.release()
