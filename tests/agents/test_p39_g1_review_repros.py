# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""P39 G1 adversarial review: failing repros (offline; FakeModel and in-memory stores only).

Repros from the independent review; the assertions are unchanged.
Every test asserted the behaviour the SDK should have and failed on the reviewed tree. The
ids match the review report. ``test_min4_*`` stays a strict xfail: the lead
accepted MIN4 as documented behaviour (guardrail usage is in the run's total but not in the
per-agent / per-model breakdowns, like nested tool usage), so it records that gap.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from nexura_agents import Agent, AgentHooks, ComposedHooks, FakeModel, Runner, ToolCall, tool
from nexura_agents.contracts.execution import EffectClass
from nexura_agents.durable import MemoryCheckpointStore
from nexura_agents.module_tools import module_tool

from nexura_runtime.integrations.tools import tool_to_module
from nexura_runtime.modules.registry import LegacyModuleAdapter


class Crash(BaseException):
    """Simulated process death."""


def call(name: str, **arguments: Any) -> list[ToolCall]:
    return [ToolCall(f"c-{name}", name, arguments)]


# ------------------------------------------------------------------ B1: effect defaults


def _payments() -> dict[str, int]:
    return {"n": 0}


def test_b1_tool_to_module_default_is_not_an_effect_declaration() -> None:
    """``tool_to_module`` wraps an undeclared (non_idempotent) @tool. Its *default*
    ``effect_policy="idempotent_write"`` is not a developer declaration about the tool, so
    the module tool must stay non_idempotent (A6: undeclared effects are non_idempotent)."""

    @tool
    def charge_card(amount: int) -> str:
        """Charge the card."""
        return "ok"

    assert charge_card.effect_class() is EffectClass.NON_IDEMPOTENT
    wrapped = module_tool(tool_to_module(charge_card), name="charge_card")
    assert wrapped.effect_class() is EffectClass.NON_IDEMPOTENT  # fails: IDEMPOTENT


def test_b1_legacy_handler_default_is_not_an_effect_declaration() -> None:
    """Any legacy callable registered with ``register_legacy`` gets a hard-coded
    ``idempotent_write``; that says nothing about the handler, which may be a payment."""
    adapter = LegacyModuleAdapter("pay", lambda inputs: {"paid": True})
    assert module_tool(adapter, name="pay").effect_class() is EffectClass.NON_IDEMPOTENT


async def test_b1_a_crash_does_not_silently_repeat_a_wrapped_payment() -> None:
    """End to end: a crash during the wrapped payment is recovered by re-sending it (same
    idempotency key, which the wrapped function ignores), so the card is charged twice.
    Before G1 the module tool was non_idempotent and recovery paused (effect_unknown)."""
    charges = _payments()

    @tool
    def charge_card(amount: int) -> str:
        """Charge the card."""
        charges["n"] += 1
        if charges["n"] == 1:
            raise Crash  # the charge went through, then the process died
        return "charged"

    pay = module_tool(tool_to_module(charge_card), name="charge_card")
    store = MemoryCheckpointStore()
    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(responses=[call("charge_card", amount=100), "x"]),
        tools=[pay],
    )
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(agent, "pay 100")
    run_id = next(iter(store._data))
    again = Agent(name="A", instructions="i", model=FakeModel(responses=["done"]), tools=[pay])
    result = await Runner(checkpoint_store=store).recover_async(again, run_id)
    assert result.status == "paused"  # fails: "completed"
    assert charges["n"] == 1  # fails: 2 (charged twice)


# ------------------------------------------------------------------ MAJ1: subclass flattening


def test_maj1_flatten_drops_a_composed_subclass_override() -> None:
    """A ``ComposedHooks`` subclass that adds its own behaviour (a policy built on it) loses
    that behaviour when nested: the constructor flattens it to its members."""

    class Policy(ComposedHooks):
        def tool_needs_approval(self, ctx: Any, tool: Any, arguments: Any) -> bool | None:
            return True  # gate everything

    class User(AgentHooks):
        pass

    composed = ComposedHooks(Policy(User()), User())
    assert any(isinstance(m, Policy) for m in composed.members)  # fails: flattened away


async def test_maj1_nested_policy_gate_is_lost_in_a_runner_list() -> None:
    """Same, through the runner: the nested policy's approval gate disappears."""

    class Policy(ComposedHooks):
        def tool_needs_approval(self, ctx: Any, tool: Any, arguments: Any) -> bool | None:
            return True

    ran = {"n": 0}

    @tool
    def send(to: str) -> str:
        """Send."""
        ran["n"] += 1
        return "sent"

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(responses=[call("send", to="x"), "done"]),
        tools=[send],
    )
    alone = await Runner(hooks=Policy()).run_async(agent, "go")
    assert alone.status == "paused"  # the policy alone gates the call
    agent2 = copy.copy(agent)
    agent2.model = FakeModel(responses=[call("send", to="x"), "done"])
    result = await Runner(hooks=[Policy(), AgentHooks()]).run_async(agent2, "go")
    assert result.status == "paused" and ran["n"] == 0  # fails: completed, ran once


