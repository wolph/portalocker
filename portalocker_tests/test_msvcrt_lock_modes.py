"""The msvcrt ``LK_*`` fallback table must match ``<sys/locking.h>``.

``MsvcrtLocker`` resolves the five ``msvcrt`` locking-mode constants
through ``_resolve_msvcrt_lock_modes``, falling back to hardcoded values
for any constant the running interpreter's ``msvcrt`` does not expose.
Two properties matter and are pinned here:

* The fallback values are the documented ``<sys/locking.h>`` numbers. The
  pre-4.2.0 table was wrong (``LK_LOCK`` fell back to 0, which is
  ``LK_UNLCK``), so a "blocking lock" through the fallback would have
  issued an *unlock*.
* Resolving never mutates the passed module. The pre-4.2.0 code called
  ``setattr`` on the shared stdlib ``msvcrt`` module instead.

The resolver is platform-independent (it takes the module as an
argument), so these tests run everywhere, including the POSIX CI cells.
"""

import types

from portalocker import portalocker as ppl

#: The values from ``<sys/locking.h>``, the C header ``msvcrt.locking``
#: forwards its mode argument to.
_SYS_LOCKING_H: dict[str, int] = {
    'LK_UNLCK': 0,
    'LK_LOCK': 1,
    'LK_NBLCK': 2,
    'LK_RLCK': 3,
    'LK_NBRLCK': 4,
}


def test_fallback_table_matches_sys_locking_h():
    """A stub msvcrt without any LK_* constants gets the documented values."""
    stub = types.SimpleNamespace()

    modes: dict[str, int] = ppl._resolve_msvcrt_lock_modes(stub)

    assert modes == _SYS_LOCKING_H


def test_real_constants_take_precedence_over_fallbacks():
    """Constants the module does expose win over the fallback table."""
    # Deliberately different from the fallbacks to prove precedence.
    stub = types.SimpleNamespace(LK_LOCK=11, LK_NBLCK=12)

    modes: dict[str, int] = ppl._resolve_msvcrt_lock_modes(stub)

    assert modes['LK_LOCK'] == 11
    assert modes['LK_NBLCK'] == 12
    # The rest still comes from the fallback table.
    assert modes['LK_UNLCK'] == 0
    assert modes['LK_RLCK'] == 3
    assert modes['LK_NBRLCK'] == 4


def test_resolving_does_not_mutate_the_module():
    """The resolver must not setattr fallbacks onto the passed module."""
    stub = types.SimpleNamespace()

    ppl._resolve_msvcrt_lock_modes(stub)

    assert vars(stub) == {}
