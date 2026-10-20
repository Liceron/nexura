# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Graph validation engine and structured issue reporting for builder contracts."""

from __future__ import annotations

import copy
from typing import Any

from nexura_agents.errors import UnsupportedWorkflowVersionError, WorkflowValidationError
from packaging.version import Version
from pydantic import BaseModel, Field

from nexura_runtime import __version__ as CURRENT_SDK_VERSION
from nexura_runtime.workflow import ModuleRegistry, WorkflowSpec, _validate_composite

EFFECT_POLICY_RANK: dict[str, int] = {
    "pure": 0,
    "read": 1,
    "idempotent_write": 2,
    "non_idempotent_write": 3,
}

RANK_TO_EFFECT_POLICY: dict[int, str] = {
    0: "pure",
    1: "read",
    2: "idempotent_write",
    3: "non_idempotent_write",
}


class ValidationIssue(BaseModel):
    """Structured validation finding with field and node specificity."""

    node_id: str | None = None
    field: str
    code: str
    message: str


class WorkflowValidateRequest(BaseModel):
    """Request payload for validating a workflow specification graph."""

    spec: dict[str, Any]
    workspace_id: str | None = None


class WorkflowValidateResponse(BaseModel):
    """Validation response report containing authoritative server diagnostics."""

    valid: bool
    errors: list[ValidationIssue] = Field(default_factory=list)
    warnings: list[ValidationIssue] = Field(default_factory=list)
    canonical_hash: str | None = None
    node_count: int = 0
    estimated_effect_policy: str = "pure"


def _spec_effect_rank(spec: WorkflowSpec, registry: ModuleRegistry) -> int:
    """Highest effect-policy rank of every handler in a spec, descending into sub-workflows."""
    highest = 0
    for node in spec.nodes.values():
        names: list[str | None] = list(node.parallel)
        if not names and node.subgraph is None:
            names = [node.handler or node.module_id]
        for name in names:
            if not name or not isinstance(name, str):
                continue
            obj: Any = None
            try:
                if (
                    node.module_id
                    and not node.parallel
                    and registry.has_module(node.module_id, node.module_version)
                ):
                    obj = registry.get_module(node.module_id, node.module_version)
                elif registry.has_handler(name):
                    obj = registry.get(name)
            except Exception:
                obj = None
            manifest = getattr(obj, "manifest", None)
            if manifest is not None:
                policy = getattr(manifest, "effect_policy", "pure")
                highest = max(highest, EFFECT_POLICY_RANK.get(policy, 1))
        if node.subgraph is not None:
            highest = max(highest, _spec_effect_rank(node.subgraph, registry))
    return highest


