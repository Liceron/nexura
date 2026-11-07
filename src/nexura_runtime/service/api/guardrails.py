# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Guardrails in the service (P39 G6): the operator's policy and saved agents' presets.

**Operator policy.** ``create_api_app(guardrail_policy=...)``, ``nexura-runtime serve
--guardrail-policy FILE`` or ``NEXURA_GUARDRAIL_POLICY=FILE`` name a policy file (data,
``nexura_guardrails.GuardrailPolicy`` JSON or YAML). The service loads it before anything
else, refuses to start when it is missing or invalid or when ``nexura-guardrails`` is not
installed (fail closed), and installs it with ``set_default_hooks(policy, locked=True)``:
every ``Runner`` in the process (chat, saved agents, agent nodes, templates, evaluations)
uses it, and no code in the process can drop it. It is operator-owned like
``approval_required_handlers``: never settable through the API or a workflow document.
``GET /api/v1/guardrails/policy`` shows a summary (no secrets).

**Presets.** A saved agent's ``guardrails`` field names presets (``pii_block``,
``pii_redact``, ``injection_fast``, ``safety_local``, ``judge``, ``guardrails_ai``); the
service builds them with ``nexura_guardrails.presets`` (``guardrails_ai`` with the
``nexura-guardrails-ai`` bridge and the validator packages installed on the server).
``GET /api/v1/guardrails/presets`` lists them with what each needs and costs.

``NEXURA_GUARDRAILS_AI_PRELOAD=1`` imports the installed Guardrails AI validators in a
background thread when the service starts, so the first run that uses them does not wait
seconds for the import.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from nexura_agents.stream_guard import StreamGuardMode

from nexura_runtime.agents_spec import (
    DEFAULT_GUARDRAIL_PRESETS,
    GUARDRAIL_PRESETS,
    GUARDRAILS_AI_VALIDATORS,
    MAX_JUDGE_POLICY_CHARS,
    AgentGuardrails,
    AgentSpecError,
)

__all__ = [
    "GUARDRAIL_POLICY_ENV",
    "AgentGuardrailParts",
    "GuardrailPolicyConfigError",
    "POLICY_EXCEPTIONS",
    "build_agent_guardrails",
    "caller_is_admin",
    "check_agent_guardrails",
    "check_guardrail_lock",
    "guardrails_ai_available",
    "install_guardrail_policy",
    "preload_guardrails_ai",
    "preset_catalog",
    "resolve_guardrail_policy",
    "router",
]

GUARDRAIL_POLICY_ENV = "NEXURA_GUARDRAIL_POLICY"
DEFAULT_SAFETY_MODEL = "llama-guard3:1b"
_INSTALL_HINT = 'pip install "nexura-guardrails[yaml]"'
GUARDRAILS_AI_PRELOAD_ENV = "NEXURA_GUARDRAILS_AI_PRELOAD"
_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _GuardrailsAIValidator:
    module: str
    cls: str
    package: str
    categories: tuple[str, ...]
    description: str


#: How the server builds each name in ``GUARDRAILS_AI_VALIDATORS``: rule-based or small local
#: models only, no model call and no network. Validators that need torch, model downloads or
#: Guardrails' remote inference are not offered.
_GUARDRAILS_AI: dict[str, _GuardrailsAIValidator] = {
    "profanity_free": _GuardrailsAIValidator(
        "guardrails_ai.profanity_free",
        "ProfanityFree",
        "guardrails-ai-profanity-free",
        ("harassment",),
        "Profanity in English (a small local model, alt-profanity-check).",
    ),
    "secrets_present": _GuardrailsAIValidator(
        "guardrails_ai.secrets_present",
        "SecretsPresent",
        "guardrails-ai-secrets-present",
        ("privacy",),
        "API keys, passwords and other secrets (detect-secrets).",
    ),
}
assert set(_GUARDRAILS_AI) == set(GUARDRAILS_AI_VALIDATORS)
_GUARDRAILS_AI_BRIDGE = "nexura_guardrails_ai"


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def guardrails_ai_available() -> dict[str, bool]:
    """Which Guardrails AI validators this server can build (without importing them)."""
    bridge = _installed(_GUARDRAILS_AI_BRIDGE)
    return {name: bridge and _installed(v.module) for name, v in _GUARDRAILS_AI.items()}


def _guardrails_ai_hint(names: list[str] | None = None) -> str:
    chosen = names or list(_GUARDRAILS_AI)
    return "pip install nexura-guardrails-ai " + " ".join(_GUARDRAILS_AI[n].package for n in chosen)


