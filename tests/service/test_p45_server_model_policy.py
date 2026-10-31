# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""P45 phase 1: server model keys only for allowed workspaces; bring-your-own-key models.

The policy refuses workspace ``ws-x`` and allows ``default``. Offline only: server models are
``FakeModel`` spies, provider calls go to an ``httpx.MockTransport`` and any real outbound
HTTP fails the test. Dummy operator keys are set in the environment and asserted never sent.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from nexura_agents.contracts import CredentialReference
from nexura_agents.models.fake import FakeModel

from nexura_runtime.credentials.errors import CredentialNotFoundError
from nexura_runtime.knowledge.embeddings.config import configured_embedder
from nexura_runtime.service.api import builder as builder_module
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.credential_models import parse_credential_model
from nexura_runtime.service.api.model_access import (
    ServerModelsNotAvailableError,
    server_key_nodes,
    server_models_allowed,
    workflow_module_gate,
)
from nexura_runtime.service.api.runs import start_workflow_run
from nexura_runtime.workflow import WorkflowEngine, WorkflowSpec

API = "/api/v1"
ENV_OPENAI = "sk-env-operator-openai-key"
ENV_GEMINI = "env-operator-gemini-key"
OWN_KEY = "sk-ws-x-own-key-0001"
X = {"X-Workspace-ID": "ws-x"}
Y = {"X-Workspace-ID": "ws-y"}
D = {"X-Workspace-ID": "default"}
REFUSED = "SERVER_MODELS_NOT_AVAILABLE"


