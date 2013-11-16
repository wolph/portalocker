from setuptools import setup, find_packages
from setuptools.command.test import test as TestCommand
import sys

author = 'Rick van Hattem'
email = 'Rick.van.Hattem@Fawo.nl'
version = '0.4'
desc = '''Wraps the portalocker recipe for easy usage'''

extra = {}
if sys.version_info >= (3, 0):
    extra.update(use_2to3=True)


class PyTest(TestCommand):
    def finalize_options(self):
        TestCommand.finalize_options(self)
        self.test_args = ['tests']
        self.test_suite = True

    def run_tests(self):
        #import here, cause outside the eggs aren't loaded
        import pytest
        errno = pytest.main(self.test_args)
        sys.exit(errno)


setup(
    name='portalocker',
    version=version,
    description=desc,
    long_description=open('README.rest').read(),
    classifiers=[
        'Intended Audience :: Developers',
        'Programming Language :: Python',
        'Programming Language :: Python :: 2.5',
        'Programming Language :: Python :: 2.6',
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.1',
    ],
    keywords='locking, locks, with statement, windows, linux, unix',
    author=author,
    author_email=email,
    url='https://github.com/WoLpH/portalocker',
    license='PSF',
    packages=find_packages(exclude=['ez_setup', 'examples', 'tests']),
    zip_safe=False,
    platforms=['any'],
    cmdclass={'test': PyTest},
    **extra
)
