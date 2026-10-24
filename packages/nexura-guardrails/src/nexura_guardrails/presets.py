# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Ready-made guardrail presets, each a one-line constructor with documented defaults.

=================  ===========================================================  ===========
Preset             Contents                                                     Network
=================  ===========================================================  ===========
``pii_block``      PII pre-filter, block: agent input and tool input            none
``pii_redact``     PII pre-filter, redact: agent output (and streamed replies)  none
                   and tool output
``injection_fast`` injection pre-filter: agent input and tool output            none
``safety_local``   tiered: injection pre-filter, then a local safety model      local
                   (Llama Guard 3 1B by default, through Ollama) on input; the
                   model on output; the pre-filter on tool output
``strict_tools``   tool allowlist, approval for every MCP and OpenAPI tool      none
                   that is not ``pure``, and the injection pre-filter on every
                   tool result
``judge``          ``JudgeCheck`` with your policy text on input and output     the model
=================  ===========================================================  ===========

A ``Preset`` holds guardrail functions per stage and, when it has tool stages, ``hooks``
that apply its tool checks to every tool call of a run (also MCP and OpenAPI tools,
sub-agents and teams when installed with ``set_default_hooks``)::

    preset = safety_local()
    agent = preset.apply(agent)                  # agent input and output guardrails ONLY
    Runner(**preset.runner_kwargs()).run(agent, ...)  # tool checks for every tool

**``apply()`` alone does not check tool calls or tool results.** The tool stages of
``pii_block``, ``pii_redact``, ``injection_fast``, ``safety_local``, ``strict_tools`` and
``judge(stages=[..., "tool_output"])`` live in ``preset.hooks``; without
``Runner(hooks=[preset.hooks])`` (``preset.runner_kwargs()``) or ``set_default_hooks`` they
do not run.

or pass ``preset.tool_kwargs()`` to ``@tool(...)`` for one tool. ``combine(a, b)`` joins
presets. The defaults need no model and no network; ``safety_local`` refuses a server that
is not on this machine unless ``allow_remote=True``, because a remote endpoint receives
every checked text and has a different trust and cost profile.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from nexura_agents.guardrails import GuardrailFn
from nexura_agents.hooks import AgentHooks, ComposedHooks
from nexura_agents.run_context import RunContext
from nexura_agents.types import ToolCall

from nexura_guardrails.checks import (
    Check,
    as_guardrail,
    as_tool_guardrail,
    rejection_message,
    run_check,
    value_text,
)
from nexura_guardrails.verdict import CheckInput, Role, Stage, Verdict

__all__ = [
    "PRESETS",
    "Preset",
    "PresetInfo",
    "StrictToolsHooks",
    "ToolCheckHooks",
    "combine",
    "injection_fast",
    "judge",
    "pii_block",
    "pii_redact",
    "safety_local",
    "strict_tools",
]

Network = Literal["none", "local", "model"]
_NETWORK_ORDER = {"none": 0, "local": 1, "model": 2}

APPROVE_EFFECTS: tuple[str, ...] = ("idempotent", "reconcilable", "non_idempotent")
"""``strict_tools`` default: every effect class except ``pure`` needs approval.
``idempotent`` means safe to *repeat* after a crash, not free of side effects."""
LOCAL_ORIGINS: tuple[str, ...] = ("python:", "builtin:", "agent:")
"""Origins whose tools a bare ``strict_tools`` name pattern may admit."""
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{0,63}")


def _word(value: str, fallback: str) -> str:
    return value if _WORD.fullmatch(value) else fallback


