# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""``guardrails.check``: a workflow node that checks text and branches on the verdict.

Inputs: ``text`` (required), ``context`` (optional, the request that led to the text) and
``stage`` (optional: ``input``, ``output``, ``tool_input`` or ``tool_output``; default
``input``). Config: ``checks``, a map of check names to check specs in the policy file's
syntax (``{"pii": {"type": "pii", "action": "redact"}}``), and ``mode`` (``tiered``, the
default, or ``parallel``). Outputs: ``decision``, ``categories``, ``score``, ``reason``,
``replacement`` (for ``rewrite``) and ``text`` (the replacement after a rewrite, the original
text when allowed, an empty string otherwise). The branch is the decision: ``allow``,
``block``, ``review``, ``error`` or ``rewrite``, so a workflow can route ``review`` to a human
approval node and ``block``/``error`` to a refusal.

A workflow document is editable by workspace editors, so its ``checks`` may only use the
deterministic pre-filters (``pii``, ``injection``, ``length``, ``topics``, ``json_only``):
no model ids, URLs, files or regular expressions (``extra_patterns`` is refused: an editor's
pattern could backtrack for minutes on the event loop; topic keywords are matched
literally). Model checks (a judge, a local safety model) and custom patterns are given in Python
by the operator, ``GuardrailCheckModule(checks={"safety": LlamaGuard(...)})``, and named in the
config by ``use: [safety]``. The node is ``pure`` for durable execution: its
outcome is committed with the node's step like any node, so a replay does not run it again.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import ModuleManifest, PortDefinition
from nexura_agents.contracts.result import ModuleResult
from nexura_agents.run_context import RunContext

from nexura_guardrails.checks import Check, run_check
from nexura_guardrails.compose import parallel, tiered
from nexura_guardrails.verdict import CheckInput, Role, Stage

__all__ = [
    "GUARDRAIL_CHECK_MODULE_ID",
    "WORKFLOW_CHECK_TYPES",
    "GuardrailCheckModule",
    "node_tool_info",
]

GUARDRAIL_CHECK_MODULE_ID = "guardrails.check"
#: Check types a workflow document may configure (deterministic pre-filters only).
WORKFLOW_CHECK_TYPES = frozenset({"pii", "injection", "length", "topics", "json_only"})
_STAGES: Mapping[str, Role] = {
    "input": "user",
    "output": "assistant",
    "tool_input": "assistant",
    "tool_output": "tool",
}
_BRANCHES = ("allow", "block", "review", "error", "rewrite")
MAX_TEXT_CHARS = 200_000


def _manifest(version: str) -> ModuleManifest:
    return ModuleManifest(
        schema_version=1,
        module_id=GUARDRAIL_CHECK_MODULE_ID,
        version=version,
        kind="guardrail",
        title="Guardrail check",
        description=(
            "Check text with guardrail pre-filters (PII, prompt injection, length, topics, "
            "JSON) or operator-provided model checks, and branch on allow, block, review, "
            "error or rewrite."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "maxLength": MAX_TEXT_CHARS},
                "context": {"type": "string", "maxLength": MAX_TEXT_CHARS},
                "stage": {"type": "string", "enum": list(_STAGES)},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": list(_BRANCHES)},
                "categories": {"type": "array", "items": {"type": "string"}},
                "score": {"type": ["number", "null"]},
                "reason": {"type": "string"},
                "replacement": {"type": ["string", "null"]},
                "text": {"type": "string"},
            },
            "required": ["decision", "categories", "score", "reason", "text"],
        },
        config_schema={
            "type": "object",
            "properties": {
                "checks": {"type": "object", "maxProperties": 16},
                "use": {"type": "array", "items": {"type": "string"}, "maxItems": 16},
                "mode": {"type": "string", "enum": ["tiered", "parallel"]},
            },
            "additionalProperties": False,
        },
        named_ports=[PortDefinition(name=b, required=False) for b in _BRANCHES],
        effect_policy="pure",
    )


def node_tool_info(
    module_id: str, effect_policy: str | None, *, handler: str | None = None, node_id: str = ""
) -> Any:
    """The ``ToolInfo`` a policy matches for a workflow module node (design 6.1.5): name
    ``handler`` (or the module id), origin ``workflow:<module_id>``, the module's durable
    effect class, and a ``node:<id>`` tag. An unknown effect policy counts as
    ``non_idempotent`` (fail closed)."""
    from nexura_agents.contracts.execution import effect_class_from_policy
    from nexura_agents.tools import ToolInfo

    try:
        effect = effect_class_from_policy(effect_policy).value
    except ValueError:
        effect = "non_idempotent"
    tags = frozenset({f"node:{node_id}"}) if node_id else frozenset()
    return ToolInfo(
        name=handler or module_id, origin=f"workflow:{module_id}", effect=str(effect), tags=tags
    )


