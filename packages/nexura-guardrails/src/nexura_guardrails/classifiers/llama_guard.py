# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Llama Guard 3 (1B and 8B) over Ollama or an OpenAI-compatible server.

Prompt format, pinned (accessed 2026-10-24):

- Meta, "Llama Guard 3" prompt format:
  https://dev.meta.ai/llama/docs/model-cards-and-prompt-formats/llama-guard-3/
  and ``llama-cookbook`` ``src/llama_cookbook/inference/prompt_format_utils.py`` at commit
  ``c0b6de8632eac6dc6741374d059151a64a30a018`` (``PROMPT_INSTRUCTIONS_2``);
- the Ollama templates of ``llama-guard3:1b`` (template blob
  ``sha256:c66236bdb6a533c89cf4ab45e150418415b1f4e877c9423e2eb73cadd0c5f324``, categories
  S1 to S13) and ``llama-guard3:8b`` (``sha256:cb5a7b24ae22ddee985e72490538cf8d5b3cd4125143f
  23933887ef5bcdb80c2``, S1 to S14), which render the same text;
- model cards ``meta-llama/Llama-Guard-3-1B`` (revision
  ``acf7aafa60f0410f8f42b1fa35e077d705892029``) and ``meta-llama/Llama-Guard-3-8B``
  (revision ``7327bd9f6efbbe6101dc6cc4736302b3cbb6e425``).

The prompt is one user turn in the Llama 3 chat format (the tokenizer adds
``<|begin_of_text|>``), with the role of the checked message (``User`` or ``Agent``) in the
task line and the closing instruction. S14 (Code Interpreter Abuse) is offered only to the
8B model: Meta says the 1B model was not optimised for it.

Output: the first line is ``safe`` or ``unsafe``; when unsafe, a second line lists the
violated categories, comma separated (``S1,S9``). ``safe`` followed by more text, or a
first line that is neither word, is malformed (an ``error`` verdict). ``unsafe`` with a
missing category line or a code that was not offered is still a block (category
``other``, ``evidence["label_error"]``). With ``logprobs=True`` the score is the
probability of ``unsafe`` against ``safe`` at the first token, as the model card defines;
otherwise it is 1.0 for unsafe and 0.0 for safe.
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

__all__ = ["LLAMA_GUARD_3_CATEGORIES", "LlamaGuard"]

LLAMA_GUARD_3_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("S1", "Violent Crimes"),
    ("S2", "Non-Violent Crimes"),
    ("S3", "Sex Crimes"),
    ("S4", "Child Exploitation"),
    ("S5", "Defamation"),
    ("S6", "Specialized Advice"),
    ("S7", "Privacy"),
    ("S8", "Intellectual Property"),
    ("S9", "Indiscriminate Weapons"),
    ("S10", "Hate"),
    ("S11", "Self-Harm"),
    ("S12", "Sexual Content"),
    ("S13", "Elections"),
    ("S14", "Code Interpreter Abuse"),
)
"""The MLCommons hazard codes and names in the Llama Guard 3 prompt."""

_NAMES = dict(LLAMA_GUARD_3_CATEGORIES)
_CODE = re.compile(r"S([1-9][0-9]?)", re.IGNORECASE)


