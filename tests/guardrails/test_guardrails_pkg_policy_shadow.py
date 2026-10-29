# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""P40 item 1: guardrail policy shadow mode.

A shadow rule (``GuardrailPolicy(mode="shadow")`` or ``Rule(..., enforcement="shadow")``) is
evaluated exactly as an enforced one and every decision is audited and sent as a decision
event with ``mode: "shadow"`` and ``applied: false``, but its action is never applied.

All offline: ``FakeModel`` agents, the regex pre-filters and scripted ``FakeModel`` judges.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from nexura_agents import (
    Agent,
    FakeModel,
    GuardrailTripwireTriggered,
    MemoryCheckpointStore,
    RunEventType,
    Runner,
    StreamGuard,
    ToolCall,
    tool,
)
from nexura_agents.contracts.execution import ExecutionId
from nexura_agents.guardrails import decision_record
from nexura_agents.policy import _unlock_for_tests, check_model_stages
from nexura_agents.run_context import RunContext
from nexura_agents.tools import ToolInfo
from nexura_agents.verdict_journal import GUARD_VERDICTS_KEY
from nexura_guardrails import (
    CallbackAuditSink,
    GuardrailPolicy,
    JudgeCheck,
    MemoryAuditSink,
    PolicyError,
    Rule,
    ShadowBudget,
    When,
    pii,
    topics,
)

EMAIL = "bob@example.com"
REPLY = f"Done: mail {EMAIL}"
RESULT = f"result for {EMAIL}: mail {EMAIL}"
ALLOW = '{"decision": "allow", "categories": [], "score": 0.0, "reason": "ok"}'
BLOCK = '{"decision": "block", "categories": ["fraud"], "score": 0.9, "reason": "fraud"}'
T = RunEventType


@pytest.fixture(autouse=True)
def _no_default_policy() -> Iterator[None]:
    _unlock_for_tests()
    yield
    _unlock_for_tests()


def rctx(agent: str) -> RunContext[Any]:
    return RunContext(deps=None, run_id="r", agent_name=agent)


def make_lookup(ran: list[str]) -> Any:
    @tool
    def lookup(q: str) -> str:
        """Look something up."""
        ran.append(q)
        return f"result for {q}: mail {EMAIL}"

    return lookup


def agent_for(ran: list[str], **kwargs: Any) -> Agent:
    model = kwargs.pop(
        "model",
        FakeModel(responses=[[ToolCall(id="c1", name="lookup", arguments={"q": EMAIL})], REPLY]),
    )
    return Agent(name="A", instructions="Help.", model=model, tools=[make_lookup(ran)], **kwargs)


def rule_for(stage: str, action: str, **kwargs: Any) -> Rule:
    if action == "scrub":
        return Rule("r", When(stage=stage), check="redact", action="scrub", **kwargs)
    if action == "require_approval":
        return Rule("r", When(stage=stage), action="require_approval", **kwargs)
    return Rule("r", When(stage=stage), check="pii", action=action, **kwargs)  # type: ignore[arg-type]


def policy_for(
    stage: str, action: str, *, mode: str = "shadow", sink: Any = None, **kwargs: Any
) -> GuardrailPolicy:
    return GuardrailPolicy(
        name="pilot",
        mode=mode,  # type: ignore[arg-type]
        checks={"pii": pii(), "redact": pii(action="redact", name="redact")},
        rules=[rule_for(stage, action)],
        audit=sink if sink is not None else MemoryAuditSink(),
        **kwargs,
    )


async def drive(
    policy: GuardrailPolicy, agent: Agent, prompt: str = f"hello {EMAIL}", **kwargs: Any
) -> tuple[list[Any], BaseException | None]:
    events: list[Any] = []
    try:
        async for event in Runner(hooks=[policy]).stream_async(agent, prompt, **kwargs):
            events.append(event)
    except Exception as exc:
        return events, exc
    return events, None


def completed(events: list[Any]) -> dict[str, Any] | None:
    done = [e for e in events if e.type == T.RUN_COMPLETED]
    return done[0].data if done else None


def tool_results(data: dict[str, Any]) -> list[str]:
    return [m.content for m in data["messages"] if m.role == "tool"]


def decisions(events: list[Any]) -> list[dict[str, Any]]:
    return [dict(e.data) for e in events if e.type == T.GUARDRAIL_DECISION]


STAGE_ACTIONS = [
    ("input", "block"),
    ("input", "fail"),
    ("model_input", "block"),
    ("model_input", "fail"),
    ("model_output", "block"),
    ("model_output", "scrub"),
    ("model_output", "fail"),
    ("tool_input", "block"),
    ("tool_input", "require_approval"),
    ("tool_input", "fail"),
    ("tool_output", "block"),
    ("tool_output", "scrub"),
    ("tool_output", "fail"),
    ("output", "block"),
    ("output", "scrub"),
    ("output", "fail"),
]


# ---------------------------------------------------------------- every action in shadow


