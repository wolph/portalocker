"""Build script automatically called from my build_and_release script"""

import argparse

from portalocker import __about__, __main__


class Args(argparse.Namespace):
    def __init__(self) -> None:
        filename = f'portalocker-{__about__.__version__}.py'
        self.output_file = (__main__.dist_path / filename).open('w')

    def __del__(self) -> None:
        self.output_file.close()


__main__.combine(Args())
