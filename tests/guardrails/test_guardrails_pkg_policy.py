# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""P39 G6: ``GuardrailPolicy`` stages, actions, severity, allowlist mode and audit.

All offline: ``FakeModel`` agents and the regex pre-filters (a scripted ``FakeModel`` judge
where a model check is needed). The ``rm -rf`` tests document, on purpose, that a blocklist
rule is bypassed by trivial variants, and that the structural controls (an allowlist and
human approval) stop every variant.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from nexura_agents import (
    Agent,
    FakeModel,
    GuardrailTripwireTriggered,
    RunEventType,
    Runner,
    ToolCall,
    tool,
)
from nexura_agents.policy import _unlock_for_tests, default_hooks, set_default_hooks
from nexura_agents.tools import ToolInfo
from nexura_guardrails import (
    CallbackAuditSink,
    GuardrailPolicy,
    JsonlAuditSink,
    MemoryAuditSink,
    PolicyError,
    Rule,
    When,
    pii,
    topics,
)
from nexura_guardrails.policy import PolicyAuditError

SECRET = "mail bob@example.com"


@pytest.fixture(autouse=True)
def _no_default_policy() -> Any:
    _unlock_for_tests()
    yield
    _unlock_for_tests()


def calls(*items: tuple[str, str, dict[str, Any]]) -> list[ToolCall]:
    return [ToolCall(id=i, name=n, arguments=a) for i, n, a in items]


@tool
def lookup(q: str) -> str:
    """Look something up."""
    return f"result for {q}: {SECRET}"


def deny(word: str = "forbidden", name: str = "deny") -> Any:
    return topics(deny=[word], name=name)


def agent_with(responses: list[Any], tools: list[Any] | None = None) -> Agent:
    return Agent(
        name="A",
        instructions="Help.",
        model=FakeModel(responses=responses),
        tools=[lookup] if tools is None else tools,
    )


async def run_events(policy: GuardrailPolicy, agent: Agent, prompt: str = "hello") -> list[Any]:
    return [ev async for ev in Runner(hooks=[policy]).stream_async(agent, prompt)]


def completed(events: list[Any]) -> dict[str, Any]:
    (done,) = [e for e in events if e.type == RunEventType.RUN_COMPLETED]
    data: dict[str, Any] = done.data
    return data


def tool_messages(data: dict[str, Any]) -> list[str]:
    return [m.content for m in data["messages"] if m.role == "tool"]


# ---------------------------------------------------------------- policy matrix


@pytest.mark.parametrize("action", ["allow", "block", "fail"])
async def test_input_stage(action: str) -> None:
    rules = [Rule("r", When(stage="input"), action=action)]
    if action != "allow":
        rules = [Rule("r", When(stage="input"), check="deny", action=action)]
    policy = GuardrailPolicy(checks={"deny": deny()}, rules=rules)
    agent = agent_with(["fine"])
    ok = await Runner(hooks=[policy]).run_async(agent, "hello")
    assert ok.output == "fine"
    if action == "allow":
        return
    with pytest.raises(GuardrailTripwireTriggered) as err:
        await Runner(hooks=[policy]).run_async(agent_with(["never"]), "this is forbidden")
    assert err.value.stage == "input" and err.value.info["rule"] == "r"


@pytest.mark.parametrize("action", ["block", "fail"])
async def test_model_input_stage(action: str) -> None:
    policy = GuardrailPolicy(
        checks={"deny": deny()},
        rules=[Rule("r", When(stage="model_input"), check="deny", action=action)],
    )
    model = FakeModel(responses=["model answer"])
    agent = Agent(name="A", instructions="Help.", model=model)
    if action == "fail":
        with pytest.raises(GuardrailTripwireTriggered) as err:
            await Runner(hooks=[policy]).run_async(agent, "forbidden words")
        assert err.value.stage == "model input"
        return
    result = await Runner(hooks=[policy]).run_async(agent, "forbidden words")
    assert result.output == "Request blocked by policy rule 'r'."
    assert model.calls == []  # the model was never called