def _import_guardrails_ai() -> None:
    importlib.import_module(_GUARDRAILS_AI_BRIDGE)
    for name, ok in guardrails_ai_available().items():
        if ok:
            importlib.import_module(_GUARDRAILS_AI[name].module)


def preload_guardrails_ai(env: Any = None) -> threading.Thread | None:
    """Import the installed Guardrails AI validators in a background thread when
    ``NEXURA_GUARDRAILS_AI_PRELOAD`` is on (they take seconds to import)."""
    value = (os.environ if env is None else env).get(GUARDRAILS_AI_PRELOAD_ENV, "")
    if value.strip().lower() not in ("1", "true", "on", "yes"):
        return None
    if not any(guardrails_ai_available().values()):
        _log.warning(
            "%s is on but no Guardrails AI validator is installed.", GUARDRAILS_AI_PRELOAD_ENV
        )
        return None

    def run() -> None:
        try:
            _import_guardrails_ai()
        except Exception:  # pragma: no cover - logged; the first run imports again
            _log.exception("Preloading the Guardrails AI validators failed.")

    thread = threading.Thread(target=run, name="nexura-guardrails-ai-preload", daemon=True)
    thread.start()
    return thread


class GuardrailPolicyConfigError(ValueError):
    """The configured guardrail policy cannot be used; the service does not start."""


def _guardrails_module() -> Any:
    try:
        import nexura_guardrails.policy as module
    except ImportError as exc:
        raise GuardrailPolicyConfigError(
            "A guardrail policy is configured but nexura-guardrails is not installed: "
            f"{_INSTALL_HINT}. The service does not start without it."
        ) from exc
    return module


def _resolver(chat_models: Any) -> Any:
    """Judge model names: a configured chat model of the service, else the Nexura model
    string as written (``ollama:qwen3:8b``); credentials come from the environment."""

    def resolve(name: str) -> Any:
        if chat_models is not None and name in chat_models and not chat_models.is_fake(name):
            try:
                return chat_models.resolve(name)
            except Exception as exc:
                raise GuardrailPolicyConfigError(
                    f"The policy's judge model '{name}' is not available: {exc}"
                ) from None
        return name

    return resolve


def resolve_guardrail_policy(value: Any, chat_models: Any = None) -> Any | None:
    """The policy to enforce: ``value`` (a ``GuardrailPolicy``, or a path to a policy file),
    else the file named by ``NEXURA_GUARDRAIL_POLICY``, else None.

    Raises ``GuardrailPolicyConfigError`` for a missing or invalid file, a value of the
    wrong type, or a policy without ``nexura-guardrails`` installed.
    """
    if value is None:
        env = os.environ.get(GUARDRAIL_POLICY_ENV, "").strip()
        if not env:
            return None
        value = env
    if isinstance(value, (str, Path)):
        if not str(value).strip():
            raise GuardrailPolicyConfigError("The guardrail policy path is empty.")
        module = _guardrails_module()
        try:
            return module.load_policy(Path(value), model_resolver=_resolver(chat_models))
        except module.PolicyError as exc:
            raise GuardrailPolicyConfigError(
                f"Guardrail policy {str(value)!r} is not valid: {exc}. The service does not "
                "start with a broken policy."
            ) from None
    module = _guardrails_module()
    if not isinstance(value, module.GuardrailPolicy):
        raise GuardrailPolicyConfigError(
            "guardrail_policy must be a nexura_guardrails.GuardrailPolicy or the path of a "
            f"policy file, not {type(value).__name__}."
        )
    return value


def check_guardrail_lock(policy: Any) -> None:
    """Refuse early when another policy is already locked in as the process default."""
    from nexura_agents.policy import default_hooks, default_hooks_locked

    if default_hooks_locked() and default_hooks() is not policy:
        raise GuardrailPolicyConfigError(
            "Another guardrail policy (or locked default hooks) is already enforced in this "
            "process; one service process enforces one operator policy."
        )


def install_guardrail_policy(policy: Any) -> None:
    """Make ``policy`` the locked process default (``set_default_hooks(..., locked=True)``)."""
    from nexura_agents.errors import SDKValidationError
    from nexura_agents.policy import set_default_hooks

    try:
        set_default_hooks(policy, locked=True)
    except SDKValidationError as exc:
        raise GuardrailPolicyConfigError(
            "Another guardrail policy is already enforced in this process; one service "
            f"process enforces one operator policy ({exc})."
        ) from None


