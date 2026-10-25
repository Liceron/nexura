"""S12 round 5: refusals before invocation stay failures; G6 policy on module nodes, subagents
and coordinator loops. Local fakes only (no network, no paid calls)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from nexura_agents import Agent, FakeModel, Runner, ToolCall, tool
from nexura_agents.contracts import BaseModule, ModuleManifest, ModuleResult
from nexura_agents.contracts.execution import EffectStatus, ExecutionId
from nexura_agents.errors import (
    ProviderResponseError,
    SDKValidationError,
    WorkflowExecutionError,
    WorkflowTimeoutError,
    WorkflowValidationError,
)
from nexura_agents.policy import _unlock_for_tests
from nexura_guardrails import GuardrailPolicy, Rule, When, pii

from nexura_runtime.workflow import (
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)


@pytest.fixture(autouse=True)
def _clean() -> Any:
    _unlock_for_tests()
    yield
    _unlock_for_tests()


class Charge(BaseModule):
    """A local fake non-idempotent write: records each charge in a list."""

    def __init__(self, charged: list[Any], output: dict[str, Any] | None = None) -> None:
        self.charged = charged
        self.output = output or {"receipt": "r-1", "card": "4111 1111 1111 1111"}
        self._manifest = ModuleManifest(
            module_id="fake.charge",
            version="1.0.0",
            kind="integration",
            title="charge",
            description="local fake charge",
            input_schema={
                "type": "object",
                "properties": {"amount": {"type": "integer"}},
                "required": ["amount"],
            },
            effect_policy="non_idempotent_write",
        )

    @property
    def manifest(self) -> ModuleManifest:
        return self._manifest

    async def invoke(
        self, context: Any, inputs: dict[str, Any], config: dict[str, Any]
    ) -> ModuleResult:
        self.charged.append(inputs["amount"])
        return ModuleResult.success(dict(self.output))


def charge_spec() -> WorkflowSpec:
    node = WorkflowNode(
        id="charge",
        handler="charge",
        input_mapping={"amount": "amount"},
        output_mapping={"receipt": "receipt", "card": "card"},
    )
    return WorkflowSpec(name="r5", entry_node="charge", nodes={"charge": node})


def engine(tmp_path: Path, charged: list[Any], **kwargs: Any) -> WorkflowEngine:
    registry = ModuleRegistry()
    registry.register("charge", Charge(charged, kwargs.pop("output", None)))
    store = SQLiteWorkflowStore(tmp_path / "wf.sqlite3")
    return WorkflowEngine(charge_spec(), registry, store=store, **kwargs)


async def journal(eng: WorkflowEngine, run_id: str = "r") -> Any:
    assert eng.store is not None
    store = await eng.store.open_execution_store()  # type: ignore[attr-defined]
    return await store.get(ExecutionId("default", run_id))


def tool_rule(action: str, **when: Any) -> GuardrailPolicy:
    return GuardrailPolicy(
        rules=[Rule("r", When(stage="tool_input", tools="charge", **when), action=action)]  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------- decision 3: not invoked


async def test_schema_refusal_before_invoke_fails_and_settles_not_applied(tmp_path: Path) -> None:
    """Inputs the module's schema refuses: BaseModule never calls invoke, so nothing was sent."""
    charged: list[Any] = []
    eng = engine(tmp_path, charged)
    with pytest.raises(WorkflowExecutionError):
        await eng.run({"amount": "not a number"}, workflow_run_id="r")
    result = await eng.get_result("r")
    assert result.status == "failed" and result.pending_effect is None
    assert charged == []
    effects = list((await journal(eng)).effects.values())
    assert len(effects) == 1 and effects[0].status == EffectStatus.RESOLVED
    assert effects[0].resolution.value == "not_applied"
    assert effects[0].resolved_by == "workflow:pre_dispatch"


async def test_handler_that_started_and_raised_still_pauses(tmp_path: Path) -> None:
    """R2/R3 behaviour kept: an undeclared handler that really started is unknown."""
    sent: list[str] = []

    async def send(state: dict[str, Any]) -> dict[str, Any]:
        sent.append("x")
        raise ConnectionError("local fake send: reply lost")

    registry = ModuleRegistry()
    registry.register("send", send)
    spec = WorkflowSpec(name="s", entry_node="s", nodes={"s": WorkflowNode(id="s", handler="send")})
    eng = WorkflowEngine(spec, registry, store=SQLiteWorkflowStore(tmp_path / "s.sqlite3"))
    paused = await eng.run({}, workflow_run_id="r")
    assert paused.status == "paused" and paused.pause_reason == "effect_unknown"
    assert sent == ["x"]


