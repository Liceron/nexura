"""P38 C6 review round 2: the service's /migrate, /decide and /input around a migration.

Converted from the reviewer's round-2 repros, with the three service defects now asserting
the fixed behaviour:

- R2-M1: the first-party Python client speaks the /migrate contract (real ASGI app);
- R2-M2: ``GET /runs`` works with a checkpoint store that has no ``run_identities``;
- R2-M3: a /decide or /input racing a /migrate is a 409 that never marks the run failed,
  and the person is asked again (or can send the same decision again);
- R2-m2: /migrate on a completed run whose row lagged behaves like /recover.

Every handler and payment is a local fake; no network or provider is used.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from fastapi.testclient import TestClient

from nexura_runtime.service.api import runs
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.client import NexuraApiError, NexuraClient
from nexura_runtime.workflow import (
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
    _LeaseBusyError,
    interrupt,
    workflow_run_identities,
)


def note(items: list[Any], value: Any) -> Literal[False]:
    """Record ``value`` (a local fake's call) and return False, for ``note(...) or {...}``."""
    items.append(value)
    return False


def definition(version: int = 1, *, gated: bool = True, extra: bool = False) -> WorkflowSpec:
    nodes = {"pay": WorkflowNode(id="pay", handler="fake", requires_approval=gated)}
    if extra:
        nodes["pay"].default_transition = "extra"
        nodes["extra"] = WorkflowNode(id="extra", handler="fake")
    return WorkflowSpec(name="review", version=version, entry_node="pay", nodes=nodes)


def start_saved(client: Any, spec: WorkflowSpec | None = None) -> tuple[dict[str, Any], str]:
    saved = client.post(
        "/api/v1/workflows", json={"name": "review", "spec": (spec or definition()).to_dict()}
    )
    assert saved.status_code == 201, saved.text
    workflow = saved.json()
    started = client.post(
        "/api/v1/runs",
        json={"workflow_id": workflow["workflow_id"], "initial_state": {"amount": 5}},
    )
    assert started.status_code == 201, started.text
    return workflow, started.json()["run_id"]


def update_saved(client: Any, workflow: dict[str, Any], target: WorkflowSpec) -> None:
    r = client.put(
        f"/api/v1/workflows/{workflow['workflow_id']}",
        json={"spec": target.to_dict(), "expected_revision": workflow["revision"]},
    )
    assert r.status_code == 200, r.text


def _app(tmp_path: Path, sends: list[int]) -> Any:
    reg = ModuleRegistry()
    reg.register("fake", lambda s: note(sends, s["amount"]) or {}, effect="pure")
    app = create_api_app(registry=reg, db_path=tmp_path / "api.db", profile="local-owner")
    app.state.workflow_migrations = {"review": {(1, 2): lambda a, b, s: {**s, "amount": 500}}}
    return app


def _event_types(client: Any, run_id: str) -> list[str]:
    return [e["type"] for e in client.get(f"/api/v1/runs/{run_id}").json()["events"]]


def _migrate_during_claim(app: Any, monkeypatch: pytest.MonkeyPatch, target: WorkflowSpec) -> None:
    """Commit a v2 migration (as another request would) right before the next claim."""
    cps = app.state.store.checkpoint_store
    real_claim = cps.claim_approval
    fired = False

    async def claim(rid: str, pending_id: str) -> bool:
        nonlocal fired
        if not fired:
            fired = True
            target.register_migration(1, 2, lambda a, b, s: {**s, "amount": 500})
            await WorkflowEngine(target, app.state.registry, store=cps).migrate(rid)
        return bool(await real_claim(rid, pending_id))

    monkeypatch.setattr(cps, "claim_approval", claim)


# ------------------------------------------------------------------ R2-M1
async def test_first_party_python_client_migrates_against_the_app(tmp_path: Path) -> None:
    sends: list[int] = []
    app = _app(tmp_path, sends)
    with TestClient(app) as tc:
        workflow, run_id = start_saved(tc)
        update_saved(tc, workflow, definition(2))
        target_digest = definition(2).canonical_hash()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local-asgi"
        ) as http:
            client = NexuraClient("http://local-asgi", http_client=http)
            with pytest.raises(TypeError):
                await client.runs.migrate(run_id, definition(2).to_dict())  # type: ignore[misc,arg-type]
            with pytest.raises(NexuraApiError) as stale:
                await client.runs.migrate(run_id, expected_definition_digest="sha256-stale")
            assert (stale.value.status_code, stale.value.code) == (409, "SPEC_MISMATCH")
            assert tc.get(f"/api/v1/runs/{run_id}").json()["workflow_version"] == 1
            migrated = await client.runs.migrate(
                run_id, expected_definition_digest=target_digest, resume=False
            )
        assert migrated["workflow_version"] == 2 and migrated["started_workflow_version"] == 1
        assert migrated["definition_digest"] == target_digest
        assert migrated["status"] == "pending_approval"
        assert migrated["current_state"]["amount"] == 500
        assert [e["type"] for e in migrated["events"]].count("RUN_DEFINITION_MIGRATED") == 1
        assert sends == []


