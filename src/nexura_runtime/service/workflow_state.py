# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Where the service keeps workflow state (A6 S8).

By default workflow checkpoints, their history and the execution journal of workflow
runs live in the service's SQLite database. A ``postgresql://`` URI, given to
``create_api_app(workflow_state_url=...)``, ``nexura-runtime serve --workflow-state-url``
or ``NEXURA_WORKFLOW_STATE_URL``, keeps them in Postgres instead
(``nexura_postgres.PostgresWorkflowStore``, from the ``nexura-postgres`` package), together
with the trigger job queue, webhooks and schedules (``nexura_postgres.PostgresTriggerStore``,
A6 S10b), so workers on several hosts share one queue.
"""

from __future__ import annotations

import os
from typing import Any

from nexura_runtime.modules.optional_packages import POSTGRES_PACKAGE, require_package

#: Environment variable read when no workflow state URL is passed explicitly.
WORKFLOW_STATE_URL_ENV = "NEXURA_WORKFLOW_STATE_URL"
_POSTGRES_SCHEMES = ("postgresql://", "postgres://")


def resolve_workflow_state_url(explicit: str | None = None) -> str | None:
    """The Postgres URI for workflow state, or ``None`` for SQLite (the default).

    ``explicit`` wins over ``NEXURA_WORKFLOW_STATE_URL``; an empty value means SQLite.
    Anything other than a ``postgresql://`` / ``postgres://`` URI raises ``ValueError``
    (the message names the scheme only, never the URI, which may hold a password).
    """
    value = explicit if explicit is not None else os.environ.get(WORKFLOW_STATE_URL_ENV, "")
    if not isinstance(value, str):
        raise ValueError("The workflow state URL must be a string.")
    value = value.strip()
    if not value:
        return None
    if value.lower().startswith(_POSTGRES_SCHEMES):
        return value
    scheme = value.split("://", 1)[0] if "://" in value else "(none)"
    raise ValueError(
        f"Unsupported workflow state URL scheme '{scheme}': use a postgresql:// URI, or "
        "leave it unset to keep workflow state in the SQLite database."
    )


def open_workflow_state_store(url: str) -> Any:
    """A ``PostgresWorkflowStore`` for ``url`` (raises ``MissingDependencyError`` naming
    ``pip install nexura-postgres`` when that package is not installed)."""
    postgres = require_package(POSTGRES_PACKAGE, "PostgreSQL workflow state")
    return postgres.PostgresWorkflowStore(url)


def open_trigger_state_store(url: str, *, secret_key_file: str | os.PathLike[str] | None) -> Any:
    """A ``PostgresTriggerStore`` for ``url``; webhook secrets are encrypted with
    ``NEXURA_TRIGGER_SECRET_KEY`` or else the key in ``secret_key_file``. With
    ``secret_key_file=None`` (Postgres mode, MVP WP9) only the variable is used: without it,
    storing or reading a webhook secret raises and no key file is ever written."""
    postgres = require_package(POSTGRES_PACKAGE, "PostgreSQL trigger state")
    return postgres.PostgresTriggerStore(url, secret_key_file=secret_key_file)
