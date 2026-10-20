# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""PostgreSQL backends for nexura-runtime: run and session storage, and the pgvector store.

Distributed as ``nexura-postgres`` (it depends on ``nexura-runtime`` and asyncpg). The runtime's
``get_storage_repository("postgresql://...")`` returns ``PostgresRepository`` when this package
is installed (``nexura_runtime.modules.optional_packages``); without it, that call raises
``MissingDependencyError`` naming ``pip install nexura-postgres``.
"""

from __future__ import annotations

from nexura_postgres.pgvector import PgVectorStoreAdapter
from nexura_postgres.repository import PostgresRepository

__all__ = ["PgVectorStoreAdapter", "PostgresRepository"]