@pytest.mark.parametrize("action", ["block", "scrub", "fail"])
async def test_model_output_stage(action: str) -> None:
    check = pii(action="redact") if action == "scrub" else deny("bob@example.com")
    policy = GuardrailPolicy(
        checks={"c": check},
        rules=[Rule("r", When(stage="model_output"), check="c", action=action)],
    )
    agent = agent_with([f"answer: {SECRET}"], tools=[])
    if action == "fail":
        with pytest.raises(GuardrailTripwireTriggered):
            await Runner(hooks=[policy]).run_async(agent, "hi")
        return
    result = await Runner(hooks=[policy]).run_async(agent, "hi")
    if action == "scrub":
        assert result.output == "answer: mail [REDACTED:email]"
    else:
        assert result.output == "Reply withheld by policy rule 'r'."


async def test_model_output_block_drops_tool_calls() -> None:
    policy = GuardrailPolicy(
        checks={"deny": deny("rm -rf")},
        rules=[Rule("r", When(stage="model_output"), check="deny", action="block")],
    )
    ran: list[str] = []

    @tool
    def shell(command: str) -> str:
        """Run a command."""
        ran.append(command)
        return "ok"

    agent = agent_with([calls(("c1", "shell", {"command": "rm -rf /"}))], tools=[shell])
    result = await Runner(hooks=[policy]).run_async(agent, "go")
    assert ran == [] and result.output == "Reply withheld by policy rule 'r'."


@pytest.mark.parametrize("action", ["allow", "block", "require_approval", "fail"])
async def test_tool_input_stage(action: str) -> None:
    rule = Rule("r", When(stage="tool_input", tools="lookup"), action=action)
    policy = GuardrailPolicy(rules=[rule])
    agent = agent_with([calls(("c1", "lookup", {"q": "x"})), "done"])
    runner = Runner(hooks=[policy])
    if action == "fail":
        with pytest.raises(GuardrailTripwireTriggered) as err:
            await runner.run_async(agent, "go")
        assert err.value.tool == "lookup"
        return
    result = await runner.run_async(agent, "go")
    if action == "require_approval":
        assert result.status == "paused"
        assert [c.name for c in result.interruption.pending] == ["lookup"]
        return
    (message,) = tool_messages(result.__dict__ | {"messages": result.messages})
    if action == "allow":
        assert message.startswith("result for x")
    else:
        assert message == "Tool call blocked by policy rule 'r': the policy does not allow it."


@pytest.mark.parametrize("action", ["block", "scrub", "fail"])
async def test_tool_output_stage(action: str) -> None:
    check = pii(action="redact") if action == "scrub" else pii()
    policy = GuardrailPolicy(
        checks={"pii": check},
        rules=[Rule("r", When(stage="tool_output"), check="pii", action=action)],
    )
    agent = agent_with([calls(("c1", "lookup", {"q": "x"})), "done"])
    if action == "fail":
        with pytest.raises(GuardrailTripwireTriggered) as err:
            await Runner(hooks=[policy]).run_async(agent, "go")
        assert err.value.stage == "tool output"
        return
    result = await Runner(hooks=[policy]).run_async(agent, "go")
    (message,) = [m.content for m in result.messages if m.role == "tool"]
    if action == "scrub":
        assert message == "result for x: mail [REDACTED:email]"
    else:
        assert message.startswith("Tool result withheld by policy rule 'r': blocked by")
        assert "bob@example.com" not in message


@pytest.mark.parametrize("action", ["block", "scrub", "fail"])
async def test_output_stage(action: str) -> None:
    check = pii(action="redact") if action == "scrub" else pii()
    policy = GuardrailPolicy(
        checks={"pii": check},
        rules=[Rule("r", When(stage="output"), check="pii", action=action)],
    )
    agent = agent_with([f"answer {SECRET}"], tools=[])
    if action == "scrub":
        result = await Runner(hooks=[policy]).run_async(agent, "hi")
        assert result.output == "answer mail [REDACTED:email]"
        return
    with pytest.raises(GuardrailTripwireTriggered) as err:
        await Runner(hooks=[policy]).run_async(agent, "hi")
    assert err.value.stage == "output"


def shown_text(events: list[Any]) -> str:
    """What a stream consumer saw: the ``text`` of every ``TEXT_DELTA``."""
    return "".join(e.data.get("text", "") for e in events if e.type == RunEventType.TEXT_DELTA)


