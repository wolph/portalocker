"""probe for the ruff annotation rendering, reverted immediately after."""

import pytest


def test_probe() -> None:
    """Probe that ruff annotates fixable and unfixable findings alike."""
    with pytest.raises(ValueError, match='probe.*value'):
        raise ValueError('probe value')