# ------------------------------------------------------------------ presets


def _preset_info() -> list[dict[str, Any]]:
    return [
        {
            "name": "pii_block",
            "description": "Block personal data and secrets in the user's message and tool "
            "arguments (regex).",
            "stages": ["input", "tool_input"],
            "network": "none",
            "cost": "none",
            "options": ["kinds"],
        },
        {
            "name": "pii_redact",
            "description": "Redact personal data and secrets in answers (also streamed) and "
            "tool results (regex).",
            "stages": ["output", "tool_output"],
            "network": "none",
            "cost": "none",
            "options": ["kinds"],
        },
        {
            "name": "injection_fast",
            "description": "Prompt-injection pre-filter on the user's message and tool results "
            "(regex; misses paraphrases).",
            "stages": ["input", "tool_output"],
            "network": "none",
            "cost": "none",
            "options": ["threshold"],
        },
        {
            "name": "safety_local",
            "description": "Injection pre-filter, then a local safety model (Llama Guard 3 1B "
            "by default) through Ollama on this server.",
            "stages": ["input", "output", "tool_output"],
            "network": "local",
            "cost": "local",
            "options": ["model"],
            "requires": f"Ollama on the server's machine with the model pulled (ollama pull "
            f"{DEFAULT_SAFETY_MODEL}).",
            "default_model": DEFAULT_SAFETY_MODEL,
            "models": _safety_models(),
        },
        {
            "name": "judge",
            "description": "An LLM judge applies your policy text to the user's message and "
            "the answer.",
            "stages": ["input", "output"],
            "network": "model",
            "cost": "model",
            "options": ["policy", "model"],
            "requires": "A configured chat model (the agent's own, or one named here); each "
            "check is one billed model call.",
            "max_policy_chars": MAX_JUDGE_POLICY_CHARS,
            "capacity_note": "At the default context (4096 tokens) the judge checks about 1.2 "
            "to 1.9 KB of text per call; longer text is blocked (fail closed). Ollama judges "
            "can be given a larger context.",
        },
        _guardrails_ai_info(),
    ]


def _guardrails_ai_info() -> dict[str, Any]:
    available = guardrails_ai_available()
    return {
        "name": "guardrails_ai",
        "description": "Guardrails AI validators that run on this server: the user's message, "
        "the answer (also streamed) and tool results.",
        "stages": ["input", "output", "tool_output"],
        "network": "none",
        "cost": "local",
        "options": ["validators"],
        "requires": "nexura-guardrails-ai and the validator packages on the server ("
        f"{_guardrails_ai_hint()}).",
        "validators": [
            {"name": name, "description": v.description, "available": available[name]}
            for name, v in _GUARDRAILS_AI.items()
        ],
    }


def _safety_models() -> list[str]:
    try:
        from nexura_guardrails.classifiers import SAFETY_MODELS
    except ImportError:
        return [DEFAULT_SAFETY_MODEL]
    tags: list[str] = []
    for entry in SAFETY_MODELS.values():
        tag = getattr(entry, "ollama", None)
        if isinstance(tag, str) and tag and tag not in tags:
            tags.append(tag)
    return tags or [DEFAULT_SAFETY_MODEL]


def preset_catalog() -> dict[str, Any]:
    """Presets a saved agent may name, whether this server can build them, and the default
    set of a new agent."""
    try:
        import nexura_guardrails.presets  # noqa: F401
    except ImportError:
        available = False
    else:
        available = True
    return {
        "available": available,
        "install_hint": None if available else _INSTALL_HINT,
        "defaults": list(DEFAULT_GUARDRAIL_PRESETS),
        "presets": _preset_info(),
    }


@dataclass
class AgentGuardrailParts:
    """Guardrail functions a saved agent's presets add, per stage."""

    input: tuple[Any, ...] = ()
    output: tuple[Any, ...] = ()
    tool_input: tuple[Any, ...] = ()
    tool_output: tuple[Any, ...] = ()
    stream_guard: StreamGuardMode = "auto"
    names: list[str] = field(default_factory=list)


