# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""P39 G6 round 5 (R4-H1): comparison rules on Decimal and non-finite values.

The reviewer's three repros, inverted, plus boundary
cases. Offline: FakeModel only.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from nexura_agents import Agent, FakeModel, Runner, ToolCall, tool
from nexura_agents.policy import _unlock_for_tests
from nexura_agents.tools import ToolInfo
from nexura_guardrails import GuardrailPolicy, Rule, When
from pydantic import BaseModel


@pytest.fixture(autouse=True)
def _clean() -> Any:
    _unlock_for_tests()
    yield
    _unlock_for_tests()


class Payment(BaseModel):
    amount: Decimal
    to: str


def gate(
    tool_name: str, path: str = "$.amount", op: str = "gt", limit: Any = 100
) -> GuardrailPolicy:
    return GuardrailPolicy(
        rules=[
            Rule(
                "big",
                When(stage="tool_input", tools=tool_name, args={path: {op: limit}}),
                action="require_approval",
            )
        ]
    )


async def run(policy: GuardrailPolicy, tool_obj: Any, args: dict[str, Any]) -> Any:
    agent = Agent(
        name="A",
        instructions="x",
        model=FakeModel(
            responses=[[ToolCall(id="c1", name=tool_obj.name, arguments=args)], "done"]
        ),
        tools=[tool_obj],
    )
    return await Runner(hooks=[policy]).run_async(agent, "go")


def decimal_payer(paid: list[Any]) -> Any:
    @tool
    def pay(amount: Decimal) -> str:
        """Pay."""
        paid.append(amount)
        return "paid"

    return pay


# ---------------------------------------------------------------- the reviewer's repros, inverted


async def test_a_decimal_amount_sent_as_a_string_pauses() -> None:
    paid: list[Any] = []
    pay = decimal_payer(paid)
    assert pay.dispatch_arguments({"amount": "5000"}) == {"amount": Decimal("5000")}
    result = await run(gate("pay"), pay, {"amount": "5000"})
    assert result.status == "paused" and paid == []


async def test_a_nested_decimal_pauses() -> None:
    paid: list[Any] = []

    @tool
    def pay2(payment: Payment) -> str:
        """Pay."""
        paid.append(payment.amount)
        return "paid"

    result = await run(
        gate("pay2", "$.payment.amount"), pay2, {"payment": {"amount": "250000", "to": "x"}}
    )
    assert result.status == "paused" and paid == []


async def test_an_infinite_float_pauses() -> None:
    paid: list[Any] = []

    @tool
    def payf(amount: float) -> str:
        """Pay."""
        paid.append(amount)
        return "paid"

    result = await run(gate("payf"), payf, {"amount": "inf"})
    assert result.status == "paused" and paid == []


# ---------------------------------------------------------------- boundaries


@pytest.mark.parametrize(
    ("op", "limit", "value", "met"),
    [
        ("gt", 100, Decimal("100.00"), False),  # equal to 100: not greater
        ("gte", 100, Decimal("100.00"), True),
        ("gt", 100, Decimal("100.01"), True),
        ("gt", 100, Decimal("100.0000000000000000000000001"), True),  # exact, not via float
        ("lt", 0, Decimal("-0.01"), True),  # a negative Decimal
        ("gt", 100, Decimal("-5000"), False),
        ("lte", 100.5, Decimal("100.5"), True),  # a float limit compares exactly too
    ],
)
def test_decimal_boundaries_compare_exactly(op: str, limit: Any, value: Decimal, met: bool) -> None:
    when = When(stage="tool_input", args={"$.amount": {op: limit}})
    info = ToolInfo(name="pay")
    assert when.matches("tool_input", tool=info, arguments={"amount": value}) is met
    assert (
        when.matches("tool_input", tool=info, arguments={"amount": value}, allow_rule=True) is met
    )


@pytest.mark.parametrize(("amount", "paused"), [(100, False), (100.0, False), (100.01, True)])
async def test_decimal_parameter_with_json_numbers(amount: Any, paused: bool) -> None:
    paid: list[Any] = []
    result = await run(gate("pay"), decimal_payer(paid), {"amount": amount})
    assert (result.status == "paused") is paused
    assert (paid == []) is paused


async def test_a_nan_amount_pauses() -> None:
    paid: list[Any] = []

    @tool
    def payf(amount: float) -> str:
        """Pay."""
        paid.append(amount)
        return "paid"

    # The rule matches (fail closed), so the call is gated; a NaN cannot go into the
    # approval digest (canonical JSON refuses non-finite numbers), so the run fails there.
    # Either way the payment never runs.
    with pytest.raises(ValueError, match="Non-finite"):
        await run(gate("payf"), payf, {"amount": float("nan")})
    assert paid == []
    # "nan" as text (the tool would coerce it to NaN) pauses for approval.
    result = await run(gate("payf"), payf, {"amount": "nan"})
    assert result.status == "paused" and paid == []
    info = ToolInfo(name="pay")
    when = When(stage="tool_input", args={"$.amount": {"gt": 100}})
    assert when.matches("tool_input", tool=info, arguments={"amount": Decimal("NaN")})
    assert not when.matches(
        "tool_input", tool=info, arguments={"amount": Decimal("NaN")}, allow_rule=True
    )


@pytest.mark.parametrize("value", ["5000", "abc", float("nan"), float("inf"), True, None])
def test_undecidable_comparisons_fail_closed_and_never_allow(value: Any) -> None:
    info = ToolInfo(name="t")
    restrict = When(stage="tool_input", args={"$.v": {"gt": 100}})
    assert restrict.matches("tool_input", tool=info, arguments={"v": value})
    assert not restrict.matches("tool_input", tool=info, arguments={"v": value}, allow_rule=True)
    assert not restrict.matches("tool_input", tool=info, arguments={})  # missing: no match


def test_allow_rules_still_compare_real_numbers() -> None:
    info = ToolInfo(name="t")
    small = When(stage="tool_input", args={"$.v": {"lt": 100}})
    assert small.matches(
        "tool_input", tool=info, arguments={"v": Decimal("99.99")}, allow_rule=True
    )
    assert not small.matches(
        "tool_input", tool=info, arguments={"v": Decimal("100")}, allow_rule=True
    )


def test_huge_integer_threshold_and_value_compare_without_overflow() -> None:
    """A 400-digit integer is a finite number: it compares, never raises OverflowError."""
    from nexura_guardrails.policy.rules import _number

    huge = 10**400
    assert _number(huge) == huge
    assert _number(-huge) == -huge
