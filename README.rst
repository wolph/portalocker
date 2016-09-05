############################################
portalocker - Cross-platform locking library
############################################

.. image:: https://travis-ci.org/WoLpH/portalocker.svg?branch=master
    :alt: Linux Test Status
    :target: https://travis-ci.org/WoLpH/portalocker
    
.. image:: https://img.shields.io/appveyor/ci/WoLpH/portalocker.svg
    :alt: Windows Tests Status
    :target: https://ci.appveyor.com/project/WoLpH/portalocker

.. image:: https://coveralls.io/repos/WoLpH/portalocker/badge.svg?branch=master
    :alt: Coverage Status
    :target: https://coveralls.io/r/WoLpH/portalocker?branch=master

.. image:: https://landscape.io/github/WoLpH/portalocker/master/landscape.png
   :target: https://landscape.io/github/WoLpH/portalocker/master
   :alt: Code Health

Overview
--------

Portalocker is a library to provide an easy API to file locking.

Originally created as a Python Recipe by Jonathan Feinberg and  Lowell Alleman
http://code.activestate.com/recipes/65203-portalocker-cross-platform-posixnt-api-for-flock-s/

The module is currently maintained by Rick van Hattem <Wolph@wol.ph>.
The project resides at https://github.com/WoLpH/portalocker . Bugs and feature
requests can be submitted there. Patches are also very welcome.

Links
-----

* Documentation
    - http://portalocker.readthedocs.org/en/latest/
* Source
    - https://github.com/WoLpH/portalocker
* Bug reports 
    - https://github.com/WoLpH/portalocker/issues
* Package homepage
    - https://pypi.python.org/pypi/portalocker
* My blog
    - http://w.wol.ph/

Examples
--------

To make sure your cache generation scripts don't race, use the `Lock` class:

>>> import portalocker
>>> with portalocker.Lock('somefile', timeout=1) as fh:
    print >>fh, 'writing some stuff to my cache...'

To customize the opening and locking a manual approach is also possible:

>>> import portalocker
>>> file = open('somefile', 'r+')
>>> portalocker.lock(file, portalocker.LOCK_EX)
>>> file.seek(12)
>>> file.write('foo')
>>> file.close()

There is no explicit need to unlock the file as it is automatically unlocked
after `file.close()`. If you still feel the need to manually unlock a file
than you can do it like this:

>>> portalocker.unlock(file)

Do note that your data might still be in a buffer so it is possible that your
data is not available until you `flush()` or `close()`.

Changelog
---------

See CHANGELOG file

License
-------

see the LICENSE file

