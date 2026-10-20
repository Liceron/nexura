"""P35 A4: replay compares the completed output / final state and records handoffs."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from nexura_agents import Agent, FakeModel, Runner, Tool, ToolCall
from nexura_agents.handoffs import Handoff
from nexura_agents.repro import (
    ReproBundle,
    ReproRecorder,
    export_repro_bundle,
    replay_repro_bundle,
)
from pydantic import BaseModel

from nexura_runtime.cli.main import main
from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)


class Invoice(BaseModel):
    amount: int
    currency: str


def _strip_new_fields(bundle: ReproBundle) -> ReproBundle:
    """The shape written before A4: no output/state, no per-agent tags."""
    data = json.loads(json.dumps(bundle.data))
    for key in ("output", "output_digest", "state", "state_digest"):
        data["expected_outcome"].pop(key, None)
    for item in data["model_responses"] + data["tool_fixtures"]:
        item.pop("agent", None)
    data.pop("recording_digest")
    return export_repro_bundle(data)


async def _invoice_recording(*, scale: bool) -> ReproRecorder:
    def times_ten(output: Invoice) -> Invoice:
        return Invoice(amount=output.amount * 10, currency=output.currency)

    agent = Agent(
        "invoicer",
        "Extract the invoice",
        FakeModel(['{"amount": 500, "currency": "INR"}']),
        output_type=Invoice,
        # Validators are code: they are not in the bundle, so replay skips them.
        output_validators=[times_ten] if scale else [],
    )
    recorder = ReproRecorder()
    result = await recorder.run_agent(Runner(), agent, "invoice text")
    assert result.output.amount == (5000 if scale else 500)
    return recorder


# --- 1. output and state divergence -------------------------------------------------


@pytest.mark.asyncio
async def test_completed_output_is_recorded_with_canonical_digest() -> None:
    bundle = export_repro_bundle(await _invoice_recording(scale=False))
    outcome = bundle.data["expected_outcome"]
    assert outcome["output"] == {"amount": 500, "currency": "INR"}
    assert len(outcome["output_digest"]) == 64
    result = await replay_repro_bundle(bundle)
    assert result.reproduced, result.first_divergence
    assert result.notes == ()
    assert result.outcome["output"] == {"amount": 500, "currency": "INR"}


@pytest.mark.asyncio
async def test_different_completed_output_is_a_divergence(tmp_path: Path) -> None:
    # The recorded run's validator turned 500 into 5000; replay (no code) yields 500.
    path = tmp_path / "invoice.json"
    export_repro_bundle(await _invoice_recording(scale=True)).save(path)
    result = await replay_repro_bundle(ReproBundle.load(path))
    assert not result.reproduced
    assert result.outcome["status"] == "completed"
    assert result.first_divergence == "output differs at $.amount: 500 != 5000"


@pytest.mark.asyncio
async def test_text_output_change_is_a_divergence() -> None:
    recorder = ReproRecorder()
    await recorder.run_agent(Runner(), Agent("ok", "local", FakeModel(["done"])), "hi")
    data = json.loads(json.dumps(export_repro_bundle(recorder).data))
    data["expected_outcome"]["output"] = "finished"
    data.pop("recording_digest")
    result = await replay_repro_bundle(export_repro_bundle(data))  # fingerprint recomputed
    assert not result.reproduced
    assert result.first_divergence == 'output differs at $: "done" != "finished"'


@pytest.mark.asyncio
async def test_edited_recorded_output_digest_is_refused() -> None:
    data = json.loads(json.dumps(export_repro_bundle(await _invoice_recording(scale=False)).data))
    data["expected_outcome"]["output_digest"] = "0" * 64
    # Recompute only the fingerprint, keeping the inconsistent output digest.
    from nexura_agents.repro import _digest

    data.pop("recording_digest")
    data["recording_digest"] = _digest(data)
    result = await replay_repro_bundle(ReproBundle(data))
    assert not result.reproduced
    assert result.first_divergence == "Recorded output digest mismatch"


def _workflow_engine(calls: list[str]) -> WorkflowEngine:
    registry = ModuleRegistry()

    @registry.register_handler("price")
    async def price(state: dict[str, Any]) -> dict[str, Any]:
        calls.append("price")
        return {"total": state["qty"] * 5, "lines": [{"sku": "a", "qty": state["qty"]}]}

    @registry.register_handler("tag")
    async def tag(state: dict[str, Any]) -> dict[str, Any]:
        calls.append("tag")
        return {"tag": "priced"}

    spec = WorkflowSpec(
        name="pricing",
        entry_node="price",
        nodes={
            "price": WorkflowNode(id="price", handler="price", default_transition="tag"),
            "tag": WorkflowNode(id="tag", handler="tag"),
        },
    )
    return WorkflowEngine(spec, registry, store=MemoryWorkflowStore())


@pytest.mark.asyncio
async def test_workflow_final_state_is_compared() -> None:
    calls: list[str] = []
    recorder = ReproRecorder()
    await recorder.run_workflow(_workflow_engine(calls), {"qty": 2})
    bundle = export_repro_bundle(recorder)
    assert bundle.data["expected_outcome"]["state"]["total"] == 10
    assert "state_digest" in bundle.data["expected_outcome"]
    result = await replay_repro_bundle(bundle)
    assert result.reproduced, result.first_divergence
    assert calls == ["price", "tag"]  # replay ran no handler

    data = json.loads(json.dumps(bundle.data))
    data["expected_outcome"]["state"]["lines"][0]["qty"] = 7
    data.pop("recording_digest")
    diverged = await replay_repro_bundle(export_repro_bundle(data))
    assert not diverged.reproduced
    assert diverged.first_divergence == "state differs at $.lines[0].qty: 2 != 7"
    assert calls == ["price", "tag"]


@pytest.mark.asyncio
async def test_workflow_state_divergence_from_changed_engine_behaviour() -> None:
    # A framework change that alters how node output lands in state is caught.
    recorder = ReproRecorder()
    await recorder.run_workflow(_workflow_engine([]), {"qty": 3})
    bundle = export_repro_bundle(recorder)
    original_run = WorkflowEngine.run

    async def drifting_run(self: WorkflowEngine, *args: Any, **kwargs: Any) -> Any:
        result = await original_run(self, *args, **kwargs)
        result.state["tag"] = "drifted"
        return result

    with patch.object(WorkflowEngine, "run", drifting_run):
        result = await replay_repro_bundle(bundle)
    assert not result.reproduced
    assert result.first_divergence == 'state differs at $.tag: "drifted" != "priced"'


# --- 2. legacy bundles ----------------------------------------------------------------


def test_legacy_agent_bundle_replays_and_says_output_was_not_recorded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def prepare() -> tuple[ReproBundle, Any]:
        legacy = _strip_new_fields(export_repro_bundle(await _invoice_recording(scale=True)))
        return legacy, await replay_repro_bundle(legacy)

    legacy, result = asyncio.run(prepare())
    assert "output" not in legacy.data["expected_outcome"]
    # The output changed (500 vs 5000), but the old bundle cannot show it.
    assert result.reproduced, result.first_divergence
    assert len(result.notes) == 1
    assert "did not record the output" in result.notes[0]

    path = tmp_path / "legacy.json"
    legacy.save(path)
    assert "output_digest" not in path.read_text()
    assert main(["repro", "run", str(path)]) == 0
    captured = capsys.readouterr()
    assert "reproduced: True" in captured.out
    assert "did not record the output" in captured.err


@pytest.mark.asyncio
async def test_legacy_failed_and_workflow_bundles_still_replay() -> None:
    async def lookup(item: int) -> str:
        raise ValueError("lookup failed")

    agent = Agent(
        "recorded",
        "Use lookup",
        FakeModel([[ToolCall("a", "lookup", {"item": 1})]]),
        tools=[Tool("lookup", lookup, max_retries=0)],
    )
    recorder = ReproRecorder()
    with pytest.raises(Exception, match="lookup failed"):
        await recorder.run_agent(Runner(), agent, "input")
    legacy = _strip_new_fields(export_repro_bundle(recorder))
    result = await replay_repro_bundle(legacy)
    assert result.reproduced, result.first_divergence
    assert result.outcome["failing_step"] == "lookup"

    workflow = ReproRecorder()
    await workflow.run_workflow(_workflow_engine([]), {"qty": 1})
    legacy_workflow = _strip_new_fields(export_repro_bundle(workflow))
    replayed = await replay_repro_bundle(legacy_workflow)
    assert replayed.reproduced, replayed.first_divergence
    assert "did not record the final state" in replayed.notes[0]


def test_literal_pre_a4_bundle_document_loads_and_replays(tmp_path: Path) -> None:
    """A document in the exact pre-A4 shape (hand-written, no new keys at all)."""
    from nexura_agents.repro import _digest

    data = {
        "schema_version": 1,
        "created_at": "2026-10-01T00:00:00+00:00",
        "framework_version": "0.0.0",
        "run_kind": "agent",
        "inputs": "hello",
        "spec": {
            "name": "ok",
            "instructions": "local",
            "max_turns": 10,
            "max_tool_calls": 50,
            "output_type": None,
            "output_retries": 1,
            "tool_use_behavior": "run_llm_again",
            "tools": [],
        },
        "model_responses": [{"message": {"role": "assistant", "content": "done"}, "usage": None}],
        "tool_fixtures": [],
        "approvals": [],
        "operations": ["run"],
        "budget_config": None,
        "usage_limits": None,
        "expected_outcome": {
            "status": "completed",
            "error_type": None,
            "error_message": None,
            "failing_step": None,
        },
    }
    data["recording_digest"] = _digest(data)
    path = tmp_path / "old.json"
    path.write_text(json.dumps(data))
    assert main(["repro", "run", str(path)]) == 0


# --- 3. handoffs ------------------------------------------------------------------------


def _support_graph(side_effects: list[str], *, approval: bool = False) -> Agent:
    async def refund(order: str, amount: int) -> dict[str, Any]:
        side_effects.append(f"refund {order}")
        return {"refunded": amount, "order": order}

    billing = Agent(
        "billing",
        "Handle refunds",
        FakeModel(
            [
                [ToolCall("r1", "refund", {"order": "A-1", "amount": 500})],
                '{"amount": 500, "currency": "INR"}',
            ]
        ),
        tools=[Tool("refund", refund, requires_approval=approval)],
        output_type=Invoice,
    )

    def noted(ctx: Any, reason: str) -> None:
        side_effects.append(f"handoff: {reason}")

    return Agent(
        "triage",
        "Route the customer",
        FakeModel([[ToolCall("h1", "transfer_to_billing", {"reason": "refund request"})]]),
        handoffs=[Handoff(billing, on_handoff=noted)],
    )


@pytest.mark.asyncio
async def test_handoff_run_records_and_replays_per_agent(tmp_path: Path) -> None:
    effects: list[str] = []
    recorder = ReproRecorder()
    result = await recorder.run_agent(Runner(), _support_graph(effects), "I want a refund")
    assert result.agent_path == ["triage", "billing"]
    assert effects == ["handoff: refund request", "refund A-1"]

    path = tmp_path / "handoff.json"
    export_repro_bundle(recorder).save(path)
    data = json.loads(path.read_text())
    assert [r["agent"] for r in data["model_responses"]] == ["triage", "billing", "billing"]
    assert [(f["agent"], f["name"]) for f in data["tool_fixtures"]] == [
        ("triage", "on_handoff:transfer_to_billing"),
        ("billing", "refund"),
    ]
    assert data["spec"]["handoffs"][0]["agent"] == "billing"
    assert data["spec"]["agents"]["billing"]["tools"][0]["schema"]["function"]["name"] == "refund"

    with patch.object(Tool, "execute", side_effect=AssertionError("real tool called")):
        replay = await replay_repro_bundle(ReproBundle.load(path))
    assert replay.reproduced, replay.first_divergence
    assert replay.outcome["output"] == {"amount": 500, "currency": "INR"}
    assert effects == ["handoff: refund request", "refund A-1"]  # nothing re-ran


@pytest.mark.asyncio
async def test_handoff_with_approval_in_target_agent_replays() -> None:
    effects: list[str] = []
    recorder = ReproRecorder()
    paused = await recorder.run_agent(Runner(), _support_graph(effects, approval=True), "refund")
    assert paused.status == "paused"
    done = await recorder.resume_agent(paused.interruption, approve=True)
    assert done.output.amount == 500
    replay = await replay_repro_bundle(export_repro_bundle(recorder))
    assert replay.reproduced, replay.first_divergence
    assert effects == ["handoff: refund request", "refund A-1"]


@pytest.mark.asyncio
async def test_cyclic_handoffs_and_failure_in_second_agent() -> None:
    async def lookup(q: str) -> str:
        raise ValueError("lookup exploded")

    # front hands to back, back hands back, and front's second turn calls a failing tool.
    front = Agent(
        "front",
        "local",
        FakeModel(
            [
                [ToolCall("h1", "transfer_to_back", {})],
                [ToolCall("l1", "lookup", {"q": "x"})],
            ]
        ),
        tools=[Tool("lookup", lookup, max_retries=0)],
    )
    back = Agent("back", "local", FakeModel([[ToolCall("h2", "transfer_to_front", {})]]))
    front.add_handoffs(back)
    back.add_handoffs(front)
    recorder = ReproRecorder()
    with pytest.raises(Exception, match="lookup exploded"):
        await recorder.run_agent(Runner(), front, "go")
    bundle = export_repro_bundle(recorder)
    assert [r["agent"] for r in bundle.data["model_responses"]] == ["front", "back", "front"]
    replay = await replay_repro_bundle(bundle)
    assert replay.reproduced, replay.first_divergence
    assert replay.outcome["failing_step"] == "lookup"


@pytest.mark.asyncio
async def test_reply_recorded_for_another_agent_is_a_divergence() -> None:
    recorder = ReproRecorder()
    await recorder.run_agent(Runner(), _support_graph([]), "refund")
    data = json.loads(json.dumps(export_repro_bundle(recorder).data))
    data["model_responses"][1]["agent"] = "triage"
    data.pop("recording_digest")
    result = await replay_repro_bundle(export_repro_bundle(data))
    assert not result.reproduced
    assert result.first_divergence == (
        "Model response 1: recorded for agent 'triage', requested by 'billing'"
    )

    data = json.loads(json.dumps(export_repro_bundle(recorder).data))
    data["tool_fixtures"][1]["agent"] = "triage"
    data.pop("recording_digest")
    result = await replay_repro_bundle(export_repro_bundle(data))
    assert not result.reproduced
    assert result.first_divergence == (
        "Fixture 1: recorded for agent 'triage', called by 'billing' at refund"
    )


@pytest.mark.asyncio
async def test_structured_handoff_input_replays() -> None:
    class Ticket(BaseModel):
        priority: int

    target = Agent("desk", "local", FakeModel(["ticket filed"]))
    start = Agent(
        "intake",
        "local",
        FakeModel([[ToolCall("h", "transfer_to_desk", {"priority": 2})]]),
        handoffs=[Handoff(target, input_type=Ticket)],
    )
    recorder = ReproRecorder()
    await recorder.run_agent(Runner(), start, "file it")
    bundle = export_repro_bundle(recorder)
    assert bundle.data["spec"]["handoffs"][0]["input_schema"]["title"] == "Ticket"
    replay = await replay_repro_bundle(bundle)
    assert replay.reproduced, replay.first_divergence
    assert replay.outcome["output"] == "ticket filed"


@pytest.mark.asyncio
async def test_handoff_graph_refusals_are_clear() -> None:
    twin = Agent("same", "local", FakeModel(["x"]))
    start = Agent("same", "local", FakeModel(["x"]), handoffs=[twin])
    with pytest.raises(ValueError, match="unique agent names; two agents are named 'same'"):
        await ReproRecorder().run_agent(Runner(), start, "hi")

    dynamic = Agent("dyn", lambda ctx: "dynamic", FakeModel(["x"]))
    start = Agent("root", "local", FakeModel(["x"]), handoffs=[dynamic])
    with pytest.raises(ValueError, match="agent 'dyn' has an instructions function"):
        await ReproRecorder().run_agent(Runner(), start, "hi")
