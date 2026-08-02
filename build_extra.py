"""Build script automatically called from my build_and_release script."""

import argparse
import pathlib

from portalocker import __about__, __main__


class Args(argparse.Namespace):
    """Stand-in for a parsed `argparse.Namespace`.

    Supplies `__main__.combine()` with a version-stamped output path,
    so the release script can call it without building a real parser.
    """

    def __init__(self) -> None:
        """Set `output_file` to `portalocker-<version>.py`."""
        filename = f'portalocker-{__about__.__version__}.py'
        self.output_file: pathlib.Path = __main__.dist_path / filename


__main__.combine(Args())