class ToolCheckHooks(AgentHooks):
    """Hooks that run a check on every tool call's arguments and every tool result.

    ``tool_input``: a check for the arguments (sorted JSON, like ``as_tool_guardrail``);
    anything but ``allow`` skips the tool and gives the model a fixed notice (a rewrite
    of arguments counts as a block). ``tool_output``: a check for the result text;
    ``rewrite`` replaces it (redaction), anything else but ``allow`` withholds it behind a
    fixed notice. Errors block (fail closed). The checks' model usage counts for the run.
    """

    def __init__(
        self,
        *,
        tool_input: Check | None = None,
        tool_output: Check | None = None,
    ) -> None:
        for stage, check in (("tool_input", tool_input), ("tool_output", tool_output)):
            if check is not None and stage not in getattr(check, "stages", ()):
                raise ValueError(f"Check {check.name!r} does not support stage {stage!r}.")
        self.tool_input = tool_input
        self.tool_output = tool_output

    async def _verdict(
        self, check: Check, ctx: RunContext[Any], stage: Stage, text: str, role: Role, tool: str
    ) -> Verdict:
        item = CheckInput(stage=stage, text=text, role=role, tool_name=tool)
        verdict = await run_check(check, ctx, item)
        if verdict.usage is not None and not verdict.cached:
            ctx.add_usage(verdict.usage)
        return verdict

    async def before_tool(self, ctx: RunContext[Any], call: ToolCall) -> str | None:
        """Check the call's arguments; a block skips the tool with a fixed notice."""
        if self.tool_input is None:
            return None
        verdict = await self._verdict(
            self.tool_input, ctx, "tool_input", value_text(call.arguments), "assistant", call.name
        )
        if verdict.decision == "allow":
            return None
        name = _word(verdict.check or self.tool_input.name, "guardrail")
        notice = rejection_message(verdict, review=verdict.decision == "review")
        return f"Tool call blocked by guardrail '{name}': {notice}"

    async def after_tool(self, ctx: RunContext[Any], call: ToolCall, result: str) -> str | None:
        """Check the result; redact it on ``rewrite``, withhold it on a block or error."""
        if self.tool_output is None:
            return None
        verdict = await self._verdict(
            self.tool_output, ctx, "tool_output", result, "tool", call.name
        )
        if verdict.decision == "allow":
            return None
        if verdict.decision == "rewrite" and verdict.replacement is not None:
            return verdict.replacement
        name = _word(verdict.check or self.tool_output.name, "guardrail")
        notice = rejection_message(verdict, review=verdict.decision == "review")
        return f"Tool result withheld by guardrail '{name}': {notice}"


class StrictToolsHooks(ToolCheckHooks):
    """Allowlist, approval and result scanning for every tool call (the ``strict_tools`` preset).

    - A tool that matches none of the ``allow`` patterns is not run; the model gets a fixed
      refusal. A pattern with a ``:`` (``"mcp:docs:*"``, ``"openapi:GET:/items*"``) is
      matched against the tool's origin. A bare pattern (``"lookup"``, ``"get_*"``) is
      matched against the name of *local* tools only: tools without an origin, ``@tool``
      functions (``python:``), the SDK's own tools (``builtin:``) and ``Agent.as_tool``
      sub-agents (``agent:``). So ``"lookup"`` never admits an MCP, OpenAPI, A2A or module
      tool that happens to share the name; allow those by origin.
    - An allowed tool whose origin starts with one of ``approve_origins`` (default MCP and
      OpenAPI tools) and whose effect is in ``approve_effects`` (default: every effect but
      ``pure``, so ``idempotent`` and ``reconcilable`` side effects such as a declared
      idempotent ``PUT`` or ``DELETE`` need approval too) pauses for human approval
      (``tool_needs_approval``).
    - Results go through ``tool_output`` (the injection pre-filter by default).
    """

    def __init__(
        self,
        allow: Iterable[str],
        *,
        approve_origins: Iterable[str] = ("mcp:", "openapi:"),
        approve_effects: Iterable[str] = APPROVE_EFFECTS,
        tool_output: Check | None = None,
    ) -> None:
        super().__init__(tool_output=tool_output)
        patterns = (allow,) if isinstance(allow, str) else tuple(allow)
        if not all(isinstance(p, str) and p for p in patterns):
            raise ValueError("allow must be a list of non-empty tool names or patterns.")
        self.allow = patterns
        self.approve_origins = tuple(approve_origins)
        self.approve_effects = frozenset(approve_effects)

    def allowed(self, name: str, origin: str | None) -> bool:
        """True when ``origin`` matches an origin pattern (one with ``:``), or the tool is
        local (see ``LOCAL_ORIGINS``) and ``name`` matches a bare pattern."""
        local = origin is None or origin.startswith(LOCAL_ORIGINS)
        for pattern in self.allow:
            if ":" in pattern:
                if origin is not None and fnmatch.fnmatchcase(origin, pattern):
                    return True
            elif local and fnmatch.fnmatchcase(name, pattern):
                return True
        return False

    def tool_needs_approval(self, ctx: Any, tool: Any, arguments: Mapping[str, Any]) -> bool | None:
        """Approval for allowed tools from the listed origins whose effect is listed."""
        info = tool.info()
        if not self.allowed(info.name, info.origin):
            return None  # refused in before_tool anyway
        origin = info.origin or ""
        if origin.startswith(self.approve_origins) and info.effect in self.approve_effects:
            return True
        return None

    async def before_tool(self, ctx: RunContext[Any], call: ToolCall) -> str | None:
        """Refuse tools outside the allowlist with a fixed notice."""
        info = getattr(ctx, "tool", None)
        origin = getattr(info, "origin", None)
        if not self.allowed(call.name, origin if isinstance(origin, str) else None):
            name = _word(call.name, "this tool")
            return f"Tool call refused by policy 'strict_tools': '{name}' is not on the allowlist."
        return await super().before_tool(ctx, call)


