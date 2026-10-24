# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""NVIDIA Llama-3.1-Nemotron-Safety-Guard-8B-v3 over an OpenAI-compatible server or Ollama.

Prompt format, pinned (accessed 2026-10-24): model card and ``inference_script.py`` of
``nvidia/Llama-3.1-Nemotron-Safety-Guard-8B-v3`` (revision
``8fdc246ba3d56db9c469d534233b9f582d3afafa``, model version v1.0), "Prompt Format" and
"Quick Start": the instruction, the categories S1 to S23, the conversation (``user:`` and,
for a response, ``response: agent:``) and the output-JSON instruction, sent as one user turn
of the model's Llama 3.1 chat template (``tokenizer_config.json`` of the same revision:
a system header with "Cutting Knowledge Date: December 2023" and "Today Date: 26 Jul 2024",
then the user turn, then the assistant header). The tokenizer adds ``<|begin_of_text|>``.

There is no official Ollama build; serve the model with vLLM (``api="openai"``) or import a
GGUF conversion into Ollama yourself. ``model`` has no default for that reason.

Output: one JSON object with ``"User Safety"`` (``safe``/``unsafe``, required),
``"Response Safety"`` (only when a response was sent, then required) and
``"Safety Categories"`` (a comma-separated string, only when something is unsafe). The
category names follow the taxonomy in the prompt and come back in any case (the card's
example is ``"guns and illegal weapons"``); S-codes are accepted too. The decision uses
``User Safety`` for user-side stages and ``Response Safety`` for agent-side stages. Other
keys, other values, prose around the JSON or two objects are malformed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from nexura_guardrails.classifiers._base import (
    Completion,
    GuardCall,
    MalformedOutputError,
    Outcome,
    SafetyModelCheck,
    defuse,
    subject_of,
)
from nexura_guardrails.verdict import CheckInput

__all__ = ["NEMOTRON_V3_CATEGORIES", "NemotronGuard"]

NEMOTRON_V3_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("S1", "Violence"),
    ("S2", "Sexual"),
    ("S3", "Criminal Planning/Confessions"),
    ("S4", "Guns and Illegal Weapons"),
    ("S5", "Controlled/Regulated Substances"),
    ("S6", "Suicide and Self Harm"),
    ("S7", "Sexual (minor)"),
    ("S8", "Hate/Identity Hate"),
    ("S9", "PII/Privacy"),
    ("S10", "Harassment"),
    ("S11", "Threat"),
    ("S12", "Profanity"),
    ("S13", "Needs Caution"),
    ("S14", "Other"),
    ("S15", "Manipulation"),
    ("S16", "Fraud/Deception"),
    ("S17", "Malware"),
    ("S18", "High Risk Gov Decision Making"),
    ("S19", "Political/Misinformation/Conspiracy"),
    ("S20", "Copyright/Trademark/Plagiarism"),
    ("S21", "Unauthorized Advice"),
    ("S22", "Illegal Activity"),
    ("S23", "Immoral/Unethical"),
)
"""The Nemotron Safety Guard v3 taxonomy, in prompt order."""

_BY_NAME = {name.casefold(): code for code, name in NEMOTRON_V3_CATEGORIES}
_CODES = {code for code, _ in NEMOTRON_V3_CATEGORIES}
_CODE_PREFIX = re.compile(r"(S[1-9][0-9]?)\s*(?::\s*(.*))?", re.IGNORECASE)
_FENCED = re.compile(r"\A```(?:json)?[ \t]*\n(.*)\n```\Z", re.DOTALL)
_KEYS = frozenset({"User Safety", "Response Safety", "Safety Categories"})
_SAFETY = ("safe", "unsafe")

_CATEGORY_LINES = "\n".join(f"{code}: {name}. " for code, name in NEMOTRON_V3_CATEGORIES)


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise MalformedOutputError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _code_for(label: str) -> str | None:
    text = label.strip().rstrip(".").strip()
    if not text:
        return None
    match = _CODE_PREFIX.fullmatch(text)
    if match:
        code = match.group(1).upper()
        name = (match.group(2) or "").strip().rstrip(".").strip()
        if code not in _CODES or (name and _BY_NAME.get(name.casefold()) != code):
            return None
        return code
    return _BY_NAME.get(text.casefold())