# ------------------------------------------------------------------ M1 (holds)
def test_m1_saved_target_audit_and_regate_via_decide(tmp_path: Path) -> None:
    sends: list[int] = []
    app = _app(tmp_path, sends)
    with TestClient(app) as tc:
        workflow, run_id = start_saved(tc)
        run = tc.get(f"/api/v1/runs/{run_id}").json()
        assert run["status"] == "pending_approval"
        approval_id = run["pending_approval"]["approval_id"]
        update_saved(tc, workflow, definition(2, gated=False))  # v2 drops the gate
        noop_body = tc.post(
            f"/api/v1/runs/{run_id}/migrate", json={"spec": definition(2).to_dict()}
        )
        assert noop_body.status_code == 422
        r = tc.post(f"/api/v1/runs/{run_id}/migrate", json={"resume": False})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["workflow_version"] == 2 and body["started_workflow_version"] == 1
        events = [e for e in body["events"] if e["type"] == "RUN_DEFINITION_MIGRATED"]
        assert len(events) == 1 and events[0]["data"]["workflow_id"] == workflow["workflow_id"]
        again = tc.post(f"/api/v1/runs/{run_id}/migrate", json={})
        assert again.status_code == 200
        migrated_events = [
            e for e in again.json()["events"] if e["type"] == "RUN_DEFINITION_MIGRATED"
        ]
        assert len(migrated_events) == 1
        tc.post(
            f"/api/v1/runs/{run_id}/decide",
            json={"approval_id": approval_id, "decision": "approve"},
        )
        assert sends == [], "the migrated amount was sent on an approval of the old amount"
        now = tc.get(f"/api/v1/runs/{run_id}").json()
        assert now["status"] == "pending_approval"
        assert now["pending_approval"]["approval_id"] != approval_id
        assert now["pending_approval"]["input_state"]["amount"] == 500


@pytest.mark.parametrize(
    "who", ["viewer_role", "run_scope_only", "other_workflow_resource", "not_starter"]
)
def test_m1_permission_and_resource_refusals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    from nexura_runtime.secrets.keys import generate_master_key
    from nexura_runtime.service.auth.models import Principal
    from nexura_runtime.service.extensions import Identity

    monkeypatch.setenv("NEXURA_MASTER_KEY", generate_master_key())
    current: dict[str, Any] = {
        "id": "owner",
        "scopes": ["run", "edit"],
        "resources": None,
        "role": "editor",
    }

    class LocalIdentity:
        name = "local-test-fake-identity"
        is_authenticating = True

        def authenticate(self, request: object, target_workspace_id: str | None) -> Identity:
            return Identity(
                Principal(
                    principal_id=current["id"],
                    profile="authenticated-shared-service",
                    scopes=list(current["scopes"]),
                    resources=current["resources"],
                ),
                "test-workspace",
                current["role"],
            )

        def is_operator(self, request: object) -> bool:
            return False

    sends: list[int] = []
    reg = ModuleRegistry()
    reg.register("fake", lambda s: note(sends, s["amount"]) or {}, effect="pure")
    app = create_api_app(
        registry=reg,
        db_path=tmp_path / "api.db",
        profile="authenticated-shared-service",
        auth_provider=LocalIdentity(),
    )
    app.state.workflow_migrations = {"review": {(1, 2): lambda a, b, s: {**s, "amount": 500}}}
    with TestClient(app) as tc:
        workflow, run_id = start_saved(tc)
        update_saved(tc, workflow, definition(2, extra=True))
        if who == "viewer_role":
            current.update(role="viewer", scopes=[])
        elif who == "run_scope_only":
            current.update(scopes=["run"])
        elif who == "other_workflow_resource":
            current.update(resources=["workflow:wf-other"])
        else:
            current.update(id="someone-else", resources=[f"workflow:{workflow['workflow_id']}"])
        r = tc.post(f"/api/v1/runs/{run_id}/migrate", json={})
        assert r.status_code in (403, 404), r.text
        current.update(id="owner", scopes=["run", "edit"], resources=None, role="editor")
        assert tc.get(f"/api/v1/runs/{run_id}").json()["workflow_version"] == 1


