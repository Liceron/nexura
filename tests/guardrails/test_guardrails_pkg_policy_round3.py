# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""P39 G6 round 3: the independent re-check's repros, inverted.

Offline: FakeModel, local files and subprocesses of this interpreter only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from nexura_agents import (
    Agent,
    FakeModel,
    MemoryCheckpointStore,
    RunEventType,
    Runner,
    ToolCall,
    tool,
)
from nexura_agents.errors import ProviderResponseError, SDKValidationError, ToolValidationError
from nexura_agents.policy import _unlock_for_tests
from nexura_agents.tools import ToolInfo
from nexura_guardrails import GuardrailPolicy, PolicyError, Rule, When, pii

EMAIL = "bob@example.com"
CARD = "4111 1111 1111 1111"


@pytest.fixture(autouse=True)
def _clean() -> Any:
    _unlock_for_tests()
    yield
    _unlock_for_tests()


def payer(paid: list[Any]) -> Any:
    @tool
    def pay(amount: int) -> str:
        """Pay."""
        paid.append(amount)
        return "paid"

    return pay


def big_payments(action: str = "require_approval") -> GuardrailPolicy:
    return GuardrailPolicy(
        rules=[
            Rule(
                "big",
                When(stage="tool_input", tools="pay", args={"$.amount": {"gt": 100}}),
                action=action,  # type: ignore[arg-type]
            )
        ]
    )


def one_call(arguments: dict[str, Any], tools: list[Any], name: str = "pay") -> Agent:
    return Agent(
        name="A",
        instructions="x",
        model=FakeModel(responses=[[ToolCall(id="c1", name=name, arguments=arguments)], "done"]),
        tools=tools,
    )


# ---------------------------------------------------------------- R2-H1: extra arguments


async def test_an_extra_argument_cannot_skip_approval() -> None:
    """R2-H1 stays closed under RC-1 (round 4): the stray key is dropped, "5000" is coerced,
    and the dispatched arguments hit $.amount > 100."""
    paid: list[Any] = []
    agent = one_call({"amount": "5000", "memo": "x"}, [payer(paid)])
    runner = Runner(hooks=[big_payments()])
    paused = await runner.run_async(agent, "go")
    assert paused.status == "paused" and paid == []
    assert paused.interruption is not None
    done = await runner.resume_async(agent, paused.interruption, approve=True)
    assert done.status == "completed" and paid == [5000]  # approved: runs with memo dropped


async def test_an_extra_argument_cannot_skip_a_block() -> None:
    paid: list[Any] = []
    agent = one_call({"amount": "999", "z": 0}, [payer(paid)])
    result = await Runner(hooks=[big_payments("block")]).run_async(agent, "go")
    assert paid == []
    (message,) = [m.content for m in result.messages if m.role == "tool"]
    assert message.startswith("Tool call blocked by policy rule 'big'")


async def test_a_stray_key_on_a_plain_tool_runs_with_the_key_dropped() -> None:
    """RC-1: a plain @tool ignores unknown names, as before G6; a stray key never fails."""
    seen: list[dict[str, Any]] = []

    @tool
    def lookup(q: str) -> str:
        """Look up."""
        seen.append({"q": q})
        return "ok"

    agent = one_call({"q": "x", "limit": 5, "debug": True}, [lookup], name="lookup")
    result = await Runner().run_async(agent, "go")
    assert result.status == "completed" and seen == [{"q": "x"}]
    assert lookup.dispatch_arguments({"q": "x", "limit": 5}) == {"q": "x"}


async def test_a_raw_only_block_rule_still_fires() -> None:
    """A block rule on a key the tool drops still sees it in the raw arguments."""
    seen: list[str] = []

    @tool
    def lookup(q: str) -> str:
        """Look up."""
        seen.append(q)
        return "ok"

    policy = GuardrailPolicy(
        rules=[Rule("no-debug", When(stage="tool_input", args={"$.debug": {"exists": True}}))]
    )
    agent = one_call({"q": "x", "debug": True}, [lookup], name="lookup")
    result = await Runner(hooks=[policy]).run_async(agent, "go")
    assert seen == []
    (message,) = [m.content for m in result.messages if m.role == "tool"]
    assert message.startswith("Tool call blocked by policy rule 'no-debug'")


