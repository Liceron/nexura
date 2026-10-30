# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""P39 G6 round 2: the independent security review's repros,
inverted. Each test asserts the fixed behaviour. Offline: FakeModel, local files.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from nexura_agents import (
    Agent,
    FakeModel,
    GuardrailTripwireTriggered,
    MemoryCheckpointStore,
    RunEventType,
    Runner,
    ToolCall,
    tool,
)
from nexura_agents.errors import SDKValidationError
from nexura_agents.policy import _unlock_for_tests
from nexura_guardrails import GuardrailPolicy, PolicyError, Rule, Verdict, When, pii, topics

EMAIL = "bob@example.com"


@pytest.fixture(autouse=True)
def _clean() -> Any:
    _unlock_for_tests()
    yield
    _unlock_for_tests()


def shown(events: list[Any]) -> str:
    return "".join(e.data.get("text", "") for e in events if e.type == RunEventType.TEXT_DELTA)


def done(events: list[Any]) -> dict[str, Any]:
    (event,) = [e for e in events if e.type == RunEventType.RUN_COMPLETED]
    data: dict[str, Any] = event.data
    return data


def streaming(text: str) -> Agent:
    return Agent(name="A", instructions="x", model=FakeModel(responses=[text], stream=True))


# ---------------------------------------------------------------- H1: streamed text


async def test_h1_model_output_scrub_holds_or_windows_the_stream() -> None:
    policy = GuardrailPolicy(
        checks={"pii": pii(action="redact")},
        rules=[Rule("scrub-model", When(stage="model_output"), check="pii", action="scrub")],
    )
    events = [
        e
        async for e in Runner(hooks=[policy]).stream_async(
            streaming(f"mail {EMAIL} now"), "hi", stream_text=True
        )
    ]
    assert EMAIL not in shown(events)
    assert done(events)["output"] == "mail [REDACTED:email] now"
    # Every rule is a regex scrub: "auto" checks windows (chunked) instead of holding.
    (started,) = [e for e in events if e.type == RunEventType.MODEL_STARTED]
    assert started.data["stream_guard"] == "chunked"
    assert shown(events) == "mail [REDACTED:email] now"


async def test_h1_model_output_block_is_never_streamed() -> None:
    policy = GuardrailPolicy(
        checks={"deny": topics(deny=["launchcodes"], name="deny")},
        rules=[Rule("no-codes", When(stage="model_output"), check="deny", action="block")],
    )
    events = [
        e
        async for e in Runner(hooks=[policy]).stream_async(
            streaming("the launchcodes are 1234"), "hi", stream_text=True
        )
    ]
    assert "1234" not in shown(events)
    (started,) = [e for e in events if e.type == RunEventType.MODEL_STARTED]
    assert started.data["stream_guard"] == "hold"  # a block is not chunk-safe
    assert done(events)["output"] == "Reply withheld by policy rule 'no-codes'."


async def test_h1_interim_text_before_tool_calls_is_checked() -> None:
    @tool
    def lookup(q: str) -> str:
        """Look up."""
        return "ok"

    from nexura_agents.types import Message, ModelResponse

    reply = ModelResponse(
        message=Message(
            role="assistant",
            content=f"Checking for {EMAIL}",
            tool_calls=[ToolCall(id="c1", name="lookup", arguments={"q": "x"})],
        )
    )
    policy = GuardrailPolicy(
        checks={"pii": pii(action="redact")},
        rules=[Rule("scrub", When(stage="model_output"), check="pii", action="scrub")],
    )
    agent = Agent(
        name="A",
        instructions="x",
        model=FakeModel(responses=[reply, "done"], stream=True),
        tools=[lookup],
    )
    events = [e async for e in Runner(hooks=[policy]).stream_async(agent, "hi", stream_text=True)]
    assert EMAIL not in shown(events)
    assert EMAIL not in json.dumps([m.content for m in done(events)["messages"]], default=str)


async def test_h1_passthrough_is_refused_while_reply_rules_apply() -> None:
    policy = GuardrailPolicy(
        checks={"pii": pii(action="redact")},
        rules=[Rule("scrub", When(stage="model_output"), check="pii", action="scrub")],
    )
    agent = Agent(
        name="A",
        instructions="x",
        model=FakeModel(responses=[f"mail {EMAIL}"], stream=True),
        stream_guard="passthrough",
    )
    with pytest.raises(SDKValidationError, match="passthrough"):
        await Runner(hooks=[policy]).run_async(agent, "hi")
    # A rule for another agent does not refuse it.
    other = GuardrailPolicy(
        checks={"pii": pii(action="redact")},
        rules=[Rule("s", When(stage="output", agents="billing"), check="pii", action="scrub")],
    )
    assert (await Runner(hooks=[other]).run_async(agent, "hi")).output == f"mail {EMAIL}"