@pytest.mark.parametrize(("stage", "action"), STAGE_ACTIONS)
async def test_shadow_never_applies_the_action_but_audits_it(stage: str, action: str) -> None:
    ran: list[str] = []
    sink = MemoryAuditSink()
    policy = policy_for(stage, action, sink=sink)
    events, error = await drive(policy, agent_for(ran))
    assert error is None
    data = completed(events)
    assert data is not None  # not tripped, failed or paused
    assert data["output"] == REPLY  # not scrubbed or replaced
    assert ran == [EMAIL]  # the tool ran: not blocked, not sent for approval
    assert tool_results(data) == [RESULT]  # the result was neither withheld nor redacted
    assert not [e for e in events if e.type in (T.RUN_PAUSED, T.GUARDRAIL_TRIPPED)]
    # Audited with the would-be action, mode shadow and applied false.
    found = [r for r in sink.records if r.stage == stage]
    assert found, sink.records
    assert all(r.mode == "shadow" and r.applied is False for r in sink.records)
    assert {r.action for r in found} == {action}
    assert found[0].rule_id == "r" and found[0].policy == "pilot"
    record = found[0].to_dict()
    assert record["mode"] == "shadow" and record["applied"] is False
    # And sent as a GUARDRAIL_DECISION run event with the same fields.
    sent = [d for d in decisions(events) if d["stage"] == stage and d.get("mode") == "shadow"]
    assert sent, decisions(events)
    assert sent[0]["action"] == action and sent[0]["applied"] is False
    assert sent[0]["rule"] == "r" and sent[0]["guardrail"] == "policy:pilot"


@pytest.mark.parametrize(("stage", "action"), STAGE_ACTIONS)
async def test_the_same_rule_enforced_still_acts(stage: str, action: str) -> None:
    """Enforce is unchanged: the same rules act, audited with mode enforce, applied true."""
    ran: list[str] = []
    sink = MemoryAuditSink()
    policy = policy_for(stage, action, mode="enforce", sink=sink)
    events, error = await drive(policy, agent_for(ran))
    data = completed(events)
    acted = (
        isinstance(error, GuardrailTripwireTriggered)
        or any(e.type == T.RUN_PAUSED for e in events)
        or data is None
        or data["output"] != REPLY
        or ran != [EMAIL]
        or tool_results(data) != [RESULT]
    )
    assert acted, (stage, action)
    found = [r for r in sink.records if r.stage == stage]
    if action != "require_approval":  # an approval pause is not an audit record
        assert found and all(r.mode == "enforce" and r.applied is True for r in found)
    assert not [r for r in sink.records if r.mode == "shadow"]


async def test_an_enforce_policy_without_shadow_rules_reports_no_shadow() -> None:
    policy = policy_for("tool_input", "block", mode="enforce")
    assert policy.mode == "enforce" and policy.shadow_rule_count == 0
    assert policy.rule_mode(policy.rules[0]) == "enforce"
    ctx: RunContext[Any] = rctx("A")
    outcome = await policy.evaluate(
        ctx, "tool_input", json.dumps({"q": EMAIL}), tool=ToolInfo(name="lookup")
    )
    assert outcome.action == "block" and outcome.applied and outcome.shadow is None


# ---------------------------------------------------------------- mixed policies


def mixed(sink: Any, **kwargs: Any) -> GuardrailPolicy:
    return GuardrailPolicy(
        name="mixed",
        checks={"pii": pii(), "deny": topics(deny=["forbidden"], name="deny")},
        rules=[
            Rule("no-shell", When(stage="tool_input", tools="shell"), action="block"),
            Rule(
                "watch-lookups",
                When(stage="tool_input", tools="lookup"),
                action="block",
                enforcement="shadow",
            ),
            Rule(
                "pii-results",
                When(stage="tool_output"),
                check="pii",
                action="block",
                enforcement="shadow",
            ),
        ],
        audit=sink,
        **kwargs,
    )


async def test_mixed_policy_enforces_enforce_rules_and_only_reports_shadow_ones() -> None:
    ran: list[str] = []
    shell_ran: list[str] = []

    @tool
    def shell(command: str) -> str:
        """Run a command."""
        shell_ran.append(command)
        return "done"

    sink = MemoryAuditSink()
    model = FakeModel(
        responses=[
            [
                ToolCall(id="c1", name="lookup", arguments={"q": EMAIL}),
                ToolCall(id="c2", name="shell", arguments={"command": "ls"}),
            ],
            "ok",
        ]
    )
    agent = Agent(name="A", instructions="x", model=model, tools=[make_lookup(ran), shell])
    events, error = await drive(mixed(sink), agent)
    assert error is None
    data = completed(events)
    assert data is not None
    assert ran == [EMAIL] and shell_ran == []
    results = tool_results(data)
    assert results[0] == RESULT  # the shadow pii rule did not withhold it
    assert results[1].startswith("Tool call blocked by policy rule 'no-shell'")
    by_rule = {r.rule_id: r for r in sink.records}
    assert by_rule["no-shell"].mode == "enforce" and by_rule["no-shell"].applied is True
    assert by_rule["watch-lookups"].mode == "shadow" and by_rule["watch-lookups"].applied is False
    assert by_rule["watch-lookups"].action == "block"
    assert by_rule["pii-results"].mode == "shadow" and by_rule["pii-results"].action == "block"


async def test_policy_shadow_with_a_rule_that_stays_enforced() -> None:
    """A rule's own enforcement wins over the policy's mode, both ways."""
    sink = MemoryAuditSink()
    policy = GuardrailPolicy(
        mode="shadow",
        rules=[
            Rule(
                "hard-stop",
                When(stage="tool_input", tools="shell"),
                action="block",
                enforcement="enforce",
            ),
            Rule("observe", When(stage="tool_input", tools="lookup"), action="block"),
        ],
        audit=sink,
    )
    assert [policy.rule_mode(r) for r in policy.rules] == ["enforce", "shadow"]
    assert policy.shadow_rule_count == 1
    ctx: RunContext[Any] = rctx("A")
    shell = await policy.evaluate(ctx, "tool_input", "{}", tool=ToolInfo(name="shell"))
    lookup = await policy.evaluate(ctx, "tool_input", "{}", tool=ToolInfo(name="lookup"))
    assert shell.action == "block" and shell.mode == "enforce" and shell.shadow is not None
    assert shell.shadow.action == "allow"
    assert lookup.action == "allow" and lookup.shadow is not None
    assert lookup.shadow.action == "block" and lookup.shadow.applied is False


