# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""P39 G6: loading a ``GuardrailPolicy`` from JSON and YAML (the ``[yaml]`` extra)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from nexura_agents import Agent, FakeModel, Runner, ToolCall, tool
from nexura_guardrails import GuardrailPolicy, JsonlAuditSink, PolicyError, load_policy
from nexura_guardrails.judge import JudgeCheck
from nexura_guardrails.ollama_chat import OllamaChatModel

POLICY_YAML = """\
version: 1
name: acme-default
on_error: block
default_tool_action: allow
timeouts: {check_seconds: 5, stage_seconds: 8}
checks:
  pii: {type: pii, kinds: [card, aadhaar, pan, secret, upi]}
  redact: {type: pii, action: redact}
  inject_fast: {type: injection, threshold: 0.5}
  bank_judge: {type: judge, model: "ollama:qwen3:8b", policy_file: policies/bank.md}
rules:
  - id: gate-writes
    when: {stage: tool_input, effect: [non_idempotent], origin: ["mcp:*", "openapi:*"]}
    action: require_approval
  - id: no-secrets-to-tools
    when: {stage: tool_input}
    check: pii
    action: block
  - id: scan-tool-results
    when: {stage: tool_output, origin: ["mcp:*", "openapi:*", "knowledge:*", "builtin:web"]}
    checks: [inject_fast]
    mode: tiered
    action: block
  - id: scrub-output
    when: {stage: [model_output, output]}
    checks: [redact]
    action: scrub
audit: {sink: jsonl, path: audit/guardrails.jsonl}
"""


def write_policy(tmp_path: Path, text: str = POLICY_YAML, name: str = "policy.yaml") -> Path:
    (tmp_path / "policies").mkdir(exist_ok=True)
    (tmp_path / "policies" / "bank.md").write_text("Block loan guarantees.", encoding="utf-8")
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_the_design_example_loads(tmp_path: Path) -> None:
    policy = load_policy(write_policy(tmp_path))
    assert policy.name == "acme-default" and len(policy.rules) == 4
    assert policy.check_timeout == 5.0 and policy.stage_timeout == 8.0
    judge = policy.checks["bank_judge"]
    assert isinstance(judge, JudgeCheck) and judge.policy == "Block loan guarantees."
    assert isinstance(judge.model, OllamaChatModel)  # the native Ollama route (G6)
    (sink,) = policy.audit
    assert isinstance(sink, JsonlAuditSink)
    assert sink.path == tmp_path.resolve() / "audit" / "guardrails.jsonl"  # relative to the file
    assert policy.source == str(tmp_path / "policy.yaml")
    summary = policy.describe()
    assert [r["id"] for r in summary["rules"]] == [
        "gate-writes",
        "no-secrets-to-tools",
        "scan-tool-results",
        "scrub-output",
    ]
    assert "Block loan" not in json.dumps(summary)  # the judge's policy text is not shown


def test_json_and_yaml_give_the_same_rules(tmp_path: Path) -> None:
    import yaml

    data = yaml.safe_load(POLICY_YAML)
    as_json = write_policy(tmp_path, json.dumps(data), "policy.json")
    from_json = load_policy(as_json)
    from_yaml = load_policy(write_policy(tmp_path))
    assert from_json.describe()["rules"] == from_yaml.describe()["rules"]


def test_digest_covers_the_judge_policy_file(tmp_path: Path) -> None:
    path = write_policy(tmp_path)
    first = load_policy(path).digest
    assert load_policy(path).digest == first
    (tmp_path / "policies" / "bank.md").write_text("Block everything.", encoding="utf-8")
    assert load_policy(path).digest != first