# ------------------------------------------------------------------ R2-M3
def test_decide_racing_migrate_is_409_and_asks_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/decide reads the bound (v1) definition; another request's /migrate commits v2 before
    the decide claims. The stale engine claims, then refuses (B1). The decide is a 409, the
    run is never marked failed, and the person is asked to approve the migrated amount."""
    sends: list[int] = []
    app = _app(tmp_path, sends)
    with TestClient(app) as tc:
        workflow, run_id = start_saved(tc)
        approval_id = tc.get(f"/api/v1/runs/{run_id}").json()["pending_approval"]["approval_id"]
        update_saved(tc, workflow, definition(2))
        _migrate_during_claim(app, monkeypatch, definition(2))
        decided = tc.post(
            f"/api/v1/runs/{run_id}/decide",
            json={"approval_id": approval_id, "decision": "approve"},
        )
        assert decided.status_code == 409, decided.text
        assert decided.json()["detail"]["code"] == "WORKFLOW_DEFINITION_CHANGED"
        assert sends == []
        row = tc.get(f"/api/v1/runs/{run_id}").json()
        assert row["status"] == "pending_approval"
        assert row["error"] is None
        fresh = row["pending_approval"]
        assert fresh["approval_id"] != approval_id
        assert fresh["approval_id"] in decided.json()["detail"]["message"]
        assert fresh["input_state"]["amount"] == 500
        assert row["workflow_version"] == 2
        types = _event_types(tc, run_id)
        assert "RUN_FAILED" not in types and "RUN_DEFINITION_CHANGED" in types
        # Not stranded: /recover answers "waiting for a person", /migrate is a no-op.
        recover = tc.post(f"/api/v1/runs/{run_id}/recover", json={})
        assert recover.status_code == 409
        assert recover.json()["detail"]["code"] == "RUN_WAITING_FOR_PERSON"
        assert tc.post(f"/api/v1/runs/{run_id}/migrate", json={}).status_code == 200
        # The old approval is used up; the fresh one sends the migrated amount once.
        stale = tc.post(
            f"/api/v1/runs/{run_id}/decide",
            json={"approval_id": approval_id, "decision": "approve"},
        )
        assert stale.status_code == 404 and sends == []
        done = tc.post(
            f"/api/v1/runs/{run_id}/decide",
            json={"approval_id": fresh["approval_id"], "decision": "approve"},
        )
        assert done.status_code == 200 and done.json()["status"] == "completed", done.text
        assert sends == [500]


def test_decide_refused_before_claim_keeps_the_decision_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The migration commits after /decide read its definition but before the engine read
    the checkpoint: the refusal comes before the claim, so the same decision can be sent
    again (it then asks for a fresh approval of the migrated amount)."""
    sends: list[int] = []
    app = _app(tmp_path, sends)
    with TestClient(app) as tc:
        workflow, run_id = start_saved(tc)
        approval_id = tc.get(f"/api/v1/runs/{run_id}").json()["pending_approval"]["approval_id"]
        update_saved(tc, workflow, definition(2))
        real_bound = runs._bound_definition
        fired = False

        async def bound_then_migrate(app_state: Any, run: Any) -> WorkflowSpec:
            nonlocal fired
            spec = await real_bound(app_state, run)
            if not fired:
                fired = True
                target = definition(2)
                target.register_migration(1, 2, lambda a, b, s: {**s, "amount": 500})
                cps = app_state.store.checkpoint_store
                await WorkflowEngine(target, app_state.registry, store=cps).migrate(run["run_id"])
            return spec

        monkeypatch.setattr(runs, "_bound_definition", bound_then_migrate)
        decided = tc.post(
            f"/api/v1/runs/{run_id}/decide",
            json={"approval_id": approval_id, "decision": "approve"},
        )
        monkeypatch.setattr(runs, "_bound_definition", real_bound)
        assert decided.status_code == 409, decided.text
        assert decided.json()["detail"]["code"] == "WORKFLOW_DEFINITION_CHANGED"
        assert "send the decision again" in decided.json()["detail"]["message"]
        row = tc.get(f"/api/v1/runs/{run_id}").json()
        assert row["status"] == "pending_approval"
        assert row["pending_approval"]["approval_id"] == approval_id
        assert "RUN_FAILED" not in _event_types(tc, run_id) and sends == []
        again = tc.post(
            f"/api/v1/runs/{run_id}/decide",
            json={"approval_id": approval_id, "decision": "approve"},
        )
        assert again.status_code == 200, again.text
        fresh = again.json()["pending_approval"]
        assert again.json()["status"] == "pending_approval" and sends == []
        assert fresh["approval_id"] != approval_id and fresh["input_state"]["amount"] == 500
        done = tc.post(
            f"/api/v1/runs/{run_id}/decide",
            json={"approval_id": fresh["approval_id"], "decision": "approve"},
        )
        assert done.json()["status"] == "completed" and sends == [500]


