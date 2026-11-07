# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""The ``guardrails_ai`` preset of saved agents: Guardrails AI validators run by the service.

Offline: FakeModel chat models and stand-in validator modules registered in ``sys.modules``
under the real import paths (the validator packages are not installed in tests).
"""

from __future__ import annotations

import importlib.machinery
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from _local_env import local_headers
from fastapi.testclient import TestClient
from guardrails.validators import FailResult, PassResult, Validator, register_validator
from nexura_agents import ToolCall
from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import ModuleManifest
from nexura_agents.contracts.result import ModuleResult
from nexura_agents.policy import _unlock_for_tests
from nexura_agents.types import Message
from pydantic import ValidationError

from nexura_runtime.agents_spec import GUARDRAILS_AI_VALIDATORS, AgentGuardrails
from nexura_runtime.service.api import guardrails as service_guardrails
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.guardrails import (
    GUARDRAIL_POLICY_ENV,
    guardrails_ai_available,
    preload_guardrails_ai,
)
from nexura_runtime.service.workspaces.repository import WorkspaceRepository
from nexura_runtime.workflow import ModuleRegistry

API = "/api/v1"
SECRET = "sk-test-0123456789abcdef"


@register_validator(name="guardrails/profanity_free", data_type="string")
class ProfanityFree(Validator):
    """Stand-in: fails on the word 'darn'."""

    def _validate(self, value: Any, metadata: dict[str, Any]) -> PassResult | FailResult:
        return FailResult(error_message="profane") if "darn" in value else PassResult()


@register_validator(name="guardrails/secrets_present", data_type="string")
class SecretsPresent(Validator):
    """Stand-in: fails on 'sk-' and echoes the secret, as the real validator does."""

    def _validate(self, value: Any, metadata: dict[str, Any]) -> PassResult | FailResult:
        if "sk-" in value:
            found = value[value.index("sk-") :].split()[0]
            return FailResult(error_message=f"secrets detected:\n{found}", fix_value="***")
        return PassResult()


def _fake_module(name: str, cls: type) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__spec__ = importlib.machinery.ModuleSpec(name, None)
    setattr(module, cls.__name__, cls)
    return module


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(GUARDRAIL_POLICY_ENV, raising=False)
    monkeypatch.delenv(service_guardrails.GUARDRAILS_AI_PRELOAD_ENV, raising=False)
    monkeypatch.setenv("NEXURA_INSECURE_ALLOW_PLAINTEXT_SECRETS", "I_UNDERSTAND_THIS_IS_UNSAFE")

    async def refuse(self: Any, request: httpx.Request) -> httpx.Response:
        raise AssertionError("Guardrails AI service tests never reach the network")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)
    _unlock_for_tests()
    yield
    _unlock_for_tests()


@pytest.fixture
def validators_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        sys.modules,
        "guardrails_ai.profanity_free",
        _fake_module("guardrails_ai.profanity_free", ProfanityFree),
    )
    monkeypatch.setitem(
        sys.modules,
        "guardrails_ai.secrets_present",
        _fake_module("guardrails_ai.secrets_present", SecretsPresent),
    )


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
        return ModuleResult.success({"answer": f"the key is {SECRET}"})


def _scripted(messages: list[Message], tools: Any) -> Any:
    last = messages[-1]
    if last.role == "tool":
        return f"Tool said: {last.content}"
    if "look" in str(last.content):
        return [ToolCall(id="t1", name="test_lookup_read", arguments={"q": "x"})]
    if "leak" in str(last.content):
        return f"Here it is: {SECRET}"
    return "All good."


@pytest.fixture
def env(tmp_path: Path) -> dict[str, Any]:
    repo = WorkspaceRepository()
    repo.create_workspace("wsA", "A", owner_id="local-owner")
    registry = ModuleRegistry()
    registry.register("test.lookup.read", LookupModule())
    app = create_api_app(
        registry=registry,
        db_path=tmp_path / "s.db",
        workspace_repo=repo,
        chat_models={"scripted": __import__("nexura_agents").FakeModel(handler=_scripted)},
    )
    return {"c": TestClient(app), "h": local_headers("wsA", "admin"), "app": app}


def _create(env: dict[str, Any], guardrails: dict[str, Any]) -> Any:
    spec = {
        "name": "helper",
        "model": "scripted",
        "tools": ["test.lookup.read"],
        "guardrails": guardrails,
    }
    return env["c"].post(f"{API}/agents", headers=env["h"], json=spec)


def _chat(env: dict[str, Any], agent_id: str, message: str) -> Any:
    return env["c"].post(
        f"{API}/sessions/s1/chat",
        headers=env["h"],
        json={"message": message, "agent_id": agent_id},
    )


BOTH = {"presets": [{"preset": "guardrails_ai", "validators": list(GUARDRAILS_AI_VALIDATORS)}]}


# ---------------------------------------------------------------- spec


@pytest.mark.parametrize(
    ("preset", "message"),
    [
        ({"preset": "guardrails_ai"}, "needs validators"),
        ({"preset": "guardrails_ai", "validators": []}, "needs validators"),
        ({"preset": "guardrails_ai", "validators": ["toxic_language"]}, "unknown Guardrails AI"),
        (
            {"preset": "guardrails_ai", "validators": ["profanity_free", "profanity_free"]},
            "listed twice",
        ),
        ({"preset": "pii_block", "validators": ["profanity_free"]}, "does not take validators"),
    ],
)
def test_spec_refuses_bad_validator_lists(preset: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        AgentGuardrails.model_validate({"presets": [preset]})


def test_spec_refuses_passthrough_streaming() -> None:
    with pytest.raises(ValidationError, match="guardrails_ai"):
        AgentGuardrails.model_validate({**BOTH, "stream_guard": "passthrough"})


# ---------------------------------------------------------------- catalogue and save


def test_catalogue_without_the_packages(env: dict[str, Any]) -> None:
    assert guardrails_ai_available() == {"profanity_free": False, "secrets_present": False}
    body = env["c"].get(f"{API}/guardrails/presets").json()
    (info,) = [p for p in body["presets"] if p["name"] == "guardrails_ai"]
    assert info["network"] == "none" and info["options"] == ["validators"]
    assert info["stages"] == ["input", "output", "tool_output"]
    assert [v["name"] for v in info["validators"]] == ["profanity_free", "secrets_present"]
    assert not any(v["available"] for v in info["validators"])
    assert "guardrails-ai-profanity-free" in info["requires"]


def test_save_is_refused_when_a_validator_is_not_installed(env: dict[str, Any]) -> None:
    res = _create(env, BOTH)
    assert res.status_code == 422, res.text
    (error,) = res.json()["detail"]["field_errors"]
    assert error["field"] == "guardrails.presets.0"
    assert "not installed" in error["message"]
    assert "guardrails-ai-secrets-present" in error["message"]


def test_catalogue_with_the_packages(env: dict[str, Any], validators_installed: None) -> None:
    body = env["c"].get(f"{API}/agents/options", headers=env["h"]).json()
    (info,) = [p for p in body["guardrails"]["presets"] if p["name"] == "guardrails_ai"]
    assert all(v["available"] for v in info["validators"])


# ---------------------------------------------------------------- runs


def test_profanity_in_the_message_is_blocked(
    env: dict[str, Any], validators_installed: None
) -> None:
    res = _create(env, {"presets": [{"preset": "guardrails_ai", "validators": ["profanity_free"]}]})
    assert res.status_code == 201, res.text
    agent_id = res.json()["agent_id"]
    ok = _chat(env, agent_id, "hello there")
    assert ok.status_code == 200 and ok.json()["reply"] == "All good.", ok.text
    blocked = _chat(env, agent_id, "this darn thing")
    assert blocked.status_code == 422, blocked.text
    assert blocked.json()["detail"]["code"] == "GUARDRAIL_BLOCKED"


def test_secrets_in_answers_and_tool_results_are_blocked(
    env: dict[str, Any], validators_installed: None
) -> None:
    res = _create(env, BOTH)
    assert res.status_code == 201, res.text
    agent_id = res.json()["agent_id"]
    built = env["app"].state.agent_runtime.build_agent("wsA", agent_id, None)
    assert [g.__name__ for g in built.agent.input_guardrails] == ["guardrails_ai"]
    assert [g.__name__ for g in built.agent.output_guardrails] == ["guardrails_ai"]
    assert built.agent.output_guardrails[0].nexura_no_passthrough is True
    leak = _chat(env, agent_id, "please leak it")
    assert leak.status_code == 422 and SECRET not in leak.text, leak.text
    assert leak.json()["detail"]["code"] == "GUARDRAIL_BLOCKED"
    looked = _chat(env, agent_id, "look it up")
    assert SECRET not in looked.text, looked.text
    assert looked.status_code == 200 and "blocked" in looked.json()["reply"], looked.text


async def test_guard_evidence_has_no_failure_reasons(validators_installed: None) -> None:
    from nexura_guardrails import evaluate

    preset = service_guardrails._guardrails_ai_preset(["secrets_present"])
    result = await evaluate(preset.checks["output"], f"token {SECRET}", stage="output")
    assert result.decision == "block" and result.categories == ("privacy",)
    assert SECRET not in repr(result.info())
    assert result.evidence["validators"][0]["name"] == "guardrails/secrets_present"


# ---------------------------------------------------------------- preload


def test_preload_is_off_unless_asked(validators_installed: None) -> None:
    assert preload_guardrails_ai({}) is None
    assert preload_guardrails_ai({"NEXURA_GUARDRAILS_AI_PRELOAD": "0"}) is None


def test_preload_without_validators_logs_and_does_nothing(caplog: pytest.LogCaptureFixture) -> None:
    assert preload_guardrails_ai({"NEXURA_GUARDRAILS_AI_PRELOAD": "1"}) is None
    assert "no Guardrails AI validator is installed" in caplog.text


def test_preload_imports_the_installed_validators(validators_installed: None) -> None:
    thread = preload_guardrails_ai({"NEXURA_GUARDRAILS_AI_PRELOAD": "on"})
    assert thread is not None
    thread.join(timeout=60)
    assert not thread.is_alive()
    assert "nexura_guardrails_ai" in sys.modules


def test_stream_and_plain_chat_name_a_guardrail_block() -> None:
    from nexura_runtime.service.api.sessions import _safe_failure

    code, message = _safe_failure("GuardrailTripwireTriggered")
    assert code == "GUARDRAIL_BLOCKED" and "guardrail" in message