async def test_deadline_before_invocation_fails_without_effect_unknown(tmp_path: Path) -> None:
    """The deadline expires in a pure step; the next (undeclared) handler is never invoked."""
    sent: list[str] = []

    async def slow(state: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0.3)
        return {}

    async def send(state: dict[str, Any]) -> dict[str, Any]:
        sent.append("x")
        return {"sent": True}

    registry = ModuleRegistry()
    registry.register("slow", slow, effect="pure")
    registry.register("send", send)
    spec = WorkflowSpec(
        name="d",
        entry_node="slow",
        nodes={
            "slow": WorkflowNode(id="slow", handler="slow", default_transition="send"),
            "send": WorkflowNode(id="send", handler="send"),
        },
    )
    eng = WorkflowEngine(
        spec, registry, store=SQLiteWorkflowStore(tmp_path / "d.sqlite3"), timeout=0.1
    )
    with pytest.raises(WorkflowTimeoutError):
        await eng.run({}, workflow_run_id="r")
    result = await eng.get_result("r")
    assert result.status == "failed" and result.pause_reason is None
    assert sent == []
    assert all(e.status == EffectStatus.RESOLVED for e in (await journal(eng)).effects.values())


# ------------------------------------------------------- G6 (a): policy on module nodes


async def test_tool_input_block_refuses_before_any_intent(tmp_path: Path) -> None:
    charged: list[Any] = []
    eng = engine(tmp_path, charged, guardrail_policy=tool_rule("block"))
    with pytest.raises(WorkflowExecutionError, match="guardrail policy rule 'r'"):
        await eng.run({"amount": 5}, workflow_run_id="r")
    assert (await eng.get_result("r")).status == "failed"
    assert charged == []
    snap = await journal(eng)
    assert snap is None or (not snap.steps and not snap.effects)


async def test_require_approval_by_amount_pauses_then_runs(tmp_path: Path) -> None:
    charged: list[Any] = []
    policy = tool_rule("require_approval", args={"$.amount": {"gt": 100}})
    eng = engine(tmp_path, charged, guardrail_policy=policy)
    small = await eng.run({"amount": 5}, workflow_run_id="small")
    assert small.status == "completed" and charged == [5]
    paused = await eng.run({"amount": 500}, workflow_run_id="big")
    assert paused.status == "paused" and paused.pending_approval is not None
    assert charged == [5]
    done = await eng.resume("big", paused.pending_approval.approval_id)
    assert done.status == "completed" and charged == [5, 500]


@pytest.mark.parametrize("action", ["block", "scrub"])
async def test_tool_output_rules_withhold_outputs_and_commit_applied(
    tmp_path: Path, action: str
) -> None:
    charged: list[Any] = []
    policy = GuardrailPolicy(
        checks={"pii": pii()},
        rules=[Rule("no-card", When(stage="tool_output"), check="pii", action=action)],  # type: ignore[arg-type]
    )
    eng = engine(tmp_path, charged, guardrail_policy=policy)
    done = await eng.run({"amount": 5}, workflow_run_id="r")
    assert done.status == "completed" and charged == [5]
    assert "card" not in done.state and "receipt" not in done.state
    effects = list((await journal(eng)).effects.values())
    assert [e.status for e in effects] == [EffectStatus.CONFIRMED]


async def test_tool_output_fail_commits_applied_then_fails_without_effect_unknown(
    tmp_path: Path,
) -> None:
    charged: list[Any] = []
    policy = GuardrailPolicy(
        checks={"pii": pii()},
        rules=[Rule("no-card", When(stage="tool_output"), check="pii", action="fail")],
    )
    eng = engine(tmp_path, charged, guardrail_policy=policy)
    with pytest.raises(WorkflowExecutionError, match="refused by guardrail policy rule 'no-card'"):
        await eng.run({"amount": 5}, workflow_run_id="r")
    result = await eng.get_result("r")
    assert result.status == "failed" and result.pause_reason is None
    assert charged == [5]  # never sent twice, never retried
    snap = await journal(eng)
    assert [e.status for e in snap.effects.values()] == [EffectStatus.CONFIRMED]
    assert snap.run.state.get("terminal_failure")
    assert snap.run.status == "failed"


