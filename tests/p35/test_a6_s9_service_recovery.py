"""A6 S9: the service decides "interrupted" from the run's journal lease, and recovers runs.

In-process tests on one SQLite file: the run lease (not who opened the database last)
decides whether a journaled run is interrupted; ``POST /runs/{id}/recover`` continues a
run through ``WorkflowEngine.recover``; ``RunEventRegistry`` passes declared effect classes
through. Crashes are simulated with a ``BaseException`` the engine does not catch; leases
of "another process" are held with a foreign owner id. All handlers are local fakes.
The two-process test (real SIGKILL, SQLite and Postgres) is in
``tests/durability/test_durability_service.py``.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from _local_env import local_headers
from _service_state import (
    owner_of_process,
    reopen_studio_store,
    service_execution_store,
    studio_sql,
)
from fastapi.testclient import TestClient
from nexura_agents.contracts.execution import EffectClass, ExecutionId
from starlette.requests import Request

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.client import NexuraClient
from nexura_runtime.service.api.runs import RunEventRegistry
from nexura_runtime.service.auth.models import Principal
from nexura_runtime.service.extensions import Identity
from nexura_runtime.service.run_liveness import (
    LEASE_EXPIRED_ERROR,
    SHUTDOWN_ERROR,
    reconcile_run_statuses,
)
from nexura_runtime.studio.store import StudioStore
from nexura_runtime.workflow import (
    ModuleRegistry,
    WorkflowEngine,
    WorkflowSpec,
    step_idempotency_key,
)

API = "/api/v1"
WS = "default"
#: A lease owner of "another serve/worker": a host identity that is not this one, so the
#: dead-owner shortcut never applies and only expiry frees the lease.
FOREIGN_OWNER = "nx1.0123456789abcdef.4242.1.feedfacefeedfacefeedfacefeedface"


class Crash(BaseException):
    """Stops the engine mid-node the way a killed process would (nothing catches it)."""


class Fakes:
    """``note`` then ``pay``; ``pay`` can crash once after its effect happened."""

    def __init__(self, pay_effect: str | None = None, note_effect: str | None = "pure") -> None:
        self.calls: dict[str, int] = {"note": 0, "pay": 0}
        self.keys: list[str | None] = []
        self.crash_pay = False
        self.registry = ModuleRegistry()

        async def note(state: dict[str, Any]) -> dict[str, Any]:
            self.calls["note"] += 1
            return {"noted": True}

        async def pay(state: dict[str, Any]) -> dict[str, Any]:
            self.calls["pay"] += 1
            self.keys.append(step_idempotency_key())
            if self.crash_pay:
                self.crash_pay = False
                raise Crash
            return {"paid": True}

        self.registry.register("note", note, effect=note_effect)
        self.registry.register("pay", pay, effect=pay_effect)


def _spec(gated: bool = False) -> WorkflowSpec:
    return WorkflowSpec.from_dict(
        {
            "name": "s9",
            "entry_node": "a",
            "terminal_nodes": ["b"],
            "nodes": {
                "a": {"id": "a", "handler": "note", "default_transition": "b"},
                "b": {"id": "b", "handler": "pay", "requires_approval": gated},
            },
        }
    )


def _app(tmp_path: Path, fakes: Fakes, **kwargs: Any) -> Any:
    return create_api_app(
        registry=fakes.registry,
        db_path=str(tmp_path / "svc.sqlite3"),
        profile="local-owner",
        **kwargs,
    )


async def _crashed_run(app: Any, fakes: Fakes, run_id: str) -> None:
    """A service run whose process stopped inside ``pay`` after the effect happened."""
    spec = _spec()
    store = app.state.store
    await store.record_run_start(run_id, None, spec, {}, journal_workspace=WS)
    app.state.workspace_repo.record_resource_ownership("run", run_id, WS, "local-owner")
    # The registry wrapper the service runs every workflow with.
    registry = RunEventRegistry(fakes.registry, store, run_id)
    engine = WorkflowEngine(spec, registry, store=store.checkpoint_store, workspace_id=WS)
    fakes.crash_pay = True
    with pytest.raises(Crash):
        await engine.run({}, workflow_run_id=run_id)


async def _hold_lease(app: Any, run_id: str, ttl: float) -> None:
    """Another process takes the run's lease (as a live serve/worker recovering it)."""
    journal = await app.state.store.checkpoint_store.open_execution_store()
    eid = ExecutionId(WS, run_id)
    snapshot = await journal.get(eid)
    lease = await journal.claim(
        eid, FOREIGN_OWNER, ttl, definition_digest=snapshot.run.definition_digest
    )
    assert lease is not None


