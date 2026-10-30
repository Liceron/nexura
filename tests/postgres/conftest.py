# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Every test here gets a database of its own, dropped afterwards.

Several store tests write to ``public`` through ``NEXURA_TEST_POSTGRES_URI``; pointing that
variable at a throwaway database keeps the shared test database clean and lets the files run
in parallel. Without the URI this is a no-op and the tests skip as before.
"""

import pytest


@pytest.fixture(autouse=True)
def _own_postgres_database(isolated_postgres_database: str | None) -> None:
    return None
