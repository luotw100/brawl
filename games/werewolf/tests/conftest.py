from __future__ import annotations

import pytest

from werewolf.registry import Registry, load_registry


@pytest.fixture
def registry() -> Registry:
    return load_registry()

