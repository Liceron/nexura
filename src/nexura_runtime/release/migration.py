# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Workflow specification migration and schema evolution runner."""

from __future__ import annotations

import copy
from typing import Any

from nexura_runtime.workflow import WorkflowNode, WorkflowSpec


class WorkflowMigrator:
    """Migrates older workflow specification schemas to the current specification version."""

    CURRENT_SPEC_VERSION = 1

    def migrate_spec_dict(self, raw_data: dict[str, Any]) -> WorkflowSpec:
        """Upgrade legacy workflow dictionary payloads to standard WorkflowSpec.

        Supports:
        - Legacy v0 linear 'steps' array -> node graph with transitions.
        - Legacy node keys: 'module_name' -> 'handler'.
        - Automatic computation of terminal nodes if omitted.
        - Preserving metadata and versioning.
        """
        data = copy.deepcopy(raw_data)

        # 1. Handle legacy linear 'steps' format (v0)
        if "steps" in data and "nodes" not in data:
            steps: list[dict[str, Any]] = data.pop("steps")
            nodes: dict[str, Any] = {}
            prev_id: str | None = None
            entry_node: str | None = None

            for idx, step in enumerate(steps):
                step_id = step.get("id") or f"step_{idx}"
                handler = step.get("handler") or step.get("module_name") or "default_handler"
                req_app = step.get("requires_approval") or step.get("approval_required", False)
                requires_approval = bool(req_app)

                if entry_node is None:
                    entry_node = step_id

                if prev_id and prev_id in nodes:
                    nodes[prev_id]["default_transition"] = step_id

                nodes[step_id] = {
                    "id": step_id,
                    "handler": handler,
                    "requires_approval": requires_approval,
                    "transitions": {},
                    "default_transition": None,
                }
                prev_id = step_id

            data["entry_node"] = entry_node or "entry"
            data["nodes"] = nodes
            data["terminal_nodes"] = [prev_id] if prev_id else []

        # 2. Upgrade node dictionaries ('module_name' -> 'handler')
        raw_nodes = data.get("nodes", {})
        converted_nodes: dict[str, WorkflowNode] = {}

        if isinstance(raw_nodes, list):
            for raw_node in raw_nodes:
                nid = raw_node.get("id", "node")
                handler = raw_node.get("handler") or raw_node.get("module_name", nid)
                req_approval = bool(
                    raw_node.get("requires_approval", False)
                    or raw_node.get("approval_required", False)
                )
                converted_nodes[nid] = WorkflowNode(
                    id=nid,
                    handler=handler,
                    transitions=dict(raw_node.get("transitions", {})),
                    default_transition=raw_node.get("default_transition"),
                    requires_approval=req_approval,
                )
        elif isinstance(raw_nodes, dict):
            for nid, raw_node in raw_nodes.items():
                if isinstance(raw_node, dict):
                    handler = raw_node.get("handler") or raw_node.get("module_name", nid)
                    req_approval = bool(
                        raw_node.get("requires_approval", False)
                        or raw_node.get("approval_required", False)
                    )
                    default_trans = raw_node.get("default_transition")
                    transitions = dict(raw_node.get("transitions", {}))
                    if "default" in transitions and not default_trans:
                        default_trans = transitions.pop("default")

                    converted_nodes[nid] = WorkflowNode(
                        id=nid,
                        handler=handler,
                        transitions=transitions,
                        default_transition=default_trans,
                        requires_approval=req_approval,
                    )

        # 3. Compute terminal nodes if omitted
        terminal_nodes = list(data.get("terminal_nodes", []))
        if not terminal_nodes:
            # Nodes with no outgoing transitions are terminal
            for nid, node in converted_nodes.items():
                if not node.transitions and not node.default_transition:
                    terminal_nodes.append(nid)

        spec = WorkflowSpec(
            name=data.get("name", "MigratedWorkflow"),
            entry_node=data.get("entry_node", next(iter(converted_nodes.keys()), "entry")),
            nodes=converted_nodes,
            spec_version=self.CURRENT_SPEC_VERSION,
            terminal_nodes=terminal_nodes,
            metadata=dict(data.get("metadata", {})),
        )

        return spec
