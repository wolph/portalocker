1.0:

 * Complete code refactor.
   
   - Splitting of code in logical classes
   - 100% test coverage and change in API behaviour
   - The default behavior of the `Lock` class has changed to append instead of
     write/truncate.

0.6:

 * Added msvcrt support for Windows

0.5:

 * Python 3 support

0.4:

 * Fixing a few bugs, added coveralls support, switched to py.test and added
   100% test coverage.

    - Fixing exception thrown when fail_when_locked is true
    - Fixing exception "Lock object has no attribute '_release_lock'" when
      fail_when_locked is true due to the call to Lock._release_lock() which
      fails because _release_lock is not defined.

0.3:

 * Now actually returning the file descriptor from the `Lock` class

0.2:

 * Added `Lock` class to help prevent cache race conditions

0.1:

 * Initial release

