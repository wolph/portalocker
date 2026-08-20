"""Command-line interface for the ``portalocker`` package.

Invoked as ``python -m portalocker``. The only subcommand is `combine`,
which bundles the package into a single vendorable ``portalocker.py``
file; see `combine` for what that transform does and why it exists.

Every file this module reads while doing that -- the package's own
modules, ``README.rst`` and ``LICENSE`` -- is opened with
``encoding='ascii'`` (see `_read_file` and `combine`). That means the
entire ``portalocker`` source tree, plus ``README.rst`` and ``LICENSE``,
must stay ASCII-only: a non-ASCII character in any of them makes this
module raise `UnicodeDecodeError` instead of silently mis-decoding it.
``portalocker_tests/test_combined.py`` asserts the combined output can
still be produced, which is how a stray non-ASCII character elsewhere in
the package would be caught.
"""

from __future__ import annotations

import argparse
import io
import logging
import pathlib
import re
import subprocess
import sys
import typing

base_path = pathlib.Path(__file__).parent.parent
src_path = base_path / 'portalocker'
dist_path = base_path / 'dist'
_default_output_path = base_path / 'dist' / 'portalocker.py'

_NAMES_RE = re.compile(r'(?P<names>[^()]+)$')
_RELATIVE_IMPORT_RE = re.compile(
    r'^from \.(?P<from>.*?) import (?P<paren>\(?)(?P<names>[^()]+)$',
)
_USELESS_ASSIGNMENT_RE = re.compile(r'^(?P<name>\w+) = \1\n$')
_TYPE_CHECKING_RE = re.compile(r'if (?:typing\.)?TYPE_CHECKING\s*:')

_TEXT_TEMPLATE = """'''
{}
'''

"""

logger = logging.getLogger(__name__)


def _log_decode_error(
    path: pathlib.Path,
    exception: UnicodeDecodeError,
) -> None:
    """Log a snippet of `path` around the bytes that failed to decode.

    Shared by every ASCII read in this module (see `_read_text_ascii`),
    so a stray non-ASCII byte in a source file, ``README.rst`` or
    ``LICENSE`` always produces an error naming the offending file and
    showing the surrounding context instead of a bare traceback.

    Args:
        path: The file that failed to decode, named in the log message.
        exception: The decode failure. Its ``args`` carry the undecoded
            bytes and the offset range of the offending byte(s).
    """
    _, text, start_byte, end_byte, error = exception.args

    offset = 100
    snippet = text[max(start_byte - offset, 0) : end_byte + offset]
    logger.error(  # noqa: TRY400
        f'Invalid encoding for {path}: {error} at byte '
        f'({start_byte}:{end_byte})\n'
        f'Snippet: {snippet!r}'
    )


def _read_text_ascii(path: pathlib.Path) -> str:
    """Read `path` as ASCII, logging a clear error on failure.

    Args:
        path: The file to read.

    Returns:
        The file's text, decoded as ASCII with universal newlines.

    Raises:
        UnicodeDecodeError: `path` contains a byte that is not valid
            ASCII. The offending snippet is logged through
            `_log_decode_error` before this is re-raised.
        SystemExit: `path` does not exist. The missing file is named in
            a logged error and the exit status is 1, instead of the raw
            ``FileNotFoundError`` traceback the CLI used to die with.
    """
    try:
        return path.read_text(encoding='ascii')
    except UnicodeDecodeError as exception:
        _log_decode_error(path, exception)
        raise
    except FileNotFoundError as exception:
        logger.error(f'Input file not found: {path}')  # noqa: TRY400
        raise SystemExit(1) from exception


def main(argv: typing.Sequence[str] | None = None) -> None:
    """Parse CLI arguments and dispatch to the selected subcommand.

    Currently the only subcommand is ``combine`` (see `combine`).

    Args:
        argv: Argument vector to parse, excluding the program name.
            Forwarded to `argparse.ArgumentParser.parse_args`, so it
            defaults to `sys.argv[1:]` when `None`. Tests pass an
            explicit list, e.g. ``['combine', '--output-file', ...]``,
            instead of relying on `sys.argv`.

    Raises:
        SystemExit: Argument parsing failed, or no subcommand was
            given -- the ``combine`` subparser is marked required.

    Example:
        .. code-block:: console

            $ python -m portalocker combine
            $ python -m portalocker combine --output-file dist/portalocker.py
    """
    parser = argparse.ArgumentParser()

    subparsers = parser.add_subparsers(required=True)
    combine_parser = subparsers.add_parser(
        'combine',
        help='Combine all Python files into a single unified `portalocker.py` '
        'file for easy distribution',
    )
    combine_parser.add_argument(
        '--output-file',
        '-o',
        type=pathlib.Path,
        default=_default_output_path,
    )

    combine_parser.set_defaults(func=combine)
    args = parser.parse_args(argv)
    args.func(args)


