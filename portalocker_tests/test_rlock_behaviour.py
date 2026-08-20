import subprocess
import sys

import pytest

import portalocker


def test_rlock_acquire_release_count(tmpdir):
    """Test that RLock acquire/release count works as expected."""
    tmpfile = tmpdir.join('test_rlock_acquire_release_count.lock')
    lock = portalocker.RLock(tmpfile)
    # Twice acquire
    h = lock.acquire()
    assert not h.closed
    lock.acquire()
    assert not h.closed

    # Two release
    lock.release()
    assert not h.closed
    lock.release()
    assert h.closed


def test_rlock_acquire_release(tmpdir):
    """Test that RLock acquire/release works as expected."""
    tmpfile = tmpdir.join('test_rlock_acquire_release.lock')
    lock = portalocker.RLock(tmpfile)
    lock2 = portalocker.RLock(tmpfile, fail_when_locked=False)

    lock.acquire()  # acquire lock when nobody is using it
    with pytest.raises(portalocker.LockException):
        # another party should not be able to acquire the lock
        lock2.acquire(timeout=0.01)

    # Now acquire again
    lock.acquire()

    lock.release()  # release the lock
    lock.release()  # second release does nothing


def test_rlock_inconsistent_state_raises(tmpdir):
    """A positive acquire count without a filehandle raises, never returns."""
    tmpfile = tmpdir.join('test_rlock_inconsistent_state.lock')
    lock = portalocker.RLock(tmpfile)
    # Forge the broken state: the count claims the lock is held, but no
    # filehandle exists to hand out.
    lock._acquire_count = 1
    with pytest.raises(portalocker.LockException, match='filehandle'):
        lock.acquire()
    # The failed acquire must not have bumped the count.
    assert lock._acquire_count == 1
    lock._acquire_count = 0


def test_rlock_inconsistent_state_raises_optimized(tmpdir):
    """The guard survives ``python -O``, which strips bare asserts."""
    tmpfile = str(tmpdir.join('test_rlock_optimized.lock'))
    code: str = (
        'import sys\n'
        '# A bare assert would be stripped by -O itself, so check\n'
        '# the optimize flag for real.\n'
        'if sys.flags.optimize < 1:\n'
        '    raise SystemExit("expected python -O")\n'
        'import portalocker\n'
        f'lock = portalocker.RLock({tmpfile!r})\n'
        'lock._acquire_count = 1\n'
        'try:\n'
        '    fh = lock.acquire()\n'
        'except portalocker.LockException:\n'
        '    sys.stdout.write("raised")\n'
        'else:\n'
        '    sys.stdout.write(f"returned {fh!r}")\n'
    )
    result = subprocess.run(
        [sys.executable, '-O', '-c', code],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        cwd=str(tmpdir),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'raised'