def _studio_row(tmp_path: Path, run_id: str) -> tuple[str, str | None, str | None]:
    # The service's run table: the SQLite file, or Postgres under --service-backend=postgres.
    (row,) = studio_sql(
        tmp_path / "svc.sqlite3",
        "SELECT status, error, journal_workspace FROM nexura_studio_runs WHERE run_id = ?",
        run_id,
    )
    return row[0], row[1], row[2]


def _set_studio_status(tmp_path: Path, run_id: str, status: str) -> None:
    studio_sql(
        tmp_path / "svc.sqlite3",
        "UPDATE nexura_studio_runs SET status = ? WHERE run_id = ?",
        status,
        run_id,
    )


@pytest.fixture
def sleeper() -> Iterator[subprocess.Popen[bytes]]:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait()


# --------------------------------------------------------------- declared effect classes


def test_run_event_registry_passes_declared_effects_through(tmp_path: Path) -> None:
    inner = ModuleRegistry()
    inner.register("p", lambda state: {}, effect="pure")
    inner.register("i", lambda state: {}, effect="idempotent")
    inner.register("u", lambda state: {})
    store = StudioStore(tmp_path / "s.sqlite3")
    try:
        wrapper = RunEventRegistry(inner, store, "run-x")
        assert wrapper.declared_effect("p") == (EffectClass.PURE, None)
        assert wrapper.declared_effect("i") == (EffectClass.IDEMPOTENT, None)
        assert wrapper.declared_effect("u") is None
        assert wrapper.declared_effect("missing") is None
    finally:
        store._close_sync()


def test_service_run_journals_a_pure_handler_as_pure(tmp_path: Path) -> None:
    """S8 finding: every service handler was journaled non_idempotent (no pure fast path)."""
    fakes = Fakes(pay_effect=None, note_effect="pure")
    app = _app(tmp_path, fakes)
    with TestClient(app, raise_server_exceptions=False) as client:
        headers = local_headers(WS)
        started = client.post(f"{API}/runs", json={"spec": _spec().to_dict()}, headers=headers)
        assert started.status_code == 201, started.text
        run_id = started.json()["run_id"]
        assert started.json()["status"] == "completed"
        effects = client.get(f"{API}/runs/{run_id}/effects", headers=headers).json()["effects"]
    # The pure node writes no intent and no effect; only the undeclared pay node does
    # (before the fix both were journaled non_idempotent: two effects).
    assert [e["intent"]["effect_class"] for e in effects] == ["non_idempotent"]

    async def journal_steps() -> list[str]:
        journal = service_execution_store(tmp_path / "svc.sqlite3")
        try:
            snapshot = await journal.get(ExecutionId(WS, run_id))
        finally:
            await journal.close()
        return sorted(
            f"{key}:{'intent' if step.intent else '-'}" for key, step in snapshot.steps.items()
        )

    assert asyncio.run(journal_steps()) == ["a#1:-", "b#2:intent"]
    assert _studio_row(tmp_path, run_id)[2] == WS  # recorded as journaled (S9)


# ------------------------------------------------------------- interrupted from the lease


def test_open_sweep_leaves_journaled_runs_to_their_lease(
    tmp_path: Path, sleeper: subprocess.Popen[bytes]
) -> None:
    """A journaled run whose recorded owner process is gone is not swept on open: the
    journal lease (held by another live process) decides. A legacy row still is (B46)."""
    fakes = Fakes()
    app = _app(tmp_path, fakes)
    asyncio.run(_crashed_run(app, fakes, "run-j"))
    asyncio.run(_hold_lease(app, "run-j", 60))
    legacy_spec = _spec()
    asyncio.run(app.state.store.record_run_start("run-legacy", None, legacy_spec, {}))
    dead = owner_of_process(sleeper.pid)  # this host's format for the run store in use
    sleeper.kill()
    sleeper.wait()
    studio_sql(tmp_path / "svc.sqlite3", "UPDATE nexura_studio_runs SET owner_process = ?", dead)

    reopen_studio_store(tmp_path / "svc.sqlite3")  # a second process opening the store

    assert _studio_row(tmp_path, "run-j")[0] == "running"
    assert _studio_row(tmp_path, "run-legacy")[0] == "interrupted"


