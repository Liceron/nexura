# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""TEST ONLY: let a file-backed workspace repository run against a Postgres test database.

In Postgres mode the service refuses a ``WorkspaceRepository`` loaded from a SQLite file
(``refuse_sqlite_loaded_repository``): copying a stale file on every start would bring back
removed members. Many existing tests build such a repository on purpose. Inside
``allow_sqlite_repository_import()`` that repository copies its records once into the
database instead, so those tests also run under ``--service-backend=postgres``.

``allow_sqlite_studio_store()`` does the same for a SQLite ``StudioStore`` passed to
``create_api_app(store=...)`` in Postgres mode (MVP WP3): many existing tests pass their own
store; inside the block it is used as given (its runs stay in that file) instead of being
refused.

There is no environment variable for either, and neither has an effect when ``K_SERVICE`` or
``CLOUD_RUN_JOB`` is set (Cloud Run). Never import this module from service code.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from nexura_runtime.service import storage_backend
from nexura_runtime.service.workspaces import repository


@contextmanager
def allow_sqlite_repository_import() -> Iterator[None]:
    """Within the block, file-backed repositories may bind to Postgres (tests only)."""
    previous = repository._TEST_SQLITE_IMPORT
    repository._TEST_SQLITE_IMPORT = True
    try:
        yield
    finally:
        repository._TEST_SQLITE_IMPORT = previous


@contextmanager
def allow_sqlite_studio_store() -> Iterator[None]:
    """Within the block, a caller-supplied SQLite StudioStore is accepted in Postgres mode."""
    previous = storage_backend._TEST_SQLITE_STUDIO_STORE
    storage_backend._TEST_SQLITE_STUDIO_STORE = True
    try:
        yield
    finally:
        storage_backend._TEST_SQLITE_STUDIO_STORE = previous
