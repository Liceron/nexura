# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Operator-configured model allowlist for the chat playground.

Clients choose among names the operator configured; they can never supply provider
strings or keys. API keys come only from the server environment.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from nexura_agents.models.base import ModelProtocol
from nexura_agents.models.infer import infer_model

DEFAULT_CHAT_MODEL = "fake-deterministic"
FAKE_LABEL = "fake-deterministic (Local Offline Fixture)"
CHAT_MODELS_ENV = "NEXURA_CHAT_MODELS"

_STREAMING_PROVIDERS = frozenset(
    {"openai", "anthropic", "gemini", "google", "vertex", "sarvam", "ollama"}
)
_OFFLINE_PROVIDERS = frozenset({"ollama"})
# Providers that do not fail at construction without a key would send an unauthenticated
# request on first use, so the server checks the environment before building them.
_REQUIRED_ENV: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "sarvam": ("SARVAM_API_KEY",),
}


class ChatModelUnavailableError(Exception):
    """A configured model cannot be built (missing key, dependency or bad spec)."""


@dataclass
class _Entry:
    name: str
    provider: str
    spec: str | None
    instance: Any | None


class ChatModelRegistry:
    """Named chat models: the always-available fake plus operator-configured ones."""

    def __init__(self, models: Mapping[str, ModelProtocol | str] | None = None) -> None:
        self._entries: dict[str, _Entry] = {
            DEFAULT_CHAT_MODEL: _Entry(DEFAULT_CHAT_MODEL, "local", None, None)
        }
        self._lock = threading.Lock()
        for name, model in (models or {}).items():
            if name == DEFAULT_CHAT_MODEL:
                raise ValueError(f"'{DEFAULT_CHAT_MODEL}' is reserved for the offline fixture.")
            if not name.strip():
                raise ValueError("Chat model names must not be empty.")
            if isinstance(model, str):
                provider = model.partition(":")[0] if ":" in model else "unknown"
                self._entries[name] = _Entry(name, provider, model, None)
            else:
                self._entries[name] = _Entry(name, "custom", None, model)

    @property
    def names(self) -> list[str]:
        return list(self._entries)

    def __contains__(self, name: object) -> bool:
        return name in self._entries

    def is_fake(self, name: str) -> bool:
        return name == DEFAULT_CHAT_MODEL

    def provider(self, name: str) -> str:
        return self._entries[name].provider

    def display(self, name: str) -> str:
        """The response ``model`` value: configured name plus provider."""
        if self.is_fake(name):
            return FAKE_LABEL
        return f"{name} ({self._entries[name].provider})"

    def resolve(self, name: str) -> Any:
        """Build (once) and return the model; failures never echo secrets."""
        entry = self._entries[name]
        if entry.instance is not None:
            return entry.instance
        assert entry.spec is not None
        with self._lock:
            if entry.instance is None:
                required = _REQUIRED_ENV.get(entry.provider)
                if required and not any(os.environ.get(var) for var in required):
                    raise ChatModelUnavailableError(
                        f"Model '{name}' is unavailable on this server: set "
                        f"{' or '.join(required)} in the server environment."
                    )
                try:
                    entry.instance = infer_model(entry.spec)
                except Exception as exc:
                    raise ChatModelUnavailableError(
                        f"Model '{name}' is unavailable on this server: its provider "
                        f"({entry.provider}) credentials or dependencies are not configured "
                        f"({type(exc).__name__})."
                    ) from None
        return entry.instance

    def model_ids(self) -> dict[str, str]:
        """Configured name -> the provider's model id (``name=provider:model`` specs, or a
        custom model's ``model_id``); used to price a model under either id."""
        ids: dict[str, str] = {}
        for name, entry in self._entries.items():
            if entry.spec is not None:
                model = entry.spec.partition(":")[2].strip()
            else:
                found = getattr(entry.instance, "model_id", None) if entry.instance else None
                model = found if isinstance(found, str) else ""
            if model:
                ids[name] = model
        return ids

    def describe(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for name, entry in self._entries.items():
            if entry.spec is None and entry.instance is None:
                offline, streaming = True, False
            elif entry.instance is not None:
                caps = getattr(entry.instance, "capabilities", None)
                streaming = bool(getattr(caps, "can_stream", False))
                offline = False
            else:
                streaming = entry.provider in _STREAMING_PROVIDERS
                offline = entry.provider in _OFFLINE_PROVIDERS
            out.append(
                {
                    "name": name,
                    "provider": entry.provider,
                    "label": FAKE_LABEL if name == DEFAULT_CHAT_MODEL else self.display(name),
                    "offline": offline,
                    "streaming": streaming,
                    "default": name == DEFAULT_CHAT_MODEL,
                }
            )
        return out


def parse_chat_model_specs(items: Iterable[str]) -> dict[str, str]:
    """Parse ``NAME=PROVIDER:MODEL`` entries; raises ValueError on malformed input."""
    parsed: dict[str, str] = {}
    for raw in items:
        item = raw.strip()
        if not item:
            continue
        name, sep, spec = item.partition("=")
        name, spec = name.strip(), spec.strip()
        provider, colon, model = spec.partition(":")
        if not sep or not name or not colon or not provider or not model:
            raise ValueError(f"Invalid chat model '{item}'; expected NAME=PROVIDER:MODEL.")
        parsed[name] = spec
    return parsed


def parse_chat_models_env(value: str | None) -> dict[str, str]:
    """Parse ``NEXURA_CHAT_MODELS="name=provider:model,name2=provider:model"``."""
    return parse_chat_model_specs((value or "").split(","))


def chat_models_from_environment(
    cli_items: Iterable[str] | None = None, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Environment entries overridden by CLI entries of the same name."""
    env = environ if environ is not None else os.environ
    merged = parse_chat_models_env(env.get(CHAT_MODELS_ENV))
    merged.update(parse_chat_model_specs(cli_items or []))
    return merged