def _read_file(  # noqa: C901
    path: pathlib.Path,
    seen_files: set[pathlib.Path],
) -> typing.Iterator[str]:
    """Yield ``path``'s lines with relative imports inlined recursively.

    Reads ``path`` with ``encoding='ascii'`` (see the module docstring
    for why every source file under `src_path` must be ASCII). A line
    matching a relative import -- ``from .foo import bar`` or its
    parenthesised multi-line form -- is not emitted as-is; instead, the
    module(s) it names are read in turn (recursively, through this same
    function) and their lines are yielded in its place, re-indented to
    match if the import itself was indented. This is what lets `combine`
    produce a single file: a relative import would not resolve once the
    referenced module is no longer a separate file.

    ``if TYPE_CHECKING:`` blocks are the one exception: they are emitted
    verbatim rather than inlined, so the runtime guard their else-branch
    relies on is preserved. ``from __future__ import ...`` lines are
    dropped, since `combine` writes a single
    ``from __future__ import annotations`` once for the whole output.

    Args:
        path: The module to read.
        seen_files: Modules already emitted, keyed by resolved path.
            Mutated in place: `path` is added to it on entry, and a
            module reached through an inlined import is skipped (this
            generator yields nothing and returns) once its path is
            already present here, so a module referenced from more than
            one place is only inlined once.

    Yields:
        Lines of `path`'s source, in order, with relative imports
        replaced by the recursively inlined source of the module(s)
        they named.

    Raises:
        UnicodeDecodeError: `path` contains a byte that is not valid
            ASCII. The offending snippet is logged before this is
            re-raised.
    """
    if path in seen_files:
        return

    names: set[str] = set()
    seen_files.add(path)
    paren = False
    from_ = None
    in_type_checking = False
    type_checking_indent = 0
    for line in io.StringIO(_read_text_ascii(path)):
        if '__future__' in line:
            continue

        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]

        # ``if TYPE_CHECKING:`` blocks are type-only (never executed at
        # runtime). Emit them verbatim without inlining: inlining their
        # relative imports would duplicate a module or break the guard
        # the else-branch relies on.
        if in_type_checking and (
            not stripped.strip() or len(indent) > type_checking_indent
        ):
            yield _clean_line(line, names)
            continue
        in_type_checking = False
        if not paren and _TYPE_CHECKING_RE.match(stripped):
            in_type_checking = True
            type_checking_indent = len(indent)
            yield _clean_line(line, names)
            continue

        if paren:
            if ')' in line:
                line = line.split(')', 1)[1]
                paren = False
                continue

            match = _NAMES_RE.match(line)
        else:
            match = _RELATIVE_IMPORT_RE.match(stripped)

        if match:
            if not paren:
                paren = bool(match.group('paren'))
                from_ = match.group('from')

            # An indented relative import (e.g. the optional-redis guard's
            # ``try`` block) is inlined re-indented so the module lands
            # inside the guard; a column-0 import inlines at module level.
            if from_:
                names.add(from_)
                yield from _reindent(
                    _read_file(src_path / f'{from_}.py', seen_files),
                    indent,
                )
            else:
                for name in match.group('names').split(','):
                    name = name.strip()
                    names.add(name)
                    yield from _reindent(
                        _read_file(src_path / f'{name}.py', seen_files),
                        indent,
                    )
        else:
            yield _clean_line(line, names)


def _clean_line(line: str, names: set[str]) -> str:
    r"""Strip inlined-module qualifiers and useless self-assignments.

    For example, with ``names={'constants'}``, the line
    ``x = constants.LockFlags.EXCLUSIVE\n`` becomes
    ``x = LockFlags.EXCLUSIVE\n``; with ``names=set()``, the line
    ``spam = spam\n`` becomes ``''``.

    Note:
        Not a doctest: pytest's ``--doctest-modules`` collector never
        imports files named ``__main__.py``, so an executable example
        here would silently never run.

    Args:
        line: A source line being emitted as-is (i.e. it did not itself
            match a relative import) into the combined output.
        names: Names of modules that `_read_file` has already inlined
            into the current output, for this line's source module.
            Used to turn a qualified reference such as
            ``constants.LockFlags`` into ``LockFlags``, since once a
            module has been inlined it is no longer a separate module
            to qualify names against.

    Returns:
        `line`, with any ``<name>.`` prefix removed for every `name` in
        `names`, and then with a resulting useless self-assignment
        (``spam = spam``, left behind when a qualified name turned out
        to equal its own unqualified form) removed entirely.
    """
    # Replace `some_import.spam` with `spam`
    if names:
        joined_names = '|'.join(names)
        line = re.sub(rf'\b({joined_names})\.', '', line)

    # Replace useless assignments (e.g. `spam = spam`)
    return _USELESS_ASSIGNMENT_RE.sub('', line)


