# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Run effects: list a run's journaled external effects and resolve an unknown one.

Reads the service's execution journal (``app.state.execution_store``). Runs whose
executor does not journal yet have no effects; the list is then empty.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi import status as http_status
from nexura_agents.contracts.execution import (
    EffectRecord,
    EffectResolution,
    EffectStatus,
    ExecutionId,
    LeaseLostError,
)
from nexura_agents.execution_store import (
    EffectAlreadyResolvedError,
    ExecutionConflictError,
    ExecutionSnapshot,
    ExecutionStore,
)
from pydantic import BaseModel, ConfigDict

from nexura_runtime.observability.run_diagnostics import redact_run_content
from nexura_runtime.service.api.runs import (
    _assert_run_ownership,
    _assert_run_resource,
    _get_caller_context,
    _require_permission,
)

router = APIRouter(prefix="/runs", tags=["runs"])


class ResolveEffectRequest(BaseModel):
    """A person's decision about an ``indeterminate`` effect.

    The actor is the authenticated caller; it is never taken from the request body.
    """

    model_config = ConfigDict(extra="forbid")
    resolution: EffectResolution
    result: Any = None
    reason: str | None = None


def _conflict(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_409_CONFLICT, detail={"code": code, "message": message}
    )


async def _owned_snapshot(
    request: Request, run_id: str
) -> tuple[ExecutionStore, ExecutionSnapshot | None]:
    """The run's journal snapshot (or None), after the same checks as other run routes."""
    workspace_id, _, _ = _get_caller_context(request)
    store: ExecutionStore = request.app.state.execution_store
    await store.init_db()
    try:
        # The journal is keyed by workspace, so a run of another workspace is not found.
        snapshot: ExecutionSnapshot | None = await store.get(ExecutionId(workspace_id, run_id))
    except KeyError:
        snapshot = None
    studio_run = await request.app.state.store.get_run(run_id)
    if snapshot is None and studio_run is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail={"code": "RUN_NOT_FOUND", "message": f"Run '{run_id}' not found."},
        )
    _assert_run_ownership(request, run_id)
    view = studio_run or {
        "run_id": run_id,
        "workflow_id": (snapshot.run.origin.get("legacy_metadata") or {}).get("workflow_id")
        if snapshot is not None
        else None,
    }
    _assert_run_resource(request, view)
    return store, snapshot


def _effect_view(record: EffectRecord) -> dict[str, Any]:
    data = asdict(record)
    data["effect_id"] = record.effect_id
    # Results are arbitrary remote payloads: mask credential-like fields as run views do.
    return redact_run_content(data)  # type: ignore[no-any-return]


@router.get("/{run_id}/effects")
async def list_run_effects(request: Request, run_id: str) -> dict[str, Any]:
    """List a run's journaled effects (``pending``, ``confirmed``, ``indeterminate``,
    ``resolved``), oldest step first. A run whose executor does not journal yet has none."""
    _, snapshot = await _owned_snapshot(request, run_id)
    if snapshot is None:
        return {"effects": []}
    return {"effects": [_effect_view(effect) for effect in snapshot.effects.values()]}


@router.post("/{run_id}/effects/{effect_id}/resolve")
async def resolve_run_effect(
    request: Request, run_id: str, effect_id: str, payload: ResolveEffectRequest
) -> dict[str, Any]:
    """Resolve an ``indeterminate`` effect exactly once, recording the caller as actor.

    ``confirmed`` (it happened; ``result`` is its output), ``not_applied`` (it did not
    happen) or ``failed``. Needs the ``approve`` permission. Resolving does not run
    anything; the run continues when its executor next claims it.
    """
    _, principal = _require_permission(request, "approve")
    if principal is None:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail={
                "code": "AUTHENTICATION_REQUIRED",
                "message": "Resolving an effect records who did it; a principal is required.",
            },
        )
    store, snapshot = await _owned_snapshot(request, run_id)
    effect = snapshot.effects.get(effect_id) if snapshot is not None else None
    if snapshot is None or effect is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail={"code": "EFFECT_NOT_FOUND", "message": f"Effect '{effect_id}' not found."},
        )
    if effect.status == EffectStatus.RESOLVED:
        raise _conflict("EFFECT_ALREADY_RESOLVED", f"Effect '{effect_id}' is already resolved.")
    if effect.status != EffectStatus.INDETERMINATE:
        raise _conflict(
            "EFFECT_NOT_INDETERMINATE",
            f"Effect '{effect_id}' is '{effect.status.value}'; only an indeterminate "
            "effect can be resolved.",
        )
    try:
        lease = await store.claim(
            snapshot.run.execution_id,
            "effect-resolution-" + uuid.uuid4().hex,
            30,
            definition_digest=snapshot.run.definition_digest,
        )
    except ExecutionConflictError as exc:
        raise _conflict(
            "RUN_FINISHED", f"Run '{run_id}' has finished; its effects can no longer be resolved."
        ) from exc
    if lease is None:
        raise _conflict("RUN_LEASE_HELD", f"Run '{run_id}' is being executed by a worker.")
    try:
        resolved = await store.resolve_effect(
            lease,
            effect_id,
            payload.resolution,
            actor=principal.principal_id,
            result=payload.result,
            reason=payload.reason,
        )
    except EffectAlreadyResolvedError as exc:
        raise _conflict("EFFECT_ALREADY_RESOLVED", str(exc)) from exc
    except (ExecutionConflictError, LeaseLostError) as exc:
        raise _conflict("EFFECT_RESOLUTION_CONFLICT", str(exc)) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_EFFECT_RESULT", "message": str(exc)},
        ) from exc
    finally:
        current = await store.get(snapshot.run.execution_id)
        if current.run.lease_owner == lease.owner:
            await store.release(lease)
    return _effect_view(resolved)
