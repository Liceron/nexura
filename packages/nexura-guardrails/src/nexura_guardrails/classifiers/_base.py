# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Shared machinery for local safety-model checks served over HTTP.

A safety model (Llama Guard 3, ShieldGemma, Granite Guardian, Nemotron Safety Guard) is
asked in its own documented prompt format and answers in its own documented output
format. Each adapter builds that prompt, sends it **raw** (no chat template is applied by
the server, so the format is exactly the documented one), parses the reply strictly and
maps the model's labels to the Nexura taxonomy through ``LABEL_MAPS``.

Two HTTP APIs are supported, both with ``httpx`` (a dependency of ``nexura-agents``):

- ``api="ollama"`` (default): Ollama's native ``POST /api/generate`` with ``raw: true``,
  ``stream: false``, temperature 0, ``num_predict`` and ``num_ctx`` set;
- ``api="openai"``: the OpenAI-compatible text-completion endpoint ``POST /v1/completions``
  (vLLM, llama.cpp server and similar), with the same raw prompt.

Fail closed: a timeout, a connection error, an HTTP error, a malformed response body, a
reply that does not follow the model's output format, a reply from a different model, a
server-side truncation signal, or a budget refusal all give ``Verdict(decision="error")``
(softened only by an explicit ``on_error``). A prompt whose strict token upper bound (its
UTF-8 length, see ``token_upper_bound``) exceeds ``context_tokens - max_output_tokens - 256``
is never sent: the verdict is a *hard* ``ContentTooLong`` error that no ``on_error``
softens. Ollama would otherwise silently drop the middle of an over-long prompt, so the
guard would judge padding instead of the request; ``"truncate": false`` and
``"shift": false`` are also sent, as defence in depth. When the model
clearly answers *unsafe* but its category labels are missing or not in its taxonomy, the
verdict is ``block`` with category ``other`` and ``evidence["label_error"]``, never an
error, so no ``on_error`` setting can turn an unsafe answer into ``allow``.

Each HTTP call goes through ``nexura_agents.usage.admission``, so inside a run the run's
budget admits and settles it like any model call, and the token counts the server reports
are returned on ``Verdict.usage`` (``as_guardrail`` adds them to the run's usage). Budgets
see the call as ``guard-local:<api>:<model>`` (a local server reached with the default
transport) or ``guard:<api>:<model>``, never as the agent-model id ``ollama:<model>``, so a
price set for one never applies to the other. With ``zero_price=True`` (set by the presets
for the guard models they name, on a local server only) the check registers a zero price
for its ``guard-local:`` id in each active budget's pricing table, never replacing an
existing price.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import math
import re
import time
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, ClassVar, Literal

import httpx
from nexura_agents.limits import ModelSettings
from nexura_agents.run_context import RunContext
from nexura_agents.types import Message, ModelResponse, Usage
from nexura_agents.usage import admission
from nexura_agents.usage.models import ModelPricing

from nexura_guardrails.compose import VerdictCache
from nexura_guardrails.judge import TEMPLATE_RESERVE_TOKENS, token_upper_bound
from nexura_guardrails.verdict import LABEL_MAPS, STAGES, CheckInput, Stage, Verdict

__all__ = [
    "DEFAULT_OLLAMA_URL",
    "DEFAULT_OPENAI_URL",
    "MAX_RESPONSE_BYTES",
    "Completion",
    "GuardCall",
    "MalformedOutputError",
    "Outcome",
    "SafetyModelCheck",
    "defuse",
    "first_token_probability",
    "is_local_url",
    "parse_ollama",
    "parse_openai",
    "subject_of",
]

Api = Literal["ollama", "openai"]
OnError = Literal["block", "allow", "review"]
Subject = Literal["user", "agent"]

DEFAULT_OLLAMA_URL = "http://localhost:11434"
"""Ollama's default address."""
DEFAULT_OPENAI_URL = "http://localhost:8000/v1"
"""vLLM's default OpenAI-compatible address."""
MAX_RESPONSE_BYTES = 256 * 1024
"""A reply body larger than this is refused (``ResponseTooLarge``) without being parsed."""
TOP_LOGPROBS = 5
_DEFAULT_STAGES = frozenset({"input", "output", "tool_input", "tool_output"})
_AGENT_STAGES = frozenset({"output", "tool_input", "model_output", "stream"})


class MalformedOutputError(ValueError):
    """The model's reply does not follow its documented output format."""


class _CallError(Exception):
    """A transport or server failure, reported as an ``error`` verdict."""

    def __init__(self, error_type: str, detail: str) -> None:
        super().__init__(f"{error_type}: {detail}")
        self.error_type = error_type
        self.detail = detail


@dataclass(frozen=True)
class GuardCall:
    """One raw prompt for the model. ``label`` names the harm or risk it asks about."""

    prompt: str
    label: str | None = None


@dataclass(frozen=True)
class Completion:
    """What the server returned for one call.

    ``top_logprobs`` holds, for each generated token position, the most likely tokens
    and their log probabilities when the server reported them (else None); ``tokens``
    holds the generated token at each of those positions when reported. ``model`` is the
    model name the server says answered (None when it does not say).
    ``usage`` has ``None`` counts (and ``is_complete=False``) when the server did not
    report them; a run budget then settles the call at its conservative reservation.
    """

    text: str
    finish_reason: str | None
    usage: Usage
    top_logprobs: tuple[Mapping[str, float], ...] | None = None
    tokens: tuple[str | None, ...] | None = None
    model: str | None = None


@dataclass(frozen=True)
class Outcome:
    """What an adapter decided from the completions (before it becomes a ``Verdict``)."""

    decision: Literal["allow", "block"]
    categories: tuple[str, ...] = ()
    score: float | None = None
    reason: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)