def check_agent_guardrails(
    guardrails: AgentGuardrails | None,
    chat_models: Any,
    agent_model: str,
    *,
    policy: Any = None,
    agent_name: str | None = None,
) -> list[dict[str, str]]:
    """Field errors for presets this server cannot build, and for a stream mode the
    operator's policy refuses (save-time check)."""
    errors: list[dict[str, str]] = []
    text_rules = getattr(policy, "text_rules", None)
    if (
        guardrails is not None
        and guardrails.stream_guard == "passthrough"
        and callable(text_rules)
        and text_rules(agent_name)
    ):
        errors.append(
            {
                "field": "guardrails.stream_guard",
                "message": "The server's guardrail policy checks answers of this agent, so "
                "they cannot be streamed with 'passthrough'; use auto, hold or chunked.",
            }
        )
    if guardrails is None or not guardrails.presets:
        return errors
    try:
        import nexura_guardrails.presets  # noqa: F401
    except ImportError:
        return [
            {
                "field": "guardrails",
                "message": "Guardrail presets need nexura-guardrails on the server: "
                f"{_INSTALL_HINT}.",
            }
        ]
    for index, ref in enumerate(guardrails.presets):
        where = f"guardrails.presets.{index}"
        if ref.preset == "safety_local":
            from nexura_guardrails.classifiers import safety_model_for

            model = ref.model or DEFAULT_SAFETY_MODEL
            if safety_model_for(model, api="ollama") is None:
                errors.append(
                    {
                        "field": where,
                        "message": f"'{model}' is not a known local safety model; use one of "
                        f"{', '.join(_safety_models())}.",
                    }
                )
        elif ref.preset == "guardrails_ai":
            available = guardrails_ai_available()
            missing = [n for n in ref.validators or () if not available.get(n)]
            if missing:
                errors.append(
                    {
                        "field": where,
                        "message": f"Guardrails AI validators {', '.join(missing)} are not "
                        f"installed on this server: {_guardrails_ai_hint(missing)}.",
                    }
                )
        elif ref.preset == "judge":
            name = ref.model or agent_model
            refused = getattr(chat_models, "refused", None)
            if callable(refused) and refused(name):
                # P45: an operator model this workspace may not use (model_access).
                from nexura_runtime.service.api.model_access import (
                    SERVER_MODELS_MESSAGE,
                    SERVER_MODELS_NOT_AVAILABLE,
                )

                errors.append(
                    {
                        "field": where,
                        "message": f"Judge model '{name}': {SERVER_MODELS_MESSAGE}",
                        "code": SERVER_MODELS_NOT_AVAILABLE,
                    }
                )
            elif chat_models is None or name not in chat_models:
                errors.append({"field": where, "message": f"Unknown chat model '{name}'."})
            elif chat_models.is_fake(name):
                errors.append(
                    {
                        "field": where,
                        "message": "The judge preset needs a configured chat model; the offline "
                        "fixture cannot judge text.",
                    }
                )
    return errors


def build_agent_guardrails(
    guardrails: AgentGuardrails | None, chat_models: Any, agent_model: str
) -> AgentGuardrailParts:
    """Build a saved agent's presets (raises ``AgentSpecError``)."""
    if guardrails is None:
        return AgentGuardrailParts()
    errors = check_agent_guardrails(guardrails, chat_models, agent_model)
    errors = [e for e in errors if e["field"] != "guardrails.stream_guard"]  # checked in check()
    if errors:
        refused = any(e.get("code") == "SERVER_MODELS_NOT_AVAILABLE" for e in errors)
        raise AgentSpecError(
            "Invalid guardrails: " + "; ".join(e["message"] for e in errors[:3]),
            [{k: v for k, v in e.items() if k != "code"} for e in errors],
            code="SERVER_MODELS_NOT_AVAILABLE" if refused else "GUARDRAILS_INVALID",
        )
    parts = AgentGuardrailParts(stream_guard=guardrails.stream_guard)
    if not guardrails.presets:
        return parts
    from nexura_guardrails import presets as catalog

    built = []
    for ref in guardrails.presets:
        if ref.preset == "pii_block":
            built.append(catalog.pii_block(ref.kinds))
        elif ref.preset == "pii_redact":
            built.append(catalog.pii_redact(ref.kinds))
        elif ref.preset == "injection_fast":
            built.append(catalog.injection_fast(0.5 if ref.threshold is None else ref.threshold))
        elif ref.preset == "safety_local":
            # The default local URL only: a saved spec never names a server (no SSRF).
            built.append(catalog.safety_local(ref.model or DEFAULT_SAFETY_MODEL))
        elif ref.preset == "judge":
            name = ref.model or agent_model
            try:
                model = chat_models.resolve(name)
            except Exception as exc:
                # A refused operator model or an unusable own key keeps its own code.
                code = getattr(exc, "code", None)
                raise AgentSpecError(
                    str(exc), code=code if isinstance(code, str) else "MODEL_UNAVAILABLE"
                ) from None
            built.append(catalog.judge(ref.policy, model))
        elif ref.preset == "guardrails_ai":
            built.append(_guardrails_ai_preset(ref.validators or []))
        parts.names.append(ref.preset)
    assert set(parts.names) <= set(GUARDRAIL_PRESETS)
    combined = catalog.combine(*built)
    parts.input = tuple(combined.input_guardrails)
    parts.output = tuple(combined.output_guardrails)
    parts.tool_input = tuple(combined.tool_input_guardrails)
    parts.tool_output = tuple(combined.tool_output_guardrails)
    return parts


