# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""ShieldGemma (2B, 9B, 27B) over Ollama or an OpenAI-compatible server.

Prompt format, pinned (accessed 2026-10-24):

- Google, ShieldGemma model card, "prompt pattern" and the prompt-only and
  prompt-response guideline tables: https://ai.google.dev/gemma/docs/shieldgemma/model_card
- Hugging Face ``google/shieldgemma-2b`` (revision
  ``d1dffc9c8c9237a90aab09c61383791e718ef9e8``): scoring mode, P(Yes) from the softmax of
  the ``Yes`` and ``No`` logits at the last position;
- the Ollama ``shieldgemma`` template (blob
  ``sha256:1b667144a522432ff2be769cedd83e6357ccb4788f56853cbe81de39ac57d7b9``) for the
  prompt-response layout (``Chatbot Response:`` turn).

ShieldGemma checks one policy per prompt, so this adapter sends one call per harm type
(``dangerous``, ``harassment``, ``hate``, ``sexual``; all four by default), concurrently.
The guideline text is the model card's, in the prompt-only wording for user text and the
"The chatbot shall not generate" wording for agent text.

Output: the reply must start with ``Yes`` or ``No`` (the card's instruction; the model then
explains, which the output cap cuts off). The score of a harm is P(Yes) from the first
token's log probabilities when the server reports them (``logprobs=True``, the default),
else 1.0 for Yes and 0.0 for No. A harm is violated when the reply says Yes **or** its score
is at least ``threshold`` (default 0.5): when the text and the probability disagree, the
check fails closed and records ``evidence["answer_mismatch"]``. So a threshold above 0.5
cannot loosen the check: a "Yes" reply blocks whatever its probability; such a threshold
only matters for "No" replies (it never does at temperature 0, where "No" means P(Yes) <
0.5). A threshold below 0.5 makes the check stricter. A reply that starts with neither word
is malformed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

from nexura_guardrails.classifiers._base import (
    Completion,
    GuardCall,
    MalformedOutputError,
    Outcome,
    SafetyModelCheck,
    defuse,
    first_token_probability,
    subject_of,
)
from nexura_guardrails.verdict import CheckInput

__all__ = ["SHIELDGEMMA_HARMS", "ShieldGemma"]

_DANGEROUS = (
    "harming oneself and/or others (e.g., accessing or building firearms and explosive "
    "devices, promotion of terrorism, instructions for suicide)."
)
_HARASSMENT = (
    "is malicious, intimidating, bullying, or abusive content targeting another individual "
    "(e.g., physical threats, denial of tragic events, disparaging victims of violence)."
)
_HATE = (
    "targeting identity and/or protected attributes (e.g., racial slurs, promotion of "
    "discrimination, calls to violence against protected groups) or "
    "dehumanizing/belittling/vilifying on the bases of race, ethnicity, religion, disability, "
    "age, nationality, veteran status, sexual orientation, gender, gender identity, caste, or "
    "any other protected status. We do allow content that includes a reference to a "
    "protected individual or group in a positive manner."
)
_SEXUAL = (
    "contains references to sexual acts or other lewd content (e.g., sexually graphic "
    "descriptions, content aimed at causing arousal). Medical or scientific terms related to "
    "human anatomy or sex education are permitted."
)

SHIELDGEMMA_HARMS: dict[str, tuple[str, str]] = {
    "dangerous": ("No Dangerous Content", _DANGEROUS),
    "harassment": ("No Harassment", _HARASSMENT),
    "hate": ("No Hate Speech", _HATE),
    "sexual": ("No Sexually Explicit Information", _SEXUAL),
}
"""Harm id to (guideline title, guideline body) from the ShieldGemma model card."""

_ANSWER = re.compile(r"(yes|no)\b", re.IGNORECASE)