def subject_of(item: CheckInput) -> Subject:
    """Who wrote the checked text, in safety-model terms.

    The agent wrote ``output``, ``tool_input`` (its tool arguments), ``model_output`` and
    ``stream`` text; everything else (the user's input, tool results, documents) is
    treated as the user side of the conversation.
    """
    return "agent" if item.stage in _AGENT_STAGES else "user"


def is_local_url(url: str) -> bool:
    """True when ``url`` is an ``http(s)`` URL whose host is ``localhost`` or a loopback IP.

    Anything else (a LAN address, a public host, ``0.0.0.0``, a name that only starts with
    ``localhost``) is not local.
    """
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError, ValueError):
        return False
    if parsed.scheme not in ("http", "https") or not parsed.host:
        return False
    host = parsed.host.lower().rstrip(".")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# Special-token look-alikes (tokenizers parse these as control tokens, even in raw prompts),
# model control tags and the structural markers of the supported prompt formats. Matched
# after NFKC normalisation, so fullwidth forms count too. Inside a tag, spaces and
# underscores are interchangeable and the closing ">" is optional.
_SPECIAL = re.compile(r"<\|[^<>|\n]{0,64}\|>")
_GAP = r"[\s_]+"
_TAGS = re.compile(
    r"<\s*/?\s*(?:start_of_turn|end_of_turn|start_of_role|end_of_role|end_of_text|bos|eos|pad"
    r"|guardian|think|no-think|score|start_of_risk_definition|end_of_risk_definition"
    rf"|begin{_GAP}unsafe{_GAP}content{_GAP}categories"
    rf"|end{_GAP}unsafe{_GAP}content{_GAP}categories"
    rf"|begin{_GAP}conversation|end{_GAP}conversation)\b\s*>?",
    re.IGNORECASE,
)
# Indentation may use any space-like or invisible character; NFKC already maps most
# Unicode spaces to " " and the fullwidth colon to ":".
_INDENT = r"[ \t\u00a0\u1680\u2000-\u200f\u2028-\u202f\u205f\u2060\u3000\ufeff]*"
# Lines that would read as a new turn, a new section or the answer slot of a prompt format.
_ROLE_LINE = re.compile(
    rf"^({_INDENT})(user message|assistant message|human question|chatbot response|user|agent"
    r"|assistant|response|context|output json|#{1,6}[ \t]*criteria|#{1,6}[ \t]*scoring schema)"
    rf"({_INDENT})[:\ufe13\ufe55\uff1a]",
    re.IGNORECASE | re.MULTILINE,
)
# Separator lines: three or more "=", or dash and box-drawing look-alikes.
_SEPARATOR = re.compile(
    rf"^({_INDENT})[=\-\u2010-\u2015\u2212\u2500-\u2501\u2550\uff1d\uff0d]{{3,}}{_INDENT}$",
    re.MULTILINE,
)
_LINE_BREAKS = re.compile(r"\r\n|[\r  \x85\x0b\x0c]")