@dataclass(frozen=True)
class Preset:
    """A named set of guardrails per stage, plus hooks for every tool call.

    ``network`` is ``"none"`` (regex only), ``"local"`` (a model on this machine) or
    ``"model"`` (the chosen model, wherever it runs, including a remote safety-model server).
    ``checks`` maps each stage to the check used there (for ``evaluate`` and inspection).
    ``models`` lists the ``SAFETY_MODELS`` ids whose license applies; it is empty when the
    model tag is not one this package can identify (then check the model card yourself).

    **``apply(agent)`` adds only the agent input and output guardrails.** Tool calls and
    tool results are checked only through ``hooks``: pass ``runner_kwargs()`` to the
    ``Runner`` (or install ``hooks`` with ``set_default_hooks``).
    """

    name: str
    network: Network = "none"
    input_guardrails: tuple[GuardrailFn, ...] = ()
    output_guardrails: tuple[GuardrailFn, ...] = ()
    tool_input_guardrails: tuple[GuardrailFn, ...] = ()
    tool_output_guardrails: tuple[GuardrailFn, ...] = ()
    hooks: AgentHooks | None = None
    checks: Mapping[str, Check] = field(default_factory=dict)
    models: tuple[str, ...] = ()

    def agent_kwargs(self) -> dict[str, list[GuardrailFn]]:
        """``{"input_guardrails": [...], "output_guardrails": [...]}`` for ``Agent(...)``."""
        return {
            "input_guardrails": list(self.input_guardrails),
            "output_guardrails": list(self.output_guardrails),
        }

    def tool_kwargs(self) -> dict[str, list[GuardrailFn]]:
        """``{"input_guardrails": [...], "output_guardrails": [...]}`` for ``@tool(...)``."""
        return {
            "input_guardrails": list(self.tool_input_guardrails),
            "output_guardrails": list(self.tool_output_guardrails),
        }

    def runner_kwargs(self) -> dict[str, list[AgentHooks]]:
        """``{"hooks": [self.hooks]}`` (or ``{}``) for ``Runner(...)``: the tool stages."""
        return {} if self.hooks is None else {"hooks": [self.hooks]}

    def pricing(self) -> list[Any]:
        """The zero ``ModelPricing`` entries this preset's local guard models register in
        the active budget when they are called (see ``SafetyModelCheck.zero_price``)."""
        out: list[Any] = []
        for check in self.checks.values():
            for member in (check, *getattr(check, "checks", ())):
                price = getattr(member, "pricing", None)
                entry = price() if callable(price) else None
                if entry is not None and all(e.model_id != entry.model_id for e in out):
                    out.append(entry)
        return out

    def apply(self, agent: Any) -> Any:
        """A clone of ``agent`` with this preset's input and output guardrails added after
        its own. **Tool stages are not changed**: pass ``runner_kwargs()`` (the ``hooks``)
        to the ``Runner``, or tool calls and results go unchecked."""
        return agent.clone(
            input_guardrails=[*agent.input_guardrails, *self.input_guardrails],
            output_guardrails=[*agent.output_guardrails, *self.output_guardrails],
        )

    def licenses(self) -> list[Any]:
        """The ``SafetyModel`` entries (source and license) of the models this preset uses."""
        if not self.models:
            return []
        from nexura_guardrails.classifiers import SAFETY_MODELS

        return [SAFETY_MODELS[m] for m in self.models if m in SAFETY_MODELS]