def test_schema_tools_keep_their_json_schema_rules() -> None:
    from nexura_agents.tools import SchemaTool

    async def handler(ctx: Any, args: Any) -> Any:
        return "ok"

    open_schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    closed_schema = {**open_schema, "additionalProperties": False}
    assert SchemaTool("t", open_schema, handler).dispatch_arguments({"a": 1, "b": 2}) == {
        "a": 1,
        "b": 2,
    }
    with pytest.raises(ToolValidationError):
        SchemaTool("t", closed_schema, handler).dispatch_arguments({"a": 1, "b": 2})


async def test_invalid_arguments_are_told_to_the_model_when_retries_allow() -> None:
    paid: list[Any] = []

    @tool(max_retries=1)
    def pay(amount: int) -> str:
        """Pay."""
        paid.append(amount)
        return "paid"

    agent = one_call({"amount": "lots"}, [pay])
    result = await Runner(hooks=[big_payments()]).run_async(agent, "go")
    assert result.status == "completed" and paid == []  # no approval asked, nothing ran
    (message,) = [m.content for m in result.messages if m.role == "tool"]
    assert message.startswith("Invalid arguments for 'pay'")


def test_uncoercible_arguments_count_as_a_match_for_restricting_rules() -> None:
    from nexura_agents import RunContext

    paid: list[Any] = []
    tool_obj = payer(paid)
    ctx: RunContext[None] = RunContext(deps=None, run_id="r", agent_name="A")
    info = ToolInfo(name="pay")
    approve = big_payments()
    assert approve.call_needs_approval(ctx, info, {"amount": "lots"}, tool_object=tool_obj)
    assert not approve.call_needs_approval(ctx, info, {"amount": 5}, tool_object=tool_obj)
    allow = GuardrailPolicy(
        default_tool_action="block",
        rules=[
            Rule(
                "small",
                When(stage="tool_input", tools="pay", args={"$.amount": {"lt": 100}}),
                action="allow",
            )
        ],
    )
    from nexura_guardrails.policy.core import argument_views

    views = argument_views(tool_obj, {"amount": "lots"})
    assert len(views) == 2 and repr(views[1]) == "UNCOERCIBLE"
    assert allow._matched("tool_input", "A", info, views) == []  # allow needs both views
    assert [r.id for r in approve._matched("tool_input", "A", info, views)] == ["big"]


def test_coerced_arguments_mirror_execute() -> None:
    tool_obj = payer([])
    assert tool_obj.coerced_arguments({"amount": "5000", "memo": "x"}) == {"amount": 5000}
    assert tool_obj.coerced_arguments({"amount": "lots"}) is None


# ---------------------------------------------------------------- R2-H2: duplicate ids


@pytest.mark.parametrize("call_id", ["dup", ""])
async def test_duplicate_call_ids_are_refused_before_anything_runs(call_id: str) -> None:
    paid: list[Any] = []
    seen: list[str] = []

    class Spy(GuardrailPolicy):
        async def after_model(self, ctx: Any, response: Any) -> Any:
            seen.append("after_model")
            return None

    policy = Spy(
        rules=[
            Rule("approve-pay", When(stage="tool_input", tools="pay"), action="require_approval")
        ]
    )
    store = MemoryCheckpointStore()
    agent = Agent(
        name="A",
        instructions="x",
        model=FakeModel(
            responses=[
                [
                    ToolCall(id=call_id, name="pay", arguments={"amount": 10}),
                    ToolCall(id=call_id, name="pay", arguments={"amount": 1_000_000}),
                ],
                "done",
            ]
        ),
        tools=[payer(paid)],
    )
    events: list[Any] = []
    with pytest.raises(ProviderResponseError, match="more than one tool call"):
        async for event in Runner(checkpoint_store=store, hooks=[policy]).stream_async(agent, "go"):
            events.append(event)
    assert paid == [] and seen == []  # before after_model, any gate or dispatch
    assert not [e for e in events if e.type == RunEventType.RUN_PAUSED]
    assert [e.type for e in events][-1] == RunEventType.RUN_FAILED