def test_model_resolver_maps_judge_models(tmp_path: Path) -> None:
    allow = '{"decision": "allow", "categories": [], "score": 0.0, "reason": "ok"}'
    fake = FakeModel(responses=[allow])
    seen: list[str] = []

    def resolve(name: str) -> Any:
        seen.append(name)
        return fake

    policy = load_policy(write_policy(tmp_path), model_resolver=resolve)
    assert seen == ["ollama:qwen3:8b"] and policy.checks["bank_judge"].model is fake


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("version: 2\n", "version must be 1"),
        ("version: 1\nrulez: []\n", "unknown keys"),
        ("version: 1\nchecks:\n  a: {type: regex}\n", "unknown check type"),
        ("version: 1\nchecks:\n  a: {type: pii, colour: red}\n", "unknown keys"),
        ("version: 1\nrules:\n  - {id: r, when: {stage: input, tools: [x]}}\n", "tool calls"),
        ("version: 1\nrules:\n  - {id: r, check: nope}\n", "unknown check"),
        ("version: 1\nrules:\n  - {when: {stage: input}}\n", "needs an id"),
        ("version: 1\nrules:\n  - {id: r, when: {stage: input, colour: red}}\n", "unknown keys"),
        ("version: 1\nrules:\n  - {id: r}\n  - {id: r}\n", "used twice"),
        ("version: 1\nrules: {}\n", "must be a list"),
        ("version: 1\naudit: {sink: syslog}\n", "jsonl"),
        ("version: 1\naudit: {sink: jsonl}\n", "needs a path"),
        ("version: 1\naudit: {capture_content: true}\n", "never hold content"),
        ("version: 1\ntimeouts: {check_seconds: -1}\n", "positive"),
        ("version: 1\non_error: ignore\n", "on_error"),
        ("version: 1\nname: a\nname: b\n", "duplicate key"),
        ("version: 1\nchecks:\n  j: {type: judge, policy: x}\n", "needs a model"),
        (
            "version: 1\nchecks:\n  j: {type: judge, model: m, policy_file: missing.md}\n",
            "does not exist",  # round 3: policy_file uses the policy file's safe reader
        ),
        ("version: 1\nchecks:\n  t: {type: injection, threshold: lots}\n", "invalid options"),
        ("[1, 2]\n", "must be an object"),
        ("version: 1\nchecks: [\n", "not valid YAML"),
    ],
)
def test_invalid_documents_are_refused(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(PolicyError, match=message):
        load_policy(write_policy(tmp_path, text))


def test_json_duplicate_keys_and_bad_files(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="duplicate key"):
        load_policy(write_policy(tmp_path, '{"version": 1, "version": 1}', "p.json"))
    with pytest.raises(PolicyError, match="does not exist"):
        load_policy(tmp_path / "missing.yaml")
    with pytest.raises(PolicyError, match="must end in"):
        load_policy(write_policy(tmp_path, "version: 1", "p.txt"))
    big = tmp_path / "big.json"
    big.write_text('{"version": 1, "name": "' + "x" * 1_000_001 + '"}', encoding="utf-8")
    with pytest.raises(PolicyError, match="larger than"):
        load_policy(big)


def test_yaml_tags_cannot_build_objects(tmp_path: Path) -> None:
    text = "version: 1\nname: !!python/object/apply:os.system ['echo hi']\n"
    with pytest.raises(PolicyError, match="not valid YAML"):
        load_policy(write_policy(tmp_path, text))


async def test_a_loaded_policy_enforces_its_rules(tmp_path: Path) -> None:
    text = """\
version: 1
default_tool_action: block
checks:
  redact: {type: pii, action: redact}
rules:
  - {id: allow-lookup, when: {stage: tool_input, tools: [lookup]}, action: allow}
  - {id: scrub, when: {stage: tool_output}, check: redact, action: scrub}
audit: {sink: jsonl, path: audit.jsonl, audit_allows: true}
"""
    policy = GuardrailPolicy.from_file(write_policy(tmp_path, text))

    @tool
    def lookup(q: str) -> str:
        """Look up."""
        return "card 4111 1111 1111 1111"

    @tool
    def delete_all() -> str:
        """Delete everything."""
        raise AssertionError("must never run")

    agent = Agent(
        name="A",
        instructions="x",
        model=FakeModel(
            responses=[
                [
                    ToolCall(id="c1", name="lookup", arguments={"q": "x"}),
                    ToolCall(id="c2", name="delete_all", arguments={}),
                ],
                "done",
            ]
        ),
        tools=[lookup, delete_all],
    )
    result = await Runner(hooks=[policy]).run_async(agent, "go")
    tool_texts = [m.content for m in result.messages if m.role == "tool"]
    assert tool_texts[0] == "card [REDACTED:card]"
    assert "allowlist" in tool_texts[1]
    actions = [
        json.loads(line)["action"] for line in (tmp_path / "audit.jsonl").read_text().splitlines()
    ]
    assert actions == ["allow", "scrub", "block", "allow"]
