import sys
import setuptools
from setuptools.command.test import test as TestCommand

__package_name__ = 'portalocker'
__author__ = 'Rick van Hattem'
__email__ = 'wolph@wol.ph'
__version__ = '0.6.0'
__description__ = '''Wraps the portalocker recipe for easy usage'''
__url__ = 'https://github.com/WoLpH/portalocker'

extra = {}
if sys.version_info >= (3, 0):
    extra.update(use_2to3=True)


class PyTest(TestCommand):

    def finalize_options(self):
        TestCommand.finalize_options(self)
        self.test_args = ['tests']
        self.test_suite = True

    def run_tests(self):
        # import here, cause outside the eggs aren't loaded
        import pytest
        errno = pytest.main(self.test_args)
        sys.exit(errno)


if __name__ == '__main__':
    setuptools.setup(
        name=__package_name__,
        version=__version__,
        description=__description__,
        long_description=open('README.rst').read(),
        classifiers=[
            'Intended Audience :: Developers',
            'Programming Language :: Python',
            'Programming Language :: Python :: 2.6',
            'Programming Language :: Python :: 2.7',
            'Programming Language :: Python :: 3.3',
            'Programming Language :: Python :: 3.4',
            'Programming Language :: Python :: 3.5',
            'Programming Language :: Python :: Implementation :: CPython',
            'Programming Language :: Python :: Implementation :: PyPy',
        ],
        keywords='locking, locks, with statement, windows, linux, unix',
        author=__author__,
        author_email=__email__,
        url=__url__,
        license='PSF',
        packages=setuptools.find_packages(exclude=['ez_setup', 'examples']),
        zip_safe=False,
        platforms=['any'],
        cmdclass={'test': PyTest},
        **extra
    )

