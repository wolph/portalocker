# Release Error Policy Design

## Goal

Make `Lock.release()` cleanup failures observable for callers that explicitly
request strict handling, while preserving the behavior of portalocker 3.3.0 by
default.

## Compatibility Contract

`Lock` gains a keyword-only `raise_on_release_error: bool = False` constructor
argument. The default remains best-effort cleanup: ordinary exceptions from
unlocking or closing are suppressed, both operations are attempted, and the
file-handle reference is cleared. Existing callers therefore retain the current
PyPI behavior without source changes.

Strict behavior is opt-in. Setting `raise_on_release_error=True` makes cleanup
failures observable without changing acquisition, successful release, repeated
release, finalizer, or subclass defaults.

## Release Behavior

`Lock.release()` captures ordinary `Exception` instances raised by
`portalocker.unlock()` and the file handle's `close()` method. Closing is still
attempted after an unlock failure. After both attempts, `self.fh` is cleared as
it is today.

With the default policy, captured errors are discarded. With strict handling,
the first captured error is raised unchanged. If both operations fail, the
close error is attached as the explicit cause of the unlock error, retaining
both exception objects on every supported Python version.

Handling of `BaseException` subclasses remains unchanged. They are not captured
as release errors, matching the existing `contextlib.suppress(Exception)`
boundary.

## Context Manager Behavior

`Lock.__exit__()` applies the same policy. With no protected-body exception, a
strict release error propagates normally. When the protected body and strict
release both fail, the protected-body exception remains the exception observed
by the caller. The release error is retained as secondary exception context;
on Python versions providing `BaseException.add_note()`, a concise cleanup note
is also added for traceback visibility.

Default context-manager behavior remains unchanged because release errors are
suppressed unless strict handling was requested.

`LockBase.__exit__()` is not changed, avoiding behavioral changes to Redis,
semaphore, or third-party `LockBase` subclasses.

## Finalization and Subclasses

`LockBase.__del__()` continues suppressing release failures because garbage
collection cannot safely surface them.

`RLock`, `TemporaryFileLock`, and `PidFileLock` retain their current constructor
signatures and default behavior. The new policy is initially exposed only on
`Lock`, matching issue #117 and minimizing public API expansion. Subclass
support can be added separately if a concrete need emerges.

## Documentation

The `Lock` class docstring documents the new argument and strict-mode semantics.
The 4.0 changelog records that release errors can now be observed explicitly
while the default remains compatible with portalocker 3.3.0.

## Tests

Focused tests cover:

- default suppression when unlock and close both fail;
- strict unlock-only failure, including the close attempt;
- strict close-only failure;
- strict dual failure with unlock primary and close as its cause;
- strict context exit without a protected-body exception;
- strict context exit preserving a protected-body exception as primary;
- successful and repeated release behavior remaining unchanged.

The full test and static-check suite verifies no regression outside the focused
release paths.