async def test_severity_is_computed_over_enforce_rules_only() -> None:
    """A shadow fail never outranks an enforced scrub, and is reported alongside it."""
    policy = GuardrailPolicy(
        checks={"redact": pii(action="redact", name="redact"), "pii": pii()},
        rules=[
            Rule("scrub", When(stage="tool_output"), check="redact", action="scrub"),
            Rule(
                "would-fail",
                When(stage="tool_output"),
                check="pii",
                action="fail",
                enforcement="shadow",
            ),
        ],
    )
    ctx: RunContext[Any] = rctx("A")
    outcome = await policy.evaluate(ctx, "tool_output", RESULT, tool=ToolInfo(name="lookup"))
    assert outcome.action == "scrub" and outcome.replacement is not None
    assert EMAIL not in outcome.replacement
    assert outcome.shadow is not None and outcome.shadow.action == "fail"
    assert outcome.shadow.rule == "would-fail" and not outcome.shadow.applied


async def test_shadow_rules_never_weaken_enforced_ones() -> None:
    """A shadow allow does not admit a call under an enforced allowlist, and a shadow
    check-less block does not spare a call its enforced approval."""
    policy = GuardrailPolicy(
        default_tool_action="block",
        rules=[
            Rule(
                "maybe-shell",
                When(stage="tool_input", tools="shell"),
                action="allow",
                enforcement="shadow",
            ),
            Rule("reads", When(stage="tool_input", tools="pay"), action="allow"),
            Rule("approve-pay", When(stage="tool_input", tools="pay"), action="require_approval"),
            Rule(
                "would-refuse-pay",
                When(stage="tool_input", tools="pay"),
                action="block",
                enforcement="shadow",
            ),
        ],
    )
    ctx: RunContext[Any] = rctx("A")
    shell = await policy.evaluate(ctx, "tool_input", "{}", tool=ToolInfo(name="shell"))
    assert shell.action == "block" and shell.rule == "default_tool_action"
    # A shadow block on pay would skip the approval if it counted: it does not.
    assert policy.call_needs_approval(ctx, ToolInfo(name="pay"), {"amount": 9}) is True
    pay = await policy.evaluate(ctx, "tool_input", "{}", tool=ToolInfo(name="pay"))
    assert pay.action == "allow" and pay.shadow is not None and pay.shadow.action == "block"


def test_shadow_rules_neither_hold_streams_nor_refuse_passthrough_or_coordinators() -> None:
    shadow_only = GuardrailPolicy(
        mode="shadow",
        checks={"pii": pii()},
        rules=[Rule("r", When(stage=["model_output", "output", "input"]), check="pii")],
    )
    agent = Agent(
        name="A",
        instructions="x",
        model=FakeModel(responses=["hi"]),
        stream_guard=StreamGuard(mode="passthrough"),
    )
    assert shadow_only.text_rules("A") == []
    assert shadow_only.requires_model_stages() is False
    assert shadow_only.output_guardrails(rctx("A"), agent) == ()
    check_model_stages(False, shadow_only)  # a coordinator is not refused
    enforced = GuardrailPolicy(
        checks={"pii": pii()},
        rules=[Rule("r", When(stage=["model_output", "output", "input"]), check="pii")],
    )
    assert enforced.requires_model_stages() is True and enforced.text_rules("A")
    from nexura_agents.errors import SDKValidationError

    with pytest.raises(SDKValidationError):
        check_model_stages(False, enforced)


# ---------------------------------------------------------------- streaming


def streamed_agent(stream_guard: StreamGuard | None = None) -> Agent:
    reply = "Opening words here. " * 3 + f"Write to {EMAIL} today. " + "Closing. " * 4
    kwargs: dict[str, Any] = {"stream_guard": stream_guard} if stream_guard else {}
    return Agent(
        name="A",
        instructions="i",
        model=FakeModel([reply], stream=True, chunk_size=4),
        **kwargs,
    )


@pytest.mark.parametrize("action", ["scrub", "block", "fail"])
async def test_shadow_output_rules_do_not_hold_a_stream_but_are_audited(action: str) -> None:
    sink = MemoryAuditSink()
    policy = GuardrailPolicy(
        mode="shadow",
        checks={"pii": pii(), "redact": pii(action="redact", name="redact")},
        rules=[
            Rule(
                "r",
                When(stage=["model_output", "output"]),
                check="redact" if action == "scrub" else "pii",
                action=action,  # type: ignore[arg-type]
            ),
        ],
        audit=sink,
    )
    for guard in (None, StreamGuard(mode="passthrough")):
        sink.records.clear()
        events, error = await drive(policy, streamed_agent(guard), "go", stream_text=True)
        assert error is None
        deltas = [e for e in events if e.type == T.TEXT_DELTA]
        assert len(deltas) > 3  # streamed as generated, not held
        assert not any(d.data.get("checked") for d in deltas)  # not chunk-checked windows
        shown = "".join(d.data["text"] for d in deltas)
        data = completed(events)
        assert data is not None and EMAIL in shown and data["output"] == shown
        stages = {(r.stage, r.action, r.mode, r.applied) for r in sink.records}
        assert ("model_output", action, "shadow", False) in stages
        assert ("output", action, "shadow", False) in stages
        sent = [d for d in decisions(events) if d.get("mode") == "shadow"]
        assert {d["stage"] for d in sent} == {"model_output", "output"}


