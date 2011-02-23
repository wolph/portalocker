import sys

extra = {}
if sys.version_info >= (3, 0):
    extra.update(use_2to3=True)

try:
    from setuptools import setup, find_packages
except ImportError:
    from distutils.core import setup, find_packages

author = 'Rick van Hattem'
email = 'Rick.van.Hattem@Fawo.nl'
version = '0.3'
desc = '''Wraps the portalocker recipe for easy usage'''

setup(name='portalocker',
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
      test_suite='nose.collector',
      **extra
)