class NemotronGuard(SafetyModelCheck):
    """Nemotron Safety Guard v3 as a check: ``NemotronGuard("<served model name>",
    api="openai", base_url="http://localhost:8000/v1")``.

    Categories map to taxonomy ids with ``LABEL_MAPS["nemotron"]`` (S5 and S18 have no
    Nexura equivalent and map to ``other``). The model card reports training in nine
    languages; this package documents and tests English only. See ``SafetyModelCheck`` for
    the common options.
    """

    source = "nemotron"
    family = "nemotron"
    prompt_version = "nemotron-safety-guard-8b-v3/1.0"
    default_model = None
    default_name = "nemotron_guard"
    default_max_output_tokens = 100
    default_context_tokens = 16384  # a Llama 3.1 (128k-context) model

    def calls(self, item: CheckInput) -> list[GuardCall]:
        text, _ = defuse(item.text)
        if subject_of(item) == "agent":
            context, _ = defuse(item.context or "")
            conversation = f"user: {context}\n\nresponse: agent: {text}\n\n"
        else:
            conversation = f"user: {text}\n\n"
        instruction = (
            "Task: Check if there is unsafe content in the user or agent messages in the below "
            "conversation in accordance with our safety policy with the below categories. "
            "Provide a 'safe' or 'unsafe' rating for both the user message and the agent "
            "response. If there is unsafe content, please also provide a list of violated "
            "categories according to our safety policy below.\n\n"
            f"<BEGIN UNSAFE CONTENT CATEGORIES>\n{_CATEGORY_LINES}\n"
            "<END UNSAFE CONTENT CATEGORIES>\n\n"
            f"<BEGIN CONVERSATION>\n\n{conversation}<END CONVERSATION>\n\n"
            "Provide your safety assessment for the above conversation in the below output "
            "JSON format:\n"
            '{"User Safety": safe or unsafe rating for the user message, "Response Safety": '
            "safe or unsafe rating for the agent response. Omit if no agent response present. "
            '"Safety Categories": a comma-separated list of applicable safety categories from '
            "the provided taxonomy. Omit if all safe.}\n\n"
            "Do not include anything other than the output JSON in your response.\n"
            # The card's Jinja template ends with "Output JSON: ", but the pinned chat
            # template renders message['content'] | trim, so the model sees no trailing space
            # (rendered with jinja2 from tokenizer_config.json, revision 8fdc246b, 2026-10-24).
            "Output JSON:"
        )
        prompt = (
            "<|start_header_id|>system<|end_header_id|>\n\n"
            "Cutting Knowledge Date: December 2023\nToday Date: 26 Jul 2024\n\n<|eot_id|>"
            f"<|start_header_id|>user<|end_header_id|>\n\n{instruction}<|eot_id|>"
            "<|start_header_id|>assistant<|end_header_id|>\n\n"
        )
        return [GuardCall(prompt)]

    def decide(
        self, item: CheckInput, calls: Sequence[GuardCall], completions: Sequence[Completion]
    ) -> Outcome:
        raw = completions[0].text.strip()
        fenced = _FENCED.match(raw)
        if fenced:
            raw = fenced.group(1).strip()
        if not (raw.startswith("{") and raw.endswith("}")):
            raise MalformedOutputError("reply is not a single JSON object")
        try:
            data = json.loads(raw, object_pairs_hook=_no_duplicates)
        except MalformedOutputError:
            raise
        except ValueError:
            raise MalformedOutputError("invalid JSON") from None
        if not isinstance(data, dict):
            raise MalformedOutputError("reply is not a JSON object")
        extra = set(data) - _KEYS
        if extra:
            raise MalformedOutputError(f"unknown keys {sorted(extra)[:3]}")
        agent = subject_of(item) == "agent"
        ratings: dict[str, str] = {}
        for key in ("User Safety", "Response Safety"):
            if key not in data:
                continue
            value = data[key]
            if not isinstance(value, str) or value.strip().lower() not in _SAFETY:
                raise MalformedOutputError(f"{key} must be 'safe' or 'unsafe'")
            ratings[key] = value.strip().lower()
        if "User Safety" not in ratings:
            raise MalformedOutputError("missing 'User Safety'")
        if agent and "Response Safety" not in ratings:
            raise MalformedOutputError("missing 'Response Safety' for a response check")
        if not agent and "Response Safety" in ratings:
            raise MalformedOutputError("'Response Safety' given without a response")
        labels_raw = data.get("Safety Categories")
        if labels_raw is not None and not isinstance(labels_raw, str):
            raise MalformedOutputError("Safety Categories must be a string")
        labels = [p for p in (labels_raw or "").split(",") if p.strip()]
        if labels and all(v == "safe" for v in ratings.values()):
            raise MalformedOutputError("categories given although everything is safe")
        decisive = ratings["Response Safety" if agent else "User Safety"]
        evidence: dict[str, Any] = {
            "user_safety": ratings["User Safety"],
            "response_safety": ratings.get("Response Safety"),
        }
        if decisive == "safe":
            evidence["labels"] = []
            return Outcome("allow", score=0.0, reason="nemotron: safe", evidence=evidence)
        codes: list[str] = []
        unknown = 0
        for label in labels:
            code = _code_for(label)
            if code is None:
                unknown += 1
            elif code not in codes:
                codes.append(code)
        if not labels:
            evidence["label_error"] = "MissingLabels"
        elif unknown:
            evidence["label_error"] = "UnknownLabel"
            evidence["unknown_labels"] = unknown
        if completions[0].finish_reason == "length":
            evidence.setdefault("label_error", "Truncated")
        categories = self.map_labels(codes)
        if "label_error" in evidence and "other" not in categories:
            categories = (*categories, "other")
        evidence["labels"] = codes
        return Outcome(
            "block",
            categories=categories,
            score=1.0,
            reason="nemotron: unsafe" + (f" ({','.join(codes)})" if codes else ""),
            evidence=evidence,
        )
