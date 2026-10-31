# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""OpenAI's Moderation API as a guardrail check.

``OpenAIModeration`` sends the checked text to ``POST {base_url}/moderations`` (model
``omni-moderation-latest`` by default) and maps the 13 OpenAI harm labels to the Nexura
taxonomy through ``LABEL_MAPS["openai_moderation"]``:

=============================================  ===================
OpenAI label                                   Nexura category
=============================================  ===================
``violence``, ``violence/graphic``             ``violence``
``illicit``                                    ``crime``
``illicit/violent``                            ``weapons``
``sexual/minors``                              ``child_safety``
``hate``, ``hate/threatening``                 ``hate``
``self-harm`` (and ``/intent``, ``/instructions``)  ``self_harm``
``sexual``                                     ``sexual``
``harassment``, ``harassment/threatening``     ``harassment``
=============================================  ===================

The decision follows OpenAI's ``flagged`` field by default. ``threshold`` blocks on any
category score at or above it instead (OpenAI's own flags are then ignored), and
``labels`` limits which OpenAI labels count. Moderation does not cover prompt injection,
jailbreaks, personal data or groundedness: combine it with the pre-filters and the judge.

The checked text leaves your machine for OpenAI, so this check suits inputs you may send
to OpenAI anyway; for private data use a local safety model instead.

Fail closed: a timeout, connection error, HTTP error (401, 429, 5xx...), a reply larger
than ``MAX_RESPONSE_BYTES``, a body that is not the documented shape, or a reply from a
different model gives ``Verdict(decision="error")``, softened only by an explicit
``on_error``. Text whose strict token upper bound exceeds ``max_input_tokens`` is never
sent: that is a *hard* ``ContentTooLong`` error no ``on_error`` softens. The API key is
read from ``api_key`` or ``OPENAI_API_KEY`` and never appears in ``repr``, errors,
evidence or logs; evidence holds only labels and scores, never the checked text.
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from collections.abc import Iterable, Mapping
from typing import Any, Literal

import httpx
from nexura_agents.run_context import RunContext

from nexura_guardrails.classifiers._base import _CallError, post_json
from nexura_guardrails.compose import VerdictCache
from nexura_guardrails.judge import token_upper_bound
from nexura_guardrails.verdict import LABEL_MAPS, STAGES, CheckInput, Verdict

__all__ = ["DEFAULT_MODERATION_MODEL", "OPENAI_MODERATION_LABELS", "OpenAIModeration"]

DEFAULT_MODERATION_MODEL = "omni-moderation-latest"
"""OpenAI's current moderation model (free of charge for OpenAI API users)."""

OPENAI_MODERATION_LABELS: tuple[str, ...] = tuple(LABEL_MAPS["openai_moderation"])
"""The 13 labels OpenAI reports, each mapped to a Nexura category."""

OnError = Literal["block", "allow", "review"]
_DEFAULT_STAGES = frozenset({"input", "output", "tool_input", "tool_output"})
_OPENAI_URL = "https://api.openai.com/v1"


class OpenAIModeration:
    """A non-deterministic ``Check`` backed by OpenAI's Moderation API."""

    deterministic = False

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = DEFAULT_MODERATION_MODEL,
        base_url: str = _OPENAI_URL,
        name: str = "openai_moderation",
        stages: Iterable[str] = _DEFAULT_STAGES,
        threshold: float | None = None,
        labels: Iterable[str] | None = None,
        include_context: bool = False,
        timeout: float = 10.0,
        max_input_tokens: int = 32_000,
        on_error: OnError = "block",
        cache: VerdictCache | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY")
        if not self._api_key:
            raise ValueError("OpenAIModeration needs api_key= or the OPENAI_API_KEY variable.")
        self.model = model
        self.base_url = base_url.rstrip("/")
        if not self.base_url.startswith("https://") and transport is None:
            # Plain HTTP would send the key and the text unencrypted (tests pass a transport).
            raise ValueError("base_url must use https://.")
        self.name = name
        self.stages = frozenset(stages)
        unknown_stages = self.stages - set(STAGES)
        if unknown_stages:
            raise ValueError(f"unknown stages: {sorted(unknown_stages)}")
        if threshold is not None and not (0.0 < threshold <= 1.0):
            raise ValueError("threshold must be in (0, 1].")
        self.threshold = threshold
        chosen = tuple(labels) if labels is not None else OPENAI_MODERATION_LABELS
        unknown = [label for label in chosen if label not in LABEL_MAPS["openai_moderation"]]
        if unknown or not chosen:
            raise ValueError(f"labels must be among {list(OPENAI_MODERATION_LABELS)}.")
        self.labels = chosen
        self.include_context = include_context
        if timeout <= 0 or max_input_tokens <= 0:
            raise ValueError("timeout and max_input_tokens must be positive.")
        self.timeout = timeout
        self.max_input_tokens = max_input_tokens
        if on_error not in ("block", "allow", "review"):
            raise ValueError("on_error must be 'block', 'allow' or 'review'.")
        self.on_error: OnError = on_error
        self.cache = cache
        self._transport = transport

    @property
    def backend(self) -> str:
        return f"openai-moderation:{self.model}"

    def __repr__(self) -> str:  # never the key
        return f"OpenAIModeration(model={self.model!r}, name={self.name!r})"

    # ------------------------------------------------------------------ verdicts

    def _common(self, started: float) -> dict[str, Any]:
        return {
            "check": self.name,
            "backend": self.backend,
            "latency_ms": (time.perf_counter() - started) * 1000,
        }

    def _failure(self, error_type: str, detail: str, started: float) -> Verdict:
        evidence = {"error_type": error_type, "detail": detail, "on_error": self.on_error}
        if self.on_error == "allow":
            return Verdict(
                decision="allow",
                reason=f"OpenAI moderation could not decide ({error_type}); allowed by "
                "on_error='allow'",
                evidence=evidence,
                **self._common(started),
            )
        if self.on_error == "review":
            return Verdict(
                decision="review",
                reason=f"OpenAI moderation could not decide ({error_type}); needs review",
                evidence=evidence,
                **self._common(started),
            )
        return Verdict(
            decision="error",
            reason=f"OpenAI moderation could not decide ({error_type})",
            evidence=evidence,
            **self._common(started),
        )

    def _parse(self, data: Any) -> tuple[bool, dict[str, bool], dict[str, float]]:
        if not isinstance(data, Mapping):
            raise _CallError("MalformedResponse", "reply is not an object")
        reported = data.get("model")
        if isinstance(reported, str) and not reported.startswith(self.model.split("-latest")[0]):
            raise _CallError("ModelMismatch", f"reply from {reported[:60]!r}")
        results = data.get("results")
        if (
            not isinstance(results, list)
            or len(results) != 1
            or not isinstance(results[0], Mapping)
        ):
            raise _CallError("MalformedResponse", "expected exactly one result")
        result = results[0]
        flagged = result.get("flagged")
        categories = result.get("categories")
        scores = result.get("category_scores")
        if not isinstance(flagged, bool) or not isinstance(categories, Mapping):
            raise _CallError("MalformedResponse", "missing flagged or categories")
        if not isinstance(scores, Mapping):
            raise _CallError("MalformedResponse", "missing category_scores")
        flags: dict[str, bool] = {}
        values: dict[str, float] = {}
        for label in OPENAI_MODERATION_LABELS:
            flag = categories.get(label)
            score = scores.get(label)
            if flag is not None and not isinstance(flag, bool):
                raise _CallError("MalformedResponse", f"category {label!r} is not a boolean")
            if score is not None:
                if isinstance(score, bool) or not isinstance(score, (int, float)):
                    raise _CallError("MalformedResponse", f"score {label!r} is not a number")
                if not math.isfinite(score) or not 0.0 <= score <= 1.0:
                    raise _CallError("MalformedResponse", f"score {label!r} out of range")
                values[label] = float(score)
            flags[label] = bool(flag)
        return flagged, flags, values

    def _decide(
        self, flagged: bool, flags: dict[str, bool], scores: dict[str, float], started: float
    ) -> Verdict:
        if self.threshold is not None:
            hits = [label for label in self.labels if scores.get(label, 0.0) >= self.threshold]
        else:
            hits = [label for label in self.labels if flags.get(label)]
            if flagged and not hits and self.labels == OPENAI_MODERATION_LABELS:
                # Flagged without a known label (a label OpenAI added later): block as "other".
                hits = ["unknown"]
        considered = [scores[label] for label in self.labels if label in scores]
        top = sorted(
            ((label, scores[label]) for label in self.labels if label in scores),
            key=lambda pair: -pair[1],
        )[:3]
        evidence: dict[str, Any] = {
            "model": self.model,
            "flagged": flagged,
            "labels": sorted(label for label in hits if label != "unknown"),
            "top_scores": {label: round(value, 4) for label, value in top},
        }
        score = max(considered) if considered else None
        if not hits:
            return Verdict(
                decision="allow",
                score=score,
                reason="OpenAI moderation found no flagged category",
                evidence=evidence,
                **self._common(started),
            )
        categories: list[str] = []
        for label in hits:
            for category in LABEL_MAPS["openai_moderation"].get(label, ("other",)):
                if category not in categories:
                    categories.append(category)
        return Verdict(
            decision="block",
            categories=tuple(categories),
            score=score,
            reason="OpenAI moderation flagged: " + ", ".join(categories),
            evidence=evidence,
            **self._common(started),
        )

    # ------------------------------------------------------------------ check

    async def __call__(self, ctx: RunContext[Any], item: CheckInput) -> Verdict:
        """Moderate ``item.text`` (and its context when ``include_context``)."""
        started = time.perf_counter()
        text = item.text
        if self.include_context and item.context:
            text = f"{item.context}\n\n{text}"
        size = token_upper_bound(text)
        if size > self.max_input_tokens:
            return Verdict(
                decision="error",
                reason="content too long for OpenAI moderation",
                evidence={
                    "error_type": "ContentTooLong",
                    "hard": True,
                    "detail": f"up to {size} tokens, limit {self.max_input_tokens}",
                },
                **self._common(started),
            )
        key = None
        if self.cache is not None:
            key = self.cache.key(
                ctx,
                f"{self.backend}|{self.threshold}|{','.join(self.labels)}",
                item.stage,
                item.role,
                item.tool_name,
                item.text,
                item.context if self.include_context else None,
            )
            hit = self.cache.get(key)
            if hit is not None:
                return hit
        try:
            data = await asyncio.wait_for(
                post_json(
                    f"{self.base_url}/moderations",
                    {"model": self.model, "input": text},
                    transport=self._transport,
                    timeout=self.timeout,
                    api_key=self._api_key,
                ),
                timeout=self.timeout,
            )
            flagged, flags, scores = self._parse(data)
        except TimeoutError:
            return self._failure("TimeoutError", f"no verdict within {self.timeout}s", started)
        except asyncio.CancelledError:
            raise
        except _CallError as exc:
            return self._failure(exc.error_type, exc.detail, started)
        except Exception as exc:  # fail closed on anything unexpected
            return self._failure(type(exc).__name__, "moderation call failed", started)
        verdict = self._decide(flagged, flags, scores, started)
        if self.cache is not None and key is not None:
            self.cache.put(key, verdict)
        return verdict