@pytest.mark.parametrize("stage", ["output", "model_output"])
async def test_streamed_text_is_held_for_reply_rules(stage: str) -> None:
    """Round 2 (M4, H1): a FakeModel that really streams; the consumer's text (key "text")
    never holds the e-mail, for an output rule and for a model_output-only rule."""
    policy = GuardrailPolicy(
        checks={"pii": pii(action="redact")},
        rules=[Rule("r", When(stage=stage), check="pii", action="scrub")],
    )

    def agent() -> Agent:
        model = FakeModel(responses=[f"answer {SECRET} done"], stream=True)
        return Agent(name="A", instructions="Help.", model=model)

    plain = [e async for e in Runner().stream_async(agent(), "hi", stream_text=True)]
    assert "bob@example.com" in shown_text(plain)  # control: the model really streams
    events = [e async for e in Runner(hooks=[policy]).stream_async(agent(), "hi", stream_text=True)]
    assert "bob@example.com" not in shown_text(events)
    assert completed(events)["output"] == "answer mail [REDACTED:email] done"


# ---------------------------------------------------------------- severity and matching


async def test_most_severe_action_wins_and_unconditional_rules_skip_checks() -> None:
    seen: list[str] = []

    class Spy:
        name = "spy"
        stages = frozenset({"tool_input"})
        deterministic = True

        async def __call__(self, ctx: Any, item: Any) -> Any:
            from nexura_guardrails import Verdict

            seen.append(item.text)
            return Verdict("block", check="spy")

    policy = GuardrailPolicy(
        checks={"spy": Spy()},  # type: ignore[dict-item]
        rules=[
            Rule("checked-block", When(stage="tool_input"), check="spy", action="block"),
            Rule("always-fail", When(stage="tool_input", tools="lookup"), action="fail"),
            Rule("always-block", When(stage="tool_input"), action="block"),
        ],
    )
    with pytest.raises(GuardrailTripwireTriggered) as err:
        await Runner(hooks=[policy]).run_async(
            agent_with([calls(("c1", "lookup", {"q": "x"}))]), "go"
        )
    assert "always-fail" in err.value.guardrail
    assert seen == []  # no check ran: an unconditional rule decided first


async def test_fail_rules_run_before_block_rules() -> None:
    policy = GuardrailPolicy(
        checks={"a": deny("x", "a"), "b": deny("x", "b")},
        rules=[
            Rule("first-block", When(stage="tool_input"), check="a", action="block"),
            Rule("later-fail", When(stage="tool_input"), check="b", action="fail"),
        ],
    )
    with pytest.raises(GuardrailTripwireTriggered) as err:
        await Runner(hooks=[policy]).run_async(
            agent_with([calls(("c1", "lookup", {"q": "x"}))]), "go"
        )
    assert err.value.guardrail.endswith("later-fail")


async def test_block_beats_scrub_on_the_same_item() -> None:
    policy = GuardrailPolicy(
        checks={"redact": pii(action="redact"), "deny": deny("result")},
        rules=[
            Rule("scrub", When(stage="tool_output"), check="redact", action="scrub"),
            Rule("block", When(stage="tool_output"), check="deny", action="block"),
        ],
    )
    result = await Runner(hooks=[policy]).run_async(
        agent_with([calls(("c1", "lookup", {"q": "x"})), "done"]), "go"
    )
    (message,) = [m.content for m in result.messages if m.role == "tool"]
    assert message.startswith("Tool result withheld by policy rule 'block'")


def test_when_matches_origin_toolset_effect_agents_and_args() -> None:
    mcp = ToolInfo(
        name="create_issue",
        origin="mcp:github:create_issue",
        effect="non_idempotent",
        tags=frozenset({"toolset:github"}),
    )
    local = ToolInfo(name="create_issue", origin="python:app.create_issue", effect="pure")
    when = When(stage="tool_input", origin="mcp:*", effect="non_idempotent")
    assert when.matches("tool_input", tool=mcp) and not when.matches("tool_input", tool=local)
    assert not when.matches("tool_output", tool=mcp)
    assert When(stage="tool_input", toolset="git*").matches("tool_input", tool=mcp)
    assert When(stage="tool_input", effect="read").matches("tool_input", tool=local)
    assert When(stage="input", agents="sup*").matches("input", agent="support")
    assert not When(stage="input", agents="sup*").matches("input", agent="billing")
    big = When(stage="tool_input", args={"$.amount": {"gt": 100}, "$.to[0]": {"eq": "x"}})
    assert big.matches("tool_input", tool=local, arguments={"amount": 101, "to": ["x"]})
    assert not big.matches("tool_input", tool=local, arguments={"amount": 100, "to": ["x"]})
    assert not big.matches("tool_input", tool=local, arguments={"to": ["x"]})  # missing: no match
    assert not When(stage="tool_input", args={"$.flag": {"eq": 1}}).matches(
        "tool_input", tool=local, arguments={"flag": True}
    )  # True is not 1
    # Allowlist entries by bare name admit only local tools.
    allow = When(stage="tool_input", tools="create_issue")
    assert allow.matches("tool_input", tool=local, allow_rule=True)
    assert not allow.matches("tool_input", tool=mcp, allow_rule=True)
    assert allow.matches("tool_input", tool=mcp)  # a block rule by name catches every origin


