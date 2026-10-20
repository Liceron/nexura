"""Unit and acceptance tests for B06: Workflow Compiler, Ports, and Control Flow."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from nexura_runtime.contracts import (
    BaseModule,
    ExecutionContext,
    ModuleManifest,
    ModuleResult,
)
from nexura_runtime.contracts.errors import BudgetExhaustedError

from nexura_runtime.modules.registry import VersionedModuleRegistry
from nexura_runtime.runtime.compiler import (
    CompiledPlanExecutor,
    FanoutLimitExceededError,
    IncompatiblePortError,
    InvalidCachePolicyError,
    UnresolvedReferenceError,
    WorkflowCompiler,
    WorkflowCompilerModule,
)


# Test helper modules
class SimpleComputeModule(BaseModule):
    manifest = ModuleManifest(
        schema_version=1,
        module_id="test.compute.step",
        version="1.0.0",
        kind="compute",
        title="Test Compute Step",
        description="Increments counter and branches.",
        input_schema={
            "type": "object",
            "properties": {
                "count": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "count": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "additionalProperties": False,
        },
        effect_policy="pure",
    )

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any],
    ) -> ModuleResult:
        cnt = inputs.get("count", 0) + 1
        limit = inputs.get("limit", 10)
        branch = "loop" if cnt < limit else "done"
        return ModuleResult(
            outputs={"count": cnt, "limit": limit},
            branch=branch,
        )


class StateModifyingModule(BaseModule):
    manifest = ModuleManifest(
        schema_version=1,
        module_id="test.effect.write",
        version="1.0.0",
        kind="writer",
        title="Test Writer",
        description="Performs non-pure side-effect.",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        effect_policy="idempotent_write",
    )

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any],
    ) -> ModuleResult:
        return ModuleResult(outputs={"written": True})


class ItemWorkerModule(BaseModule):
    manifest = ModuleManifest(
        schema_version=1,
        module_id="test.worker.item",
        version="1.0.0",
        kind="worker",
        title="Item Worker",
        description="Processes an item asynchronously with a brief delay.",
        input_schema={
            "type": "object",
            "properties": {
                "val": {"type": "integer"},
            },
            "required": ["val"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "doubled": {"type": "integer"},
            },
            "required": ["doubled"],
            "additionalProperties": False,
        },
        effect_policy="pure",
    )

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any],
    ) -> ModuleResult:
        await asyncio.sleep(0.01)
        return ModuleResult(outputs={"doubled": inputs["val"] * 2})


@pytest.fixture
def test_registry() -> VersionedModuleRegistry:
    reg = VersionedModuleRegistry()
    reg.register(SimpleComputeModule)
    reg.register(StateModifyingModule)
    reg.register(ItemWorkerModule)
    return reg


@pytest.fixture
def context() -> ExecutionContext:
    return ExecutionContext(
        workspace_id="ws_test",
        principal_id="test_user",
        run_id="run_comp_001",
        node_id="root",
    )


def test_unknown_modules_ports_targets_rejected_before_invocation(
    test_registry: VersionedModuleRegistry,
) -> None:
    compiler = WorkflowCompiler(registry=test_registry)

    # 1. Unknown module rejected eagerly
    spec_unknown_module = {
        "workflow_id": "test_wf",
        "entry_node": "n1",
        "nodes": {
            "n1": {"module_id": "nonexistent.module.xyz"},
        },
    }
    with pytest.raises(UnresolvedReferenceError) as exc_info:
        compiler.compile(spec_unknown_module)
    assert "nonexistent.module.xyz" in str(exc_info.value)
    assert exc_info.value.details["reference_kind"] == "module"

    # 2. Unknown transition target rejected eagerly
    spec_unknown_target = {
        "workflow_id": "test_wf",
        "entry_node": "n1",
        "nodes": {
            "n1": {
                "module_id": "test.compute.step",
                "transitions": {"loop": "ghost_node"},
            },
        },
    }
    with pytest.raises(UnresolvedReferenceError) as exc_info:
        compiler.compile(spec_unknown_target)
    assert "ghost_node" in str(exc_info.value)
    assert exc_info.value.details["reference_kind"] == "node"

    # 3. Unknown source node in binding rejected eagerly
    spec_unknown_binding_src = {
        "workflow_id": "test_wf",
        "entry_node": "n1",
        "nodes": {
            "n1": {
                "module_id": "test.compute.step",
                "bindings": [
                    {
                        "source_node": "nonexistent_source",
                        "source_port": "count",
                        "target_port": "count",
                    }
                ],
            }
        },
    }
    with pytest.raises(UnresolvedReferenceError) as exc_info:
        compiler.compile(spec_unknown_binding_src)
    assert "nonexistent_source" in str(exc_info.value)

    # 4. Incompatible target port rejected eagerly
    spec_invalid_port = {
        "workflow_id": "test_wf",
        "entry_node": "n1",
        "nodes": {
            "n1": {"module_id": "test.compute.step"},
            "n2": {
                "module_id": "test.compute.step",
                "bindings": [
                    {
                        "source_node": "n1",
                        "source_port": "count",
                        "target_port": "completely_invalid_port_name",
                    }
                ],
            },
        },
    }
    with pytest.raises(IncompatiblePortError) as exc_info_port:
        compiler.compile(spec_invalid_port)
    assert "completely_invalid_port_name" in str(exc_info_port.value)

    # 5. Non-pure node declaring cacheable=True rejected eagerly
    spec_invalid_cache = {
        "workflow_id": "test_wf",
        "entry_node": "w1",
        "nodes": {
            "w1": {
                "module_id": "test.effect.write",
                "cacheable": True,
            }
        },
    }
    with pytest.raises(InvalidCachePolicyError) as exc_info_cache:
        compiler.compile(spec_invalid_cache)
    assert "w1" in str(exc_info_cache.value)


@pytest.mark.asyncio
async def test_cycles_consume_fuel_and_enforce_budget(
    test_registry: VersionedModuleRegistry,
    context: ExecutionContext,
) -> None:
    compiler = WorkflowCompiler(registry=test_registry)
    executor = CompiledPlanExecutor(registry=test_registry)

    # Looping workflow: n1 loops back to n1 until count >= 5
    spec_loop = {
        "workflow_id": "fuel_loop_wf",
        "entry_node": "loop_step",
        "terminal_nodes": ["done_step"],
        "nodes": {
            "loop_step": {
                "module_id": "test.compute.step",
                "fuel_cost": 3,
                "transitions": {
                    "loop": "loop_step",
                    "done": "done_step",
                },
            },
            "done_step": {
                "module_id": "test.compute.step",
                "fuel_cost": 1,
            },
        },
        "default_fuel_budget": 100,
    }
    plan = compiler.compile(spec_loop)

    # 1. Successful run consuming fuel: count from 0 to 5 requires 5 iterations
    # of loop_step (5 * 3 = 15) + 1 done_step (1) = 16 fuel
    res = await executor.execute(
        plan,
        context,
        initial_state={"count": 0, "limit": 5},
        initial_fuel=20,
    )
    assert res["count"] == 6  # 5 loop iterations + 1 done iteration
    assert res["_fuel_consumed"] == 16
    assert res["_fuel_remaining"] == 4

    # 2. Exhaustion when fuel budget is insufficient
    with pytest.raises(BudgetExhaustedError) as exc_fuel:
        await executor.execute(
            plan,
            context,
            initial_state={"count": 0, "limit": 10},
            initial_fuel=5,  # Only enough for 1 loop_step (3 fuel), 2nd loop needs 3 more (6 > 5)
        )
    assert exc_fuel.value.details["budget_kind"] == "step_fuel"

    # 3. Cycle limit exhaustion
    spec_infinite_loop = {
        "workflow_id": "infinite_loop_wf",
        "entry_node": "spin_step",
        "nodes": {
            "spin_step": {
                "module_id": "test.compute.step",
                "fuel_cost": 0,
                "default_transition": "spin_step",
            }
        },
        "max_cycles": 10,
        "default_fuel_budget": 1000,
    }
    plan_infinite = compiler.compile(spec_infinite_loop)
    with pytest.raises(BudgetExhaustedError) as exc_cycle:
        await executor.execute(plan_infinite, context, initial_state={"limit": 9999})
    assert exc_cycle.value.details["budget_kind"] == "cycles"
    assert exc_cycle.value.details["limit"] == 10


@pytest.mark.asyncio
async def test_bounded_fanout_honors_limit(
    test_registry: VersionedModuleRegistry,
    context: ExecutionContext,
) -> None:
    compiler = WorkflowCompiler(registry=test_registry, max_fanout_limit=5)
    executor = CompiledPlanExecutor(registry=test_registry)

    # 1. Compilation failure when requesting concurrency exceeding max_fanout_limit
    spec_excessive_fanout = {
        "workflow_id": "fanout_wf",
        "entry_node": "w_node",
        "nodes": {
            "w_node": {"module_id": "test.worker.item"},
        },
        "parallel_maps": {
            "excessive_map": {
                "items_field": "nums",
                "worker_node_id": "w_node",
                "max_concurrency": 10,  # exceeds limit 5
            }
        },
    }
    with pytest.raises(FanoutLimitExceededError) as exc_fanout:
        compiler.compile(spec_excessive_fanout)
    assert exc_fanout.value.details["requested_concurrency"] == 10
    assert exc_fanout.value.details["limit"] == 5

    # 2. Concurrency strictly honored during execution
    spec_valid_fanout = {
        "workflow_id": "fanout_wf",
        "entry_node": "w_node",
        "nodes": {
            "w_node": {"module_id": "test.worker.item"},
        },
        "parallel_maps": {
            "bounded_map": {
                "items_field": "nums",
                "item_var": "val",
                "worker_node_id": "w_node",
                "max_concurrency": 2,
                "output_field": "doubled_nums",
            }
        },
    }
    plan = compiler.compile(spec_valid_fanout)

    peak_tracker: list[int] = []
    state = {"nums": [1, 2, 3, 4, 5, 6]}
    results = await executor.execute_parallel_map(
        plan,
        "bounded_map",
        context,
        state,
        peak_concurrency_tracker=peak_tracker,
    )
    assert len(results) == 6
    assert [r["doubled"] for r in results] == [2, 4, 6, 8, 10, 12]
    assert max(peak_tracker) <= 2, f"Peak concurrency was {max(peak_tracker)}, expected <= 2"
    assert state["doubled_nums"] == results


def test_deterministic_joins_under_reordered_completion(
    test_registry: VersionedModuleRegistry,
) -> None:
    compiler = WorkflowCompiler(registry=test_registry)
    executor = CompiledPlanExecutor(registry=test_registry)

    spec = {
        "workflow_id": "join_wf",
        "entry_node": "step1",
        "nodes": {
            "step1": {"module_id": "test.compute.step"},
        },
        "joins": {
            "join_point": {
                "incoming_branches": ["branch_zebra", "branch_aardvark", "branch_monkey"],
                "reducer": "sorted_merge",
            }
        },
    }
    plan = compiler.compile(spec)

    # Order 1: zebra, monkey, aardvark
    payloads_order1 = {
        "branch_zebra": {"z": 1},
        "branch_monkey": {"m": 2},
        "branch_aardvark": {"a": 3},
    }
    res1 = executor.execute_deterministic_join("join_point", plan, payloads_order1)

    # Order 2: monkey, zebra, aardvark
    payloads_order2 = {
        "branch_monkey": {"m": 2},
        "branch_zebra": {"z": 1},
        "branch_aardvark": {"a": 3},
    }
    res2 = executor.execute_deterministic_join("join_point", plan, payloads_order2)

    # Both must produce identical ordered branch trace
    expected_order = ["branch_aardvark", "branch_monkey", "branch_zebra"]
    assert res1["_joined_branches_ordered"] == expected_order
    assert res2["_joined_branches_ordered"] == expected_order
    assert res1 == res2


def test_graph_edit_cannot_alter_in_flight_release(
    test_registry: VersionedModuleRegistry,
) -> None:
    compiler = WorkflowCompiler(registry=test_registry)

    original_spec = {
        "workflow_id": "financial_approval",
        "version": "1.0.0",
        "entry_node": "init",
        "nodes": {
            "init": {
                "module_id": "test.compute.step",
                "fuel_cost": 2,
            }
        },
    }

    # Release 1
    plan_v1 = compiler.compile(original_spec)
    initial_digest = plan_v1.release_digest
    assert len(initial_digest) == 64

    # Mutate the dictionary that was used to create plan_v1
    original_spec["nodes"]["init"]["fuel_cost"] = 999  # type: ignore[index]
    original_spec["nodes"]["rogue_node"] = {  # type: ignore[index]
        "module_id": "test.effect.write",
    }

    # plan_v1 remains completely unaffected
    assert plan_v1.release_digest == initial_digest
    assert "rogue_node" not in plan_v1.nodes
    assert plan_v1.nodes["init"].fuel_cost == 2

    # A new compilation with the mutated dictionary yields a distinct release digest
    plan_v2 = compiler.compile(original_spec)
    assert plan_v2.release_digest != initial_digest
    assert "rogue_node" in plan_v2.nodes


@pytest.mark.asyncio
async def test_workflow_compiler_module_wrapper(
    test_registry: VersionedModuleRegistry,
    context: ExecutionContext,
) -> None:
    compiler_mod = WorkflowCompilerModule(registry=test_registry)
    spec = {
        "workflow_id": "mod_wf",
        "entry_node": "c1",
        "nodes": {
            "c1": {"module_id": "test.compute.step"},
        },
    }

    result = await compiler_mod.execute(context, inputs={"spec": spec}, config={})
    assert result.status == "success"
    assert result.outputs["workflow_id"] == "mod_wf"
    assert result.outputs["nodes_count"] == 1
    assert len(result.outputs["release_digest"]) == 64
