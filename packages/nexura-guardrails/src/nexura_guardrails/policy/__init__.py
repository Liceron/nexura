# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""``GuardrailPolicy``: one declarative guardrail policy for every run (P39 G6).

See :mod:`nexura_guardrails.policy.core` for the stages and actions,
:mod:`nexura_guardrails.policy.rules` for matching, :mod:`nexura_guardrails.policy.loader`
for the JSON/YAML format and :mod:`nexura_guardrails.policy.audit` for audit sinks.
"""

from __future__ import annotations

from nexura_guardrails.policy.audit import (
    AuditRecord,
    AuditSink,
    CallbackAuditSink,
    JsonlAuditSink,
    MemoryAuditSink,
)
from nexura_guardrails.policy.core import GuardrailPolicy, PolicyAuditError, StageOutcome
from nexura_guardrails.policy.loader import (
    CHECK_TYPES,
    load_policy,
    policy_from_dict,
    policy_from_json,
    policy_from_yaml,
)
from nexura_guardrails.policy.rules import (
    ACTIONS,
    POLICY_STAGES,
    ArgCondition,
    PolicyError,
    Rule,
    When,
)

__all__ = [
    "ACTIONS",
    "CHECK_TYPES",
    "POLICY_STAGES",
    "ArgCondition",
    "AuditRecord",
    "AuditSink",
    "CallbackAuditSink",
    "GuardrailPolicy",
    "JsonlAuditSink",
    "MemoryAuditSink",
    "PolicyAuditError",
    "PolicyError",
    "Rule",
    "StageOutcome",
    "When",
    "load_policy",
    "policy_from_dict",
    "policy_from_json",
    "policy_from_yaml",
]