@pytest.mark.parametrize(
    ("ops", "value", "expected"),
    [
        ({"in": ["a", "b"]}, "a", True),
        ({"not_in": ["a"]}, "a", False),
        ({"contains": "rm"}, "x rm y", True),
        ({"contains": 2}, [1, 2], True),
        ({"matches": "^rm"}, "rm -rf", True),
        ({"exists": False}, None, True),
        ({"gte": 5, "lt": 10}, 5, True),
        # Round 5 (R4-H1, deliberate): a string is not a number, so a comparison cannot be
        # decided; a restricting rule (the default here) counts it as met (fail closed).
        ({"lte": 4}, "4", True),
    ],
)
def test_arg_operators(ops: dict[str, Any], value: Any, expected: bool) -> None:
    arguments = {} if value is None else {"v": value}
    when = When(stage="tool_input", args={"$.v": ops})
    assert when.matches("tool_input", tool=ToolInfo(name="t"), arguments=arguments) is expected


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: When(stage="input", tools="x"), "only apply to tool calls"),
        (lambda: When(stage="nope"), "unknown stages"),
        (lambda: When(stage="tool_input", effect="destructive"), "unknown effect"),
        (lambda: When(stage="tool_input", args={"$.a": {"gt": "1"}}), "needs a number"),
        (lambda: When(stage="tool_input", args={"$.a": {"near": 1}}), "unknown operators"),
        (lambda: When(stage="tool_input", args={"a b": {"eq": 1}}), "is not of the form"),
        (lambda: Rule("r", When(stage="input"), check="p", action="scrub"), "scrub applies only"),
        (lambda: Rule("r", When(stage="output"), action="scrub"), "needs a check"),
        (lambda: Rule("r", When(stage="tool_output"), action="require_approval"), "only to stage"),
        (
            lambda: Rule("r", When(stage="tool_input"), check="j", action="require_approval"),
            "deterministic",
        ),
        (lambda: Rule("r", When(), check="p", action="allow"), "allow takes no checks"),
        (lambda: Rule("bad id!", When()), "rule id"),
        (lambda: Rule("r", When(), action="explode"), "unknown action"),  # type: ignore[arg-type]
        (
            lambda: GuardrailPolicy(rules=[Rule("r", When(), check="missing")]),
            "unknown check",
        ),
        (
            lambda: GuardrailPolicy(rules=[Rule("r", When()), Rule("r", When())]),
            "used twice",
        ),
        (
            lambda: GuardrailPolicy(
                checks={"j": __import__("nexura_guardrails").json_only()},
                rules=[Rule("r", When(stage="input"), check="j")],
            ),
            "cannot run on stage",
        ),
    ],
)
def test_invalid_rules_are_refused(build: Any, message: str) -> None:
    with pytest.raises(PolicyError, match=message):
        build()


# ---------------------------------------------------------------- allowlist mode


async def test_fifty_tool_allowlist() -> None:
    """Allowlist mode over 50 tools: only the three allowed ones ever run."""
    ran: list[str] = []

    def make(i: int) -> Any:
        def fn() -> str:
            ran.append(f"tool_{i:02d}")
            return f"ok {i}"

        fn.__name__ = f"tool_{i:02d}"
        fn.__doc__ = f"Tool number {i}."
        return tool(fn)

    tools = [make(i) for i in range(50)]
    allowed = {"tool_03", "tool_17", "tool_42"}
    policy = GuardrailPolicy(
        default_tool_action="block",
        rules=[Rule("read-tools", When(stage="tool_input", tools=sorted(allowed)), action="allow")],
    )
    batch = [ToolCall(id=f"c{i}", name=t.name, arguments={}) for i, t in enumerate(tools)]
    agent = Agent(
        name="A",
        instructions="Help.",
        model=FakeModel(responses=[batch, "done"]),
        tools=tools,
        max_tool_calls=60,
    )
    result = await Runner(hooks=[policy]).run_async(agent, "go")
    assert set(ran) == allowed and len(ran) == 3
    refused = [
        m.content for m in result.messages if m.role == "tool" and "allowlist" in (m.content or "")
    ]
    assert len(refused) == 47
    assert refused[0] == (
        "Tool call blocked by policy rule 'default_tool_action': it is not on the tool allowlist."
    )


