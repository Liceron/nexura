# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""The verdict every check returns, what a check receives, and the category taxonomy.

A check (a pre-filter, the LLM judge, a composition) answers with one ``Verdict``:
``allow``, ``block``, ``rewrite`` (a replacement text), ``review`` (a human should decide)
or ``error`` (the check could not decide: a timeout, a provider error, a budget refusal or
a malformed verdict). ``error`` is never "safe": the guardrail boundary blocks it unless
``on_error="allow"`` is set explicitly.

Categories are stable ids from one taxonomy. Classifier labels (Llama Guard ``S1`` to
``S14``, ShieldGemma, Granite Guardian, Nemotron Safety Guard v3, OpenAI moderation) map to
those ids through :func:`map_label`; unmapped labels become ``other``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from nexura_agents.types import Usage

__all__ = [
    "CATEGORIES",
    "DECISIONS",
    "DEFAULT_JUDGE_CATEGORIES",
    "LABEL_MAPS",
    "STAGES",
    "Category",
    "CheckInput",
    "Decision",
    "Stage",
    "Verdict",
    "map_label",
    "policy_for",
    "validate_categories",
]

Decision = Literal["allow", "block", "rewrite", "review", "error"]
"""What a check decided. ``error`` means it could not decide and is treated as a block."""

DECISIONS: tuple[str, ...] = ("allow", "block", "rewrite", "review", "error")

Stage = Literal[
    "input",
    "output",
    "tool_input",
    "tool_output",
    "model_input",
    "model_output",
    "stream",
    "node",
]
"""Where the checked text comes from. G3 uses the first four; the rest are reserved."""

STAGES: tuple[str, ...] = (
    "input",
    "output",
    "tool_input",
    "tool_output",
    "model_input",
    "model_output",
    "stream",
    "node",
)

Role = Literal["user", "assistant", "tool", "document"]


@dataclass(frozen=True)
class CheckInput:
    """What one check looks at.

    ``text`` is the canonical text form of the checked value (tool arguments as sorted
    JSON). ``context`` is optional extra text, for example the user's prompt for an output
    check; the judge fences it as untrusted data too. ``tool_name`` is set on tool stages.
    """

    stage: Stage
    text: str
    role: Role = "user"
    tool_name: str | None = None
    context: str | None = None

    def __post_init__(self) -> None:
        if self.stage not in STAGES:
            raise ValueError(f"Unknown stage {self.stage!r}; choose from {list(STAGES)}.")
        if not isinstance(self.text, str):
            raise TypeError("CheckInput.text must be a string.")


@dataclass(frozen=True)
class Verdict:
    """The outcome of one check.

    ``reason`` is short and never contains the checked content; ``evidence`` holds masked,
    JSON-safe details (signal ids, spans, masked previews, an error type). ``usage`` is the
    model usage of the check itself, counted toward the run by ``as_guardrail``. ``cached``
    is True when the verdict was served from a cache (its usage is then ``None``).
    """

    decision: Decision
    categories: tuple[str, ...] = ()
    score: float | None = None
    reason: str = ""
    replacement: str | None = None
    check: str = ""
    backend: str = ""
    latency_ms: float = 0.0
    usage: Usage | None = None
    cached: bool = False
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.decision not in DECISIONS:
            raise ValueError(f"Unknown decision {self.decision!r}; choose from {list(DECISIONS)}.")
        if self.score is not None:
            if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
                raise ValueError("Verdict.score must be a number between 0 and 1.")
            if not math.isfinite(self.score) or not 0.0 <= self.score <= 1.0:
                raise ValueError("Verdict.score must be between 0 and 1.")
        if (self.decision == "rewrite") != (self.replacement is not None):
            raise ValueError("Verdict.replacement is set exactly when decision is 'rewrite'.")
        if not isinstance(self.categories, tuple):
            object.__setattr__(self, "categories", tuple(self.categories))

    @property
    def blocks(self) -> bool:
        """True for ``block`` and ``error`` (fail closed) and for ``review``."""
        return self.decision in ("block", "error", "review")

    def info(self) -> dict[str, Any]:
        """The plain, content-free dict put in ``GuardrailResult.info``."""
        return {
            "decision": self.decision,
            "check": self.check,
            "backend": self.backend,
            "categories": list(self.categories),
            "score": self.score,
            "reason": self.reason,
            "latency_ms": round(self.latency_ms, 3),
            "cached": self.cached,
            "evidence": dict(self.evidence),
        }


