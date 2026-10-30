# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Local safety-model checks: Llama Guard 3, ShieldGemma, Granite Guardian, Nemotron Safety Guard.

Each adapter sends the model's own documented prompt to a server you run (Ollama by
default, or any OpenAI-compatible text-completion endpoint such as vLLM), parses the
model's documented output strictly and returns a ``Verdict`` with Nexura taxonomy ids.
They need no extra dependency (``pip install "nexura-guardrails[ollama]"`` is an empty
marker): HTTP goes through ``httpx``, which ``nexura-agents`` already depends on.

Nexura never downloads, bundles or redistributes model weights. You pull a model yourself
(``ollama pull llama-guard3:1b``) and accept its license; ``SAFETY_MODELS`` lists each
model's license and source.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from nexura_guardrails.classifiers._base import (
    DEFAULT_OLLAMA_URL,
    DEFAULT_OPENAI_URL,
    MalformedOutputError,
    SafetyModelCheck,
    is_local_url,
)
from nexura_guardrails.classifiers.granite_guardian import GraniteGuardian
from nexura_guardrails.classifiers.llama_guard import LlamaGuard
from nexura_guardrails.classifiers.nemotron import NemotronGuard
from nexura_guardrails.classifiers.shieldgemma import ShieldGemma

__all__ = [
    "DEFAULT_OLLAMA_URL",
    "DEFAULT_OPENAI_URL",
    "SAFETY_MODELS",
    "GraniteGuardian",
    "LlamaGuard",
    "MalformedOutputError",
    "NemotronGuard",
    "SafetyModel",
    "SafetyModelCheck",
    "ShieldGemma",
    "is_local_url",
    "safety_model_for",
]


@dataclass(frozen=True)
class SafetyModel:
    """One supported safety model: where it comes from and under which license.

    ``ollama`` is the Ollama library tag (None when there is no official build).
    ``revision`` is the Hugging Face model-card revision the prompt format was checked
    against (2026-10-24). ``languages`` is what the model card claims.
    """

    id: str
    adapter: str
    ollama: str | None
    source: str
    revision: str
    license: str
    license_url: str
    languages: str

    @property
    def repo(self) -> str:
        """The Hugging Face repository id (``owner/name``), the served name on vLLM."""
        return self.source.removeprefix("https://huggingface.co/")

    def served_names(self) -> tuple[str, ...]:
        """The exact model names that are this model: its Ollama tag and its HF repo id."""
        return tuple(n for n in (self.ollama, self.repo) if n)