async def test_h1_a_judge_reply_rule_holds_and_is_asked_once_per_text() -> None:
    calls: list[str] = []

    class Judge:
        name = "judge"
        stages = frozenset({"output", "model_output"})
        deterministic = False

        async def __call__(self, ctx: Any, item: Any) -> Verdict:
            calls.append(item.text)
            return Verdict("allow", check="judge")

    policy = GuardrailPolicy(
        checks={"judge": Judge()},  # type: ignore[dict-item]
        rules=[Rule("j", When(stage="model_output"), check="judge", action="block")],
    )
    events = [
        e
        async for e in Runner(hooks=[policy]).stream_async(
            streaming("a fine answer"), "hi", stream_text=True
        )
    ]
    (started,) = [e for e in events if e.type == RunEventType.MODEL_STARTED]
    assert started.data["stream_guard"] == "hold"
    assert done(events)["output"] == "a fine answer"
    assert calls == ["a fine answer"]  # after_model judged it; the held release reuses that


async def test_m2_pii_redact_preset_refuses_passthrough() -> None:
    from nexura_guardrails import presets

    agent = Agent(
        name="A",
        instructions="x",
        model=FakeModel(responses=[f"mail {EMAIL}"], stream=True),
        output_guardrails=list(presets.pii_redact().output_guardrails),
        stream_guard="passthrough",
    )
    with pytest.raises(SDKValidationError, match="passthrough"):
        [e async for e in Runner().stream_async(agent, "hi", stream_text=True)]


# ---------------------------------------------------------------- H2: coerced arguments


def payer(paid: list[Any]) -> Any:
    @tool
    def pay(amount: int) -> str:
        """Pay."""
        paid.append(amount)
        return "paid"

    return pay


@pytest.mark.parametrize("amount", [5000, "5000", " 5000 ", "5000.0"])
async def test_h2_string_numbers_hit_argument_rules(amount: Any) -> None:
    paid: list[Any] = []
    policy = GuardrailPolicy(
        rules=[
            Rule(
                "big-payments",
                When(stage="tool_input", tools="pay", args={"$.amount": {"gt": 100}}),
                action="require_approval",
            )
        ]
    )
    agent = Agent(
        name="A",
        instructions="x",
        model=FakeModel(
            responses=[[ToolCall(id="c1", name="pay", arguments={"amount": amount})], "done"]
        ),
        tools=[payer(paid)],
    )
    result = await Runner(hooks=[policy]).run_async(agent, "go")
    assert paid == []  # never paid without approval
    try:
        payer([]).args_schema.model_validate({"amount": amount})
    except Exception:
        return  # the tool refuses this value itself
    assert result.status == "paused"


async def test_h2_block_rule_sees_coerced_booleans() -> None:
    ran: list[Any] = []

    @tool
    def delete(path: str, force: bool = False) -> str:
        """Delete."""
        ran.append(force)
        return "deleted"

    policy = GuardrailPolicy(
        rules=[Rule("no-force", When(stage="tool_input", args={"$.force": {"eq": True}}))]
    )
    agent = Agent(
        name="A",
        instructions="x",
        model=FakeModel(
            responses=[
                [ToolCall(id="c1", name="delete", arguments={"path": "/x", "force": "true"})],
                "done",
            ]
        ),
        tools=[delete],
    )
    await Runner(hooks=[policy]).run_async(agent, "go")
    assert ran == []


async def test_h2_allow_rules_must_match_both_views() -> None:
    """Allowlist entries take the most severe reading: an allow by argument value admits a
    call only when the raw and the coerced arguments both match."""
    paid: list[Any] = []
    policy = GuardrailPolicy(
        default_tool_action="block",
        rules=[
            Rule(
                "small",
                When(stage="tool_input", tools="pay", args={"$.amount": {"eq": "50"}}),
                action="allow",
            )
        ],
    )
    agent = Agent(
        name="A",
        instructions="x",
        model=FakeModel(
            responses=[[ToolCall(id="c1", name="pay", arguments={"amount": "50"})], "done"]
        ),
        tools=[payer(paid)],
    )
    await Runner(hooks=[policy]).run_async(agent, "go")
    assert paid == []  # raw "50" matches, but the tool would receive 50: not allowed