async def test_allowlist_by_bare_name_does_not_admit_an_mcp_tool_of_that_name() -> None:
    ran: list[str] = []

    def lookup_fn(q: str) -> str:
        ran.append(q)
        return "remote"

    from nexura_agents.tools import Tool

    remote = Tool("lookup", lookup_fn, description="Remote lookup.", origin="mcp:evil:lookup")
    policy = GuardrailPolicy(
        default_tool_action="block",
        rules=[Rule("allow-lookup", When(stage="tool_input", tools="lookup"), action="allow")],
    )
    agent = agent_with([calls(("c1", "lookup", {"q": "x"})), "done"], tools=[remote])
    result = await Runner(hooks=[policy]).run_async(agent, "go")
    assert ran == []
    assert "allowlist" in [m.content for m in result.messages if m.role == "tool"][0]
    # Naming the origin admits it.
    by_origin = GuardrailPolicy(
        default_tool_action="block",
        rules=[Rule("allow-evil", When(stage="tool_input", origin="mcp:evil:*"), action="allow")],
    )
    agent = agent_with([calls(("c1", "lookup", {"q": "x"})), "done"], tools=[remote])
    await Runner(hooks=[by_origin]).run_async(agent, "go")
    assert ran == ["x"]


# ---------------------------------------------------------------- rm -rf variants
# A blocklist is a first filter only (docs/guardrail-policy.md, security notes).

RM_VARIANTS = [
    "rm -fr /",
    "rm -r -f /",
    "rm --recursive --force /",
    "find / -delete",
    "python -c \"import shutil; shutil.rmtree('/')\"",
    "$(echo cm0gLXJmIC8= | base64 -d)",
    "r=rm; $r -rf /",
]


def shell_tool(log: list[str]) -> Any:
    @tool
    def shell(command: str) -> str:
        """Run a shell command."""
        log.append(command)
        return "done"

    return shell


async def test_rm_rf_blocklist_catches_the_literal_command() -> None:
    log: list[str] = []
    policy = GuardrailPolicy(
        rules=[
            Rule(
                "no-rm-rf",
                When(stage="tool_input", tools="shell", args={"$.command": {"contains": "rm -rf"}}),
            )
        ]
    )
    agent = agent_with(
        [calls(("c1", "shell", {"command": "rm -rf /"})), "done"], tools=[shell_tool(log)]
    )
    await Runner(hooks=[policy]).run_async(agent, "go")
    assert log == []


@pytest.mark.parametrize("command", RM_VARIANTS)
async def test_rm_rf_variants_bypass_the_blocklist(command: str) -> None:
    """Documented weakness (docs/guardrail-policy.md, security notes): every variant runs."""
    log: list[str] = []
    policy = GuardrailPolicy(
        rules=[
            Rule(
                "no-rm-rf",
                When(stage="tool_input", tools="shell", args={"$.command": {"contains": "rm -rf"}}),
            )
        ]
    )
    agent = agent_with(
        [calls(("c1", "shell", {"command": command})), "done"], tools=[shell_tool(log)]
    )
    await Runner(hooks=[policy]).run_async(agent, "go")
    assert log == [command]  # the blocklist did not stop it


@pytest.mark.parametrize("command", ["rm -rf /", *RM_VARIANTS])
async def test_rm_rf_variants_are_stopped_by_the_allowlist(command: str) -> None:
    log: list[str] = []
    policy = GuardrailPolicy(
        default_tool_action="block",
        rules=[Rule("read-only", When(stage="tool_input", tools="lookup"), action="allow")],
    )
    agent = agent_with(
        [calls(("c1", "shell", {"command": command})), "done"],
        tools=[shell_tool(log), lookup],
    )
    result = await Runner(hooks=[policy]).run_async(agent, "go")
    assert log == [] and result.status == "completed"