# ------------------------------------------------------------------ m: usage after the limit


async def test_min2_output_guardrail_usage_is_checked_against_usage_limits() -> None:
    """G1 now adds an output guardrail's (judge's) usage to RunResult.usage after the last
    UsageLimits check, so a run completes reporting more tokens than its hard cap."""
    from nexura_agents import GuardrailResult, UsageLimitExceededError, UsageLimits
    from nexura_agents.types import Usage

    def judge(ctx: Any, value: Any) -> GuardrailResult:
        ctx.add_usage(Usage(prompt_tokens=7, completion_tokens=3, total_tokens=10))
        return GuardrailResult.passed()

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(["hi"], default_usage=Usage(7, 3, 10)),
        output_guardrails=[judge],
    )
    limits = UsageLimits(total_tokens_limit=15)
    with pytest.raises(UsageLimitExceededError):  # fails: completes with usage 20 > 15
        result = await Runner(usage_limits=limits).run_async(agent, "go")
        assert result.usage.total_tokens <= 15, result.usage  # type: ignore[operator]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="MIN4 accepted as documented: breakdowns cover the agents' own model calls only",
)
async def test_min4_guardrail_usage_is_missing_from_usage_by_agent() -> None:
    """Guardrail usage reaches RunResult.usage but never usage_by_agent/usage_by_model, so
    they disagree on a successful run (documented deviation 5; design 4.4 says per model)."""
    from nexura_agents import GuardrailResult
    from nexura_agents.types import Usage

    def judge(ctx: Any, value: Any) -> GuardrailResult:
        ctx.add_usage(Usage(prompt_tokens=7, completion_tokens=3, total_tokens=10))
        return GuardrailResult.passed()

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(["hi"], default_usage=Usage(7, 3, 10)),
        output_guardrails=[judge],
    )
    result = await Runner().run_async(agent, "go")
    by_agent = sum(u.total_tokens or 0 for u in result.usage_by_agent.values())
    assert result.usage.total_tokens == by_agent  # fails: 20 != 10


# ------------------------------------------------------------------ m: list passed as a member


async def test_min1_a_list_member_is_refused_not_silently_skipped() -> None:
    """``ComposedHooks([policy])`` (a list instead of *args) is accepted as one member with
    no hook methods, so every hook of the policy is skipped: the approval gate is lost
    (fail open). It should raise SDKValidationError like a str member does."""
    from nexura_agents.errors import SDKValidationError

    class Policy(AgentHooks):
        def tool_needs_approval(self, ctx: Any, tool: Any, arguments: Any) -> bool | None:
            return True

    with pytest.raises(SDKValidationError):  # fails: accepted
        ComposedHooks([Policy()])


# ------------------------------------------------------------------ m: cancel drops usage


async def test_min3_cancelled_output_stage_keeps_usage_of_guardrails_that_finished() -> None:
    """The first output guardrail (a judge) finishes and records 10 tokens; the second is
    cancelled. RUN_CANCELLED reports the run's usage without the judge's tokens: the drain
    runs only after every output guardrail passed."""
    import asyncio

    from nexura_agents import CancelToken, GuardrailResult
    from nexura_agents.types import Usage

    cancel = CancelToken()

    async def judge(ctx: Any, value: Any) -> GuardrailResult:
        ctx.add_usage(Usage(prompt_tokens=7, completion_tokens=3, total_tokens=10))
        return GuardrailResult.passed()

    async def slow(ctx: Any, value: Any) -> GuardrailResult:
        cancel.cancel("stop")
        await asyncio.sleep(5)
        return GuardrailResult.passed()

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(["hi"], default_usage=Usage(7, 3, 10)),
        output_guardrails=[judge, slow],
    )
    result = await Runner().run_async(agent, "go", cancel=cancel)
    assert result.status == "cancelled"
    assert result.usage.total_tokens == 20  # fails: 10


# ------------------------------------------------------------------ m: cancel before guardrails


async def test_min5_cancel_during_output_validator_does_not_return_the_unchecked_answer() -> None:
    """G1 pops the final answer when the run is cancelled during its output guardrails, but
    an async output validator runs first (also under the CancelToken) and a cancel there
    returns the answer in result.messages, although no output guardrail has seen it."""
    import asyncio

    from nexura_agents import CancelToken, GuardrailResult

    cancel = CancelToken()
    seen: list[str] = []

    async def validator(ctx: Any, output: Any) -> Any:
        cancel.cancel("stop")
        await asyncio.sleep(5)
        return output

    def redact(ctx: Any, value: Any) -> GuardrailResult:
        seen.append(value)
        return GuardrailResult.rewritten("[redacted]")

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(["card 4111 1111 1111 1111"]),
        output_validators=[validator],
        output_guardrails=[redact],
    )
    result = await Runner().run_async(agent, "go", cancel=cancel)
    assert result.status == "cancelled" and seen == []
    texts = [m.content for m in result.messages if m.role == "assistant"]
    assert "card 4111 1111 1111 1111" not in texts  # fails: returned unchecked