def combine(*presets: Preset, name: str | None = None) -> Preset:
    """Join presets: guardrail lists are concatenated in order and hooks are composed."""
    if not presets:
        raise ValueError("combine() needs at least one preset.")
    hooks = [p.hooks for p in presets if p.hooks is not None]
    checks: dict[str, Check] = {}
    for preset in presets:
        for stage, check in preset.checks.items():
            checks.setdefault(stage, check)
    network = max((p.network for p in presets), key=_NETWORK_ORDER.__getitem__)
    return Preset(
        name=name or "+".join(p.name for p in presets),
        network=network,
        input_guardrails=tuple(g for p in presets for g in p.input_guardrails),
        output_guardrails=tuple(g for p in presets for g in p.output_guardrails),
        tool_input_guardrails=tuple(g for p in presets for g in p.tool_input_guardrails),
        tool_output_guardrails=tuple(g for p in presets for g in p.tool_output_guardrails),
        hooks=None if not hooks else hooks[0] if len(hooks) == 1 else ComposedHooks(*hooks),
        checks=MappingProxyType(checks),
        models=tuple(dict.fromkeys(m for p in presets for m in p.models)),
    )


def _build(
    name: str,
    network: Network,
    *,
    input: Check | None = None,
    output: Check | None = None,
    tool_input: Check | None = None,
    tool_output: Check | None = None,
    models: tuple[str, ...] = (),
) -> Preset:
    checks = {
        stage: check
        for stage, check in (
            ("input", input),
            ("output", output),
            ("tool_input", tool_input),
            ("tool_output", tool_output),
        )
        if check is not None
    }
    tool_hooks = (
        ToolCheckHooks(tool_input=tool_input, tool_output=tool_output)
        if tool_input is not None or tool_output is not None
        else None
    )
    return Preset(
        name=name,
        network=network,
        input_guardrails=() if input is None else (as_guardrail(input, name=name),),
        output_guardrails=()
        if output is None
        else (as_guardrail(output, stage="output", name=name),),
        tool_input_guardrails=()
        if tool_input is None
        else (as_tool_guardrail(tool_input, name=name),),
        tool_output_guardrails=()
        if tool_output is None
        else (as_tool_guardrail(tool_output, stage="tool_output", name=name),),
        hooks=tool_hooks,
        checks=MappingProxyType(checks),
        models=models,
    )


# --------------------------------------------------------------------------- presets


def pii_block(kinds: Iterable[str] | None = None) -> Preset:
    """Block personal data and secrets in agent input and tool arguments (regex, no network).

    ``kinds``: the ``detect_pii`` kinds (default all: cards, Aadhaar, PAN, emails, phones,
    keys and the rest of the built-in list).
    """
    from nexura_guardrails.prefilters import pii

    check = pii(kinds, action="block", name="pii_block")
    return _build("pii_block", "none", input=check, tool_input=check)


def pii_redact(kinds: Iterable[str] | None = None, *, mask: str = "[REDACTED:{kind}]") -> Preset:
    """Redact personal data and secrets in agent output and tool results (regex, no network).

    The output guardrail also covers streamed replies: they are held until it passed, or
    checked window by window with ``Agent(stream_guard="chunked")``.
    """
    from nexura_guardrails.prefilters import pii

    check = pii(kinds, action="redact", mask=mask, name="pii_redact")
    return _build("pii_redact", "none", output=check, tool_output=check)


def injection_fast(threshold: float = 0.5) -> Preset:
    """Prompt-injection pre-filter on agent input and tool results (regex, no network).

    ``threshold`` is the summed signal score at which it blocks (default 0.5).
    """
    from nexura_guardrails.prefilters import injection

    check = injection(threshold=threshold, name="injection_fast")
    return _build("injection_fast", "none", input=check, tool_output=check)


_GUARDS = {
    "llama_guard": "LlamaGuard",
    "shieldgemma": "ShieldGemma",
    "granite_guardian": "GraniteGuardian",
    "nemotron": "NemotronGuard",
}


def _guess_guard(model: str) -> str:
    name = model.lower()
    for marker, guard in (
        ("llama-guard", "llama_guard"),
        ("llama_guard", "llama_guard"),
        ("shieldgemma", "shieldgemma"),
        ("granite", "granite_guardian"),
        ("nemotron", "nemotron"),
    ):
        if marker in name:
            return guard
    raise ValueError(
        f"Cannot tell which safety model {model!r} is; pass guard='llama_guard', "
        "'shieldgemma', 'granite_guardian' or 'nemotron'."
    )


