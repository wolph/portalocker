"""Tests for the BoundedSemaphore helper."""

import pathlib
import random
import subprocess
import sys
import textwrap
import threading
import time
import typing

import pytest

import portalocker
from portalocker import utils


@pytest.mark.parametrize('timeout', [None, 0, 0.001])
@pytest.mark.parametrize('check_interval', [None, 0, 0.0005])
def test_bounded_semaphore(timeout, check_interval, monkeypatch):
    """Ensure that the semaphore honours *maximum*, *timeout* and
    *check_interval* and raises AlreadyLocked when exhausted.
    """
    n = 2
    name: str = str(random.random())
    monkeypatch.setattr(utils, 'DEFAULT_TIMEOUT', 0.0001)
    monkeypatch.setattr(utils, 'DEFAULT_CHECK_INTERVAL', 0.0005)

    semaphore_a = portalocker.BoundedSemaphore(n, name=name, timeout=timeout)
    semaphore_b = portalocker.BoundedSemaphore(n, name=name, timeout=timeout)
    semaphore_c = portalocker.BoundedSemaphore(n, name=name, timeout=timeout)

    semaphore_a.acquire(timeout=timeout)
    semaphore_b.acquire()
    with pytest.raises(portalocker.AlreadyLocked):
        semaphore_c.acquire(check_interval=check_interval, timeout=timeout)

    semaphore_c.acquire(
        check_interval=check_interval,
        timeout=timeout,
        fail_when_locked=False,
    )


def test_bounded_semaphore_recovers_after_acquire_error(tmp_path):
    """A3: a non-AlreadyLocked failure (e.g. a missing directory raising
    FileNotFoundError) must not leave ``self.lock`` set. Otherwise the
    ``assert not self.lock`` guard bricks the instance for every later
    acquire.
    """
    missing = tmp_path / 'missing'
    semaphore = portalocker.NamedBoundedSemaphore(
        1,
        name='recover',
        directory=str(missing),
        timeout=0,
    )

    with pytest.raises(FileNotFoundError):
        semaphore.acquire()
    assert semaphore.lock is None, 'a failed acquire must not leak self.lock'

    # Create the directory the second time around; the SAME instance must now
    # acquire cleanly instead of raising AssertionError.
    missing.mkdir()
    lock = semaphore.acquire()
    assert lock is not None
    semaphore.release()


def test_bounded_semaphore_full_waits_out_timeout_then_raises() -> None:
    """A full semaphore with ``fail_when_locked=True`` (the default) must
    retry for the whole timeout before raising ``AlreadyLocked``.

    This wait-then-raise timing has been the behaviour since 3.2.0 and
    diverges from the fail-fast handling of the other lock classes. The
    4.1.0 constructor docstring wrongly promised fail-fast, so this test
    pins the real contract with a measured lower bound.
    """
    name: str = str(random.random())
    holder_a = portalocker.BoundedSemaphore(2, name=name)
    holder_b = portalocker.BoundedSemaphore(2, name=name)
    holder_a.acquire()
    holder_b.acquire()

    contender = portalocker.BoundedSemaphore(
        2,
        name=name,
        timeout=0.3,
        check_interval=0.05,
    )
    start: float = time.perf_counter()
    with pytest.raises(portalocker.AlreadyLocked):
        contender.acquire()
    elapsed: float = time.perf_counter() - start
    assert elapsed >= 0.29, (
        'fail_when_locked=True must wait out the whole timeout first'
    )

    holder_a.release()
    holder_b.release()


def test_bounded_semaphore_returns_none_after_timeout() -> None:
    """With ``fail_when_locked=False`` a full semaphore must keep retrying
    for the whole timeout and then return ``None``, the documented legacy
    contract (every other lock raises).
    """
    name: str = str(random.random())
    holder = portalocker.BoundedSemaphore(1, name=name)
    holder.acquire()

    contender = portalocker.BoundedSemaphore(
        1,
        name=name,
        timeout=0.1,
        check_interval=0.01,
    )
    result = contender.acquire(fail_when_locked=False)
    assert result is None

    holder.release()


def test_bounded_semaphore_double_acquire_raises_and_keeps_slot() -> None:
    """Acquiring an instance that already holds a slot must raise
    ``LockException`` without consuming a second slot or dropping the
    first one.
    """
    name: str = str(random.random())
    semaphore = portalocker.BoundedSemaphore(2, name=name)
    first = semaphore.acquire()
    assert first is not None

    with pytest.raises(portalocker.LockException, match='Already locked'):
        semaphore.acquire()

    # The held slot survived the failed call untouched.
    assert semaphore.lock is first

    # Only one of the two slots is consumed: a competitor still gets one.
    competitor = portalocker.BoundedSemaphore(2, name=name, timeout=0)
    assert competitor.acquire() is not None

    competitor.release()
    semaphore.release()

    # After release the instance is usable again.
    assert semaphore.acquire() is not None
    semaphore.release()