# --------------------------------------------------------------------------- taxonomy


@dataclass(frozen=True)
class Category:
    """One taxonomy entry. ``parent`` links a regional category to its general one."""

    id: str
    title: str
    description: str
    parent: str | None = None
    region: str = "global"
    review_status: str = "approved"


_GENERAL: tuple[Category, ...] = (
    Category("violence", "Violence", "Violent crimes, threats or incitement to violence."),
    Category("crime", "Crime", "Help with non-violent crimes or illegal activity."),
    Category("sexual_crime", "Sexual crimes", "Sex-related crimes such as assault or trafficking."),
    Category(
        "child_safety", "Child safety", "Sexual content involving minors or child exploitation."
    ),
    Category("defamation", "Defamation", "False statements that damage a real person's name."),
    Category(
        "unauthorized_advice",
        "Unqualified advice",
        "Specialised financial, medical or legal advice presented as professional advice.",
    ),
    Category("privacy", "Privacy", "Exposure of personal data or sensitive personal details."),
    Category("ip", "Intellectual property", "Copyright, trademark or plagiarism violations."),
    Category("weapons", "Weapons", "Making or using weapons, including mass-casualty weapons."),
    Category("hate", "Hate", "Hateful or demeaning content about protected groups."),
    Category("self_harm", "Self-harm", "Encouraging or instructing suicide or self-harm."),
    Category("sexual", "Sexual content", "Explicit sexual content."),
    Category("elections", "Elections", "False information about voting or election processes."),
    Category("misinformation", "Misinformation", "Deliberately false or misleading claims."),
    Category("code_abuse", "Code abuse", "Malware or abuse of code-execution tools."),
    Category("harassment", "Harassment", "Insults, bullying, threats or profanity at people."),
    Category("fraud", "Fraud", "Scams, deception, impersonation or manipulation for gain."),
    Category(
        "prompt_injection",
        "Prompt injection",
        "Text that tries to override, replace or reveal the assistant's instructions.",
    ),
    Category("jailbreak", "Jailbreak", "Attempts to make the assistant ignore its safety rules."),
    Category("groundedness", "Groundedness", "Claims not supported by the provided sources."),
    Category(
        "function_call_error",
        "Function-call error",
        "A tool call with a wrong tool, wrong arguments or a hallucinated call.",
    ),
    Category("other", "Other", "Anything else the policy forbids."),
)

CATEGORIES: Mapping[str, Category] = MappingProxyType({c.id: c for c in _GENERAL})
"""Every category id."""

DEFAULT_JUDGE_CATEGORIES: tuple[str, ...] = (
    "prompt_injection",
    "jailbreak",
    "violence",
    "weapons",
    "self_harm",
    "hate",
    "harassment",
    "sexual",
    "child_safety",
    "crime",
    "fraud",
    "privacy",
    "other",
)
"""The categories ``JudgeCheck`` offers the judge when none are given."""


def validate_categories(ids: Iterable[str]) -> tuple[str, ...]:
    """Return ``ids`` as a de-duplicated tuple; raise ``ValueError`` on an unknown id."""
    if isinstance(ids, str):
        ids = (ids,)
    out: list[str] = []
    for item in ids:
        if item not in CATEGORIES:
            raise ValueError(f"Unknown category {item!r}; choose from {sorted(CATEGORIES)}.")
        if item not in out:
            out.append(item)
    return tuple(out)


