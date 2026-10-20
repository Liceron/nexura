"""Workflow composition: sub-workflows as steps, parallel branches, and map-over-items."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from nexura_runtime.errors import WorkflowError, WorkflowExecutionError, WorkflowValidationError

from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
    interrupt,
)


@pytest.fixture(params=["memory", "sqlite"])
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Any]:
    s: Any = (
        MemoryWorkflowStore()
        if request.param == "memory"
        else SQLiteWorkflowStore(tmp_path / "wf.sqlite3")
    )
    yield s
    await s.close()


def chain(name: str, *nodes: WorkflowNode) -> WorkflowSpec:
    """Link nodes in order through default_transition; the last one is terminal."""
    linked = {}
    for i, node in enumerate(nodes):
        node.default_transition = nodes[i + 1].id if i + 1 < len(nodes) else None
        linked[node.id] = node
    return WorkflowSpec(
        name=name, entry_node=nodes[0].id, nodes=linked, terminal_nodes=[nodes[-1].id]
    )


def kyc_registry(log: list[str]) -> ModuleRegistry:
    registry = ModuleRegistry()

    @registry.register_handler("check_pan")
    def check_pan(state: dict[str, Any]) -> dict[str, Any]:
        log.append(f"pan:{state['pan']}")
        return {"pan_ok": state["pan"].startswith("ABC")}

    @registry.register_handler("score")
    def score(state: dict[str, Any]) -> dict[str, Any]:
        return {"risk": "low" if state["pan_ok"] else "high"}

    @registry.register_handler("ask_officer")
    def ask_officer(state: dict[str, Any]) -> dict[str, Any]:
        return {"officer_note": interrupt({"question": "Anything to add?", "risk": state["risk"]})}

    @registry.register_handler("open_account")
    def open_account(state: dict[str, Any]) -> dict[str, Any]:
        log.append(f"open:{state['kyc_risk']}")
        return {"account": "ACC-1"}

    @registry.register_handler("noop")
    def noop(state: dict[str, Any]) -> dict[str, Any]:
        return {}

    return registry


KYC = chain(
    "kyc",
    WorkflowNode(id="pan", handler="check_pan"),
    WorkflowNode(id="score", handler="score"),
)


def onboarding(
    kyc: WorkflowSpec = KYC, output_mapping: dict[str, Any] | None = None, **sub: Any
) -> WorkflowSpec:
    return chain(
        "onboarding",
        WorkflowNode(
            id="kyc",
            handler="",
            subgraph=kyc,
            input_mapping={"pan": "customer_pan"},
            output_mapping=output_mapping or {"risk": "kyc_risk"},
            **sub,
        ),
        WorkflowNode(id="open", handler="open_account"),
    )


# --------------------------------------------------------------------------- subgraphs


async def test_subgraph_runs_as_one_step_with_mapped_inputs_and_outputs(store: Any) -> None:
    log: list[str] = []
    engine = WorkflowEngine(onboarding(), kyc_registry(log), store=store)
    result = await engine.run({"customer_pan": "ABCDE1234F"}, workflow_run_id="ob-1")

    assert result.status == "completed"
    assert result.state == {"customer_pan": "ABCDE1234F", "kyc_risk": "low", "account": "ACC-1"}
    assert result.step_count == 2  # the sub-workflow counts as one parent step
    assert log == ["pan:ABCDE1234F", "open:low"]

    # The sub-workflow is a real run with its own inspectable history.
    child = await engine.history("ob-1/kyc#1")
    assert [c.current_node_id for c in child] == ["score", None]
    assert child[-1].state == {"pan": "ABCDE1234F", "pan_ok": True, "risk": "low"}


async def test_subgraph_without_mappings_returns_only_what_it_changed() -> None:
    spec = chain(
        "outer",
        WorkflowNode(id="kyc", handler="", subgraph=KYC),
        WorkflowNode(id="done", handler="noop"),
    )
    result = await WorkflowEngine(spec, kyc_registry([])).run({"pan": "XYZ", "other": 1})
    assert result.state == {"pan": "XYZ", "other": 1, "pan_ok": False, "risk": "high"}


async def test_question_inside_a_subgraph_pauses_the_parent_and_resumes_after_restart(
    store: Any,
) -> None:
    log: list[str] = []
    kyc = chain(
        "kyc",
        WorkflowNode(id="pan", handler="check_pan"),
        WorkflowNode(id="score", handler="score"),
        WorkflowNode(id="ask", handler="ask_officer"),
    )
    spec = onboarding(kyc, output_mapping={"risk": "kyc_risk", "officer_note": "note"})
    paused = await WorkflowEngine(spec, kyc_registry(log), store=store).run(
        {"customer_pan": "ABCDE1234F"}, workflow_run_id="ob-2"
    )
    assert paused.status == "paused" and paused.interrupt is not None
    assert paused.interrupt["node_id"] == "kyc/ask"  # path to the asking node
    assert paused.interrupt["value"] == {"question": "Anything to add?", "risk": "low"}

    # A fresh engine (as after a restart) answers on the parent run.
    done = await WorkflowEngine(spec, kyc_registry(log), store=store).resume_interrupt(
        "ob-2", paused.interrupt["interrupt_id"], "Verified in person"
    )
    assert done.status == "completed"
    assert done.state["note"] == "Verified in person" and done.state["account"] == "ACC-1"
    assert log.count("pan:ABCDE1234F") == 1  # finished sub-steps were not repeated

    with pytest.raises(WorkflowError):
        await WorkflowEngine(spec, kyc_registry(log), store=store).resume_interrupt(
            "ob-2", paused.interrupt["interrupt_id"], "again"
        )


async def test_approval_inside_a_nested_subgraph_surfaces_on_the_top_run(store: Any) -> None:
    log: list[str] = []
    inner = chain(
        "inner",
        WorkflowNode(id="pan", handler="check_pan"),
        WorkflowNode(id="score", handler="score", requires_approval=True),
    )
    middle = chain("middle", WorkflowNode(id="inner", handler="", subgraph=inner))
    spec = onboarding(middle)
    engine = WorkflowEngine(spec, kyc_registry(log), store=store)

    paused = await engine.run({"customer_pan": "ABCDE1234F"}, workflow_run_id="ob-3")
    assert paused.status == "paused" and paused.pending_approval is not None
    assert paused.pending_approval.node_id == "kyc/inner/score"
    assert paused.pending_approval.workflow_run_id == "ob-3"

    done = await engine.resume("ob-3", paused.pending_approval.approval_id, actor_role="admin")
    assert done.status == "completed" and done.state["kyc_risk"] == "low"


async def test_rejecting_inside_a_subgraph_rejects_both_runs(store: Any) -> None:
    kyc = chain(
        "kyc",
        WorkflowNode(id="pan", handler="check_pan"),
        WorkflowNode(id="score", handler="score", requires_approval=True),
    )
    engine = WorkflowEngine(onboarding(kyc), kyc_registry([]), store=store)
    paused = await engine.run({"customer_pan": "ABC"}, workflow_run_id="ob-4")
    assert paused.pending_approval is not None
    rejected = await engine.resume("ob-4", paused.pending_approval.approval_id, decision="reject")
    assert rejected.status == "rejected"
    assert (await store.get("ob-4/kyc#1")).status == "rejected"


async def test_failing_subgraph_fails_the_parent_and_retries_with_a_fresh_child(
    store: Any,
) -> None:
    calls: list[int] = []
    registry = kyc_registry([])

    @registry.register_handler("flaky_bureau")
    def flaky_bureau(state: dict[str, Any]) -> dict[str, Any]:
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("bureau timeout")
        return {"risk": "low"}

    bureau = chain("bureau", WorkflowNode(id="call", handler="flaky_bureau"))
    no_retry = onboarding(bureau)
    with pytest.raises(WorkflowExecutionError, match="bureau timeout"):
        await WorkflowEngine(no_retry, registry, store=store).run({}, workflow_run_id="f-1")
    assert (await store.get("f-1")).status == "failed"

    retried = onboarding(bureau, retry={"max_attempts": 2, "initial_delay": 0})
    result = await WorkflowEngine(retried, registry, store=store).run({}, workflow_run_id="f-2")
    assert result.status == "completed" and result.state["kyc_risk"] == "low"
    assert (await store.get("f-2/kyc#1")).status == "completed"  # the failed attempt above


async def test_fork_before_a_subgraph_starts_new_child_runs(store: Any) -> None:
    # Fork from the checkpoint before the sub-workflow, with a different PAN.
    spec = chain(
        "pre",
        WorkflowNode(id="prep", handler="noop"),
        *onboarding().nodes.values(),
    )
    pre_engine = WorkflowEngine(spec, kyc_registry([]), store=store)
    await pre_engine.run({"customer_pan": "ABC"}, workflow_run_id="p")
    forked = await pre_engine.fork("p", 0, state_update={"customer_pan": "ZZZ"}, new_run_id="p2")
    assert forked.state["kyc_risk"] == "high"
    assert (await store.get("p/kyc#2")).state["risk"] == "low"  # original child untouched
    assert (await store.get("p2/kyc#2")).state["risk"] == "high"


# --------------------------------------------------------------------------- parallel


def research_registry(seen: list[str]) -> ModuleRegistry:
    registry = ModuleRegistry()
    barrier = asyncio.Barrier(3)

    async def source(name: str, hits: list[str], cost: int, delay: float = 0) -> dict[str, Any]:
        # All three must be running at once to get past the barrier.
        await asyncio.wait_for(barrier.wait(), timeout=2)
        await asyncio.sleep(delay)
        seen.append(name)
        return {"sources": hits, "cost": cost, "by_source": {name: len(hits)}}

    async def web(state: dict[str, Any]) -> dict[str, Any]:
        # Finishes last, but is merged first.
        return await source("web", [f"web:{state['topic']}"], 3, delay=0.05)

    async def docs(state: dict[str, Any]) -> dict[str, Any]:
        return await source("docs", ["docs:a", "docs:b"], 1)

    async def crm(state: dict[str, Any]) -> dict[str, Any]:
        out = await source("crm", [], 2)
        return {**out, "crm_customer": "Asha"}

    for name, fn in (("web", web), ("docs", docs), ("crm", crm)):
        registry.register(name, fn)
    registry.register("summarise", lambda s: {"summary": f"{len(s['sources'])} sources"})
    return registry


REDUCE = {"sources": "extend", "cost": "sum", "by_source": "merge"}


async def test_parallel_branches_run_concurrently_and_merge_with_reducers(store: Any) -> None:
    seen: list[str] = []
    spec = chain(
        "research",
        WorkflowNode(id="gather", handler="", parallel=["web", "docs", "crm"], reducers=REDUCE),
        WorkflowNode(id="sum", handler="summarise"),
    )
    result = await WorkflowEngine(spec, research_registry(seen), store=store).run(
        {"topic": "refunds", "cost": 10}
    )
    assert result.status == "completed"
    assert seen[-1] == "web"  # web finished last...
    assert result.state["sources"] == ["web:refunds", "docs:a", "docs:b"]  # ...but merges first
    assert result.state["cost"] == 16  # reducers fold into the existing value
    assert result.state["by_source"] == {"web": 1, "docs": 2, "crm": 0}
    assert result.state["crm_customer"] == "Asha"  # written by one branch: no reducer needed
    assert result.state["summary"] == "3 sources"
    assert result.step_count == 2


async def test_two_branches_writing_one_key_without_a_reducer_is_an_error() -> None:
    spec = chain(
        "research",
        WorkflowNode(id="gather", handler="", parallel=["web", "docs", "crm"]),
    )
    with pytest.raises(WorkflowExecutionError, match="reducer"):
        await WorkflowEngine(spec, research_registry([])).run({"topic": "x"})


async def test_a_failing_branch_cancels_the_others_and_fails_the_step(store: Any) -> None:
    cancelled: list[str] = []
    registry = ModuleRegistry()

    async def slow(state: dict[str, Any]) -> dict[str, Any]:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.append("slow")
            raise
        return {}

    async def broken(state: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("search quota exceeded")

    registry.register("slow", slow)
    registry.register("broken", broken)
    spec = chain("p", WorkflowNode(id="p", handler="", parallel=["slow", "broken"]))
    with pytest.raises(WorkflowExecutionError, match="search quota exceeded"):
        await WorkflowEngine(spec, registry, store=store).run({}, workflow_run_id="p")
    assert cancelled == ["slow"]
    assert (await store.get("p")).status == "failed"


async def test_interrupt_inside_a_parallel_branch_fails_clearly() -> None:
    registry = ModuleRegistry()
    registry.register("asks", lambda s: {"x": interrupt("?")})
    registry.register("fine", lambda s: {"y": 1})
    spec = chain("p", WorkflowNode(id="p", handler="", parallel=["asks", "fine"]))
    with pytest.raises(WorkflowExecutionError, match="cannot be used inside parallel"):
        await WorkflowEngine(spec, registry, store=MemoryWorkflowStore()).run({})


# --------------------------------------------------------------------------- map_over


async def test_map_over_runs_once_per_item_in_order_with_a_concurrency_cap() -> None:
    running = 0
    peak = 0
    registry = ModuleRegistry()

    @registry.register_handler("classify")
    async def classify(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02 * (5 - state["ticket_index"]))  # later items finish first
        running -= 1
        ticket = state["ticket"]
        return {"labels": {"id": ticket["id"], "urgent": ticket["amount"] > 1000}}

    spec = chain(
        "triage",
        WorkflowNode(
            id="each",
            handler="classify",
            map_over="tickets",
            map_as="ticket",
            reducers={"labels": "append"},
            max_concurrency=2,
        ),
    )
    tickets = [{"id": f"T{i}", "amount": i * 600} for i in range(5)]
    result = await WorkflowEngine(spec, registry).run({"tickets": tickets})
    assert [label["id"] for label in result.state["labels"]] == ["T0", "T1", "T2", "T3", "T4"]
    assert [label["urgent"] for label in result.state["labels"]] == [
        False,
        False,
        True,
        True,
        True,
    ]
    assert peak == 2
    assert "ticket" not in result.state  # the per-item key does not leak into the state

    empty = await WorkflowEngine(spec, registry).run({"tickets": []})
    assert empty.state["labels"] == []
    with pytest.raises(WorkflowExecutionError, match="must be a list"):
        await WorkflowEngine(spec, registry).run({"tickets": "T1"})


async def test_map_over_a_subgraph_runs_one_child_per_item(store: Any) -> None:
    spec = chain(
        "batch",
        WorkflowNode(
            id="kyc",
            handler="",
            subgraph=KYC,
            map_over="pans",
            input_mapping={"pan": "item"},
            output_mapping={"risk": "risks"},
            reducers={"risks": "append"},
        ),
    )
    engine = WorkflowEngine(spec, kyc_registry([]), store=store)
    result = await engine.run({"pans": ["ABC1", "XYZ2", "ABC3"]}, workflow_run_id="b")
    assert result.state["risks"] == ["low", "high", "low"]
    assert (await store.get("b/kyc#1[1]")).state["pan"] == "XYZ2"


async def test_validation_rejects_bad_composite_steps() -> None:
    registry = research_registry([])
    gated = chain("g", WorkflowNode(id="a", handler="web", requires_approval=True))
    cases: list[tuple[WorkflowNode, str]] = [
        (WorkflowNode(id="n", handler="", subgraph=gated, parallel=["web"]), "not both"),
        (WorkflowNode(id="n", handler="web", subgraph=gated), "must not also set handler"),
        (WorkflowNode(id="n", handler="", parallel=["web", "nope"]), "unknown handler 'nope'"),
        (WorkflowNode(id="n", handler="", parallel=["web", "web"]), "distinct"),
        (WorkflowNode(id="n", handler="", parallel=["web"], map_over="x"), "map_over"),
        (WorkflowNode(id="n", handler="web", reducers={"a": "append"}), "only to parallel"),
        (WorkflowNode(id="n", handler="", parallel=["web"], reducers={"a": "avg"}), "unknown"),
        (WorkflowNode(id="n", handler="web", map_over="x", map_as="a b"), "map_as"),
        (WorkflowNode(id="n", handler="web", map_over="x", max_concurrency=0), "max_concurrency"),
        (WorkflowNode(id="n", handler="", subgraph=gated, map_over="x"), "approval gates"),
        (
            WorkflowNode(id="n", handler="", subgraph=chain("bad", WorkflowNode("z", "nope"))),
            "Sub-workflow in node 'n'",
        ),
    ]
    for node, message in cases:
        with pytest.raises(WorkflowValidationError, match=message):
            WorkflowEngine(chain("t", node), registry)


def test_composite_steps_round_trip_and_change_the_hash() -> None:
    spec = chain(
        "outer",
        WorkflowNode(id="kyc", handler="", subgraph=KYC, output_mapping={"risk": "r"}),
        WorkflowNode(id="fan", handler="", parallel=["a", "b"], reducers={"x": "append"}),
        WorkflowNode(id="each", handler="h", map_over="items", map_as="row", max_concurrency=4),
    )
    again = WorkflowSpec.from_json(spec.to_json())
    assert again.canonical_hash() == spec.canonical_hash()
    assert again.nodes["kyc"].subgraph is not None
    assert again.nodes["kyc"].subgraph.canonical_hash() == KYC.canonical_hash()
    assert again.nodes["fan"].parallel == ["a", "b"]
    assert again.nodes["each"].map_as == "row" and again.nodes["each"].max_concurrency == 4

    # Editing the sub-workflow changes the parent's identity, so old runs cannot resume
    # against a silently different child.
    edited = chain("kyc", WorkflowNode(id="pan", handler="check_pan"))
    spec.nodes["kyc"].subgraph = edited
    assert spec.canonical_hash() != again.canonical_hash()

    plain = WorkflowNode(id="n", handler="h").to_dict()
    assert not {"subgraph", "parallel", "map_over", "map_as", "reducers"} & set(plain)


# --------------------------------------------------------------------------- B1 regressions


async def test_retrying_a_sub_workflow_with_a_payment_inside_is_refused() -> None:
    from nexura_agents.contracts import BaseModule, ExecutionContext, ModuleManifest, ModuleResult

    class SendPayment(BaseModule):
        manifest = ModuleManifest(
            module_id="pay.send",
            version="1.0.0",
            kind="tool",
            title="Send",
            description="Moves money.",
            effect_policy="non_idempotent_write",
        )

        async def invoke(self, ctx: ExecutionContext, inputs: Any, cfg: Any) -> ModuleResult:
            return ModuleResult(status="success")

    registry = kyc_registry([])
    registry.register("pay", SendPayment())
    inner = chain(
        "inner", WorkflowNode(id="pay", handler="pay"), WorkflowNode(id="s", handler="noop")
    )
    nested = chain("nested", WorkflowNode(id="inner", handler="", subgraph=inner))
    for child in (inner, nested):
        spec = chain(
            "top", WorkflowNode(id="sub", handler="", subgraph=child, retry={"max_attempts": 3})
        )
        with pytest.raises(WorkflowValidationError, match="non-idempotent"):
            WorkflowEngine(spec, registry)
    # Without the retry it is allowed.
    WorkflowEngine(chain("top", WorkflowNode(id="sub", handler="", subgraph=nested)), registry)


async def test_interrupt_deep_inside_a_mapped_sub_workflow_fails_instead_of_hanging() -> None:
    registry = kyc_registry([])
    registry.register("asks", lambda s: {"x": interrupt("?")})
    deepest = chain("deepest", WorkflowNode(id="ask", handler="asks"))
    middle = chain("middle", WorkflowNode(id="g", handler="", subgraph=deepest))
    spec = chain("top", WorkflowNode(id="m", handler="", subgraph=middle, map_over="items"))
    engine = WorkflowEngine(spec, registry, store=MemoryWorkflowStore())
    with pytest.raises(WorkflowExecutionError, match="cannot be used inside parallel"):
        await engine.run({"items": [1, 2]})


async def test_mapped_sub_workflow_writes_of_unchanged_values_still_reach_reducers() -> None:
    registry = ModuleRegistry()
    registry.register("vote", lambda s: {"votes": 1})
    child = chain("child", WorkflowNode(id="vote", handler="vote"))
    spec = chain(
        "top",
        WorkflowNode(
            id="each", handler="", subgraph=child, map_over="voters", reducers={"votes": "sum"}
        ),
    )
    # The state already holds votes=1, so each item's write equals its input value.
    result = await WorkflowEngine(spec, registry).run({"voters": ["a", "b", "c"], "votes": 1})
    assert result.state["votes"] == 4