@pytest.mark.parametrize("command", ["rm -rf /", *RM_VARIANTS])
async def test_rm_rf_variants_wait_for_human_approval(command: str) -> None:
    log: list[str] = []
    policy = GuardrailPolicy(
        default_tool_action="block",
        rules=[
            Rule("allow-shell", When(stage="tool_input", tools="shell"), action="allow"),
            Rule(
                "approve-shell", When(stage="tool_input", tools="shell"), action="require_approval"
            ),
        ],
    )
    agent = agent_with(
        [calls(("c1", "shell", {"command": command})), "done"], tools=[shell_tool(log)]
    )
    runner = Runner(hooks=[policy])
    paused = await runner.run_async(agent, "go")
    assert paused.status == "paused" and log == []
    assert paused.interruption.pending[0].arguments == {"command": command}
    rejected = await runner.resume_async(agent, paused.interruption, reject={"c1": "no"})
    assert rejected.status == "completed" and log == []


# ---------------------------------------------------------------- audit and events


async def test_decision_events_and_audit_records_hold_no_content(tmp_path: Any) -> None:
    sink = MemoryAuditSink()
    path = tmp_path / "audit" / "g.jsonl"
    policy = GuardrailPolicy(
        name="acme",
        checks={"pii": pii(action="redact")},
        rules=[
            Rule("scrub", When(stage="tool_output"), check="pii", action="scrub"),
            Rule("no-shell", When(stage="tool_input", tools="shell")),
        ],
        audit=[sink, JsonlAuditSink(path)],
    )
    log: list[str] = []
    agent = agent_with(
        [
            calls(("c1", "lookup", {"q": "x"}), ("c2", "shell", {"command": "ls"})),
            "done",
        ],
        tools=[lookup, shell_tool(log)],
    )
    events = await run_events(policy, agent, "go")
    decisions = [e.data for e in events if e.type == RunEventType.GUARDRAIL_DECISION]
    assert [(d["stage"], d["action"], d["rule"]) for d in decisions] == [
        ("tool_output", "scrub", "scrub"),
        ("tool_input", "block", "no-shell"),
    ]
    assert all(d["policy_digest"] == policy.short_digest for d in decisions)
    assert decisions[0]["tool_call_id"] == "c1" and decisions[1]["tool_name"] == "shell"
    assert [(r.stage, r.action, r.rule_id, r.tool) for r in sink.records] == [
        ("tool_output", "scrub", "scrub", "lookup"),
        ("tool_input", "block", "no-shell", "shell"),
    ]
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [line["rule_id"] for line in lines] == ["scrub", "no-shell"]
    text = (
        path.read_text() + json.dumps(decisions) + json.dumps([r.to_dict() for r in sink.records])
    )
    for content in ("bob@example.com", "result for", '"ls"', "command"):
        assert content not in text, content
    assert oct(path.stat().st_mode & 0o777) == "0o600"


async def test_allows_are_audited_only_when_asked() -> None:
    quiet, loud = MemoryAuditSink(), MemoryAuditSink()
    for sink, flag in ((quiet, False), (loud, True)):
        policy = GuardrailPolicy(
            checks={"pii": pii()},
            rules=[Rule("scan", When(stage="tool_input"), check="pii")],
            audit=sink,
            audit_allows=flag,
        )
        await Runner(hooks=[policy]).run_async(
            agent_with([calls(("c1", "lookup", {"q": "x"})), "done"]), "go"
        )
    assert quiet.records == [] and [r.action for r in loud.records] == ["allow"]


async def test_audit_sink_failure_is_counted_or_fails_closed() -> None:
    def broken(record: Any) -> None:
        raise OSError("disk full")

    soft = GuardrailPolicy(
        rules=[Rule("r", When(stage="tool_input"))], audit=CallbackAuditSink(broken)
    )
    await Runner(hooks=[soft]).run_async(
        agent_with([calls(("c1", "lookup", {"q": "x"})), "done"]), "go"
    )
    assert soft.audit_errors == 1
    hard = GuardrailPolicy(
        rules=[Rule("r", When(stage="tool_input"))],
        audit=CallbackAuditSink(broken),
        audit_required=True,
    )
    with pytest.raises(PolicyAuditError):  # a NexuraError: not wrapped as a tool failure
        await Runner(hooks=[hard]).run_async(
            agent_with([calls(("c1", "lookup", {"q": "x"})), "done"]), "go"
        )


