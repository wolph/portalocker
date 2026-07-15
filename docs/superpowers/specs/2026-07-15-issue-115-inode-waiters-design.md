# Issue 115 Inode Waiters Design

## Goal

Prevent POSIX `TemporaryFileLock` and `PidFileLock` waiters from returning
success while they hold an obsolete inode, without changing the observable
behavior of portalocker 3.3.0.

## Compatibility Contract

Backwards compatibility with the current PyPI release takes priority over
changing the lock-file lifecycle:

- Public constructors, arguments, return values, timeout behavior, and
  exceptions remain unchanged.
- `TemporaryFileLock` continues to create its pathname while held and remove
  it on ordinary release and finalization.
- `PidFileLock` continues to remove both its PID file and sidecar lock file.
- Windows behavior remains unchanged because Windows does not allow a locked
  file to be unlinked.
- `TemporaryFileLock` does not gain a persistent sidecar or leave a new
  filesystem artifact.

This preserves public behavior for applications upgrading from portalocker
3.3.0. It does not promise safe simultaneous coordination between upgraded
and legacy processes: portalocker 3.3.0 waiters do not perform inode
revalidation, so new code cannot force them to reject an obsolete descriptor.

## Root Cause

On POSIX, a waiter can open a lock file before the current holder releases it.
If release unlinks the pathname, the waiter retains a descriptor for the old
inode. A third process can then recreate the pathname and lock a new inode. If
the waiter returns after locking the old inode, both processes believe they
hold the same named lock.

The current branch already contains the intended compatibility-preserving
defense: after acquiring a native lock, `_acquire_verified()` compares the
descriptor inode with the inode currently named by the path. A missing or
different path makes the acquisition stale; the code releases that descriptor
and retries within the configured timeout.

## Selected Design

Keep inode revalidation as the coordination mechanism. An acquisition on
POSIX succeeds only after all of these conditions hold:

1. The process opens and natively locks a file descriptor.
2. The lock pathname exists.
3. `fstat()` on the descriptor and `stat()` on the pathname identify the same
   inode.

If either pathname check fails, release the stale native lock and retry using
the existing timeout and polling contract. If the deadline expires or
`fail_when_locked` requires an immediate failure, surface the existing
`AlreadyLocked` behavior. Do not add a persistent lock file, new public API,
or platform-specific cleanup contract.

Apply the same invariant to the `PidFileLock` sidecar because it has the same
open-wait-unlink race.

## Implementation Scope

The production implementation is already present on the current branch. Add
the missing end-to-end regression before deciding whether production changes
are necessary. Change production code only if that regression demonstrates a
remaining violation of the selected invariant.

Correct the changelog attribution at the same time:

- Associate issue #115 with the split-inode waiter fix.
- Keep the stale-object release fix documented, but do not attribute that
  separate problem to #115.

## Regression Test

Add an event-driven, POSIX-only multiprocessing regression covering both
`TemporaryFileLock` and `PidFileLock`:

1. The parent acquires the named lock.
2. A waiter process opens the current native lock inode and signals that the
   descriptor is open before attempting acquisition.
3. The parent releases its lock only after receiving that signal.
4. The waiter returns from `acquire()` and keeps its lock held.
5. A third acquisition against the same pathname must raise `AlreadyLocked`.
6. For `TemporaryFileLock`, and for the `PidFileLock` sidecar, the waiter's
   descriptor inode must match the inode currently named by the lock path.
7. Events release the waiter and all child processes are joined and checked
   for clean exit.

Use process events and queues instead of timing sleeps. Run the regression on
Linux and macOS; skip it on Windows where unlinking a locked file is not
possible.

The test must fail against the portalocker 3.3.0 release behavior by allowing
the third acquisition, then pass against the implementation on this branch.

## Verification

- Run the new focused multiprocessing regression.
- Run the complete pytest suite with its configured 100% coverage threshold.
- Run the repository's configured static checks relevant to changed Python
  and documentation files.
- Confirm the worktree diff contains only issue #115 tests, any demonstrated
  production correction, and the changelog attribution update.

## Out of Scope

- Changing `TemporaryFileLock` to retain its lock file after release.
- Adding a permanent sidecar to `TemporaryFileLock`.
- Guaranteeing coordination with concurrently running legacy portalocker
  processes.
- Refactoring unrelated locking behavior or dependency configuration.
