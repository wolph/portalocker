from __future__ import print_function

import re
import os
import setuptools


# To prevent importing about and thereby breaking the coverage info we use this
# exec hack
about = {}
with open('portalocker/__about__.py') as fp:
    exec(fp.read(), about)


test_requirements_file = os.path.join('tests', 'requirements.txt')
if os.path.isfile(test_requirements_file):
    with open(test_requirements_file) as fh:
        tests_require = fh.read().splitlines()
else:
    tests_require = ['pytest>=3.0']


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
            'Programming Language :: Python :: 2.6',
            'Programming Language :: Python :: 2.7',
            'Programming Language :: Python :: 3.3',
            'Programming Language :: Python :: 3.4',
            'Programming Language :: Python :: 3.5',
            'Programming Language :: Python :: Implementation :: CPython',
            'Programming Language :: Python :: Implementation :: PyPy',
        ],
        keywords='locking, locks, with statement, windows, linux, unix',
        author=about['__author__'],
        author_email=about['__email__'],
        url=about['__url__'],
        license='PSF',
        packages=setuptools.find_packages(exclude=['ez_setup', 'examples']),
        # zip_safe=False,
        platforms=['any'],
        cmdclass={
            'combine': Combine,
        },
        setup_requires=[
            'pytest-runner',
        ],
        tests_require=tests_require,
    )