# ---------------------------------------------------------------- H3: verdict identity


class ToolAwareJudge:
    name = "judge"
    stages = frozenset({"tool_input"})
    deterministic = False

    def __init__(self) -> None:
        self.calls: list[str | None] = []

    async def __call__(self, ctx: Any, item: Any) -> Verdict:
        self.calls.append(item.tool_name)
        if item.tool_name == "delete_file":
            return Verdict("block", categories=("other",), check="judge")
        return Verdict("allow", check="judge")


def file_tools(ran: list[str]) -> list[Any]:
    @tool
    def read_file(path: str) -> str:
        """Read."""
        ran.append("read")
        return "content"

    @tool
    def delete_file(path: str) -> str:
        """Delete."""
        ran.append("delete")
        return "deleted"

    return [read_file, delete_file]


async def test_h3_duplicate_call_ids_never_share_a_verdict_across_tools() -> None:
    """Round 3 (R2-H2): a reply with two calls sharing an id is refused outright, before any
    judge, gate or dispatch (it was judged per tool in round 2)."""
    from nexura_agents.errors import ProviderResponseError

    ran: list[str] = []
    judge = ToolAwareJudge()
    policy = GuardrailPolicy(
        checks={"judge": judge},  # type: ignore[dict-item]
        rules=[Rule("judge-tools", When(stage="tool_input"), check="judge", action="block")],
    )
    agent = Agent(
        name="A",
        instructions="x",
        model=FakeModel(
            responses=[
                [
                    ToolCall(id="dup", name="read_file", arguments={"path": "/d"}),
                    ToolCall(id="dup", name="delete_file", arguments={"path": "/d"}),
                ],
                "ok",
            ]
        ),
        tools=file_tools(ran),
    )
    with pytest.raises(ProviderResponseError, match="dup"):
        await Runner(hooks=[policy]).run_async(agent, "go")
    assert ran == [] and judge.calls == []


async def test_h3_a_stored_verdict_with_another_identity_is_not_reused() -> None:
    from nexura_agents import RunContext
    from nexura_agents.verdict_journal import RunVerdicts, recorded_verdict, verdict_identity

    ctx: RunContext[None] = RunContext(deps=None, run_id="r", agent_name="a")
    ctx._verdicts = RunVerdicts(lambda: None)
    parts = {
        "policy_digest": "p",
        "rule_id": "r",
        "check": "j",
        "stage": "tool_input",
        "step": "turn:1/call:x",
        "text": "{}",
        "agent": "a",
        "tool": "read_file",
    }
    stored = verdict_identity(**parts)
    await ctx._verdicts.put("k", {"decision": "allow", "identity": stored})
    assert recorded_verdict(ctx, "k", stored) is not None
    other = verdict_identity(**{**parts, "tool": "delete_file"})
    assert recorded_verdict(ctx, "k", other) is None  # same key, different identity: ask again
    await ctx._verdicts.put("old", {"decision": "allow"})  # a record without an identity
    assert recorded_verdict(ctx, "old", stored) is None


async def test_h3_recovery_still_reuses_an_exact_match() -> None:
    from nexura_guardrails import CallbackAuditSink

    class Crash(BaseException):
        pass

    ran: list[str] = []
    judge = ToolAwareJudge()
    armed = {"on": True}

    def sink(record: Any) -> None:
        if armed["on"]:
            armed["on"] = False
            raise Crash

    policy = GuardrailPolicy(
        checks={"judge": judge},  # type: ignore[dict-item]
        rules=[Rule("judge-tools", When(stage="tool_input"), check="judge", action="block")],
        audit=CallbackAuditSink(sink),
        audit_allows=True,
    )
    store = MemoryCheckpointStore()
    tools = file_tools(ran)
    first = Agent(
        name="A",
        instructions="x",
        model=FakeModel(
            responses=[[ToolCall(id="c1", name="read_file", arguments={"path": "/d"})], "ok"]
        ),
        tools=tools,
    )
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store, hooks=[policy]).run_async(first, "go")
    (run_id,) = await store.list_incomplete()
    again = Agent(name="A", instructions="x", model=FakeModel(responses=["ok"]), tools=tools)
    result = await Runner(checkpoint_store=store, hooks=[policy]).recover_async(again, run_id)
    assert result.status == "completed" and ran == ["read"] and judge.calls == ["read_file"]


# ---------------------------------------------------------------- M1: coordinator