class GuardrailCheckModule(BaseModule):
    """The ``guardrails.check`` workflow node (see the module docstring).

    ``checks``: operator-provided checks (any ``Check``, including model checks) that a
    node's config may name with ``use``.
    """

    def __init__(
        self, checks: Mapping[str, Check] | None = None, *, version: str = "1.0.0"
    ) -> None:
        self.provided: dict[str, Check] = dict(checks or {})
        self._manifest = _manifest(version)

    @property
    def manifest(self) -> ModuleManifest:
        """The module manifest."""
        return self._manifest

    def _checks(self, config: Mapping[str, Any]) -> list[Check]:
        from nexura_guardrails.policy.loader import _build_check
        from nexura_guardrails.policy.rules import PolicyError

        found: list[Check] = []
        for name in config.get("use", []) or []:
            if name not in self.provided:
                raise PolicyError(f"no operator check named {name!r} is installed.", "use")
            found.append(self.provided[name])
        specs = config.get("checks", {}) or {}
        if not isinstance(specs, Mapping):
            raise PolicyError("must be an object of check specs.", "checks")
        for name, spec in specs.items():
            kind = spec.get("type") if isinstance(spec, Mapping) else None
            if isinstance(spec, Mapping) and "extra_patterns" in spec:
                # A workflow document is editor-controlled: never compile or run an editor's
                # regular expression (catastrophic backtracking blocks the event loop).
                raise PolicyError(
                    f"check {name!r}: extra_patterns (custom regular expressions) are not "
                    "allowed in a workflow; the operator adds custom patterns in Python "
                    "(GuardrailCheckModule(checks={...})) and names them with 'use'.",
                    f"checks.{name}.extra_patterns",
                )
            if kind not in WORKFLOW_CHECK_TYPES:
                raise PolicyError(
                    f"check {name!r}: a workflow may configure only {sorted(WORKFLOW_CHECK_TYPES)}"
                    "; model checks are installed by the operator and named with 'use'.",
                    f"checks.{name}",
                )
            found.append(
                _build_check(str(name), spec, base_dir=None, model_resolver=None, extra_digests={})
            )
        if not found:
            raise PolicyError("configure at least one check (checks or use).", "checks")
        return found

    async def invoke(
        self, context: ExecutionContext, inputs: dict[str, Any], config: dict[str, Any]
    ) -> ModuleResult:
        """Run the configured checks on ``inputs["text"]`` and branch on the decision."""
        from nexura_guardrails.policy.rules import PolicyError

        try:
            checks = self._checks(config)
        except PolicyError as exc:
            return ModuleResult.failure("GUARDRAIL_CONFIG_INVALID", str(exc))
        stage = inputs.get("stage") or "input"
        if stage not in _STAGES:
            return ModuleResult.failure("GUARDRAIL_STAGE_INVALID", f"unknown stage {stage!r}")
        if len(checks) == 1:
            check = checks[0]
        elif config.get("mode") == "parallel":
            check = parallel(*checks, name="guardrails.check")
        else:
            check = tiered(*checks, name="guardrails.check")
        if stage not in check.stages:
            return ModuleResult.failure(
                "GUARDRAIL_STAGE_INVALID", f"the configured checks cannot run on stage {stage!r}"
            )
        text = inputs["text"]
        ctx: RunContext[Any] = RunContext(
            deps=None,
            run_id=context.run_id,
            agent_name=f"workflow:{context.node_id}",
        )
        item = CheckInput(
            stage=cast(Stage, stage),
            text=text,
            role=_STAGES[stage],
            context=inputs.get("context"),
        )
        verdict = await run_check(check, ctx, item)
        decision = verdict.decision
        if decision == "rewrite":
            safe_text = verdict.replacement or ""
        elif decision == "allow":
            safe_text = text
        else:
            safe_text = ""
        outputs = {
            "decision": decision,
            "categories": list(verdict.categories),
            "score": verdict.score,
            "reason": verdict.reason[:200],
            "replacement": verdict.replacement,
            "text": safe_text,
        }
        usage = verdict.usage if not verdict.cached else None
        return ModuleResult.success(
            outputs,
            branch=decision,
            usage=usage,
            metadata={"check": verdict.check, "backend": verdict.backend},
        )
