# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""B43 review: the liveness and design gaps the review listed without repros, plus edges of
the finding fixes (positional binding, canonical keys, module decider, workflow expiry).

Offline: models are ``StreamingFakeProvider`` (a scripted local fake); approvals use a
disposable local SQLite database or ``":memory:"``; workflows use local stores only.
"""

from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path
from typing import Any

import pytest
from nexura_agents.contracts.result import ModuleResult
from nexura_runtime.contracts import ExecutionContext
from nexura_runtime.contracts.canonical import canonical_digest
from nexura_runtime.providers.fake import StreamingFakeProvider
from nexura_runtime.tools import tool
from nexura_runtime.types import Message, ModelResponse, ToolCall

from nexura_runtime.runtime.agents import AgentCoordinator, AgentDefinition
from nexura_runtime.runtime.agents.definition import current_agent_context
from nexura_runtime.runtime.approvals import (
    ApprovalClaimConflictError,
    ApprovalDecisionRecord,
    ApprovalError,
    ApprovalExpiredError,
    ApprovalGate,
    ApprovalIdentityMismatchError,
    ApprovalModule,
    ApprovalPolicyManager,
    ApprovalRequest,
    ApprovalRequiredError,
)
from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    PendingApproval,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
    WorkflowValidationError,
    interrupt,
)


def _approve(manager: ApprovalPolicyManager, workspace: str, approval_id: str) -> None:
    req = manager.get_request(workspace, approval_id)
    assert req is not None
    manager.submit_decision(
        workspace,
        approval_id,
        ApprovalDecisionRecord(
            approval_id=approval_id,
            workspace_id=workspace,
            decided_by_principal_id="reviewer",
            decided_by_role="admin",
            decision="approved",
            expected_payload_digest=req.payload_digest,
        ),
    )


def _rows(manager: ApprovalPolicyManager) -> int:
    with manager._connection() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0])


def _tool_call(name: str, arguments: dict[str, Any], call_id: str = "call_1") -> ModelResponse:
    return ModelResponse(
        message=Message(
            role="assistant",
            tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
        )
    )


def _final(text: str = "done") -> ModelResponse:
    return ModelResponse(message=Message(role="assistant", content=text))


# ---------------------------------------------------------------------------
# Coordinator tool approvals (gap: they could never be granted)
# ---------------------------------------------------------------------------


def _coordinator(
    tmp_path: Path, responses: list[ModelResponse]
) -> tuple[AgentCoordinator, AgentDefinition, ApprovalPolicyManager, list[int]]:
    sent: list[int] = []

    @tool
    def wire(amount: int) -> str:
        sent.append(amount)
        return f"sent {amount}"

    agent = AgentDefinition(
        name="payer",
        model=StreamingFakeProvider(canned_responses=responses),
        tools=[wire],
        approval_required=True,
    )
    manager = ApprovalPolicyManager(tmp_path / "coord.db")
    coord = AgentCoordinator(agents=[agent], approval_gate=ApprovalGate(manager))
    return coord, agent, manager, sent


CTX = ExecutionContext(workspace_id="ws", principal_id="p", run_id="coord-run", node_id="n")


async def test_coordinator_tool_approval_is_grantable_and_single_use(tmp_path: Path) -> None:
    coord, _agent, manager, sent = _coordinator(
        tmp_path,
        [
            _tool_call("wire", {"amount": 5}),
            _tool_call("wire", {"amount": 5}),
            _final(),
            _tool_call("wire", {"amount": 5}),
        ],
    )
    with pytest.raises(ApprovalRequiredError) as paused:
        await coord.run_async("payer", "pay", context=CTX)
    approval_id = paused.value.approval_id
    assert sent == []
    _approve(manager, "ws", approval_id)

    # Re-running the same run finds the same request (stable id) and claims it.
    result = await coord.run_async("payer", "pay", context=CTX)
    assert result.status == "completed"
    assert sent == [5]
    assert _rows(manager) == 1

    # The approval is single use: replaying the run does not dispatch the call again.
    with pytest.raises(ApprovalClaimConflictError):
        await coord.run_async("payer", "pay", context=CTX)
    assert sent == [5]


async def test_coordinator_approval_is_bound_to_the_agent_definition(tmp_path: Path) -> None:
    coord, agent, manager, sent = _coordinator(
        tmp_path, [_tool_call("wire", {"amount": 5}), _tool_call("wire", {"amount": 5})]
    )
    with pytest.raises(ApprovalRequiredError) as paused:
        await coord.run_async("payer", "pay", context=CTX)
    _approve(manager, "ws", paused.value.approval_id)
    agent.instructions = "Send everything to a new account."  # edited after the request
    with pytest.raises(ApprovalIdentityMismatchError) as refused:
        await coord.run_async("payer", "pay", context=CTX)
    assert "spec_digest" in refused.value.details["mismatched_fields"]
    assert sent == []


async def test_coordinator_changed_arguments_are_refused(tmp_path: Path) -> None:
    coord, _agent, manager, sent = _coordinator(
        tmp_path, [_tool_call("wire", {"amount": 5}), _tool_call("wire", {"amount": 9_000})]
    )
    with pytest.raises(ApprovalRequiredError) as paused:
        await coord.run_async("payer", "pay", context=CTX)
    _approve(manager, "ws", paused.value.approval_id)
    # A different call on the re-run is not covered: it asks for its own approval.
    with pytest.raises(ApprovalRequiredError) as again:
        await coord.run_async("payer", "pay", context=CTX)
    assert again.value.approval_id != paused.value.approval_id
    assert sent == []
    first = manager.get_request("ws", paused.value.approval_id)
    assert first is not None and first.claimed_by is None


async def test_coordinator_approval_survives_a_new_provider_call_id(tmp_path: Path) -> None:
    """Real providers mint a new tool-call id per response; the approval still applies."""
    coord, _agent, manager, sent = _coordinator(
        tmp_path,
        [
            _tool_call("wire", {"amount": 5}, call_id="call_first"),
            _tool_call("wire", {"amount": 5}, call_id="call_second"),
            _final(),
        ],
    )
    with pytest.raises(ApprovalRequiredError) as paused:
        await coord.run_async("payer", "pay", context=CTX)
    _approve(manager, "ws", paused.value.approval_id)
    result = await coord.run_async("payer", "pay", context=CTX)
    assert result.status == "completed"
    assert sent == [5]
    assert _rows(manager) == 1


# ---------------------------------------------------------------------------
# Subagents (gaps: nested inheritance of approval_id, no spec binding)
# ---------------------------------------------------------------------------


async def test_nested_subagent_asks_for_its_own_approval(tmp_path: Path) -> None:
    manager = ApprovalPolicyManager(tmp_path / "sub.db")
    gate = ApprovalGate(manager)
    inner = AgentDefinition(
        name="inner",
        model=StreamingFakeProvider(canned_responses=[_final("inner done")]),
        approval_required=True,
    )
    inner_tool = inner.as_tool(approval_gate=gate)
    outer = AgentDefinition(
        name="outer",
        model=StreamingFakeProvider(
            canned_responses=[_tool_call("delegate_to_inner", {"query": "go deeper"})]
        ),
        tools=[inner_tool],
        approval_required=True,
    )
    outer_tool = outer.as_tool(approval_gate=gate)
    parent = ExecutionContext(workspace_id="ws", run_id="root", node_id="root-node")
    token = current_agent_context.set(parent)
    try:
        with pytest.raises(ApprovalRequiredError) as first:
            await outer_tool.arun(query="start")
        outer_id = first.value.approval_id
    finally:
        current_agent_context.reset(token)
    _approve(manager, "ws", outer_id)
    # The caller carries the outer approval in its context attributes. The outer delegation
    # claims it; the nested one must pause for a new approval instead of inheriting it
    # (which used to fail as MutatedArgumentsError / ApprovalIdentityMismatchError).
    approved_parent = ExecutionContext(
        workspace_id="ws",
        run_id="root",
        node_id="root-node",
        attributes={"approval_id": outer_id},
    )
    token = current_agent_context.set(approved_parent)
    try:
        with pytest.raises(ApprovalRequiredError) as nested:
            await outer_tool.arun(query="start")
    finally:
        current_agent_context.reset(token)
    outer_req = manager.get_request("ws", outer_id)
    assert outer_req is not None and outer_req.claimed_by is not None
    assert nested.value.action_name == "delegate_to_inner"
    assert nested.value.approval_id != outer_id
    req = manager.get_request("ws", nested.value.approval_id)
    assert req is not None and req.status == "pending"


async def test_subagent_approval_is_bound_to_the_agent_definition(tmp_path: Path) -> None:
    manager = ApprovalPolicyManager(tmp_path / "sub.db")
    gate = ApprovalGate(manager)
    agent = AgentDefinition(
        name="banker",
        model=StreamingFakeProvider(canned_responses=[_final("moved")]),
        approval_required=True,
    )
    agent_tool = agent.as_tool(approval_gate=gate)
    parent = ExecutionContext(workspace_id="ws", run_id="root", node_id="root-node")
    token = current_agent_context.set(parent)
    try:
        with pytest.raises(ApprovalRequiredError) as paused:
            await agent_tool.arun(query="move funds")
        approval_id = paused.value.approval_id
        req = manager.get_request("ws", approval_id)
        assert req is not None and req.spec_digest == agent.definition_digest()
        _approve(manager, "ws", approval_id)
        agent.max_turns = 50  # the definition changed after the approval was requested
        with pytest.raises(ApprovalIdentityMismatchError):
            await agent_tool.arun(query="move funds", context_data={"approval_id": approval_id})
    finally:
        current_agent_context.reset(token)


def test_definition_digest_is_stable_and_covers_the_definition() -> None:
    def build(instructions: str) -> AgentDefinition:
        return AgentDefinition(
            name="a",
            instructions=instructions,
            model=StreamingFakeProvider(canned_responses=[]),
        )

    assert build("x").definition_digest() == build("x").definition_digest()
    assert build("x").definition_digest() != build("y").definition_digest()


# ---------------------------------------------------------------------------
# ApprovalGate: direct-call fallback, positional binding, canonical payloads
# ---------------------------------------------------------------------------


class _Transfer:
    name = "transfer"

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def run(self, amount: Any = None, *extra: Any, **kw: Any) -> str:
        self.calls.append((amount, extra, kw))
        return "ok"


def test_direct_call_fallback_does_not_share_approvals_between_tools(tmp_path: Path) -> None:
    manager = ApprovalPolicyManager(tmp_path / "direct.db")
    gate = ApprovalGate(manager)
    first = gate.wrap_tool(_Transfer())
    other = gate.wrap_tool(_Transfer())  # a different tool with the same name
    with pytest.raises(ApprovalRequiredError) as paused:
        first.run(amount=1)
    approval_id = paused.value.approval_id
    _approve(manager, "default", approval_id)
    with pytest.raises(ApprovalIdentityMismatchError):
        other.run(amount=1, approval_id=approval_id)
    assert other.calls == []
    assert first.run(amount=1, approval_id=approval_id) == "ok"
    assert first.calls == [(1, (), {})]


def test_positional_and_keyword_calls_bind_the_same_payload(tmp_path: Path) -> None:
    manager = ApprovalPolicyManager(tmp_path / "bind.db")
    gate = ApprovalGate(manager)
    ctx = ExecutionContext(workspace_id="local", run_id="r", node_id="n")
    tool_obj = gate.wrap_tool(_Transfer())
    with pytest.raises(ApprovalRequiredError) as paused:
        tool_obj.run(7, "memo", note="x", context=ctx)
    approval_id = paused.value.approval_id
    req = manager.get_request("local", approval_id)
    assert req is not None
    # The reviewer sees every argument, positional ones by parameter name.
    assert req.action_payload == {"amount": 7, "*extra": ["memo"], "note": "x"}
    _approve(manager, "local", approval_id)
    with pytest.raises(ApprovalError):
        tool_obj.run(7, "other memo", note="x", context=ctx, approval_id=approval_id)
    assert tool_obj.run(7, "memo", note="x", context=ctx, approval_id=approval_id) == "ok"
    assert tool_obj.calls == [(7, ("memo",), {"note": "x"})]

    with pytest.raises(ApprovalRequiredError) as again:
        tool_obj.run(amount=3, context=ctx)
    with pytest.raises(ApprovalRequiredError) as again_positional:
        tool_obj.run(3, context=ctx)
    assert again.value.payload_digest == again_positional.value.payload_digest


def test_var_positional_and_same_named_keyword_never_share_a_payload(tmp_path: Path) -> None:
    """``run(1, "memo")`` and ``run(1, extra=["memo"])`` are different calls."""
    manager = ApprovalPolicyManager(tmp_path / "varpos.db")
    gate = ApprovalGate(manager)
    ctx = ExecutionContext(workspace_id="local", run_id="r", node_id="n")
    tool_obj = gate.wrap_tool(_Transfer())
    with pytest.raises(ApprovalRequiredError) as positional:
        tool_obj.run(1, "memo", context=ctx)
    with pytest.raises(ApprovalRequiredError) as keyword:
        tool_obj.run(1, extra=["memo"], context=ctx)
    assert positional.value.payload_digest != keyword.value.payload_digest
    # A keyword argument cannot pose as the *args entry.
    with pytest.raises(ApprovalError, match="ambiguous"):
        tool_obj.run(1, context=ctx, **{"*extra": ["memo"]})
    assert tool_obj.calls == []


def test_unbindable_or_uncanonical_arguments_are_refused_before_any_request(
    tmp_path: Path,
) -> None:
    manager = ApprovalPolicyManager(tmp_path / "refuse.db")
    gate = ApprovalGate(manager)
    ctx = ExecutionContext(workspace_id="local", run_id="r", node_id="n")

    class _Strict:
        name = "strict"

        def run(self, amount: int) -> str:
            return "ran"

    strict = gate.wrap_tool(_Strict())
    with pytest.raises(ApprovalError, match="signature"):
        strict.run(1, 2, context=ctx)
    loose = gate.wrap_tool(_Transfer())
    with pytest.raises(ApprovalError, match="canonicalised"):
        loose.run({1: "a"}, context=ctx)
    with pytest.raises(ApprovalError, match="canonicalised"):
        loose.run(amount=math.nan, context=ctx)
    with pytest.raises(ApprovalError, match="canonicalised"):
        gate.check_or_request_approval(ctx, "act", {"k": {2: "v"}})
    assert _rows(manager) == 0
    assert loose.calls == []


def test_canonical_digest_refuses_non_string_keys() -> None:
    with pytest.raises(TypeError, match="keys must be strings"):
        canonical_digest({1: "x"})
    with pytest.raises(TypeError):
        canonical_digest({"a": [{None: 1}]})
    assert canonical_digest({"1": "x"}) == canonical_digest({"1": "x"})


# ---------------------------------------------------------------------------
# ApprovalPolicyManager: create_request status, ":memory:" persistence, claim clock
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "approved"},
        {"status": "rejected"},
        {"status": "expired"},
        {"claimed_by": "worker"},
        {
            "decision": ApprovalDecisionRecord(
                approval_id="pre",
                workspace_id="local",
                decided_by_principal_id="x",
                decided_by_role="admin",
                decision="approved",
            )
        },
    ],
)
def test_create_request_refuses_a_pre_decided_row(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    manager = ApprovalPolicyManager(tmp_path / "pre.db")
    with pytest.raises(ApprovalError):
        manager.create_request(
            ApprovalRequest(
                approval_id="pre",
                workspace_id="local",
                run_id="r",
                expires_at=time.time() + 60,
                **overrides,
            )
        )
    assert manager.get_request("local", "pre") is None


async def test_memory_manager_keeps_one_database() -> None:
    manager = ApprovalPolicyManager(":memory:")
    gate = ApprovalGate(manager)
    ctx = ExecutionContext(workspace_id="local", run_id="r", node_id="n")
    with pytest.raises(ApprovalRequiredError) as paused:
        gate.check_or_request_approval(ctx, "act", {"v": 1})
    req = manager.get_request("local", paused.value.approval_id)
    assert req is not None and req.status == "pending"
    # The default ApprovalModule() (its own ":memory:" manager) works across calls.
    module = ApprovalModule()
    created = await module.invoke(ctx, {"operation": "create", "action_payload": {"x": 1}})
    fetched = await module.invoke(
        ctx, {"operation": "get", "approval_id": created.outputs["approval_id"]}
    )
    assert fetched.outputs["approval_status"] == "pending"


def test_claim_now_can_only_move_the_clock_forward(tmp_path: Path) -> None:
    manager = ApprovalPolicyManager(tmp_path / "clock.db")
    gate = ApprovalGate(manager)
    ctx = ExecutionContext(workspace_id="local", run_id="r", node_id="n")
    with pytest.raises(ApprovalRequiredError) as paused:
        gate.check_or_request_approval(ctx, "act", {"v": 1})
    approval_id = paused.value.approval_id
    _approve(manager, "local", approval_id)
    req = manager.get_request("local", approval_id)
    assert req is not None
    # A time in the past is ignored (the manager's clock is used): still claimable.
    assert (
        manager.claim_for_dispatch(
            "local", approval_id, "w", identity_digest=req.identity_digest, now=0.0
        )
        == "claimed"
    )


# ---------------------------------------------------------------------------
# ApprovalModule: decider comes from the context
# ---------------------------------------------------------------------------


async def test_approval_module_decide_takes_principal_and_role_from_context(
    tmp_path: Path,
) -> None:
    manager = ApprovalPolicyManager(tmp_path / "module.db")
    module = ApprovalModule(manager)
    ctx = ExecutionContext(workspace_id="ws", principal_id="alice", run_id="r", node_id="n")
    created = await module.invoke(
        ctx, {"operation": "create", "required_role": "director", "action_payload": {"x": 1}}
    )
    approval_id = created.outputs["approval_id"]

    # No role in the context: refused, whatever the inputs claim.
    with pytest.raises(ApprovalError, match="principal_role"):
        await module.invoke(
            ctx,
            {
                "operation": "decide",
                "approval_id": approval_id,
                "decision": "approved",
                "decided_by_role": "director",
            },
        )
    viewer = ExecutionContext(
        workspace_id="ws",
        principal_id="bob",
        run_id="r",
        node_id="n",
        attributes={"principal_role": "viewer"},
    )
    # Inputs that disagree with the context are refused.
    with pytest.raises(ApprovalError, match="does not match"):
        await module.invoke(
            viewer,
            {
                "operation": "decide",
                "approval_id": approval_id,
                "decision": "approved",
                "decided_by_role": "director",
            },
        )
    with pytest.raises(ApprovalError, match="does not match"):
        await module.invoke(
            viewer,
            {
                "operation": "decide",
                "approval_id": approval_id,
                "decision": "approved",
                "decided_by_principal_id": "dana",
            },
        )
    req = manager.get_request("ws", approval_id)
    assert req is not None and req.status == "pending"

    director = ExecutionContext(
        workspace_id="ws",
        principal_id="dana",
        run_id="r",
        node_id="n",
        attributes={"principal_role": "director"},
    )
    decided = await module.invoke(
        director, {"operation": "decide", "approval_id": approval_id, "decision": "approved"}
    )
    assert decided.outputs["approval_status"] == "approved"
    req = manager.get_request("ws", approval_id)
    assert req is not None and req.decision is not None
    assert (req.decision.decided_by_principal_id, req.decision.decided_by_role) == (
        "dana",
        "director",
    )


# ---------------------------------------------------------------------------
# Workflow engine: expiry at the claim, legacy rows, colliding keys, interrupts
# ---------------------------------------------------------------------------


def _gated(store: Any, calls: list[dict[str, Any]], **kw: Any) -> WorkflowEngine:
    reg = ModuleRegistry()

    def send(state: dict[str, Any]) -> dict[str, Any]:
        calls.append(dict(state))
        return {"sent": True}

    reg.register("send", send)
    spec = WorkflowSpec(
        name="w",
        entry_node="send",
        nodes={"send": WorkflowNode(id="send", handler="send", requires_approval=True)},
    )
    return WorkflowEngine(spec, reg, store=store, **kw)


async def test_sqlite_store_claim_refuses_an_expired_approval(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    store = SQLiteWorkflowStore(tmp_path / "wf.db")
    try:
        eng = _gated(store, calls, approval_ttl=0.2)
        paused = await eng.run({"to": "a"})
        assert paused.pending_approval is not None
        await asyncio.sleep(0.4)
        # The claim itself compares expires_at in its UPDATE.
        assert not await store.claim_approval(
            paused.workflow_run_id, paused.pending_approval.approval_id
        )
        with pytest.raises(ApprovalExpiredError):
            await eng.resume(paused.workflow_run_id, paused.pending_approval.approval_id)
    finally:
        await store.close()
    assert calls == []


async def test_memory_store_claim_refuses_an_expired_approval() -> None:
    calls: list[dict[str, Any]] = []
    store = MemoryWorkflowStore()
    eng = _gated(store, calls, approval_ttl=0.2)
    paused = await eng.run({"to": "a"})
    assert paused.pending_approval is not None
    await asyncio.sleep(0.4)
    assert not await store.claim_approval(
        paused.workflow_run_id, paused.pending_approval.approval_id
    )
    assert calls == []


async def test_in_time_approval_still_runs_once() -> None:
    calls: list[dict[str, Any]] = []
    eng = _gated(MemoryWorkflowStore(), calls, approval_ttl=60.0)
    paused = await eng.run({"to": "a"})
    assert paused.pending_approval is not None
    done = await eng.resume(paused.workflow_run_id, paused.pending_approval.approval_id)
    assert done.status == "completed"
    assert calls == [{"to": "a"}]


def test_legacy_pending_row_without_timestamps_is_treated_as_expired() -> None:
    legacy = PendingApproval.from_dict(
        {"approval_id": "a", "workflow_run_id": "r", "node_id": "n", "input_state": {}}
    )
    assert legacy.expires_at is None and legacy.created_at == 0.0
    eng = _gated(MemoryWorkflowStore(), [], approval_ttl=3600.0)
    deadline = eng._approval_deadline(legacy)
    assert deadline is not None and deadline < time.time()


async def test_colliding_state_update_keys_are_refused_before_the_claim() -> None:
    calls: list[dict[str, Any]] = []
    eng = _gated(MemoryWorkflowStore(), calls)
    paused = await eng.run({"1": "approved"})
    assert paused.pending_approval is not None
    with pytest.raises(WorkflowValidationError, match="collide"):
        await eng.resume(
            paused.workflow_run_id,
            paused.pending_approval.approval_id,
            state_update={1: "x", "1": "y"},  # type: ignore[dict-item]
        )
    # Nothing was claimed: the same approval still resumes the run.
    done = await eng.resume(paused.workflow_run_id, paused.pending_approval.approval_id)
    assert done.status == "completed"
    assert calls == [{"1": "approved"}]


class _GatedAsker:
    def __init__(self) -> None:
        self.granted: list[bool] = []

    async def execute(self, ctx: Any, inputs: dict[str, Any], config: dict[str, Any]) -> Any:
        self.granted.append(ctx.approval_granted)
        answer = interrupt("which account?")
        return ModuleResult(outputs={"answer": answer})


async def test_resume_interrupt_keeps_the_approval_of_a_gated_node() -> None:
    mod = _GatedAsker()
    reg = ModuleRegistry()
    reg.register("ask", mod)
    spec = WorkflowSpec(
        name="g",
        entry_node="ask",
        nodes={"ask": WorkflowNode(id="ask", handler="ask", requires_approval=True)},
    )
    eng = WorkflowEngine(spec, reg, store=MemoryWorkflowStore())
    paused = await eng.run({"x": 1})
    assert paused.pending_approval is not None
    asked = await eng.resume(paused.workflow_run_id, paused.pending_approval.approval_id)
    assert asked.status == "paused" and asked.interrupt is not None
    done = await eng.resume_interrupt(
        asked.workflow_run_id, asked.interrupt["interrupt_id"], "acct-1"
    )
    assert done.status == "completed"
    # Approved for these inputs before it asked, so it is still approved when it re-runs.
    assert mod.granted == [True, True]
