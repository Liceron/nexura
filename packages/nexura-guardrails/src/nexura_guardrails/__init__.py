# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Nexura guardrails: verdicts, a category taxonomy, fast pre-filters, an LLM judge, local
safety models (Llama Guard 3, ShieldGemma, Granite Guardian, Nemotron Safety Guard), presets
(``nexura_guardrails.presets``), tiered or parallel composition and ``GuardrailPolicy`` (one
declarative policy for every run, ``nexura_guardrails.policy``), for ``nexura_agents`` agents
and tools.

Distributed as ``nexura-guardrails`` (depends on ``nexura-agents`` only). Every check
converts to a plain guardrail function, so it works with today's
``Agent(input_guardrails=..., output_guardrails=...)`` and ``@tool(...)`` guardrails::

    from nexura_guardrails import JudgeCheck, as_guardrail, injection, tiered

    check = tiered(injection(), JudgeCheck(model="ollama:qwen3:8b", policy=POLICY))
    agent = Agent(..., input_guardrails=[as_guardrail(check)])

Guardrail checks reduce risk and leave an audit trail; they are not a security boundary.
Tool allowlists, human approval for side effects and a sandbox for code are the structural
controls. Exports load lazily, so importing the package is cheap.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

__version__ = "0.4.1"

_EXPORTS: dict[str, str] = {
    # Verdicts and taxonomy
    "Verdict": "nexura_guardrails.verdict",
    "Decision": "nexura_guardrails.verdict",
    "Stage": "nexura_guardrails.verdict",
    "CheckInput": "nexura_guardrails.verdict",
    "Category": "nexura_guardrails.verdict",
    "CATEGORIES": "nexura_guardrails.verdict",
    "DEFAULT_JUDGE_CATEGORIES": "nexura_guardrails.verdict",
    "LABEL_MAPS": "nexura_guardrails.verdict",
    "map_label": "nexura_guardrails.verdict",
    "policy_for": "nexura_guardrails.verdict",
    "validate_categories": "nexura_guardrails.verdict",
    # Checks and adapters
    "Check": "nexura_guardrails.checks",
    "as_guardrail": "nexura_guardrails.checks",
    "as_tool_guardrail": "nexura_guardrails.checks",
    "evaluate": "nexura_guardrails.checks",
    "run_check": "nexura_guardrails.checks",
    "rejection_message": "nexura_guardrails.checks",
    "is_hard_error": "nexura_guardrails.checks",
    "check_identity": "nexura_guardrails.compose",
    "estimate_tokens": "nexura_guardrails.judge",
    "token_upper_bound": "nexura_guardrails.judge",
    # Pre-filters
    "Prefilter": "nexura_guardrails.prefilters",
    "pii": "nexura_guardrails.prefilters",
    "injection": "nexura_guardrails.prefilters",
    "length": "nexura_guardrails.prefilters",
    "topics": "nexura_guardrails.prefilters",
    "json_only": "nexura_guardrails.prefilters",
    # Judge
    "JudgeCheck": "nexura_guardrails.judge",
    "MalformedVerdictError": "nexura_guardrails.judge",
    "parse_verdict": "nexura_guardrails.judge",
    # Local safety models ([ollama] extra: no extra dependency)
    "LlamaGuard": "nexura_guardrails.classifiers",
    "ShieldGemma": "nexura_guardrails.classifiers",
    "GraniteGuardian": "nexura_guardrails.classifiers",
    "NemotronGuard": "nexura_guardrails.classifiers",
    "SafetyModelCheck": "nexura_guardrails.classifiers",
    "SAFETY_MODELS": "nexura_guardrails.classifiers",
    # Composition
    "tiered": "nexura_guardrails.compose",
    "parallel": "nexura_guardrails.compose",
    "cached": "nexura_guardrails.compose",
    "Tiered": "nexura_guardrails.compose",
    "Parallel": "nexura_guardrails.compose",
    "Cached": "nexura_guardrails.compose",
    "VerdictCache": "nexura_guardrails.compose",
    "is_fallback": "nexura_guardrails.compose",
    # Policy (P39 G6)
    "GuardrailPolicy": "nexura_guardrails.policy",
    "Rule": "nexura_guardrails.policy",
    "When": "nexura_guardrails.policy",
    "ArgCondition": "nexura_guardrails.policy",
    "PolicyError": "nexura_guardrails.policy",
    "AuditRecord": "nexura_guardrails.policy",
    "JsonlAuditSink": "nexura_guardrails.policy",
    "CallbackAuditSink": "nexura_guardrails.policy",
    "MemoryAuditSink": "nexura_guardrails.policy",
    "ShadowBudget": "nexura_guardrails.policy",
    "load_policy": "nexura_guardrails.policy",
    # Benchmark
    "run_benchmark": "nexura_guardrails.bench",
    "run_benchmark_sync": "nexura_guardrails.bench",
}