def validate_workflow_graph(
    spec_payload: dict[str, Any],
    registry: ModuleRegistry,
    approval_required_handlers: frozenset[str] | set[str] | None = None,
) -> WorkflowValidateResponse:
    """Perform authoritative server-side graph validation for a workflow specification."""
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []

    try:
        spec = WorkflowSpec.from_dict(copy.deepcopy(spec_payload))
    except UnsupportedWorkflowVersionError as exc:
        return WorkflowValidateResponse(
            valid=False,
            errors=[
                ValidationIssue(
                    node_id=None,
                    field="spec_version",
                    code="UNSUPPORTED_SPEC_VERSION",
                    message=str(exc),
                )
            ],
            warnings=[],
            canonical_hash=None,
            node_count=0,
            estimated_effect_policy="pure",
        )
    except Exception as exc:
        return WorkflowValidateResponse(
            valid=False,
            errors=[
                ValidationIssue(
                    node_id=None,
                    field="spec",
                    code="MALFORMED_SPEC",
                    message=f"Malformed workflow specification: {exc}",
                )
            ],
            warnings=[],
            canonical_hash=None,
            node_count=0,
            estimated_effect_policy="pure",
        )

    # 1. Check entry node and node count
    if not spec.nodes:
        errors.append(
            ValidationIssue(
                node_id=None,
                field="nodes",
                code="EMPTY_NODES",
                message="Workflow must contain at least one node.",
            )
        )
    elif spec.entry_node not in spec.nodes:
        errors.append(
            ValidationIssue(
                node_id=None,
                field="entry_node",
                code="MISSING_ENTRY_NODE",
                message=(
                    f"Entry node '{spec.entry_node}' does not exist in nodes: "
                    f"{list(spec.nodes.keys())}."
                ),
            )
        )

    highest_effect_rank = 0

    # 2. Inspect each node definition
    for node_id, node in spec.nodes.items():
        # Composite steps (parallel / sub-workflow / map_over) follow the engine's own rules.
        try:
            _validate_composite(node_id, node, registry, approval_required_handlers)
        except WorkflowValidationError as exc:
            errors.append(
                ValidationIssue(
                    node_id=node_id,
                    field="composite",
                    code="INVALID_COMPOSITE",
                    message=str(exc),
                )
            )
        if node.parallel or node.subgraph is not None:
            highest_effect_rank = max(
                highest_effect_rank,
                _spec_effect_rank(
                    WorkflowSpec(name="_", entry_node=node_id, nodes={node_id: node}), registry
                ),
            )
            # A composite step has no handler of its own; only its routes remain to check.
            for target in [*node.transitions.values(), node.default_transition]:
                if target and target not in spec.nodes and target not in spec.terminal_nodes:
                    errors.append(
                        ValidationIssue(
                            node_id=node_id,
                            field="transitions",
                            code="UNSUPPORTED_ROUTE",
                            message=(
                                f"Node '{node_id}' route targets unknown node or terminal "
                                f"'{target}'."
                            ),
                        )
                    )
            continue
        handler_name = node.handler or node.module_id
        if not handler_name or not isinstance(handler_name, str):
            errors.append(
                ValidationIssue(
                    node_id=node_id,
                    field="handler",
                    code="MISSING_HANDLER",
                    message=f"Node '{node_id}' must specify a valid handler or module_id.",
                )
            )
            continue

        has_h = registry.has_handler(handler_name)
        has_m = bool(node.module_id and registry.has_module(node.module_id, node.module_version))

        if not has_h and not has_m:
            errors.append(
                ValidationIssue(
                    node_id=node_id,
                    field="handler",
                    code="UNKNOWN_HANDLER",
                    message=(
                        f"Node '{node_id}' references unknown handler or module '{handler_name}'."
                    ),
                )
            )
        else:
            # Check module availability / missing dependencies
            module_unavailable = False
            if node.module_id and registry.versioned_registry:
                v_reg = registry.versioned_registry
                if v_reg.has(node.module_id, node.module_version):
                    entry = v_reg.get_entry(node.module_id, node.module_version)
                    if not entry.is_available:
                        module_unavailable = True
                        errors.append(
                            ValidationIssue(
                                node_id=node_id,
                                field="module_id",
                                code="MODULE_UNAVAILABLE",
                                message=(
                                    f"Module '{node.module_id}' is unavailable: "
                                    f"{entry.missing_reason or 'missing optional dependency'}."
                                ),
                            )
                        )

            # Resolve module object if BaseModule
            mod_obj = None
            if module_unavailable:
                # Already reported above; loading it would raise MissingDependencyError.
                pass
            elif node.module_id and registry.has_module(node.module_id, node.module_version):
                mod_obj = registry.get_module(node.module_id, node.module_version)
            elif has_h:
                try:
                    resolved = registry.get(handler_name)
                    if hasattr(resolved, "manifest"):
                        mod_obj = resolved
                except Exception:
                    pass

            if mod_obj is not None and hasattr(mod_obj, "manifest"):
                manifest = mod_obj.manifest
                policy_str = getattr(manifest, "effect_policy", "pure")
                highest_effect_rank = max(
                    highest_effect_rank,
                    EFFECT_POLICY_RANK.get(policy_str, 1),
                )

                # SDK version compatibility
                min_sdk = getattr(manifest, "min_sdk_version", "0.3.0")
                if Version(min_sdk) > Version(CURRENT_SDK_VERSION):
                    errors.append(
                        ValidationIssue(
                            node_id=node_id,
                            field="module_version",
                            code="INCOMPATIBLE_SDK_VERSION",
                            message=(
                                f"Module '{manifest.module_id}' requires SDK >= '{min_sdk}', "
                                f"but runtime SDK is '{CURRENT_SDK_VERSION}'."
                            ),
                        )
                    )

                # Schema-driven configuration validation
                if node.config:
                    try:
                        manifest.validate_config(node.config)
                    except Exception as exc:
                        err_field = "config"
                        details = getattr(exc, "details", None)
                        if isinstance(details, dict) and "field" in details:
                            err_field = f"config.{details['field']}"
                        errors.append(
                            ValidationIssue(
                                node_id=node_id,
                                field=err_field,
                                code="INVALID_CONFIG",
                                message=f"Node '{node_id}' configuration invalid: {exc}",
                            )
                        )

                # Input port mapping validation
                if node.input_mapping:
                    allowed_inputs = set(manifest.input_schema.get("properties", {}).keys()) | {
                        p.name for p in manifest.named_ports
                    }
                    if allowed_inputs:
                        for port_name in node.input_mapping:
                            if port_name not in allowed_inputs:
                                errors.append(
                                    ValidationIssue(
                                        node_id=node_id,
                                        field=f"input_mapping.{port_name}",
                                        code="INVALID_INPUT_MAPPING",
                                        message=(
                                            f"Node '{node_id}' input_mapping references unknown "
                                            f"port '{port_name}' for module "
                                            f"'{manifest.module_id}'. "
                                            f"Allowed ports: {sorted(allowed_inputs)}"
                                        ),
                                    )
                                )

                # Output port mapping validation
                if node.output_mapping:
                    allowed_outputs = set(manifest.output_schema.get("properties", {}).keys()) | {
                        p.name for p in manifest.named_ports
                    }
                    if allowed_outputs:
                        for port_name in node.output_mapping:
                            if port_name not in allowed_outputs:
                                errors.append(
                                    ValidationIssue(
                                        node_id=node_id,
                                        field=f"output_mapping.{port_name}",
                                        code="INVALID_OUTPUT_MAPPING",
                                        message=(
                                            f"Node '{node_id}' output_mapping references unknown "
                                            f"port '{port_name}' for module "
                                            f"'{manifest.module_id}'. "
                                            f"Allowed ports: {sorted(allowed_outputs)}"
                                        ),
                                    )
                                )

        # Mandatory approval policy enforcement
        if approval_required_handlers is not None:
            is_mandatory = node.handler in approval_required_handlers or (
                node.module_id is not None and node.module_id in approval_required_handlers
            )
            if is_mandatory and not node.requires_approval:
                errors.append(
                    ValidationIssue(
                        node_id=node_id,
                        field="requires_approval",
                        code="MANDATORY_APPROVAL_REQUIRED",
                        message=(
                            f"Handler '{node.handler}' requires mandatory approval; "
                            "cannot be disabled."
                        ),
                    )
                )

        # Transition routes check
        for branch, target in node.transitions.items():
            if target not in spec.nodes and target not in spec.terminal_nodes:
                errors.append(
                    ValidationIssue(
                        node_id=node_id,
                        field=f"transitions.{branch}",
                        code="UNSUPPORTED_ROUTE",
                        message=(
                            f"Node '{node_id}' transition '{branch}' targets "
                            f"unknown node or terminal '{target}'."
                        ),
                    )
                )

        if node.default_transition and (
            node.default_transition not in spec.nodes
            and node.default_transition not in spec.terminal_nodes
        ):
            errors.append(
                ValidationIssue(
                    node_id=node_id,
                    field="default_transition",
                    code="UNSUPPORTED_ROUTE",
                    message=(
                        f"Node '{node_id}' default_transition targets unknown node or terminal "
                        f"'{node.default_transition}'."
                    ),
                )
            )

    # 3. Reachability analysis
    if spec.entry_node in spec.nodes:
        visited: set[str] = set()
        queue = [spec.entry_node]
        while queue:
            curr = queue.pop(0)
            if curr in visited or curr not in spec.nodes:
                continue
            visited.add(curr)
            curr_node = spec.nodes[curr]
            for target_id in curr_node.transitions.values():
                if target_id in spec.nodes and target_id not in visited:
                    queue.append(target_id)
            if (
                curr_node.default_transition
                and curr_node.default_transition in spec.nodes
                and curr_node.default_transition not in visited
            ):
                queue.append(curr_node.default_transition)

        unreachable = set(spec.nodes.keys()) - visited
        for unreach_id in sorted(unreachable):
            warnings.append(
                ValidationIssue(
                    node_id=unreach_id,
                    field="graph",
                    code="DISCONNECTED_NODE",
                    message=(
                        f"Node '{unreach_id}' is disconnected and cannot be reached "
                        f"from entry node '{spec.entry_node}'."
                    ),
                )
            )

    is_valid = len(errors) == 0
    canonical_hash = spec.canonical_hash() if is_valid else None
    estimated_effect = RANK_TO_EFFECT_POLICY.get(highest_effect_rank, "pure")

    return WorkflowValidateResponse(
        valid=is_valid,
        errors=errors,
        warnings=warnings,
        canonical_hash=canonical_hash,
        node_count=len(spec.nodes),
        estimated_effect_policy=estimated_effect,
    )