def test_decide_race_when_asking_again_is_blocked_leaves_run_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the run cannot be continued at once (another worker holds its lease), the row
    shows 'interrupted' instead of a false 'failed', and /recover asks again."""
    sends: list[int] = []
    app = _app(tmp_path, sends)
    with TestClient(app) as tc:
        workflow, run_id = start_saved(tc)
        approval_id = tc.get(f"/api/v1/runs/{run_id}").json()["pending_approval"]["approval_id"]
        update_saved(tc, workflow, definition(2))
        _migrate_during_claim(app, monkeypatch, definition(2))
        real_recover = runs._recover_execution

        async def busy(*args: Any, **kwargs: Any) -> None:
            raise _LeaseBusyError("another local test worker holds the run lease")

        monkeypatch.setattr(runs, "_recover_execution", busy)
        decided = tc.post(
            f"/api/v1/runs/{run_id}/decide",
            json={"approval_id": approval_id, "decision": "approve"},
        )
        monkeypatch.setattr(runs, "_recover_execution", real_recover)
        assert decided.status_code == 409, decided.text
        assert decided.json()["detail"]["code"] == "WORKFLOW_DEFINITION_CHANGED"
        assert "recover" in decided.json()["detail"]["message"]
        assert tc.portal is not None
        stored = tc.portal.call(app.state.store.get_run, run_id)
        assert stored["status"] == "interrupted" and stored["error"] is None and sends == []
        # The view shows it running while this (live) process still owns the row (B46).
        assert tc.get(f"/api/v1/runs/{run_id}").json()["status"] in ("interrupted", "running")
        assert "RUN_FAILED" not in _event_types(tc, run_id)
        recovered = tc.post(f"/api/v1/runs/{run_id}/recover", json={})
        assert recovered.status_code == 200, recovered.text
        fresh = recovered.json()["pending_approval"]
        assert recovered.json()["status"] == "pending_approval" and sends == []
        assert fresh["approval_id"] != approval_id and fresh["input_state"]["amount"] == 500
        assert tc.post(f"/api/v1/runs/{run_id}/migrate", json={}).status_code == 200
        done = tc.post(
            f"/api/v1/runs/{run_id}/decide",
            json={"approval_id": fresh["approval_id"], "decision": "approve"},
        )
        assert done.json()["status"] == "completed" and sends == [500]


def test_input_racing_migrate_is_409_and_asks_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[int, Any]] = []

    def ask(state: dict[str, Any]) -> dict[str, Any]:
        answer = interrupt("local question")
        seen.append((state["amount"], answer))
        return {"answer": answer}

    reg = ModuleRegistry()
    reg.register("fake", ask, effect="pure")
    app = create_api_app(registry=reg, db_path=tmp_path / "api.db", profile="local-owner")
    app.state.workflow_migrations = {"review": {(1, 2): lambda a, b, s: {**s, "amount": 500}}}
    with TestClient(app) as tc:
        workflow, run_id = start_saved(tc, definition(gated=False))
        run = tc.get(f"/api/v1/runs/{run_id}").json()
        assert run["status"] == "waiting_for_input", run
        interrupt_id = run["pending_input"]["interrupt_id"]
        update_saved(tc, workflow, definition(2, gated=False))
        _migrate_during_claim(app, monkeypatch, definition(2, gated=False))
        answered = tc.post(
            f"/api/v1/runs/{run_id}/input",
            json={"interrupt_id": interrupt_id, "value": "local answer"},
        )
        assert answered.status_code == 409, answered.text
        assert answered.json()["detail"]["code"] == "WORKFLOW_DEFINITION_CHANGED"
        assert seen == []
        row = tc.get(f"/api/v1/runs/{run_id}").json()
        assert row["status"] == "waiting_for_input" and row["error"] is None
        assert row["workflow_version"] == 2
        assert "RUN_FAILED" not in _event_types(tc, run_id)
        again = row["pending_input"]["interrupt_id"]
        done = tc.post(
            f"/api/v1/runs/{run_id}/input",
            json={"interrupt_id": again, "value": "local answer"},
        )
        assert done.status_code == 200 and done.json()["status"] == "completed", done.text
        assert seen == [(500, "local answer")]


# ------------------------------------------------------------------ R2-m2
def test_migrate_on_lagging_row_of_completed_run_records_the_result(tmp_path: Path) -> None:
    """The row still says 'interrupted' but the checkpoint completed (the row update was
    lost). /migrate, like /recover, records the committed result instead of failing it."""
    sends: list[int] = []
    app = _app(tmp_path, sends)
    with TestClient(app) as tc:
        workflow, run_id = start_saved(tc)
        approval_id = tc.get(f"/api/v1/runs/{run_id}").json()["pending_approval"]["approval_id"]
        done = tc.post(
            f"/api/v1/runs/{run_id}/decide",
            json={"approval_id": approval_id, "decision": "approve"},
        )
        assert done.status_code == 200 and done.json()["status"] == "completed", done.text
        update_saved(tc, workflow, definition(2))
        assert tc.portal is not None
        cp = tc.portal.call(app.state.store.checkpoint_store.get, run_id)
        tc.portal.call(
            app.state.store.update_run,
            run_id,
            "interrupted",
            cp.current_node_id,
            cp.state,
            cp.step_count,
        )
        r = tc.post(f"/api/v1/runs/{run_id}/migrate", json={})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "completed" and body["error"] is None
        assert body["workflow_version"] == 1  # a finished run is not migrated
        assert "RUN_FAILED" not in [e["type"] for e in body["events"]]
        assert tc.portal.call(app.state.store.checkpoint_store.get, run_id).status == "completed"
        assert sends == [5]


# ------------------------------------------------------------------ R2-M2
class _GetOnlyStore:
    """A third-party checkpoint store exposing ``get()`` but no ``run_identities``."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    async def get(self, run_id: str) -> Any:
        return await self.inner.get(run_id)