async def test_m1_coordinator_is_refused_under_a_locked_model_stage_policy() -> None:
    from nexura_runtime.runtime.agents.coordinator import AgentCoordinator
    from nexura_runtime.runtime.agents.definition import AgentDefinition

    policy = GuardrailPolicy(
        checks={"redact": pii(action="redact")},
        rules=[Rule("scrub", When(stage="output"), check="redact", action="scrub")],
    )
    policy.install(locked=True)
    worker = AgentDefinition(name="w", model=FakeModel(responses=[f"mail {EMAIL}"]))
    with pytest.raises(SDKValidationError, match="coordinator|AgentCoordinator"):
        await AgentCoordinator([worker]).run_async("w", "hello")
    # A Runner under the same lock applies the policy.
    agent = Agent(name="w", instructions="x", model=FakeModel(responses=[f"mail {EMAIL}"]))
    assert EMAIL not in (await Runner().run_async(agent, "hello")).output


async def test_m1_a_tool_only_policy_still_allows_the_coordinator() -> None:
    from nexura_runtime.runtime.agents.coordinator import AgentCoordinator
    from nexura_runtime.runtime.agents.definition import AgentDefinition

    GuardrailPolicy(rules=[Rule("no-shell", When(stage="tool_input", tools="shell"))]).install(
        locked=True
    )
    worker = AgentDefinition(name="w", model=FakeModel(responses=["hi"]))
    result = await AgentCoordinator([worker]).run_async("w", "hello")
    assert result.status == "completed"


# ---------------------------------------------------------------- M3: editor regex


async def test_m3_workflow_config_cannot_carry_regex() -> None:
    from nexura_agents.run_context import RunContext
    from nexura_guardrails.runtime import GuardrailCheckModule
    from nexura_guardrails.verdict import CheckInput

    module = GuardrailCheckModule()
    with pytest.raises(PolicyError, match="extra_patterns"):
        module._checks({"checks": {"x": {"type": "injection", "extra_patterns": ["^(a+)+$"]}}})
    # Topic keywords are matched literally, so a pathological "pattern" costs nothing.
    (check,) = module._checks({"checks": {"t": {"type": "topics", "deny": ["^(a+)+$"]}}})
    ctx: RunContext[Any] = RunContext(deps=None, run_id="r", agent_name="n")
    started = time.perf_counter()
    verdict = await check(ctx, CheckInput(stage="input", text="a" * 40 + "!"))
    assert verdict.decision == "allow" and time.perf_counter() - started < 1.0


# ---------------------------------------------------------------- L1: digest kept


@pytest.mark.parametrize("digest", ["a1234567bcdef012", "1234567890123456", "abcdef12ab34cd56"])
def test_l1_policy_digest_survives_the_digit_filter(digest: str) -> None:
    from nexura_agents.guardrails import decision_record

    record = decision_record("tool_input", "p", "block", info={"policy_digest": digest})
    assert record["policy_digest"] == digest
    bad = decision_record("tool_input", "p", "block", info={"policy_digest": "card 4111"})
    assert "policy_digest" not in bad


# ---------------------------------------------------------------- L2: loader hardening


def test_l2_yaml_aliases_and_anchors_are_refused(tmp_path: Path) -> None:
    text = "version: 1\nrules:\n  - &r {id: a, when: {stage: input}, action: block}\n  - *r\n"
    path = tmp_path / "p.yaml"
    path.write_text(text)
    with pytest.raises(PolicyError, match="anchors and aliases"):
        GuardrailPolicy.from_file(path)


@pytest.mark.parametrize("suffix", [".json", ".yaml"])
def test_l2_deep_nesting_is_a_policy_error(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / f"deep{suffix}"
    path.write_text('{"version": 1, "name": ' + "[" * 200_000 + "]" * 200_000 + "}")
    with pytest.raises(PolicyError, match="nested deeper"):
        GuardrailPolicy.from_file(path)
    shallow = tmp_path / f"shallow{suffix}"
    shallow.write_text('{"version": 1, "name": ' + "[" * 40 + "]" * 40 + "}")
    with pytest.raises(PolicyError, match="nested deeper"):
        GuardrailPolicy.from_file(shallow)


def test_l2_cli_serve_exits_2_for_a_deep_policy(tmp_path: Path) -> None:
    path = tmp_path / "deep.json"
    path.write_text('{"version": 1, "name": ' + "[" * 200_000 + "]" * 200_000 + "}")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "nexura_runtime.cli.main",
            "serve",
            "--db",
            str(tmp_path / "s.db"),
            "--guardrail-policy",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
        check=False,
    )
    assert proc.returncode == 2 and "nested deeper" in proc.stderr
    assert "Traceback" not in proc.stderr and not (tmp_path / "s.db").exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo") or not hasattr(os, "O_NOFOLLOW"), reason="POSIX")