async def test_the_same_output_rule_enforced_holds_the_stream() -> None:
    policy = GuardrailPolicy(
        checks={"pii": pii()},
        rules=[Rule("r", When(stage="output"), check="pii", action="block")],
    )
    events, error = await drive(policy, streamed_agent(), "go", stream_text=True)
    assert isinstance(error, GuardrailTripwireTriggered)
    assert not any(EMAIL in e.data.get("text", "") for e in events if e.type == T.TEXT_DELTA)


# ---------------------------------------------------------------- judges and errors


class CountingJudge(FakeModel):
    """A scripted judge model that counts its calls."""

    def __init__(self, replies: list[str]) -> None:
        super().__init__(responses=replies)
        self.count = 0

    async def generate(self, *args: Any, **kwargs: Any) -> Any:
        self.count += 1
        return await super().generate(*args, **kwargs)


class BrokenJudge(FakeModel):
    """A judge model whose provider call fails."""

    async def generate(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("provider down")


@pytest.mark.parametrize("on_error", ["block", "allow"])
async def test_a_judge_error_in_shadow_never_fails_the_run_and_is_audited(on_error: str) -> None:
    ran: list[str] = []
    sink = MemoryAuditSink()
    policy = GuardrailPolicy(
        mode="shadow",
        on_error=on_error,  # type: ignore[arg-type]
        checks={"judge": JudgeCheck(BrokenJudge(responses=[]), "Block fraud.", cache=False)},
        rules=[
            Rule("judge-in", When(stage="input"), check="judge", action="fail"),
            Rule("judge-calls", When(stage="tool_input"), check="judge", action="block"),
        ],
        audit=sink,
    )
    events, error = await drive(policy, agent_for(ran))
    assert error is None
    data = completed(events)
    assert data is not None and data["output"] == REPLY and ran == [EMAIL]
    by_stage = {r.stage: r for r in sink.records}
    assert set(by_stage) == {"input", "tool_input"}
    for record in by_stage.values():
        assert record.mode == "shadow" and record.applied is False
        assert record.decision == "error" and record.error_type is not None
    # on_error block: the would-be action; on_error allow: an allow that records the error.
    expected = {"block": ("fail", "block"), "allow": ("allow", "allow")}[on_error]
    assert (by_stage["input"].action, by_stage["tool_input"].action) == expected
    sent = [d for d in decisions(events) if d.get("mode") == "shadow"]
    assert {d["stage"] for d in sent} == {"input", "tool_input"}
    assert all(d.get("error_type") and d["decision"] == "error" for d in sent)


async def test_a_judge_error_enforced_still_fails_closed() -> None:
    ran: list[str] = []
    policy = GuardrailPolicy(
        checks={"judge": JudgeCheck(BrokenJudge(responses=[]), "Block fraud.", cache=False)},
        rules=[Rule("judge-calls", When(stage="tool_input"), check="judge", action="block")],
    )
    events, error = await drive(policy, agent_for(ran))
    data = completed(events)
    assert error is None and data is not None and ran == []
    assert tool_results(data)[0].startswith("Tool call blocked by policy rule 'judge-calls'")


class Crash(BaseException):
    """Simulated process death."""


async def test_shadow_judge_verdicts_are_journaled_and_not_asked_again() -> None:
    store, ran = MemoryCheckpointStore(), []
    judge = CountingJudge([BLOCK] * 3)
    armed = {"on": True}

    def crash_once(record: Any) -> None:
        if armed["on"] and record.stage == "tool_input":
            armed["on"] = False
            raise Crash

    policy = GuardrailPolicy(
        name="journaled",
        mode="shadow",
        checks={"judge": JudgeCheck(judge, "Block fraud.", cache=False)},
        rules=[Rule("judge-sends", When(stage="tool_input"), check="judge", action="block")],
        audit=CallbackAuditSink(crash_once),
    )
    runner = Runner(checkpoint_store=store, hooks=[policy])
    with pytest.raises(Crash):
        await runner.run_async(agent_for(ran), "pay")
    assert judge.count == 1 and ran == []
    (run_id,) = await store.list_incomplete()
    journal = await (await store.open_execution_store()).get(ExecutionId("default", run_id))
    (stored,) = journal.run.state[GUARD_VERDICTS_KEY].values()
    assert stored["decision"] == "block" and stored["check"] == "judge"
    result = await runner.recover_async(agent_for(ran, model=FakeModel(responses=[REPLY])), run_id)
    assert result.status == "completed" and ran == [EMAIL]  # shadow: the block was not applied
    assert judge.count == 1  # the journaled verdict was reused


async def test_an_audit_failure_never_fails_a_shadow_decision() -> None:
    ran: list[str] = []

    def broken(record: Any) -> None:
        raise OSError("disk full")

    policy = policy_for("tool_input", "block", sink=CallbackAuditSink(broken), audit_required=True)
    events, error = await drive(policy, agent_for(ran))
    assert error is None and completed(events) is not None and ran == [EMAIL]
    assert policy.audit_errors >= 1


async def test_a_shadow_stage_timeout_is_reported_not_applied() -> None:
    import asyncio

    from nexura_guardrails import Verdict

    class Slow:
        name = "slow"
        stages = frozenset({"input", "output", "tool_input", "tool_output"})
        deterministic = True

        async def __call__(self, ctx: Any, item: Any) -> Verdict:
            await asyncio.sleep(5)
            return Verdict(decision="allow")

    sink = MemoryAuditSink()
    policy = GuardrailPolicy(
        mode="shadow",
        stage_timeout=0.05,
        checks={"slow": Slow()},  # type: ignore[dict-item]
        rules=[Rule("slow-calls", When(stage="tool_input"), check="slow", action="block")],
        audit=sink,
    )
    ran: list[str] = []
    events, error = await drive(policy, agent_for(ran))
    assert error is None and completed(events) is not None and ran == [EMAIL]
    (record,) = sink.records
    assert record.rule_id == "stage_timeout" and record.error_type == "TimeoutError"
    assert record.mode == "shadow" and record.applied is False


# ---------------------------------------------------------------- allowlist mode


async def test_shadow_allowlist_reports_would_be_blocks_without_blocking() -> None:
    shell_ran: list[str] = []

    @tool
    def shell(command: str) -> str:
        """Run a command."""
        shell_ran.append(command)
        return "done"

    ran: list[str] = []
    sink = MemoryAuditSink()
    policy = GuardrailPolicy(
        mode="shadow",
        default_tool_action="block",
        rules=[Rule("reads", When(stage="tool_input", tools="lookup"), action="allow")],
        audit=sink,
    )
    model = FakeModel(
        responses=[
            [
                ToolCall(id="c1", name="lookup", arguments={"q": EMAIL}),
                ToolCall(id="c2", name="shell", arguments={"command": "rm -fr /tmp/x"}),
            ],
            "ok",
        ]
    )
    agent = Agent(name="A", instructions="x", model=model, tools=[make_lookup(ran), shell])
    events, error = await drive(policy, agent)
    assert error is None and completed(events) is not None
    assert ran == [EMAIL] and shell_ran == ["rm -fr /tmp/x"]
    (record,) = sink.records  # lookup is on the allowlist: no record
    assert record.tool == "shell" and record.rule_id == "default_tool_action"
    assert record.action == "block" and record.mode == "shadow" and record.applied is False
    assert policy.call_needs_approval(rctx("A"), ToolInfo(name="shell"), {}) is False


async def test_enforced_allowlist_with_a_shadow_policy_rule_still_blocks() -> None:
    """The allowlist follows the policy's mode: enforced here, so it blocks."""
    shell_ran: list[str] = []

    @tool
    def shell(command: str) -> str:
        """Run a command."""
        shell_ran.append(command)
        return "done"

    policy = GuardrailPolicy(
        default_tool_action="block",
        rules=[
            Rule(
                "watch",
                When(stage="tool_input", tools="shell"),
                action="allow",
                enforcement="shadow",
            )
        ],
    )
    model = FakeModel(
        responses=[[ToolCall(id="c1", name="shell", arguments={"command": "ls"})], "ok"]
    )
    agent = Agent(name="A", instructions="x", model=model, tools=[shell])
    events, error = await drive(policy, agent)
    data = completed(events)
    assert error is None and data is not None and shell_ran == []
    assert "not on the tool allowlist" in tool_results(data)[0]


# ---------------------------------------------------------------- workflow module nodes


async def test_check_call_returns_the_enforced_outcome_and_reports_shadow() -> None:
    sink = MemoryAuditSink()
    policy = GuardrailPolicy(
        mode="shadow",
        rules=[
            Rule(
                "big-payments",
                When(stage="tool_input", args={"$.amount": {"gt": 500}}),
                action="block",
            ),
            Rule("approve", When(stage="tool_input", tools="pay"), action="require_approval"),
        ],
        audit=sink,
    )
    ctx: RunContext[Any] = rctx("wf")
    info = ToolInfo(name="pay", origin="workflow:pay")
    outcome = await policy.check_call(ctx, "tool_input", {"amount": 900}, tool=info)
    assert outcome.action == "allow" and outcome.shadow is not None
    assert outcome.shadow.action == "block" and outcome.shadow.rule == "big-payments"
    assert policy.call_needs_approval(ctx, info, {"amount": 900}) is False
    small = await policy.check_call(ctx, "tool_input", {"amount": 10}, tool=info)
    assert small.shadow is not None and small.shadow.action == "require_approval"
    assert [(r.action, r.mode, r.applied) for r in sink.records] == [
        ("block", "shadow", False),
        ("require_approval", "shadow", False),
    ]


# ---------------------------------------------------------------- configuration


SHADOW_YAML = """
version: 1
name: pilot
mode: shadow
checks:
  pii: {type: pii}
rules:
  - {id: big, when: {stage: tool_input, args: {$.amount: {gt: 500}}}, action: block}
  - {id: hard, when: {stage: tool_input, tools: [shell]}, action: block, enforcement: enforce}
  - {id: watch, when: {stage: output}, check: pii, action: block, enforcement: shadow}
"""


def test_loader_reads_policy_and_rule_modes() -> None:
    policy = GuardrailPolicy.from_yaml(SHADOW_YAML)
    assert policy.mode == "shadow" and policy.shadow_rule_count == 2
    assert [r.enforcement for r in policy.rules] == [None, "enforce", "shadow"]
    assert [policy.rule_mode(r) for r in policy.rules] == ["shadow", "enforce", "shadow"]
    described = policy.describe()
    assert described["mode"] == "shadow" and described["shadow_rules"] == 2
    assert [r["enforcement"] for r in described["rules"]] == ["shadow", "enforce", "shadow"]
    as_json = GuardrailPolicy.from_json(
        json.dumps(
            {
                "version": 1,
                "mode": "shadow",
                "rules": [{"id": "r", "when": {"stage": "tool_input"}, "enforcement": "enforce"}],
            }
        )
    )
    assert as_json.mode == "shadow" and as_json.rule_mode(as_json.rules[0]) == "enforce"
    default = GuardrailPolicy.from_json('{"version": 1, "rules": []}')
    assert default.mode == "enforce"


def test_loader_and_python_api_agree() -> None:
    built = GuardrailPolicy.build(
        mode="shadow",
        rules=[
            {
                "id": "r",
                "when": {"stage": "tool_input"},
                "action": "block",
                "enforcement": "enforce",
            }
        ],
    )
    assert built.mode == "shadow" and built.rules[0].enforcement == "enforce"
    assert Rule("r", enforcement="shadow").describe()["enforcement"] == "shadow"
    assert "enforcement" not in Rule("r").describe()


@pytest.mark.parametrize(
    ("document", "path"),
    [
        ({"version": 1, "mode": "observe"}, "mode"),
        ({"version": 1, "mode": "SHADOW"}, "mode"),
        ({"version": 1, "mode": True}, "mode"),
        ({"version": 1, "mode": ["shadow"]}, "mode"),
        ({"version": 1, "rules": [{"id": "r", "enforcement": "dry-run"}]}, "rules[0]"),
        ({"version": 1, "rules": [{"id": "r", "enforcement": False}]}, "rules[0]"),
        ({"version": 1, "rules": [{"id": "r", "mode": "shadow"}]}, "rules[0]"),
        ({"version": 1, "rules": [{"id": "r", "shadow": True}]}, "rules[0]"),
    ],
)
def test_loader_refuses_unknown_modes(document: dict[str, Any], path: str) -> None:
    with pytest.raises(PolicyError) as err:
        GuardrailPolicy.from_json(json.dumps(document))
    assert err.value.path.startswith(path)


def test_a_rule_mode_of_shadow_points_at_enforcement() -> None:
    with pytest.raises(PolicyError, match="enforcement"):
        Rule("r", mode="shadow")  # type: ignore[arg-type]
    with pytest.raises(PolicyError, match="unknown enforcement"):
        Rule("r", enforcement="report")  # type: ignore[arg-type]
    with pytest.raises(PolicyError, match="unknown mode"):
        GuardrailPolicy(mode="report")  # type: ignore[arg-type]
    with pytest.raises(PolicyError, match="unknown mode"):
        GuardrailPolicy(mode=None)  # type: ignore[arg-type]


def test_the_digest_covers_the_mode_and_is_unchanged_for_enforce() -> None:
    rules = [Rule("r", When(stage="tool_input"), action="block")]
    default = GuardrailPolicy(rules=rules)
    assert GuardrailPolicy(rules=rules, mode="enforce").digest == default.digest
    assert GuardrailPolicy(rules=rules, mode="shadow").digest != default.digest
    shadow_rule = [Rule("r", When(stage="tool_input"), action="block", enforcement="shadow")]
    assert GuardrailPolicy(rules=shadow_rule).digest != default.digest


# ---------------------------------------------------------------- events and traces


def test_decision_records_carry_mode_and_applied_only_from_the_vocabulary() -> None:
    record = decision_record(
        "tool_input", "policy:p", "block", info={"mode": "shadow", "applied": False}
    )
    assert record["mode"] == "shadow" and record["applied"] is False
    odd = decision_record(
        "tool_input", "policy:p", "block", info={"mode": "bob@example.com", "applied": "no"}
    )
    assert "mode" not in odd and "applied" not in odd


async def test_shadow_decisions_are_trace_events_with_mode_and_applied() -> None:
    from nexura_agents import tracing as nexura_tracing
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    nexura_tracing.instrument(tracer_provider=provider)
    try:
        ran: list[str] = []
        events, error = await drive(policy_for("tool_input", "block"), agent_for(ran))
        assert error is None and ran == [EMAIL]
    finally:
        nexura_tracing.uninstrument()
    found = [
        dict(e.attributes or {})
        for span in memory.get_finished_spans()
        for e in span.events
        if e.name == "nexura.guardrail.decision"
    ]
    shadow = [a for a in found if a.get("nexura.guardrail.mode") == "shadow"]
    assert shadow, found
    assert shadow[0]["nexura.guardrail.applied"] is False
    assert shadow[0]["nexura.guardrail.action"] == "block"


# ---------------------------------------------------------------- round 2: M1 shadow usage


class UsageJudge:
    """A model-style check (not deterministic) that reports usage; verdict scripted."""

    name = "judge"
    stages = frozenset({"input", "output", "tool_input", "tool_output"})
    deterministic = False

    def __init__(self, tokens: int, decision: str = "block") -> None:
        self.tokens = tokens
        self.decision = decision
        self.calls = 0
        self.budgets: list[Any] = []

    async def __call__(self, ctx: Any, item: Any) -> Any:
        from nexura_agents.types import Usage
        from nexura_agents.usage import admission
        from nexura_guardrails import Verdict

        self.calls += 1
        self.budgets.append(admission.active_budget.get())
        usage = Usage(prompt_tokens=self.tokens, completion_tokens=0, total_tokens=self.tokens)
        return Verdict(self.decision, categories=("fraud",), check="judge", usage=usage)  # type: ignore[arg-type]


def small_usage_agent(ran: list[str]) -> Agent:
    from nexura_agents.types import Usage

    usage = Usage(prompt_tokens=10, completion_tokens=10, total_tokens=20)
    model = FakeModel(
        responses=[[ToolCall(id="c1", name="lookup", arguments={"q": "x"})], "fine"],
        default_usage=usage,
    )
    return Agent(name="A", instructions="x", model=model, tools=[make_lookup(ran)])


async def test_shadow_judge_usage_never_counts_toward_run_limits() -> None:
    from nexura_agents.limits import UsageLimits

    judge = UsageJudge(tokens=5000)
    sink = MemoryAuditSink()
    policy = GuardrailPolicy(
        mode="shadow",
        checks={"judge": judge},  # type: ignore[dict-item]
        rules=[
            Rule("in", When(stage="input"), check="judge", action="block"),
            Rule("calls", When(stage="tool_input"), check="judge", action="block"),
        ],
        audit=sink,
    )
    ran: list[str] = []
    events: list[Any] = []
    runner = Runner(hooks=[policy], usage_limits=UsageLimits(total_tokens_limit=1000))
    async for event in runner.stream_async(small_usage_agent(ran), "hi"):
        events.append(event)
    data = completed(events)
    assert data is not None and data["output"] == "fine" and ran == ["x"]
    assert judge.calls == 2
    assert data["usage"].total_tokens == 40  # the agent's two model calls only
    run_id = events[0].run_id
    assert policy.shadow_usage(run_id) == {
        "calls": 2,
        "tokens": 10000,
        "skipped": 0,
        "estimated_cost": None,
    }
    # Reported, labelled shadow: audit records and decision events carry the tokens.
    assert [(r.stage, r.tokens, r.mode) for r in sink.records] == [
        ("input", 5000, "shadow"),
        ("tool_input", 5000, "shadow"),
    ]
    sent = [d for d in decisions(events) if d.get("mode") == "shadow"]
    assert [d["tokens"] for d in sent] == [5000, 5000]


async def test_enforced_judge_usage_still_counts_toward_run_limits() -> None:
    from nexura_agents.errors import UsageLimitExceededError
    from nexura_agents.limits import UsageLimits

    judge = UsageJudge(tokens=5000, decision="allow")
    policy = GuardrailPolicy(
        checks={"judge": judge},  # type: ignore[dict-item]
        rules=[Rule("in", When(stage="input"), check="judge", action="block")],
    )
    with pytest.raises(UsageLimitExceededError, match="total_tokens_limit"):
        await Runner(hooks=[policy], usage_limits=UsageLimits(total_tokens_limit=1000)).run_async(
            small_usage_agent([]), "hi"
        )
    result = await Runner(hooks=[policy]).run_async(small_usage_agent([]), "hi")
    assert result.usage.total_tokens == 5040  # the judge's usage is part of the run's
    assert policy.shadow_usage(result.run_id)["calls"] == 0


async def test_shadow_judges_run_outside_the_run_budget_admission() -> None:
    from nexura_agents.usage import admission

    shadow_judge, enforced_judge = UsageJudge(tokens=1, decision="allow"), UsageJudge(tokens=1)
    enforced_judge.decision = "allow"
    policy = GuardrailPolicy(
        checks={"s": shadow_judge, "e": enforced_judge},  # type: ignore[dict-item]
        rules=[
            Rule("shadow-in", When(stage="input"), check="s", action="block", enforcement="shadow"),
            Rule("enforced-in", When(stage="input"), check="e", action="block"),
        ],
    )
    budget = object()  # a stand-in run budget: only its presence is observed
    token = admission.active_budget.set(budget)
    try:
        outcome = await policy.evaluate(rctx("A"), "input", "hi")
    finally:
        admission.active_budget.reset(token)
    assert outcome.action == "allow" and outcome.shadow is not None
    assert admission.active_budget.get() is None
    assert enforced_judge.budgets == [budget]  # enforced checks are admitted as before
    assert shadow_judge.budgets == [None]  # shadow checks are kept out of the run's budget


async def test_without_a_shadow_budget_every_shadow_judge_runs() -> None:
    judge = UsageJudge(tokens=10**6)
    policy = GuardrailPolicy(
        mode="shadow",
        checks={"judge": judge},  # type: ignore[dict-item]
        rules=[Rule("calls", When(stage=["input", "tool_input", "output"]), check="judge")],
    )
    result = await Runner(hooks=[policy]).run_async(small_usage_agent([]), "hi")
    assert result.status == "completed" and judge.calls == 3
    assert policy.shadow_usage(result.run_id)["tokens"] == 3 * 10**6


async def test_a_small_shadow_budget_skips_further_judges_and_audits_them() -> None:
    judge = UsageJudge(tokens=5000)
    sink = MemoryAuditSink()
    policy = GuardrailPolicy(
        mode="shadow",
        shadow_budget={"max_tokens": 1000, "max_cost": 1.0, "cost_per_1k_tokens": 0.01},
        # pii is deterministic: free, so never skipped by the shadow budget.
        checks={"judge": judge, "pii": pii()},  # type: ignore[dict-item]
        rules=[
            Rule("in", When(stage="input"), check="judge", action="block"),
            Rule("calls", When(stage="tool_input"), check="judge", action="block"),
            Rule("pii-results", When(stage="tool_output"), check="pii", action="block"),
        ],
        audit=sink,
    )
    ran: list[str] = []
    events, error = await drive(policy, agent_for(ran))
    assert error is None and completed(events) is not None and ran == [EMAIL]
    assert judge.calls == 1  # the input judge ran (5000 tokens); the next one was skipped
    by_rule = {r.rule_id: r for r in sink.records}
    assert by_rule["in"].action == "block" and by_rule["in"].tokens == 5000
    skipped = by_rule["calls"]
    assert skipped.skipped == "shadow_budget" and skipped.action == "allow"
    assert skipped.mode == "shadow" and skipped.applied is False and skipped.tokens is None
    assert by_rule["pii-results"].action == "block"  # the regex check still ran
    sent = [d for d in decisions(events) if d.get("skipped")]
    assert [(d["rule"], d["skipped"]) for d in sent] == [("calls", "shadow_budget")]
    run_id = events[0].run_id
    assert policy.shadow_usage(run_id) == {
        "calls": 1,
        "tokens": 5000,
        "skipped": 1,
        "estimated_cost": 0.05,
    }


async def test_a_shadow_budget_skips_a_judge_whose_text_alone_would_exceed_it() -> None:
    judge = UsageJudge(tokens=1)
    policy = GuardrailPolicy(
        mode="shadow",
        shadow_budget=ShadowBudget(max_tokens=5),
        checks={"judge": judge},  # type: ignore[dict-item]
        rules=[Rule("in", When(stage="input"), check="judge", action="block")],
        audit=MemoryAuditSink(),
    )
    result = await Runner(hooks=[policy]).run_async(small_usage_agent([]), "a long message " * 20)
    assert result.status == "completed" and judge.calls == 0
    assert policy.shadow_usage(result.run_id)["skipped"] == 1


async def test_a_shadow_budget_never_skips_enforced_judges() -> None:
    judge = UsageJudge(tokens=5000, decision="allow")
    policy = GuardrailPolicy(
        shadow_budget={"max_tokens": 1},
        checks={"judge": judge},  # type: ignore[dict-item]
        rules=[Rule("calls", When(stage=["input", "tool_input"]), check="judge")],
    )
    result = await Runner(hooks=[policy]).run_async(small_usage_agent([]), "hi")
    assert result.status == "completed" and judge.calls == 2


def test_shadow_budget_is_loaded_and_checked() -> None:
    policy = GuardrailPolicy.from_yaml(
        "version: 1\nmode: shadow\n"
        "shadow_budget: {max_tokens: 200000, max_cost: 2.5, cost_per_1k_tokens: 0.002}\n"
    )
    assert policy.shadow_budget == ShadowBudget(
        max_tokens=200000, max_cost=2.5, cost_per_1k_tokens=0.002
    )
    assert policy.describe()["shadow_budget"] == {
        "max_tokens": 200000,
        "max_cost": 2.5,
        "cost_per_1k_tokens": 0.002,
    }
    assert GuardrailPolicy(mode="shadow").describe()["shadow_budget"] is None
    plain = GuardrailPolicy(mode="shadow")
    assert GuardrailPolicy(mode="shadow", shadow_budget={"max_tokens": 9}).digest != plain.digest


@pytest.mark.parametrize(
    "budget",
    [
        {},
        {"max_tokens": 0},
        {"max_tokens": -5},
        {"max_tokens": 1.5},
        {"max_tokens": True},
        {"max_tokens": "100"},
        {"max_cost": 1.0},
        {"max_cost": float("nan"), "cost_per_1k_tokens": 1},
        {"max_cost": 1, "cost_per_1k_tokens": 0},
        {"max_tokens": 10, "tokens": 5},
        [100],
        "100",
    ],
)
def test_a_bad_shadow_budget_is_refused(budget: Any) -> None:
    with pytest.raises(PolicyError) as err:
        GuardrailPolicy.from_json(json.dumps({"version": 1, "shadow_budget": budget}))
    assert err.value.path.startswith("shadow_budget")


# ---------------------------------------------------------------- round 2: L1 frozen mode


def test_the_mode_is_read_only_and_rule_modes_are_fixed() -> None:
    policy = GuardrailPolicy(
        rules=[
            Rule("cap", When(stage="tool_input"), action="block"),
            Rule("watch", When(stage="tool_input"), action="block", enforcement="shadow"),
        ],
        shadow_budget={"max_tokens": 10},
    )
    digest = policy.digest
    with pytest.raises(AttributeError):
        policy.mode = "shadow"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        policy.shadow_budget = None  # type: ignore[misc]
    with pytest.raises(TypeError):
        policy._rule_modes["cap"] = "shadow"  # type: ignore[index]
    assert policy.mode == "enforce" and policy.digest == digest
    assert [policy.rule_mode(r) for r in policy.rules] == ["enforce", "shadow"]


def test_switching_mode_means_a_new_policy_which_a_locked_install_refuses() -> None:
    from nexura_agents.errors import SDKValidationError
    from nexura_agents.policy import default_hooks

    live = GuardrailPolicy(rules=[Rule("cap", When(stage="tool_input"), action="block")])
    live.install(locked=True)
    shadow = GuardrailPolicy(mode="shadow", rules=list(live.rules))
    assert shadow.digest != live.digest
    with pytest.raises(SDKValidationError):
        shadow.install()
    assert default_hooks() is live and live.mode == "enforce"