def test_live_lease_is_running_and_expiry_makes_it_interrupted(tmp_path: Path) -> None:
    fakes = Fakes()
    app = _app(tmp_path, fakes)
    asyncio.run(_crashed_run(app, fakes, "run-l"))
    # A lease long enough to outlast a slow CI runner's startup, then waited out below.
    asyncio.run(_hold_lease(app, "run-l", 4.0))
    with TestClient(app, raise_server_exceptions=False) as client:  # startup reconcile runs
        headers = local_headers(WS)
        assert client.get(f"{API}/runs/run-l", headers=headers).json()["status"] == "running"
        listed = client.get(f"{API}/runs", params={"status": "running"}, headers=headers)
        assert [r["run_id"] for r in listed.json()["items"]] == ["run-l"]
        time.sleep(4.3)  # the other process stopped renewing: its lease expires
        run = client.get(f"{API}/runs/run-l", headers=headers).json()
        assert run["status"] == "interrupted"
        assert run["error"] == LEASE_EXPIRED_ERROR.replace("{run_id}", "run-l")
        assert [e["type"] for e in run["events"]].count("RUN_INTERRUPTED") == 1
        # Persisted, so status filters agree.
        listed = client.get(f"{API}/runs", params={"status": "interrupted"}, headers=headers)
        assert [r["run_id"] for r in listed.json()["items"]] == ["run-l"]
    assert _studio_row(tmp_path, "run-l")[0] == "interrupted"


def test_startup_marks_a_stopped_journaled_run_interrupted(tmp_path: Path) -> None:
    fakes = Fakes()
    app = _app(tmp_path, fakes)
    asyncio.run(_crashed_run(app, fakes, "run-s"))  # its lease was given back: free
    assert _studio_row(tmp_path, "run-s")[0] == "running"
    with TestClient(app, raise_server_exceptions=False):
        pass
    status, error, _ = _studio_row(tmp_path, "run-s")
    assert status == "interrupted"
    assert error is not None and "run lease expired" in error


def test_interrupted_row_whose_lease_is_live_again_is_shown_running(tmp_path: Path) -> None:
    fakes = Fakes()
    app = _app(tmp_path, fakes)
    asyncio.run(_crashed_run(app, fakes, "run-r"))
    _set_studio_status(tmp_path, "run-r", "interrupted")
    asyncio.run(_hold_lease(app, "run-r", 60))  # another process is recovering it
    with TestClient(app, raise_server_exceptions=False) as client:
        headers = local_headers(WS)
        run = client.get(f"{API}/runs/run-r", headers=headers).json()
        assert run["status"] == "running" and run["error"] is None
        listed = client.get(f"{API}/runs", headers=headers).json()["items"]
        assert [r["status"] for r in listed if r["run_id"] == "run-r"] == ["running"]
        refused = client.post(f"{API}/runs/run-r/recover", headers=headers)
        assert refused.status_code == 409
        assert refused.json()["detail"]["code"] == "RUN_LEASE_HELD"
    assert _studio_row(tmp_path, "run-r")[0] == "interrupted"  # unchanged by the refusal
    assert fakes.calls["pay"] == 1


def test_shutdown_does_not_interrupt_a_run_another_worker_holds(tmp_path: Path) -> None:
    fakes = Fakes()
    app = _app(tmp_path, fakes)
    asyncio.run(_crashed_run(app, fakes, "run-h"))
    asyncio.run(_hold_lease(app, "run-h", 60))
    asyncio.run(
        reconcile_run_statuses(
            app.state, run_ids=["run-h"], error=SHUTDOWN_ERROR, stopped_here=True
        )
    )
    assert _studio_row(tmp_path, "run-h")[0] == "running"


