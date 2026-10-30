# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Two service instances on one Postgres database (MVP Postgres WP3, design §4 WP3 row).

Apps A and B share one Postgres schema and one blob folder, as two Cloud Run instances share
the database and the bucket; each has its *own* SQLite file, so nothing about runs can come
from a shared file. Covered:

* a run started through A is visible, with its events, through B;
* two people approving the same gated run at the same moment, one through each instance: one
  wins and the other gets 409 ``APPROVAL_ALREADY_CLAIMED``; the gated step runs once;
* two people resolving the same indeterminate effect at the same moment through A and B: one
  resolution is recorded, the other request is refused with 409; the run then continues once.

Both racing requests are held at the contended call until both have reached it (an
``asyncio`` gate), so the race really happens. Skipped without ``NEXURA_TEST_POSTGRES_URI``;
all handlers are local fakes, nothing calls a model or the network.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import httpx
import pytest
from _local_env import local_headers
from nexura_agents.contracts.execution import ExecutionId

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.runs import RunEventRegistry
from nexura_runtime.workflow import ModuleRegistry, WorkflowEngine, WorkflowSpec

API = "/api/v1"
WS = "default"


class Crash(BaseException):
    """Stops the engine mid-node the way a killed process would."""


class Fakes:
    def __init__(self) -> None:
        self.calls = {"note": 0, "pay": 0}
        self.crash_pay = False
        self.registry = ModuleRegistry()

        async def note(state: dict[str, Any]) -> dict[str, Any]:
            self.calls["note"] += 1
            return {"noted": True}

        async def pay(state: dict[str, Any]) -> dict[str, Any]:
            self.calls["pay"] += 1
            if self.crash_pay:
                self.crash_pay = False
                raise Crash
            return {"paid": True}

        self.registry.register("note", note, effect="pure")
        self.registry.register("pay", pay)  # undeclared: non_idempotent


def _spec(gated: bool = False) -> WorkflowSpec:
    return WorkflowSpec.from_dict(
        {
            "name": "wp3-two-instances",
            "entry_node": "a",
            "terminal_nodes": ["b"],
            "nodes": {
                "a": {"id": "a", "handler": "note", "default_transition": "b"},
                "b": {"id": "b", "handler": "pay", "requires_approval": gated},
            },
        }
    )


class Gate:
    """Holds each caller until ``parties`` callers have arrived (or ``timeout`` passed)."""

    def __init__(self, parties: int = 2, timeout: float = 10.0) -> None:
        self.parties = parties
        self.timeout = timeout
        self.arrived = 0
        self.event = asyncio.Event()

    def wrap(self, fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        async def gated(*args: Any, **kwargs: Any) -> Any:
            self.arrived += 1
            if self.arrived >= self.parties:
                self.event.set()
            await asyncio.wait_for(self.event.wait(), self.timeout)
            return await fn(*args, **kwargs)

        return gated


@pytest.fixture
async def instances(
    service_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Any, Any, httpx.AsyncClient, httpx.AsyncClient, Fakes]]:
    monkeypatch.setenv("NEXURA_ALLOW_PARTIAL_POSTGRES", "1")
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    fakes = Fakes()
    blobs = tmp_path / "blobs"

    def make(name: str) -> Any:
        return create_api_app(
            registry=fakes.registry,
            db_path=tmp_path / name / "svc.sqlite3",  # each instance has its own file
            database_url=service_database_url,
            blob_url=blobs.as_uri(),
            profile="local-owner",
        )

    app_a, app_b = make("a"), make("b")
    async with AsyncExitStack() as stack:
        for app in (app_a, app_b):
            await stack.enter_async_context(app.router.lifespan_context(app))
        a = await stack.enter_async_context(
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app_a), base_url="http://127.0.0.1")
        )
        b = await stack.enter_async_context(
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app_b), base_url="http://127.0.0.1")
        )
        yield app_a, app_b, a, b, fakes


async def test_a_run_started_on_one_instance_is_read_on_the_other(instances: Any) -> None:
    app_a, app_b, a, b, fakes = instances
    headers = local_headers(WS)
    started = await a.post(f"{API}/runs", json={"spec": _spec().to_dict()}, headers=headers)
    assert started.status_code == 201, started.text
    run_id = started.json()["run_id"]
    seen = await b.get(f"{API}/runs/{run_id}", headers=headers)
    assert seen.status_code == 200, seen.text
    assert seen.json()["status"] == "completed"
    assert [e["seq"] for e in seen.json()["events"]] == list(
        range(1, len(seen.json()["events"]) + 1)
    )
    listed = await b.get(f"{API}/runs", headers=headers)
    assert [r["run_id"] for r in listed.json()["items"]] == [run_id]
    # Nothing about the run is in either instance's SQLite file.
    for app in (app_a, app_b):
        assert getattr(app.state.store, "database", None) is not None