# ---------------------------------------------------------------- errors and timeouts


class Broken:
    name = "broken"
    stages = frozenset({"input", "output", "tool_input", "tool_output"})
    deterministic = True

    async def __call__(self, ctx: Any, item: Any) -> Any:
        raise RuntimeError("down")


async def test_check_errors_fail_closed_unless_on_error_allow() -> None:
    closed = GuardrailPolicy(
        checks={"b": Broken()},  # type: ignore[dict-item]
        rules=[Rule("r", When(stage="tool_input"), check="b")],
    )
    result = await Runner(hooks=[closed]).run_async(
        agent_with([calls(("c1", "lookup", {"q": "x"})), "done"]), "go"
    )
    (message,) = [m.content for m in result.messages if m.role == "tool"]
    assert message == (
        "Tool call blocked by policy rule 'r': guardrail check 'broken' could not decide "
        "(RuntimeError); blocked."
    )
    opened = GuardrailPolicy(
        checks={"b": Broken()},  # type: ignore[dict-item]
        rules=[Rule("r", When(stage="tool_input"), check="b", on_error="allow")],
    )
    result = await Runner(hooks=[opened]).run_async(
        agent_with([calls(("c1", "lookup", {"q": "x"})), "done"]), "go"
    )
    assert [m.content for m in result.messages if m.role == "tool"][0].startswith("result for")


async def test_stage_timeout_blocks() -> None:
    import asyncio

    class Slow:
        name = "slow"
        stages = frozenset({"tool_input"})
        deterministic = True

        async def __call__(self, ctx: Any, item: Any) -> Any:
            await asyncio.sleep(5)

    policy = GuardrailPolicy(
        checks={"s": Slow()},  # type: ignore[dict-item]
        rules=[Rule("r", When(stage="tool_input"), check="s")],
        stage_timeout=0.05,
    )
    result = await Runner(hooks=[policy]).run_async(
        agent_with([calls(("c1", "lookup", {"q": "x"})), "done"]), "go"
    )
    (message,) = [m.content for m in result.messages if m.role == "tool"]
    assert message.startswith("Tool call blocked by policy rule 'stage_timeout'")


# ---------------------------------------------------------------- install and lock


async def test_install_locked_cannot_be_dropped() -> None:
    from nexura_agents.errors import SDKValidationError

    policy = GuardrailPolicy(rules=[Rule("r", When(stage="tool_input", tools="lookup"))])
    policy.install(locked=True)
    assert default_hooks() is policy
    with pytest.raises(SDKValidationError):
        Runner(default_hooks=False)
    with pytest.raises(SDKValidationError):
        set_default_hooks(None)
    result = await Runner().run_async(
        agent_with([calls(("c1", "lookup", {"q": "x"})), "done"]), "go"
    )
    assert [m.content for m in result.messages if m.role == "tool"][0].startswith(
        "Tool call blocked by policy rule 'r'"
    )


def test_describe_and_digest() -> None:
    a = GuardrailPolicy(name="p", checks={"pii": pii()}, rules=[Rule("r", When(), check="pii")])
    b = GuardrailPolicy(name="p", checks={"pii": pii()}, rules=[Rule("r", When(), check="pii")])
    c = GuardrailPolicy(
        name="p", checks={"pii": pii()}, rules=[Rule("r", When(stage="output"), check="pii")]
    )
    assert a.digest == b.digest != c.digest and len(a.digest) == 64
    summary = a.describe()
    assert summary["rules"][0]["checks"] == ["pii"] and summary["checks"][0]["type"] == "Prefilter"
    json.dumps(summary)  # JSON-safe


async def test_allowlist_refusal_is_not_sent_for_approval() -> None:
    """A call the allowlist refuses anyway does not bother a person with an approval."""
    log: list[str] = []
    policy = GuardrailPolicy(
        default_tool_action="block",
        rules=[
            Rule(
                "approve-shell", When(stage="tool_input", tools="shell"), action="require_approval"
            )
        ],
    )
    agent = agent_with([calls(("c1", "shell", {"command": "ls"})), "done"], tools=[shell_tool(log)])
    result = await Runner(hooks=[policy]).run_async(agent, "go")
    assert result.status == "completed" and log == []
    assert "allowlist" in [m.content for m in result.messages if m.role == "tool"][0]