# ---------------------------------------------------------------------------- recover


def test_recover_pauses_on_an_unknown_effect_then_continues_once_resolved(
    tmp_path: Path,
) -> None:
    fakes = Fakes(pay_effect=None)  # undeclared: non_idempotent
    app = _app(tmp_path, fakes)
    asyncio.run(_crashed_run(app, fakes, "run-p"))
    with TestClient(app, raise_server_exceptions=False) as client:
        headers = local_headers(WS)
        assert client.get(f"{API}/runs/run-p", headers=headers).json()["status"] == "interrupted"
        paused = client.post(f"{API}/runs/run-p/recover", headers=headers)
        assert paused.status_code == 200, paused.text
        body = paused.json()
        assert body["status"] == "paused"
        assert body["pause_reason"] == "effect_unknown"
        effect_id = body["pending_effect"]["effect_id"]
        assert fakes.calls["pay"] == 1  # never repeated silently
        types = [e["type"] for e in body["events"]]
        assert "RUN_RECOVERY_STARTED" in types and "RUN_PAUSED_EFFECT_UNKNOWN" in types

        # Recovering again before anyone resolved it changes nothing.
        again = client.post(f"{API}/runs/run-p/recover", headers=headers)
        assert again.status_code == 200 and again.json()["status"] == "paused"
        assert fakes.calls["pay"] == 1

        resolved = client.post(
            f"{API}/runs/run-p/effects/{effect_id}/resolve",
            json={"resolution": "confirmed", "result": {"paid": "confirmed-by-ops"}},
            headers=headers,
        )
        assert resolved.status_code == 200, resolved.text
        done = client.post(f"{API}/runs/run-p/recover", headers=headers)
        assert done.status_code == 200, done.text
        assert done.json()["status"] == "completed"
        assert done.json()["current_state"]["paid"] == "confirmed-by-ops"
        assert done.json()["pending_effect"] is None and done.json()["pause_reason"] is None
        assert fakes.calls == {"note": 1, "pay": 1}

        terminal = client.post(f"{API}/runs/run-p/recover", headers=headers)
        assert terminal.status_code == 409
        assert terminal.json()["detail"]["code"] == "RUN_ALREADY_TERMINAL"


def test_recover_reruns_an_idempotent_step_with_the_same_key(tmp_path: Path) -> None:
    """Needs the RunEventRegistry fix: the intent records ``effect="idempotent"``."""
    fakes = Fakes(pay_effect="idempotent")
    app = _app(tmp_path, fakes)
    asyncio.run(_crashed_run(app, fakes, "run-i"))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            f"{API}/runs/run-i/recover", json={"on_uncertain": "pause"}, headers=local_headers(WS)
        )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    assert fakes.calls == {"note": 1, "pay": 2}
    assert fakes.keys[0] is not None and fakes.keys == [fakes.keys[0]] * 2


def test_recover_with_on_uncertain_fail_fails_the_run(tmp_path: Path) -> None:
    fakes = Fakes(pay_effect=None)
    app = _app(tmp_path, fakes)
    asyncio.run(_crashed_run(app, fakes, "run-f"))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            f"{API}/runs/run-f/recover", json={"on_uncertain": "fail"}, headers=local_headers(WS)
        )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed"
    assert fakes.calls["pay"] == 1


