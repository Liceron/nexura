# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""P39 G6: the service enforces the operator's guardrail policy and saved agents' presets.

Offline: FakeModel chat models, the in-process test client and local files only.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from _local_env import local_headers
from fastapi.testclient import TestClient
from nexura_agents import Agent, FakeModel, Runner, ToolCall, tool
from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import ModuleManifest
from nexura_agents.contracts.result import ModuleResult
from nexura_agents.errors import SDKValidationError
from nexura_agents.policy import _unlock_for_tests, default_hooks, default_hooks_locked
from nexura_agents.types import Message

from nexura_runtime.cli.main import main as cli_main
from nexura_runtime.service.api import sessions
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.guardrails import GUARDRAIL_POLICY_ENV, GuardrailPolicyConfigError
from nexura_runtime.service.workspaces.repository import WorkspaceRepository
from nexura_runtime.workflow import ModuleRegistry

API = "/api/v1"
POLICY = """\
version: 1
name: operator
checks:
  redact: {type: pii, action: redact}
rules:
  - {id: no-shell, when: {stage: tool_input, tools: ["shell*"]}, action: block}
  - {id: scrub-answers, when: {stage: output}, check: redact, action: scrub}
"""


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(GUARDRAIL_POLICY_ENV, raising=False)
    monkeypatch.setenv("NEXURA_INSECURE_ALLOW_PLAINTEXT_SECRETS", "I_UNDERSTAND_THIS_IS_UNSAFE")

    async def refuse(self: Any, request: httpx.Request) -> httpx.Response:
        raise AssertionError("G6 service tests never reach the network")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)
    _unlock_for_tests()
    yield
    _unlock_for_tests()


def policy_file(tmp_path: Path, text: str = POLICY, name: str = "policy.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------- startup refusal


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, "does not exist"),
        ("version: 2\n", "version must be 1"),
        ("version: 1\nrules:\n  - {id: r, check: missing}\n", "unknown check"),
        ("version: 1\nchecks: [\n", "not valid YAML"),
    ],
)
def test_the_service_refuses_a_missing_or_bad_policy(
    tmp_path: Path, content: str | None, message: str
) -> None:
    path = tmp_path / "policy.yaml"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    with pytest.raises(GuardrailPolicyConfigError, match=message):
        create_api_app(db_path=tmp_path / "s.db", guardrail_policy=path)
    assert default_hooks() is None and not default_hooks_locked()
    assert not (tmp_path / "s.db").exists()  # refused before the database was created


def test_the_env_var_names_the_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GUARDRAIL_POLICY_ENV, str(tmp_path / "missing.yaml"))
    with pytest.raises(GuardrailPolicyConfigError, match="does not exist"):
        create_api_app(db_path=tmp_path / "s.db")
    monkeypatch.setenv(GUARDRAIL_POLICY_ENV, str(policy_file(tmp_path)))
    app = create_api_app(db_path=tmp_path / "s.db")
    assert app.state.guardrail_policy.name == "operator"
    assert default_hooks() is app.state.guardrail_policy and default_hooks_locked()


def test_a_policy_without_the_package_refuses_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins

    real_import = builtins.__import__

    def no_guardrails(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("nexura_guardrails"):
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_guardrails)
    with pytest.raises(GuardrailPolicyConfigError, match="not installed"):
        create_api_app(db_path=tmp_path / "s.db", guardrail_policy=policy_file(tmp_path))


def test_wrong_types_and_a_second_policy_are_refused(tmp_path: Path) -> None:
    with pytest.raises(GuardrailPolicyConfigError, match="must be a"):
        create_api_app(db_path=tmp_path / "s.db", guardrail_policy=42)
    create_api_app(db_path=tmp_path / "a.db", guardrail_policy=policy_file(tmp_path))
    other = policy_file(tmp_path, POLICY.replace("operator", "other"), "other.yaml")
    with pytest.raises(GuardrailPolicyConfigError, match="already enforced"):
        create_api_app(db_path=tmp_path / "b.db", guardrail_policy=other)