__all__ = [
    "CATEGORIES",
    "ArgCondition",
    "AuditRecord",
    "CallbackAuditSink",
    "GuardrailPolicy",
    "JsonlAuditSink",
    "MemoryAuditSink",
    "PolicyError",
    "Rule",
    "ShadowBudget",
    "When",
    "load_policy",
    "Cached",
    "Category",
    "Check",
    "CheckInput",
    "DEFAULT_JUDGE_CATEGORIES",
    "Decision",
    "GraniteGuardian",
    "JudgeCheck",
    "LABEL_MAPS",
    "LlamaGuard",
    "MalformedVerdictError",
    "NemotronGuard",
    "Parallel",
    "Prefilter",
    "SAFETY_MODELS",
    "SafetyModelCheck",
    "ShieldGemma",
    "Stage",
    "Tiered",
    "Verdict",
    "VerdictCache",
    "as_guardrail",
    "as_tool_guardrail",
    "cached",
    "check_identity",
    "estimate_tokens",
    "evaluate",
    "injection",
    "is_fallback",
    "is_hard_error",
    "json_only",
    "length",
    "map_label",
    "parallel",
    "parse_verdict",
    "pii",
    "policy_for",
    "rejection_message",
    "run_benchmark",
    "run_benchmark_sync",
    "run_check",
    "tiered",
    "token_upper_bound",
    "topics",
    "validate_categories",
]


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module 'nexura_guardrails' has no attribute {name!r}")
    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)


if TYPE_CHECKING:
    from nexura_guardrails.bench import (
        run_benchmark,
        run_benchmark_sync,
    )
    from nexura_guardrails.checks import (
        Check,
        as_guardrail,
        as_tool_guardrail,
        evaluate,
        is_hard_error,
        rejection_message,
        run_check,
    )
    from nexura_guardrails.classifiers import (
        SAFETY_MODELS,
        GraniteGuardian,
        LlamaGuard,
        NemotronGuard,
        SafetyModelCheck,
        ShieldGemma,
    )
    from nexura_guardrails.compose import (
        Cached,
        Parallel,
        Tiered,
        VerdictCache,
        cached,
        check_identity,
        is_fallback,
        parallel,
        tiered,
    )
    from nexura_guardrails.judge import (
        JudgeCheck,
        MalformedVerdictError,
        estimate_tokens,
        parse_verdict,
        token_upper_bound,
    )
    from nexura_guardrails.policy import (
        ArgCondition,
        AuditRecord,
        CallbackAuditSink,
        GuardrailPolicy,
        JsonlAuditSink,
        MemoryAuditSink,
        PolicyError,
        Rule,
        ShadowBudget,
        When,
        load_policy,
    )
    from nexura_guardrails.prefilters import (
        Prefilter,
        injection,
        json_only,
        length,
        pii,
        topics,
    )
    from nexura_guardrails.verdict import (
        CATEGORIES,
        DEFAULT_JUDGE_CATEGORIES,
        LABEL_MAPS,
        Category,
        CheckInput,
        Decision,
        Stage,
        Verdict,
        map_label,
        policy_for,
        validate_categories,
    )