def safety_local(
    model: str = "llama-guard3:1b",
    *,
    guard: Literal["llama_guard", "shieldgemma", "granite_guardian", "nemotron"] | None = None,
    base_url: str | None = None,
    api: Literal["ollama", "openai"] = "ollama",
    allow_remote: bool = False,
    escalate: str = "always",
    injection_threshold: float = 0.5,
    **guard_options: Any,
) -> Preset:
    """Tiered safety on a local model: the injection pre-filter, then a safety model.

    Defaults: Llama Guard 3 1B served by Ollama at ``http://localhost:11434``
    (``ollama pull llama-guard3:1b``), ``escalate="always"`` (the model sees every input
    the pre-filter let through). Input: ``tiered(injection, model)``; output: the model
    (streamed replies are held, or checked per window in chunked mode); tool results: the
    injection pre-filter. ``"granite3-guardian:2b"`` (Apache-2.0) is the license-clean
    alternative; ``guard`` is inferred from the model name. ``guard_options`` go to the
    adapter (``timeout``, ``context_tokens``, ``on_error``, ``risks``, ``harms``...).

    The server must be on this machine (``localhost`` or a loopback address): a remote
    endpoint receives every checked text and is a different trust and cost profile, so it
    raises ``ValueError`` unless ``allow_remote=True`` (the preset's ``network`` is then
    ``"model"``). For the exact guard models it names (a ``SAFETY_MODELS`` Ollama tag or
    Hugging Face repo id) served by Ollama's native API (``api="ollama"``, never an
    OpenAI-compatible URL, which may proxy a paid provider) on a local server with the
    default transport, the guard registers
    a zero price for its ``guard-local:`` budget id (``zero_price``), so the preset works
    under a cost cap; pass ``zero_price=False`` to opt out. Only ``preset.hooks`` checks
    tool results: use ``Runner(**preset.runner_kwargs())``.
    """
    from nexura_guardrails import classifiers
    from nexura_guardrails.classifiers import (
        DEFAULT_OLLAMA_URL,
        DEFAULT_OPENAI_URL,
        is_local_url,
        safety_model_for,
    )
    from nexura_guardrails.compose import tiered
    from nexura_guardrails.prefilters import injection

    url = base_url or (DEFAULT_OLLAMA_URL if api == "ollama" else DEFAULT_OPENAI_URL)
    if not allow_remote and not is_local_url(url):
        raise ValueError(
            f"safety_local only talks to a server on this machine; {url!r} is not localhost "
            "or a loopback address. A remote endpoint receives every checked text and has its "
            "own trust and cost profile: pass allow_remote=True to use it deliberately."
        )
    kind = guard or _guess_guard(model)
    if kind not in _GUARDS:
        raise ValueError(f"guard must be one of {sorted(_GUARDS)}.")
    adapter: Callable[..., Check] = getattr(classifiers, _GUARDS[kind])
    guard_options.setdefault("name", "safety_model")
    local = is_local_url(url)
    known = safety_model_for(model, api=api)
    # A zero price only for a guard model this preset names exactly, on this machine, over
    # the default transport (a custom transport could route anywhere).
    exact = (
        known is not None
        and known.adapter == _GUARDS[kind]
        and model.removeprefix("ollama:") in known.served_names()
    )
    if "zero_price" not in guard_options:
        # Ollama's native API only: a localhost OpenAI-compatible URL may be a proxy to a
        # paid provider, so it never gets a zero price.
        guard_options["zero_price"] = (
            exact and local and api == "ollama" and guard_options.get("transport") is None
        )
    safety = adapter(model, base_url=url, api=api, **guard_options)
    prefilter = injection(threshold=injection_threshold, name="injection_fast")
    first = tiered(prefilter, safety, escalate=escalate, name="safety_local")
    return _build(
        "safety_local",
        "local" if local else "model",
        input=first,
        output=safety,
        tool_output=prefilter,
        models=() if known is None else (known.id,),
    )


