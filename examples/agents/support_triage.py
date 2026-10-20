"""Support triage with handoffs and an approval-gated refund.

Scenario: a Triage agent routes customer messages to a Billing or Tech
specialist. Billing owns an ``issue_refund`` tool that requires human approval.
The run pauses before the refund, the interruption is saved as JSON (as you
would in a database), and a reviewer later resumes it by approving the exact
digest of the arguments they saw.

Demonstrates: handoffs, ``requires_approval``, ``RunInterruption.to_json`` /
``from_json``, ``pending_digests`` and digest-bound ``Runner.resume``.

Offline: every model here is a scripted ``FakeModel``. To use a real model, set
the model argument of each agent, for example:
# model="openai:gpt-4.1-mini"
"""

from nexura_agents import Agent, FakeModel, Runner, ToolCall, tool
from nexura_agents.types import RunInterruption

REFUNDS: list[tuple[str, int]] = []


@tool(requires_approval=True)
def issue_refund(order_id: str, amount: int) -> str:
    """Refund an order. Needs human approval."""
    REFUNDS.append((order_id, amount))
    return f"refunded {amount} for {order_id}"


def build_agents(resumed: bool = False) -> Agent:
    """Build the agent graph.

    The scripted models are stateful, so a "restarted process" gets fresh ones
    that skip the turns already taken before the pause. A real model needs no
    such trick: the saved interruption carries the conversation state.
    """
    billing_script: list = ["Your refund of 40 for order A1 is on its way."]
    if not resumed:
        billing_script.insert(
            0, [ToolCall(id="r1", name="issue_refund", arguments={"order_id": "A1", "amount": 40})]
        )
    billing = Agent(
        name="Billing",
        description="Handles refunds and invoices.",
        instructions="You handle billing. Refund only with the tool.",
        model=FakeModel(responses=billing_script),
        tools=[issue_refund],
    )
    tech = Agent(
        name="Tech",
        description="Handles technical problems.",
        instructions="You fix technical issues.",
        model=FakeModel(responses=["Try restarting the router."]),
    )
    triage_script: list = []
    if not resumed:
        triage_script.append(
            [ToolCall(id="h1", name="transfer_to_billing", arguments={"reason": "refund"})]
        )
    return Agent(
        name="Triage",
        instructions="Route to Billing for money, Tech for technical problems.",
        model=FakeModel(responses=triage_script or ["unused"]),
        handoffs=[billing, tech],
    )


def main() -> None:
    runner = Runner()
    triage = build_agents()
    paused = runner.run(triage, "Please refund order A1, it arrived broken.")
    assert paused.status == "paused" and REFUNDS == []
    interruption = paused.interruption
    print("agent path:", interruption.agent_path)
    digests = interruption.pending_digests()
    call = interruption.pending[0]
    print("pending:", call.name, call.arguments)

    saved = interruption.to_json()  # store anywhere; this is a str
    print("saved interruption bytes:", len(saved))

    # Later, in another process: rebuild the agents and restore the interruption.
    restored = RunInterruption.from_json(saved)
    assert restored.pending_digests() == digests
    final = runner.resume(build_agents(resumed=True), restored, approve=digests)
    assert final.status == "completed"
    assert REFUNDS == [("A1", 40)]
    print("final:", final.final_output)
    print("REFUND APPROVED AND EXECUTED:", REFUNDS[0])


if __name__ == "__main__":
    main()
