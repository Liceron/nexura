"""P26: agents as a workspace resource (CRUD, roles, caps, refusals) and the agent node.

Offline only: saved agents run on the server's offline fixture or on scripted ``FakeModel``
instances registered as chat models; the write tool is a local recording module.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from _local_env import local_headers
from fastapi.testclient import TestClient
from nexura_agents import FakeModel
from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import ModuleManifest
from nexura_agents.contracts.result import ModuleResult
from nexura_agents.types import Message, ToolCall

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.workspaces.repository import WorkspaceRepository
from nexura_runtime.workflow import ModuleRegistry

API = "/api/v1"
WRITE_ID = "test.ticket.create"
WRITE_TOOL = "test_ticket_create"
READ_ID = "test.lookup.read"
INTERNAL_ID = "test.internal.svc"


class TicketModule(BaseModule):
    """A write module (always_write): records each call it actually performs."""

    def __init__(self) -> None:
        self.created: list[str] = []
        self.contexts: list[ExecutionContext] = []
        self._manifest = ModuleManifest(
            module_id=WRITE_ID,
            version="1.0.0",
            kind="integration",
            title="Create ticket",
            description="Create a support ticket (writes).",
            input_schema={
                "type": "object",
                "properties": {"title": {"type": "string"}},
                "required": ["title"],
            },
            effect_policy="non_idempotent_write",
            always_write=True,
        )

    @property
    def manifest(self) -> ModuleManifest:
        return self._manifest

    async def invoke(
        self, context: ExecutionContext, inputs: dict[str, Any], config: dict[str, Any]
    ) -> ModuleResult:
        context.require_write_approval("Ticket creation")
        self.created.append(str(inputs["title"]))
        self.contexts.append(context)
        return ModuleResult.success({"ticket_id": f"T-{len(self.created)}"})


class LookupModule(BaseModule):
    def __init__(self, *, internal: bool = False, module_id: str = READ_ID) -> None:
        self._manifest = ModuleManifest(
            module_id=module_id,
            version="1.0.0",
            kind="integration",
            title="Lookup",
            description="Read-only lookup.",
            input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
            effect_policy="read",
            internal=internal,
        )

    @property
    def manifest(self) -> ModuleManifest:
        return self._manifest

    async def invoke(
        self, context: ExecutionContext, inputs: dict[str, Any], config: dict[str, Any]
    ) -> ModuleResult:
        return ModuleResult.success({"answer": "42"})


def _writer(messages: list[Message], tools: list[dict[str, Any]] | None) -> Any:
    """Scripted model: asks the ticket tool once, then reports the tool result."""
    last = messages[-1]
    if last.role == "tool":
        return f"Ticket created: {last.content}"
    return [ToolCall(id="call_t1", name=WRITE_TOOL, arguments={"title": str(last.content)})]


class Spy:
    def __init__(self) -> None:
        self.system: list[str] = []
        self.tools: list[list[str]] = []

    def __call__(self, messages: list[Message], tools: list[dict[str, Any]] | None) -> Any:
        self.system.append(next((m.content or "" for m in messages if m.role == "system"), ""))
        self.tools.append([t.get("name") or t["function"]["name"] for t in tools or []])
        return "spied"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("NEXURA_INSECURE_ALLOW_PLAINTEXT_SECRETS", "I_UNDERSTAND_THIS_IS_UNSAFE")
    repo = WorkspaceRepository()
    repo.create_workspace("wsA", "A", owner_id="local-owner")
    repo.create_workspace("wsB", "B", owner_id="local-owner")
    headers: dict[str, dict[str, str]] = {}
    for label, ws, role in (
        ("admin", "wsA", "admin"),
        ("editor", "wsA", "editor"),
        ("viewer", "wsA", "viewer"),
        ("other", "wsB", "admin"),
    ):
        headers[label] = local_headers(ws, role)
    ticket = TicketModule()
    registry = ModuleRegistry()
    registry.register(WRITE_ID, ticket)
    registry.register(READ_ID, LookupModule())
    registry.register(INTERNAL_ID, LookupModule(internal=True, module_id=INTERNAL_ID))
    spy = Spy()
    db = tmp_path / "s.db"

    def make_app(**overrides: Any) -> Any:
        models = {"scripted": FakeModel(handler=_writer), "spy": FakeModel(handler=spy)}
        return create_api_app(
            registry=registry,
            db_path=db,
            workspace_repo=repo,
            profile="local-owner",
            chat_models=overrides.get("chat_models", models),
        )

    return {
        "c": TestClient(make_app()),
        "make_app": make_app,
        "headers": headers,
        "ticket": ticket,
        "spy": spy,
    }


def _spec(name: str = "helper", **kw: Any) -> dict[str, Any]:
    return {"name": name, "instructions": "Help the user.", **kw}


def _create(env: dict[str, Any], body: dict[str, Any], who: str = "admin") -> Any:
    return env["c"].post(f"{API}/agents", headers=env["headers"][who], json=body)


def _agent_id(env: dict[str, Any], body: dict[str, Any], who: str = "admin") -> str:
    res = _create(env, body, who)
    assert res.status_code == 201, res.text
    return str(res.json()["agent_id"])


def _workflow(
    env: dict[str, Any], config: dict[str, Any], who: str = "admin", c: TestClient | None = None
) -> str:
    c = c or env["c"]
    spec = {
        "spec_version": 1,
        "name": "agentic",
        "entry_node": "ask",
        "nodes": {
            "ask": {
                "id": "ask",
                "handler": "nexura.agents.agent",
                "module_id": "nexura.agents.agent",
                "config": config,
            }
        },
        "terminal_nodes": ["ask"],
    }
    wf = c.post(f"{API}/workflows", headers=env["headers"][who], json={"spec": spec})
    assert wf.status_code == 201, wf.text
    return str(wf.json()["workflow_id"])


def _run(
    env: dict[str, Any],
    workflow_id: str,
    state: dict[str, Any],
    who: str = "admin",
    c: TestClient | None = None,
) -> dict[str, Any]:
    c = c or env["c"]
    h = env["headers"][who]
    run = c.post(
        f"{API}/runs", headers=h, json={"workflow_id": workflow_id, "initial_state": state}
    )
    assert run.status_code in (200, 201), run.text
    data: dict[str, Any] = c.get(f"{API}/runs/{run.json()['run_id']}", headers=h).json()
    return data


def _decide(env: dict[str, Any], run: dict[str, Any], decision: str) -> dict[str, Any]:
    c, h = env["c"], env["headers"]["admin"]
    approval_id = run["pending_approval"]["approval_id"]
    res = c.post(
        f"{API}/runs/{run['run_id']}/decide",
        headers=h,
        json={"approval_id": approval_id, "decision": decision},
    )
    assert res.status_code == 200, res.text
    data: dict[str, Any] = c.get(f"{API}/runs/{run['run_id']}", headers=h).json()
    return data


# --------------------------------------------------------------------------- CRUD


def test_crud_revisions_and_conflicts(env: dict[str, Any]) -> None:
    c, h = env["c"], env["headers"]["admin"]
    listed = c.get(f"{API}/agents", headers=h).json()
    assert listed["items"] == [] and listed["limits"]["max_agents_per_workspace"] == 100
    created = _create(env, _spec(tools=[READ_ID, WRITE_ID]))
    assert created.status_code == 201, created.text
    body = created.json()
    aid = body["agent_id"]
    assert aid.startswith("agt_") and body["revision"] == 1
    assert body["spec"]["model"] == "fake-deterministic"
    assert body["spec"]["limits"] == {"max_turns": 6, "max_tool_calls": 10, "timeout_seconds": 60.0}
    # Writes are forced to approval; reads are not.
    assert {t["module_id"]: t["requires_approval"] for t in body["tools"]} == {
        READ_ID: False,
        WRITE_ID: True,
    }
    assert c.get(f"{API}/agents/{aid}", headers=h).json()["spec"]["name"] == "helper"
    assert [i["agent_id"] for i in c.get(f"{API}/agents", headers=h).json()["items"]] == [aid]

    updated = c.put(
        f"{API}/agents/{aid}",
        headers=h,
        json={**_spec(description="v2"), "expected_revision": 1},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["revision"] == 2
    stale = c.put(f"{API}/agents/{aid}", headers=h, json={**_spec(), "expected_revision": 1})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "REVISION_CONFLICT"
    assert stale.json()["detail"]["current_revision"] == 2
    # Saving the same spec again is not a new revision.
    same = c.put(
        f"{API}/agents/{aid}", headers=h, json={**_spec(description="v2"), "expected_revision": 2}
    )
    assert same.json()["revision"] == 2
    missing = c.put(f"{API}/agents/{aid}", headers=h, json=_spec())
    assert missing.status_code == 422
    assert missing.json()["detail"]["code"] == "EXPECTED_REVISION_REQUIRED"
    # Names are unique per workspace.
    clash = _create(env, _spec())
    assert clash.status_code == 409 and clash.json()["detail"]["code"] == "AGENT_NAME_TAKEN"
    other = _agent_id(env, _spec("other"))
    rename = c.put(f"{API}/agents/{other}", headers=h, json={**_spec(), "expected_revision": 1})
    assert rename.status_code == 409

    gone = c.delete(f"{API}/agents/{aid}?expected_revision=1", headers=h)
    assert gone.status_code == 409
    assert c.delete(f"{API}/agents/{aid}?expected_revision=2", headers=h).status_code == 200
    res = c.get(f"{API}/agents/{aid}", headers=h)
    assert res.status_code == 404 and res.json()["detail"]["code"] == "AGENT_NOT_FOUND"
    assert c.delete(f"{API}/agents/{aid}", headers=h).status_code == 404


def test_list_names_tools_and_the_tools_that_need_approval(env: dict[str, Any]) -> None:
    # Studio's agent list shows an approval badge without fetching every agent.
    _agent_id(env, _spec("mixed", tools=[READ_ID, WRITE_ID]))
    _agent_id(env, _spec("reader", tools=[READ_ID]))
    _agent_id(env, _spec("bare"))
    listed = env["c"].get(f"{API}/agents", headers=env["headers"]["viewer"])
    assert listed.status_code == 200, listed.text
    items = {i["name"]: i for i in listed.json()["items"]}
    assert items["mixed"]["tools"] == [READ_ID, WRITE_ID]
    assert items["mixed"]["tool_count"] == 2
    assert items["mixed"]["approval_tools"] == [WRITE_ID]
    assert items["reader"]["approval_tools"] == []
    assert items["bare"]["tools"] == [] and items["bare"]["approval_tools"] == []


def test_agents_survive_restart(env: dict[str, Any]) -> None:
    aid = _agent_id(env, _spec())
    reborn = TestClient(env["make_app"]())
    assert reborn.get(f"{API}/agents/{aid}", headers=env["headers"]["admin"]).status_code == 200


def test_workspace_isolation(env: dict[str, Any]) -> None:
    c, headers = env["c"], env["headers"]
    aid = _agent_id(env, _spec())
    other = headers["other"]
    assert c.get(f"{API}/agents", headers=other).json()["items"] == []
    for method in ("GET", "PUT", "DELETE"):
        res = c.request(
            method, f"{API}/agents/{aid}", headers=other, json={**_spec(), "expected_revision": 1}
        )
        assert res.status_code == 404, (method, res.text)
        assert res.json()["detail"]["code"] == "AGENT_NOT_FOUND"
    # The same name is free in another workspace.
    assert _create(env, _spec(), "other").status_code == 201
    assert c.get(f"{API}/agents/{aid}", headers=headers["admin"]).status_code == 200


@pytest.mark.parametrize(
    "body,code,field",
    [
        (_spec(model="gpt-unknown"), "MODEL_NOT_AVAILABLE", "model"),
        (_spec(tools=["ghost.module"]), "AGENT_INVALID", "tools.0"),
        (_spec(tools=[INTERNAL_ID]), "AGENT_INVALID", "tools.0"),
        (_spec(tools=["nexura.evaluation.runner"]), "AGENT_INVALID", "tools.0"),
        (_spec(tools=["nexura.agents.coordinator"]), "AGENT_INVALID", "tools.0"),
        (_spec(tools=[READ_ID, READ_ID]), "AGENT_INVALID", "tools"),
        (_spec(knowledge=["nope"]), "AGENT_INVALID", "knowledge.0"),
        (_spec(skills=["ghost"]), "AGENT_INVALID", "skills.0"),
        (_spec(output_schema={"type": "bogus"}), "AGENT_INVALID", "output_schema"),
        (_spec(output_schema={"type": "string", "format": "email"}), "AGENT_INVALID", None),
        (_spec(output_schema={"$ref": "#/$defs/missing"}), "AGENT_INVALID", None),
        (_spec(instructions="x" * 20_001), "AGENT_INVALID", "instructions"),
        (_spec(name="Bad Name"), "AGENT_INVALID", "name"),
        (_spec(code="print('hi')"), "AGENT_INVALID", "code"),
        (_spec(limits={"max_turns": 99}), "AGENT_INVALID", "limits.max_turns"),
        (_spec(limits={"timeout_seconds": 10_000}), "AGENT_INVALID", "limits.timeout_seconds"),
        (_spec(output_schema={"description": "d" * 17_000}), "AGENT_INVALID", "output_schema"),
    ],
)
def test_validation_refusals(
    env: dict[str, Any], body: dict[str, Any], code: str, field: str | None
) -> None:
    res = _create(env, body)
    assert res.status_code == 422, res.text
    detail = res.json()["detail"]
    assert detail["code"] == code and detail["message"]
    if field is not None:
        assert field in [e["field"] for e in detail["field_errors"]], detail


def test_size_limits_and_agent_cap(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import nexura_runtime.agents_store as agents_store

    # The largest allowed fields still save (one character more: test_validation_refusals).
    assert (
        _create(env, _spec("big", instructions="x" * 20_000, description="d" * 500)).status_code
        == 201
    )
    monkeypatch.setattr(agents_store, "MAX_AGENTS_PER_WORKSPACE", 3)  # "big" is one
    _agent_id(env, _spec("a1"))
    _agent_id(env, _spec("a2"))
    third = _create(env, _spec("a3"))
    assert third.status_code == 413 and third.json()["detail"]["code"] == "TOO_MANY_AGENTS"
    # The cap is per workspace.
    assert _create(env, _spec("a3"), "other").status_code == 201


def test_options_list_what_a_spec_may_name(env: dict[str, Any]) -> None:
    opts = env["c"].get(f"{API}/agents/options", headers=env["headers"]["admin"]).json()
    tools = {t["module_id"]: t for t in opts["tools"]}
    assert tools[WRITE_ID]["requires_approval"] is True
    assert tools[READ_ID]["requires_approval"] is False
    assert INTERNAL_ID not in tools and "nexura.evaluation.runner" not in tools
    assert "nexura.agents.agent" not in tools and "nexura.agents.coordinator" not in tools
    assert {m["name"] for m in opts["models"]} >= {"fake-deterministic", "scripted"}
    assert opts["knowledge_collections"] == ["default"]
    assert opts["node_module_id"] == "nexura.agents.agent"


# --------------------------------------------------------------------- agent node


def test_agent_node_reads_input_key_and_writes_output_key(env: dict[str, Any]) -> None:
    aid = _agent_id(env, _spec("echo"))
    wf = _workflow(env, {"agent_id": aid, "input_key": "question", "output_key": "answer"})
    run = _run(env, wf, {"question": "hello there"})
    assert run["status"] == "completed", run
    state = run["current_state"]
    assert state["answer"] == "Offline fake reply from agent 'echo': hello there"
    assert "output" not in state
    # Defaults: input -> output.
    run = _run(env, _workflow(env, {"agent_id": aid}), {"input": "hi"})
    assert run["current_state"]["output"].endswith(": hi")
    # A missing input key fails the node.
    assert _run(env, wf, {"other": 1})["status"] == "failed"


def test_structured_output_schema(env: dict[str, Any]) -> None:
    schema = {
        "type": "object",
        "properties": {"priority": {"enum": ["low", "high"]}, "count": {"type": "integer"}},
        "required": ["priority", "count"],
        "additionalProperties": False,
    }
    aid = _agent_id(env, _spec("triage", output_schema=schema))
    run = _run(env, _workflow(env, {"agent_id": aid}), {"input": "x"})
    assert run["status"] == "completed", run
    assert run["current_state"]["output"] == {"priority": "low", "count": 0}


def test_write_tool_pauses_for_approval_then_runs_once(env: dict[str, Any]) -> None:
    ticket = env["ticket"]
    aid = _agent_id(env, _spec("filer", model="scripted", tools=[WRITE_ID]))
    wf = _workflow(env, {"agent_id": aid, "input_key": "q", "output_key": "a"})
    paused = _run(env, wf, {"q": "printer broken"})
    assert paused["status"] == "pending_approval", paused
    assert ticket.created == []  # nothing written before the decision
    details = paused["pending_approval"]["details"]
    assert details["kind"] == "agent_tool_calls" and details["node_request"] is True
    assert details["agent_id"] == aid
    assert [(c["tool"], c["arguments"]) for c in details["calls"]] == [
        (WRITE_TOOL, {"title": "printer broken"})
    ]
    done = _decide(env, paused, "approve")
    assert done["status"] == "completed", done
    assert ticket.created == ["printer broken"]
    assert done["current_state"]["a"].startswith("Ticket created:")
    # The tool ran with the run's workspace and only that call was approved.
    ctx = ticket.contexts[-1]
    assert ctx.workspace_id == "wsA" and ctx.run_id == paused["run_id"]


def test_rejected_write_never_runs(env: dict[str, Any]) -> None:
    ticket = env["ticket"]
    aid = _agent_id(env, _spec("filer", model="scripted", tools=[WRITE_ID]))
    paused = _run(env, _workflow(env, {"agent_id": aid}), {"input": "x"})
    assert paused["status"] == "pending_approval"
    out = _decide(env, paused, "reject")
    assert out["status"] == "rejected"
    assert ticket.created == []


def test_agent_edited_while_paused_is_refused(env: dict[str, Any]) -> None:
    c, h = env["c"], env["headers"]["admin"]
    aid = _agent_id(env, _spec("filer", model="scripted", tools=[WRITE_ID]))
    paused = _run(env, _workflow(env, {"agent_id": aid}), {"input": "x"})
    c.put(
        f"{API}/agents/{aid}",
        headers=h,
        json={
            **_spec("filer", model="scripted", tools=[WRITE_ID], description="new"),
            "expected_revision": 1,
        },
    )
    out = _decide(env, paused, "approve")
    # S12 round 7 (R6-1): the node already ran once (its agent asked for the write), so the
    # refused re-run cannot prove nothing happened: the run pauses effect_unknown for a
    # person instead of failing; the agent's refusal is in the run's events.
    assert out["status"] == "paused", out
    assert "edited after this approval" in json.dumps(out["events"])  # the module's refusal
    assert env["ticket"].created == []
    effects = c.get(f"{API}/runs/{paused['run_id']}/effects", headers=h).json()["effects"]
    assert [e["status"] for e in effects] == ["indeterminate"]


def test_cross_workspace_agent_id_refused_at_run(env: dict[str, Any]) -> None:
    aid = _agent_id(env, _spec("mine"))
    wf = _workflow(env, {"agent_id": aid}, who="other")
    run = _run(env, wf, {"input": "x"}, who="other")
    assert run["status"] == "failed"
    assert "not found" in str(run["error"])


def test_unknown_model_or_deleted_agent_refused_at_run(env: dict[str, Any]) -> None:
    aid = _agent_id(env, _spec("filer", model="scripted"))
    # Same database, but this server does not offer "scripted".
    other = TestClient(env["make_app"](chat_models={}))
    wf = _workflow(env, {"agent_id": aid}, c=other)
    run = _run(env, wf, {"input": "x"}, c=other)
    assert run["status"] == "failed" and "scripted" in str(run["error"])
    env["c"].delete(f"{API}/agents/{aid}", headers=env["headers"]["admin"])
    run = _run(env, _workflow(env, {"agent_id": aid}), {"input": "x"})
    assert run["status"] == "failed" and "not found" in str(run["error"])


def test_skills_are_attached_at_run_time(env: dict[str, Any]) -> None:
    c, h, spy = env["c"], env["headers"]["admin"], env["spy"]
    md = "---\nname: refunds\ndescription: How to handle refunds.\n---\nRefund within 30 days."
    assert c.post(f"{API}/skills", headers=h, json={"markdown": md}).status_code == 201
    aid = _agent_id(env, _spec("skilled", model="spy", skills=["refunds"]))
    wf = _workflow(env, {"agent_id": aid})
    # No production label yet: the run fails closed.
    assert _run(env, wf, {"input": "x"})["status"] == "failed"
    c.put(f"{API}/skills/refunds/labels/production", headers=h, json={"version": 1})
    run = _run(env, wf, {"input": "x"})
    assert run["status"] == "completed", run
    assert "Available skills" in spy.system[-1] and "refunds" in spy.system[-1]
    assert "load_skill" in spy.tools[-1]


def test_knowledge_collections_are_searched(env: dict[str, Any]) -> None:
    c, h = env["c"], env["headers"]["admin"]
    up = c.post(
        f"{API}/knowledge/documents/upload",
        headers=h,
        json={
            "collection_id": "default",
            "filename": "refunds.txt",
            "content": "Refunds are issued within 30 days of purchase.",
        },
    )
    assert up.status_code in (200, 201), up.text
    aid = _agent_id(env, _spec("librarian", knowledge=["default"]))
    detail = c.get(f"{API}/agents/{aid}", headers=h).json()
    assert detail["spec"]["knowledge"] == ["default"]
    session = c.post(f"{API}/sessions", headers=h, json={}).json()["session_id"]
    chat = c.post(
        f"{API}/sessions/{session}/chat",
        headers=h,
        json={"message": "search the docs: refund window?", "agent_id": aid},
    )
    assert chat.status_code == 200, chat.text
    body = chat.json()
    assert [t["tool"] for t in body["tool_executions"]] == ["knowledge_search"]
    assert "refunds.txt" in body["reply"] and "30 days" in body["reply"]


# ----------------------------------------------------------------- playground


def test_playground_chats_with_a_saved_agent(env: dict[str, Any]) -> None:
    c, h = env["c"], env["headers"]["admin"]
    aid = _agent_id(env, _spec("echo"))
    session = c.post(f"{API}/sessions", headers=h, json={}).json()["session_id"]
    res = c.post(
        f"{API}/sessions/{session}/chat", headers=h, json={"message": "hi", "agent_id": aid}
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["reply"] == "Offline fake reply from agent 'echo': hi"
    assert body["agent_id"] == aid and body["model_name"] == "fake-deterministic"
    assert body["refused_tool_calls"] == []
    # Without agent_id the demo playground answers as before.
    demo = c.post(f"{API}/sessions/{session}/chat", headers=h, json={"message": "hello"}).json()
    assert "Nexura Assistant" in demo["reply"] and "agent_id" not in demo


def test_playground_never_runs_write_tools(env: dict[str, Any]) -> None:
    c, h, ticket = env["c"], env["headers"]["admin"], env["ticket"]
    aid = _agent_id(env, _spec("filer", model="scripted", tools=[WRITE_ID]))
    session = c.post(f"{API}/sessions", headers=h, json={}).json()["session_id"]
    res = c.post(
        f"{API}/sessions/{session}/chat", headers=h, json={"message": "file it", "agent_id": aid}
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["refused_tool_calls"] == [{"id": "call_t1", "tool": WRITE_TOOL}]
    assert ticket.created == []
    assert "needs human approval" in body["reply"]
    # The streaming endpoint refuses the same way.
    with c.stream(
        "POST",
        f"{API}/sessions/{session}/chat/stream",
        headers=h,
        json={"message": "file again", "agent_id": aid},
    ) as stream:
        text = "".join(stream.iter_text())
    assert "event: chat_completed" in text and WRITE_TOOL in text
    assert '"messages": [{' in text  # final body only; paused snapshots are not streamed
    assert "RunInterruption" not in text
    assert ticket.created == []


def test_playground_agent_refusals(env: dict[str, Any]) -> None:
    c, headers = env["c"], env["headers"]
    aid = _agent_id(env, _spec("filer", model="scripted", tools=[WRITE_ID]))
    session = c.post(f"{API}/sessions", headers=headers["admin"], json={}).json()["session_id"]
    url = f"{API}/sessions/{session}/chat"
    wrong = c.post(
        url, headers=headers["admin"], json={"message": "x", "agent_id": aid, "model": "spy"}
    )
    assert wrong.status_code == 422 and wrong.json()["detail"]["code"] == "AGENT_MODEL_FIXED"
    ghost = c.post(url, headers=headers["admin"], json={"message": "x", "agent_id": "agt_nope"})
    assert ghost.status_code == 404
    # Another workspace's agent is not found from its own sessions.
    other_session = c.post(f"{API}/sessions", headers=headers["other"], json={}).json()[
        "session_id"
    ]
    res = c.post(
        f"{API}/sessions/{other_session}/chat",
        headers=headers["other"],
        json={"message": "x", "agent_id": aid},
    )
    assert res.status_code == 404
    # A forged viewer header cannot lower the local owner role.
    viewer_session = c.post(f"{API}/sessions", headers=headers["admin"], json={}).json()
    res = c.post(
        f"{API}/sessions/{viewer_session['session_id']}/chat",
        headers=headers["viewer"],
        json={"message": "x", "agent_id": aid},
    )
    assert res.status_code == 200, res.text
    assert res.json()["refused_tool_calls"] == [{"id": "call_t1", "tool": WRITE_TOOL}]
    assert "needs human approval" in res.json()["reply"]
    assert env["ticket"].created == []


# ------------------------------------------------------------- eval / red-team


def test_saved_agent_is_an_eval_target(env: dict[str, Any]) -> None:
    c, h = env["c"], env["headers"]["admin"]
    aid = _agent_id(env, _spec("echo"))
    base = f"{API}/evaluations/studio"
    targets = c.get(f"{base}/targets", headers=h).json()["targets"]
    saved = [t for t in targets if t.get("saved")]
    assert saved == [
        {"kind": "agent", "id": f"agent:{aid}", "name": "echo", "version": "rev_1", "saved": True}
    ]
    others = c.get(f"{base}/targets", headers=env["headers"]["other"]).json()["targets"]
    assert not [t for t in others if t.get("saved")]
    cases = [{"name": "hi", "input": "hi", "expected_output": "hi"}]
    assert c.put(f"{base}/datasets/ds1", headers=h, json={"cases": cases}).status_code in (
        200,
        201,
    )
    run = c.post(
        f"{base}/runs",
        headers=h,
        json={"dataset_id": "ds1", "target": {"kind": "agent", "id": f"agent:{aid}"}},
    )
    assert run.status_code == 200, run.text
    assert run.json()["target"]["version"] == "rev_1"
    ghost = c.post(
        f"{base}/runs",
        headers=h,
        json={"dataset_id": "ds1", "target": {"kind": "agent", "id": "agent:agt_missing"}},
    )
    assert ghost.status_code == 404


def test_saved_agent_is_a_redteam_target(env: dict[str, Any]) -> None:
    c, h, ticket = env["c"], env["headers"]["admin"], env["ticket"]
    aid = _agent_id(env, _spec("filer", tools=[WRITE_ID, READ_ID]))
    url = f"{API}/evaluations/studio/redteam"
    res = c.post(url, headers=h, json={"agent_id": aid})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["agent_id"] == aid and body["agent_version"] == "rev_1"
    assert body["model"] == {"name": "fake-deterministic", "offline": True}
    assert body["write_tools"] == [WRITE_TOOL] and body["synthetic_write_tool"] is False
    assert ticket.created == []
    both = c.post(url, headers=h, json={"agent_id": aid, "workflow_id": "wf"})
    assert both.status_code == 422 and both.json()["detail"]["code"] == "INVALID_TARGET"
    assert c.post(url, headers=h, json={}).status_code == 422
    assert c.post(url, headers=env["headers"]["other"], json={"agent_id": aid}).status_code == 404
    # The agent's own model must be offered by this server.
    scripted = _agent_id(env, _spec("filer2", model="scripted"))
    other = TestClient(env["make_app"](chat_models={}))
    res = other.post(url, headers=h, json={"agent_id": scripted})
    assert res.status_code == 422 and res.json()["detail"]["code"] == "MODEL_NOT_AVAILABLE"


# ------------------------------------------------------------- compatibility


def test_palette_lists_the_agent_node_and_the_demo_is_unchanged(env: dict[str, Any]) -> None:
    c, h = env["c"], env["headers"]["admin"]
    items = {m["module_id"]: m for m in c.get(f"{API}/modules", headers=h).json()["items"]}
    node = items["nexura.agents.agent"]
    assert node["kind"] == "agent" and node["internal"] is False
    assert node["config_schema"]["required"] == ["agent_id"]
    demo = items["nexura.agents.coordinator"]
    assert demo["description"].startswith("Offline demo")
    spec = {
        "spec_version": 1,
        "name": "demo",
        "entry_node": "ask",
        "nodes": {
            "ask": {
                "id": "ask",
                "handler": "nexura.agents.coordinator",
                "module_id": "nexura.agents.coordinator",
            }
        },
        "terminal_nodes": ["ask"],
    }
    wf = c.post(f"{API}/workflows", headers=h, json={"spec": spec}).json()["workflow_id"]
    run = _run(env, wf, {"prompt": "my order"})
    assert run["status"] == "completed", run
    # The demo's canned provider is process-wide (its reply depends on earlier runs).
    assert run["current_state"]["final_agent"] == "support_agent"
    assert run["current_state"]["final_output"]


def test_eval_of_a_write_agent_pauses_instead_of_writing(env: dict[str, Any]) -> None:
    c, h = env["c"], env["headers"]["admin"]
    aid = _agent_id(env, _spec("filer", model="scripted", tools=[WRITE_ID]))
    base = f"{API}/evaluations/studio"
    cases = [{"name": "file", "input": "printer", "expected_output": "Ticket"}]
    c.put(f"{base}/datasets/ds1", headers=h, json={"cases": cases})
    run = c.post(
        f"{base}/runs",
        headers=h,
        json={"dataset_id": "ds1", "target": {"kind": "agent", "id": f"agent:{aid}"}},
    )
    assert run.status_code == 200, run.text
    results = run.json()["report"]["results"]
    assert [r["status"] for r in results] == ["paused"]
    assert env["ticket"].created == []


def test_several_knowledge_collections_are_merged(env: dict[str, Any]) -> None:
    c, h = env["c"], env["headers"]["admin"]
    made = c.post(
        f"{API}/knowledge/collections", headers=h, json={"collection_id": "faq", "name": "FAQ"}
    )
    assert made.status_code == 201, made.text
    for collection, name, text in (
        ("default", "refunds.txt", "Refunds are issued within 30 days of purchase."),
        ("faq", "shipping.txt", "Shipping refunds take 5 days to reach your account."),
    ):
        up = c.post(
            f"{API}/knowledge/documents/upload",
            headers=h,
            json={"collection_id": collection, "filename": name, "content": text},
        )
        assert up.status_code in (200, 201), up.text
    aid = _agent_id(env, _spec("librarian", knowledge=["default", "faq"]))
    session = c.post(f"{API}/sessions", headers=h, json={}).json()["session_id"]
    body = c.post(
        f"{API}/sessions/{session}/chat",
        headers=h,
        json={"message": "search the docs about refunds", "agent_id": aid},
    ).json()
    output = json.loads(body["tool_executions"][0]["output"])
    titles = {cit["title"] for cit in output["citations"]}
    assert titles == {"refunds.txt", "shipping.txt"}
    assert [cit["citation_id"] for cit in output["citations"]] == ["[1]", "[2]"]
    # Another workspace's collection ids are refused when saving.
    res = _create(env, _spec("spy2", knowledge=["faq"]), "other")
    assert res.status_code == 422
