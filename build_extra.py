"""Build script automatically called from my build_and_release script"""

import argparse
import pathlib

from portalocker import __about__, __main__


class Args(argparse.Namespace):
    def __init__(self) -> None:
        filename = f'portalocker-{__about__.__version__}.py'
        self.output_file: pathlib.Path = __main__.dist_path / filename


__main__.combine(Args())
