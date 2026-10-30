# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Workflow compiler producing immutable, version-pinned release plans."""

from __future__ import annotations

from typing import Any

from nexura_agents.contracts.canonical import canonical_digest

from nexura_runtime.modules.registry import VersionedModuleRegistry, default_registry
from nexura_runtime.runtime.compiler.errors import (
    FanoutLimitExceededError,
    IncompatiblePortError,
    InvalidCachePolicyError,
    UnresolvedReferenceError,
)
from nexura_runtime.runtime.compiler.models import (
    CompiledNode,
    CompiledWorkflowPlan,
    DeterministicJoinSpec,
    ParallelMapSpec,
    PortBinding,
)

MAX_ALLOWED_FANOUT = 50


class WorkflowCompiler:
    """Compiles workflow specifications into frozen, verified release bundles."""

    def __init__(
        self,
        registry: VersionedModuleRegistry | None = None,
        max_fanout_limit: int = MAX_ALLOWED_FANOUT,
    ) -> None:
        self.registry = registry or default_registry
        self.max_fanout_limit = max_fanout_limit

    def compile(self, spec: dict[str, Any]) -> CompiledWorkflowPlan:
        """Validate and compile a workflow specification dictionary into a frozen release bundle."""
        workflow_id = spec.get("workflow_id")
        if not workflow_id:
            raise UnresolvedReferenceError(
                "Workflow specification must define 'workflow_id'.",
                reference_kind="workflow",
                reference_value="",
            )

        entry_node_id = spec.get("entry_node")
        if not entry_node_id:
            raise UnresolvedReferenceError(
                "Workflow specification must define 'entry_node'.",
                reference_kind="entry_node",
                reference_value="",
            )

        raw_nodes = spec.get("nodes", {})
        if not raw_nodes:
            raise UnresolvedReferenceError(
                "Workflow specification must declare at least one node.",
                reference_kind="nodes",
                reference_value="",
            )

        terminal_nodes = list(spec.get("terminal_nodes", []))
        all_valid_node_targets = set(raw_nodes.keys()).union(terminal_nodes)

        if entry_node_id not in raw_nodes:
            raise UnresolvedReferenceError(
                f"Entry node '{entry_node_id}' not declared in workflow nodes.",
                reference_kind="node",
                reference_value=entry_node_id,
            )

        compiled_nodes: dict[str, CompiledNode] = {}
        for node_id, node_def in raw_nodes.items():
            module_id = node_def.get("module_id")
            if not module_id:
                raise UnresolvedReferenceError(
                    f"Node '{node_id}' must specify 'module_id'.",
                    reference_kind="module",
                    reference_value="",
                    referencing_node=node_id,
                )

            # Resolve in registry
            requested_version = node_def.get("module_version")
            try:
                reg_module = self.registry.get_module(module_id, version=requested_version)
            except Exception as exc:
                raise UnresolvedReferenceError(
                    f"Node '{node_id}' references unknown or unavailable module "
                    f"'{module_id}': {exc}",
                    reference_kind="module",
                    reference_value=module_id,
                    referencing_node=node_id,
                ) from exc

            manifest = reg_module.manifest
            manifest_hash = manifest.canonical_hash()
            effect_policy = manifest.effect_policy

            # Validate cache policy (only pure nodes may be cacheable)
            cacheable = bool(node_def.get("cacheable", False))
            if cacheable and effect_policy != "pure":
                raise InvalidCachePolicyError(
                    f"Node '{node_id}' declares cacheable=True, but module '{module_id}' "
                    f"has effect_policy='{effect_policy}' (only 'pure' allowed).",
                    node_id=node_id,
                    effect_policy=effect_policy,
                )

            # Validate transitions
            transitions = node_def.get("transitions", {})
            for branch, target in transitions.items():
                if target not in all_valid_node_targets:
                    raise UnresolvedReferenceError(
                        f"Node '{node_id}' transition '{branch}' targets unknown node '{target}'.",
                        reference_kind="node",
                        reference_value=target,
                        referencing_node=node_id,
                    )

            default_transition = node_def.get("default_transition")
            if default_transition and default_transition not in all_valid_node_targets:
                raise UnresolvedReferenceError(
                    f"Node '{node_id}' default_transition targets unknown node "
                    f"'{default_transition}'.",
                    reference_kind="node",
                    reference_value=default_transition,
                    referencing_node=node_id,
                )

            # Validate port bindings
            raw_bindings = node_def.get("bindings", [])
            port_bindings: list[PortBinding] = []
            for b in raw_bindings:
                src_node = b.get("source_node")
                src_port = b.get("source_port")
                tgt_port = b.get("target_port")

                if src_node not in raw_nodes:
                    raise UnresolvedReferenceError(
                        f"Port binding in node '{node_id}' references unknown source "
                        f"node '{src_node}'.",
                        reference_kind="node",
                        reference_value=src_node,
                        referencing_node=node_id,
                    )

                # Validate input port existence on target module schema
                in_props = manifest.input_schema.get("properties", {})
                if tgt_port not in in_props and in_props:
                    raise IncompatiblePortError(
                        f"Target port '{tgt_port}' not found in input schema of "
                        f"module '{module_id}'.",
                        source_node=src_node,
                        source_port=src_port,
                        target_node=node_id,
                        target_port=tgt_port,
                    )

                port_bindings.append(
                    PortBinding(
                        source_node=src_node,
                        source_port=src_port,
                        target_port=tgt_port,
                    )
                )

            compiled_nodes[node_id] = CompiledNode(
                node_id=node_id,
                module_id=module_id,
                module_version=manifest.version,
                manifest_hash=manifest_hash,
                effect_policy=effect_policy,
                cacheable=cacheable,
                config=node_def.get("config", {}),
                bindings=port_bindings,
                transitions=transitions,
                default_transition=default_transition,
                fuel_cost=int(node_def.get("fuel_cost", 1)),
                timeout_seconds=node_def.get("timeout_seconds"),
            )

        # Validate Parallel Maps (bounded fanout)
        parallel_maps: dict[str, ParallelMapSpec] = {}
        for p_id, p_def in spec.get("parallel_maps", {}).items():
            max_conc = int(p_def.get("max_concurrency", 5))
            if max_conc > self.max_fanout_limit:
                raise FanoutLimitExceededError(
                    f"Parallel map '{p_id}' requested concurrency {max_conc}, "
                    f"which exceeds the maximum allowed fanout limit {self.max_fanout_limit}.",
                    node_id=p_id,
                    requested_concurrency=max_conc,
                    limit=self.max_fanout_limit,
                )
            worker_id = p_def.get("worker_node_id")
            if worker_id not in raw_nodes:
                raise UnresolvedReferenceError(
                    f"Parallel map '{p_id}' references unknown worker node '{worker_id}'.",
                    reference_kind="node",
                    reference_value=worker_id,
                    referencing_node=p_id,
                )
            parallel_maps[p_id] = ParallelMapSpec(
                node_id=p_id,
                items_field=p_def["items_field"],
                item_var=p_def.get("item_var", "item"),
                worker_node_id=worker_id,
                max_concurrency=max_conc,
                output_field=p_def.get("output_field", "mapped_results"),
            )

        # Validate Joins
        joins: dict[str, DeterministicJoinSpec] = {}
        for j_id, j_def in spec.get("joins", {}).items():
            joins[j_id] = DeterministicJoinSpec(
                node_id=j_id,
                incoming_branches=list(j_def.get("incoming_branches", [])),
                reducer=j_def.get("reducer", "sorted_merge"),
            )

        # Construct and compute frozen release digest
        draft_plan = CompiledWorkflowPlan(
            workflow_id=workflow_id,
            version=spec.get("version", "1.0.0"),
            release_digest="",
            entry_node_id=entry_node_id,
            terminal_node_ids=terminal_nodes,
            nodes=compiled_nodes,
            parallel_maps=parallel_maps,
            joins=joins,
            max_cycles=int(spec.get("max_cycles", 50)),
            default_fuel_budget=int(spec.get("default_fuel_budget", 100)),
            default_time_budget_seconds=float(spec.get("default_time_budget_seconds", 60.0)),
        )

        canonical_payload = draft_plan.model_dump(exclude={"release_digest", "created_at"})
        draft_plan.release_digest = canonical_digest(canonical_payload)
        return draft_plan
