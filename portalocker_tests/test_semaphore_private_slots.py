from __future__ import annotations

import copy
import pathlib
import pickle
import typing

import pytest

import portalocker


class _StateLayout:
    __slots__ = ('__dict__', '__weakref__')


class _LayoutSemaphore(portalocker.BoundedSemaphore, _StateLayout):
    extra: str


def test_transfer_preserves_mixed_dictionary_layout() -> None:
    original = _LayoutSemaphore(1, name='layout')
    original.extra = 'keep'
    restored = copy.copy(original)
    assert restored.extra == 'keep'
    assert restored.__dict__ is not original.__dict__
    assert restored._state_lock is not original._state_lock


class _PrivateSemaphore(portalocker.BoundedSemaphore):
    __slots__ = ('__value', '__value__')

    def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        super().__init__(*args, **kwargs)
        self.__value = 'parent'
        self.__value__ = 'dunder'

    def parent_value(self) -> str:
        return self.__value


class _ChildSemaphore(_PrivateSemaphore):
    __slots__ = ('__value',)

    def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        super().__init__(*args, **kwargs)
        self.__value = 'child'

    def child_value(self) -> str:
        return self.__value


class ___(portalocker.BoundedSemaphore):  # noqa: N801 - Python does not mangle this class name
    __slots__ = ('__value',)

    def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        super().__init__(*args, **kwargs)
        self.__value = 'unmangled'

    def value(self) -> str:
        return self.__value


@pytest.mark.parametrize('method', ['pickle', 'copy', 'deepcopy'])
def test_transfer_preserves_private_slots(
    tmp_path: pathlib.Path, method: str
) -> None:
    original = _ChildSemaphore(1, name='private', directory=str(tmp_path))
    if method == 'pickle':
        restored = pickle.loads(pickle.dumps(original))
    elif method == 'copy':
        restored = copy.copy(original)
    else:
        restored = copy.deepcopy(original)
    assert restored.parent_value() == 'parent'
    assert restored.child_value() == 'child'
    assert restored.__value__ == 'dunder'
    assert original.parent_value() == 'parent'
    assert original.child_value() == 'child'
    assert restored.lock is None
    assert restored._state_lock is not original._state_lock


def test_transfer_keeps_private_name_when_class_is_all_underscores(
    tmp_path: pathlib.Path,
) -> None:
    original = ___(1, name='underscores', directory=str(tmp_path))
    restored = copy.copy(original)
    assert restored.value() == 'unmangled'
