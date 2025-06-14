import dataclasses
import multiprocessing
import platform
import time
import typing

import pytest

import portalocker
from portalocker import LockFlags


@dataclasses.dataclass(order=True)
class LockResult:
    """Helper dataclass for multiprocessing lock results."""

    exception_class: typing.Union[type, None] = None
    exception_message: typing.Union[str, None] = None
    exception_repr: typing.Union[str, None] = None


def lock(
    filename: str,
    fail_when_locked: bool,
    flags: LockFlags,
    timeout: float = 0.1,
    keep_locked: float = 0.05,
) -> LockResult:
    """Helper function for multiprocessing lock tests."""
    try:
        with portalocker.Lock(
            filename,
            timeout=timeout,
            fail_when_locked=fail_when_locked,
            flags=flags,
        ):
            time.sleep(keep_locked)
            return LockResult()

    except Exception as exception:
        return LockResult(
            type(exception),
            str(exception),
            repr(exception),
        )


def shared_lock(filename, **kwargs):
    """Helper for shared lock in multiprocessing tests."""
    with portalocker.Lock(
        filename,
        timeout=0.1,
        fail_when_locked=False,
        flags=LockFlags.SHARED | LockFlags.NON_BLOCKING,
    ):
        time.sleep(0.2)
        return True


def shared_lock_fail(filename, **kwargs):
    """Helper for shared lock fail in multiprocessing tests."""
    with portalocker.Lock(
        filename,
        timeout=0.1,
        fail_when_locked=True,
        flags=LockFlags.SHARED | LockFlags.NON_BLOCKING,
    ):
        time.sleep(0.2)
        return True


def exclusive_lock(filename, **kwargs):
    """Helper for exclusive lock in multiprocessing tests."""
    with portalocker.Lock(
        filename,
        timeout=0.1,
        fail_when_locked=False,
        flags=LockFlags.EXCLUSIVE | LockFlags.NON_BLOCKING,
    ):
        time.sleep(0.2)
        return True


@pytest.mark.parametrize('fail_when_locked', [True, False])
@pytest.mark.skipif(
    'pypy' in platform.python_implementation().lower(),
    reason='pypy3 does not support the multiprocessing test',
)
@pytest.mark.flaky(reruns=5, reruns_delay=1)
def test_shared_processes(tmpfile, fail_when_locked):
    """Test that shared locks work correctly across processes."""
    flags = LockFlags.SHARED | LockFlags.NON_BLOCKING
    with multiprocessing.Pool(processes=2) as pool:
        args = tmpfile, fail_when_locked, flags
        results = pool.starmap_async(lock, 2 * [args])

        for result in results.get(timeout=1.5):
            if result.exception_class is not None:
                raise result.exception_class  # type: ignore[reportGeneratlTypeIssues]
            assert result == LockResult()


@pytest.mark.parametrize('fail_when_locked', [True, False])
@pytest.mark.parametrize(
    'locker',
    [
        # The actual locker param is handled by the test runner
    ],
    indirect=True,
)
@pytest.mark.skipif(
    'pypy' in platform.python_implementation().lower(),
    reason='pypy3 does not support the multiprocessing test',
)
@pytest.mark.flaky(reruns=5, reruns_delay=1)
def test_exclusive_processes(
    tmpfile: str,
    fail_when_locked: bool,
    locker: typing.Callable[..., typing.Any],
) -> None:
    """Test that exclusive locks work correctly across processes."""
    flags = LockFlags.EXCLUSIVE | LockFlags.NON_BLOCKING

    with multiprocessing.Pool(processes=2) as pool:
        # Submit tasks individually
        result_a = pool.apply_async(lock, [tmpfile, fail_when_locked, flags])
        result_b = pool.apply_async(lock, [tmpfile, fail_when_locked, flags])

        try:
            a = result_a.get(timeout=1.2)  # Wait for 'a' with timeout
        except multiprocessing.TimeoutError:
            a = None

        try:
            # Lower timeout since we already waited with `a`
            b = result_b.get(timeout=0.6)  # Wait for 'b' with timeout
        except multiprocessing.TimeoutError:
            b = None

        assert a or b
        # Make sure a is always filled
        if a is None:
            b, a = a, b

        assert a is not None

        if b:
            assert b is not None

            assert not a.exception_class or not b.exception_class
            assert issubclass(
                a.exception_class or b.exception_class,  # type: ignore[arg-type]
                portalocker.LockException,
            )
        else:
            assert not a.exception_class
