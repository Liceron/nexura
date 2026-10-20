"""Fixtures for connector tests (offline; see connector_helpers.py)."""

from __future__ import annotations

import pytest
from connector_helpers import CASES, Negatives


@pytest.fixture
def negatives() -> Negatives:
    """The shared negative-path suite."""
    return Negatives()


@pytest.fixture(params=CASES)
def neg_case(request: pytest.FixtureRequest) -> str:
    """Parametrizes a test over every shared negative case name."""
    return str(request.param)