def policy_for(ids: Iterable[str]) -> str:
    """Policy text listing what to block for ``ids``, from the taxonomy descriptions."""
    lines = ["Block content in any of these categories:"]
    for item in validate_categories(ids):
        cat = CATEGORIES[item]
        lines.append(f"- {cat.id}: {cat.description}")
    lines.append("Allow everything else.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- label maps

_LLAMA_GUARD = {
    "S1": ("violence",),
    "S2": ("crime", "fraud"),
    "S3": ("sexual_crime",),
    "S4": ("child_safety",),
    "S5": ("defamation",),
    "S6": ("unauthorized_advice",),
    "S7": ("privacy",),
    "S8": ("ip",),
    "S9": ("weapons",),
    "S10": ("hate",),
    "S11": ("self_harm",),
    "S12": ("sexual",),
    "S13": ("elections",),
    "S14": ("code_abuse",),
}

_SHIELDGEMMA = {
    "dangerous": ("violence", "weapons", "self_harm"),
    "hate": ("hate",),
    "harassment": ("harassment",),
    "sexual": ("sexual",),
}

_GRANITE_GUARDIAN = {
    "violence": ("violence",),
    "unethical_behavior": ("crime",),
    "social_bias": ("hate",),
    "sexual_content": ("sexual",),
    "profanity": ("harassment",),
    "jailbreak": ("jailbreak",),
    "groundedness": ("groundedness",),
    "function_call": ("function_call_error",),
}

_NEMOTRON_V3 = {
    "S1": ("violence",),
    "S2": ("sexual",),
    "S3": ("crime",),
    "S4": ("weapons",),
    "S6": ("self_harm",),
    "S7": ("child_safety",),
    "S8": ("hate",),
    "S9": ("privacy",),
    "S10": ("harassment",),
    "S11": ("violence",),
    "S12": ("harassment",),
    "S13": ("other",),
    "S14": ("other",),
    "S15": ("fraud",),
    "S16": ("fraud",),
    "S17": ("code_abuse",),
    "S19": ("misinformation",),
    "S20": ("ip",),
    "S21": ("unauthorized_advice",),
    "S22": ("crime",),
    "S23": ("other",),
}

_OPENAI_MODERATION = {
    "violence": ("violence",),
    "violence/graphic": ("violence",),
    "illicit": ("crime",),
    "illicit/violent": ("weapons",),
    "sexual/minors": ("child_safety",),
    "hate": ("hate",),
    "hate/threatening": ("hate",),
    "self-harm": ("self_harm",),
    "self-harm/intent": ("self_harm",),
    "self-harm/instructions": ("self_harm",),
    "sexual": ("sexual",),
    "harassment": ("harassment",),
    "harassment/threatening": ("harassment",),
}

LABEL_MAPS: Mapping[str, Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "llama_guard": MappingProxyType(_LLAMA_GUARD),
        "shieldgemma": MappingProxyType(_SHIELDGEMMA),
        "granite_guardian": MappingProxyType(_GRANITE_GUARDIAN),
        "nemotron": MappingProxyType(_NEMOTRON_V3),
        "openai_moderation": MappingProxyType(_OPENAI_MODERATION),
    }
)
"""Classifier label to Nexura category ids, per source (design section 4.5)."""


def map_label(source: str, label: str) -> tuple[str, ...]:
    """Map one classifier label to Nexura category ids; unmapped labels give ``("other",)``.

    Matching ignores case and surrounding whitespace. An unknown ``source`` raises
    ``ValueError``.
    """
    table = LABEL_MAPS.get(source)
    if table is None:
        raise ValueError(f"Unknown label source {source!r}; choose from {sorted(LABEL_MAPS)}.")
    key = label.strip()
    for candidate in (key, key.upper(), key.lower()):
        if candidate in table:
            return table[candidate]
    return ("other",)