def defuse(text: str) -> tuple[str, int]:
    """Neutralise prompt-format structure inside checked text; return it and a change count.

    The text is NFKC-normalised (fullwidth forms and Unicode spaces become ASCII) and line
    breaks become ``\\n``. Special-token look-alikes (``<|eot_id|>``), control tags and
    markers (``<start_of_turn>``, ``<score>``, ``<END CONVERSATION>``, ``<END_CONVERSATION``)
    and separator lines (``===``, ``---``, ``———``) are replaced by a bracketed notice; a
    line starting with a role or section label (``User:``, ``Agent:``, ``response:``,
    ``Human Question:``, ``### Criteria:``), whatever its indentation, gets `` (quoted)``
    before its colon. So the checked text cannot end
    its turn, open a new one or fill the answer slot. It does not make a guard model
    immune to persuasive text inside the content.
    """
    count = 0

    def bump(replacement: str) -> Any:
        def sub(match: re.Match[str]) -> str:
            nonlocal count
            count += 1
            return match.expand(replacement)

        return sub

    out = _LINE_BREAKS.sub("\n", unicodedata.normalize("NFKC", text))
    out = _SPECIAL.sub(bump("[token removed]"), out)
    out = _TAGS.sub(bump("[tag removed]"), out)
    out = _SEPARATOR.sub(bump(r"\1[separator removed]"), out)
    out = _ROLE_LINE.sub(bump(r"\1\2 (quoted)\3:"), out)
    return out, count