async def test_process_default_policy_applies_and_bad_value_is_refused(tmp_path: Path) -> None:
    charged: list[Any] = []
    tool_rule("block").install()
    eng = engine(tmp_path, charged)
    with pytest.raises(WorkflowExecutionError, match="guardrail policy"):
        await eng.run({"amount": 5}, workflow_run_id="r")
    assert charged == []
    with pytest.raises(WorkflowValidationError, match="GuardrailPolicy"):
        engine(tmp_path, charged, guardrail_policy=object())


async def test_allowlist_refuses_plain_handler_nodes_and_allows_listed_modules(
    tmp_path: Path,
) -> None:
    ran: list[str] = []

    def plain(state: dict[str, Any]) -> dict[str, Any]:
        ran.append("plain")
        return {}

    allow = GuardrailPolicy(
        default_tool_action="block",
        # An allow rule admits a non-local tool only when it names its origin.
        rules=[
            Rule(
                "ok",
                When(stage="tool_input", tools="charge", origin="workflow:fake.charge"),
                action="allow",
            )
        ],
    )
    charged: list[Any] = []
    assert (await engine(tmp_path, charged, guardrail_policy=allow).run({"amount": 3})).status == (
        "completed"
    )
    assert charged == [3]
    registry = ModuleRegistry()
    registry.register("plain", plain, effect="pure")
    spec = WorkflowSpec(
        name="p", entry_node="p", nodes={"p": WorkflowNode(id="p", handler="plain")}
    )
    eng = WorkflowEngine(spec, registry, guardrail_policy=allow)
    with pytest.raises(WorkflowExecutionError, match="default_tool_action"):
        await eng.run({})
    assert ran == []


# ------------------------------------------------- G6 (b), (c): subagents, coordinator


async def test_subagent_is_refused_before_any_model_call_under_a_model_stage_policy() -> None:
    from nexura_runtime.runtime.agents.definition import AgentDefinition

    seen: list[Any] = []
    sub = AgentDefinition(name="sub", model=FakeModel(handler=lambda m, t: seen.append(m) or "ok"))
    GuardrailPolicy(
        checks={"pii": pii()},
        rules=[Rule("no-pii", When(stage="model_input"), check="pii", action="fail")],
    ).install(locked=True)
    with pytest.raises(SDKValidationError, match="guardrail policy"):
        await sub.as_tool().arun(query="contact bob@example.com")
    assert seen == []


async def test_subagent_and_coordinator_refuse_duplicate_tool_call_ids() -> None:
    from nexura_runtime.runtime.agents.coordinator import AgentCoordinator
    from nexura_runtime.runtime.agents.definition import AgentDefinition

    ran: list[str] = []

    @tool
    def act(n: int) -> str:
        """Act."""
        ran.append(str(n))
        return "done"

    def dup() -> list[ToolCall]:
        return [ToolCall(id="c1", name="act", arguments={"n": 1}), ToolCall("c1", "act", {"n": 2})]

    sub = AgentDefinition(name="w", model=FakeModel(responses=[dup(), "ok"]), tools=[act])
    with pytest.raises(ProviderResponseError, match="unique within a reply"):
        await sub.as_tool().arun(query="go")
    coordinated = AgentDefinition(name="w", model=FakeModel(responses=[dup(), "ok"]), tools=[act])
    with pytest.raises(ProviderResponseError, match="unique within a reply"):
        await AgentCoordinator([coordinated]).run_async("w", "go")
    assert ran == []


async def test_runner_parent_still_runs_subagents_without_a_policy() -> None:
    from nexura_runtime.runtime.agents.definition import AgentDefinition

    sub = AgentDefinition(name="sub", model=FakeModel(responses=["sub answer"]))
    delegate = sub.as_tool()
    parent = Agent(
        name="P",
        instructions="x",
        model=FakeModel(
            responses=[[ToolCall(id="c1", name=delegate.name, arguments={"query": "q"})], "done"]
        ),
        tools=[delegate],
    )
    result = await Runner().run_async(parent, "go")
    assert result.output == "done"
    assert any(m.role == "tool" and m.content == "sub answer" for m in result.messages)
