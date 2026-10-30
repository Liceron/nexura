# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Names removed by P35 A6 S12, kept only to raise an error that names the replacement.

The legacy ``DurableExecutionEngine`` (with ``DurableWorkflowSpec``) and the
``EffectReconciler`` wrapper were removed before the first release: every durable path now
runs on one execution journal (``docs/durable-execution.md``). Importing one of these names
raises ``RemovedAPIError``, an ``ImportError`` whose message says what to use instead. These
stubs stay for one release and are then deleted.
"""

from __future__ import annotations

from typing import NoReturn

_ENGINE = (
    "Durable workflows run on nexura_runtime.workflow.WorkflowEngine with a "
    "SQLiteWorkflowStore (or nexura_postgres.PostgresWorkflowStore): it holds a fenced run "
    "lease, writes a step intent before each non-pure step, and continues a stopped run with "
    "WorkflowEngine.recover(run_id); an unknown outcome is settled with "
    "WorkflowEngine.resolve_effect(...)."
)
_LEASES = (
    "Leases and fencing tokens are nexura_agents.execution_store (ExecutionStore.claim, "
    "commit, renew; a stale owner gets nexura_agents.contracts.execution.LeaseLostError)."
)
_EFFECTS = (
    "External effects are recorded in the execution journal: ExecutionStore effects "
    "(nexura_agents.execution_store), settled once with ExecutionStore.resolve_effect, "
    "WorkflowEngine.resolve_effect, or POST /api/v1/runs/{run_id}/effects/{effect_id}/resolve."
)

#: Removed name -> what to use instead.
REPLACEMENTS: dict[str, str] = {
    "DurableExecutionEngine": _ENGINE,
    "DurableWorkflowSpec": "Use nexura_runtime.workflow.WorkflowSpec. " + _ENGINE,
    "DurableNodeSpec": (
        "Use nexura_runtime.workflow.WorkflowNode and declare the handler's effect class with "
        "ModuleRegistry.register(name, fn, effect='pure'|'idempotent'|'non_idempotent'|"
        "'reconcilable'). " + _ENGINE
    ),
    "DurableRunState": (
        "Read a run with WorkflowEngine.get_result(run_id) or its journal with "
        "ExecutionStore.get(ExecutionId(workspace_id, run_id)). " + _ENGINE
    ),
    "DurableExecutionModule": _ENGINE,
    "NodeIntent": (
        "Step intents are nexura_agents.contracts.execution.StepIntent, written by the "
        "runtime before each non-pure step. " + _ENGINE
    ),
    "WorkerLease": "Use nexura_agents.execution_store.ExecutionLease. " + _LEASES,
    "EffectPolicy": (
        "Use nexura_agents.contracts.execution.EffectClass (effect_class_from_policy maps the "
        "manifest spellings)."
    ),
    "DurableExecutionError": "WorkflowEngine raises nexura_agents.errors.WorkflowError. " + _ENGINE,
    "DurableDefinitionMismatchError": (
        "A changed definition raises nexura_agents.contracts.execution.DefinitionMismatchError "
        "(WorkflowSpecMismatchError for WorkflowEngine)."
    ),
    "FencingConflictError": "A stale owner gets LeaseLostError. " + _LEASES,
    "LeaseExpiredError": "A stale owner gets LeaseLostError. " + _LEASES,
    "EffectReconciler": _EFFECTS,
    "EffectRecord": "Use nexura_agents.contracts.execution.EffectRecord. " + _EFFECTS,
    "EffectStatus": "Use nexura_agents.contracts.execution.EffectStatus. " + _EFFECTS,
    "EffectError": _EFFECTS,
    "EffectIndeterminateError": _EFFECTS,
    "ReconciliationError": (
        "A second resolution raises nexura_agents.execution_store.EffectAlreadyResolvedError. "
        + _EFFECTS
    ),
}


class RemovedAPIError(ImportError):
    """A name removed by P35 A6 S12; the message names what to use instead."""


def removed(module: str, name: str, names: frozenset[str] | set[str]) -> NoReturn:
    """Raise ``RemovedAPIError`` for ``module.name`` when ``name`` is one ``module`` exported.

    Used from a module ``__getattr__``: ``from module import name`` then fails with the
    replacement in the message instead of a bare "cannot import name". Any other missing
    name raises the usual ``AttributeError``.
    """
    replacement = REPLACEMENTS.get(name) if name in names else None
    if replacement is None:
        raise AttributeError(f"module {module!r} has no attribute {name!r}")
    raise RemovedAPIError(
        f"{module}.{name} was removed (P35 A6 S12, see docs/durable-execution.md). {replacement}",
        name=module,
    )