def test_bounded_semaphore_double_acquire_guard_survives_optimization(
    tmp_path: pathlib.Path,
) -> None:
    """The double-acquire guard must hold under ``python -O`` as well: a
    plain ``assert`` would be stripped and silently leak the first slot.
    """
    script: str = textwrap.dedent(
        f"""\
        import portalocker

        semaphore = portalocker.NamedBoundedSemaphore(
            2, name='guard', directory={str(tmp_path)!r}
        )
        first = semaphore.acquire()
        if first is None:
            raise RuntimeError('first acquire failed')
        try:
            semaphore.acquire()
        except portalocker.LockException:
            pass
        else:
            raise RuntimeError('missing LockException under optimized Python')
        if semaphore.lock is not first:
            raise RuntimeError('the held slot was dropped or replaced')
        """,
    )
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, '-O', '-c', script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout == ''
    assert completed.stderr == ''


def test_bounded_semaphore_deprecation_warning_names_the_caller() -> None:
    """The default-name ``DeprecationWarning`` must point at the caller,
    not at portalocker's own source, so it deduplicates per call site.
    """
    with pytest.warns(DeprecationWarning) as records:
        portalocker.BoundedSemaphore(1)

    assert len(records) == 1
    assert records[0].filename == __file__


def test_bounded_semaphore_concurrent_acquire_takes_one_slot(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two threads sharing one instance must end up with exactly one slot.

    The first thread is parked between locking its slot file and
    publishing it on ``self.lock``, which is the historical lost-update
    window: the second thread's sweep then took a second slot and its
    publication was overwritten, leaking the slot until garbage
    collection. The publication re-checks the guard atomically now, so
    whichever thread publishes second releases its extra slot and raises
    instead of overwriting.
    """
    semaphore = portalocker.NamedBoundedSemaphore(
        2,
        name='concurrent-acquire',
        directory=str(tmp_path),
        timeout=0,
        fail_when_locked=False,
    )

    real_acquire = utils.Lock.acquire
    calls: list[int] = []
    parked = threading.Event()
    resume = threading.Event()

    def gated_acquire(
        self: utils.Lock,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> typing.Any:
        calls.append(threading.get_ident())
        result = real_acquire(self, *args, **kwargs)
        if len(calls) == 1:
            parked.set()
            assert resume.wait(timeout=5), 'the sweep was never resumed'
        return result

    monkeypatch.setattr(utils.Lock, 'acquire', gated_acquire)

    outcomes: dict[str, object] = {}

    def first_acquire() -> None:
        try:
            outcomes['first'] = semaphore.acquire()
        except portalocker.LockException as error:
            outcomes['first'] = error

    def second_acquire() -> None:
        try:
            outcomes['second'] = semaphore.acquire()
        except portalocker.LockException as error:
            outcomes['second'] = error

    first_thread = threading.Thread(target=first_acquire)
    first_thread.start()
    assert parked.wait(timeout=5), 'the first sweep never locked a slot'

    second_thread = threading.Thread(target=second_acquire)
    second_thread.start()
    # The second thread's whole sweep and publication run inside the
    # parked window, so its outcome settles before the first resumes.
    second_thread.join(timeout=5)
    assert not second_thread.is_alive()

    resume.set()
    first_thread.join(timeout=5)
    assert not first_thread.is_alive()

    values = [outcomes['first'], outcomes['second']]
    winners = [value for value in values if isinstance(value, utils.Lock)]
    losers = [
        value
        for value in values
        if isinstance(value, portalocker.LockException)
    ]
    assert len(winners) == 1, f'expected exactly one slot holder: {outcomes}'
    assert len(losers) == 1, (
        f'the losing thread kept a slot instead of raising: {outcomes}'
    )
    # Compared through a local so mypy does not narrow the attribute
    # and declare the release assertions below unreachable.
    held_after: utils.Lock | None = semaphore.lock
    assert held_after is winners[0]

    monkeypatch.undo()
    semaphore.release()
    assert semaphore.lock is None
    for filename in semaphore.get_filenames():
        probe = portalocker.Lock(
            str(filename),
            timeout=0,
            fail_when_locked=True,
        )
        probe.acquire()
        probe.release()


def test_bounded_semaphore_concurrent_release_releases_once(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent ``release()`` calls must give the slot back once.

    The first releaser is parked inside the slot lock's release. The
    second call must find the slot already claimed and return without
    touching the slot lock again, instead of running a second teardown
    against a filehandle the winner is still tearing down.
    """
    semaphore = portalocker.NamedBoundedSemaphore(
        1,
        name='concurrent-release',
        directory=str(tmp_path),
        timeout=0,
    )
    semaphore.acquire()

    real_release = utils.Lock.release
    calls: list[int] = []
    parked = threading.Event()
    resume = threading.Event()

    def gated_release(self: utils.Lock) -> None:
        calls.append(threading.get_ident())
        if len(calls) == 1:
            parked.set()
            assert resume.wait(timeout=5), 'the release was never resumed'
        real_release(self)

    monkeypatch.setattr(utils.Lock, 'release', gated_release)

    releaser = threading.Thread(target=semaphore.release)
    releaser.start()
    assert parked.wait(timeout=5), 'the release never reached the slot lock'

    semaphore.release()
    assert len(calls) == 1, 'the losing release touched the slot lock'
    assert semaphore.lock is None

    resume.set()
    releaser.join(timeout=5)
    assert not releaser.is_alive()
    assert len(calls) == 1


@pytest.mark.parametrize('interrupt', [KeyboardInterrupt, SystemExit])
def test_bounded_semaphore_interrupt_after_slot_lock_releases_slot(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt: type[BaseException],
) -> None:
    """An interrupt between locking a slot and publishing it must roll
    the slot back.

    Without the rollback the OS lock is stranded on a local that only
    refcount garbage collection releases, and a pinned traceback (this
    test keeps the ExceptionInfo alive) blocks the slot indefinitely -
    the same window ``PidFileLock`` closed for its sidecar in 4.2.0.
    The interrupt is staged at the acquire return, the first bytecode
    of the window.
    """
    semaphore = portalocker.NamedBoundedSemaphore(
        1,
        name='interrupt-slot',
        directory=str(tmp_path),
        timeout=0,
    )
    real_acquire = utils.Lock.acquire

    def interrupted_acquire(
        self: utils.Lock,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> typing.Any:
        real_acquire(self, *args, **kwargs)
        raise interrupt('signal right after the slot lock')

    monkeypatch.setattr(utils.Lock, 'acquire', interrupted_acquire)
    with pytest.raises(interrupt) as excinfo:
        semaphore.try_lock(semaphore.get_filenames())
    monkeypatch.undo()

    # The pinned exception keeps the try_lock frame, and with it the
    # local slot Lock, alive: refcount collection cannot help here.
    assert excinfo.traceback is not None
    assert semaphore.lock is None

    contender = portalocker.NamedBoundedSemaphore(
        1,
        name='interrupt-slot',
        directory=str(tmp_path),
        timeout=0,
    )
    assert contender.acquire() is not None
    contender.release()


def test_bounded_semaphore_interrupt_after_publication_unpublishes(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupt right after the publication un-publishes and rolls
    back.

    The publication window closes on the state-lock exit, so an
    interrupt landing there leaves ``self.lock`` already set. The
    rollback must recognise its own publication (identity, exactly as
    ``PidFileLock`` guards its sidecar rollback), clear it and release
    the slot, so the instance neither believes it holds a released
    slot nor strands the OS lock.
    """
    semaphore = portalocker.NamedBoundedSemaphore(
        1,
        name='interrupt-published-slot',
        directory=str(tmp_path),
        timeout=0,
    )
    real_state_lock = semaphore._state_lock
    fired: list[bool] = []

    class InterruptingStateLock:
        def __enter__(self) -> None:
            real_state_lock.acquire()

        def __exit__(self, *exc_info: typing.Any) -> None:
            real_state_lock.release()
            if not fired:
                fired.append(True)
                raise KeyboardInterrupt('signal at the publication exit')

    monkeypatch.setattr(semaphore, '_state_lock', InterruptingStateLock())
    with pytest.raises(KeyboardInterrupt) as excinfo:
        semaphore.try_lock(semaphore.get_filenames())
    monkeypatch.undo()

    assert excinfo.traceback is not None
    assert fired == [True]
    assert semaphore.lock is None

    contender = portalocker.NamedBoundedSemaphore(
        1,
        name='interrupt-published-slot',
        directory=str(tmp_path),
        timeout=0,
    )
    assert contender.acquire() is not None
    contender.release()


def test_bounded_semaphore_try_lock_guards_direct_calls(
    tmp_path: pathlib.Path,
) -> None:
    """``try_lock`` is public API, so its own entry guard must refuse a
    second slot for an instance that already holds one even when the
    ``acquire`` wrapper (and its identical guard) is bypassed.
    """
    semaphore = portalocker.NamedBoundedSemaphore(
        2,
        name='direct-try-lock',
        directory=str(tmp_path),
        timeout=0,
    )
    assert semaphore.try_lock(semaphore.get_filenames()) is True
    with pytest.raises(portalocker.LockException, match='Already locked'):
        semaphore.try_lock(semaphore.get_filenames())
    semaphore.release()
    assert semaphore.lock is None