def test_l2_only_regular_files_are_read(tmp_path: Path) -> None:
    """Round 3 (R2-M3): links are resolved once; the resolved target must be a regular file."""
    real = tmp_path / "real.json"
    real.write_text('{"version": 1}')
    link = tmp_path / "link.json"
    link.symlink_to(real)
    assert GuardrailPolicy.from_file(link).name == "policy"
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    with pytest.raises(PolicyError, match="not a regular file"):
        GuardrailPolicy.from_file(fifo)  # does not hang waiting for a writer
    device = tmp_path / "zero.json"
    device.symlink_to("/dev/zero")
    with pytest.raises(PolicyError):
        GuardrailPolicy.from_file(device)


def test_l2_size_cap_counts_what_is_read(tmp_path: Path) -> None:
    big = tmp_path / "big.json"
    big.write_text('{"version": 1, "name": "' + "x" * 1_000_001 + '"}')
    with pytest.raises(PolicyError, match="larger than"):
        GuardrailPolicy.from_file(big)


# ---------------------------------------------------------------- L4: block before approval


async def test_l4_a_blocked_call_is_not_sent_for_approval() -> None:
    ran: list[str] = []

    @tool
    def shell(command: str) -> str:
        """Run."""
        ran.append(command)
        return "ok"

    policy = GuardrailPolicy(
        rules=[
            Rule(
                "approve-shell", When(stage="tool_input", tools="shell"), action="require_approval"
            ),
            Rule("never-shell", When(stage="tool_input", tools="shell"), action="block"),
        ]
    )
    agent = Agent(
        name="A",
        instructions="x",
        model=FakeModel(
            responses=[[ToolCall(id="c1", name="shell", arguments={"command": "ls"})], "done"]
        ),
        tools=[shell],
    )
    result = await Runner(hooks=[policy]).run_async(agent, "go")
    assert result.status == "completed" and ran == []
    assert "never-shell" in [m.content for m in result.messages if m.role == "tool"][0]


# ---------------------------------------------------------------- L5: stage timeout wording


async def test_l5_stage_timeout_follows_on_error() -> None:
    class Slow:
        name = "slow"
        stages = frozenset({"output"})
        deterministic = True

        async def __call__(self, ctx: Any, item: Any) -> Verdict:
            await asyncio.sleep(5)
            return Verdict("block", check="slow")

    for on_error, blocked in (("allow", False), ("block", True)):
        policy = GuardrailPolicy(
            checks={"slow": Slow()},  # type: ignore[dict-item]
            rules=[Rule("r", When(stage="output"), check="slow", action="block")],
            on_error=on_error,  # type: ignore[arg-type]
            stage_timeout=0.05,
        )
        agent = Agent(name="A", instructions="x", model=FakeModel(responses=["answer"]))
        if blocked:
            with pytest.raises(GuardrailTripwireTriggered):
                await Runner(hooks=[policy]).run_async(agent, "hi")
        else:
            assert (await Runner(hooks=[policy]).run_async(agent, "hi")).output == "answer"
    docs = (Path(__file__).resolve().parents[2] / "docs" / "guardrail-policy.md").read_text()
    assert "a stage timeout blocks;" not in docs
    assert "stage timeout follows `on_error`" in docs


async def test_h3_the_tool_name_is_part_of_the_journaled_identity() -> None:
    """Same step, same occurrence, same text: a verdict for one tool is not one for another."""
    from nexura_agents import RunContext
    from nexura_agents.verdict_journal import RunVerdicts

    judge = ToolAwareJudge()
    policy = GuardrailPolicy(
        checks={"judge": judge},  # type: ignore[dict-item]
        rules=[Rule("j", When(stage="tool_input"), check="judge", action="block")],
    )
    base: RunContext[None] = RunContext(deps=None, run_id="r", agent_name="a", turn=1)
    base._verdicts = RunVerdicts(lambda: None)
    for name in ("read_file", "delete_file"):
        ctx = base.for_tool("x", name, 0)
        await policy.evaluate(ctx, "tool_input", '{"path": "/d"}', arguments={"path": "/d"})
    assert judge.calls == ["read_file", "delete_file"]
