# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Fast pre-filters as ``Check`` objects, over the ``nexura_agents`` built-ins.

There is one implementation: each wrapper builds the matching ``nexura_agents`` guardrail
(``block_pii``, ``redact_output_pii``, ``no_prompt_injection``, ``max_length``,
``allowed_topics``, ``json_output_only``) and translates its ``GuardrailResult`` into a
``Verdict``. They are regex and checksum heuristics, about 1 ms per KB, with the measured
misses listed in ``docs/guardrails-hooks-limits.md``: use them as the first tier in front
of a model-based check.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from nexura_agents.guardrails import GuardrailFn, GuardrailResult
from nexura_agents.guardrails_builtin import (
    allowed_topics,
    block_pii,
    json_output_only,
    max_length,
    no_prompt_injection,
    redact_output_pii,
)
from nexura_agents.run_context import RunContext

from nexura_guardrails.verdict import STAGES, CheckInput, Verdict

__all__ = ["Prefilter", "injection", "json_only", "length", "pii", "topics"]

_ALL_STAGES = frozenset(STAGES)
_OUTPUT_STAGES = frozenset({"output", "tool_output", "model_output", "stream", "node"})
_INDIAN_ID_KINDS = frozenset({"aadhaar", "pan", "upi"})

Categorise = Callable[[dict[str, Any]], tuple[str, ...]]


class Prefilter:
    """A deterministic check that runs one ``nexura_agents`` built-in guardrail.

    ``stages`` are the stages it may be used on; ``categories`` maps the built-in's
    evidence to taxonomy ids when it blocks. ``config`` is the JSON-safe set of arguments the
    built-in was made with (the factories fill it in); it goes into ``cache_identity`` so two
    pre-filters with different settings never share a cached verdict. Without it there is
    no stable identity (``cache_identity`` is None) and ``Cached`` uses a pinned object
    identity instead.
    """

    deterministic = True
    backend = "regex"

    def __init__(
        self,
        name: str,
        guardrail: GuardrailFn,
        *,
        stages: frozenset[str] = _ALL_STAGES,
        categories: Categorise | tuple[str, ...] = (),
        config: Mapping[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.stages = stages
        self._guardrail = guardrail
        self._categories = categories
        self._config_digest: str | None = None
        if config is not None:
            encoded = json.dumps(dict(config), sort_keys=True, ensure_ascii=False)
            self._config_digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @property
    def cache_identity(self) -> str | None:
        """Identity used in cache keys: name, built-in and a digest of its settings; None
        for a pre-filter built without ``config``."""
        if self._config_digest is None:
            return None
        builtin = getattr(self._guardrail, "__name__", "")
        return f"prefilter:{self.name}:{builtin}:{self._config_digest}"

    def _cats(self, info: dict[str, Any]) -> tuple[str, ...]:
        if callable(self._categories):
            return self._categories(info)
        return self._categories

    async def __call__(self, ctx: RunContext[Any], item: CheckInput) -> Verdict:
        """Run the built-in on ``item.text`` and translate its result."""
        result = self._guardrail(ctx, item.text)
        if not isinstance(result, GuardrailResult):  # the built-ins are sync and typed
            raise TypeError(f"pre-filter {self.name!r} returned {type(result).__name__}")
        info: dict[str, Any] = dict(result.info) if isinstance(result.info, dict) else {}
        raw = info.get("score")
        score = (
            min(1.0, float(raw))
            if isinstance(raw, (int, float)) and not isinstance(raw, bool)
            else None
        )
        evidence = {k: v for k, v in info.items() if k != "reason"}
        if raw is not None:
            evidence["raw_score"] = raw
        common: dict[str, Any] = {
            "check": self.name,
            "backend": self.backend,
            "score": score,
            "evidence": evidence,
        }
        if result.replacement is not None:
            return Verdict(
                decision="rewrite",
                replacement=result.replacement,
                categories=self._cats(info),
                reason="redacted",
                **common,
            )
        if result.tripwire:
            return Verdict(
                decision="block",
                categories=self._cats(info),
                reason=str(info.get("reason", f"{self.name} pre-filter tripped")),
                **common,
            )
        return Verdict(decision="allow", **common)


def _pii_categories(info: dict[str, Any]) -> tuple[str, ...]:
    kinds = set(info.get("kinds") or ())
    kinds |= {f.get("kind") for f in info.get("redacted") or () if isinstance(f, dict)}
    return ("privacy", "id_exposure_in") if kinds & _INDIAN_ID_KINDS else ("privacy",)


def pii(
    kinds: Iterable[str] | None = None,
    *,
    action: str = "block",
    mask: str = "[REDACTED:{kind}]",
    name: str = "pii",
) -> Prefilter:
    """PII and secrets pre-filter (``detect_pii``): ``action="block"`` or ``"redact"``.

    Category ``privacy``; findings of Aadhaar, PAN or UPI also add ``id_exposure_in``.
    ``"redact"`` returns a ``rewrite`` verdict with the masked text (output stages rewrite
    it; on input stages the guardrail trips because input cannot be rewritten).
    """
    chosen = None if kinds is None else list(kinds)
    if action == "block":
        fn = block_pii(chosen)
    elif action == "redact":
        fn = redact_output_pii(chosen, mask=mask)
    else:
        raise ValueError("action must be 'block' or 'redact'.")
    config = {
        "kinds": None if chosen is None else sorted(set(chosen)),
        "action": action,
        "mask": mask if action == "redact" else None,
    }
    return Prefilter(name, fn, categories=_pii_categories, config=config)


def injection(
    *,
    threshold: float = 0.5,
    extra_patterns: Sequence[str | re.Pattern[str]] = (),
    name: str = "injection",
) -> Prefilter:
    """Prompt-injection pre-filter (``no_prompt_injection``), category ``prompt_injection``.

    ``Verdict.score`` is the summed signal score capped at 1 (the raw sum is in
    ``evidence["raw_score"]``). A score above 0 on an allowed text is a *signal* that
    ``tiered(..., escalate="on_signal")`` uses to call the next tier.
    """
    fn = no_prompt_injection(extra_patterns=extra_patterns, threshold=threshold)
    patterns = [[p, 0] if isinstance(p, str) else [p.pattern, int(p.flags)] for p in extra_patterns]
    config = {"threshold": threshold, "extra_patterns": patterns}
    return Prefilter(name, fn, categories=("prompt_injection",), config=config)


def length(
    chars: int | None = None, tokens: int | None = None, *, name: str = "length"
) -> Prefilter:
    """Length cap (``max_length``); the token count is an estimate."""
    return Prefilter(
        name, max_length(chars=chars, tokens=tokens), config={"chars": chars, "tokens": tokens}
    )


def topics(
    keywords: Iterable[str] = (), deny: Iterable[str] = (), *, name: str = "topics"
) -> Prefilter:
    """Keyword topic filter (``allowed_topics``). Lexical, not semantic."""
    keywords, deny = list(keywords), list(deny)
    config = {"keywords": sorted(set(keywords)), "deny": sorted(set(deny))}
    return Prefilter(name, allowed_topics(keywords=keywords, deny=deny), config=config)


def json_only(*, name: str = "json_only") -> Prefilter:
    """Output must be exactly one JSON object or array (``json_output_only``)."""
    return Prefilter(name, json_output_only(), stages=_OUTPUT_STAGES, config={})
