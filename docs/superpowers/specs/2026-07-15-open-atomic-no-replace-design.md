# `open_atomic()` No-Replace Publication Design

## Context

`open_atomic()` writes to a temporary file in the destination directory,
flushes and synchronizes that file, then publishes it with `os.rename()`.
The function checks at entry that the destination is absent, but that check is
an `assert`. Optimized Python removes it. More importantly, a destination
created after context entry is silently replaced by `os.rename()` on POSIX,
while Windows normally rejects the same race. Issue #114 reports this
platform-dependent data-loss window.

## Compatibility Contract

The function remains a create-if-absent API. Its public signature, yielded file
object, binary and text modes, parent-directory creation, flush, `fsync()`, and
successful publication behavior remain unchanged.

For compatibility with existing callers, a destination that already exists at
context entry continues to raise `AssertionError` with the existing message.
The check becomes explicit so it remains active under `python -O`.

If another actor creates the destination after context entry, that actor wins.
Publication raises `FileExistsError`, leaves the destination untouched, and
removes the unpublished temporary file. This matches existing Windows race
behavior and replaces unsafe POSIX replacement behavior.

If a POSIX filesystem cannot create hard links, `open_atomic()` propagates the
relevant `OSError`. It must never fall back to a replacement operation because
that would violate the contract and reintroduce the data-loss race. Windows
retains its existing atomic `os.rename()` publication, which already refuses an
existing destination and supports filesystems without hard links.

## Publication Design

The existing temporary file remains in the destination directory, ensuring the
temporary file and destination are on the same filesystem. After the context
body completes, `open_atomic()` flushes and synchronizes the temporary file as
it does today.

Publication uses `os.rename(temporary_name, destination)` on Windows because
Windows rename is atomic and refuses an existing destination. POSIX publication
uses `os.link(temporary_name, destination)`, which atomically refuses an
existing destination; the temporary filename is then removed, leaving the
destination as the sole name for the synchronized inode. The existing
`finally` cleanup remains responsible for removing the temporary filename on
success and failure.

The entry check becomes an explicit conditional that raises `AssertionError`.
No new option or replacement mode is added.

## Error Handling

- Existing destination at entry: raise the existing `AssertionError` before
  creating a temporary file.
- Destination created during the context: propagate `FileExistsError` from the
  platform publication primitive and preserve the concurrent destination.
- Other publication failures: propagate the original `OSError` without
  attempting a weaker publication method.
- Context-body, flush, or `fsync()` failure: retain current behavior and do not
  publish the destination.
- All publication outcomes: best-effort removal of the temporary filename,
  matching current cleanup behavior.

## Testing

Add focused tests for these externally visible behaviors:

1. A destination present at entry raises `AssertionError`, including under
   optimized Python, and its contents remain unchanged.
2. A destination created inside the context causes publication to raise
   `FileExistsError`.
3. The concurrent winner's contents remain unchanged after the failed
   publication.
4. The temporary file is removed after failed publication.
5. A normal publication still produces the written bytes and leaves no
   temporary file behind.
6. A non-`FileExistsError` publication failure propagates and still removes the
   temporary file.
7. Windows selects `os.rename()` while POSIX selects `os.link()`.

Run the complete test suite and the repository's configured type and lint
checks after the focused tests pass.

## Documentation

Update the `open_atomic()` docstring to state that the destination must not
exist at entry or publication time. Add a changelog item describing the race
fix and its `FileExistsError` outcome.

## Non-Goals

- Adding an atomic-replacement mode.
- Adding platform-specific native rename bindings.
- Changing locking APIs or other temporary-file utilities.
- Guaranteeing POSIX operation on filesystems without hard-link support.