class ShieldGemma(SafetyModelCheck):
    """ShieldGemma as a check: ``ShieldGemma("shieldgemma:2b")`` (default).

    ``harms``: the harm types to check, one model call each (default all four).
    ``threshold``: the P(Yes) at or above which a harm counts as violated (default 0.5). A
    "Yes" reply counts as violated whatever the threshold, so raising it above 0.5 cannot
    loosen the check; lowering it makes the check stricter.
    Harms map to taxonomy ids with ``LABEL_MAPS["shieldgemma"]`` (``dangerous`` gives
    ``violence``, ``weapons`` and ``self_harm``). The model card covers English only.
    """

    source = "shieldgemma"
    family = "shieldgemma"
    prompt_version = "shieldgemma/google-2024-07"
    default_model = "shieldgemma:2b"
    default_name = "shieldgemma"
    default_max_output_tokens = 8
    default_context_tokens = 8192  # Gemma 2's context length
    default_logprobs = True

    def __init__(
        self,
        model: str | None = None,
        *,
        harms: Iterable[str] = tuple(SHIELDGEMMA_HARMS),
        threshold: float = 0.5,
        **kwargs: Any,
    ) -> None:
        self._harms_requested = (harms,) if isinstance(harms, str) else tuple(harms)
        self.threshold = threshold
        super().__init__(model, **kwargs)

    def _validate(self) -> None:
        super()._validate()
        harms: list[str] = []
        for harm in self._harms_requested:
            if harm not in SHIELDGEMMA_HARMS:
                raise ValueError(
                    f"Unknown ShieldGemma harm {harm!r}; choose from {list(SHIELDGEMMA_HARMS)}."
                )
            if harm not in harms:
                harms.append(harm)
        if not harms:
            raise ValueError("harms must not be empty.")
        self.harms = tuple(harms)
        t = self.threshold
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not 0.0 < t <= 1.0:
            raise ValueError("threshold must be a number above 0 and at most 1.")
        self.threshold = float(t)

    def config(self) -> dict[str, Any]:
        return {"harms": list(self.harms), "threshold": self.threshold}

    def calls(self, item: CheckInput) -> list[GuardCall]:
        text, _ = defuse(item.text)
        agent = subject_of(item) == "agent"
        if agent:
            context, _ = defuse(item.context or "")
            preamble = (
                "You are a policy expert trying to help determine whether an AI response to a "
                "prompt is in violation of the defined safety policies."
            )
            turns = (
                f"<start_of_turn>\nHuman Question: {context}\n<end_of_turn>\n"
                f"<start_of_turn>\nChatbot Response: {text}\n<end_of_turn>"
            )
            subject, question = "The chatbot shall not generate content that", "chatbot response"
        else:
            preamble = (
                "You are a policy expert trying to help determine whether a user prompt is in "
                "violation of the defined safety policies."
            )
            turns = f"<start_of_turn>\nHuman Question: {text}\n<end_of_turn>"
            subject = "The prompt shall not contain or seek generation of content that"
            question = "human question"
        out: list[GuardCall] = []
        for harm in self.harms:
            title, body = SHIELDGEMMA_HARMS[harm]
            prompt = (
                f"{preamble}\n\n{turns}\n\n"
                "Our safety principle is defined in the below:\n\n"
                f'* "{title}": {subject} {body}\n\n'
                "===\n\n"
                f"Does the {question} violate the above principle? Your answer must start with "
                "'Yes' or 'No'. And then walk through step by step to be sure we answer "
                "correctly.\n\n"
            )
            out.append(GuardCall(prompt, label=harm))
        return out

    def decide(
        self, item: CheckInput, calls: Sequence[GuardCall], completions: Sequence[Completion]
    ) -> Outcome:
        scores: dict[str, float] = {}
        said_yes: dict[str, bool] = {}
        for call, completion in zip(calls, completions, strict=True):
            match = _ANSWER.match(completion.text.strip())
            if match is None:
                raise MalformedOutputError(f"reply for {call.label} does not start with Yes or No")
            yes = match.group(1).lower() == "yes"
            probability = (
                first_token_probability(completion, "yes", "no") if self.logprobs else None
            )
            if probability is None:
                probability = 1.0 if yes else 0.0
            scores[str(call.label)] = probability
            said_yes[str(call.label)] = yes
        # Fail closed: a harm counts as violated when the text answer says Yes OR the
        # probability reaches the threshold; a disagreement is recorded in the evidence.
        violated = [h for h in self.harms if said_yes[h] or scores[h] >= self.threshold]
        mismatch = [h for h in self.harms if said_yes[h] != (scores[h] >= self.threshold)]
        score = max(scores.values())
        evidence: dict[str, Any] = {
            "harm_scores": {h: round(s, 4) for h, s in scores.items()},
            "labels": violated,
        }
        if mismatch:
            evidence["answer_mismatch"] = mismatch
        if not violated:
            return Outcome(
                "allow", score=score, reason="shieldgemma: no policy violated", evidence=evidence
            )
        return Outcome(
            "block",
            categories=self.map_labels(violated),
            score=score,
            reason=f"shieldgemma: violates {', '.join(violated)}",
            evidence=evidence,
        )
