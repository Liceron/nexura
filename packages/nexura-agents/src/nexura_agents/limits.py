# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Per-run usage limits and model settings.

Retries and fallbacks live in ``nexura_agents.models.resilience``."""

from __future__ import annotations

import copy
import warnings
from dataclasses import dataclass, fields
from typing import Any

from nexura_agents.errors import UsageLimitExceededError
from nexura_agents.types import Usage


@dataclass(frozen=True)
class UsageLimits:
    """Hard caps checked after every model call. ``None`` means unlimited.

    Unknown token counts are not treated as zero: if a limit on tokens is set and the
    model did not report them, the run fails closed unless ``allow_unknown_usage``.
    """

    request_limit: int | None = None
    input_tokens_limit: int | None = None
    output_tokens_limit: int | None = None
    total_tokens_limit: int | None = None
    allow_unknown_usage: bool = False

    def check(self, requests: int, usage: Usage) -> None:
        """Raise ``UsageLimitExceededError`` if requests or token usage exceed a limit."""
        if self.request_limit is not None and requests > self.request_limit:
            raise UsageLimitExceededError("request_limit", requests, self.request_limit)
        for name, limit, value in (
            ("input_tokens_limit", self.input_tokens_limit, usage.prompt_tokens),
            ("output_tokens_limit", self.output_tokens_limit, usage.completion_tokens),
            ("total_tokens_limit", self.total_tokens_limit, usage.total_tokens),
        ):
            if limit is None:
                continue
            if value is None:
                if not self.allow_unknown_usage:
                    raise UsageLimitExceededError(f"{name} (usage not reported)", -1, limit)
                continue
            if value > limit:
                raise UsageLimitExceededError(name, value, limit)


REASONING_EFFORTS = ("minimal", "low", "medium", "high")
# Settings added in F30, carried on GenerationRequest under the same names.
_EXTRA_SETTINGS = (
    "top_p",
    "seed",
    "presence_penalty",
    "frequency_penalty",
    "reasoning_effort",
    "thinking_budget_tokens",
    "extra",
)


def _check_number(name: str, value: Any, low: float, high: float) -> None:
    from nexura_agents.errors import SDKValidationError

    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise SDKValidationError(
            f"{name} must be a number from {low:g} to {high:g}, got {value!r}."
        )


@dataclass(frozen=True)
class ModelSettings:
    """Generation settings forwarded to models. ``None`` means the model's default.

    Set them on the ``Runner`` for every agent, or on an ``Agent``; the agent's fields win.
    """

    temperature: float | None = None
    max_tokens: int | None = None
    stop_sequences: list[str] | None = None
    # "auto", "required" (must call some tool), "none" (no tools), or one tool's name.
    tool_choice: str | None = None
    top_p: float | None = None
    seed: int | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    # OpenAI reasoning models: "minimal" | "low" | "medium" | "high".
    reasoning_effort: str | None = None
    # Anthropic extended thinking / Gemini thinkingBudget (Gemini: 0 turns thinking off).
    thinking_budget_tokens: int | None = None
    # Extra request fields for this provider, merged into the request body last.
    extra: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        from nexura_agents.errors import SDKValidationError

        t = self.temperature
        if t is not None and (isinstance(t, bool) or not isinstance(t, (int, float)) or t < 0):
            raise SDKValidationError(f"temperature must be a number >= 0, got {t!r}.")
        m = self.max_tokens
        if m is not None and (isinstance(m, bool) or not isinstance(m, int) or m < 1):
            raise SDKValidationError(f"max_tokens must be an integer >= 1, got {m!r}.")
        stops = self.stop_sequences
        if stops is not None:
            # A bare string would be split into one stop sequence per character.
            if isinstance(stops, (str, bytes)) or not isinstance(stops, (list, tuple)):
                raise SDKValidationError(
                    f"stop_sequences must be a list of strings, got {stops!r}."
                )
            if not all(isinstance(s, str) and s for s in stops):
                raise SDKValidationError("stop_sequences must contain non-empty strings.")
            # Frozen: keep a private copy so a caller mutating its list cannot change it.
            object.__setattr__(self, "stop_sequences", list(stops))
        choice = self.tool_choice
        if choice is not None and (not isinstance(choice, str) or not choice.strip()):
            raise SDKValidationError(
                f"tool_choice must be 'auto', 'required', 'none' or a tool name, got {choice!r}."
            )
        _check_number("top_p", self.top_p, 0.0, 1.0)
        _check_number("presence_penalty", self.presence_penalty, -2.0, 2.0)
        _check_number("frequency_penalty", self.frequency_penalty, -2.0, 2.0)
        if self.seed is not None and (
            isinstance(self.seed, bool) or not isinstance(self.seed, int)
        ):
            raise SDKValidationError(f"seed must be an integer, got {self.seed!r}.")
        if self.reasoning_effort is not None and self.reasoning_effort not in REASONING_EFFORTS:
            raise SDKValidationError(
                f"reasoning_effort must be one of {', '.join(REASONING_EFFORTS)}, "
                f"got {self.reasoning_effort!r}."
            )
        b = self.thinking_budget_tokens
        if b is not None and (isinstance(b, bool) or not isinstance(b, int) or b < 0):
            raise SDKValidationError(f"thinking_budget_tokens must be an integer >= 0, got {b!r}.")
        if self.extra is not None:
            if not isinstance(self.extra, dict) or not all(isinstance(k, str) for k in self.extra):
                raise SDKValidationError("extra must be a dict with string keys.")
            object.__setattr__(self, "extra", copy.deepcopy(self.extra))

    def merged(self, override: ModelSettings | None) -> ModelSettings:
        """These settings, with every field that ``override`` sets taking precedence."""
        if override is None:
            return self
        values = {}
        for f in fields(self):
            mine, theirs = getattr(self, f.name), getattr(override, f.name)
            values[f.name] = mine if theirs is None else theirs
        if self.extra and override.extra:
            values["extra"] = {**self.extra, **override.extra}  # override keys win
        return ModelSettings(**values)

    def is_empty(self) -> bool:
        """True when no field is set."""
        return all(getattr(self, f.name) is None for f in fields(self))

    def to_openai_params(self) -> dict[str, Any]:
        """The set fields as OpenAI chat-completions parameters (also used by LiteLLM)."""
        params: dict[str, Any] = {}
        if self.temperature is not None:
            params["temperature"] = self.temperature
        if self.max_tokens is not None:
            params["max_tokens"] = self.max_tokens
        if self.stop_sequences is not None:
            params["stop"] = list(self.stop_sequences)
        if self.tool_choice is not None:
            params["tool_choice"] = openai_tool_choice(self.tool_choice)
        for name in ("top_p", "seed", "presence_penalty", "frequency_penalty", "reasoning_effort"):
            if getattr(self, name) is not None:
                params[name] = getattr(self, name)
        return params


def settings_from_request(request: Any) -> ModelSettings:
    """The generation settings carried by a ``GenerationRequest``."""
    return ModelSettings(
        temperature=getattr(request, "temperature", None),
        max_tokens=getattr(request, "max_tokens", None),
        stop_sequences=getattr(request, "stop_sequences", None),
        tool_choice=getattr(request, "tool_choice", None),
        **{name: getattr(request, name, None) for name in _EXTRA_SETTINGS},
    )


def openai_tool_choice(choice: str) -> Any:
    """``tool_choice`` in the OpenAI chat-completions format (also used by LiteLLM)."""
    if choice in ("auto", "required", "none"):
        return choice
    return {"type": "function", "function": {"name": choice}}


def gemini_tool_config(choice: str) -> dict[str, Any]:
    """``tool_choice`` as a Gemini ``toolConfig``."""
    mode = {"auto": "AUTO", "required": "ANY", "none": "NONE"}.get(choice)
    if mode is not None:
        return {"functionCallingConfig": {"mode": mode}}
    return {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [choice]}}


def anthropic_tool_choice(choice: str) -> dict[str, Any]:
    """``tool_choice`` in the Anthropic Messages format."""
    kind = {"auto": "auto", "required": "any", "none": "none"}.get(choice)
    if kind is not None:
        return {"type": kind}
    return {"type": "tool", "name": choice}


def warn_unsupported(provider: str, request: Any, names: tuple[str, ...]) -> None:
    """Warn (instead of silently dropping) when a set field has no mapping for a provider."""
    unset = [n for n in names if getattr(request, n, None) is not None]
    if unset:
        warnings.warn(
            f"{provider} does not support {', '.join(unset)}; not sent.",
            RuntimeWarning,
            stacklevel=3,
        )


def apply_openai_settings(payload: dict[str, Any], request: Any) -> None:
    """F30 settings for OpenAI-compatible chat completions (OpenAI provider, Ollama)."""
    for name in ("top_p", "seed", "presence_penalty", "frequency_penalty", "reasoning_effort"):
        value = getattr(request, name, None)
        if value is not None:
            payload[name] = value
    warn_unsupported("The OpenAI chat API", request, ("thinking_budget_tokens",))
    if getattr(request, "extra", None):
        merge_extra(payload, request.extra)


def apply_gemini_settings(
    payload: dict[str, Any], gen_config: dict[str, Any], request: Any
) -> None:
    """F30 settings for Gemini / Vertex AI (``generationConfig`` fields)."""
    for name, key in (
        ("top_p", "topP"),
        ("seed", "seed"),
        ("presence_penalty", "presencePenalty"),
        ("frequency_penalty", "frequencyPenalty"),
    ):
        value = getattr(request, name, None)
        if value is not None:
            gen_config[key] = value
    budget = getattr(request, "thinking_budget_tokens", None)
    if budget is not None:
        gen_config["thinkingConfig"] = {"thinkingBudget": budget}
    warn_unsupported("Gemini", request, ("reasoning_effort",))
    if gen_config:
        payload["generationConfig"] = gen_config
    if getattr(request, "extra", None):
        merge_extra(payload, request.extra)


def _check_anthropic_thinking(payload: dict[str, Any], budget: int) -> None:
    """Refuse combinations the Anthropic API rejects, before anything is sent."""
    from nexura_agents.errors import SDKValidationError

    def fail(reason: str) -> None:
        raise SDKValidationError(f"Anthropic extended thinking: {reason}")

    if budget < 1024:
        fail(f"thinking_budget_tokens must be at least 1024, got {budget}.")
    if budget >= payload.get("max_tokens", 0):
        fail(
            f"thinking_budget_tokens ({budget}) must be lower than max_tokens "
            f"({payload.get('max_tokens')})."
        )
    temperature = payload.get("temperature")
    if temperature is not None and temperature != 1:
        fail(f"temperature must be unset or 1 when thinking is on, got {temperature}.")
    top_p = payload.get("top_p")
    if top_p is not None and not 0.95 <= top_p <= 1:
        fail(f"top_p must be between 0.95 and 1 when thinking is on, got {top_p}.")
    forced = payload.get("tool_choice")
    if isinstance(forced, dict) and forced.get("type") in ("any", "tool"):
        # Anthropic does not allow forcing a tool while thinking. Native structured output
        # forces one, and so do tool_choice="required" or a tool name.
        fail(
            "cannot force a tool call (tool_choice 'required' or a tool name, or native "
            "structured output) while thinking is on."
        )


def apply_anthropic_settings(payload: dict[str, Any], request: Any) -> None:
    """F30 settings for the Anthropic Messages API."""
    if getattr(request, "top_p", None) is not None:
        payload["top_p"] = request.top_p
    budget = getattr(request, "thinking_budget_tokens", None)
    if budget:  # 0 (like None) leaves extended thinking off
        _check_anthropic_thinking(payload, budget)
        payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
    warn_unsupported(
        "Anthropic", request, ("seed", "presence_penalty", "frequency_penalty", "reasoning_effort")
    )
    if getattr(request, "extra", None):
        merge_extra(payload, request.extra)


def merge_extra(body: dict[str, Any], extra: dict[str, Any] | None) -> None:
    """Merge ``ModelSettings.extra`` into a request body without replacing what the SDK set.

    A key the body already has (``model``, ``messages``, ``stream``, a setting the SDK maps
    itself, ...) raises ``SDKValidationError``; use the dedicated setting instead.
    """
    from nexura_agents.errors import SDKValidationError

    if not extra:
        return
    clash = sorted(key for key in extra if key in body)
    if clash:
        raise SDKValidationError(
            f"ModelSettings.extra cannot replace {', '.join(clash)}, which the SDK already "
            "sets for this request; use the dedicated setting instead."
        )
    body.update(copy.deepcopy(extra))
