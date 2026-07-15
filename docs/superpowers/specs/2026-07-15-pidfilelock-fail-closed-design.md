# PidFileLock Fail-Closed Context Design

## Goal

Add an opt-in `PidFileLock` context-manager API that never executes the
protected body unless this process owns the lock, while preserving the current
inspection-oriented context behavior.

## Existing Behavior

`with PidFileLock(...) as holder_pid:` remains unchanged:

- `holder_pid is None` means this process acquired the lock.
- An integer means another process holds the lock, but the context body still
  runs so callers can inspect the holder.

This behavior remains the default for compatibility.

## Public API

`PidFileLock.fail_closed()` returns a context manager whose `__enter__` return
type is `None`:

```python
lock = portalocker.PidFileLock('worker.pid')

with lock.fail_closed():
    run_singleton_worker()
```

Entering this context calls the existing `PidFileLock.acquire()` path. The body
runs only after acquisition succeeds. Context exit delegates to the lock's
existing release behavior.

The existing context remains typed as `int | None`; the fail-closed context is
typed as `AbstractContextManager[None]`. This distinguishes inspection and
ownership contexts without making `PidFileLock` generic or adding a public
subclass.

## Contention and Error Data

`AlreadyLocked` gains a backward-compatible, optional
`holder_pid: int | None` attribute. Existing construction and exception
handling continue to work unchanged.

When fail-closed entry catches `AlreadyLocked`, it reads the PID file and stores
the result in `holder_pid` before re-raising the same exception. Missing,
unreadable, empty, or invalid PID files produce `holder_pid is None`. Other
acquisition errors propagate unchanged.

The adapter does not suppress exceptions from the protected body. It releases
the lock through the existing `PidFileLock.__exit__` path only after successful
entry.

## Implementation Boundaries

- `PidFileLock` owns acquisition, PID reading, release, and the public
  `fail_closed()` factory method.
- A private context-manager adapter owns only fail-closed entry and exit
  semantics.
- `AlreadyLocked` owns the structured holder PID attribute.
- No constructor flags, public subclasses, retry behavior, or PID liveness
  checks are added.

## Testing

Tests will verify:

- Existing inspection contexts still enter and return the competing PID.
- A fail-closed context acquires, enters, and cleans up normally.
- A contended fail-closed context raises before its body executes.
- The raised `AlreadyLocked` exposes the competing PID when readable.
- Invalid or unavailable PID content produces `holder_pid is None`.
- Static type checks retain `int | None` for inspection contexts and infer
  `None` for fail-closed contexts.
- The complete test and type-check suites remain clean.

## Documentation

The README will show inspection and fail-closed examples side by side. The
4.0.0 changelog will record the new opt-in behavior and structured exception
attribute.
