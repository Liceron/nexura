# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Executor for compiled workflow plans with deterministic joins and bounded fanout."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.errors import BudgetExhaustedError

from nexura_runtime.modules.registry import VersionedModuleRegistry, default_registry
from nexura_runtime.runtime.compiler.models import CompiledWorkflowPlan


class CompiledPlanExecutor:
    """Executes frozen workflow plans with deterministic join and bounded fanout guarantees."""

    def __init__(
        self,
        registry: VersionedModuleRegistry | None = None,
    ) -> None:
        self.registry = registry or default_registry

    async def execute(
        self,
        plan: CompiledWorkflowPlan,
        context: ExecutionContext,
        initial_state: dict[str, Any] | None = None,
        initial_fuel: int | None = None,
    ) -> dict[str, Any]:
        """Execute a compiled release plan to completion."""
        state: dict[str, Any] = dict(initial_state or {})
        fuel_remaining = initial_fuel if initial_fuel is not None else plan.default_fuel_budget
        fuel_consumed = 0

        node_outputs: dict[str, dict[str, Any]] = {}
        cycle_counts: dict[str, int] = defaultdict(int)

        current_node_id: str | None = plan.entry_node_id

        while current_node_id is not None:
            node = plan.nodes[current_node_id]

            # 1. Fuel accounting and cycle check
            if fuel_remaining < node.fuel_cost:
                raise BudgetExhaustedError(
                    f"Fuel budget exhausted while executing node '{current_node_id}': "
                    f"required {node.fuel_cost}, remaining {fuel_remaining}.",
                    budget_kind="step_fuel",
                    consumed=fuel_consumed,
                    limit=fuel_consumed + fuel_remaining,
                )

            fuel_remaining -= node.fuel_cost
            fuel_consumed += node.fuel_cost

            cycle_counts[current_node_id] += 1
            if cycle_counts[current_node_id] > plan.max_cycles:
                raise BudgetExhaustedError(
                    f"Cycle limit exceeded on node '{current_node_id}' "
                    f"({cycle_counts[current_node_id]} > {plan.max_cycles}).",
                    budget_kind="cycles",
                    consumed=cycle_counts[current_node_id],
                    limit=plan.max_cycles,
                )

            # 2. Resolve input port bindings
            reg_entry = self.registry.get_module(node.module_id, version=node.module_version)
            manifest = reg_entry.manifest
            in_props = manifest.input_schema.get("properties", {})
            allow_additional = manifest.input_schema.get("additionalProperties", True)

            node_inputs: dict[str, Any] = {}
            for binding in node.bindings:
                src_outputs = node_outputs.get(binding.source_node, {})
                if binding.source_port in src_outputs:
                    node_inputs[binding.target_port] = src_outputs[binding.source_port]
                elif binding.source_port in state:
                    node_inputs[binding.target_port] = state[binding.source_port]

            # Pass matching keys from current state if accepted by manifest
            for k, v in state.items():
                if k.startswith("_"):
                    continue
                if k not in node_inputs and (allow_additional or k in in_props):
                    node_inputs[k] = v

            # 3. Module invocation through B00/B01 contract
            module_instance = reg_entry.get_instance()
            result = await module_instance.execute(context, node_inputs, node.config)
            node_outputs[current_node_id] = result.outputs
            state.update(result.outputs)

            # 4. Check if terminal node reached
            if current_node_id in plan.terminal_node_ids:
                break

            # 5. Transition to next node
            branch = result.branch or "default"
            next_node = node.transitions.get(branch)
            if next_node is None and branch != "default":
                next_node = node.transitions.get("default")
            if next_node is None:
                next_node = node.default_transition

            current_node_id = next_node

        state["_fuel_consumed"] = fuel_consumed
        state["_fuel_remaining"] = fuel_remaining
        state["_release_digest"] = plan.release_digest
        return state

    async def execute_parallel_map(
        self,
        plan: CompiledWorkflowPlan,
        map_id: str,
        context: ExecutionContext,
        state: dict[str, Any],
        peak_concurrency_tracker: list[int] | None = None,
    ) -> list[Any]:
        """Execute parallel map with strict bounded fanout semaphore."""
        map_spec = plan.parallel_maps[map_id]
        items = list(state.get(map_spec.items_field, []))
        worker_node = plan.nodes[map_spec.worker_node_id]
        reg_entry = self.registry.get_module(
            worker_node.module_id, version=worker_node.module_version
        )
        module_instance = reg_entry.get_instance()

        semaphore = asyncio.Semaphore(map_spec.max_concurrency)
        current_concurrent = 0
        tracker = peak_concurrency_tracker if peak_concurrency_tracker is not None else []

        async def _run_item(idx: int, item: Any) -> tuple[int, Any]:
            nonlocal current_concurrent
            async with semaphore:
                current_concurrent += 1
                tracker.append(current_concurrent)
                try:
                    res = await module_instance.execute(
                        context,
                        {map_spec.item_var: item},
                        worker_node.config,
                    )
                    return idx, res.outputs
                finally:
                    current_concurrent -= 1

        tasks = [_run_item(i, it) for i, it in enumerate(items)]
        indexed_results = await asyncio.gather(*tasks)

        # Maintain deterministic order identical to input items
        indexed_results.sort(key=lambda t: t[0])
        ordered_outputs = [r[1] for r in indexed_results]
        state[map_spec.output_field] = ordered_outputs
        return ordered_outputs

    def execute_deterministic_join(
        self,
        join_spec_or_id: str,
        plan: CompiledWorkflowPlan,
        branch_payloads: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Merge branch outputs into a strictly deterministic combined state.

        Sorts incoming branches alphabetically by branch identifier so that asynchronous
        arrival order does not alter the merged result.
        """
        join_spec = plan.joins[join_spec_or_id]
        sorted_branches = sorted(join_spec.incoming_branches)

        merged: dict[str, Any] = {}
        ordered_branch_results: list[tuple[str, dict[str, Any]]] = []

        for b_name in sorted_branches:
            payload = branch_payloads.get(b_name, {})
            ordered_branch_results.append((b_name, payload))
            merged.update(payload)

        merged["_joined_branches_ordered"] = [b for b, _ in ordered_branch_results]
        return merged
