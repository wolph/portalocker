from __future__ import annotations

import copy
import multiprocessing
import pathlib
import pickle
import threading
import typing

import pytest

import portalocker
from portalocker import utils

if typing.TYPE_CHECKING:
    from multiprocessing.connection import _ConnectionBase as Connection
    from multiprocessing.process import BaseProcess
    from multiprocessing.synchronize import Event


def take_slot(
    semaphore: utils.BoundedSemaphore,
    pipe: Connection,
    release: Event | None,
) -> None:
    held = semaphore.acquire(timeout=0, fail_when_locked=False)
    try:
        pipe.send(held is not None)
        if held is not None and release is not None:
            assert release.wait(10), 'parent did not release the worker'
    finally:
        if held is not None:
            semaphore.release()
        pipe.close()


@pytest.mark.parametrize('name', [None, 'spawned-workers'])
def test_transferred_semaphore_limits_spawned_workers(
    tmp_path: pathlib.Path,
    name: str | None,
) -> None:
    context = multiprocessing.get_context('spawn')
    semaphore = portalocker.NamedBoundedSemaphore(
        2,
        name=name,
        directory=str(tmp_path),
        timeout=0,
    )
    release = context.Event()
    workers: list[BaseProcess] = []
    pipes: list[Connection] = []

    def start_worker(hold: bool) -> tuple[BaseProcess, Connection]:
        parent, child = context.Pipe(duplex=False)
        pipes.extend((parent, child))
        process = context.Process(
            target=take_slot,
            args=(semaphore, child, release if hold else None),
        )
        process.start()
        workers.append(process)
        child.close()
        assert parent.poll(10), 'worker did not report its acquisition'
        return process, parent

    try:
        for _ in range(2):
            _, parent = start_worker(hold=True)
            assert parent.recv() is True
        full, parent = start_worker(hold=False)
        assert parent.recv() is False
        full.join(10)
        assert full.exitcode == 0
        release.set()
        for process in workers:
            process.join(10)
            assert process.exitcode == 0
        available, parent = start_worker(hold=False)
        assert parent.recv() is True
        available.join(10)
        assert available.exitcode == 0
    finally:
        release.set()
        for process in workers:
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join(10)
        for pipe in pipes:
            pipe.close()
        semaphore.release()


def restore(
    semaphore: utils.BoundedSemaphore,
    method: str,
) -> utils.BoundedSemaphore:
    if method == 'copy':
        return copy.copy(semaphore)
    if method == 'deepcopy':
        return copy.deepcopy(semaphore)
    return typing.cast(
        utils.BoundedSemaphore,
        pickle.loads(pickle.dumps(semaphore)),
    )


@pytest.mark.parametrize('method', ['pickle', 'copy', 'deepcopy'])
def test_transferred_semaphore_keeps_configuration_and_contends(
    tmp_path: pathlib.Path,
    method: str,
) -> None:
    semaphore = portalocker.BoundedSemaphore(
        1,
        name='copied-settings',
        filename_pattern='{name}-{number}.slot',
        directory=str(tmp_path),
        timeout=0,
        check_interval=0.01,
        fail_when_locked=False,
    )
    restored = restore(semaphore, method)
    assert type(restored) is type(semaphore)
    assert restored is not semaphore
    assert restored.get_filenames() == semaphore.get_filenames()
    assert restored.timeout == semaphore.timeout
    assert restored.check_interval == semaphore.check_interval
    assert restored.fail_when_locked == semaphore.fail_when_locked
    assert restored.lock is None
    assert not list(tmp_path.iterdir())
    assert restored.acquire() is not None
    try:
        assert semaphore.acquire() is None
    finally:
        restored.release()
    assert semaphore.acquire() is not None
    semaphore.release()


@pytest.mark.parametrize('method', ['pickle', 'copy', 'deepcopy'])
def test_cannot_transfer_an_acquired_semaphore(
    tmp_path: pathlib.Path,
    method: str,
) -> None:
    semaphore = portalocker.NamedBoundedSemaphore(
        1,
        name='held-slot',
        directory=str(tmp_path),
        timeout=0,
    )
    competitor = portalocker.NamedBoundedSemaphore(
        1,
        name=semaphore.name,
        directory=str(tmp_path),
        timeout=0,
        fail_when_locked=False,
    )
    held = semaphore.acquire()
    assert held is not None
    try:
        with pytest.raises(TypeError, match='acquired semaphore'):
            restore(semaphore, method)
        assert semaphore.lock is held
        assert competitor.acquire() is None
    finally:
        semaphore.release()
    assert restore(semaphore, method).lock is None
    assert competitor.acquire() is not None
    competitor.release()


