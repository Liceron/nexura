# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""PostgreSQL backends: run and session storage, the execution journal, durable SDK agent
checkpoints (``PostgresCheckpointStore``), durable workflow state
(``PostgresWorkflowStore``), the trigger job queue (``PostgresTriggerStore``) and the pgvector
store.

Distributed as ``nexura-postgres`` (it depends on ``nexura-runtime`` and asyncpg). The runtime's
``get_storage_repository("postgresql://...")`` returns ``PostgresRepository`` when this package
is installed (``nexura_runtime.modules.optional_packages``); without it, that call raises
``MissingDependencyError`` naming ``pip install nexura-postgres``.
"""

from __future__ import annotations

from nexura_postgres.checkpoint_store import PostgresCheckpointStore
from nexura_postgres.execution_store import PostgresExecutionStore
from nexura_postgres.pgvector import PgVectorStoreAdapter
from nexura_postgres.repository import PostgresRepository
from nexura_postgres.trigger_store import PostgresTriggerStore
from nexura_postgres.workflow_store import PostgresWorkflowStore

__all__ = [
    "PgVectorStoreAdapter",
    "PostgresCheckpointStore",
    "PostgresRepository",
    "PostgresExecutionStore",
    "PostgresTriggerStore",
    "PostgresWorkflowStore",
]
