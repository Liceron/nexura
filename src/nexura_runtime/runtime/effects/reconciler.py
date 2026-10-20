# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Deprecated synchronous effect API backed exclusively by ExecutionStore records."""

from __future__ import annotations

import contextlib
import copy
import json
import sqlite3
import uuid
import warnings
from collections.abc import Coroutine
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from nexura_agents.contracts.canonical import canonical_digest
from nexura_agents.contracts.execution import (
    EffectRecord as JournalEffect,
)
from nexura_agents.contracts.execution import (
    EffectResolution,
    EffectStatus,
    ExecutionId,
    StepIntent,
    StepKey,
)
from nexura_agents.execution_store import (
    ExecutionConflictError,
    ExecutionStore,
    SQLiteExecutionStore,
)

from nexura_runtime.runtime.effects.errors import EffectError, ReconciliationError
from nexura_runtime.runtime.effects.models import EffectRecord


def _sync[T](call: Coroutine[Any, Any, T]) -> T:
    # Memory/SQLite journal methods do synchronous work and never suspend. Drive
    # this compatibility coroutine on its calling thread, including inside an
    # event loop and with a caller-owned, thread-affine sqlite connection.
    try:
        call.send(None)
    except StopIteration as done:
        return done.value  # type: ignore[no-any-return]
    finally:
        call.close()
    raise TypeError("The synchronous EffectReconciler requires a non-suspending local store")