def _usage(prompt_tokens: Any, completion_tokens: Any) -> Usage:
    def count(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return int(value)

    prompt, completion = count(prompt_tokens), count(completion_tokens)
    if prompt is None or completion is None:
        return Usage(prompt_tokens=prompt, completion_tokens=completion, is_complete=False)
    return Usage(
        prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion
    )


def _sum_usage(usages: Sequence[Usage]) -> Usage | None:
    total: Usage | None = None
    for usage in usages:
        total = usage if total is None else total.add(usage)
    return total


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


Positions = tuple[Mapping[str, float], ...]
Chosen = tuple[str | None, ...]


def _ollama_logprobs(raw: Any) -> tuple[Positions | None, Chosen | None]:
    if not isinstance(raw, list) or not raw:
        return None, None
    positions: list[Mapping[str, float]] = []
    chosen: list[str | None] = []
    for entry in raw:
        if not isinstance(entry, dict):
            return None, None
        table: dict[str, float] = {}
        token, value = entry.get("token"), _finite(entry.get("logprob"))
        chosen.append(token if isinstance(token, str) else None)
        if isinstance(token, str) and value is not None:
            table[token] = value
        for alt in entry.get("top_logprobs") or ():
            if isinstance(alt, dict):
                alt_token, alt_value = alt.get("token"), _finite(alt.get("logprob"))
                if isinstance(alt_token, str) and alt_value is not None:
                    table[alt_token] = max(alt_value, table.get(alt_token, -math.inf))
        positions.append(table)
    return tuple(positions), tuple(chosen)


def _openai_logprobs(raw: Any) -> tuple[Positions | None, Chosen | None]:
    if not isinstance(raw, dict):
        return None, None
    tops = raw.get("top_logprobs")
    if not isinstance(tops, list) or not tops:
        return None, None
    positions: list[Mapping[str, float]] = []
    for entry in tops:
        if not isinstance(entry, dict):
            return None, None
        table = {k: v for k, v in ((k, _finite(v)) for k, v in entry.items()) if isinstance(k, str)}
        positions.append({k: v for k, v in table.items() if v is not None})
    tokens = raw.get("tokens")
    chosen: Chosen | None = None
    if isinstance(tokens, list) and len(tokens) == len(positions):
        chosen = tuple(t if isinstance(t, str) else None for t in tokens)
    return tuple(positions), chosen


def _reported_model(data: dict[str, Any]) -> str | None:
    model = data.get("model")
    return model if isinstance(model, str) and model else None


def _truncated(*parts: Any) -> bool:
    """True when a body (or choice) carries a truncation flag (llama.cpp, some gateways)."""
    return any(
        isinstance(part, dict)
        and any(part.get(key) is True for key in ("truncated", "prompt_truncated"))
        for part in parts
    )


def parse_ollama(data: Any) -> Completion:
    """Read an Ollama ``/api/generate`` (``stream: false``) response body."""
    if not isinstance(data, dict):
        raise _CallError("MalformedResponse", "response body is not a JSON object")
    if "error" in data:
        raise _CallError("ServerError", "the server returned an error")
    text = data.get("response")
    if not isinstance(text, str):
        raise _CallError("MalformedResponse", "no 'response' text")
    if data.get("done") is not True:
        raise _CallError("MalformedResponse", "response is not complete ('done' is not true)")
    if _truncated(data):
        raise _CallError("PromptTruncated", "the server truncated the prompt")
    reason = data.get("done_reason")
    positions, chosen = _ollama_logprobs(data.get("logprobs"))
    return Completion(
        text=text,
        finish_reason=reason if isinstance(reason, str) else None,
        usage=_usage(data.get("prompt_eval_count"), data.get("eval_count")),
        top_logprobs=positions,
        tokens=chosen,
        model=_reported_model(data),
    )


def parse_openai(data: Any) -> Completion:
    """Read an OpenAI-compatible ``/v1/completions`` response body."""
    if not isinstance(data, dict):
        raise _CallError("MalformedResponse", "response body is not a JSON object")
    if "error" in data or data.get("object") == "error":
        raise _CallError("ServerError", "the server returned an error")
    choices = data.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise _CallError("MalformedResponse", "expected exactly one choice")
    choice = choices[0]
    text = choice.get("text")
    if not isinstance(text, str):
        raise _CallError("MalformedResponse", "no choice text")
    if _truncated(data, choice):
        raise _CallError("PromptTruncated", "the server truncated the prompt")
    reason = choice.get("finish_reason")
    raw_usage = data.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    positions, chosen = _openai_logprobs(choice.get("logprobs"))
    return Completion(
        text=text,
        finish_reason=reason if isinstance(reason, str) else None,
        usage=_usage(usage.get("prompt_tokens"), usage.get("completion_tokens")),
        top_logprobs=positions,
        tokens=chosen,
        model=_reported_model(data),
    )


def first_token_probability(completion: Completion, positive: str, negative: str) -> float | None:
    """P(``positive``) against ``negative`` at the first non-blank generated token.

    The answer position is the first whose generated token is not blank (when the server
    reports generated tokens), else the first with a non-blank alternative. Tokens are
    compared after stripping whitespace and ignoring case; the probabilities of matching
    alternatives are summed, then normalised over the two answers (the scoring rule of the
    ShieldGemma and Granite Guardian model cards). None when the server did not report log
    probabilities or neither answer is among them. Callers must still compare the result
    with the text answer and fail closed when they disagree.
    """
    chosen = completion.tokens
    for index, table in enumerate(completion.top_logprobs or ()):
        if not table:
            continue
        token = chosen[index] if chosen is not None and index < len(chosen) else None
        if token is not None and not token.strip():
            continue  # the model generated whitespace here, not its answer
        stripped = {t.strip().lower() for t in table}
        if token is None and stripped <= {""}:
            continue  # a whitespace-only position before the answer
        pos = sum(math.exp(v) for t, v in table.items() if t.strip().lower() == positive)
        neg = sum(math.exp(v) for t, v in table.items() if t.strip().lower() == negative)
        if pos == 0.0 and neg == 0.0:
            return None
        return pos / (pos + neg)
    return None


async def post_json(
    url: str,
    payload: dict[str, Any],
    *,
    transport: httpx.AsyncBaseTransport | None,
    timeout: float,
    api_key: str | None = None,
) -> Any:
    """POST ``payload`` as JSON and return the parsed JSON reply (fail closed).

    No proxies from the environment, no redirects, no compressed bodies, at most
    ``MAX_RESPONSE_BYTES``; transport, HTTP and body problems raise ``_CallError`` with an
    error type (``TimeoutError``, ``HTTPStatusError``, ``MalformedResponse``...).
    """
    # identity: no compressed body can expand past the size cap before it is counted.
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-Encoding": "identity",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = bytearray()
    try:
        async with (
            httpx.AsyncClient(
                transport=transport,
                timeout=httpx.Timeout(timeout),
                trust_env=False,
                follow_redirects=False,
            ) as client,
            client.stream("POST", url, json=payload, headers=headers) as response,
        ):
            encoding = response.headers.get("content-encoding", "identity").strip().lower()
            if encoding not in ("", "identity"):
                raise _CallError("UnsupportedEncoding", "compressed response refused")
            # The encoding is identity (checked above), so these bytes are not expanded.
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    raise _CallError("ResponseTooLarge", f"more than {MAX_RESPONSE_BYTES} bytes")
            status = response.status_code
    except httpx.TimeoutException:
        raise _CallError("TimeoutError", f"no reply within {timeout}s") from None
    except httpx.HTTPError as exc:
        raise _CallError(type(exc).__name__, "could not reach the server") from None
    if status != 200:
        hint = " (is the model pulled?)" if status == 404 else ""
        raise _CallError("HTTPStatusError", f"HTTP {status}{hint}")
    try:
        return json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _CallError("MalformedResponse", "response body is not JSON") from None


class _EndpointCall:
    """A one-shot model object, so ``admission.generate`` admits and settles the call."""

    def __init__(self, owner: SafetyModelCheck, call: GuardCall) -> None:
        self.model_id = owner.budget_model_id
        self._owner = owner
        self._call = call
        self.completion: Completion | None = None

    async def generate(self, messages: list[Message], tools: Any = None, **_: Any) -> ModelResponse:
        completion = await self._owner._http(self._call)
        self.completion = completion
        return ModelResponse(
            message=Message(role="assistant", content=completion.text),
            usage=completion.usage,
            finish_reason=completion.finish_reason,
        )


def _first_error(group: BaseExceptionGroup[BaseException]) -> BaseException:
    leaves: list[BaseException] = []

    def walk(exc: BaseException) -> None:
        if isinstance(exc, BaseExceptionGroup):
            for inner in exc.exceptions:
                walk(inner)
        else:
            leaves.append(exc)

    walk(group)
    for exc in leaves:
        if not isinstance(exc, asyncio.CancelledError):
            return exc
    return leaves[0] if leaves else group


class SafetyModelCheck:
    """Base class of the safety-model adapters; a non-deterministic ``Check``.

    Subclasses set the class attributes and implement ``calls`` (the raw prompts for one
    checked text) and ``decide`` (the verdict from the completions, raising
    ``MalformedOutputError`` when a reply does not follow the documented format).

    Common options:

    - ``model``: the model name on the server (an Ollama tag such as ``"llama-guard3:1b"``,
      or the served model name of an OpenAI-compatible server). A leading ``"ollama:"`` is
      accepted and removed.
    - ``base_url``: the server (default ``http://localhost:11434`` for ``api="ollama"``,
      ``http://localhost:8000/v1`` for ``api="openai"``). Environment proxies are ignored
      and redirects are not followed, so text goes only to this address.
    - ``api_key``: sent as ``Authorization: Bearer ...`` when set; never logged.
    - ``timeout`` (seconds, default 10) bounds the whole check, all calls included.
    - ``max_output_tokens``: the reply cap (``num_predict`` / ``max_tokens``).
    - ``context_tokens``: the context window the server uses; sent as ``num_ctx`` to
      Ollama (default per family: 16384 for Llama Guard 3, Granite Guardian 4.1 and
      Nemotron, 8192 for ShieldGemma and Granite Guardian 3.0). A prompt whose UTF-8
      length (a strict upper bound on its tokens) is over
      ``context_tokens - max_output_tokens - 256`` is refused with a hard
      ``ContentTooLong`` error. With ``api="openai"`` the server's own context setting
      applies: set ``context_tokens`` to it, never higher.
    - ``on_error``: ``"block"`` (default, the verdict is ``error``), ``"allow"`` or
      ``"review"``. Hard errors are never softened.
    - ``cache``, ``cache_size``, ``cache_ttl``: as for ``JudgeCheck``.
    - ``logprobs``: ask the server for log probabilities, used for a probability score
      where the model card defines one (default per adapter).
    - ``keep_alive``: Ollama's ``keep_alive`` (for example ``"10m"``); not sent when None.
    - ``transport``: an ``httpx.AsyncBaseTransport`` (tests pass ``httpx.MockTransport``).
    - ``zero_price``: register a zero price for this check's ``guard-local:`` budget id in
      the active budgets (presets set it for the guard models they name). Only allowed for
      Ollama's native API on a local server reached with the default transport; never for
      an OpenAI-compatible URL, even on localhost (it may proxy a paid provider).
    """

    deterministic = False

    source: ClassVar[str] = ""
    """The ``LABEL_MAPS`` key of this model family."""
    family: ClassVar[str] = ""
    prompt_version: str = ""
    """Names the pinned prompt format; part of the cache key."""
    default_model: ClassVar[str | None] = None
    default_name: ClassVar[str] = ""
    default_max_output_tokens: ClassVar[int] = 32
    default_logprobs: ClassVar[bool] = False
    default_context_tokens: int = 8192

    def __init__(
        self,
        model: str | None = None,
        *,
        base_url: str | None = None,
        api: Api = "ollama",
        api_key: str | None = None,
        name: str | None = None,
        stages: Iterable[str] = _DEFAULT_STAGES,
        timeout: float = 10.0,
        max_output_tokens: int | None = None,
        context_tokens: int | None = None,
        on_error: OnError = "block",
        cache: bool | VerdictCache = True,
        cache_size: int = 256,
        cache_ttl: float | None = None,
        logprobs: bool | None = None,
        keep_alive: str | int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        zero_price: bool = False,
    ) -> None:
        if api not in ("ollama", "openai"):
            raise ValueError("api must be 'ollama' or 'openai'.")
        self.api: Api = api
        if model is None:
            model = self.default_model
        if not isinstance(model, str) or not model.strip():
            raise ValueError(f"{type(self).__name__} needs a model name.")
        model = model.strip()
        if model.startswith("ollama:"):
            if api != "ollama":
                raise ValueError("An 'ollama:' model name needs api='ollama'.")
            model = model.removeprefix("ollama:")
        if not model:
            raise ValueError(f"{type(self).__name__} needs a model name.")
        self.model = model
        url = base_url or (DEFAULT_OLLAMA_URL if api == "ollama" else DEFAULT_OPENAI_URL)
        try:
            parsed = httpx.URL(url)
        except (httpx.InvalidURL, TypeError) as exc:
            raise ValueError(f"base_url is not a valid URL: {exc}") from None
        if parsed.scheme not in ("http", "https") or not parsed.host:
            raise ValueError("base_url must be an http:// or https:// URL with a host.")
        if parsed.query or parsed.fragment or parsed.userinfo:
            raise ValueError("base_url must not contain a query, a fragment or credentials.")
        self.base_url = str(parsed).rstrip("/")
        if api_key is not None and (not isinstance(api_key, str) or not api_key):
            raise ValueError("api_key must be a non-empty string or None.")
        self._api_key = api_key
        self.name = name or self.default_name or self.family
        self.stages = frozenset(stages)
        bad = self.stages - set(STAGES)
        if bad or not self.stages:
            raise ValueError(f"Unknown or empty stages {sorted(bad)}; choose from {list(STAGES)}.")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be a positive number of seconds.")
        self.timeout = float(timeout)
        if max_output_tokens is None:
            max_output_tokens = self.default_max_output_tokens
        if (
            isinstance(max_output_tokens, bool)
            or not isinstance(max_output_tokens, int)
            or max_output_tokens < 1
        ):
            raise ValueError("max_output_tokens must be a positive integer.")
        self.max_output_tokens = max_output_tokens
        if on_error not in ("block", "allow", "review"):
            raise ValueError("on_error must be 'block', 'allow' or 'review'.")
        self.on_error: OnError = on_error
        if context_tokens is None:
            context_tokens = self.default_context_tokens
        if isinstance(context_tokens, bool) or not isinstance(context_tokens, int):
            raise ValueError("context_tokens must be an integer.")
        self.context_tokens = context_tokens
        self.max_prompt_tokens = context_tokens - max_output_tokens - TEMPLATE_RESERVE_TOKENS
        if isinstance(cache, VerdictCache):
            self.cache: VerdictCache | None = cache
        elif cache is True:
            self.cache = VerdictCache(cache_size, ttl=cache_ttl)
        elif cache is False:
            self.cache = None
        else:
            raise TypeError("cache must be True, False or a VerdictCache.")
        self.logprobs = self.default_logprobs if logprobs is None else bool(logprobs)
        self.keep_alive = keep_alive
        self._transport = transport
        self.local = is_local_url(self.base_url) and transport is None
        if zero_price and not (self.local and api == "ollama"):
            # A localhost OpenAI-compatible URL may be a proxy to a paid provider; only
            # Ollama's native API on this machine is known to run the model locally.
            raise ValueError(
                "zero_price is only for Ollama's native API (api='ollama') on a local server "
                "(localhost or a loopback address) reached with the default transport."
            )
        self.zero_price = bool(zero_price)
        self._validate()
        probes: tuple[Stage, ...] = ("input", "output")
        overhead = max(
            self.prompt_tokens(c.prompt)
            for stage in probes
            for c in self.calls(CheckInput(stage, "", context=""))
        )
        if overhead >= self.max_prompt_tokens:
            raise ValueError(
                f"The {self.family} prompt alone is up to {overhead} tokens, which leaves no "
                f"room for content in context_tokens={context_tokens} (minus "
                f"max_output_tokens and {TEMPLATE_RESERVE_TOKENS} reserved). Raise "
                "context_tokens."
            )
        config = json.dumps(
            [self.prompt_version, self.api, self.base_url, self.model, self.config()],
            sort_keys=True,
        )
        self.config_digest = hashlib.sha256(config.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ subclass hooks

    def _validate(self) -> None:
        """Check adapter-specific settings (called once from ``__init__``)."""
        if self.source not in LABEL_MAPS:
            raise TypeError(f"{type(self).__name__}.source must be a LABEL_MAPS key.")

    def config(self) -> dict[str, Any]:
        """Adapter settings that change verdicts (part of the cache key)."""
        return {}

    def calls(self, item: CheckInput) -> list[GuardCall]:
        """The raw prompts for ``item`` (one per harm or risk for some models)."""
        raise NotImplementedError

    def decide(
        self, item: CheckInput, calls: Sequence[GuardCall], completions: Sequence[Completion]
    ) -> Outcome:
        """The outcome from the completions; raise ``MalformedOutputError`` on a bad reply."""
        raise NotImplementedError

    # ------------------------------------------------------------------ identity

    @property
    def backend(self) -> str:
        """``"<family>:<api>:<model>"``, for example ``"llama_guard:ollama:llama-guard3:1b"``."""
        return f"{self.family}:{self.api}:{self.model}"

    @property
    def budget_model_id(self) -> str:
        """The model id budgets see: ``guard-local:<api>:<model>`` for a local server reached
        with the default transport, else ``guard:<api>:<model>`` (``<api>`` is ``ollama`` or
        ``openai-compatible``). Never an agent-model id such as ``ollama:<model>``."""
        api = "ollama" if self.api == "ollama" else "openai-compatible"
        return f"guard{'-local' if self.local else ''}:{api}:{self.model}"

    def pricing(self) -> ModelPricing | None:
        """The zero price this check registers (``zero_price=True``), else None."""
        if not self.zero_price:
            return None
        return ModelPricing(
            model_id=self.budget_model_id,
            input_cost_per_million=Decimal("0"),
            output_cost_per_million=Decimal("0"),
            source="nexura-guardrails: local safety model",
        )

    @staticmethod
    def prompt_tokens(prompt: str) -> int:
        """A strict upper bound on the prompt's tokens: its UTF-8 length plus one (BOS)."""
        return token_upper_bound(prompt) + 1

    @property
    def cache_identity(self) -> str:
        """Identity used in cache keys: family, name and a digest of every setting."""
        return f"{self.family}:{self.name}:{self.config_digest}"

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(model={self.model!r}, api={self.api!r}, "
            f"base_url={self.base_url!r}, name={self.name!r})"
        )

    # ------------------------------------------------------------------ HTTP

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        return await post_json(
            self.base_url + path,
            payload,
            transport=self._transport,
            timeout=self.timeout,
            api_key=self._api_key,
        )

    async def _http(self, call: GuardCall) -> Completion:
        if self.api == "ollama":
            payload: dict[str, Any] = {
                "model": self.model,
                "prompt": call.prompt,
                "raw": True,
                "stream": False,
                # Never truncate or shift: an over-long prompt must fail, not lose its middle.
                "truncate": False,
                "shift": False,
                "options": {
                    "temperature": 0,
                    "num_predict": self.max_output_tokens,
                    "num_ctx": self.context_tokens,
                },
            }
            if self.logprobs:
                payload["logprobs"] = True
                payload["top_logprobs"] = TOP_LOGPROBS
            if self.keep_alive is not None:
                payload["keep_alive"] = self.keep_alive
            return self._same_model(parse_ollama(await self._post("/api/generate", payload)))
        payload = {
            "model": self.model,
            "prompt": call.prompt,
            "max_tokens": self.max_output_tokens,
            "temperature": 0,
            "stream": False,
        }
        if self.logprobs:
            payload["logprobs"] = TOP_LOGPROBS
        return self._same_model(parse_openai(await self._post("/completions", payload)))

    def _same_model(self, completion: Completion) -> Completion:
        """Refuse a reply from another model than the one asked (a gateway or proxy mix-up)."""

        def base(name: str) -> str:
            return name.removesuffix(":latest")

        if completion.model is not None and base(completion.model) != base(self.model):
            raise _CallError("ModelMismatch", "the reply came from a different model")
        return completion

    def _register_price(self) -> None:
        pricing = self.pricing()
        if pricing is None:
            return
        scope = admission.effective_budget()
        for budget in getattr(scope, "budgets", None) or ([] if scope is None else [scope]):
            table = getattr(getattr(budget, "manager", None), "pricing_table", None)
            if table is not None and table.get_pricing(pricing.model_id) is None:
                table.add_model_pricing(pricing)  # never replaces an operator's price

    async def _complete(self, call: GuardCall, usages: list[Usage]) -> Completion:
        endpoint = _EndpointCall(self, call)
        self._register_price()
        await admission.generate(
            endpoint,
            [Message(role="user", content=call.prompt)],
            None,
            model_settings=ModelSettings(temperature=0.0, max_tokens=self.max_output_tokens),
        )
        completion = endpoint.completion
        if completion is None:  # pragma: no cover - generate always sets it or raises
            raise _CallError("MalformedResponse", "no completion")
        usages.append(completion.usage)
        return completion

    async def _run(self, calls: Sequence[GuardCall], usages: list[Usage]) -> list[Completion]:
        if len(calls) == 1:
            return [await self._complete(calls[0], usages)]
        results: list[Completion | None] = [None] * len(calls)

        async def one(index: int, call: GuardCall) -> None:
            results[index] = await self._complete(call, usages)

        try:
            async with asyncio.TaskGroup() as group:
                for index, call in enumerate(calls):
                    group.create_task(one(index, call))
        except BaseExceptionGroup as exc_group:
            raise _first_error(exc_group) from None
        return [r for r in results if r is not None]

    # ------------------------------------------------------------------ verdicts

    def _common(self, started: float, usage: Usage | None) -> dict[str, Any]:
        return {
            "check": self.name,
            "backend": self.backend,
            "latency_ms": (time.perf_counter() - started) * 1000,
            "usage": usage,
        }

    def _failure(
        self, error_type: str, detail: str, started: float, usage: Usage | None
    ) -> Verdict:
        evidence = {"error_type": error_type, "detail": detail, "on_error": self.on_error}
        common = self._common(started, usage)
        if self.on_error == "allow":
            return Verdict(
                decision="allow",
                reason=f"{self.family} could not decide ({error_type}); allowed by "
                "on_error='allow'",
                evidence=evidence,
                **common,
            )
        if self.on_error == "review":
            return Verdict(
                decision="review",
                reason=f"{self.family} could not decide ({error_type}); needs review",
                evidence=evidence,
                **common,
            )
        return Verdict(
            decision="error",
            reason=f"{self.family} could not decide ({error_type})",
            evidence=evidence,
            **common,
        )

    def _too_long(self, size: int, started: float) -> Verdict:
        # Never sent, never cached, and not softened by any on_error: fail closed.
        return Verdict(
            decision="error",
            reason=f"content too long for {self.family}",
            evidence={
                "error_type": "ContentTooLong",
                "hard": True,
                "detail": f"up to {size} tokens, limit {self.max_prompt_tokens}",
            },
            **self._common(started, None),
        )

    def map_labels(self, labels: Iterable[str]) -> tuple[str, ...]:
        """Nexura category ids for model labels, through ``LABEL_MAPS[self.source]``."""
        table = LABEL_MAPS[self.source]
        out: list[str] = []
        for label in labels:
            for category in table.get(label, ("other",)):
                if category not in out:
                    out.append(category)
        return tuple(out)

    async def __call__(self, ctx: RunContext[Any], item: CheckInput) -> Verdict:
        """Classify ``item`` with the safety model (served from the cache on a repeat)."""
        started = time.perf_counter()
        rough = token_upper_bound(item.text) + token_upper_bound(item.context or "")
        if rough > 8 * self.max_prompt_tokens:
            return self._too_long(rough, started)
        key = None
        if self.cache is not None:
            key = self.cache.key(
                ctx,
                self.cache_identity,
                item.stage,
                item.role,
                item.tool_name,
                item.text,
                item.context,
            )
            hit = self.cache.get(key)
            if hit is not None:
                return hit
        calls = self.calls(item)
        size = max(self.prompt_tokens(call.prompt) for call in calls)
        if size > self.max_prompt_tokens:
            return self._too_long(size, started)
        usages: list[Usage] = []
        try:
            completions = await asyncio.wait_for(self._run(calls, usages), timeout=self.timeout)
        except TimeoutError:
            return self._failure(
                "TimeoutError", f"no verdict within {self.timeout}s", started, _sum_usage(usages)
            )
        except asyncio.CancelledError:
            raise
        except _CallError as exc:
            return self._failure(exc.error_type, exc.detail, started, _sum_usage(usages))
        except Exception as exc:  # budget refusals and other model-layer errors: fail closed
            return self._failure(
                type(exc).__name__, "safety model call failed", started, _sum_usage(usages)
            )
        usage = _sum_usage(usages)
        try:
            outcome = self.decide(item, calls, completions)
        except MalformedOutputError as exc:
            return self._failure("MalformedOutput", str(exc)[:120], started, usage)
        evidence: dict[str, Any] = {
            "model": self.model,
            "api": self.api,
            "prompt": self.prompt_version,
            **outcome.evidence,
        }
        if any(not u.is_complete for u in usages):
            evidence["usage_reported"] = False
        verdict = Verdict(
            decision=outcome.decision,
            categories=outcome.categories,
            score=outcome.score,
            reason=outcome.reason,
            evidence=evidence,
            **self._common(started, usage),
        )
        if self.cache is not None and key is not None and "label_error" not in evidence:
            self.cache.put(key, verdict)
        return verdict