class _GetAndHistoryStore(_GetOnlyStore):
    """A third-party checkpoint store with ``get()`` and ``history()``."""

    async def history(self, run_id: str) -> Any:
        return await self.inner.history(run_id)


async def test_identities_fall_back_to_per_run_reads(tmp_path: Path) -> None:
    store = SQLiteWorkflowStore(tmp_path / "state.db")
    reg = ModuleRegistry()
    reg.register("fake", lambda s: {}, effect="pure")
    try:
        await WorkflowEngine(definition(), reg, store=store).run({}, workflow_run_id="r")
        target = definition(2)
        target.register_migration(1, 2, lambda a, b, s: s)
        await WorkflowEngine(target, reg, store=store).migrate("r")
        assert await workflow_run_identities(_GetOnlyStore(store), ["r", "missing"]) == {
            "r": {"definition_digest": target.canonical_hash(), "workflow_version": 2}
        }
        assert await workflow_run_identities(_GetOnlyStore(store), []) == {}
        assert await workflow_run_identities(_GetAndHistoryStore(store), ["r", "missing"]) == {
            "r": {
                "definition_digest": target.canonical_hash(),
                "workflow_version": 2,
                "started_workflow_version": 1,
            }
        }
    finally:
        await store.close()


def test_run_list_without_batch_identity_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sends: list[int] = []
    app = _app(tmp_path, sends)
    with TestClient(app) as tc:
        workflow, run_id = start_saved(tc)
        update_saved(tc, workflow, definition(2))
        assert tc.post(f"/api/v1/runs/{run_id}/migrate", json={}).status_code == 200
        # The service's checkpoint store class (SQLite, or Postgres under --service-backend).
        monkeypatch.delattr(type(app.state.store.checkpoint_store), "run_identities")
        assert not hasattr(app.state.store.checkpoint_store, "run_identities")
        listed = tc.get("/api/v1/runs")
        assert listed.status_code == 200, listed.text
        (item,) = [i for i in listed.json()["items"] if i["run_id"] == run_id]
        assert item["workflow_version"] == 2 and item["started_workflow_version"] == 1
        assert item["definition_digest"] == definition(2).canonical_hash()
        assert item["origin_definition_digest"] == definition().canonical_hash()