def strict_tools(
    allow: Iterable[str],
    *,
    approve_origins: Iterable[str] = ("mcp:", "openapi:"),
    approve_effects: Iterable[str] = APPROVE_EFFECTS,
    scan_results: bool = True,
    result_check: Check | None = None,
) -> Preset:
    """Allowlist mode for tools, approval for MCP and OpenAPI tools that are not ``pure``,
    and result scanning.

    ``allow`` (required; an empty list refuses every tool): ``fnmatch`` patterns. One with a
    ``:`` matches the tool's origin (``"mcp:docs:*"``); a bare one matches the names of
    local tools only (no origin, ``python:``, ``builtin:`` or ``agent:``), never an MCP,
    OpenAPI, A2A or module tool with the same name. Allowed tools with an origin starting
    with ``approve_origins`` and an effect in ``approve_effects`` (default: ``idempotent``,
    ``reconcilable`` and ``non_idempotent``, that is everything but ``pure``) pause for
    human approval.
    Results go through ``result_check`` (default: the injection pre-filter; pass for
    example ``tiered(injection(), LlamaGuard())`` to add a model) unless
    ``scan_results=False``. Everything is in ``preset.hooks``; install them with
    ``Runner(hooks=[preset.hooks])`` or ``set_default_hooks``.
    """
    from nexura_guardrails.prefilters import injection

    check: Check | None = None
    if scan_results:
        check = result_check or injection(name="strict_tools")
    hooks = StrictToolsHooks(
        allow,
        approve_origins=approve_origins,
        approve_effects=approve_effects,
        tool_output=check,
    )
    network: Network = "none" if result_check is None or result_check.deterministic else "model"
    return Preset(
        name="strict_tools",
        network=network,
        tool_output_guardrails=()
        if check is None
        else (as_tool_guardrail(check, stage="tool_output", name="strict_tools"),),
        hooks=hooks,
        checks=MappingProxyType({} if check is None else {"tool_output": check}),
    )


def judge(
    policy: str | None = None,
    model: Any = None,
    *,
    stages: Iterable[Literal["input", "output", "tool_output"]] = ("input", "output"),
    **judge_options: Any,
) -> Preset:
    """``JudgeCheck`` with your policy text on agent input and output (default).

    ``model``: a model string (``"ollama:qwen3:8b"``), a model object, or an ``Agent``
    (its own model is used). ``stages`` may add ``"tool_output"`` (applied through
    ``hooks``). ``judge_options`` go to ``JudgeCheck`` (``categories``, ``policy_file``,
    ``timeout``, ``on_error``, ``context_tokens``...). Each checked stage costs one model
    call, admitted against the run's budget.

    **Capacity warning.** With the default ``context_tokens=4096`` the judge accepts only
    about 1.2 to 1.9 KB of checked text (the content plus, for an answer, the user's
    request): the prompt is measured by a strict byte bound and the policy and instructions
    take about 1.7 to 2.4 KB. Anything longer is a hard ``ContentTooLong`` error, so the
    guardrail blocks it. To check longer text, raise the judge server's context and pass the
    same number, for example ``OLLAMA_CONTEXT_LENGTH=16384`` and
    ``judge(..., context_tokens=16384)``; never set ``context_tokens`` above the server's
    real context. (Ollama judges go through its OpenAI-compatible endpoint, which takes no
    per-request context size; moving them to Ollama's native API is planned for G6.)
    """
    from nexura_guardrails.judge import JudgeCheck

    if model is None:
        raise ValueError("judge() needs a model: a model string, a model object or an Agent.")
    from nexura_agents import Agent

    if isinstance(model, Agent):
        model = model.model  # judge with the agent's own model
    chosen = set(stages)
    bad = chosen - {"input", "output", "tool_output"}
    if bad or not chosen:
        raise ValueError("stages must be a non-empty subset of input, output and tool_output.")
    judge_options.setdefault("name", "judge")
    check = JudgeCheck(model, policy, **judge_options)
    return _build(
        "judge",
        "model",
        input=check if "input" in chosen else None,
        output=check if "output" in chosen else None,
        tool_output=check if "tool_output" in chosen else None,
    )


@dataclass(frozen=True)
class PresetInfo:
    """What a preset is, for listings (Studio, the service, docs)."""

    name: str
    network: Network
    description: str
    build: Callable[..., Preset]


PRESETS: Mapping[str, PresetInfo] = MappingProxyType(
    {
        info.name: info
        for info in (
            PresetInfo("pii_block", "none", "PII block on input and tool input", pii_block),
            PresetInfo(
                "pii_redact", "none", "PII redaction on output, streams and tool output", pii_redact
            ),
            PresetInfo(
                "injection_fast",
                "none",
                "Injection pre-filter on input and tool output",
                injection_fast,
            ),
            PresetInfo(
                "safety_local",
                "local",
                "Injection pre-filter, then a local safety model via Ollama",
                safety_local,
            ),
            PresetInfo(
                "strict_tools",
                "none",
                "Tool allowlist, approval for side-effecting MCP/OpenAPI tools, result scan",
                strict_tools,
            ),
            PresetInfo("judge", "model", "LLM judge with your policy text", judge),
        )
    }
)
"""Every preset by name."""