class EffectReconciler:
    """Deprecated synchronous effect API; new code uses ``ExecutionStore`` effects.

    Every record lives in the execution journal (``nexura_exec_effects``): a legacy
    ``effect_id`` is an alias run ``legacy-effect:<effect_id>`` holding one effect.
    The resolution is stored machine-readably: legacy ``rolled_back`` is
    ``not_applied`` and ``voided`` is ``failed``. The resolving actor defaults to
    ``legacy-effect-reconciler``; pass ``actor=`` to record a person. Rows in the
    pre-0.5 ``effect_records`` table of the same SQLite database are imported once,
    when the wrapper opens it; that table is left untouched.

    Only local stores whose methods never suspend (``SQLiteExecutionStore``,
    ``MemoryExecutionStore``) can back this synchronous wrapper. For Postgres, use
    ``PostgresExecutionStore`` directly. Removed in 0.6.
    """

    _LABELS = {"confirmed": "CONFIRMED", "not_applied": "ROLLED_BACK", "failed": "VOIDED"}
    _RESOLUTIONS = {
        "confirmed": EffectResolution.CONFIRMED,
        "rolled_back": EffectResolution.NOT_APPLIED,
        "voided": EffectResolution.FAILED,
    }
    _ACTOR = "legacy-effect-reconciler"

    def __init__(
        self,
        db_path_or_conn: str | Path | sqlite3.Connection = ":memory:",
        *,
        store: ExecutionStore | None = None,
    ) -> None:
        warnings.warn(
            "EffectReconciler is deprecated and will be removed in 0.6; "
            "use ExecutionStore effects (see docs/durable-execution.md)",
            DeprecationWarning,
            stacklevel=2,
        )
        self.store = store or (
            SQLiteExecutionStore.from_connection(db_path_or_conn)
            if isinstance(db_path_or_conn, sqlite3.Connection)
            else SQLiteExecutionStore(db_path_or_conn)
        )
        _sync(self.store.init_db())
        _sync(self._import_legacy_rows())

    @staticmethod
    def _identity(workspace_id: str, effect_id: str) -> ExecutionId:
        return ExecutionId(workspace_id, "legacy-effect:" + effect_id)

    def _view(self, snapshot: Any) -> EffectRecord | None:
        effect = next(iter(snapshot.effects.values()), None)
        if effect is None:
            return None  # an alias run whose intent was refused (duplicate key)
        legacy = snapshot.run.origin["legacy"]
        notes = snapshot.run.state.get("notes")
        if effect.status == EffectStatus.RESOLVED:
            label = self._LABELS[effect.resolution.value]
            notes = f"RECONCILED [{label}]: {(effect.result or {}).get('notes', '')}"
        return EffectRecord(
            workspace_id=effect.execution_id.workspace_id,
            effect_id=legacy["effect_id"],
            run_id=legacy["run_id"],
            action_name=legacy["action_name"],
            idempotency_key=effect.intent.idempotency_key,
            payload_digest=effect.intent.payload_digest,
            status="reconciled" if effect.status == EffectStatus.RESOLVED else effect.status.value,
            dispatched_at=legacy.get("dispatched_at", snapshot.run.created_at),
            response_payload=snapshot.run.state.get("response"),
            reconciliation_notes=notes,
            created_at=snapshot.run.created_at,
            updated_at=snapshot.run.updated_at,
        )

    async def _read(self, workspace_id: str, effect_id: str) -> EffectRecord | None:
        try:
            return self._view(await self.store.get(self._identity(workspace_id, effect_id)))
        except KeyError:
            return None

    async def _record(
        self,
        workspace_id: str,
        effect_id: str,
        run_id: str,
        action_name: str,
        idempotency_key: str,
        payload_digest: str,
        dispatched_at: float | None = None,
    ) -> EffectRecord:
        identity = self._identity(workspace_id, effect_id)
        legacy: dict[str, Any] = {
            "effect_id": effect_id,
            "run_id": run_id,
            "action_name": action_name,
        }
        if dispatched_at is not None:
            legacy["dispatched_at"] = dispatched_at
        digest = canonical_digest({"action": action_name})
        try:
            run = await self.store.create(
                identity,
                owner="legacy-" + uuid.uuid4().hex,
                definition_digest=digest,
                state={},
                origin={"source": "sdk", "legacy": legacy},
            )
            lease = run.lease
        except ExecutionConflictError:
            # The alias exists: refuse a second intent, or finish one whose intent was refused.
            snapshot = await self.store.get(identity)
            if snapshot.effects:
                raise EffectError(f"Effect '{effect_id}' is already recorded.") from None
            claimed = await self.store.claim(
                identity, "legacy-" + uuid.uuid4().hex, 30, definition_digest=digest
            )
            if claimed is None:
                raise EffectError("Effect run has a live owner") from None
            lease = claimed
        intent = StepIntent(StepKey.workflow(action_name, 0), payload_digest, idempotency_key)
        try:
            await self.store.commit(
                lease, state={}, steps=[intent], effects=[JournalEffect(identity, intent)]
            )
        except ExecutionConflictError as exc:
            raise EffectError(
                f"Effect '{effect_id}' conflicts with a journaled effect "
                f"(idempotency keys are unique per workspace): {exc}"
            ) from exc
        finally:
            with contextlib.suppress(Exception):
                await self.store.release(lease)
        view = await self._read(workspace_id, effect_id)
        assert view is not None
        return view

    def record_intent(
        self,
        workspace_id: str,
        effect_id: str,
        run_id: str,
        action_name: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> EffectRecord:
        """Record a pending effect before dispatching it (one fenced journal commit)."""
        return _sync(
            self._record(
                workspace_id,
                effect_id,
                run_id,
                action_name,
                idempotency_key,
                canonical_digest(payload),
            )
        )

    async def _change_async(
        self,
        workspace_id: str,
        effect_id: str,
        *,
        status: EffectStatus | None = None,
        response: dict[str, Any] | None = None,
        notes: str | None = None,
        resolution: EffectResolution | None = None,
        actor: str = _ACTOR,
    ) -> EffectRecord:
        identity = self._identity(workspace_id, effect_id)
        try:
            snapshot = await self.store.get(identity)
        except KeyError as exc:
            raise EffectError(f"Effect '{effect_id}' not found.") from exc
        effect = next(iter(snapshot.effects.values()), None)
        if effect is None:
            raise EffectError(f"Effect '{effect_id}' not found.")
        if status is not None and effect.status != EffectStatus.PENDING:
            raise ReconciliationError(
                f"Effect '{effect_id}' has status '{effect.status.value}'; only a pending "
                f"effect can become '{status.value}'.",
                effect_id=effect_id,
                current_status=effect.status.value,
            )
        lease = await self.store.claim(
            identity,
            "legacy-" + uuid.uuid4().hex,
            30,
            definition_digest=snapshot.run.definition_digest,
        )
        if lease is None:
            raise EffectError("Effect run has a live owner")
        try:
            if resolution is not None:
                await self.store.resolve_effect(
                    lease, effect.effect_id, resolution, actor=actor, result={"notes": notes}
                )
            else:
                state = copy.deepcopy(snapshot.run.state)
                if notes is not None:
                    state["notes"] = notes
                if response is not None:
                    state["response"] = response
                await self.store.commit(
                    lease,
                    state=state,
                    effects=[replace(effect, status=status or effect.status, result=response)],
                )
        except ExecutionConflictError as exc:
            raise ReconciliationError(
                f"Effect '{effect_id}' has status '{effect.status.value}': {exc}",
                effect_id=effect_id,
                current_status=effect.status.value,
            ) from exc
        finally:
            current = await self.store.get(identity)
            if current.run.lease_owner == lease.owner:
                await self.store.release(lease)
        view = await self._read(workspace_id, effect_id)
        assert view is not None
        return view

    def _change(self, workspace_id: str, effect_id: str, **changes: Any) -> EffectRecord:
        return _sync(self._change_async(workspace_id, effect_id, **changes))

    def record_confirmed(
        self,
        workspace_id: str,
        effect_id: str,
        response_payload: dict[str, Any],
    ) -> EffectRecord:
        """Confirm a pending effect (journal transition pending -> confirmed)."""
        return self._change(
            workspace_id, effect_id, status=EffectStatus.CONFIRMED, response=response_payload
        )

    def record_indeterminate(
        self,
        workspace_id: str,
        effect_id: str,
        reason: str = "Connection lost during dispatch",
    ) -> EffectRecord:
        """Mark a pending effect indeterminate, keeping the legacy reason text."""
        return self._change(
            workspace_id,
            effect_id,
            status=EffectStatus.INDETERMINATE,
            notes=f"INDETERMINATE: {reason}",
        )

    def get_effect(self, workspace_id: str, effect_id: str) -> EffectRecord | None:
        """Read the legacy view of a journaled effect, or None."""
        return _sync(self._read(workspace_id, effect_id))

    def reconcile_effect(
        self,
        workspace_id: str,
        effect_id: str,
        resolution: Literal["confirmed", "rolled_back", "voided"],
        notes: str,
        *,
        actor: str = _ACTOR,
    ) -> EffectRecord:
        """Resolve an indeterminate effect once, recording ``actor``; a second call fails."""
        if resolution not in self._RESOLUTIONS:
            raise ReconciliationError(
                f"Unknown resolution '{resolution}'.",
                effect_id=effect_id,
                current_status="unknown",
            )
        return self._change(
            workspace_id,
            effect_id,
            resolution=self._RESOLUTIONS[resolution],
            notes=notes,
            actor=actor,
        )

    async def _import_legacy_rows(self) -> None:
        """Copy rows from the pre-0.5 ``effect_records`` table into the journal, once."""
        db = getattr(self.store, "_db", None)
        if not isinstance(db, sqlite3.Connection):
            return
        try:
            rows = db.execute(
                "SELECT workspace_id, effect_id, run_id, action_name, idempotency_key, "
                "payload_digest, status, dispatched_at, response_json, reconciliation_notes "
                "FROM effect_records ORDER BY created_at"
            ).fetchall()
        except sqlite3.OperationalError:
            return  # no legacy table in this database
        for row in rows:
            ws, effect_id = row["workspace_id"], row["effect_id"]
            if await self._read(ws, effect_id) is not None:
                continue
            try:
                await self._record(
                    ws,
                    effect_id,
                    row["run_id"],
                    row["action_name"],
                    row["idempotency_key"],
                    row["payload_digest"],
                    row["dispatched_at"],
                )
                status = row["status"]
                notes = row["reconciliation_notes"]
                response = json.loads(row["response_json"]) if row["response_json"] else None
                if status == "confirmed":
                    await self._change_async(
                        ws, effect_id, status=EffectStatus.CONFIRMED, response=response
                    )
                elif status in ("indeterminate", "reconciled"):
                    pending_notes = notes if status == "indeterminate" else "INDETERMINATE"
                    await self._change_async(
                        ws, effect_id, status=EffectStatus.INDETERMINATE, notes=pending_notes
                    )
                if status == "reconciled":
                    label, _, text = (notes or "").partition("]: ")
                    legacy = label.removeprefix("RECONCILED [").lower()
                    await self._change_async(
                        ws,
                        effect_id,
                        resolution=self._RESOLUTIONS.get(legacy, EffectResolution.CONFIRMED),
                        notes=text or notes,
                    )
            except (EffectError, ExecutionConflictError) as exc:
                warnings.warn(
                    f"Legacy effect '{effect_id}' was not imported into the journal: {exc}",
                    RuntimeWarning,
                    stacklevel=3,
                )

    def close(self) -> None:
        """Close an owned journal connection; a borrowed connection stays open."""
        _sync(self.store.close())
