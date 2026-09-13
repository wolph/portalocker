"""Run the Markdown README's examples as published, outside the checkout."""

import pathlib
import re
import subprocess
import sys

import pytest

README: pathlib.Path = pathlib.Path(__file__).parents[1] / 'README.md'
EXAMPLES: list[str] = re.findall(
    r'^```python\n(.*?)^```$',
    README.read_text(encoding='ascii'),
    flags=re.MULTILINE | re.DOTALL,
)


def test_readme_contains_executable_examples() -> None:
    """Removing or relabelling every example must not silently skip tests."""
    assert EXAMPLES


def test_ci_badge_tracks_master() -> None:
    """The status image and its destination both refer to master."""
    workflow: str = (
        'https://github.com/wolph/portalocker/actions/workflows/ci.yml'
    )
    badges: list[tuple[str, str]] = re.findall(
        r'\[!\[CI\]\((.*?)\)\]\((.*?)\)',
        README.read_text(encoding='ascii'),
    )
    assert badges == [
        (
            f'{workflow}/badge.svg?branch=master',
            f'{workflow}?query=branch%3Amaster',
        )
    ]


@pytest.mark.parametrize('source', EXAMPLES)
def test_readme_python_example(source: str, tmp_path: pathlib.Path) -> None:
    result: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, '-I', '-c', source],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    if 'report.txt' in source:
        assert (tmp_path / 'report.txt').read_text() == 'Report complete.\n'
    else:
        assert result.stdout.splitlines() == [
            'The first holder still owns the lock.',
            'The lock is available after release.',
        ]
