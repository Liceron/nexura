# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Stop the runs of a principal, API key or publication whose access has ended (B40).

Revoking access must also stop what that access already started. Ownership comes from what
:func:`~nexura_runtime.service.api.runs.start_workflow_run` records for every run:

* the workspace ownership row ``("run", run_id) -> (workspace_id, owner_id)``, where
  ``owner_id`` is the principal id (people and API keys alike) or
  ``publication:<publication_id>`` for public runs;
* ``started_by`` on the run row, with ``api_key_id`` when an API key started it.

Each matching run that is still running, waiting for an approval or waiting for input is
cancelled through the shared cancel path
(:func:`~nexura_runtime.service.api.runs.cancel_run_record`) with a ``cancel_reason`` that
``GET /runs/{id}`` returns; a later approve or answer is refused with ``RUN_ACCESS_REVOKED``.
Everything is scoped to one workspace and repeating a revocation is a no-op.

The callers that revoke access (member removal, API key revocation, org member removal,
publication revocation) are in the closed edition since EDITIONS_PLAN steps 4, 5 and 7
(including the API key revocation listener). The
cancel path stays open source: it works on runs, which are open source.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Final, Literal

logger = logging.getLogger("nexura.service.revocation")

CancelReason = Literal["access_revoked", "publication_revoked"]
ACCESS_REVOKED: Final[CancelReason] = "access_revoked"
PUBLICATION_REVOKED: Final[CancelReason] = "publication_revoked"

#: Statuses revocation stops; terminal and ``interrupted`` runs cannot continue anyway.
REVOCABLE_STATUSES = frozenset({"running", "pending_approval", "waiting_for_input"})

_MESSAGES: dict[str, str] = {
    ACCESS_REVOKED: "Access of the principal that started this run was revoked.",
    PUBLICATION_REVOKED: "The publication that started this run was revoked.",
}


def cancel_message(reason: str) -> str:
    """The cancel message stored on a run cancelled for ``reason``."""
    return _MESSAGES.get(reason, reason)


def _lock(app_state: Any) -> asyncio.Lock:
    """One revocation at a time per app, so overlapping revocations cancel a run once."""
    lock: asyncio.Lock | None = getattr(app_state, "run_revocation_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app_state.run_revocation_lock = lock
    return lock


async def _candidate_run_ids(
    app_state: Any,
    workspace_id: str,
    *,
    principal_id: str | None,
    api_key_id: str | None,
    publication_id: str | None,
) -> list[str]:
    ws_repo = app_state.workspace_repo
    if principal_id is not None:
        return list(ws_repo.list_owned_resource_ids("run", workspace_id, principal_id))
    if publication_id is not None:
        return list(
            ws_repo.list_owned_resource_ids("run", workspace_id, f"publication:{publication_id}")
        )
    assert api_key_id is not None
    started = await app_state.store.list_run_ids_started_by_key(api_key_id)
    # The key's own workspace only: a run id another workspace owns is never touched.
    return [
        run_id
        for run_id in started
        if (owner := ws_repo.get_resource_ownership("run", run_id)) is not None
        and owner[0] == workspace_id
    ]


async def cancel_revoked_runs(
    app_state: Any,
    *,
    workspace_id: str,
    reason: CancelReason,
    principal_id: str | None = None,
    api_key_id: str | None = None,
    publication_id: str | None = None,
) -> list[str]:
    """Cancel the live runs one principal, API key or publication started in a workspace.

    Pass exactly one of ``principal_id``, ``api_key_id`` or ``publication_id``. Running runs
    have their background task stopped; runs waiting for an approval or for input are marked
    cancelled so they can no longer be resumed. Returns the ids cancelled by this call, sorted
    (empty when there was nothing left to cancel, so a repeated revocation changes nothing).
    """
    from nexura_runtime.service.api.runs import cancel_run_record

    selectors = [s for s in (principal_id, api_key_id, publication_id) if s is not None]
    if len(selectors) != 1:
        raise ValueError("Pass exactly one of principal_id, api_key_id or publication_id.")
    store = app_state.store
    cancelled: list[str] = []
    async with _lock(app_state):
        run_ids = await _candidate_run_ids(
            app_state,
            workspace_id,
            principal_id=principal_id,
            api_key_id=api_key_id,
            publication_id=publication_id,
        )
        for run_id in sorted(run_ids):
            if await store.get_run_status(run_id) not in REVOCABLE_STATUSES:
                continue
            updated = await cancel_run_record(
                app_state, run_id, reason=cancel_message(reason), cancel_reason=reason
            )
            if updated is not None:
                cancelled.append(run_id)
    if cancelled:
        logger.info(
            "cancelled %d run(s) in workspace %s after revocation (%s)",
            len(cancelled),
            workspace_id,
            reason,
        )
    return cancelled


__all__ = [
    "ACCESS_REVOKED",
    "PUBLICATION_REVOKED",
    "REVOCABLE_STATUSES",
    "CancelReason",
    "cancel_message",
    "cancel_revoked_runs",
]
