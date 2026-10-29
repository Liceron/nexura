# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""P40 item 1: the service shows a guardrail policy's shadow mode and runs it as reporting only.

Offline: FakeModel chat models, the in-process test client and local files only.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from nexura_agents import Agent, FakeModel, Runner, ToolCall, tool
from nexura_agents.policy import _unlock_for_tests

from nexura_runtime.agents_spec import AgentGuardrails
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.guardrails import (
    GUARDRAIL_POLICY_ENV,
    check_agent_guardrails,
    policy_view,
)

API = "/api/v1"
SHADOW = """\
version: 1
name: pilot
mode: shadow
checks:
  redact: {type: pii, action: redact}
rules:
  - {id: payment-ceiling, when: {stage: tool_input, args: {$.amount: {gt: 500}}}, action: block}
  - id: no-shell
    when: {stage: tool_input, tools: ["shell*"]}
    action: block
    enforcement: enforce
  - {id: scrub-answers, when: {stage: output}, check: redact, action: scrub}
audit: {sink: jsonl, path: audit/guardrails.jsonl}
"""
ENFORCE = """\
version: 1
name: operator
rules:
  - {id: no-shell, when: {stage: tool_input, tools: ["shell*"]}, action: block}
  - {id: watch, when: {stage: tool_input, tools: [lookup]}, action: block, enforcement: shadow}
"""


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(GUARDRAIL_POLICY_ENV, raising=False)
    monkeypatch.setenv("NEXURA_INSECURE_ALLOW_PLAINTEXT_SECRETS", "I_UNDERSTAND_THIS_IS_UNSAFE")

    async def refuse(self: Any, request: httpx.Request) -> httpx.Response:
        raise AssertionError("P40 service tests never reach the network")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)
    _unlock_for_tests()
    yield
    _unlock_for_tests()


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "policy.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_policy_view_shows_shadow_mode_and_each_rules_enforcement(tmp_path: Path) -> None:
    app = create_api_app(db_path=tmp_path / "s.db", guardrail_policy=write(tmp_path, SHADOW))
    body = TestClient(app).get(f"{API}/guardrails/policy").json()  # the local owner is admin
    assert body["configured"] is True and body["enforced"] is True
    assert body["mode"] == "shadow" and body["shadow_rules"] == 2
    assert body["policy"]["mode"] == "shadow" and body["policy"]["shadow_rules"] == 2
    assert {r["id"]: r["enforcement"] for r in body["policy"]["rules"]} == {
        "payment-ceiling": "shadow",
        "no-shell": "enforce",
        "scrub-answers": "shadow",
    }
    # A viewer gets the mode and the count, not the rules.
    summary = policy_view(app.state.guardrail_policy)
    assert summary["mode"] == "shadow" and summary["shadow_rules"] == 2
    assert summary["policy"] == {"digest": app.state.guardrail_policy.digest, "file": "policy.yaml"}
    assert "rules" not in json.dumps(summary["policy"])


def test_an_enforce_policy_view_says_enforce(tmp_path: Path) -> None:
    app = create_api_app(db_path=tmp_path / "s.db", guardrail_policy=write(tmp_path, ENFORCE))
    body = TestClient(app).get(f"{API}/guardrails/policy").json()
    assert body["mode"] == "enforce" and body["shadow_rules"] == 1
    assert [r["enforcement"] for r in body["policy"]["rules"]] == ["enforce", "shadow"]
    options = TestClient(app).get(f"{API}/agents/options").json()
    assert options["guardrails"]["policy"]["mode"] == "enforce"


def test_no_policy_has_no_mode(tmp_path: Path) -> None:
    body = (
        TestClient(create_api_app(db_path=tmp_path / "s.db")).get(f"{API}/guardrails/policy").json()
    )
    assert body == {"configured": False, "enforced": False, "policy": None, "exceptions": []}


def test_shadow_answer_rules_do_not_refuse_passthrough_at_save(tmp_path: Path) -> None:
    app = create_api_app(db_path=tmp_path / "s.db", guardrail_policy=write(tmp_path, SHADOW))
    errors = check_agent_guardrails(
        AgentGuardrails(stream_guard="passthrough"),
        None,
        "fake",
        policy=app.state.guardrail_policy,
        agent_name="helper",
    )
    assert errors == []
    _unlock_for_tests()
    enforced = create_api_app(
        db_path=tmp_path / "e.db",
        guardrail_policy=write(tmp_path, SHADOW.replace("mode: shadow\n", "")),
    )
    errors = check_agent_guardrails(
        AgentGuardrails(stream_guard="passthrough"),
        None,
        "fake",
        policy=enforced.state.guardrail_policy,
        agent_name="helper",
    )
    assert [e["field"] for e in errors] == ["guardrails.stream_guard"]


async def test_the_installed_shadow_policy_reports_to_the_jsonl_audit_file(
    tmp_path: Path,
) -> None:
    create_api_app(db_path=tmp_path / "s.db", guardrail_policy=write(tmp_path, SHADOW))
    paid: list[float] = []
    shell_ran: list[str] = []

    @tool
    def pay(amount: float) -> str:
        """Pay."""
        paid.append(amount)
        return "paid"

    @tool
    def shell_run(command: str) -> str:
        """Run a command."""
        shell_ran.append(command)
        return "done"

    agent = Agent(
        name="helper",
        instructions="x",
        model=FakeModel(
            responses=[
                [
                    ToolCall(id="c1", name="pay", arguments={"amount": 900}),
                    ToolCall(id="c2", name="shell_run", arguments={"command": "ls"}),
                ],
                "Mail bob@example.com",
            ]
        ),
        tools=[pay, shell_run],
    )
    result = await Runner().run_async(agent, "go")  # the service's locked default policy
    assert paid == [900] and shell_ran == []  # shadow ceiling reported; enforced rule applied
    assert result.output == "Mail bob@example.com"  # the shadow scrub did not change it
    lines = [
        json.loads(line)
        for line in (tmp_path / "audit" / "guardrails.jsonl").read_text().splitlines()
    ]
    seen = {(r["rule_id"], r["action"], r["mode"], r["applied"]) for r in lines}
    assert ("payment-ceiling", "block", "shadow", False) in seen
    assert ("no-shell", "block", "enforce", True) in seen
    assert ("scrub-answers", "scrub", "shadow", False) in seen
    assert "bob@example.com" not in json.dumps(lines)  # records never hold content