def _reindent(
    lines: typing.Iterator[str], indent: str
) -> typing.Iterator[str]:
    """Prefix each non-blank inlined line with ``indent``.

    Used when a relative import is itself indented (e.g. the optional-redis
    guard's ``try`` block): the inlined module is emitted at that indentation
    so it stays inside the guard while preserving its own relative structure.
    """
    if not indent:
        yield from lines
        return
    for line in lines:
        yield f'{indent}{line}' if line.strip() else line


def combine(args: argparse.Namespace) -> None:
    """Bundle the package into a single vendorable ``portalocker.py`` file.

    This is the ``combine`` subcommand's implementation (see `main`). It
    exists for projects that cannot add ``portalocker`` as a dependency
    -- for example a vendoring or single-file-deployment constraint --
    and instead copy one generated file with the same public API into
    their own tree.

    Starting from ``portalocker/__init__.py``, the package's modules are
    read with `_read_file`, which inlines every relative import
    recursively (so the result has none left) while leaving
    ``TYPE_CHECKING`` blocks intact. That includes the optional
    ``try: from .redis import RedisLock`` guard in ``__init__.py``:
    ``redis.py`` is inlined the same way as any other sibling module,
    re-indented to stay inside the ``try``, so the combined file binds
    `RedisLock` the same way the package does -- to the stub class that
    raises ``ImportError`` on construction if importing it fails.

    Every module under `src_path`, plus ``README.rst`` and ``LICENSE``,
    is read with ``encoding='ascii'`` (see the module docstring): all of
    them must be ASCII-only or this raises `UnicodeDecodeError`.
    ``README.rst`` and ``LICENSE`` are also written near the top of the
    output, each wrapped in a triple-quoted string, for reference; they
    are not the output file's docstring, since the single
    ``from __future__ import annotations`` line this function writes
    first has to stay the first statement.

    All inputs are read and assembled in memory first, and the output
    file is only opened once the assembly has succeeded. Writing first
    used to truncate ``--output-file`` before the first input was read,
    so a decode failure destroyed a pre-existing build. A plain write is
    deliberate here: ``--output-file`` legitimately overwrites its
    previous build, which is exactly the no-replace semantic
    `portalocker.utils.open_atomic` exists to forbid.

    If the ``ruff`` command is available, the output is formatted and
    lint-fixed in place; if not, that step is skipped with a warning.
    Either way, the output is then executed once with `sys.executable`.
    That run's exit status is not checked, so it surfaces problems in
    the log rather than failing `combine` itself.

    Args:
        args: Parsed CLI namespace; only `args.output_file` is used.
            Populated by `main` via the ``combine`` subparser, whose
            ``--output-file``/``-o`` option defaults to
            `_default_output_path` (``dist/portalocker.py``).

    Note:
        The combined file still needs the ``redis`` package installed
        for `RedisLock` to work: combining inlines the code that binds
        to ``redis``, not ``redis`` itself.
    """
    output_path: pathlib.Path = args.output_file

    # Read and assemble every input before touching the output file, so
    # a failure while reading (a non-ASCII byte, a missing file) cannot
    # truncate a pre-existing build.
    parts: list[str] = [
        # We're handling this separately because it has to be the first
        # import.
        'from __future__ import annotations\n',
        _TEXT_TEMPLATE.format(_read_text_ascii(base_path / 'README.rst')),
        _TEXT_TEMPLATE.format(_read_text_ascii(base_path / 'LICENSE')),
    ]
    seen_files: set[pathlib.Path] = set()
    parts.extend(_read_file(src_path / '__init__.py', seen_files))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(''.join(parts))

    logger.info(f'Wrote combined file to {output_path}')
    # Run ruff if available. If not then just run the file.
    try:
        subprocess.run(['ruff', 'format', str(output_path)], timeout=3)
        subprocess.run(
            ['ruff', 'check', '--fix', '--fix-only', str(output_path)],
            timeout=3,
        )
    except FileNotFoundError:
        logger.warning(
            'Ruff is not installed. Skipping linting and formatting step.'
        )
    subprocess.run([sys.executable, str(output_path)])


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    main()
