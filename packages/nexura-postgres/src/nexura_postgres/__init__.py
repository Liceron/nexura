# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""PostgreSQL backends: run and session storage, the execution journal, durable SDK agent
checkpoints (``PostgresCheckpointStore``), durable workflow state
(``PostgresWorkflowStore``), the trigger job queue (``PostgresTriggerStore``), the pgvector
store, the shared service ``Database`` with its migration ledger, and service workspaces and
resource ownership (``PostgresWorkspaceStore``), Studio workflows, runs and run events
(``PostgresStudioStore``), chat sessions (``PostgresSessionStore``), saved agents
(``PostgresAgentStore``), versioned skills (``PostgresSkillStore``), envelope-encrypted secrets
(``PostgresSecretStore``), the usage ledger (``PostgresUsageLedger``), the hybrid knowledge index
(``PostgresHybridKnowledgeIndex``: ``tsvector`` keyword search and pgvector similarity), evaluation
datasets, runs and traces (``PostgresEvaluationStore``), Studio evals (``PostgresStudioEvalStore``),
channel dedup keys and outbox receipts (``PostgresChannelStore``), integration tool registrations
(``PostgresIntegrationRegistrations``), and the blob reference scan for blob retention
(``referenced_blob_keys``).

Distributed as ``nexura-postgres`` (it depends on ``nexura-runtime`` and asyncpg). The runtime's
``get_storage_repository("postgresql://...")`` returns ``PostgresRepository`` when this package
is installed (``nexura_runtime.modules.optional_packages``); without it, that call raises
``MissingDependencyError`` naming ``pip install nexura-postgres``.
"""

from __future__ import annotations

from nexura_postgres.agents import PostgresAgentStore
from nexura_postgres.blobs import PostgresBlobBinding, referenced_blob_keys
from nexura_postgres.channels import PostgresChannelStore
from nexura_postgres.checkpoint_store import PostgresCheckpointStore
from nexura_postgres.db import Database
from nexura_postgres.evaluations import PostgresEvaluationStore, PostgresStudioEvalStore
from nexura_postgres.execution_store import PostgresExecutionStore
from nexura_postgres.integrations import PostgresIntegrationRegistrations
from nexura_postgres.knowledge import PostgresHybridKnowledgeIndex
from nexura_postgres.migrations import (
    MIGRATION_TIMEOUT,
    apply_migrations,
    migrate_all,
    migration_status,
)
from nexura_postgres.pgvector import PgVectorStoreAdapter
from nexura_postgres.repository import PostgresRepository
from nexura_postgres.secret_store import PostgresSecretStore
from nexura_postgres.sessions import PostgresSessionStore
from nexura_postgres.skills import PostgresSkillStore
from nexura_postgres.studio import PostgresStudioStore
from nexura_postgres.trigger_store import PostgresTriggerStore
from nexura_postgres.usage import PostgresUsageLedger
from nexura_postgres.workflow_store import PostgresWorkflowStore
from nexura_postgres.workspaces import PostgresWorkspaceStore

__all__ = [
    "MIGRATION_TIMEOUT",
    "Database",
    "apply_migrations",
    "migrate_all",
    "migration_status",
    "PgVectorStoreAdapter",
    "PostgresAgentStore",
    "PostgresBlobBinding",
    "PostgresChannelStore",
    "PostgresCheckpointStore",
    "PostgresEvaluationStore",
    "PostgresIntegrationRegistrations",
    "PostgresRepository",
    "PostgresSecretStore",
    "PostgresSessionStore",
    "PostgresSkillStore",
    "PostgresStudioEvalStore",
    "PostgresStudioStore",
    "PostgresExecutionStore",
    "PostgresHybridKnowledgeIndex",
    "PostgresTriggerStore",
    "PostgresUsageLedger",
    "PostgresWorkflowStore",
    "PostgresWorkspaceStore",
    "referenced_blob_keys",
]