def _guardrails_ai_preset(names: list[str]) -> Any:
    """The ``guardrails_ai`` preset: the chosen validators in one check, blocking on input,
    output and tool results. Evidence keeps validator names and statuses only (a failure
    reason can echo the secret that was found)."""
    from nexura_guardrails_ai import guardrails_ai

    validators = []
    for name in names:
        spec = _GUARDRAILS_AI[name]
        validators.append(getattr(importlib.import_module(spec.module), spec.cls)())
    preset = guardrails_ai(
        *validators,
        on_fail="block",
        tool_output=True,
        categories={_GUARDRAILS_AI[n].cls: _GUARDRAILS_AI[n].categories for n in names},
        evidence_reasons=False,
        network="none",
    )
    for guard in preset.output_guardrails:
        # Answers are checked before they are shown, as for pii_redact (P39 G6).
        guard.nexura_no_passthrough = True  # type: ignore[attr-defined]
    return preset


# ------------------------------------------------------------------ routes

router = APIRouter(prefix="/guardrails", tags=["guardrails"])


#: What a guardrail policy does not cover in this release (shown with the policy view).
POLICY_EXCEPTIONS = (
    "coordinator_refused_with_model_stage_rules",  # AgentCoordinator runs are refused
)


def policy_view(policy: Any, *, admin: bool = False) -> dict[str, Any]:
    """The ``GET /guardrails/policy`` body for ``policy`` (or None).

    Everyone sees whether a policy is configured and enforced, its ``mode`` (``enforce``, or
    ``shadow``: decisions are reported, nothing is blocked), how many rules run in shadow mode
    (``shadow_rules``), the policy file's base name, the digest and what it does not cover.
    The rules (each with its effective ``enforcement``), checks, name and the file's full path
    are shown to admins only (``admin=True``): a viewer must not learn the exact blocklist
    or the server's file layout.
    """
    if policy is None:
        return {"configured": False, "enforced": False, "policy": None, "exceptions": []}
    from nexura_agents.policy import default_hooks, default_hooks_locked

    source = getattr(policy, "source", None)
    mode = getattr(policy, "mode", "enforce")
    shadow_rules = getattr(policy, "shadow_rule_count", 0)
    summary: dict[str, Any] = {
        "digest": str(getattr(policy, "digest", "")),
        "file": Path(source).name if isinstance(source, str) and source else None,
    }
    if admin:
        describe = getattr(policy, "describe", None)
        summary = {**(describe() if callable(describe) else {"name": str(policy)}), **summary}
    return {
        "configured": True,
        "enforced": default_hooks() is policy and default_hooks_locked(),
        # shadow: every decision is audited and reported, none is applied (P40).
        "mode": mode if mode in ("enforce", "shadow") else "enforce",
        "shadow_rules": shadow_rules if isinstance(shadow_rules, int) else 0,
        "detail": "full" if admin else "summary",
        "policy": summary,
        "exceptions": list(POLICY_EXCEPTIONS),
    }


def caller_is_admin(request: Request) -> bool:
    """True for the workspace admin role (the local owner is admin)."""
    from nexura_runtime.service.api.credentials import _get_caller_context

    return _get_caller_context(request)[2] == "admin"


@router.get("/policy")
async def get_guardrail_policy(request: Request) -> dict[str, Any]:
    """The operator's guardrail policy (read-only): a summary for everyone, the rules,
    checks and file path for admins."""
    return policy_view(
        getattr(request.app.state, "guardrail_policy", None), admin=caller_is_admin(request)
    )


@router.get("/presets")
async def get_guardrail_presets(request: Request) -> dict[str, Any]:
    """Presets a saved agent may use, with what each needs and costs."""
    return preset_catalog()