async def test_a_single_empty_id_still_works() -> None:
    paid: list[Any] = []
    agent = Agent(
        name="A",
        instructions="x",
        model=FakeModel(responses=[[ToolCall(id="", name="pay", arguments={"amount": 1})], "done"]),
        tools=[payer(paid)],
    )
    result = await Runner().run_async(agent, "go")
    assert result.status == "completed" and paid == [1]


async def test_distinct_ids_each_need_their_own_approval() -> None:
    paid: list[Any] = []
    policy = GuardrailPolicy(
        rules=[
            Rule("approve-pay", When(stage="tool_input", tools="pay"), action="require_approval")
        ]
    )
    agent = Agent(
        name="A",
        instructions="x",
        model=FakeModel(
            responses=[
                [
                    ToolCall(id="a", name="pay", arguments={"amount": 10}),
                    ToolCall(id="b", name="pay", arguments={"amount": 1_000_000}),
                ],
                "done",
            ]
        ),
        tools=[payer(paid)],
    )
    runner = Runner(hooks=[policy])
    paused = await runner.run_async(agent, "go")
    assert paused.interruption is not None
    done = await runner.resume_async(agent, paused.interruption, approve={"a"}, reject={"b": "no"})
    assert done.status == "completed" and paid == [10]


# ---------------------------------------------------------------- R2-M2: tool-call deltas


def sender() -> Any:
    @tool
    def send(to: str, body: str) -> str:
        """Send."""
        return "sent"

    return send


def streamed_call_agent() -> Agent:
    return Agent(
        name="A",
        instructions="x",
        model=FakeModel(
            responses=[
                [ToolCall(id="c1", name="send", arguments={"to": "x", "body": f"card {CARD}"})],
                "done",
            ],
            stream=True,
        ),
        tools=[sender()],
    )


def arguments_streamed(events: list[Any]) -> str:
    return "".join(
        e.data.get("arguments_delta", "") for e in events if e.type == RunEventType.TOOL_CALL_DELTA
    )


async def test_tool_call_deltas_are_withheld_with_a_blocked_reply() -> None:
    policy = GuardrailPolicy(
        checks={"pii": pii()},
        rules=[Rule("no-pii-out", When(stage="model_output"), check="pii", action="block")],
    )
    plain = [e async for e in Runner().stream_async(streamed_call_agent(), "go", stream_text=True)]
    assert CARD in arguments_streamed(plain)  # control: the arguments really stream
    events = [
        e
        async for e in Runner(hooks=[policy]).stream_async(
            streamed_call_agent(), "go", stream_text=True
        )
    ]
    assert CARD not in arguments_streamed(events)
    assert not [e for e in events if e.type == RunEventType.TOOL_CALL_DELTA]


async def test_tool_call_deltas_are_released_after_an_allowed_reply() -> None:
    policy = GuardrailPolicy(
        checks={"pii": pii(kinds=["email"])},
        rules=[Rule("no-mail-out", When(stage="model_output"), check="pii", action="block")],
    )
    events = [
        e
        async for e in Runner(hooks=[policy]).stream_async(
            streamed_call_agent(), "go", stream_text=True
        )
    ]
    deltas = [i for i, e in enumerate(events) if e.type == RunEventType.TOOL_CALL_DELTA]
    completed = [i for i, e in enumerate(events) if e.type == RunEventType.MODEL_COMPLETED]
    assert deltas and CARD in arguments_streamed(events)
    assert min(deltas) > completed[0]  # held until after_model passed the reply


# ---------------------------------------------------------------- R2-M3 / R2-L1: files

DOC = json.dumps({"version": 1, "rules": []})