async def test_double_approve_across_instances_is_claimed_once(instances: Any) -> None:
    app_a, app_b, a, b, fakes = instances
    headers = local_headers(WS)
    started = await a.post(
        f"{API}/runs", json={"spec": _spec(gated=True).to_dict()}, headers=headers
    )
    assert started.status_code == 201, started.text
    body = started.json()
    assert body["status"] == "pending_approval", body
    run_id, approval_id = body["run_id"], body["pending_approval"]["approval_id"]
    assert (await b.get(f"{API}/runs/{run_id}", headers=headers)).json()[
        "status"
    ] == "pending_approval"

    gate = Gate()
    for app in (app_a, app_b):
        checkpoints = app.state.store.checkpoint_store
        checkpoints.claim_approval = gate.wrap(checkpoints.claim_approval)
    decision = {"approval_id": approval_id, "decision": "approve"}
    results = await asyncio.gather(
        a.post(f"{API}/runs/{run_id}/decide", json=decision, headers=headers),
        b.post(f"{API}/runs/{run_id}/decide", json=decision, headers=headers),
    )
    assert gate.arrived == 2  # both requests reached the claim
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409], [r.text for r in results]
    refused = next(r for r in results if r.status_code == 409)
    assert refused.json()["detail"]["code"] == "APPROVAL_ALREADY_CLAIMED"
    assert fakes.calls["pay"] == 1  # the gated step ran once
    for client in (a, b):
        final = (await client.get(f"{API}/runs/{run_id}", headers=headers)).json()
        assert final["status"] == "completed"
        seqs = [e["seq"] for e in final["events"]]
        assert seqs == list(range(1, len(seqs) + 1))  # unique and gap-free


async def _paused_on_an_unknown_effect(app: Any, client: httpx.AsyncClient, fakes: Fakes) -> str:
    """A run whose worker died inside ``pay``; recovering it pauses on the unknown effect."""
    run_id = "run-effect"
    spec = _spec()
    store = app.state.store
    await store.record_run_start(run_id, None, spec, {}, journal_workspace=WS)
    app.state.workspace_repo.record_resource_ownership("run", run_id, WS, "local-owner")
    engine = WorkflowEngine(
        spec,
        RunEventRegistry(fakes.registry, store, run_id),
        store=store.checkpoint_store,
        workspace_id=WS,
    )
    fakes.crash_pay = True
    with pytest.raises(Crash):
        await engine.run({}, workflow_run_id=run_id)
    paused = await client.post(f"{API}/runs/{run_id}/recover", headers=local_headers(WS))
    assert paused.status_code == 200, paused.text
    assert paused.json()["pause_reason"] == "effect_unknown"
    return str(paused.json()["pending_effect"]["effect_id"])


async def test_effect_resolve_race_across_instances_records_one_resolution(
    instances: Any,
) -> None:
    app_a, app_b, a, b, fakes = instances
    headers = local_headers(WS)
    effect_id = await _paused_on_an_unknown_effect(app_a, a, fakes)
    assert fakes.calls["pay"] == 1
    listed = await b.get(f"{API}/runs/run-effect/effects", headers=headers)
    assert [e["status"] for e in listed.json()["effects"]] == ["indeterminate"]

    gate = Gate()
    for app in (app_a, app_b):
        journal = app.state.execution_store
        journal.claim = gate.wrap(journal.claim)
    results = await asyncio.gather(
        a.post(
            f"{API}/runs/run-effect/effects/{effect_id}/resolve",
            json={"resolution": "confirmed", "result": {"paid": "by-a"}},
            headers=headers,
        ),
        b.post(
            f"{API}/runs/run-effect/effects/{effect_id}/resolve",
            json={"resolution": "not_applied", "reason": "by-b"},
            headers=headers,
        ),
    )
    assert gate.arrived == 2
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409], [r.text for r in results]
    refused = next(r for r in results if r.status_code == 409)
    assert refused.json()["detail"]["code"] in {"RUN_LEASE_HELD", "EFFECT_ALREADY_RESOLVED"}
    winner = next(r for r in results if r.status_code == 200).json()
    snapshot = await app_b.state.execution_store.get(ExecutionId(WS, "run-effect"))
    (effect,) = snapshot.effects.values()
    assert effect.status.value == "resolved"
    recorded = getattr(effect.resolution, "value", effect.resolution)
    assert recorded == winner["resolution"]  # the one recorded is the winner's

    # The run continues exactly once, from either instance.
    for app in (app_a, app_b):
        del app.state.execution_store.claim  # the gate is removed again
    done = await b.post(f"{API}/runs/run-effect/recover", headers=headers)
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "completed"
    assert fakes.calls["pay"] == (1 if winner["resolution"] == "confirmed" else 2)
