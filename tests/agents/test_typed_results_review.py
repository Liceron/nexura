"""F53 review: the documented limits of typed results (paused, cancelled, handoff)."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

from nexura_agents import Agent, CancelToken, FakeModel, Runner, ToolCall, tool
from pydantic import BaseModel


class Invoice(BaseModel):
    number: str


@tool(requires_approval=True)
def refund(order_id: str) -> str:
    """Refund."""
    return "ok"


def _mypy(tmp_path: Path, src: str) -> str:
    f = tmp_path / "snip.py"
    f.write_text(textwrap.dedent(src))
    p = subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", str(f)], capture_output=True, text=True
    )
    return p.stdout


def test_paused_result_has_no_output_as_documented() -> None:
    """RunResult[Invoice].output is typed Invoice, yet a paused run has output None."""
    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(
            responses=[[ToolCall(id="c1", name="refund", arguments={"order_id": "1"})]]
        ),
        tools=[refund],
        output_type=Invoice,
    )
    r = Runner().run(agent, "go")
    assert r.status == "paused"
    # Documented: a paused run has no output yet; check status before using it.
    assert r.status == "paused" and r.output is None


def test_cancelled_result_has_no_output_as_documented() -> None:
    token = CancelToken()
    token.cancel("x")
    agent = Agent(
        name="A", instructions="i", model=FakeModel(responses=["{}"]), output_type=Invoice
    )
    r = Runner().run(agent, "go", cancel=token)
    assert r.status == "cancelled"
    # Documented: a cancelled run has no output.
    assert r.status == "cancelled" and r.output is None


def test_handoff_target_decides_the_output_as_documented() -> None:
    """Agent[str] hands off to an Invoice agent: output is an Invoice, typed str."""
    target = Agent(
        name="billing",
        instructions="i",
        model=FakeModel(responses=['{"number": "7"}']),
        output_type=Invoice,
    )
    lead = Agent(
        name="lead",
        instructions="i",
        model=FakeModel(responses=[[ToolCall(id="h", name="transfer_to_billing", arguments={})]]),
        handoffs=[target],
    )
    r = Runner().run(lead, "go")
    assert r.status == "completed"
    # Documented: a handoff target with another output_type decides the output.
    assert isinstance(r.output, Invoice)


def test_mypy_optional_output_type_gives_union_agent(tmp_path: Path) -> None:
    out = _mypy(
        tmp_path,
        """
        from nexura_agents import Agent, FakeModel, Runner
        from pydantic import BaseModel
        class Inv(BaseModel):
            n: str
        def f(ot: type[Inv] | None) -> None:
            a = Agent(name="a", instructions="x", model=FakeModel(responses=["x"]), output_type=ot)
            reveal_type(Runner().run(a, "hi").output)
        """,
    )
    assert "Union" not in out and " | " not in out.split("Revealed type")[-1], out
