from __future__ import annotations

import argparse
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


def main(argv: typing.Sequence[str] | None = None) -> None:
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
    if path in seen_files:
        return

    names: set[str] = set()
    seen_files.add(path)
    paren = False
    from_ = None
    in_type_checking = False
    type_checking_indent = 0
    try:
        for line in path.open(encoding='ascii'):
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
    except UnicodeDecodeError as exception:  # pragma: no cover
        _, text, start_byte, end_byte, error = exception.args

        offset = 100
        snippet = text[start_byte - offset : end_byte + offset]
        logger.error(  # noqa: TRY400
            f'Invalid encoding for {path}: {error} at byte '
            f'({start_byte}:{end_byte})\n'
            f'Snippet: {snippet!r}'
        )
        raise


def _clean_line(line: str, names: set[str]) -> str:
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
    output_path: pathlib.Path = args.output_file
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open('w') as output_file:
        # We're handling this separately because it has to be the first
        # import.
        output_file.write('from __future__ import annotations\n')

        output_file.write(
            _TEXT_TEMPLATE.format(
                (base_path / 'README.rst').read_text(encoding='ascii')
            ),
        )
        output_file.write(
            _TEXT_TEMPLATE.format(
                (base_path / 'LICENSE').read_text(encoding='ascii')
            ),
        )

        seen_files: set[pathlib.Path] = set()
        for line in _read_file(src_path / '__init__.py', seen_files):
            output_file.write(line)

    logger.info(f'Wrote combined file to {output_path}')
    # Run ruff if available. If not then just run the file.
    try:  # pragma: no cover
        subprocess.run(['ruff', 'format', str(output_path)], timeout=3)
        subprocess.run(
            ['ruff', 'check', '--fix', '--fix-only', str(output_path)],
            timeout=3,
        )
    except FileNotFoundError:  # pragma: no cover
        logger.warning(
            'Ruff is not installed. Skipping linting and formatting step.'
        )
    subprocess.run([sys.executable, str(output_path)])


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    main()