@pytest.mark.skipif(
    'fork' not in multiprocessing.get_all_start_methods(),
    reason='fork is unavailable',
)
def test_restored_semaphore_reinitializes_its_state_lock_after_fork(
    tmp_path: pathlib.Path,
) -> None:
    semaphore = portalocker.NamedBoundedSemaphore(
        1,
        directory=str(tmp_path),
        timeout=0,
    )
    restored = restore(semaphore, 'pickle')
    entered, finish = threading.Event(), threading.Event()

    def hold_state_lock() -> None:
        # The child must reset the lock inherited from another thread.
        with restored._state_lock:
            entered.set()
            assert finish.wait(10)

    thread = threading.Thread(target=hold_state_lock)
    thread.start()
    context = multiprocessing.get_context('fork')
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=take_slot, args=(restored, child, None))
    started = False
    try:
        assert entered.wait(10)
        process.start()
        started = True
        child.close()
        assert parent.poll(10), 'child inherited a locked state lock'
        assert parent.recv() is True
        process.join(10)
        assert process.exitcode == 0
    finally:
        finish.set()
        thread.join(10)
        if started:
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join(10)
        parent.close()
        child.close()


class _SlottedSemaphore(portalocker.BoundedSemaphore):
    __slots__ = ('extra',)

    def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        super().__init__(*args, **kwargs)
        self.extra: str = 'spam'


@pytest.mark.parametrize('method', ['pickle', 'copy', 'deepcopy'])
def test_transfer_keeps_subclass_slots(
    tmp_path: pathlib.Path,
    method: str,
) -> None:
    # 4.4.2 copied slot attributes through object.__reduce_ex__.
    semaphore = _SlottedSemaphore(
        1, name='slotted', directory=str(tmp_path), timeout=0
    )
    restored = restore(semaphore, method)
    assert isinstance(restored, _SlottedSemaphore)
    assert restored.extra == 'spam'
    assert restored.lock is None


class _BareSemaphore(portalocker.BoundedSemaphore):
    def __init__(self) -> None:  # noqa: D107 - skips the base constructor
        self.maximum: int = 2
        self.name: str = 'bare'
        self.filename_pattern: str = '{name}.{number:02d}.lock'
        self.directory: str = ''
        self.lock: utils.Lock | None = None


def test_copy_of_subclass_without_base_init() -> None:
    # 4.4.2 copied such an instance, which has no state lock to guard.
    semaphore = _BareSemaphore()
    restored = copy.copy(semaphore)
    assert isinstance(restored, _BareSemaphore)
    assert restored.maximum == 2
    assert isinstance(restored._state_lock, type(threading.RLock()))


class _UnassignedSlotSemaphore(portalocker.BoundedSemaphore):
    __slots__ = 'unset'


def test_transfer_skips_an_unassigned_slot(tmp_path: pathlib.Path) -> None:
    # A declared but never assigned slot has no value to carry over.
    semaphore = _UnassignedSlotSemaphore(
        1, name='unset-slot', directory=str(tmp_path), timeout=0
    )
    restored = copy.copy(semaphore)
    assert isinstance(restored, _UnassignedSlotSemaphore)
    assert not hasattr(restored, 'unset')


class _ObjectProtocolSemaphore(portalocker.BoundedSemaphore):
    """A subclass returning the object protocol state pair."""

    __slots__ = ('extra',)

    def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        super().__init__(*args, **kwargs)
        self.extra = 'spam'

    def __getstate__(self) -> typing.Any:
        mapping: dict[str, typing.Any] = dict(vars(self))
        mapping.pop('_state_lock', None)
        return mapping, {'extra': self.extra}


@pytest.mark.parametrize('method', ['copy', 'deepcopy', 'pickle'])
def test_setstate_accepts_the_object_protocol_tuple(
    tmp_path: pathlib.Path, method: str
) -> None:
    """A subclass handing back the stdlib's state pair is restored as before.

    4.4.2 rebuilt such a subclass through ``object.__reduce_ex__``. The
    base ``__setstate__`` that replaced it must take the same shape.
    """
    original = _ObjectProtocolSemaphore(
        1, name='protocol', directory=str(tmp_path)
    )
    if method == 'copy':
        restored = copy.copy(original)
    elif method == 'deepcopy':
        restored = copy.deepcopy(original)
    else:
        restored = pickle.loads(pickle.dumps(original))  # noqa: S301

    assert restored.extra == 'spam'
    assert restored.name == 'protocol'
    assert restored.lock is None
    assert restored._state_lock is not original._state_lock