def configmap(tmp_path: Path, target_name: str = "policy.json") -> Path:
    """policy.json -> ..data/policy.json -> ..2026_10_25/policy.json (Kubernetes layout)."""
    stamp = tmp_path / "..2026_10_25_00_00_00.1"
    stamp.mkdir()
    (tmp_path / "..data").symlink_to(stamp.name)
    (tmp_path / "policy.json").symlink_to(f"..data/{target_name}")
    return stamp


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX")
def test_a_configmap_symlink_chain_loads_and_resolves_relative_paths(tmp_path: Path) -> None:
    stamp = configmap(tmp_path)
    (stamp / "bank.md").write_text("Block loan guarantees.")
    (stamp / "policy.json").write_text(
        json.dumps(
            {
                "version": 1,
                "checks": {
                    "j": {"type": "judge", "model": "ollama:qwen3:8b", "policy_file": "bank.md"}
                },
                "audit": {"sink": "jsonl", "path": "audit.jsonl"},
            }
        )
    )
    policy = GuardrailPolicy.from_file(tmp_path / "policy.json")
    assert policy.checks["j"].policy == "Block loan guarantees."  # type: ignore[attr-defined]
    assert policy.audit[0].path == stamp.resolve() / "audit.jsonl"
    assert policy.source == str(tmp_path / "policy.json")
    assert policy.resolved_source == str((stamp / "policy.json").resolve())


def serve(tmp_path: Path, policy: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "nexura_runtime.cli.main",
            "serve",
            "--db",
            str(tmp_path / "s.db"),
            "--guardrail-policy",
            str(policy),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
        check=False,
    )


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX")
def test_a_symlink_to_a_fifo_is_refused_with_exit_2(tmp_path: Path) -> None:
    stamp = configmap(tmp_path)
    os.mkfifo(stamp / "policy.json")
    proc = serve(tmp_path, tmp_path / "policy.json")
    assert proc.returncode == 2 and "not a regular file" in proc.stderr
    assert not (tmp_path / "s.db").exists()


def test_a_symlink_to_a_directory_is_refused_with_exit_2(tmp_path: Path) -> None:
    stamp = configmap(tmp_path)
    (stamp / "policy.json").mkdir()
    proc = serve(tmp_path, tmp_path / "policy.json")
    assert proc.returncode == 2 and "policy file" in proc.stderr
    assert "Traceback" not in proc.stderr and not (tmp_path / "s.db").exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX")
def test_a_judge_policy_file_gets_the_same_guards(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "bank.md")
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "checks": {
                    "j": {"type": "judge", "model": "ollama:qwen3:8b", "policy_file": "bank.md"}
                },
            }
        )
    )
    with pytest.raises(PolicyError, match="not a regular file"):
        GuardrailPolicy.from_file(path)  # refused at once, no hang
    big = tmp_path / "big.md"
    big.write_text("x" * 1_000_001)
    path.write_text(path.read_text().replace("bank.md", "big.md"))
    with pytest.raises(PolicyError, match="larger than"):
        GuardrailPolicy.from_file(path)


# ---------------------------------------------------------------- R2-L2: coordinator


def scrubbing_policy() -> GuardrailPolicy:
    return GuardrailPolicy(
        checks={"redact": pii(action="redact")},
        rules=[
            Rule("scrub", When(stage=["model_output", "output"]), check="redact", action="scrub")
        ],
    )


async def test_the_coordinator_is_refused_with_its_own_policy_hooks() -> None:
    from nexura_runtime.runtime.agents.coordinator import AgentCoordinator
    from nexura_runtime.runtime.agents.definition import AgentDefinition

    worker = AgentDefinition(name="w", model=FakeModel(responses=[f"mail {EMAIL}"]))
    with pytest.raises(SDKValidationError, match="guardrail policy"):
        await AgentCoordinator([worker], hooks=scrubbing_policy()).run_async("w", "hi")


async def test_the_coordinator_is_refused_under_an_unlocked_default() -> None:
    from nexura_runtime.runtime.agents.coordinator import AgentCoordinator
    from nexura_runtime.runtime.agents.definition import AgentDefinition

    scrubbing_policy().install()  # not locked
    worker = AgentDefinition(name="w", model=FakeModel(responses=[f"mail {EMAIL}"]))
    with pytest.raises(SDKValidationError, match="guardrail policy"):
        await AgentCoordinator([worker]).run_async("w", "hi")


def test_check_model_stages_for_the_subagent_fix() -> None:
    """The one-line S12-side fix calls check_model_stages(False) at the start of _delegate."""
    from nexura_agents.policy import check_model_stages

    check_model_stages(False)  # nothing installed: allowed
    scrubbing_policy().install()
    with pytest.raises(SDKValidationError):
        check_model_stages(False)
    check_model_stages(True)  # a caller that applies the stages is never refused
