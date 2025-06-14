import types
import typing

import pytest

import portalocker
from portalocker import LockFlags
from portalocker_tests.conftest import LOCKERS


# @pytest.mark.skipif(
#     os.name == 'nt',
#     reason='Locking on Windows requires a file object',
# )
@pytest.mark.parametrize('locker', LOCKERS, indirect=True)
def test_lock_fileno(tmpfile, locker):
    """Test that locking using fileno() works as expected."""
    with open(tmpfile, 'a+') as a, open(tmpfile, 'a+') as b:
        # Lock shared non-blocking
        flags = LockFlags.SHARED | LockFlags.NON_BLOCKING

        # First lock file a
        portalocker.lock(a, flags)

        # Now see if we can lock using fileno()
        portalocker.lock(b.fileno(), flags)


@pytest.mark.parametrize('locker', LOCKERS, indirect=True)
def test_locker_mechanism(tmpfile, locker):
    """Can we switch the locking mechanism?"""
    # We can test for flock vs lockf based on their different behaviour re.
    # locking the same file.
    with portalocker.Lock(tmpfile, 'a+', flags=LockFlags.EXCLUSIVE):
        # If we have lockf(), we cannot get another lock on the same file.
        fcntl: typing.Optional[types.ModuleType]
        try:
            import fcntl
        except ImportError:
            fcntl = None

        if fcntl is not None and locker is fcntl.lockf:  # type: ignore[attr-defined]
            portalocker.Lock(
                tmpfile,
                'r+',
                flags=LockFlags.EXCLUSIVE | LockFlags.NON_BLOCKING,
            ).acquire(timeout=0.1)
        # But with other lock methods we can't
        else:
            with pytest.raises(portalocker.LockException):
                portalocker.Lock(
                    tmpfile,
                    'r+',
                    flags=LockFlags.EXCLUSIVE | LockFlags.NON_BLOCKING,
                ).acquire(timeout=0.1)
