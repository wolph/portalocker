from __future__ import print_function
import re
import os
import sys
import setuptools
from setuptools.command.test import test as TestCommand
from distutils.version import LooseVersion
from setuptools import __version__ as setuptools_version


if LooseVersion(setuptools_version) < LooseVersion('38.3.0'):
    raise SystemExit(
        'Your `setuptools` version is old. '
        'Please upgrade setuptools by running `pip install -U setuptools` '
        'and try again.'
    )


# To prevent importing about and thereby breaking the coverage info we use this
# exec hack
about = {}
with open('portalocker/__about__.py') as fp:
    exec(fp.read(), about)


tests_require = [
    'flake8>=3.5.0',
    'pytest>=3.4.0',
    'pytest-cache>=1.0',
    'pytest-cov>=2.5.1',
    'pytest-flakes>=2.0.0',
    'pytest-pep8>=1.0.6',
    'sphinx>=1.7.1',
]


class PyTest(TestCommand):
    user_options = [('pytest-args=', 'a', "Arguments to pass to pytest")]

    def initialize_options(self):
        TestCommand.initialize_options(self)
        self.pytest_args = ''

    def run_tests(self):
        import shlex
        # import here, cause outside the eggs aren't loaded
        import pytest
        errno = pytest.main(shlex.split(self.pytest_args))
        sys.exit(errno)


class Combine(setuptools.Command):
    description = 'Build single combined portalocker file'
    relative_import_re = re.compile(r'^from \. import (?P<name>.+)$',
                                    re.MULTILINE)
    user_options = [
        ('output-file=', 'o', 'Path to the combined output file'),
    ]

    def initialize_options(self):
        self.output_file = os.path.join(
            'dist', '%(package_name)s_%(version)s.py' % dict(
                package_name=about['__package_name__'],
                version=about['__version__'].replace('.', '-'),
            ))

    def finalize_options(self):
        pass

    def run(self):
        dirname = os.path.dirname(self.output_file)
        if dirname and not os.path.isdir(dirname):
            os.makedirs(dirname)

        output = open(self.output_file, 'w')
        print("'''", file=output)
        with open('README.rst') as fh:
            output.write(fh.read().rstrip())
            print('', file=output)
            print('', file=output)

        with open('LICENSE') as fh:
            output.write(fh.read().rstrip())

        print('', file=output)
        print("'''", file=output)

        names = set()
        lines = []
        for line in open('portalocker/__init__.py'):
            match = self.relative_import_re.match(line)
            if match:
                names.add(match.group('name'))
                with open('portalocker/%(name)s.py' % match.groupdict()) as fh:
                    line = fh.read()
                    line = self.relative_import_re.sub('', line)

            lines.append(line)

        import_attributes = re.compile(r'\b(%s)\.' % '|'.join(names))
        for line in lines[:]:
            line = import_attributes.sub('', line)
            output.write(line)

        print('Wrote combined file to %r' % self.output_file)


if __name__ == '__main__':
    setuptools.setup(
        name=about['__package_name__'],
        version=about['__version__'],
        description=about['__description__'],
        long_description=open('README.rst').read(),
        classifiers=[
            'Intended Audience :: Developers',
            'Programming Language :: Python',
            'Programming Language :: Python :: 2.7',
            'Programming Language :: Python :: 3.3',
            'Programming Language :: Python :: 3.4',
            'Programming Language :: Python :: 3.5',
            'Programming Language :: Python :: 3.6',
            'Programming Language :: Python :: Implementation :: CPython',
            'Programming Language :: Python :: Implementation :: PyPy',
        ],
        keywords='locking, locks, with statement, windows, linux, unix',
        author=about['__author__'],
        author_email=about['__email__'],
        url=about['__url__'],
        license='PSF',
        packages=setuptools.find_packages(exclude=['ez_setup', 'examples', 'portalocker_tests']),
        # zip_safe=False,
        platforms=['any'],
        cmdclass={
            'combine': Combine,
            'test': PyTest,
        },
        install_requires=[
            'pywin32!=226; platform_system == "Windows"',
        ],
        tests_require=tests_require,
        extras_require=dict(
            docs=[
                'sphinx>=1.7.1',
            ],
            tests=tests_require,
        ),
    )