def _operator_only(workspace_id: str) -> bool:
    return workspace_id == "default"


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operator keys exist in the environment; any real network call fails the test."""
    monkeypatch.setenv("OPENAI_API_KEY", ENV_OPENAI)
    monkeypatch.setenv("GEMINI_API_KEY", ENV_GEMINI)

    async def refuse(self: Any, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"test tried a real network call to {request.url.host}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


class Calls:
    """Requests seen by the mock provider (headers kept to check which key was sent)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def sent_keys(self) -> list[str]:
        return [r.headers.get("authorization", "") for r in self.requests]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.path.endswith("/chat/completions")
        return httpx.Response(
            200,
            json={
                "id": "cmpl-1",
                "object": "chat.completion",
                "model": json.loads(request.content)["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "own-key reply"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    """BYOK models are real ``OpenAIProvider`` instances on a mock transport."""
    from nexura_agents.providers.openai.provider import OpenAIProvider

    seen = Calls()
    built: list[tuple[str, str, str]] = []

    def build(provider: str, model_name: str, api_key: str) -> Any:
        assert provider == "openai"
        built.append((provider, model_name, api_key))
        return OpenAIProvider(
            model_name, api_key=api_key, transport=httpx.MockTransport(seen.handler)
        )

    monkeypatch.setattr(builder_module, "build_model_from_credential", build)
    seen.built = built  # type: ignore[attr-defined]
    return seen


class ServerSpy:
    """The operator's model: counts every call."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, messages: Any, tools: Any) -> str:
        self.calls += 1
        return "server reply"


def _app(tmp_path: Path, *, policy: Any = _operator_only, **kwargs: Any) -> TestClient:
    spy = ServerSpy()
    server = FakeModel(handler=spy)
    kwargs.setdefault("chat_models", {"srv": server})
    app = create_api_app(
        db_path=str(tmp_path / "p45.sqlite3"),
        profile="local-owner",
        server_model_policy=policy,
        **kwargs,
    )
    app.state.server_fake = spy
    return TestClient(app)


def _credential(c: TestClient, headers: dict[str, str], **kw: Any) -> Any:
    body = {
        "credential_id": "mykey",
        "credential_type": "api_key",
        "provider": "openai",
        "secret": OWN_KEY,
        **kw,
    }
    return c.post(f"{API}/credentials", json=body, headers=headers)


def _code(resp: Any) -> str:
    return str(resp.json()["detail"]["code"])


def _chat(c: TestClient, headers: dict[str, str], model: str, session: str = "s1") -> Any:
    return c.post(
        f"{API}/sessions/{session}-{headers['X-Workspace-ID']}/chat",
        json={"message": "hello", "model": model},
        headers=headers,
    )


def _provider_spec(module_id: str = "nexura.models.gemini") -> dict[str, Any]:
    return {
        "spec_version": 1,
        "name": "provider-node",
        "entry_node": "llm",
        "nodes": {"llm": {"id": "llm", "handler": module_id, "module_id": module_id}},
        "terminal_nodes": ["llm"],
    }


def _nested_provider_spec() -> dict[str, Any]:
    return {
        "spec_version": 1,
        "name": "outer",
        "entry_node": "sub",
        "nodes": {"sub": {"id": "sub", "subgraph": _provider_spec("nexura.models.openai")}},
        "terminal_nodes": ["sub"],
    }


# ----------------------------------------------------------------------------- the policy


def test_policy_helper_fails_closed() -> None:
    class State:
        server_model_policy: Any = None

    state = State()
    assert server_models_allowed(state, None) is True  # no policy: everything as before
    state.server_model_policy = _operator_only
    assert server_models_allowed(state, "default") is True
    assert server_models_allowed(state, "ws-x") is False
    assert server_models_allowed(state, None) is False
    assert server_models_allowed(state, "") is False

    def broken(workspace_id: str) -> bool:
        raise RuntimeError("policy store down")

    state.server_model_policy = broken
    assert server_models_allowed(state, "default") is False
    state.server_model_policy = lambda ws: "yes"  # truthy but not True
    assert server_models_allowed(state, "default") is False


def test_cred_names_parse_strictly() -> None:
    assert parse_credential_model("cred:mykey/gpt-4.1-mini") == ("mykey", "gpt-4.1-mini")
    assert parse_credential_model("cred:a.b_c-d/models:x.y") == ("a.b_c-d", "models:x.y")
    for bad in (
        "cred:mykey",
        "cred:/gpt",
        "cred:my key/gpt",
        "cred:mykey/-gpt",
        "cred:mykey/gpt/4",
        "cred:mykey/" + "a" * 101,
        "srv",
        None,
    ):
        assert parse_credential_model(bad) is None, bad


def test_cred_prefix_is_reserved_for_operator_names(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="reserved"):
        create_api_app(db_path=str(tmp_path / "r.sqlite3"), chat_models={"cred:x/y": "openai:x"})


# ----------------------------------------------------------------------------- chat models


def test_no_policy_keeps_todays_behaviour(tmp_path: Path) -> None:
    c = _app(tmp_path, policy=None)
    names = [m["name"] for m in c.get(f"{API}/chat/models", headers=X).json()]
    assert names == ["fake-deterministic", "srv"]
    resp = _chat(c, X, "srv")
    assert resp.status_code == 200, resp.text
    assert "server reply" in json.dumps(resp.json())
    assert _credential(c, X, environment_variable="OPENAI_API_KEY").status_code == 201


def test_refused_workspace_cannot_list_or_use_server_models(tmp_path: Path) -> None:
    c = _app(tmp_path)
    assert [m["name"] for m in c.get(f"{API}/chat/models", headers=X).json()] == [
        "fake-deterministic"
    ]
    resp = _chat(c, X, "srv")
    assert resp.status_code == 403 and _code(resp) == REFUSED
    assert "Settings → AI keys" in resp.json()["detail"]["message"]
    assert c.app.state.server_fake.calls == 0  # type: ignore[attr-defined]
    # The offline fixture still works, and the operator workspace is unchanged.
    assert _chat(c, X, "fake-deterministic").status_code == 200
    assert [m["name"] for m in c.get(f"{API}/chat/models", headers=D).json()] == [
        "fake-deterministic",
        "srv",
    ]
    ok = _chat(c, D, "srv")
    assert ok.status_code == 200 and "server reply" in json.dumps(ok.json())


def test_agents_refuse_server_models_and_options_hide_them(tmp_path: Path) -> None:
    c = _app(tmp_path)
    options = c.get(f"{API}/agents/options", headers=X).json()
    assert [m["name"] for m in options["models"]] == ["fake-deterministic"]
    resp = c.post(f"{API}/agents", json={"name": "a", "model": "srv"}, headers=X)
    assert resp.status_code == 422 and _code(resp) == REFUSED
    assert c.post(f"{API}/agents", json={"name": "a", "model": "srv"}, headers=D).status_code == 201


def test_agent_saved_before_the_policy_is_refused_when_it_runs(tmp_path: Path) -> None:
    c = _app(tmp_path, policy=None)
    created = c.post(f"{API}/agents", json={"name": "a", "model": "srv"}, headers=X)
    assert created.status_code == 201
    c.app.state.server_model_policy = _operator_only  # type: ignore[attr-defined]
    resp = c.post(
        f"{API}/sessions/s9/chat",
        json={"message": "hi", "agent_id": created.json()["agent_id"]},
        headers=X,
    )
    assert resp.status_code == 403 and _code(resp) == REFUSED
    runtime = c.app.state.agent_runtime  # type: ignore[attr-defined]
    with pytest.raises(Exception) as caught:
        runtime.build_agent("ws-x", created.json()["agent_id"], None)
    assert getattr(caught.value, "code", None) == REFUSED
    assert c.app.state.server_fake.calls == 0  # type: ignore[attr-defined]


def test_judge_preset_refuses_server_models(tmp_path: Path) -> None:
    pytest.importorskip("nexura_guardrails.presets")
    c = _app(tmp_path)
    body = {
        "name": "judged",
        "model": "fake-deterministic",
        "guardrails": {"presets": [{"preset": "judge", "model": "srv", "policy": "Be kind."}]},
    }
    resp = c.post(f"{API}/agents", json=body, headers=X)
    assert resp.status_code == 422 and _code(resp) == REFUSED
    assert c.post(f"{API}/agents", json=body, headers=D).status_code == 201


def test_builder_never_falls_back_to_server_models(tmp_path: Path) -> None:
    c = _app(tmp_path)
    resp = c.post(f"{API}/builder/draft", json={"description": "Echo the input."}, headers=X)
    assert resp.status_code == 400 and _code(resp) == "NO_MODEL_CREDENTIAL"
    resp = c.post(
        f"{API}/builder/draft",
        json={"description": "Echo the input.", "model_ref": "srv"},
        headers=X,
    )
    assert resp.status_code == 403 and _code(resp) == REFUSED
    assert c.app.state.server_fake.calls == 0  # type: ignore[attr-defined]
    # The operator workspace still falls back to the operator's model (it is called).
    c.post(f"{API}/builder/draft", json={"description": "Echo the input."}, headers=D)
    assert c.app.state.server_fake.calls > 0  # type: ignore[attr-defined]


def test_redteam_never_uses_the_server_model(tmp_path: Path) -> None:
    c = _app(tmp_path)
    c.app.state.redteam_model = "srv"  # type: ignore[attr-defined]
    spec = {
        "name": "support",
        "entry_node": "lookup",
        "nodes": {"lookup": {"id": "lookup", "handler": "synthetic_order_lookup"}},
        "terminal_nodes": ["lookup"],
    }
    for headers in (X, D):
        r = c.post(f"{API}/workflows", json={"spec": spec}, headers=headers)
        assert r.status_code == 201, r.text
    url = f"{API}/evaluations/studio/redteam"
    wid_x = c.get(f"{API}/workflows", headers=X).json()["items"][0]["workflow_id"]
    body = c.post(url, json={"workflow_id": wid_x}, headers=X).json()
    assert body["model"] == {"name": "fake-deterministic", "offline": True}
    assert c.app.state.server_fake.calls == 0  # type: ignore[attr-defined]
    # A saved agent on a refused server model is reported, not tested on it.
    agent = c.post(f"{API}/agents", json={"name": "a"}, headers=X).json()["agent_id"]
    store = c.app.state.agent_store  # type: ignore[attr-defined]
    spec_x, rev = store.get_spec("ws-x", agent)
    store.update("ws-x", agent, spec_x.model_copy(update={"model": "srv"}), expected_revision=rev)
    resp = c.post(url, json={"agent_id": agent}, headers=X)
    assert resp.status_code == 403 and _code(resp) == REFUSED
    wid_d = c.get(f"{API}/workflows", headers=D).json()["items"][0]["workflow_id"]
    body = c.post(url, json={"workflow_id": wid_d}, headers=D).json()
    assert body["model"] == {"name": "srv", "offline": False}


# ----------------------------------------------------------------------------- workflows


def test_workflow_save_refuses_server_key_nodes(tmp_path: Path) -> None:
    c = _app(tmp_path)
    for spec in (_provider_spec("nexura.models.gemini"), _nested_provider_spec()):
        resp = c.post(f"{API}/workflows", json={"spec": spec}, headers=X)
        assert resp.status_code == 403 and _code(resp) == REFUSED, resp.text
    created = c.post(f"{API}/workflows", json={"spec": _provider_spec()}, headers=D)
    assert created.status_code == 201, created.text
    # An update of an existing workflow is refused the same way.
    plain = {
        "spec_version": 1,
        "name": "plain",
        "entry_node": "lookup",
        "nodes": {"lookup": {"id": "lookup", "handler": "synthetic_order_lookup"}},
        "terminal_nodes": ["lookup"],
    }
    made = c.post(f"{API}/workflows", json={"spec": plain}, headers=X)
    assert made.status_code == 201, made.text
    resp = c.put(
        f"{API}/workflows/{made.json()['workflow_id']}",
        json={"spec": _provider_spec("nexura.models.openai"), "expected_revision": 1},
        headers=X,
    )
    assert resp.status_code == 403 and _code(resp) == REFUSED


def test_run_start_refuses_server_key_nodes_on_every_entry(tmp_path: Path) -> None:
    c = _app(tmp_path)
    # API runs (inline spec).
    resp = c.post(f"{API}/runs", json={"spec": _provider_spec(), "initial_state": {}}, headers=X)
    assert resp.status_code == 403 and _code(resp) == REFUSED
    # A workflow saved before the policy existed.
    c.app.state.server_model_policy = None  # type: ignore[attr-defined]
    wid = c.post(f"{API}/workflows", json={"spec": _provider_spec()}, headers=X).json()[
        "workflow_id"
    ]
    c.app.state.server_model_policy = _operator_only  # type: ignore[attr-defined]
    resp = c.post(f"{API}/runs", json={"workflow_id": wid, "initial_state": {}}, headers=X)
    assert resp.status_code == 403 and _code(resp) == REFUSED
    # Triggers, schedules and publications start runs through start_workflow_run.
    spec = WorkflowSpec.from_dict(_nested_provider_spec())
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            start_workflow_run(
                c.app.state,  # type: ignore[attr-defined]
                spec=spec,
                workflow_id=None,
                workspace_id="ws-x",
                principal_id="trigger:sched-1",
                initial_state={},
                trigger={"kind": "schedule"},
            )
        )
    assert caught.value.status_code == 403
    assert caught.value.detail["code"] == REFUSED  # type: ignore[index]


def test_engine_gate_refuses_server_key_modules_at_step_time(tmp_path: Path) -> None:
    c = _app(tmp_path)
    state = c.app.state  # type: ignore[attr-defined]
    spec = WorkflowSpec.from_dict(_nested_provider_spec())
    assert server_key_nodes(spec, state.registry) == ["sub/llm"]
    assert workflow_module_gate(state, "default") is None
    gate = workflow_module_gate(state, "ws-x")
    assert gate is not None
    engine = WorkflowEngine(spec, state.registry, workspace_id="ws-x", module_gate=gate)

    async def run() -> Any:
        return await engine.run({})

    with pytest.raises(Exception) as caught:
        result = asyncio.run(run())
        # A run that records the failure instead of raising must not have succeeded.
        assert result.status != "completed"
        raise AssertionError(str(getattr(result, "error", "")))
    assert "server's key" in str(caught.value) or isinstance(
        caught.value, ServerModelsNotAvailableError
    )


# ----------------------------------------------------------------------------- embeddings


def test_embeddings_never_use_the_server_key_for_a_refused_workspace(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("authorization", ""))
        count = len(json.loads(request.content)["input"])
        return httpx.Response(
            200,
            json={
                "data": [{"index": i, "embedding": [1.0, 0.5, 0.25]} for i in range(count)],
                "usage": {"prompt_tokens": count, "total_tokens": count},
            },
        )

    c = _app(tmp_path)
    state = c.app.state  # type: ignore[attr-defined]
    state.knowledge_embedder = configured_embedder(
        "openai:mock-embed",
        dimensions=3,
        environ={"OPENAI_API_KEY": ENV_OPENAI},
        transport=httpx.MockTransport(handler),
    )
    state.hybrid_knowledge_index = None  # rebuilt lazily with the keyed embedder
    doc = {"filename": "a.txt", "content": "Refunds take 30 days from purchase."}
    up = c.post(f"{API}/knowledge/documents/upload", json=doc, headers=X)
    assert up.status_code in (200, 201), up.text
    hits = c.post(f"{API}/knowledge/retrieve", json={"query": "refund days"}, headers=X)
    assert hits.status_code == 200, hits.text
    assert calls == []  # the operator's embedding key was never used for ws-x
    status_x = c.get(f"{API}/knowledge/limits", headers=X).json()
    assert status_x["embedder"]["is_test_fixture"] is True
    up = c.post(f"{API}/knowledge/documents/upload", json=doc, headers=D)
    assert up.status_code in (200, 201), up.text
    assert calls and all(key == f"Bearer {ENV_OPENAI}" for key in calls)


# ----------------------------------------------------------------------------- credentials


def test_environment_variable_credentials_are_refused_and_never_resolved(
    tmp_path: Path,
) -> None:
    c = _app(tmp_path)
    resp = _credential(c, X, environment_variable="OPENAI_API_KEY")
    assert resp.status_code == 400 and _code(resp) == REFUSED
    assert ENV_OPENAI not in resp.text
    assert _credential(c, D, environment_variable="OPENAI_API_KEY").status_code == 201
    store = c.app.state.credential_store  # type: ignore[attr-defined]
    ref = CredentialReference(
        credential_id="absent", credential_type="api_key", environment_variable="OPENAI_API_KEY"
    )
    with pytest.raises(CredentialNotFoundError):
        store.resolve_ref("ws-x", ref)
    assert store.for_workspace("ws-x").resolve(ref) is None
    assert store.resolve_ref("default", ref).reveal() == ENV_OPENAI


# ----------------------------------------------------------------------------- BYOK


def test_byok_models_are_listed_and_use_the_workspace_key(tmp_path: Path, calls: Calls) -> None:
    c = _app(tmp_path)
    assert _credential(c, X).status_code == 201
    listed = c.get(f"{API}/chat/models", headers=X).json()
    names = [m["name"] for m in listed]
    assert names == [
        "fake-deterministic",
        "cred:mykey/gpt-4.1-mini",
        "cred:mykey/gpt-4.1",
        "cred:mykey/gpt-4o-mini",
    ]
    assert OWN_KEY not in json.dumps(listed) and ENV_OPENAI not in json.dumps(listed)
    assert listed[1]["provider"] == "openai" and "mykey" in listed[1]["label"]
    options = c.get(f"{API}/agents/options", headers=X).json()
    assert "cred:mykey/gpt-4.1-mini" in [m["name"] for m in options["models"]]

    resp = _chat(c, X, "cred:mykey/gpt-4.1-mini")
    assert resp.status_code == 200, resp.text
    assert "own-key reply" in json.dumps(resp.json())
    assert OWN_KEY not in resp.text
    # Any well-formed model id resolves, not only the suggested ones.
    assert _chat(c, X, "cred:mykey/o9-custom", session="s2").status_code == 200
    assert calls.sent_keys() and all(k == f"Bearer {OWN_KEY}" for k in calls.sent_keys())
    assert all(ENV_OPENAI not in str(r.headers) for r in calls.requests)
    assert {json.loads(r.content)["model"] for r in calls.requests} == {"gpt-4.1-mini", "o9-custom"}


def test_byok_agent_runs_with_the_workspace_key(tmp_path: Path, calls: Calls) -> None:
    c = _app(tmp_path)
    assert _credential(c, X).status_code == 201
    agent = c.post(
        f"{API}/agents", json={"name": "mine", "model": "cred:mykey/gpt-4.1-mini"}, headers=X
    )
    assert agent.status_code == 201, agent.text
    resp = c.post(
        f"{API}/sessions/s5/chat",
        json={"message": "hi", "agent_id": agent.json()["agent_id"]},
        headers=X,
    )
    assert resp.status_code == 200, resp.text
    assert calls.sent_keys() == [f"Bearer {OWN_KEY}"]


def test_byok_cache_is_invalidated_by_a_replaced_or_revoked_key(
    tmp_path: Path, calls: Calls
) -> None:
    c = _app(tmp_path)
    assert _credential(c, X).status_code == 201
    assert _chat(c, X, "cred:mykey/gpt-4.1-mini").status_code == 200
    assert _chat(c, X, "cred:mykey/gpt-4.1-mini", session="s2").status_code == 200
    assert len(calls.built) == 1  # type: ignore[attr-defined]  # cached between turns
    store = c.app.state.credential_store  # type: ignore[attr-defined]
    store.store(
        "ws-x", "mykey", "api_key", "sk-ws-x-new-key-0002", provider="openai", mode="replace"
    )
    assert _chat(c, X, "cred:mykey/gpt-4.1-mini", session="s3").status_code == 200
    assert len(calls.built) == 2  # type: ignore[attr-defined]
    assert calls.sent_keys()[-1] == "Bearer sk-ws-x-new-key-0002"
    assert len(c.app.state.credential_model_cache) == 1  # type: ignore[attr-defined]
    assert c.post(f"{API}/credentials/mykey/revoke", headers=X).status_code == 200
    resp = _chat(c, X, "cred:mykey/gpt-4.1-mini", session="s4")
    assert resp.status_code == 400 and _code(resp) == "CREDENTIAL_UNUSABLE"
    assert len(calls.requests) == 3


def test_workspace_cannot_use_another_workspaces_credential(tmp_path: Path, calls: Calls) -> None:
    c = _app(tmp_path)
    assert _credential(c, X).status_code == 201
    resp = _chat(c, Y, "cred:mykey/gpt-4.1-mini")
    assert resp.status_code == 400 and _code(resp) == "MODEL_NOT_AVAILABLE"
    assert [m["name"] for m in c.get(f"{API}/chat/models", headers=Y).json()] == [
        "fake-deterministic"
    ]
    resp = c.post(f"{API}/agents", json={"name": "a", "model": "cred:mykey/gpt-4.1"}, headers=Y)
    assert resp.status_code == 422
    assert calls.requests == [] and calls.built == []  # type: ignore[attr-defined]


def test_non_model_and_empty_credentials_are_not_models(tmp_path: Path, calls: Calls) -> None:
    c = _app(tmp_path)
    assert _credential(c, X, credential_id="gh", provider="github").status_code == 201
    assert [m["name"] for m in c.get(f"{API}/chat/models", headers=X).json()] == [
        "fake-deterministic"
    ]
    resp = _chat(c, X, "cred:gh/gpt-4.1")
    assert resp.status_code == 400 and _code(resp) == "MODEL_NOT_AVAILABLE"
    # A stored value that is only whitespace would let the provider read the environment.
    store = c.app.state.credential_store  # type: ignore[attr-defined]
    store.store("ws-x", "blank", "api_key", "   ", provider="openai")
    resp = _chat(c, X, "cred:blank/gpt-4.1", session="s2")
    assert resp.status_code == 400 and _code(resp) == "CREDENTIAL_UNUSABLE"
    assert calls.requests == []


def test_builder_uses_byok_model_ref(tmp_path: Path, calls: Calls) -> None:
    c = _app(tmp_path)
    assert _credential(c, X).status_code == 201
    resp = c.post(
        f"{API}/builder/draft",
        json={"description": "Echo the input.", "model_ref": "cred:mykey/gpt-4.1-mini"},
        headers=X,
    )
    # The draft model is built with the workspace's own key, never the environment's.
    assert [b[2] for b in calls.built] == [OWN_KEY], resp.text  # type: ignore[attr-defined]
    assert all(k == f"Bearer {OWN_KEY}" for k in calls.sent_keys())
    # A real provider adapter is called (it takes the schema on a GenerationRequest, not a
    # ``response_schema`` keyword) and receives the draft schema as structured output.
    assert calls.requests, resp.text
    body = json.loads(calls.requests[0].content)
    assert body["response_format"]["type"] == "json_schema"
    # The mock's prose reply is not a valid draft: a clean draft error, never a server error.
    assert resp.status_code < 500, resp.text
