<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/wolph/portalocker/develop/docs/_static/portalocker-dark.svg">
  <img src="https://raw.githubusercontent.com/wolph/portalocker/develop/docs/_static/portalocker-light.svg" alt="portalocker" width="300" height="64">
</picture>

# Coordinate access. Keep your code simple.

[![CI](https://github.com/wolph/portalocker/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/wolph/portalocker/actions/workflows/ci.yml?query=branch%3Amaster)
[![PyPI](https://img.shields.io/pypi/v/portalocker)](https://pypi.org/project/portalocker/)
[![Python versions](https://img.shields.io/pypi/pyversions/portalocker)](https://pypi.org/project/portalocker/)
[![Documentation](https://readthedocs.org/projects/portalocker/badge/?version=latest)](https://portalocker.readthedocs.io/en/latest/)
[![Licence](https://img.shields.io/pypi/l/portalocker)](https://github.com/wolph/portalocker/blob/develop/LICENSE)

Portalocker provides file locks for Python on Linux, macOS and Windows.
Use a context manager to coordinate access between processes, a semaphore to
limit concurrent workers, or a Redis lock to coordinate across machines.
Python 3.10 or later is required.

[Get started](https://portalocker.readthedocs.io/en/latest/quickstart.html) |
[Choose a lock](https://portalocker.readthedocs.io/en/latest/lock-types.html) |
[API reference](https://portalocker.readthedocs.io/en/latest/api/index.html) |
[Changelog](https://portalocker.readthedocs.io/en/latest/changelog.html)

## Install

```console
python -m pip install portalocker
```

Exclusive file locks work without extra dependencies. Install the optional
`redis` extra for Redis locks, or `win32` for shared file locks on Windows:

```console
python -m pip install "portalocker[redis]"
python -m pip install "portalocker[win32]"
```

Redis locks need a Redis server. Shared file locks on Windows without `pywin32`
raise an `ImportError` explaining which extra to install.

## Start with a file lock

Use the file handle inside the context:

```python
import portalocker

with portalocker.Lock('report.txt', 'a', timeout=5) as fh:
    fh.write('Report complete.\n')
```

`Lock` waits up to five seconds to acquire the lock. Leaving the context
releases the lock and closes the file, including when the body raises an
exception. Give every participating process the same file path.

When another holder prevents acquisition, `AlreadyLocked` lets you handle
contention separately from other failures:

```python
import portalocker

with portalocker.Lock('worker.lock', 'a', timeout=1):
    try:
        with portalocker.Lock('worker.lock', 'a', timeout=0):
            print('The second holder entered.')
    except portalocker.AlreadyLocked:
        print('The first holder still owns the lock.')

with portalocker.Lock('worker.lock', 'a', timeout=0):
    print('The lock is available after release.')
```

The second lock cannot enter while the first is held. Once the outer context
exits, a new holder can acquire it. This example uses the default file-locking
backend. See [platform behaviour](https://portalocker.readthedocs.io/en/latest/platforms.html)
before choosing a different POSIX locking primitive.

## Choose a lock

| Your task | Lock | Guide |
| --- | --- | --- |
| Coordinate access to a file | `Lock` | [File locking](https://portalocker.readthedocs.io/en/latest/quickstart.html) |
| Acquire again through the same lock instance | `RLock` | [Reentrant locks](https://portalocker.readthedocs.io/en/latest/lock-types.html#rlock) |
| Limit the number of concurrent workers | `NamedBoundedSemaphore` | [Workers and slots](https://portalocker.readthedocs.io/en/latest/lock-types.html#namedboundedsemaphore) |
| Inspect a worker PID or run a singleton | `PidFileLock` | [PID files](https://portalocker.readthedocs.io/en/latest/lock-types.html#pidfilelock) |
| Coordinate processes across machines | `RedisLock` | [Redis locks](https://portalocker.readthedocs.io/en/latest/redis.html) |

Give cooperating semaphores the same explicit `name` and `directory`. Keep
their slot files in a directory that will survive while workers hold them.

## Locking details that matter

- Unix file locks are advisory. Every participating process must acquire a
  lock to honour them. Linux removed its old mandatory locking feature in
  [kernel 5.15](https://man7.org/linux/man-pages/man2/fcntl_locking.2.html).
- Network filesystems have their own locking and buffering behaviour. You may
  need `fh.flush()` followed by `os.fsync(fh.fileno())` before releasing a lock.
  Read the [platform guide](https://portalocker.readthedocs.io/en/latest/platforms.html)
  and test on the filesystem you deploy to.
- A `PidFileLock` context is for inspection by default. Its body runs even
  when another process holds the lock, returning that holder's PID. `None`
  means this process acquired it. An unreadable or corrupt PID for a held
  lock raises `AlreadyLocked`. Use `fail_closed()` when the body must only
  run after acquisition. See the [PID examples](https://portalocker.readthedocs.io/en/latest/lock-types.html#pidfilelock).
- A Redis lock uses a pub/sub subscription. Redis releases ownership when it
  removes the subscription. Detecting a broken connection or a network
  partition can take time, and the holder observes loss separately. The
  [Redis guide](https://portalocker.readthedocs.io/en/latest/redis.html#losing-a-lock)
  covers `lost`, `ensure_held()`, health checks and optional fencing tokens.
  Fencing only protects writes when the resource checks the token.

For single-file vendoring, see the
[CLI guide](https://portalocker.readthedocs.io/en/latest/cli.html).
For upgrades from 3.x, see the
[migration guide](https://portalocker.readthedocs.io/en/latest/migration.html).

## Contributing

Portalocker is maintained by [Rick van Hattem](https://github.com/wolph).
[Bug reports and feature requests](https://github.com/wolph/portalocker/issues)
and patches are welcome. See the
[contribution guide](https://github.com/wolph/portalocker/blob/develop/CONTRIBUTING.md)
for development and test commands.

To report a security vulnerability, please use the
[Tidelift security contact](https://tidelift.com/security).
Tidelift will coordinate the fix and disclosure.

## Support

portalocker is maintained by [Rick van Hattem](https://github.com/wolph) in
his own time. Most of that time goes on the platforms you are not running, so
the lock behaves the same on Windows, BSD and NFS as it does on your laptop.

If it saved you an afternoon, a tip covers an hour of issue triage:
[Ko-fi](https://ko-fi.com/wolph_gh) or
[GitHub Sponsors](https://github.com/sponsors/wolph).

If your company funds its dependencies, this package is on
[thanks.dev](https://thanks.dev/u/gh/wolph).

[![Support on Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/wolph_gh)

## Licence

Portalocker is distributed under the BSD 3-Clause licence. See
[LICENSE](https://github.com/wolph/portalocker/blob/develop/LICENSE).