SAFETY_MODELS: Mapping[str, SafetyModel] = MappingProxyType(
    {
        m.id: m
        for m in (
            SafetyModel(
                id="llama-guard-3-1b",
                adapter="LlamaGuard",
                ollama="llama-guard3:1b",
                source="https://huggingface.co/meta-llama/Llama-Guard-3-1B",
                revision="acf7aafa60f0410f8f42b1fa35e077d705892029",
                license="Llama 3.2 Community License (with the Llama 3.2 Acceptable Use Policy)",
                license_url="https://www.llama.com/llama3_2/license/",
                languages="English, French, German, Hindi, Italian, Portuguese, Spanish, Thai",
            ),
            SafetyModel(
                id="llama-guard-3-8b",
                adapter="LlamaGuard",
                ollama="llama-guard3:8b",
                source="https://huggingface.co/meta-llama/Llama-Guard-3-8B",
                revision="7327bd9f6efbbe6101dc6cc4736302b3cbb6e425",
                license="Llama 3.1 Community License (with the Llama 3.1 Acceptable Use Policy)",
                license_url="https://www.llama.com/llama3_1/license/",
                languages="English, French, German, Hindi, Italian, Portuguese, Spanish, Thai",
            ),
            SafetyModel(
                id="shieldgemma-2b",
                adapter="ShieldGemma",
                ollama="shieldgemma:2b",
                source="https://huggingface.co/google/shieldgemma-2b",
                revision="d1dffc9c8c9237a90aab09c61383791e718ef9e8",
                license="Gemma Terms of Use (with the Gemma Prohibited Use Policy)",
                license_url="https://ai.google.dev/gemma/terms",
                languages="English",
            ),
            SafetyModel(
                id="shieldgemma-9b",
                adapter="ShieldGemma",
                ollama="shieldgemma:9b",
                source="https://huggingface.co/google/shieldgemma-9b",
                revision="b8b636016df4540721a098c7aab91c97ec6ee508",
                license="Gemma Terms of Use (with the Gemma Prohibited Use Policy)",
                license_url="https://ai.google.dev/gemma/terms",
                languages="English",
            ),
            SafetyModel(
                id="granite-guardian-3.0-2b",
                adapter="GraniteGuardian",
                ollama="granite3-guardian:2b",
                source="https://huggingface.co/ibm-granite/granite-guardian-3.0-2b",
                revision="e48b7b8acf438d24daa2271ada6df945b5b8895e",
                license="Apache-2.0",
                license_url="https://www.apache.org/licenses/LICENSE-2.0",
                languages="English",
            ),
            SafetyModel(
                id="granite-guardian-3.0-8b",
                adapter="GraniteGuardian",
                ollama="granite3-guardian:8b",
                source="https://huggingface.co/ibm-granite/granite-guardian-3.0-8b",
                revision="81c9105958545715d412c688afb862e4b1395f56",
                license="Apache-2.0",
                license_url="https://www.apache.org/licenses/LICENSE-2.0",
                languages="English",
            ),
            SafetyModel(
                id="granite-guardian-4.1-8b",
                adapter="GraniteGuardian",
                ollama="granite4.1-guardian:8b",
                source="https://huggingface.co/ibm-granite/granite-guardian-4.1-8b",
                revision="ab01ccca5dcfb80246369a086a4a87a29198f5af",
                license="Apache-2.0",
                license_url="https://www.apache.org/licenses/LICENSE-2.0",
                languages="English",
            ),
            SafetyModel(
                id="nemotron-safety-guard-8b-v3",
                adapter="NemotronGuard",
                ollama=None,
                source="https://huggingface.co/nvidia/Llama-3.1-Nemotron-Safety-Guard-8B-v3",
                revision="8fdc246ba3d56db9c469d534233b9f582d3afafa",
                license="NVIDIA Open Model License (built with Llama: the Llama 3.1 Community "
                "License also applies)",
                license_url=(
                    "https://www.nvidia.com/en-us/agreements/enterprise-software/"
                    "nvidia-open-model-license/"
                ),
                languages="English, Spanish, Mandarin, German, French, Hindi, Japanese, Arabic, "
                "Thai (trained); about 20 more zero-shot",
            ),
        )
    }
)
"""The supported safety models, their sources and licenses (checked 2026-10-24)."""

# Ollama's ":latest" for these names, as listed in the Ollama library (checked 2026-10-24).
_LATEST = {"llama-guard3": "8b", "granite3-guardian": "2b"}


def safety_model_for(model: str, *, api: str = "ollama") -> SafetyModel | None:
    """The ``SAFETY_MODELS`` entry for a served model name, or None when it is unknown.

    Exact Ollama tags and Hugging Face repo ids match directly. An Ollama tag variant
    (``llama-guard3:8b-q4_K_M``, ``granite3-guardian:8b-fp16``) or a bare or ``:latest`` name
    resolves to the entry of the same size (``:latest`` only where the Ollama library says
    which size it is). Anything else gives None: a wrong license is never returned.
    """
    name = model.strip().removeprefix("ollama:")
    for entry in SAFETY_MODELS.values():
        if name == entry.ollama or name.lower() == entry.repo.lower():
            return entry
    if api != "ollama":
        return None
    base, _, tag = name.partition(":")
    size = (tag or "latest").split("-", 1)[0].lower()
    if size == "latest":
        size = _LATEST.get(base, "")
    if not size:
        return None
    for entry in SAFETY_MODELS.values():
        if entry.ollama == f"{base}:{size}":
            return entry
    return None
