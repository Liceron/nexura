"""P39 G2 review round 2: attacks on CoordinatorCallLedger (M3), the lock (M4), M1.

Repros from the independent review; assertions unchanged.
Each test asserts the promised behaviour (the independent review, round 2).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from nexura_agents import AgentHooks, ToolCall, set_default_hooks, tool
from nexura_agents import policy as policy_module

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    policy_module._unlock_for_tests()
    yield
    policy_module._unlock_for_tests()


def _provider() -> Any:
    return importlib.import_module("nexura_runtime.providers.fake").StreamingFakeProvider


def _tc(name: str, cid: str, **args: Any) -> Any:
    from nexura_agents.types import Message, ModelResponse

    return ModelResponse(
        message=Message(role="assistant", tool_calls=[ToolCall(id=cid, name=name, arguments=args)])
    )


def _batch(*calls: tuple[str, str]) -> Any:
    from nexura_agents.types import Message, ModelResponse

    return ModelResponse(
        message=Message(
            role="assistant",
            tool_calls=[ToolCall(id=cid, name=name, arguments={}) for name, cid in calls],
        )
    )


def _final() -> Any:
    from nexura_agents.types import Message, ModelResponse

    return ModelResponse(message=Message(role="assistant", content="done"))


def _approve(manager: Any, workspace: str, approval_id: str) -> None:
    from nexura_runtime.runtime.approvals import ApprovalDecisionRecord

    req = manager.get_request(workspace, approval_id)
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


def _coord(tmp_path: Path, tools: list[Any], responses: list[Any], db: str = "l.db") -> Any:
    from nexura_runtime.runtime.agents.coordinator import AgentCoordinator
    from nexura_runtime.runtime.agents.definition import AgentDefinition
    from nexura_runtime.runtime.approvals import ApprovalGate, ApprovalPolicyManager

    agent = AgentDefinition(
        name="worker", model=_provider()(canned_responses=responses), tools=tools
    )
    manager = ApprovalPolicyManager(tmp_path / db)
    return AgentCoordinator([agent], approval_gate=ApprovalGate(manager)), manager


def _ctx(run_id: str = "r", attempt: str = "a1") -> Any:
    from nexura_agents.contracts.context import ExecutionContext

    return ExecutionContext(
        workspace_id="ws", principal_id="p", run_id=run_id, node_id="n", attempt_id=attempt
    )


# ------------------------------------------------------------------ M3 ledger


async def test_q1_rerun_with_reordered_batch_does_not_resend_the_side_effect(
    tmp_path: Path,
) -> None:
    """Attempt 1: the model emits [notify, refund] in one batch; notify runs, refund
    (requires_approval) pauses. After the approval a real model emits the same calls in
    the other order. The ledger key includes the call's position, so notify is a 'new'
    call and is sent a second time."""
    from nexura_runtime.runtime.approvals import ApprovalRequiredError

    sent: list[str] = []

    @tool
    def notify() -> str:
        """SMS the customer (not idempotent, not gated)."""
        sent.append("notify")
        return "ok"

    @tool(requires_approval=True)
    def refund() -> str:
        """Refund."""
        sent.append("refund")
        return "refunded"

    responses = [
        _batch(("notify", "a"), ("refund", "b")),
        _batch(("refund", "x"), ("notify", "y")),  # same calls, other order
        _final(),
    ]
    coord, manager = _coord(tmp_path, [notify, refund], responses)
    with pytest.raises(ApprovalRequiredError) as paused:
        await coord.run_async("worker", "go", context=_ctx())
    _approve(manager, "ws", paused.value.approval_id)
    with contextlib.suppress(Exception):
        # a refusal is fail closed; what matters is that notify is not repeated
        await coord.run_async("worker", "go", context=_ctx())
    assert sent.count("notify") == 1, sent


async def test_q2_rerun_with_an_extra_turn_does_not_resend_the_side_effect(
    tmp_path: Path,
) -> None:
    """The re-run's model first calls a pure lookup (one more turn), then repeats
    notify and refund. The turn number is in the key, so notify is sent again."""
    from nexura_runtime.runtime.approvals import ApprovalRequiredError

    sent: list[str] = []

    @tool(effect="pure")
    def lookup() -> str:
        """Look up."""
        return "found"

    @tool
    def notify() -> str:
        """SMS."""
        sent.append("notify")
        return "ok"

    @tool(requires_approval=True)
    def refund() -> str:
        """Refund."""
        sent.append("refund")
        return "refunded"

    responses = [
        _tc("notify", "a"),
        _tc("refund", "b"),
        _tc("lookup", "l"),
        _tc("notify", "a"),
        _tc("refund", "b"),
        _final(),
    ]
    coord, manager = _coord(tmp_path, [lookup, notify, refund], responses)
    with pytest.raises(ApprovalRequiredError) as paused:
        await coord.run_async("worker", "go", context=_ctx())
    _approve(manager, "ws", paused.value.approval_id)
    with contextlib.suppress(Exception):
        await coord.run_async("worker", "go", context=_ctx())
    assert sent.count("notify") == 1, sent


async def test_q3_concurrent_reruns_send_once(tmp_path: Path) -> None:
    """Two concurrent attempts of the same run reach the same ungated non-idempotent call:
    the ledger INSERT fences them, the loser fails closed."""
    sent: list[str] = []

    @tool
    async def notify() -> str:
        """SMS."""
        sent.append("notify")
        await asyncio.sleep(0.05)
        return "ok"

    c1, _ = _coord(tmp_path, [notify], [_tc("notify", "a"), _final()])
    c2, _ = _coord(tmp_path, [notify], [_tc("notify", "z"), _final()])
    results = await asyncio.gather(
        c1.run_async("worker", "go", context=_ctx()),
        c2.run_async("worker", "go", context=_ctx()),
        return_exceptions=True,
    )
    assert sent == ["notify"], (sent, results)


async def test_q4_a_second_visit_of_the_same_node_runs_its_calls(tmp_path: Path) -> None:
    """Workflow cycles visit a node again with the same run_id and node_id (only
    attempt_id differs: ``att_<step>``). The ledger key ignores attempt_id, so the second
    visit replays the first visit's recorded result instead of calling the tool: a
    polling tool returns stale data, a per-iteration side effect is silently skipped."""
    polled: list[int] = []

    @tool(effect="pure")
    def poll_status() -> str:
        """Current job status."""
        polled.append(1)
        return f"status #{len(polled)}"

    coord, _ = _coord(
        tmp_path,
        [poll_status],
        [_tc("poll_status", "a"), _final(), _tc("poll_status", "b"), _final()],
    )
    await coord.run_async("worker", "go", context=_ctx(attempt="att_1"))
    await coord.run_async("worker", "go", context=_ctx(attempt="att_3"))
    assert polled == [1, 1], "the second visit did not call the tool (stale replay)"


async def test_q5_other_runs_and_workspaces_do_not_share_rows(tmp_path: Path) -> None:
    sent: list[str] = []

    @tool
    def notify() -> str:
        """SMS."""
        sent.append("n")
        return "ok"

    from nexura_agents.contracts.context import ExecutionContext

    coord, _ = _coord(tmp_path, [notify], [_tc("notify", "a"), _final()] * 3)
    await coord.run_async("worker", "go", context=_ctx(run_id="r1"))
    await coord.run_async("worker", "go", context=_ctx(run_id="r2"))
    other_ws = ExecutionContext(workspace_id="ws2", principal_id="p", run_id="r1", node_id="n")
    await coord.run_async("worker", "go", context=other_ws)
    assert sent == ["n", "n", "n"]


async def test_q6_without_a_sqlite_manager_the_rerun_repeats_the_ungated_call(
    tmp_path: Path,
) -> None:
    """Documented limitation: a gate whose manager has no SQLite DB records nothing."""
    from nexura_runtime.runtime.agents.coordinator import AgentCoordinator
    from nexura_runtime.runtime.agents.definition import AgentDefinition

    sent: list[str] = []
    approved: set[str] = set()

    class Gate:
        def check_or_request_approval(self, context: Any, **kw: Any) -> Any:
            if kw["request_id"] in approved:
                return None
            approved.add(kw["request_id"])
            raise RuntimeError("pending")

    @tool
    def notify() -> str:
        """SMS."""
        sent.append("notify")
        return "ok"

    @tool(requires_approval=True)
    def refund() -> str:
        """Refund."""
        sent.append("refund")
        return "ok"

    responses = [
        _tc("notify", "a"),
        _tc("refund", "b"),
        _tc("notify", "a"),
        _tc("refund", "b"),
        _final(),
    ]
    agent = AgentDefinition(
        name="worker", model=_provider()(canned_responses=responses), tools=[notify, refund]
    )
    coord = AgentCoordinator([agent], approval_gate=Gate())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        await coord.run_async("worker", "go", context=_ctx())
    await coord.run_async("worker", "go", context=_ctx())
    assert sent.count("notify") == 1, sent


# ------------------------------------------------------------------ M4 lock


class _Gate(AgentHooks):  # was duck-typed; a subclass for mypy, same behaviour
    def __init__(self) -> None:
        self.names = {"pay"}

    def tool_needs_approval(self, ctx: Any, tool: Any, arguments: Any) -> bool | None:
        return True if tool.name in self.names else None


def test_q7_lock_refuses_opt_out_and_replacement_and_threads_keep_it() -> None:
    from nexura_agents import Runner
    from nexura_agents.errors import SDKValidationError

    gate = _Gate()
    set_default_hooks(gate, locked=True)
    with pytest.raises(SDKValidationError):
        Runner(default_hooks=False)
    with pytest.raises(SDKValidationError):
        set_default_hooks(None)
    with pytest.raises(SDKValidationError):
        set_default_hooks([gate])  # a list is a new ComposedHooks object
    set_default_hooks(gate)  # same object: allowed, still locked
    assert policy_module.default_hooks_locked()
    runner = Runner()
    runner.use_default_hooks = False  # attribute poke after construction
    assert runner.effective_hooks() is gate
    box: list[Any] = []
    t = threading.Thread(target=lambda: box.append(Runner().effective_hooks()))
    t.start()
    t.join()
    assert box == [gate]


def test_q8_lock_is_not_a_boundary_against_in_process_code() -> None:
    """Characterization (expected, documented): code in the process can still mutate
    the locked policy object or flip the private module flag."""
    from nexura_agents import Runner

    gate = _Gate()
    set_default_hooks(gate, locked=True)
    gate.names.clear()  # same object, mutated
    policy_module._locked = False  # private flag
    Runner(default_hooks=False)  # now allowed
