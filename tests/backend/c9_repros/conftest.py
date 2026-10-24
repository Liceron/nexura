"""Release unreachable SQLite connections before isolated C9 descriptor checks."""

import gc
import os

import pytest


@pytest.fixture(autouse=True)
def private_database_fixture_directories():
    # Round 7 requires operator-controlled DB ancestors. Deliberately unsafe
    # permission tests set their exact bits explicitly and remain unaffected.
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.fixture(autouse=True)
def release_unreferenced_sqlite_connections():
    # Connection context managers do not close their descriptors. Earlier suite
    # modules may leave unreachable cycles; collecting them preserves the actual
    # live-connection test cases while avoiding dependence on worker scheduling.
    gc.collect()