def test_recover_refusals(tmp_path: Path) -> None:
    fakes = Fakes()
    app = _app(tmp_path, fakes)
    app.state.workspace_repo.create_workspace("wsB", "B", owner_id="local-owner")
    asyncio.run(_crashed_run(app, fakes, "run-c"))
    asyncio.run(_hold_lease(app, "run-c", 60))  # a live process executes it
    legacy = _spec()
    asyncio.run(app.state.store.record_run_start("run-old", None, legacy, {}))
    app.state.workspace_repo.record_resource_ownership("run", "run-old", WS, "local-owner")
    _set_studio_status(tmp_path, "run-old", "interrupted")
    with TestClient(app, raise_server_exceptions=False) as client:
        headers = local_headers(WS)

        def code(response: httpx.Response) -> tuple[int, Any]:
            detail = response.json().get("detail")
            return response.status_code, detail.get("code") if isinstance(detail, dict) else None

        assert code(client.post(f"{API}/runs/nope/recover", headers=headers)) == (
            404,
            "RUN_NOT_FOUND",
        )
        other = client.post(f"{API}/runs/run-c/recover", headers=local_headers("wsB"))
        assert code(other) == (403, "CROSS_WORKSPACE_ACCESS_DENIED")
        assert code(client.post(f"{API}/runs/run-c/recover", headers=headers)) == (
            409,
            "RUN_LEASE_HELD",
        )
        assert code(client.post(f"{API}/runs/run-old/recover", headers=headers)) == (
            409,
            "RUN_NOT_JOURNALED",
        )
        bad = client.post(
            f"{API}/runs/run-c/recover", json={"on_uncertain": "retry"}, headers=headers
        )
        assert bad.status_code == 422
        extra = client.post(f"{API}/runs/run-c/recover", json={"actor": "x"}, headers=headers)
        assert extra.status_code == 422

        gated = client.post(
            f"{API}/runs", json={"spec": _spec(gated=True).to_dict()}, headers=headers
        )
        assert gated.json()["status"] == "pending_approval", gated.text
        waiting = client.post(f"{API}/runs/{gated.json()['run_id']}/recover", headers=headers)
        assert code(waiting) == (409, "RUN_WAITING_FOR_PERSON")
    assert _studio_row(tmp_path, "run-c")[0] == "running"
    assert fakes.calls["pay"] == 1


class _RoleProvider:
    """Test-only shared-service identity: X-Test-Role selects the role."""

    __test__ = False
    name = "a6-s9-test-auth"
    is_authenticating = True
    routes_without_membership: frozenset[tuple[str, str]] = frozenset()

    def authenticate(self, request: Request, target_workspace_id: str | None) -> Identity:
        role = request.headers.get("x-test-role", "viewer")
        return Identity(
            Principal(principal_id=f"person-{role}", profile="authenticated-shared-service"),
            target_workspace_id or "default",
            role,  # type: ignore[arg-type]
        )

    def is_operator(self, request: Request) -> bool:
        return False


def test_shared_service_viewer_cannot_recover(tmp_path: Path) -> None:
    fakes = Fakes(pay_effect="idempotent")
    app = create_api_app(
        registry=fakes.registry,
        db_path=str(tmp_path / "svc.sqlite3"),
        profile="authenticated-shared-service",
        auth_provider=_RoleProvider(),
    )
    asyncio.run(_crashed_run(app, fakes, "run-v"))
    app.state.workspace_repo.record_resource_ownership("run", "run-v", WS, "person-editor")
    with TestClient(app, raise_server_exceptions=False) as client:
        denied = client.post(f"{API}/runs/run-v/recover", headers={"X-Test-Role": "viewer"})
        assert denied.status_code == 403
        assert fakes.calls["pay"] == 1
        allowed = client.post(f"{API}/runs/run-v/recover", headers={"X-Test-Role": "editor"})
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["status"] == "completed"


def test_workflow_lease_ttl_is_validated_and_used(tmp_path: Path) -> None:
    for bad in (0, -1, float("inf"), True, "30"):
        with pytest.raises(ValueError, match="workflow_lease_ttl"):
            create_api_app(db_path=str(tmp_path / "x.sqlite3"), workflow_lease_ttl=bad)
    app = _app(tmp_path, Fakes(), workflow_lease_ttl=5.0)
    assert app.state.workflow_lease_ttl == 5.0


async def test_python_client_recover_encodes_the_id_and_body() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"run_id": "run 1", "status": "completed"})

    client = NexuraClient(
        base_url="http://mock-service",
        workspace_id="wsA",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert (await client.runs.recover("run 1"))["status"] == "completed"
    await client.runs.recover("run 1", on_uncertain="fail")
    assert calls[0].method == "POST" and calls[0].url.raw_path == b"/api/v1/runs/run%201/recover"
    assert json.loads(calls[0].content) == {}
    assert json.loads(calls[1].content) == {"on_uncertain": "fail"}
    assert NexuraClient.COVERED_ROUTES[("POST", "/api/v1/runs/{run_id}/recover")] == (
        "runs",
        "recover",
    )