def test_cli_serve_refuses_a_bad_policy(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli_main(
        ["serve", "--db", str(tmp_path / "s.db"), "--guardrail-policy", str(tmp_path / "nope.yaml")]
    )
    assert code == 2
    assert "does not exist" in capsys.readouterr().err


def test_cli_worker_takes_the_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = policy_file(tmp_path, "version: 3\n")
    code = cli_main(
        ["worker", "--db", str(tmp_path / "s.db"), "--once", "--guardrail-policy", str(bad)]
    )
    assert code == 2 and "version must be 1" in capsys.readouterr().err


# ---------------------------------------------------------------- enforcement


def test_policy_endpoint_and_lock(tmp_path: Path) -> None:
    app = create_api_app(db_path=tmp_path / "s.db", guardrail_policy=policy_file(tmp_path))
    client = TestClient(app)
    body = client.get(f"{API}/guardrails/policy").json()
    assert body["configured"] is True and body["enforced"] is True
    assert body["policy"]["name"] == "operator" and len(body["policy"]["digest"]) == 64
    assert [r["id"] for r in body["policy"]["rules"]] == ["no-shell", "scrub-answers"]
    with pytest.raises(SDKValidationError):
        Runner(default_hooks=False)  # code in the service process cannot drop it
    plain = TestClient(create_api_app(db_path=tmp_path / "p.db"))
    assert plain.get(f"{API}/guardrails/policy").json() == {
        "configured": False,
        "enforced": False,
        "policy": None,
        "exceptions": [],
    }


def test_presets_endpoint(tmp_path: Path) -> None:
    client = TestClient(create_api_app(db_path=tmp_path / "s.db"))
    body = client.get(f"{API}/guardrails/presets").json()
    assert body["available"] is True and body["defaults"] == ["pii_redact", "injection_fast"]
    by_name = {p["name"]: p for p in body["presets"]}
    assert set(by_name) == {"pii_block", "pii_redact", "injection_fast", "safety_local", "judge"}
    assert by_name["pii_redact"]["cost"] == "none"
    assert (
        by_name["safety_local"]["cost"] == "local"
        and "llama-guard3:1b" in by_name["safety_local"]["models"]
    )
    assert by_name["judge"]["cost"] == "model" and "1.2" in by_name["judge"]["capacity_note"]


async def test_chat_runs_under_the_operator_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[str] = []

    @tool
    def shell_exec(command: str) -> str:
        """Run a command."""
        ran.append(command)
        return "ok"

    model = FakeModel(
        responses=[
            [ToolCall(id="c1", name="shell_exec", arguments={"command": "ls"})],
            "write to bob@example.com",
        ]
    )
    agent = Agent(name="Playground", instructions="x", model=model, tools=[shell_exec])
    monkeypatch.setattr(sessions, "_build_playground_agent", lambda *a, **kw: agent)
    app = create_api_app(
        db_path=tmp_path / "s.db",
        chat_models={"local": model},
        guardrail_policy=policy_file(tmp_path),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        res = await client.post(f"{API}/sessions/s1/chat", json={"message": "go", "model": "local"})
    assert res.status_code == 200, res.text
    assert ran == []  # blocked by the operator rule
    assert res.json()["reply"] == "write to [REDACTED:email]"


async def test_in_process_runners_inherit_the_policy(tmp_path: Path) -> None:
    """Modules, templates and evaluations build their own Runner(): the locked default."""
    create_api_app(db_path=tmp_path / "s.db", guardrail_policy=policy_file(tmp_path))
    agent = Agent(name="A", instructions="x", model=FakeModel(responses=["mail a@b.co"]))
    result = await Runner().run_async(agent, "hi")
    assert result.output == "mail [REDACTED:email]"


# ---------------------------------------------------------------- saved agents: presets


class LookupModule(BaseModule):
    def __init__(self) -> None:
        self._manifest = ModuleManifest(
            module_id="test.lookup.read",
            version="1.0.0",
            kind="integration",
            title="Lookup",
            description="Read-only lookup.",
            input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
            effect_policy="read",
        )

    @property
    def manifest(self) -> ModuleManifest:
        return self._manifest

    async def invoke(
        self, context: ExecutionContext, inputs: dict[str, Any], config: dict[str, Any]
    ) -> ModuleResult:
        return ModuleResult.success({"answer": "mail carol@example.com"})


def _scripted(messages: list[Message], tools: Any) -> Any:
    last = messages[-1]
    if last.role == "tool":
        return f"Tool said: {last.content}"
    return [ToolCall(id="t1", name="test_lookup_read", arguments={"q": "x"})]


@pytest.fixture
def agents_app(tmp_path: Path) -> dict[str, Any]:
    repo = WorkspaceRepository()
    repo.create_workspace("wsA", "A", owner_id="local-owner")
    registry = ModuleRegistry()
    registry.register("test.lookup.read", LookupModule())
    allow = '{"decision": "allow", "categories": [], "score": 0.0, "reason": "ok"}'
    app = create_api_app(
        registry=registry,
        db_path=tmp_path / "s.db",
        workspace_repo=repo,
        chat_models={
            "scripted": FakeModel(handler=_scripted),
            "judge": FakeModel(responses=[allow] * 10),
        },
    )
    return {"c": TestClient(app), "h": local_headers("wsA", "admin"), "app": app}


def _create(env: dict[str, Any], spec: dict[str, Any]) -> Any:
    return env["c"].post(f"{API}/agents", headers=env["h"], json=spec)


def test_options_list_presets_policy_and_effect_classes(agents_app: dict[str, Any]) -> None:
    body = agents_app["c"].get(f"{API}/agents/options", headers=agents_app["h"]).json()
    assert body["guardrails"]["defaults"] == ["pii_redact", "injection_fast"]
    assert body["guardrails"]["policy"]["configured"] is False
    (lookup,) = [t for t in body["tools"] if t["module_id"] == "test.lookup.read"]
    assert lookup["effect_policy"] == "read" and lookup["effect_class"] == "pure"


def test_saved_agent_presets_apply_to_answers_and_tool_results(agents_app: dict[str, Any]) -> None:
    spec = {
        "name": "helper",
        "model": "scripted",
        "tools": ["test.lookup.read"],
        "guardrails": {"presets": [{"preset": "pii_redact"}, {"preset": "injection_fast"}]},
    }
    res = _create(agents_app, spec)
    assert res.status_code == 201, res.text
    assert res.json()["spec"]["guardrails"]["presets"][0]["preset"] == "pii_redact"
    agent_id = res.json()["agent_id"]
    chat = agents_app["c"].post(
        f"{API}/sessions/s1/chat",
        headers=agents_app["h"],
        json={"message": "look it up", "agent_id": agent_id},
    )
    assert chat.status_code == 200, chat.text
    reply = chat.json()["reply"]
    assert "carol@example.com" not in reply and "[REDACTED:email]" in reply


@pytest.mark.parametrize(
    ("guardrails", "field", "message"),
    [
        (
            {"presets": [{"preset": "judge", "policy": "No fraud."}]},
            "guardrails.presets.0",
            "offline",
        ),
        (
            {"presets": [{"preset": "judge", "policy": "x", "model": "nope"}]},
            "guardrails.presets.0",
            "Unknown chat model",
        ),
        (
            {"presets": [{"preset": "safety_local", "model": "llama3:70b"}]},
            "guardrails.presets.0",
            "not a known local safety model",
        ),
        (
            {"presets": [{"preset": "pii_block", "threshold": 1}]},
            "guardrails.presets.0",
            "does not take",
        ),
        ({"presets": [{"preset": "judge"}]}, "guardrails.presets.0", "needs policy text"),
        ({"stream_guard": "sometimes"}, "guardrails.stream_guard", "Input should be"),
        ({"presets": [], "extra": 1}, "guardrails.extra", "Extra inputs"),
    ],
)
def test_bad_presets_are_refused_on_save(
    agents_app: dict[str, Any], guardrails: dict[str, Any], field: str, message: str
) -> None:
    res = _create(agents_app, {"name": "helper", "guardrails": guardrails})
    assert res.status_code == 422, res.text
    errors = res.json()["detail"]["field_errors"]
    assert any(e["field"] == field and message in e["message"] for e in errors), errors


def test_judge_preset_with_a_configured_model(agents_app: dict[str, Any]) -> None:
    spec = {
        "name": "judged",
        "model": "scripted",
        "guardrails": {"presets": [{"preset": "judge", "policy": "No fraud.", "model": "judge"}]},
    }
    res = _create(agents_app, spec)
    assert res.status_code == 201, res.text
    runtime = agents_app["app"].state.agent_runtime
    built = runtime.build_agent("wsA", res.json()["agent_id"], None)
    assert [g.__name__ for g in built.agent.input_guardrails] == ["judge"]
    assert [g.__name__ for g in built.agent.output_guardrails] == ["judge"]


def test_safety_local_preset_builds_for_a_known_model(agents_app: dict[str, Any]) -> None:
    spec = {
        "name": "safe",
        "guardrails": {
            "presets": [{"preset": "safety_local", "model": "granite3-guardian:2b"}],
            "stream_guard": "hold",
        },
    }
    res = _create(agents_app, spec)
    assert res.status_code == 201, res.text
    built = agents_app["app"].state.agent_runtime.build_agent("wsA", res.json()["agent_id"], None)
    assert built.agent.input_guardrails and built.agent.output_guardrails
    assert built.agent.stream_guard.mode == "hold"


def test_specs_without_guardrails_still_load(agents_app: dict[str, Any]) -> None:
    res = _create(agents_app, {"name": "plain"})
    assert res.status_code == 201 and res.json()["spec"]["guardrails"] is None
    built = agents_app["app"].state.agent_runtime.build_agent("wsA", res.json()["agent_id"], None)
    assert built.agent.input_guardrails == [] or list(built.agent.input_guardrails) == []


async def test_python_client_reads_policy_and_presets(tmp_path: Path) -> None:
    from nexura_runtime.service.api.client import NexuraClient

    app = create_api_app(db_path=tmp_path / "s.db", guardrail_policy=policy_file(tmp_path))
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")
    client = NexuraClient(base_url="http://127.0.0.1", http_client=http)
    assert (await client.guardrails.policy())["policy"]["name"] == "operator"
    assert "judge" in {p["name"] for p in (await client.guardrails.presets())["presets"]}
    await http.aclose()


# ---------------------------------------------------------------- round 2 (review L3, H1, M2)


def test_round2_policy_detail_is_for_admins_only(tmp_path: Path) -> None:
    from nexura_runtime.service.api.guardrails import policy_view

    path = policy_file(tmp_path)
    app = create_api_app(db_path=tmp_path / "s.db", guardrail_policy=path)
    policy = app.state.guardrail_policy
    summary = policy_view(policy)
    assert summary["detail"] == "summary" and summary["enforced"] is True
    assert summary["policy"] == {"digest": policy.digest, "file": "policy.yaml"}
    assert str(tmp_path) not in json.dumps(summary)
    assert "workflow_module_nodes" not in summary["exceptions"]  # WorkflowEngine applies it
    assert "coordinator_refused_with_model_stage_rules" in summary["exceptions"]
    full = policy_view(policy, admin=True)
    assert full["policy"]["source"] == str(path) and full["policy"]["rules"]
    # The local owner is an admin; a viewer of a workspace gets the summary.
    client = TestClient(app)
    assert client.get(f"{API}/guardrails/policy").json()["detail"] == "full"


def test_round2_viewers_get_the_summary(tmp_path: Path) -> None:
    from nexura_runtime.service.api import guardrails as module

    app = create_api_app(db_path=tmp_path / "s.db", guardrail_policy=policy_file(tmp_path))
    request: Any = type("R", (), {"app": app, "state": type("S", (), {"role": "viewer"})()})()
    assert module.caller_is_admin(request) is False
    body = module.policy_view(app.state.guardrail_policy, admin=module.caller_is_admin(request))
    assert "rules" not in body["policy"] and "source" not in body["policy"]


def test_round2_passthrough_is_refused_with_answer_checks(agents_app: dict[str, Any]) -> None:
    res = _create(
        agents_app,
        {
            "name": "p",
            "guardrails": {"presets": [{"preset": "pii_redact"}], "stream_guard": "passthrough"},
        },
    )
    assert res.status_code == 422
    assert any("passthrough" in e["message"] for e in res.json()["detail"]["field_errors"])
    ok = _create(
        agents_app,
        {
            "name": "q",
            "guardrails": {
                "presets": [{"preset": "injection_fast"}],
                "stream_guard": "passthrough",
            },
        },
    )
    assert ok.status_code == 201, ok.text


def test_round2_operator_policy_refuses_passthrough_on_save(tmp_path: Path) -> None:
    repo = WorkspaceRepository()
    repo.create_workspace("wsA", "A", owner_id="local-owner")
    app = create_api_app(
        db_path=tmp_path / "s.db", workspace_repo=repo, guardrail_policy=policy_file(tmp_path)
    )
    client = TestClient(app)
    res = client.post(
        f"{API}/agents",
        headers=local_headers("wsA", "admin"),
        json={"name": "p", "guardrails": {"presets": [], "stream_guard": "passthrough"}},
    )
    assert res.status_code == 422
    fields = [e["field"] for e in res.json()["detail"]["field_errors"]]
    assert "guardrails.stream_guard" in fields