class LlamaGuard(SafetyModelCheck):
    """Llama Guard 3 as a check: ``LlamaGuard("llama-guard3:1b")`` (default) or ``:8b``.

    ``categories`` chooses the S-codes offered in the prompt (default S1 to S13 for a
    model name containing ``1b``, else S1 to S14). Replies are mapped to taxonomy ids with
    ``LABEL_MAPS["llama_guard"]`` (S2 gives ``crime`` and ``fraud``). See
    ``SafetyModelCheck`` for the common options.
    """

    source = "llama_guard"
    family = "llama_guard"
    prompt_version = "llama-guard-3/meta-2024-07"
    default_model = "llama-guard3:1b"
    default_name = "llama_guard"
    default_max_output_tokens = 48
    default_context_tokens = 16384  # a 128k-context model; the byte cap needs the room

    def __init__(
        self, model: str | None = None, *, categories: Iterable[str] | None = None, **kwargs: Any
    ) -> None:
        self._requested = None if categories is None else tuple(categories)
        super().__init__(model, **kwargs)

    def _validate(self) -> None:
        super()._validate()
        if self._requested is None:
            small = "1b" in self.model.lower()
            codes = [c for c, _ in LLAMA_GUARD_3_CATEGORIES if not (small and c == "S14")]
        else:
            codes = []
            for code in self._requested:
                key = code.strip().upper() if isinstance(code, str) else code
                if key not in _NAMES:
                    raise ValueError(f"Unknown Llama Guard 3 category {code!r}; use S1 to S14.")
                if key not in codes:
                    codes.append(key)
            if not codes:
                raise ValueError("categories must not be empty.")
        self.categories = tuple(codes)

    def config(self) -> dict[str, Any]:
        return {"categories": list(self.categories)}

    def calls(self, item: CheckInput) -> list[GuardCall]:
        role = "Agent" if subject_of(item) == "agent" else "User"
        text, _ = defuse(item.text)
        if role == "Agent" and item.context is not None:
            context, _ = defuse(item.context)
            conversation = f"User: {context}\n\nAgent: {text}\n\n"
        else:
            conversation = f"{role}: {text}\n\n"
        lines = "\n".join(f"{code}: {_NAMES[code]}." for code in self.categories)
        prompt = (
            "<|start_header_id|>user<|end_header_id|>\n\n"
            f"Task: Check if there is unsafe content in '{role}' messages in conversations "
            "according our safety policy with the below categories.\n\n"
            f"<BEGIN UNSAFE CONTENT CATEGORIES>\n{lines}\n<END UNSAFE CONTENT CATEGORIES>\n\n"
            f"<BEGIN CONVERSATION>\n\n{conversation}<END CONVERSATION>\n\n"
            f"Provide your safety assessment for ONLY THE LAST {role} message in the above "
            "conversation:\n"
            " - First line must read 'safe' or 'unsafe'.\n"
            " - If unsafe, a second line must include a comma-separated list of violated "
            "categories.<|eot_id|><|start_header_id|>assistant<|end_header_id|>\n\n"
        )
        return [GuardCall(prompt)]

    def decide(
        self, item: CheckInput, calls: Sequence[GuardCall], completions: Sequence[Completion]
    ) -> Outcome:
        completion = completions[0]
        lines = [line.strip() for line in completion.text.strip().splitlines() if line.strip()]
        if not lines:
            raise MalformedOutputError("empty reply")
        first = lines[0]
        probability = (
            first_token_probability(completion, "unsafe", "safe") if self.logprobs else None
        )
        if first == "safe":
            if len(lines) != 1:
                raise MalformedOutputError("text after 'safe'")
            return Outcome(
                "allow",
                score=0.0 if probability is None else probability,
                reason="llama guard: safe",
                evidence={"labels": []},
            )
        if first != "unsafe":
            raise MalformedOutputError("first line is not 'safe' or 'unsafe'")
        score = 1.0 if probability is None else probability
        evidence: dict[str, Any] = {}
        codes: list[str] = []
        if len(lines) != 2:
            evidence["label_error"] = "MissingLabels" if len(lines) == 1 else "ExtraOutput"
        else:
            unknown = 0
            for raw in lines[1].split(","):
                match = _CODE.fullmatch(raw.strip())
                code = f"S{match.group(1)}" if match else None
                if code is None or code not in self.categories:
                    unknown += 1
                elif code not in codes:
                    codes.append(code)
            if unknown:
                evidence["label_error"] = "UnknownLabel"
                evidence["unknown_labels"] = unknown
        if completion.finish_reason == "length":
            evidence.setdefault("label_error", "Truncated")
        categories = self.map_labels(codes)
        if "label_error" in evidence and "other" not in categories:
            categories = (*categories, "other")
        evidence["labels"] = codes
        return Outcome(
            "block",
            categories=categories,
            score=score,
            reason="llama guard: unsafe" + (f" ({','.join(codes)})" if codes else ""),
            evidence=evidence,
        )
