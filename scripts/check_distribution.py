"""Install and exercise both release archives outside the source checkout."""

from __future__ import annotations

import argparse
import email.message
import email.parser
import os
import pathlib
import subprocess
import sys
import tarfile
import tempfile
import zipfile

_SMOKE: str = """
import importlib.metadata
import importlib.resources
import importlib.util
import pathlib
import sys

import portalocker

assert portalocker.__version__ == sys.argv[1]
assert importlib.metadata.version('portalocker') == sys.argv[1]
assert importlib.resources.files('portalocker').joinpath('py.typed').is_file()
assert pathlib.Path(portalocker.__file__).is_relative_to(sys.prefix)
with portalocker.Lock('smoke.txt', 'w', timeout=0) as fh:
    fh.write('release smoke test')
    try:
        with portalocker.Lock('smoke.txt', 'a', timeout=0):
            raise AssertionError('contended lock acquired')
    except portalocker.AlreadyLocked:
        pass
assert fh.closed
assert pathlib.Path('smoke.txt').read_text() == 'release smoke test'
with portalocker.Lock('smoke.txt', 'a', timeout=0):
    pass
if sys.argv[2] == 'base':
    assert importlib.util.find_spec('redis') is None
    try:
        portalocker.RedisLock('missing-extra')
    except ImportError as exc:
        assert 'redis' in str(exc)
    else:
        raise AssertionError('missing Redis extra went unnoticed')
else:
    from portalocker.redis import RedisLock
    assert portalocker.RedisLock is RedisLock
    lock = RedisLock('installed-extra')
    lock.release()
"""


def _run(command: list[str], cwd: pathlib.Path) -> None:
    """Run a check and propagate its failure without hiding its output."""
    subprocess.run(command, cwd=cwd, check=True)


def _check_archives(wheel: pathlib.Path, sdist: pathlib.Path) -> str:
    """Require matching metadata and the files needed to use each archive."""
    with zipfile.ZipFile(wheel) as archive:
        members: set[str] = set(archive.namelist())
        assert 'portalocker/py.typed' in members
        metadata_files: list[str] = [
            name for name in members if name.endswith('.dist-info/METADATA')
        ]
        assert len(metadata_files) == 1
        metadata: bytes = archive.read(metadata_files[0])
    message: email.message.Message = email.parser.BytesParser().parsebytes(
        metadata
    )
    version: str = str(message['Version'])
    assert message['Name'] == 'portalocker'
    assert message['Description-Content-Type'] == 'text/markdown'
    assert message.get_payload()
    with tarfile.open(sdist) as archive:
        names: set[str] = {
            name.partition('/')[2] for name in archive.getnames()
        }
        required: set[str] = {
            'README.md',
            'LICENSE',
            'conftest.py',
            'build_extra.py',
            'portalocker/py.typed',
            'portalocker_tests/test_readme.py',
            'docs/index.rst',
            'docs/conf.py',
            'tox.toml',
            'ruff.toml',
            'scripts/check_distribution.py',
        }
        assert required <= names, required - names
        assert not any(name.startswith('docs/_build/') for name in names)
        pkg_info = archive.extractfile(f'portalocker-{version}/PKG-INFO')
        assert pkg_info is not None
        with pkg_info:
            source_metadata: email.message.Message = (
                email.parser.BytesParser().parse(pkg_info)
            )
        assert source_metadata['Version'] == version
        assert source_metadata['Description-Content-Type'] == 'text/markdown'
    return version


def _check_install(archive: pathlib.Path, version: str) -> None:
    """Test base installation and the Redis extra in a fresh environment."""
    with tempfile.TemporaryDirectory(prefix='portalocker-dist-') as directory:
        root: pathlib.Path = pathlib.Path(directory)
        environment: pathlib.Path = root / 'venv'
        python: pathlib.Path = environment / (
            'Scripts/python.exe' if os.name == 'nt' else 'bin/python'
        )
        _run(
            ['uv', 'venv', '--python', sys.executable, str(environment)], root
        )
        _run(
            ['uv', 'pip', 'install', '--python', str(python), str(archive)],
            root,
        )
        _run([str(python), '-I', '-c', _SMOKE, version, 'base'], root)
        _run(
            [
                'uv',
                'pip',
                'install',
                '--python',
                str(python),
                f'portalocker[redis] @ {archive.as_uri()}',
            ],
            root,
        )
        _run([str(python), '-I', '-c', _SMOKE, version, 'redis'], root)


def main() -> None:
    """Validate the wheel and source distribution in the given directory."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(__doc__)
    parser.add_argument('dist', type=pathlib.Path)
    args: argparse.Namespace = parser.parse_args()
    directory: pathlib.Path = args.dist.resolve()
    wheels: list[pathlib.Path] = list(directory.glob('portalocker-*.whl'))
    sources: list[pathlib.Path] = list(directory.glob('portalocker-*.tar.gz'))
    assert len(wheels) == len(sources) == 1, 'Expected one wheel and one sdist'
    _run(
        [
            sys.executable,
            '-m',
            'twine',
            'check',
            '--strict',
            str(wheels[0]),
            str(sources[0]),
        ],
        directory,
    )
    version: str = _check_archives(wheels[0], sources[0])
    for archive in (wheels[0], sources[0]):
        _check_install(archive, version)


if __name__ == '__main__':
    main()
