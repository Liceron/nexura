# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Execution runner for Nexura agents.

Coordinates the model-tool execution loop, enforces turns, tool call, time and usage
limits, exposes ordered per-run lifecycle events, and handles synchronous and
asynchronous invocation with safety checks.

Beyond the basic loop the runner supports typed dependencies (``deps``), dynamic
instructions, validated structured output (``Agent(output_type=...)``) with retries,
tool retries via ``ModelRetry``, parallel tool calls, input/output guardrails,
lifecycle hooks, usage limits, per-tool human approval with pause/resume, model text
streaming, and budget/cancellation context for provider models.

Cancellation & Sync Functions:
Cancelling an asynchronous task while a synchronous tool function is executing
in a worker thread cannot forcibly terminate that operating system thread or undo
any side-effects already initiated by that tool. The SDK does not provide
exactly-once guarantees and never automatically retries tools with potential
side-effects; a tool is only re-invoked when it raised ``ModelRetry`` itself.

Concurrency & Persistence:
When session persistence is used, optimistic concurrency control is enforced via
compare-and-swap (CAS) on expected revisions. In concurrent executions against the
same session, tools may execute prior to a CAS conflict occurring; no exactly-once
guarantees are provided.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import inspect
import json
import math
import re
import time
import uuid
import warnings
from collections.abc import AsyncGenerator, Awaitable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from nexura_agents import tracing
from nexura_agents._lazy import LazyModule
from nexura_agents.agent import Agent
from nexura_agents.attachments import Attachment
from nexura_agents.cancel import CancelToken
from nexura_agents.errors import (
    ApprovalDecisionError,
    EventLoopError,
    GuardrailTripwireTriggered,
    MaxHandoffsExceededError,
    MaxToolCallsExceededError,
    MaxTurnsExceededError,
    ModelRetry,
    ModelTimeoutError,
    NexuraError,
    OutputValidationError,
    ProviderError,
    RunRecoveryError,
    SDKValidationError,
    ToolError,
    ToolExecutionError,
    ToolValidationError,
    UnknownToolError,
)
from nexura_agents.events import RunEvent, RunEventType
from nexura_agents.guardrails import check_tool_input, check_tool_output, run_guardrail
from nexura_agents.handoffs import Handoff
from nexura_agents.hooks import AgentHooks
from nexura_agents.json_schema import validate_instance
from nexura_agents.limits import ModelSettings, UsageLimits
from nexura_agents.models.resilience import _accepts_model_settings, _accepts_run_context
from nexura_agents.run_context import RunContext
from nexura_agents.session import SessionSnapshot, SessionStore, _accepts_state
from nexura_agents.tools import StopAtTools, Tool
from nexura_agents.types import (
    ApproveWith,
    Message,
    ModelResponse,
    OutputT,
    RunInterruption,
    RunResult,
    ToolCall,
    Usage,
)

if TYPE_CHECKING:
    import asyncio

    from nexura_agents.durable import RunCheckpointStore
else:
    asyncio = LazyModule("asyncio")

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
_NO_HOOKS = AgentHooks()


def _validate_timeout(timeout: float | None) -> None:
    if timeout is not None and (
        not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0
    ):
        raise SDKValidationError(f"timeout must be a positive number and finite, got {timeout!r}.")


def _failed(run_id: str, exc: BaseException) -> RunEvent:
    # Event consumers (stores, logs, UIs) get the message with credentials masked; the raised
    # exception keeps the full text for the caller.
    return RunEvent(
        type=RunEventType.RUN_FAILED,
        run_id=run_id,
        data={"error": str(tracing.redact(str(exc))), "error_type": type(exc).__name__},
    )


def _parse_output(agent: Agent, text: str) -> Any:
    """Validate the final text against ``agent.output_type``; raises ValueError."""
    assert agent.output_type is not None
    match = _FENCE.match(text)
    candidate = match.group(1) if match else text.strip()
    if isinstance(agent.output_type, dict):
        try:
            parsed = json.loads(candidate)
        except ValueError as exc:
            raise ValueError(f"the reply is not valid JSON ({exc})") from exc
        problems = validate_instance(agent.output_type, parsed)
        if problems:
            raise ValueError("\n".join(problems))
        return parsed
    try:
        return agent.output_type.model_validate_json(candidate)
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc


# A prompt: text, a list of messages, or a list mixing text and attachments
# (``["Describe this", Attachment.from_path("photo.jpg")]``).
Prompt = str | Sequence[Message] | Sequence[str | Attachment]


def _prompt_messages(prompt: Prompt) -> list[Message]:
    if isinstance(prompt, str):
        return [Message(role="user", content=prompt)]
    items = list(prompt)
    if all(isinstance(item, Message) for item in items):
        return copy.deepcopy(items)  # type: ignore[arg-type]
    if any(isinstance(item, Message) for item in items):
        raise SDKValidationError(
            "A prompt list holds either Message objects or text and attachments, not both."
        )
    texts = [item for item in items if isinstance(item, str)]
    attachments = [item for item in items if isinstance(item, Attachment)]
    if len(texts) + len(attachments) != len(items):
        bad = next(i for i in items if not isinstance(i, (str, Attachment)))
        raise SDKValidationError(f"Unsupported prompt item: {type(bad).__name__}.")
    return [
        Message(
            role="user",
            content="\n\n".join(texts) or None,
            attachments=attachments or None,
        )
    ]


def _add_memory_block(messages: list[Message], block: str) -> None:
    """Append recalled memories to the system instructions of a per-call copy."""
    for message in messages:
        if message.role == "system":
            message.content = f"{message.content}\n\n{block}" if message.content else block
            return
    messages.insert(0, Message(role="system", content=block))


# Results the runner writes for calls that did not succeed (never a final output).
_FAILED_RESULT_PREFIXES = (
    "Tool call rejected by a human reviewer: ",
    "Retry requested by tool '",
    "Invalid arguments for '",
    "Tool call blocked by guardrail '",
    "Tool result blocked by guardrail '",
    "Tool call timed out: ",
)


def _tool_final_text(agent: Agent, state: _RunState, calls: list[ToolCall]) -> str | None:
    """The result that ends the run under ``agent.tool_use_behavior``, or None to go on.

    The first call of the batch (in model order) that matches and succeeded wins; calls
    that were rejected, retried, had invalid arguments or an unknown outcome never count.
    """
    behavior = agent.tool_use_behavior
    if behavior == "run_llm_again":
        return None
    results = {m.tool_call_id: m.content for m in state.messages if m.role == "tool"}
    for tc in calls:
        if tc.name in agent.handoffs_by_name:
            continue
        if isinstance(behavior, StopAtTools) and tc.name not in behavior.tool_names:
            continue
        text = results.get(tc.id)
        if text is None or text == _UNKNOWN_RESULT or text.startswith(_FAILED_RESULT_PREFIXES):
            continue
        return text
    return None


async def _bounded(state: _RunState, awaitable: Awaitable[Any]) -> Any:
    """Await ``awaitable`` within what is left of the run's timeout (TimeoutError past it)."""
    remaining = state.remaining()
    return await (asyncio.wait_for(awaitable, timeout=remaining) if remaining else awaitable)


class _PartialOutput:
    """Best-effort parse of a JSON answer as it streams (``PARTIAL_OUTPUT`` events).

    Uses pydantic-core's partial JSON parser. A new value is reported only when it
    changes, and only once the text looks like JSON (an optional code fence is skipped).
    The values are not validated against ``output_type``; the final output still is.

    Parsing re-reads the whole text, so it runs only after the text has grown by about
    10% (and at least ``MIN_STEP`` characters) since the last parse, plus once at the end
    (``flush``). That keeps the total work linear in the answer size.
    """

    MIN_STEP = 32

    def __init__(self) -> None:
        self.text = ""
        self.last: Any = _UNSET
        self.parsed_len = 0

    def feed(self, chunk: str) -> Any:
        self.text += chunk
        due = self.parsed_len + max(self.MIN_STEP, self.parsed_len // 10)
        if len(self.text) < due:
            return None
        return self._parse()

    def flush(self) -> Any:
        """Parse whatever arrived since the last parse (end of the stream)."""
        if len(self.text) == self.parsed_len:
            return None
        return self._parse()

    def _parse(self) -> Any:
        from pydantic_core import from_json

        self.parsed_len = len(self.text)
        body = self.text.lstrip()
        if body.startswith("```"):
            newline = body.find("\n")
            if newline < 0:
                return None
            body = body[newline + 1 :]
        if not body or body[0] not in "{[":
            return None
        try:
            # "trailing-strings": a string still being written is kept, so text grows live.
            value = from_json(body, allow_partial="trailing-strings")
        except ValueError:
            return None
        if value == self.last:
            return None
        self.last = value
        return copy.deepcopy(value)  # consumers may mutate the event; keep ours intact


_UNSET = object()


def assembler_has_calls(assembler: Any) -> bool:
    """True once a tool call started streaming: that turn's text is not the answer."""
    return bool(assembler._calls_by_index or assembler._calls_by_id)


async def _run_output_validators(agent: Agent, ctx: RunContext[Any], output: Any) -> Any:
    """Run ``agent.output_validators`` in order; each gets the previous one's result."""
    from nexura_agents.history import _takes_context

    for validator in agent.output_validators:
        call: Any = validator
        result = call(ctx, output) if _takes_context(validator) else call(output)
        if inspect.isawaitable(result):
            result = await result
        output = result
    return output


def _effective_tool_choice(
    agent: Agent, state: _RunState, choice: str | None, tools_schema: list[Any] | None
) -> str | None:
    """The ``tool_choice`` for this model call, checked against the tools offered."""
    if choice is None or choice == "auto":
        return choice
    if agent.reset_tool_choice and _called_tool_this_turn(state.messages[state.agent_started_at :]):
        return None  # back to the model's default ("auto") after a tool call
    offered = {s["function"]["name"] for s in tools_schema or ()}
    if choice == "none":
        return choice if offered else None
    if not offered:
        raise SDKValidationError(
            f"tool_choice={choice!r}, but agent '{agent.name}' offers no tools on this call."
        )
    if choice != "required" and choice not in offered:
        raise SDKValidationError(
            f"tool_choice names '{choice}', which agent '{agent.name}' does not offer on this "
            f"call (offered: {', '.join(sorted(offered))})."
        )
    return choice


def _called_tool_this_turn(messages: list[Message]) -> bool:
    """True when the model already called a tool since the user's latest message."""
    from nexura_agents.history import _is_retry_feedback

    for message in reversed(messages):
        if message.role == "user" and not _is_retry_feedback(message):
            return False
        if message.role == "assistant" and message.tool_calls:
            return True
    return False


def _apply_edits(
    agent: Agent, messages: list[Message], calls: list[ToolCall], edits: dict[str, ApproveWith]
) -> list[ToolCall]:
    """Swap in reviewer-edited arguments, in the batch and in the transcript."""
    by_id = {c.id: c for c in calls}
    for call_id, edit in edits.items():
        call = by_id.get(call_id)
        tool = agent.tools_by_name.get(call.name) if call is not None else None
        if call is None or tool is None:
            raise ApprovalDecisionError(f"Cannot edit '{call_id}': it is not a pending tool call.")
        if getattr(tool, "asks_user", False):
            raise ApprovalDecisionError("A question is answered with answers=, not edited.")
        if not isinstance(edit.arguments, dict):
            raise ApprovalDecisionError(f"Edited arguments for '{call_id}' must be a dict.")
        try:
            tool.validate_arguments(edit.arguments)
        except ToolValidationError as exc:
            raise ApprovalDecisionError(
                f"Edited arguments for '{call_id}' do not fit tool '{call.name}': {exc.message}"
            ) from exc
        by_id[call_id] = dataclasses.replace(
            call, arguments=copy.deepcopy(edit.arguments), raw_arguments=None
        )
    edited = [by_id[c.id] for c in calls]
    # The transcript shows what actually ran (call and result stay consistent).
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role == "assistant" and message.tool_calls:
            messages[index] = dataclasses.replace(
                message, tool_calls=[by_id.get(c.id, c) for c in message.tool_calls]
            )
            break
    return edited


def _check_answers(answers: dict[str, Any], questions: set[str], decisions: _Decisions) -> None:
    """``answers`` must answer pending ask_user questions only, with text, once each."""
    unknown = sorted(set(answers) - questions)
    if unknown:
        raise ApprovalDecisionError(
            f"answers given for calls that are not pending questions: {', '.join(unknown)}."
        )
    bad = sorted(k for k, v in answers.items() if not isinstance(v, str))
    if bad:
        raise ApprovalDecisionError(f"Answers must be text; not for: {', '.join(bad)}.")
    approved_questions = sorted(questions & decisions.approved)
    if approved_questions:
        raise ApprovalDecisionError(
            "A question cannot be approved, only answered (or rejected): "
            f"{', '.join(approved_questions)}."
        )


def _cancelled(state: _RunState) -> RunEvent:
    reason = state.cancel.reason if state.cancel is not None else None
    return RunEvent(
        type=RunEventType.RUN_CANCELLED,
        run_id=state.run_id,
        data={
            "reason": reason,
            "usage": state.usage,
            "turns": state.turn,
            "tool_calls_count": state.tool_calls_count,
            "messages": list(state.messages),
            "last_agent": state.agent,
            "agent_path": list(state.path),
            "state": state.ctx.state,
            "usage_by_agent": dict(state.usage_by_agent),
            "usage_by_model": dict(state.usage_by_model),
            "tags": list(state.ctx.tags),
        },
    )


class _RunCancelled(BaseException):  # noqa: N818 - internal control flow, never escapes
    """The run's CancelToken fired during a long wait (a BaseException so that the
    generic error handlers do not turn it into a provider or tool error)."""


async def _until_cancelled(awaitable: Awaitable[Any], cancel: CancelToken | None) -> Any:
    """Await ``awaitable``, stopping early with ``_RunCancelled`` if ``cancel`` fires."""
    if cancel is None:
        return await awaitable
    if cancel.cancelled:
        if inspect.iscoroutine(awaitable):
            awaitable.close()
        raise _RunCancelled
    work = asyncio.ensure_future(awaitable)
    waiter = asyncio.ensure_future(cancel.wait())
    try:
        done, _ = await asyncio.wait({work, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            return work.result()
        raise _RunCancelled
    finally:
        waiter.cancel()
        if not work.done():
            # Our token fired, or the surrounding task was cancelled: stop the work too.
            work.cancel()
            with contextlib.suppress(BaseException):
                await work


@dataclass(frozen=True)
class _TimedOut:
    message: str


def _cache_key(arguments: dict[str, Any]) -> str:
    """Stable key for a tool call's arguments (order-independent JSON)."""
    return json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=repr)


async def _save_session(
    store: SessionStore,
    session_id: str,
    messages: list[Message],
    expected_revision: int,
    run_state: dict[str, Any],
) -> Any:
    """Save the transcript and, when the store supports it, the run state."""
    if _accepts_state(store.save):
        return await store.save(  # type: ignore[call-arg]
            session_id, messages, expected_revision=expected_revision, state=run_state
        )
    if run_state:
        warnings.warn(
            f"{type(store).__name__}.save does not accept state; RunContext.state is not saved "
            "with the session.",
            RuntimeWarning,
            stacklevel=3,
        )
    return await store.save(session_id, messages, expected_revision=expected_revision)


async def _hook(hooks: Any, name: str, *args: Any) -> None:
    """Call an optional hook (hooks objects written before it existed may lack it)."""
    method = getattr(hooks, name, None)
    if method is not None:
        await _until_cancelled(method(*args), None)


def _add_tool_files(agent: Agent, state: _RunState) -> None:
    """Show files returned by this batch's tools to the model, in one message after it.

    Tool results stay text (every provider accepts that); the files follow as a user
    message, which multimodal models read like any other attachment. Models that declare
    they cannot take files get a short description instead.
    """
    from nexura_agents.history import TOOL_FILES_PREFIX

    files, state.tool_files = state.tool_files, []
    names = ", ".join(f"{a.name or 'file'} ({a.mime_type})" for a in files)
    capabilities = getattr(agent.model, "capabilities", None)
    if capabilities is not None and not getattr(capabilities, "can_multimodal", True):
        note = f"{TOOL_FILES_PREFIX} {names}. This model cannot view files, so they are omitted."
        state.messages.append(Message(role="user", content=note))
        return
    state.messages.append(
        Message(role="user", content=f"{TOOL_FILES_PREFIX} {names}.", attachments=files)
    )


def _hook_override(hook: str, value: Any, expected: type) -> Any:
    """A hook's return value: ``None`` (no change) or a replacement of ``expected`` type."""
    if value is None or isinstance(value, expected):
        return value
    raise SDKValidationError(
        f"AgentHooks.{hook} must return None or a {expected.__name__}, not {type(value).__name__}."
    )


def _custom_event(run_id: str, payload: dict[str, Any]) -> RunEvent:
    return RunEvent(type=RunEventType.CUSTOM, run_id=run_id, data=payload)


def _pending_custom(state: _RunState) -> list[RunEvent]:
    """Events ``ctx.emit`` buffered outside a tool batch, as ``CUSTOM`` run events."""
    return [_custom_event(state.run_id, p) for p in state.ctx._events.take_pending()]


def _set_run_attributes(span: Any, ctx: RunContext[Any], session_id: str | None) -> None:
    if session_id:
        span.set(tracing.CONVERSATION_ID, session_id)
    if ctx.tags:
        span.set("nexura.run.tags", list(ctx.tags))
    for key, value in tracing._user_attributes(dict(ctx.metadata)).items():
        span.set(f"nexura.run.metadata.{key}", value)


def _check_tags(tags: Any) -> list[str]:
    if isinstance(tags, (str, bytes)):
        raise SDKValidationError("tags must be a list of strings.")
    items = list(tags)  # once: a generator cannot be read twice
    if not all(isinstance(t, str) for t in items):
        raise SDKValidationError("tags must be a list of strings.")
    return items


def model_label(model: Any) -> str:
    """A readable id for a model in usage breakdowns: its model name, or its class.

    Wrappers (``RetryModel``, ``RateLimitedModel``) keep the wrapped model in ``.model``;
    their label is the wrapped model's.
    """
    for _ in range(8):  # bounded: wrappers nest a few levels at most
        inner = getattr(model, "model", None)
        if inner is None or isinstance(inner, str) or inner is model:
            break
        model = inner
    name = getattr(model, "model", None) or getattr(model, "model_name", None)
    return str(name) if isinstance(name, str) and name else type(model).__name__


def _add_breakdown(state: _RunState, agent: Agent, usage: Usage | None) -> None:
    # A missing report (None) marks the entry incomplete, exactly as it does the total.
    for table, key in (
        (state.usage_by_agent, agent.name),
        (state.usage_by_model, model_label(agent.model)),
    ):
        table[key] = table.get(key, Usage()).add(usage)


def _warn_settings_ignored(model: Any, method: str) -> None:
    warnings.warn(
        f"{type(model).__name__}.{method} does not accept model_settings; "
        "temperature/max_tokens/stop_sequences are not applied.",
        RuntimeWarning,
        stacklevel=3,
    )


def _takes_param(fn: Any, name: str) -> bool:
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


async def _offered_tools(agent: Agent, ctx: RunContext[Any]) -> list[Tool]:
    """The agent's tools the model may see and call now: ``enabled``, then ``prepare_tools``."""
    offered: list[Tool] = []
    for t in agent.tools:
        try:
            if await t.is_enabled(ctx):
                offered.append(t)
        except Exception as exc:
            raise SDKValidationError(
                f"enabled check of tool '{t.name}' failed: {exc}", original_error=exc
            ) from exc
    if agent.prepare_tools is None:
        return offered
    try:
        prepared: Any = agent.prepare_tools(ctx, list(offered))
        if inspect.isawaitable(prepared):
            prepared = await prepared
    except Exception as exc:
        raise SDKValidationError(f"prepare_tools failed: {exc}", original_error=exc) from exc
    if not isinstance(prepared, list) or not all(isinstance(t, Tool) for t in prepared):
        raise SDKValidationError("prepare_tools must return a list of Tool.")
    allowed = {t.name for t in offered}
    names = [t.name for t in prepared]
    extra = sorted(set(names) - allowed)
    if extra:
        raise SDKValidationError(
            f"prepare_tools returned tools that are not enabled on agent '{agent.name}': "
            f"{', '.join(extra)}."
        )
    if len(set(names)) != len(names):
        raise SDKValidationError("prepare_tools returned the same tool name twice.")
    return prepared


def _handoff_offered(agent: Agent, state: _RunState, name: str) -> bool:
    """A handoff the model may call now (offered on the latest call; all after a resume)."""
    if name not in agent.handoffs_by_name:
        return False
    return state.offered_handoffs is None or name in state.offered_handoffs


def _callable_tool(agent: Agent, state: _RunState, name: str) -> Tool | None:
    """The tool a model call may run: one offered on the latest call, else none.

    Resumed or recovered batches (no model call yet in this process) use all agent tools;
    those calls were offered when the model made them.
    """
    if state.offered_tools is not None:
        return state.offered_tools.get(name)
    return agent.tools_by_name.get(name)


@dataclass
class _Decisions:
    """Human decisions for a resumed batch of tool calls."""

    approved: set[str] = field(default_factory=set)
    rejected: dict[str, str] = field(default_factory=dict)
    # Calls whose outcome is unknown after a crash: the model is told, they are not run.
    unknown: set[str] = field(default_factory=set)
    # ask_user calls answered by a person: the answer is the tool result, nothing runs.
    answers: dict[str, str] = field(default_factory=dict)


_UNKNOWN_REASON = "outcome_unknown_after_crash"
_UNKNOWN_RESULT = (
    "The outcome of this action is unknown: the process stopped while it was running. "
    "It was not retried."
)


def _unanswered(messages: list[Message]) -> tuple[list[ToolCall], list[ToolCall]]:
    """The tool calls of the last assistant message that has any, and those without a result."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role == "assistant" and message.tool_calls:
            answered = {m.tool_call_id for m in messages[index + 1 :] if m.role == "tool"}
            calls = list(message.tool_calls)
            return calls, [c for c in calls if c.id not in answered]
    return [], []


def _filter_transcript(handoff: Handoff, body: list[Message]) -> list[Message]:
    """Apply a handoff's ``input_filter`` and check the transcript is still well-formed."""
    if handoff.input_filter is None:
        return body
    filtered = handoff.input_filter(copy.deepcopy(body))
    if not isinstance(filtered, list) or not all(isinstance(m, Message) for m in filtered):
        raise SDKValidationError(
            f"input_filter of handoff '{handoff.tool_name}' must return a list of Message."
        )
    called = {c.id for m in filtered if m.role == "assistant" for c in m.tool_calls or []}
    answered = {m.tool_call_id for m in filtered if m.role == "tool"}
    if called != answered:
        raise SDKValidationError(
            f"input_filter of handoff '{handoff.tool_name}' broke the transcript: every tool "
            "call must keep its result. "
            f"Unanswered: {sorted(called - answered)}; orphan results: "
            f"{sorted(str(i) for i in answered - called)}."
        )
    return filtered


def _resolve_active_agent(start: Agent, interruption: RunInterruption) -> Agent:
    """Find the agent that was active when ``interruption`` was raised, by walking handoffs."""
    path = interruption.agent_path
    target = interruption.active_agent or start.name
    if not path or path[0] != start.name:
        path = [start.name] if target == start.name else []
    if path:
        current = start
        for name in path[1:]:
            nxt = next((h.agent for h in current.handoffs if h.agent.name == name), None)
            if nxt is None:
                path = []
                break
            current = nxt
        if path and current.name == target:
            return current
    # No usable path (old or hand-made payload): breadth-first search by name.
    seen = {id(start)}
    queue = [start]
    while queue:
        node = queue.pop(0)
        if node.name == target:
            return node
        for h in node.handoffs:
            if id(h.agent) not in seen:
                seen.add(id(h.agent))
                queue.append(h.agent)
    raise ApprovalDecisionError(
        f"Interruption was raised by agent '{target}', which is not reachable through the "
        f"handoffs of '{start.name}'."
    )


@dataclass
class _RunState:
    run_id: str
    messages: list[Message]
    ctx: RunContext[Any]
    start_time: float
    effective_timeout: float | None
    agent: Agent  # the active agent; changes on handoff
    root: Agent  # the agent that started the run; owns the run-wide limits
    path: list[str] = field(default_factory=list)
    handoffs_count: int = 0
    # (handoff, reason, tool_call_id) chosen this turn; applied once the batch is finished
    pending_handoff: tuple[Handoff, str, str] | None = None
    root_system: Message | None = None  # the starting agent's system message, for storage
    turn: int = 0
    tool_calls_count: int = 0
    requests: int = 0
    usage: Usage = field(default_factory=Usage)
    output_attempts: int = 0
    tool_retries: dict[str, int] = field(default_factory=dict)
    session_id: str | None = None
    expected_revision: int = 0
    span: Any = tracing.NOOP  # the invoke_agent span of this run
    memory_block: str = ""  # recalled memories, shown to the model but never saved
    checkpoint_store_failed: bool = False  # a checkpoint write failed: keep the last good one
    checkpoint_owner: str | None = None  # recovery lease holder; fences checkpoint writes
    final_text: str | None = None  # the run's final output text, kept in "completed" checkpoints
    agent_started_at: int = 0  # index in messages where the active agent took over (handoffs)
    cancel: CancelToken | None = None  # run(..., cancel=token): stop when it is cancelled
    offered_handoffs: set[str] | None = None  # handoff tools offered on the latest model call
    usage_by_agent: dict[str, Usage] = field(default_factory=dict)
    usage_by_model: dict[str, Usage] = field(default_factory=dict)
    # Files returned by the tools of the current batch (shown in one follow-up message).
    tool_files: list[Attachment] = field(default_factory=list)
    # @tool(cache=True): keyed by the tool object, so another agent's same-named tool
    # (after a handoff) never gets this tool's results.
    tool_cache: dict[tuple[int, str], Any] = field(default_factory=dict)
    # Tools offered on the latest model call (enabled / prepare_tools); None: all tools.
    offered_tools: dict[str, Tool] | None = None

    def remaining(self) -> float | None:
        if self.effective_timeout is None:
            return None
        elapsed = time.monotonic() - self.start_time
        if elapsed >= self.effective_timeout:
            raise ModelTimeoutError(self.effective_timeout, elapsed)
        return self.effective_timeout - elapsed


class Runner:
    """Orchestrates agent execution against models and registered tools.

    Stateless: can be used to run multiple agents concurrently.
    Supports optional persistent session storage across runs.
    """

    def __init__(
        self,
        session_store: SessionStore | None = None,
        *,
        hooks: AgentHooks | None = None,
        usage_limits: UsageLimits | None = None,
        model_settings: ModelSettings | None = None,
        budget: Any | None = None,
        max_handoffs: int | None = None,
        checkpoint_store: RunCheckpointStore | None = None,
    ) -> None:
        if max_handoffs is not None and (
            not isinstance(max_handoffs, int) or isinstance(max_handoffs, bool) or max_handoffs < 0
        ):
            raise SDKValidationError(f"max_handoffs must be an integer >= 0, got {max_handoffs!r}.")
        # Overrides ``Agent.max_handoffs`` of the starting agent when set.
        self.max_handoffs = max_handoffs
        self.session_store = session_store
        # Durable runs: when set, the run is checkpointed and can be recovered (see durable.py).
        self.checkpoint_store = checkpoint_store
        self.hooks = hooks
        self.usage_limits = usage_limits
        self.model_settings = model_settings
        # A usage RunBudget; provider models admit and settle every call against it.
        self.budget = budget

    # ------------------------------------------------------------------ public API

    def run(
        self,
        agent: Agent[OutputT],
        prompt: Prompt,
        *,
        session_id: str | None = None,
        history: list[Message] | None = None,
        timeout: float | None = None,
        session_store: SessionStore | None = None,
        deps: Any = None,
        cancel: CancelToken | None = None,
        state: dict[str, Any] | None = None,
        tags: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> RunResult[OutputT]:
        """Synchronously execute an agent run.

        Refuses unsafe invocation inside an active asyncio event loop.

        Raises:
            EventLoopError: If invoked while an asyncio event loop is already running.
            SDKValidationError: If timeout is nonpositive or nonfinite, or persistence args invalid.
        """
        _validate_timeout(timeout)
        self._refuse_running_loop("run")
        return asyncio.run(
            self.run_async(
                agent,
                prompt,
                session_id=session_id,
                history=history,
                timeout=timeout,
                session_store=session_store,
                deps=deps,
                cancel=cancel,
                state=state,
                tags=tags,
                metadata=metadata,
            )
        )

    async def run_async(
        self,
        agent: Agent[OutputT],
        prompt: Prompt,
        *,
        session_id: str | None = None,
        history: list[Message] | None = None,
        timeout: float | None = None,
        session_store: SessionStore | None = None,
        deps: Any = None,
        cancel: CancelToken | None = None,
        state: dict[str, Any] | None = None,
        tags: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> RunResult[OutputT]:
        """Asynchronously execute an agent to completion (or approval pause, or cancel).

        ``state`` seeds ``RunContext.state`` (merged over state saved with the session).
        """
        _validate_timeout(timeout)
        return await self._collect(
            self.stream_async(
                agent,
                prompt,
                session_id=session_id,
                history=history,
                timeout=timeout,
                session_store=session_store,
                deps=deps,
                cancel=cancel,
                state=state,
                tags=tags,
                metadata=metadata,
            )
        )

    async def resume_async(
        self,
        agent: Agent[OutputT],
        interruption: RunInterruption,
        *,
        approve: bool | Iterable[str] | Mapping[str, str | ApproveWith] = (),
        reject: Mapping[str, str] | None = None,
        answers: Mapping[str, str] | None = None,
        deps: Any = None,
        timeout: float | None = None,
        session_store: SessionStore | None = None,
        cancel: CancelToken | None = None,
    ) -> RunResult[OutputT]:
        """Continue a paused run once every pending tool call has a decision.

        ``approve`` is ``True`` (approve everything not in ``reject``), a list of call IDs,
        or a mapping of call ID -> the action digest the reviewer saw
        (``interruption.pending_digests()``) so approval binds to the exact arguments.
        Approved calls execute; rejected calls are reported to the model with the reason.
        """
        _validate_timeout(timeout)
        return await self._collect(
            self.resume_stream_async(
                agent,
                interruption,
                approve=approve,
                reject=reject,
                answers=answers,
                deps=deps,
                timeout=timeout,
                session_store=session_store,
                cancel=cancel,
            )
        )

    def resume(
        self, agent: Agent[OutputT], interruption: RunInterruption, **kwargs: Any
    ) -> RunResult[OutputT]:
        """Synchronous form of :meth:`resume_async`."""
        self._refuse_running_loop("resume")
        return asyncio.run(self.resume_async(agent, interruption, **kwargs))

    def stream_async(
        self,
        agent: Agent,
        prompt: Prompt,
        *,
        session_id: str | None = None,
        history: list[Message] | None = None,
        timeout: float | None = None,
        session_store: SessionStore | None = None,
        deps: Any = None,
        stream_text: bool = False,
        cancel: CancelToken | None = None,
        state: dict[str, Any] | None = None,
        tags: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> AsyncGenerator[RunEvent, None]:
        """Stream ordered lifecycle and execution events for an agent run.

        Yields events:
            RUN_STARTED: Beginning of the run with metadata.
            MODEL_STARTED: Model request initiated for a turn.
            TEXT_DELTA: Model text as generated (``stream_text=True`` and the model streams).
            MODEL_COMPLETED: Model response received.
            TOOL_STARTED: Tool invocation initiated.
            TOOL_COMPLETED: Tool returned a validated result.
            RETRY: A tool error or invalid output was sent back to the model.
            HANDOFF: Control moved to another agent (``from``, ``to``, ``reason``, ``path``).
            FINAL_OUTPUT: Final assistant response text produced.
            RUN_COMPLETED: Successful terminal completion.
            RUN_PAUSED: Stopped before tool calls that need approval.
            RUN_FAILED: Terminal failure event prior to exception being raised.
            RUN_CANCELLED: ``cancel`` was cancelled; the run stopped (checkpoint kept).
        """
        kwargs: dict[str, Any] = {
            "session_id": session_id,
            "history": history,
            "timeout": timeout,
            "session_store": session_store,
            "deps": deps,
            "stream_text": stream_text,
            "cancel": cancel,
            "start_state": state,
            "tags": tags,
            "metadata": metadata,
        }
        if tracing._STATE is None:
            stream = self._stream_impl(agent, prompt, **kwargs)
        else:
            stream = self._traced(
                tracing.start_run, agent.name, False, self._stream_impl, agent, prompt, **kwargs
            )
        return stream

    async def _stream_impl(
        self,
        agent: Agent,
        prompt: Prompt,
        *,
        session_id: str | None = None,
        history: list[Message] | None = None,
        timeout: float | None = None,
        session_store: SessionStore | None = None,
        deps: Any = None,
        stream_text: bool = False,
        cancel: CancelToken | None = None,
        start_state: dict[str, Any] | None = None,
        tags: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
        _span: Any = tracing.NOOP,
    ) -> AsyncGenerator[RunEvent, None]:
        _validate_timeout(timeout)
        run_id = str(uuid.uuid4())
        effective_timeout = timeout if timeout is not None else agent.timeout
        start_time = time.monotonic()

        effective_store = self._store(session_store)
        if effective_store is not None:
            if not session_id:
                raise SDKValidationError(
                    "Explicit session_id is required when using a session store."
                )
            if history is not None:
                raise SDKValidationError(
                    "Cannot provide both explicit history and a session store. "
                    "Use either explicit history or persisted session history."
                )

        # Load persisted messages if store is used
        persisted_snapshot: SessionSnapshot | None = None
        expected_revision = 0
        if effective_store is not None and session_id is not None:
            persisted_snapshot = await effective_store.get(session_id)
            if persisted_snapshot is not None:
                expected_revision = persisted_snapshot.revision

        # Build isolated in-memory message history for this run
        messages: list[Message] = []
        has_system = False
        if persisted_snapshot is not None:
            messages.extend(copy.deepcopy(persisted_snapshot.messages))
            has_system = any(m.role == "system" for m in messages)
        elif history:
            messages.extend(copy.deepcopy(history))
            has_system = any(m.role == "system" for m in messages)

        initial_state: dict[str, Any] = {}
        if persisted_snapshot is not None:
            initial_state = copy.deepcopy(getattr(persisted_snapshot, "state", None) or {})
        if start_state is not None:
            initial_state.update(copy.deepcopy(start_state))  # run(..., state=) adds/overrides
        ctx: RunContext[Any] = RunContext(
            deps=deps,
            run_id=run_id,
            agent_name=agent.name,
            messages=messages,
            state=initial_state,
            tags=_check_tags(tags),
            metadata=dict(metadata or {}),
        )
        try:
            system_text = await agent.render_instructions_async(ctx)
        except Exception as exc:
            yield _failed(run_id, exc)
            raise
        if has_system:
            # The rendered text is not sent when history/session already has a system
            # message, so do not claim a prompt version that the model never saw.
            ctx.prompt = None
        if ctx.prompt:
            _span.set(tracing.PROMPT_NAME, ctx.prompt["name"])
            _span.set(tracing.PROMPT_VERSION, ctx.prompt["version"])
        # Ensure system prompt is present, without duplicating an existing system prompt
        if system_text and not has_system:
            messages.insert(0, Message(role="system", content=system_text))

        try:
            messages.extend(_prompt_messages(prompt))
        except SDKValidationError as exc:
            yield _failed(run_id, exc)
            raise

        _span.set("nexura.run_id", run_id)
        if agent.handoffs:
            _span.set(tracing.AGENT_PATH, [agent.name])
            _span.set(tracing.LAST_AGENT, agent.name)
        root_system = next((copy.copy(m) for m in messages if m.role == "system"), None)
        state = _RunState(
            run_id=run_id,
            messages=messages,
            ctx=ctx,
            start_time=start_time,
            effective_timeout=effective_timeout,
            agent=agent,
            root=agent,
            path=[agent.name],
            root_system=root_system,
            session_id=session_id,
            expected_revision=expected_revision,
            span=_span,
            cancel=cancel,
        )

        _set_run_attributes(_span, ctx, session_id)
        yield RunEvent(
            type=RunEventType.RUN_STARTED,
            run_id=run_id,
            data={
                "session_id": session_id or run_id,
                "agent_name": agent.name,
                "max_turns": agent.max_turns,
                "max_tool_calls": agent.max_tool_calls,
                "tags": list(ctx.tags),
                "timeout": effective_timeout,
                "prompt": dict(ctx.prompt) if ctx.prompt else None,
            },
        )

        hooks = self.hooks or _NO_HOOKS
        await hooks.on_run_start(ctx)
        await _hook(hooks, "on_agent_start", ctx, agent)
        yield RunEvent(
            type=RunEventType.AGENT_STARTED,
            run_id=run_id,
            data={"agent": agent.name, "path": [agent.name]},
        )

        # Input guardrails see the user's latest message before any model call.
        user_text = next((m.content or "" for m in reversed(messages) if m.role == "user"), "")
        for check in agent.input_guardrails:
            outcome = await run_guardrail(check, ctx, user_text)
            if outcome.replacement is not None:
                bad = TypeError(
                    f"Input guardrail '{getattr(check, '__name__', 'guardrail')}' returned "
                    "rewritten(); only output guardrails can rewrite."
                )
                yield _failed(run_id, bad)
                raise bad
            if outcome.tripwire:
                tripped = GuardrailTripwireTriggered(
                    getattr(check, "__name__", "guardrail"), "input", outcome.info
                )
                yield _failed(run_id, tripped)
                raise tripped

        # Long-term memory: recall once per run (not on resume), attachments-only skips it.
        memory = agent.memory
        if memory is not None and memory.auto_recall and user_text.strip():
            try:
                recall_budget = state.remaining()
                try:
                    state.memory_block, recall_usage = await asyncio.wait_for(
                        memory.recall_block(ctx, user_text), timeout=recall_budget
                    )
                except TimeoutError:
                    raise ModelTimeoutError(
                        state.effective_timeout or 0.0, time.monotonic() - state.start_time
                    ) from None
            except Exception as exc:
                yield _failed(run_id, exc)
                raise
            if recall_usage is not None:
                state.usage = state.usage.add(recall_usage)
                ctx.usage = state.usage

        async for event in self._loop(state, effective_store, hooks, stream_text=stream_text):
            yield event

    def resume_stream_async(
        self,
        agent: Agent,
        interruption: RunInterruption,
        *,
        approve: bool | Iterable[str] | Mapping[str, str | ApproveWith] = (),
        reject: Mapping[str, str] | None = None,
        answers: Mapping[str, str] | None = None,
        deps: Any = None,
        timeout: float | None = None,
        session_store: SessionStore | None = None,
        stream_text: bool = False,
        cancel: CancelToken | None = None,
    ) -> AsyncGenerator[RunEvent, None]:
        """Event-stream form of :meth:`resume_async`."""
        kwargs: dict[str, Any] = {
            "cancel": cancel,
            "approve": approve,
            "reject": reject,
            "answers": answers,
            "deps": deps,
            "timeout": timeout,
            "session_store": session_store,
            "stream_text": stream_text,
        }
        if tracing._STATE is None:
            return self._resume_stream_impl(agent, interruption, **kwargs)
        return self._traced(
            tracing.start_run,
            agent.name,
            True,
            self._resume_stream_impl,
            agent,
            interruption,
            **kwargs,
        )

    async def _resume_stream_impl(
        self,
        agent: Agent,
        interruption: RunInterruption,
        *,
        approve: bool | Iterable[str] | Mapping[str, str | ApproveWith] = (),
        reject: Mapping[str, str] | None = None,
        answers: Mapping[str, str] | None = None,
        deps: Any = None,
        timeout: float | None = None,
        session_store: SessionStore | None = None,
        stream_text: bool = False,
        cancel: CancelToken | None = None,
        _span: Any = tracing.NOOP,
    ) -> AsyncGenerator[RunEvent, None]:
        _validate_timeout(timeout)
        if interruption.agent_name != agent.name:
            raise ApprovalDecisionError(
                f"Interruption belongs to agent '{interruption.agent_name}', not '{agent.name}'."
            )
        # Only calls to the agent's built-in ask_user tool are questions; a user tool that
        # happens to be named "ask_user" is approved or rejected like any other.
        active = _resolve_active_agent(agent, interruption)
        questions = {
            c.id
            for c in interruption.pending
            if getattr(active.tools_by_name.get(c.name), "asks_user", False)
        }
        answers = dict(answers or {})
        if approve is True:
            # Approve everything still pending, bound to the actions in this interruption.
            # Questions are answered, never approved (running ask_user would mean no answer).
            approve = {
                call_id: digest
                for call_id, digest in interruption.pending_digests().items()
                if call_id not in (reject or {}) and call_id not in questions
            }
        elif approve is False:
            approve = ()
        edits = {
            call_id: decision
            for call_id, decision in (approve.items() if isinstance(approve, Mapping) else ())
            if isinstance(decision, ApproveWith)
        }
        decisions = _Decisions(set(approve), dict(reject or {}))
        pending_ids = {c.id for c in interruption.pending}
        _check_answers(answers, questions, decisions)
        decisions.answers = answers
        if isinstance(approve, Mapping):
            expected = interruption.pending_digests()
            for call_id, digest in approve.items():
                if isinstance(digest, ApproveWith):
                    if digest.digest is None:
                        continue  # approved by call id; the edit is checked below
                    digest = digest.digest
                if expected.get(call_id) != digest:
                    raise ApprovalDecisionError(
                        f"Approval for '{call_id}' does not match the pending action; "
                        "the arguments changed after review."
                    )
        decided = decisions.approved | set(decisions.rejected) | set(answers)
        if decided != pending_ids or decisions.approved & set(decisions.rejected):
            raise ApprovalDecisionError(
                "Approve, reject or answer every pending tool call exactly once. "
                f"Pending: {sorted(pending_ids)}; decided: {sorted(decided)}."
            )

        root = agent
        agent = _resolve_active_agent(root, interruption)
        state = self._state_from_interruption(interruption, root, agent, deps, timeout, _span)
        state.cancel = cancel
        messages = state.messages
        if interruption.reason is not None:
            # Calls that may already have run before a crash: a rejection tells the model the
            # outcome is unknown instead of claiming a human refused.
            decisions.unknown, decisions.rejected = set(decisions.rejected), {}
        _span.set("nexura.run_id", interruption.run_id)
        _set_run_attributes(_span, state.ctx, state.session_id)
        if root.handoffs:
            _span.set(tracing.AGENT_PATH, list(state.path))
            _span.set(tracing.LAST_AGENT, agent.name)
        hooks = self.hooks or _NO_HOOKS
        last = messages[-1] if messages else None
        if interruption.reason is None:
            if last is None or last.role != "assistant" or not last.tool_calls:
                raise ApprovalDecisionError("Interruption transcript does not end with tool calls.")
            batch_calls = list(last.tool_calls)
        else:
            all_calls, batch_calls = _unanswered(messages)
            if not batch_calls:
                raise ApprovalDecisionError("Interruption transcript has no unanswered tool calls.")
            self._restore_handoff(state, all_calls, interruption)
        # The calls that will execute must be exactly the ones that were reviewed.
        batch = {c.id: RunInterruption.action_digest(c) for c in batch_calls}
        for call_id, digest in interruption.pending_digests().items():
            if batch.get(call_id) != digest:
                raise ApprovalDecisionError(
                    f"Pending call '{call_id}' differs from the transcript; refusing to resume."
                )
        if edits:
            batch_calls = _apply_edits(agent, state.messages, batch_calls, edits)
        async for event in self._loop(
            state,
            self._store(session_store),
            hooks,
            stream_text=stream_text,
            resume_batch=(batch_calls, decisions),
        ):
            yield event

    @staticmethod
    async def _traced(
        start: Any,
        agent_name: str,
        resumed: bool,
        impl: Any,
        *args: Any,
        **kwargs: Any,
    ) -> AsyncGenerator[RunEvent, None]:
        """Run ``impl`` inside an ``invoke_agent`` span (opt-in, see ``nexura_agents.tracing``).

        The span is parented explicitly on children instead of being kept current across
        ``yield``s, which would break when the consumer resumes the stream from another task.
        """
        span = start(agent_name, resumed=resumed)
        status = "failed"
        try:
            async for event in impl(*args, **kwargs, _span=span):
                if event.type == RunEventType.RUN_COMPLETED:
                    status = "completed"
                    span.set_usage(event.data.get("usage"))
                elif event.type == RunEventType.RUN_PAUSED:
                    status = "paused"
                    span.set_usage(event.data["interruption"].usage)
                elif event.type == RunEventType.RUN_CANCELLED:
                    status = "cancelled"
                    span.set_usage(event.data.get("usage"))
                yield event
        except (asyncio.CancelledError, GeneratorExit):
            status = "cancelled"
            raise
        except BaseException as exc:
            status = "failed"
            span.fail(exc)
            raise
        finally:
            span.set("nexura.run.status", status)
            if status == "cancelled":
                span.cancelled()
            span.end()

    # ------------------------------------------------------------------ the loop

    def _loop(
        self,
        state: _RunState,
        store: SessionStore | None,
        hooks: AgentHooks,
        *,
        stream_text: bool,
        resume_batch: tuple[list[ToolCall], _Decisions] | None = None,
    ) -> AsyncGenerator[RunEvent, None]:
        if self.checkpoint_store is None:
            return self._loop_body(state, store, hooks, stream_text, resume_batch)
        return self._durable_loop(state, store, hooks, stream_text, resume_batch)

    async def _durable_loop(
        self,
        state: _RunState,
        store: SessionStore | None,
        hooks: AgentHooks,
        stream_text: bool,
        resume_batch: tuple[list[ToolCall], _Decisions] | None,
    ) -> AsyncGenerator[RunEvent, None]:
        """The loop with checkpoint cleanup: finished or failed runs leave no checkpoint.

        A crash (``BaseException``: process kill, cancellation) leaves the last checkpoint.
        """
        assert self.checkpoint_store is not None
        try:
            async for event in self._loop_body(state, store, hooks, stream_text, resume_batch):
                if event.type == RunEventType.RUN_COMPLETED:
                    # The run is done and persisted: a failing delete must not turn it into
                    # an error. A stale "completed" checkpoint is finished by recover.
                    await self._delete_checkpoint(state.run_id, state.checkpoint_owner)
                yield event
        except Exception:
            # A storage error is not a run failure: the previous checkpoint stays the truth.
            if not state.checkpoint_store_failed:
                await self._delete_checkpoint(state.run_id, state.checkpoint_owner)
            raise

    async def _loop_body(
        self,
        state: _RunState,
        store: SessionStore | None,
        hooks: AgentHooks,
        stream_text: bool,
        resume_batch: tuple[list[ToolCall], _Decisions] | None,
    ) -> AsyncGenerator[RunEvent, None]:
        run_id = state.run_id
        ctx = state.ctx
        messages = state.messages
        root = state.root

        try:
            if resume_batch is not None:
                calls, decisions = resume_batch
                resumed_agent = state.agent
                async for event in self._run_tools(resumed_agent, state, hooks, calls, decisions):
                    yield event
                async for event in self._apply_handoff(state):
                    yield event
                if state.agent is resumed_agent:
                    stopped = False
                    # The whole batch counts, including calls finished before a crash.
                    batch = _unanswered(state.messages)[0]
                    async for event in self._stop_at_tool(
                        state, store, hooks, resumed_agent, batch
                    ):
                        stopped = True
                        yield event
                    if stopped:
                        return

            while True:
                agent = state.agent
                if state.cancel is not None and state.cancel.cancelled:
                    yield _cancelled(state)  # between steps: nothing is half-done
                    return
                if self.checkpoint_store is not None:
                    await self._checkpoint(state, "model_next")
                # 1. Enforce elapsed timeout before turn begins
                try:
                    remaining_timeout = state.remaining()
                except ModelTimeoutError as turn_timeout_err:
                    yield _failed(run_id, turn_timeout_err)
                    raise turn_timeout_err

                # 2. Enforce turn limits
                state.turn += 1
                ctx.turn = state.turn
                if state.turn > root.max_turns:
                    turns_err = MaxTurnsExceededError(state.turn - 1, root.max_turns)
                    yield _failed(run_id, turns_err)
                    raise turns_err
                if self.budget is not None:
                    try:
                        self.budget.record_turn()
                    except NexuraError as budget_err:
                        yield _failed(run_id, budget_err)
                        raise

                yield RunEvent(
                    type=RunEventType.MODEL_STARTED,
                    run_id=run_id,
                    data={"turn": state.turn, "messages_count": len(messages)},
                )

                # 3. Invoke the model (unless a before_model hook answers instead)
                try:
                    from_hook = _hook_override(
                        "before_model", await hooks.before_model(ctx, messages), ModelResponse
                    )
                except SDKValidationError as hook_err:
                    yield _failed(run_id, hook_err)
                    raise
                holder: list[ModelResponse] = [from_hook] if from_hook is not None else []
                chat = tracing.start_chat(state.span, agent.model) if not holder else tracing.NOOP
                if chat.capturing:
                    chat.content(
                        tracing.EVENT_PROMPT, "gen_ai.prompt", tracing.messages_payload(messages)
                    )
                try:
                    if from_hook is None:
                        async for delta_event in self._call_model(
                            agent, state, remaining_timeout, stream_text, holder, chat
                        ):
                            yield delta_event
                except TimeoutError as exc:
                    chat.fail(exc)
                    elapsed = time.monotonic() - state.start_time
                    model_timeout_err = ModelTimeoutError(state.effective_timeout or 0.0, elapsed)
                    yield _failed(run_id, model_timeout_err)
                    raise model_timeout_err from exc
                except NexuraError as exc:
                    chat.fail(exc)
                    yield _failed(run_id, exc)
                    raise
                except Exception as exc:
                    chat.fail(exc)
                    prov_err = ProviderError(
                        f"Unexpected provider error: {exc}", original_error=exc
                    )
                    yield _failed(run_id, prov_err)
                    raise prov_err from exc
                finally:
                    if not holder:  # failed or abandoned before a response arrived
                        chat.end()
                response = holder[0]
                chat.set_usage(response.usage)
                if response.finish_reason:
                    chat.set(tracing.FINISH_REASONS, [str(response.finish_reason)])
                if chat.capturing:
                    chat.content(
                        tracing.EVENT_COMPLETION,
                        "gen_ai.completion",
                        tracing.messages_payload([response.message]),
                    )
                chat.end()
                billed = response.usage  # what the provider charged, kept if a hook replaces it
                try:
                    replaced = _hook_override(
                        "after_model", await hooks.after_model(ctx, response), ModelResponse
                    )
                except SDKValidationError as hook_err:
                    yield _failed(run_id, hook_err)
                    raise
                if replaced is not None:
                    response = replaced

                # Update usage (registers missing metrics if turn lacks counts); a reply
                # from before_model made no request and is not counted.
                if from_hook is None:
                    state.requests += 1
                    state.usage = state.usage.add(billed)
                    _add_breakdown(state, agent, billed)
                    ctx.usage = state.usage
                    state.span.set_usage(state.usage)
                # Usage from inside history processors or instructions (a summarizer model,
                # ctx.add_usage) counts now, not only after a tool step.
                for nested in ctx.take_nested_usage():
                    state.usage = state.usage.add(nested)
                    ctx.usage = state.usage
                    state.span.set_usage(state.usage)
                limits = self.usage_limits
                if limits is not None and from_hook is None:
                    try:
                        limits.check(state.requests, state.usage)
                    except NexuraError as limit_err:
                        yield _failed(run_id, limit_err)
                        raise

                yield RunEvent(
                    type=RunEventType.MODEL_COMPLETED,
                    run_id=run_id,
                    data={
                        "turn": state.turn,
                        "finish_reason": response.finish_reason,
                        "usage": billed if from_hook is None else None,
                        "has_tool_calls": bool(response.message.tool_calls),
                        "from_hook": from_hook is not None or replaced is not None,
                    },
                )
                for custom in _pending_custom(state):  # from instructions and model hooks
                    yield custom

                messages.append(response.message)

                # 4. Final answer (no tool calls)
                if not response.message.tool_calls:
                    final_text = response.message.content or ""
                    output: Any = final_text
                    if agent.output_type is not None:
                        try:
                            output = _parse_output(agent, final_text)
                        except ValueError as invalid:
                            if state.output_attempts < agent.output_retries:
                                state.output_attempts += 1
                                from nexura_agents.history import (
                                    OUTPUT_RETRY_PREFIX,
                                    OUTPUT_RETRY_SUFFIX,
                                )

                                feedback = f"{OUTPUT_RETRY_PREFIX}{invalid}{OUTPUT_RETRY_SUFFIX}"
                                messages.append(Message(role="user", content=feedback))
                                yield RunEvent(
                                    type=RunEventType.RETRY,
                                    run_id=run_id,
                                    data={"reason": "output_validation", "error": str(invalid)},
                                )
                                continue
                            out_err = OutputValidationError(
                                str(invalid),
                                raw_output=final_text,
                                attempts=state.output_attempts + 1,
                            )
                            yield _failed(run_id, out_err)
                            raise out_err from invalid

                    if agent.output_validators:
                        try:
                            output = await _until_cancelled(
                                _run_output_validators(agent, ctx, output), state.cancel
                            )
                        except ModelRetry as retry:
                            if state.output_attempts < agent.output_retries:
                                state.output_attempts += 1
                                from nexura_agents.history import (
                                    VALIDATOR_RETRY_PREFIX,
                                    VALIDATOR_RETRY_SUFFIX,
                                )

                                messages.append(
                                    Message(
                                        role="user",
                                        content=f"{VALIDATOR_RETRY_PREFIX}{retry.message}"
                                        f"{VALIDATOR_RETRY_SUFFIX}",
                                    )
                                )
                                yield RunEvent(
                                    type=RunEventType.RETRY,
                                    run_id=run_id,
                                    data={"reason": "output_validator", "error": retry.message},
                                )
                                continue
                            rejected = OutputValidationError(
                                f"Output validator rejected the answer: {retry.message}",
                                raw_output=final_text,
                                attempts=state.output_attempts + 1,
                            )
                            yield _failed(run_id, rejected)
                            raise rejected from retry
                        except Exception as exc:
                            yield _failed(run_id, exc)
                            raise
                        if agent.output_type is None and isinstance(output, str):
                            final_text = output  # a validator may clean up a text answer
                            messages[-1] = dataclasses.replace(messages[-1], content=final_text)

                    async for event in self._finish_run(
                        state, store, hooks, agent, final_text, output, from_tool=False
                    ):
                        yield event
                    return

                # 5. Tool calls: pause first if any of them needs a human decision.
                calls = list(response.message.tool_calls)
                # A call to a tool that was not offered fails the whole batch before
                # anything runs or is paused: a hidden tool must never ride along with an
                # approved call into resume or durable recovery.
                for tc in calls:
                    if (
                        not _handoff_offered(agent, state, tc.name)
                        and _callable_tool(agent, state, tc.name) is None
                    ):
                        permitted = state.offered_tools or {}
                        hidden_err = UnknownToolError(
                            tool_name=tc.name, permitted_tools=list(permitted)
                        )
                        yield _failed(run_id, hidden_err)
                        raise hidden_err
                pending = [
                    tc
                    for tc in calls
                    if (tool := _callable_tool(agent, state, tc.name)) is not None
                    and tool.needs_approval(ctx, tc.arguments)
                ]
                if pending:
                    for waiting in pending:
                        pause_span = tracing.start_tool(state.span, waiting, pending_approval=True)
                        pause_span.content(
                            "gen_ai.tool.call.arguments",
                            "gen_ai.tool.call.arguments",
                            waiting.arguments,
                        )
                        pause_span.end()
                    # The whole batch waits; on resume the other calls run with the approved ones.
                    interruption = self._make_interruption(state, pending, copy_messages=True)
                    if self.checkpoint_store is not None:
                        await self._save_interruption(state, "paused", interruption)
                    yield RunEvent(
                        type=RunEventType.RUN_PAUSED,
                        run_id=run_id,
                        data={
                            "pending": [
                                {"id": c.id, "name": c.name, "arguments": c.arguments}
                                for c in pending
                            ],
                            "interruption": interruption,
                            "last_agent": agent,
                            "agent_path": list(state.path),
                        },
                    )
                    return

                async for event in self._run_tools(agent, state, hooks, calls, None):
                    yield event
                async for event in self._apply_handoff(state):
                    yield event
                if state.agent is agent:  # no handoff: tool_use_behavior may end the run
                    stopped = False
                    async for event in self._stop_at_tool(state, store, hooks, agent, calls):
                        stopped = True
                        yield event
                    if stopped:
                        return

        except _RunCancelled:
            # The token interrupted a model call or tool. Nothing from that step is
            # recorded; the checkpoint is kept (it is deleted only on completion/failure).
            yield _cancelled(state)
            return
        except asyncio.CancelledError:
            # Propagate cancellation cleanly
            raise

    async def _call_model(
        self,
        agent: Agent,
        state: _RunState,
        remaining_timeout: float | None,
        stream_text: bool,
        holder: list[ModelResponse],
        chat: Any = tracing.NOOP,
    ) -> AsyncGenerator[RunEvent, None]:
        """Call the model once; yields TEXT_DELTA events and stores the response."""
        model: Any = agent.model
        offered = await _offered_tools(agent, state.ctx)
        state.offered_tools = {t.name: t for t in offered}
        schemas = [t.to_schema() for t in offered]
        offered_handoffs = [h for h in agent.handoffs if await h.tool.is_enabled(state.ctx)]
        state.offered_handoffs = {h.tool_name for h in offered_handoffs}
        schemas.extend(h.tool.to_schema() for h in offered_handoffs)
        tools_schema = schemas or None
        snapshot = copy.deepcopy(state.messages)
        if agent.history_processors:
            from nexura_agents.history import apply_processors

            snapshot = await apply_processors(agent.history_processors, state.ctx, snapshot)
        if state.memory_block:
            _add_memory_block(snapshot, state.memory_block)

        # Native structured output: only models that opt in get the schema (see
        # Agent.native_output_schema); the others keep the schema in the prompt.
        native_schema = agent.native_output_schema()
        settings = (self.model_settings or ModelSettings()).merged(agent.model_settings)
        choice = _effective_tool_choice(agent, state, settings.tool_choice, tools_schema)
        if choice != settings.tool_choice:
            settings = dataclasses.replace(settings, tool_choice=choice)

        if not _accepts_run_context(model):
            # Legacy models that opt in (capabilities.can_stream + stream()) stream text;
            # native-schema turns stream only when the model sets streams_response_schema.
            caps = getattr(model, "capabilities", None)
            if (
                stream_text
                and (native_schema is None or getattr(model, "streams_response_schema", False))
                and caps is not None
                and caps.can_stream
                and callable(getattr(model, "stream", None))
            ):
                stream_extra: dict[str, Any] = {}
                if native_schema is not None:
                    stream_extra["response_schema"] = native_schema
                if not settings.is_empty():
                    if _takes_param(model.stream, "model_settings"):
                        stream_extra["model_settings"] = settings
                    else:
                        _warn_settings_ignored(model, "stream")
                legacy_stream = model.stream(
                    snapshot, tools_schema, timeout=remaining_timeout, **stream_extra
                )
                async for event in self._consume_stream(
                    legacy_stream, state, holder, remaining_timeout
                ):
                    yield event
                return
            extra: dict[str, Any] = {}
            if native_schema is not None:
                extra["response_schema"] = native_schema
            if not settings.is_empty():
                if _accepts_model_settings(model):
                    extra["model_settings"] = settings
                else:
                    _warn_settings_ignored(model, "generate")
            call = model.generate(
                messages=snapshot, tools=tools_schema, timeout=remaining_timeout, **extra
            )
            with chat.activated():
                if remaining_timeout is not None:
                    holder.append(
                        await _until_cancelled(
                            asyncio.wait_for(call, timeout=remaining_timeout), state.cancel
                        )
                    )
                else:
                    holder.append(await _until_cancelled(call, state.cancel))
            return

        # Provider models: pass settings and an ExecutionContext so budgets, cancellation
        # and provider policies apply to agent runs.
        from nexura_agents.contracts.context import ExecutionContext
        from nexura_agents.providers.types import GenerationRequest

        request = GenerationRequest(
            messages=snapshot,
            tools=tools_schema,
            schema=native_schema,
            timeout=remaining_timeout,
            max_tokens=settings.max_tokens,
            temperature=settings.temperature,
            stop_sequences=settings.stop_sequences,
            tool_choice=settings.tool_choice,
            top_p=settings.top_p,
            seed=settings.seed,
            presence_penalty=settings.presence_penalty,
            frequency_penalty=settings.frequency_penalty,
            reasoning_effort=settings.reasoning_effort,
            thinking_budget_tokens=settings.thinking_budget_tokens,
            extra=settings.extra,
        )
        exec_ctx = ExecutionContext(
            run_id=state.run_id,
            node_id=f"{agent.name}:turn-{state.turn}",
            budget=self.budget,
            attributes={"agent": agent.name},
        )
        capabilities = getattr(model, "capabilities", None)
        # Native-schema turns stream only when the model sets streams_response_schema.
        if not (stream_text and capabilities is not None and capabilities.can_stream) or (
            native_schema is not None and not getattr(model, "streams_response_schema", False)
        ):
            call = model.generate(request, context=exec_ctx)
            with chat.activated():
                if remaining_timeout is not None:
                    holder.append(
                        await _until_cancelled(
                            asyncio.wait_for(call, timeout=remaining_timeout), state.cancel
                        )
                    )
                else:
                    holder.append(await _until_cancelled(call, state.cancel))
            return

        stream = model.stream(request, context=exec_ctx)
        async for event in self._consume_stream(stream, state, holder, remaining_timeout):
            yield event

    async def _consume_stream(
        self,
        stream: Any,
        state: _RunState,
        holder: list[ModelResponse],
        remaining_timeout: float | None,
    ) -> AsyncGenerator[RunEvent, None]:
        """Drain a model's ``StreamDelta`` iterator into TEXT_DELTA events and one response.

        The response is stored only when the stream completes, so a mid-stream error or a
        cancellation never commits partial text; the stream is always closed.
        """
        from nexura_agents.providers.types import ToolCallAssembler

        text_parts: list[str] = []
        assembler = ToolCallAssembler()
        usage: Usage | None = None
        finish_reason: str | None = None
        partials = _PartialOutput() if state.agent.output_type is not None else None
        thinking: list[dict[str, Any]] = []  # Anthropic thinking blocks, sent back next call
        call_ids: dict[int, tuple[str | None, str | None]] = {}  # index -> (id, name) so far
        try:
            async with asyncio.timeout(remaining_timeout):
                async for delta in stream:
                    if state.cancel is not None and state.cancel.cancelled:
                        raise _RunCancelled  # streamed calls stop at the next chunk
                    if delta.kind == "text_delta" and delta.text:
                        text_parts.append(delta.text)
                        yield RunEvent(
                            type=RunEventType.TEXT_DELTA,
                            run_id=state.run_id,
                            data={"turn": state.turn, "text": delta.text},
                        )
                        if partials is not None and not assembler_has_calls(assembler):
                            partial = partials.feed(delta.text)
                            if partial is not None:
                                yield RunEvent(
                                    type=RunEventType.PARTIAL_OUTPUT,
                                    run_id=state.run_id,
                                    data={"turn": state.turn, "output": partial},
                                )
                    elif delta.kind == "tool_call_delta":
                        assembler.feed(delta)
                        index = delta.tool_call_index or 0
                        known = call_ids.get(index, (None, None))
                        call_ids[index] = (
                            delta.tool_call_id or known[0],
                            delta.tool_name or known[1],
                        )
                        # Keepalive chunks (nothing new) emit no event.
                        if delta.tool_arguments_delta or call_ids[index] != known:
                            yield RunEvent(
                                type=RunEventType.TOOL_CALL_DELTA,
                                run_id=state.run_id,
                                data={
                                    "turn": state.turn,
                                    "index": index,
                                    "id": call_ids[index][0],
                                    "name": call_ids[index][1],
                                    "arguments_delta": delta.tool_arguments_delta or "",
                                },
                            )
                    elif (
                        delta.kind == "continuation_delta"
                        and delta.continuation is not None
                        and delta.continuation.token == "anthropic_thinking"
                    ):
                        thinking.append(delta.continuation.provider_metadata.get("block", {}))
                    elif delta.kind == "finish" and delta.finish_reason == "refusal" and delta.text:
                        # Same text as a non-streamed refusal, so it is reported, not lost.
                        if not text_parts or not text_parts[0].startswith("Model Refusal: "):
                            text_parts.insert(0, "Model Refusal: ")
                        text_parts.append(delta.text)
                    if delta.usage is not None:
                        usage = delta.usage
                    if delta.finish_reason and finish_reason != "refusal":
                        finish_reason = delta.finish_reason
        finally:
            aclose = getattr(stream, "aclose", None)
            if callable(aclose):
                await aclose()
        tool_calls = assembler.validate_and_assemble()
        if partials is not None and not tool_calls and finish_reason != "refusal":
            last = partials.flush()
            if last is not None:
                yield RunEvent(
                    type=RunEventType.PARTIAL_OUTPUT,
                    run_id=state.run_id,
                    data={"turn": state.turn, "output": last},
                )
        holder.append(
            ModelResponse(
                message=Message(
                    role="assistant",
                    content="".join(text_parts) or None,
                    tool_calls=tool_calls or None,
                    provider_data={"anthropic_thinking": thinking} if thinking else None,
                ),
                usage=usage,
                finish_reason=finish_reason,
            )
        )

    async def _finish_run(
        self,
        state: _RunState,
        store: SessionStore | None,
        hooks: AgentHooks,
        agent: Agent,
        final_text: str,
        output: Any,
        *,
        from_tool: bool,
    ) -> AsyncGenerator[RunEvent, None]:
        """Output guardrails, session save, completed checkpoint and the final events.

        ``from_tool``: the output is a tool result (``tool_use_behavior``), so a guardrail
        rewrite changes what the caller gets but not the stored tool message.
        """
        run_id = state.run_id
        ctx = state.ctx
        messages = state.messages
        for check in agent.output_guardrails:
            outcome = await run_guardrail(check, ctx, output)
            if outcome.tripwire:
                tripped = GuardrailTripwireTriggered(
                    getattr(check, "__name__", "guardrail"), "output", outcome.info
                )
                yield _failed(run_id, tripped)
                raise tripped
            if outcome.replacement is not None:
                if agent.output_type is not None:
                    bad = TypeError(
                        "An output guardrail returned a rewritten text for an "
                        "agent with output_type; rewriting supports text only."
                    )
                    yield _failed(run_id, bad)
                    raise bad
                final_text = output = outcome.replacement
                if not from_tool:
                    # Keep the stored history in step with what the caller gets.
                    messages[-1] = dataclasses.replace(messages[-1], content=final_text)

        yield RunEvent(
            type=RunEventType.FINAL_OUTPUT,
            run_id=run_id,
            data={"output": final_text},
        )
        final_messages = self._storage_messages(state)

        # Persist session if store is configured; must happen BEFORE RUN_COMPLETED
        if store is not None and state.session_id is not None:
            try:
                await _save_session(
                    store, state.session_id, final_messages, state.expected_revision, ctx.state
                )
            except Exception as exc:
                yield _failed(run_id, exc)
                raise

        if self.checkpoint_store is not None:
            # The session is saved: record that, so a crash from here on never
            # repeats the final turn (and its session save).
            state.final_text = final_text
            await self._checkpoint(state, "completed")
        await _hook(hooks, "on_agent_end", ctx, agent, output)
        await hooks.on_run_end(ctx, output)
        for custom in _pending_custom(state):
            yield custom
        yield RunEvent(
            type=RunEventType.RUN_COMPLETED,
            run_id=run_id,
            data={
                "output": final_text,
                "output_value": output,
                "turns": state.turn,
                "tool_calls_count": state.tool_calls_count,
                "usage": state.usage,
                "messages": final_messages,
                "last_agent": agent,
                "agent_path": list(state.path),
                "state": ctx.state,
                "usage_by_agent": dict(state.usage_by_agent),
                "usage_by_model": dict(state.usage_by_model),
                "tags": list(state.ctx.tags),
            },
        )

    async def _stop_at_tool(
        self,
        state: _RunState,
        store: SessionStore | None,
        hooks: AgentHooks,
        agent: Agent,
        calls: list[ToolCall],
    ) -> AsyncGenerator[RunEvent, None]:
        """End the run with a tool result when ``agent.tool_use_behavior`` says so.

        Yields nothing when the model should be called again.
        """
        final_text = _tool_final_text(agent, state, calls)
        if final_text is None:
            return
        output: Any = final_text
        if agent.output_type is not None:
            try:
                output = _parse_output(agent, final_text)
            except ValueError as invalid:
                out_err = OutputValidationError(
                    f"The tool result chosen as the final output does not match output_type: "
                    f"{invalid}",
                    raw_output=final_text,
                    attempts=1,
                )
                yield _failed(state.run_id, out_err)
                raise out_err from invalid
        if agent.output_validators:
            try:
                output = await _until_cancelled(
                    _run_output_validators(agent, state.ctx, output), state.cancel
                )
            except ModelRetry as retry:
                # The output came from a tool: there is no model turn to retry.
                rejected = OutputValidationError(
                    f"Output validator rejected the tool result: {retry.message}",
                    raw_output=final_text,
                    attempts=1,
                )
                yield _failed(state.run_id, rejected)
                raise rejected from retry
            except Exception as exc:
                yield _failed(state.run_id, exc)
                raise
            if agent.output_type is None and isinstance(output, str):
                final_text = output
        async for event in self._finish_run(
            state, store, hooks, agent, final_text, output, from_tool=True
        ):
            yield event

    async def _run_tools(
        self,
        agent: Agent,
        state: _RunState,
        hooks: AgentHooks,
        calls: list[ToolCall],
        decisions: _Decisions | None,
    ) -> AsyncGenerator[RunEvent, None]:
        """Execute one batch of tool calls (in parallel when the agent allows it)."""
        run_id = state.run_id
        ctx = state.ctx
        if self.checkpoint_store is not None:
            await self._checkpoint(state, "tools_pending")

        to_run: list[tuple[ToolCall, Tool]] = []
        for tc in calls:
            # Enforce overall run deadline prior to dispatching each tool call
            try:
                state.remaining()
            except ModelTimeoutError as tool_pre_timeout_err:
                yield _failed(run_id, tool_pre_timeout_err)
                raise tool_pre_timeout_err

            chosen = agent.handoffs_by_name.get(tc.name)
            if chosen is not None:
                # A handoff is not a tool run: results stay in call order, the switch is
                # applied after the batch (see ``_apply_handoff``).
                if to_run:
                    async for event in self._finish_tools(agent, state, hooks, to_run, decisions):
                        yield event
                    to_run = []
                async for event in self._dispatch_handoff(agent, state, tc, chosen):
                    yield event
                continue

            # Enforce cumulative tool call bound before dispatch
            if state.tool_calls_count + len(to_run) + 1 > state.root.max_tool_calls:
                tc_err = MaxToolCallsExceededError(
                    state.tool_calls_count + len(to_run) + 1, state.root.max_tool_calls
                )
                yield _failed(run_id, tc_err)
                raise tc_err

            yield RunEvent(
                type=RunEventType.TOOL_STARTED,
                run_id=run_id,
                data={"tool_call_id": tc.id, "tool_name": tc.name, "arguments": tc.arguments},
            )
            tool = _callable_tool(agent, state, tc.name)
            if tool is None:
                permitted = (
                    agent.tools_by_name if state.offered_tools is None else state.offered_tools
                )
                unk_err = UnknownToolError(tool_name=tc.name, permitted_tools=list(permitted))
                yield _failed(run_id, unk_err)
                raise unk_err
            rejected = decisions is not None and (
                tc.id in decisions.rejected or tc.id in decisions.unknown
            )
            if self.budget is not None and not rejected:
                try:
                    self.budget.record_tool_call(tc.name)
                except NexuraError as budget_err:
                    yield _failed(run_id, budget_err)
                    raise
            to_run.append((tc, tool))
            if not agent.parallel_tool_calls:
                # Sequential: run each call before announcing the next one.
                async for event in self._finish_tools(agent, state, hooks, to_run, decisions):
                    yield event
                to_run = []

        if to_run:
            async for event in self._finish_tools(agent, state, hooks, to_run, decisions):
                yield event

        if state.tool_files:
            _add_tool_files(agent, state)
        for nested in ctx.take_nested_usage():
            state.usage = state.usage.add(nested)
        ctx.usage = state.usage
        state.span.set_usage(state.usage)

    async def _dispatch_handoff(
        self, agent: Agent, state: _RunState, tc: ToolCall, chosen: Handoff
    ) -> AsyncGenerator[RunEvent, None]:
        """Answer one handoff call. The first one in a turn wins; later ones are ignored."""
        run_id = state.run_id
        if state.pending_handoff is not None:
            first = state.pending_handoff[0].agent.name
            state.messages.append(
                Message(
                    role="tool",
                    content=f"Ignored: only one handoff per turn; already transferring to {first}.",
                    tool_call_id=tc.id,
                    name=tc.name,
                )
            )
            if self.checkpoint_store is not None:
                await self._checkpoint(state, "tools_pending")
            return
        limit = self.max_handoffs if self.max_handoffs is not None else state.root.max_handoffs
        if state.handoffs_count + 1 > limit:
            limit_err = MaxHandoffsExceededError(state.handoffs_count + 1, limit)
            yield _failed(run_id, limit_err)
            raise limit_err
        try:
            reason = chosen.reason(tc.arguments)
        except Exception as exc:
            invalid = ToolValidationError(
                tool_name=tc.name, message=str(exc), arguments=tc.arguments, original_error=exc
            )
            yield _failed(run_id, invalid)
            raise invalid from exc
        state.handoffs_count += 1
        state.pending_handoff = (chosen, reason, tc.id)
        state.messages.append(
            Message(
                role="tool",
                content=f"Transferred to {chosen.agent.name}.",
                tool_call_id=tc.id,
                name=tc.name,
            )
        )
        # Checkpoint the handoff before its callback runs: after a crash the callback is not
        # repeated (it runs at most once).
        if self.checkpoint_store is not None:
            await self._checkpoint(state, "tools_pending")
        if chosen.on_handoff is not None:
            try:
                outcome = chosen.on_handoff(
                    state.ctx.for_tool(tc.id, tc.name, 0), chosen.parsed_input(reason)
                )
                if inspect.isawaitable(outcome):
                    await _until_cancelled(outcome, state.cancel)
            except Exception as exc:
                failed = ToolExecutionError(tool_name=tc.name, original_error=exc)
                yield _failed(run_id, failed)
                raise failed from exc

    async def _apply_handoff(self, state: _RunState) -> AsyncGenerator[RunEvent, None]:
        """Make the chosen target the active agent (after the turn's tool results exist)."""
        if state.pending_handoff is None:
            return
        chosen, reason, call_id = state.pending_handoff
        state.pending_handoff = None
        source, target = state.agent, chosen.agent
        ctx, messages = state.ctx, state.messages
        try:
            body = _filter_transcript(chosen, [m for m in messages if m.role != "system"])
            ctx.agent_name = target.name
            ctx.prompt = None
            if chosen.input_type is not None:
                # Visible to the target's instructions, tools and hooks.
                ctx.metadata["handoff_input"] = json.loads(reason)
            else:
                ctx.metadata.pop("handoff_input", None)  # never an earlier agent's input
            system_text = await target.render_instructions_async(ctx)
        except Exception as exc:
            yield _failed(state.run_id, exc)
            raise
        messages[:] = ([Message(role="system", content=system_text)] if system_text else []) + body
        if ctx.prompt:
            state.span.set(tracing.PROMPT_NAME, ctx.prompt["name"])
            state.span.set(tracing.PROMPT_VERSION, ctx.prompt["version"])
        state.agent = target
        state.agent_started_at = len(messages)  # earlier tool calls belong to other agents
        state.output_attempts = 0  # the new agent gets its own output_retries
        state.path.append(target.name)
        state.memory_block = ""  # recalled for the starting agent only
        state.span.set(tracing.AGENT_PATH, list(state.path))
        state.span.set(tracing.LAST_AGENT, target.name)
        mark = tracing.start_handoff(state.span, source.name, target.name, reason, state.path)
        mark.end()
        hooks = self.hooks or _NO_HOOKS
        try:
            await _hook(hooks, "on_agent_end", ctx, source, None)
            await _hook(hooks, "on_handoff", ctx, source, target)
            await _hook(hooks, "on_agent_start", ctx, target)
        except Exception as exc:
            yield _failed(state.run_id, exc)
            raise
        yield RunEvent(
            type=RunEventType.HANDOFF,
            run_id=state.run_id,
            data={
                "from": source.name,
                "to": target.name,
                "reason": reason,
                "tool_call_id": call_id,
                "path": list(state.path),
            },
        )
        yield RunEvent(
            type=RunEventType.AGENT_STARTED,
            run_id=state.run_id,
            data={"agent": target.name, "path": list(state.path)},
        )

    @staticmethod
    def _storage_messages(state: _RunState) -> list[Message]:
        """The transcript to save or return: the starting agent's system message comes back,
        so a later run of the same session starts from the right instructions."""
        if len(state.path) <= 1:
            return state.messages
        rest = [m for m in state.messages if m.role != "system"]
        if state.root_system is None:
            return rest
        return [copy.copy(state.root_system), *rest]

    async def _finish_tools(
        self,
        agent: Agent,
        state: _RunState,
        hooks: AgentHooks,
        batch: list[tuple[ToolCall, Tool]],
        decisions: _Decisions | None,
    ) -> AsyncGenerator[RunEvent, None]:
        run_id = state.run_id
        # Run the batch while streaming ctx.emit events live as the tools send them.
        channel = state.ctx._events
        for custom in _pending_custom(state):
            yield custom
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        channel.attach(queue.put_nowait, asyncio.get_running_loop())
        batch_task = asyncio.ensure_future(
            asyncio.gather(
                *(self._invoke_tool(state, hooks, tc, tool, decisions) for tc, tool in batch),
                return_exceptions=True,
            )
        )
        getter: asyncio.Future[dict[str, Any]] | None = None
        try:
            while True:
                getter = asyncio.ensure_future(queue.get())
                waiting: set[asyncio.Future[Any]] = {getter, batch_task}
                await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                if getter.done():
                    yield _custom_event(run_id, getter.result())
                    continue
                break
            while not queue.empty():
                yield _custom_event(run_id, queue.get_nowait())
            outcomes = batch_task.result()
        finally:
            channel.detach()
            if getter is not None and not getter.done():
                getter.cancel()
            if not batch_task.done():
                batch_task.cancel()
                await asyncio.gather(batch_task, return_exceptions=True)
        for custom in _pending_custom(state):  # sent from a thread just as the batch ended
            yield custom
        # Record every call that finished before failing on the first one that did not, so a
        # sibling that already ran (maybe a write) is in the events and the checkpoint.
        failure: tuple[ToolCall, BaseException] | None = None
        for (tc, _tool), outcome in zip(batch, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                if failure is None:
                    failure = (tc, outcome)
                continue
            result_text, retry_reason = outcome
            state.tool_calls_count += 1
            if retry_reason is not None:
                yield RunEvent(
                    type=RunEventType.RETRY,
                    run_id=run_id,
                    data={"reason": retry_reason, "tool_call_id": tc.id, "tool_name": tc.name},
                )
            yield RunEvent(
                type=RunEventType.TOOL_COMPLETED,
                run_id=run_id,
                data={"tool_call_id": tc.id, "tool_name": tc.name, "result": result_text},
            )
            state.messages.append(
                Message(role="tool", content=result_text, tool_call_id=tc.id, name=tc.name)
            )
            if self.checkpoint_store is not None:
                await self._checkpoint(state, "tools_pending")
        if failure is not None:
            tc, outcome = failure
            if isinstance(outcome, TimeoutError):
                elapsed = time.monotonic() - state.start_time
                timeout_err = ModelTimeoutError(state.effective_timeout or 0.0, elapsed)
                yield _failed(run_id, timeout_err)
                raise timeout_err from outcome
            if isinstance(outcome, (ToolError, NexuraError)):
                yield _failed(run_id, outcome)
                raise outcome
            if not isinstance(outcome, Exception):
                raise outcome
            exec_err = ToolExecutionError(tool_name=tc.name, original_error=outcome)
            yield _failed(run_id, exec_err)
            raise exec_err from outcome

    async def _invoke_tool(
        self,
        state: _RunState,
        hooks: AgentHooks,
        tc: ToolCall,
        tool: Tool,
        decisions: _Decisions | None,
    ) -> tuple[str, str | None]:
        """Run one tool call inside an ``execute_tool`` span when tracing is on."""
        span = tracing.start_tool(state.span, tc)
        if span is tracing.NOOP:
            return await self._invoke_tool_inner(state, hooks, tc, tool, decisions)
        span.content("gen_ai.tool.call.arguments", "gen_ai.tool.call.arguments", tc.arguments)
        span.attach()  # tool code (an agent used as a tool, HTTP clients) nests under it
        try:
            result, retry_reason = await self._invoke_tool_inner(state, hooks, tc, tool, decisions)
        except Exception as exc:
            span.fail(exc)
            raise
        except (asyncio.CancelledError, _RunCancelled):  # task cancel or the run's CancelToken
            span.cancelled()
            raise
        else:
            if retry_reason is not None:
                span.set("nexura.tool.retry_reason", retry_reason)
            span.content("gen_ai.tool.call.result", "gen_ai.tool.call.result", result)
            return result, retry_reason
        finally:
            span.end()

    async def _invoke_tool_inner(
        self,
        state: _RunState,
        hooks: AgentHooks,
        tc: ToolCall,
        tool: Tool,
        decisions: _Decisions | None,
    ) -> tuple[str, str | None]:
        """Run one tool call. Returns (result text, retry reason or None)."""
        if decisions is not None and tc.id in decisions.unknown:
            return _UNKNOWN_RESULT, "outcome_unknown"
        if decisions is not None and tc.id in decisions.rejected:
            reason = decisions.rejected[tc.id] or "no reason given"
            return f"Tool call rejected by a human reviewer: {reason}", "rejected"
        if decisions is not None and tc.id in decisions.answers:
            return decisions.answers[tc.id], None  # the person's answer to ask_user

        retries = state.tool_retries.get(tc.name, 0)
        tool_ctx = state.ctx.for_tool(tc.id, tc.name, retries)
        limit = tool.max_retries if tool.max_retries is not None else 1
        if tool.input_guardrails:
            blocked = await _until_cancelled(
                _bounded(state, check_tool_input(tool, tool_ctx, tc.arguments)), state.cancel
            )
            if blocked is not None:
                return blocked, "guardrail_rejected"
        cache_key = (id(tool), _cache_key(tc.arguments)) if tool.cache else None
        if cache_key is not None and cache_key in state.tool_cache:
            result, files = state.tool_cache[cache_key]  # same call earlier in this run
        else:
            try:
                answered = _hook_override(
                    "before_tool",
                    await _until_cancelled(hooks.before_tool(tool_ctx, tc), state.cancel),
                    str,
                )
                if answered is not None:
                    result = answered  # the hook answered: the tool does not run
                else:
                    result = await self._execute_with_retry(state, tc, tool, tool_ctx)
                if isinstance(result, _TimedOut):
                    return result.message, "tool_timeout"
            except ModelRetry as retry:
                if retries >= limit:
                    raise ToolExecutionError(tool_name=tc.name, original_error=retry) from retry
                state.tool_retries[tc.name] = retries + 1
                return f"Retry requested by tool '{tc.name}': {retry.message}", "model_retry"
            except ToolValidationError as invalid:
                if tool.max_retries is None or retries >= tool.max_retries:
                    raise
                state.tool_retries[tc.name] = retries + 1
                return f"Invalid arguments for '{tc.name}': {invalid.message}", "invalid_arguments"
            rewritten = _hook_override(
                "after_tool",
                await _until_cancelled(hooks.after_tool(tool_ctx, tc, result), state.cancel),
                str,
            )
            files = list(tool_ctx.tool_attachments)
            if rewritten is not None:
                result, files = rewritten, []  # the tool's files are not sent with a rewrite
            if cache_key is not None and answered is None:
                state.tool_cache[cache_key] = (result, files)
        if tool.output_guardrails:
            checked, rejected = await _until_cancelled(
                _bounded(state, check_tool_output(tool, tool_ctx, result)), state.cancel
            )
            if rejected:
                return checked, "guardrail_rejected"  # its files are withheld too
            if checked != result:
                files = []  # a guardrail rewrote the result: do not send the original files
            result = checked
        # Files reach the model only once the result has passed its output guardrails.
        state.tool_files.extend(files)
        return result, None

    async def _execute_with_retry(
        self, state: _RunState, tc: ToolCall, tool: Tool, tool_ctx: RunContext[Any]
    ) -> Any:
        """Run the tool (with its own timeout), retrying transient errors per ``tool.retry``."""
        policy = tool.retry
        attempt = 0
        while True:
            attempt += 1
            remaining = state.remaining()
            run = tool.execute(copy.deepcopy(tc.arguments), tool_ctx)
            own = tool.timeout
            try:
                if own is not None and (remaining is None or own < remaining):
                    # The tool's own limit is the tighter one: its expiry is a tool outcome.
                    return await _until_cancelled(asyncio.wait_for(run, timeout=own), state.cancel)
                return await _until_cancelled(
                    asyncio.wait_for(run, timeout=remaining) if remaining else run, state.cancel
                )
            except (TimeoutError, ToolExecutionError) as failure:
                own_timeout = (
                    isinstance(failure, TimeoutError)
                    and own is not None
                    and (remaining is None or own < remaining)
                )
                cause = failure if isinstance(failure, TimeoutError) else failure.original_error
                retryable = (
                    policy is not None
                    and attempt < policy.max_attempts
                    and isinstance(cause, policy.retry_on)
                    and (own_timeout or not isinstance(failure, TimeoutError))
                    # A timed-out sync tool keeps running in its thread: retrying would run
                    # the same call twice at once.
                    and not (own_timeout and not tool.is_async)
                )
                if retryable:
                    assert policy is not None
                    delay = policy.delay(attempt)
                    left = state.remaining()
                    if left is None or delay < left:
                        await _until_cancelled(asyncio.sleep(delay), state.cancel)
                        continue
                if own_timeout:
                    if tool.on_timeout == "fail":
                        raise ToolExecutionError(
                            tool_name=tc.name, original_error=failure
                        ) from failure
                    return _TimedOut(
                        f"Tool call timed out: '{tc.name}' did not finish within {own:g}s."
                    )
                raise

    # ------------------------------------------------------------------ durable runs

    @staticmethod
    def _state_from_interruption(
        interruption: RunInterruption,
        root: Agent,
        agent: Agent,
        deps: Any,
        timeout: float | None,
        span: Any,
    ) -> _RunState:
        messages = copy.deepcopy(interruption.messages)
        ctx: RunContext[Any] = RunContext(
            deps=deps,
            run_id=interruption.run_id,
            agent_name=agent.name,
            turn=interruption.turn,
            usage=interruption.usage,
            messages=messages,
            state=copy.deepcopy(interruption.state),
            tags=list(interruption.tags),
            metadata=copy.deepcopy(interruption.metadata),
        )
        return _RunState(
            output_attempts=interruption.output_attempts,
            agent_started_at=min(interruption.agent_started_at, len(messages)),
            usage_by_agent=dict(interruption.usage_by_agent),
            usage_by_model=dict(interruption.usage_by_model),
            run_id=interruption.run_id,
            messages=messages,
            ctx=ctx,
            start_time=time.monotonic(),
            effective_timeout=timeout if timeout is not None else root.timeout,
            agent=agent,
            root=root,
            path=list(interruption.agent_path) or [root.name],
            handoffs_count=interruption.handoffs_count,
            root_system=copy.copy(interruption.root_system),
            turn=interruption.turn,
            tool_calls_count=interruption.tool_calls_count,
            requests=interruption.requests,
            usage=interruption.usage,
            session_id=interruption.session_id,
            expected_revision=interruption.expected_revision,
            span=span,
            memory_block=interruption.memory_block,
        )

    @staticmethod
    def _restore_handoff(
        state: _RunState, calls: list[ToolCall], interruption: RunInterruption
    ) -> None:
        """Re-arm a handoff that was chosen but not yet applied when the run stopped."""
        info = interruption.pending_handoff
        if not info:
            return
        call = next((c for c in calls if c.id == info.get("tool_call_id")), None)
        chosen = state.agent.handoffs_by_name.get(call.name) if call is not None else None
        if call is None or chosen is None:
            raise RunRecoveryError(
                f"Checkpoint of run '{state.run_id}' records a handoff the agent no longer has."
            )
        state.pending_handoff = (chosen, info.get("reason", ""), call.id)

    def _make_interruption(
        self,
        state: _RunState,
        pending: list[ToolCall],
        *,
        copy_messages: bool = False,
        reason: str | None = None,
    ) -> RunInterruption:
        agent, root = state.agent, state.root
        handoff = state.pending_handoff
        return RunInterruption(
            run_id=state.run_id,
            agent_name=root.name,
            messages=copy.deepcopy(state.messages) if copy_messages else state.messages,
            pending=pending,
            usage=state.usage,
            turn=state.turn,
            tool_calls_count=state.tool_calls_count,
            requests=state.requests,
            session_id=state.session_id,
            expected_revision=state.expected_revision,
            memory_block=state.memory_block,
            active_agent=agent.name if agent is not root else None,
            agent_path=list(state.path) if len(state.path) > 1 else [],
            handoffs_count=state.handoffs_count,
            root_system=copy.copy(state.root_system),
            reason=reason,
            state=copy.deepcopy(state.ctx.state),
            usage_by_agent=dict(state.usage_by_agent),
            usage_by_model=dict(state.usage_by_model),
            tags=list(state.ctx.tags),
            metadata=copy.deepcopy(state.ctx.metadata),
            output_attempts=state.output_attempts,
            agent_started_at=state.agent_started_at,
            question_ids=[
                c.id
                for c in pending
                if getattr(agent.tools_by_name.get(c.name), "asks_user", False)
            ],
            pending_handoff=(
                {"tool_call_id": handoff[2], "reason": handoff[1]} if handoff is not None else None
            ),
        )

    async def _save_interruption(
        self, state: _RunState, phase: str, interruption: RunInterruption
    ) -> None:
        from nexura_agents.durable import accepts_fencing, encode_checkpoint

        assert self.checkpoint_store is not None
        final = state.final_text if phase == "completed" else None
        data = encode_checkpoint(
            phase,
            interruption.to_json(),
            final_text=final,
            output_attempts=state.output_attempts,
            agent_started_at=state.agent_started_at,
        )
        try:
            if accepts_fencing(self.checkpoint_store.save):
                # Refused when another caller holds the recovery lease, so a stalled or
                # superseded writer can never overwrite newer progress.
                await self.checkpoint_store.save(  # type: ignore[call-arg]
                    state.run_id, data, owner=state.checkpoint_owner
                )
            else:
                await self.checkpoint_store.save(state.run_id, data)
        except Exception:
            state.checkpoint_store_failed = True
            raise

    async def _delete_checkpoint(self, run_id: str, owner: str | None) -> None:
        """Best-effort cleanup; fenced so a stale caller cannot delete a recoverer's work."""
        from nexura_agents.durable import accepts_fencing

        store = self.checkpoint_store
        assert store is not None
        with contextlib.suppress(Exception):
            if accepts_fencing(store.delete, "fenced"):
                await store.delete(run_id, owner, fenced=True)  # type: ignore[call-arg]
            else:
                await store.delete(run_id)

    async def _checkpoint(self, state: _RunState, phase: str) -> None:
        pending = _unanswered(state.messages)[1] if phase == "tools_pending" else []
        await self._save_interruption(state, phase, self._make_interruption(state, pending))

    async def recover_async(
        self,
        agent: Agent[OutputT],
        run_id: str,
        *,
        deps: Any = None,
        on_uncertain: str = "pause",
        timeout: float | None = None,
        session_store: SessionStore | None = None,
        cancel: CancelToken | None = None,
    ) -> RunResult[OutputT]:
        """Continue a run from its last checkpoint after a crash.

        Tool calls that may have executed before the crash are *uncertain*. Tools declared
        ``idempotent=True`` are re-run; for the others ``on_uncertain`` decides:
        ``"pause"`` (default) returns a paused result to approve or reject, ``"fail"``
        raises ``RunRecoveryError``, ``"tell_model"`` reports the outcome as unknown.

        Raises:
            RunRecoveryError: unknown, corrupt or mismatched checkpoint.
        """
        _validate_timeout(timeout)
        return await self._collect(
            self.recover_stream_async(
                agent,
                run_id,
                deps=deps,
                on_uncertain=on_uncertain,
                cancel=cancel,
                timeout=timeout,
                session_store=session_store,
            )
        )

    def recover(self, agent: Agent[OutputT], run_id: str, **kwargs: Any) -> RunResult[OutputT]:
        """Synchronous form of :meth:`recover_async`."""
        self._refuse_running_loop("recover")
        return asyncio.run(self.recover_async(agent, run_id, **kwargs))

    def recover_stream_async(
        self,
        agent: Agent,
        run_id: str,
        *,
        deps: Any = None,
        on_uncertain: str = "pause",
        timeout: float | None = None,
        session_store: SessionStore | None = None,
        stream_text: bool = False,
        cancel: CancelToken | None = None,
    ) -> AsyncGenerator[RunEvent, None]:
        """Event-stream form of :meth:`recover_async`."""
        kwargs: dict[str, Any] = {
            "cancel": cancel,
            "deps": deps,
            "on_uncertain": on_uncertain,
            "timeout": timeout,
            "session_store": session_store,
            "stream_text": stream_text,
        }
        if tracing._STATE is None:
            return self._recover_stream_impl(agent, run_id, **kwargs)

        def start(name: str, *, resumed: bool) -> Any:
            return tracing.start_recover(name)

        return self._traced(
            start, agent.name, True, self._recover_stream_impl, agent, run_id, **kwargs
        )

    async def _recover_stream_impl(
        self,
        agent: Agent,
        run_id: str,
        *,
        deps: Any = None,
        on_uncertain: str = "pause",
        timeout: float | None = None,
        session_store: SessionStore | None = None,
        stream_text: bool = False,
        cancel: CancelToken | None = None,
        _span: Any = tracing.NOOP,
    ) -> AsyncGenerator[RunEvent, None]:
        _validate_timeout(timeout)
        if on_uncertain not in ("pause", "fail", "tell_model"):
            raise SDKValidationError(
                f"on_uncertain must be 'pause', 'fail' or 'tell_model', got {on_uncertain!r}."
            )
        checkpoints = self.checkpoint_store
        if checkpoints is None:
            raise SDKValidationError("recover needs Runner(checkpoint_store=...).")
        # Exclusive recovery lease: a second recoverer of the same run must not continue it.
        claim = getattr(checkpoints, "claim", None)
        owner = uuid.uuid4().hex
        if claim is not None and not await claim(run_id, owner):
            raise RunRecoveryError(f"Run '{run_id}' is already being recovered by another caller.")
        try:
            async for event in self._recover_claimed(
                agent,
                run_id,
                deps=deps,
                on_uncertain=on_uncertain,
                timeout=timeout,
                session_store=session_store,
                stream_text=stream_text,
                cancel=cancel,
                _span=_span,
                _owner=owner if claim is not None else None,
            ):
                yield event
        finally:
            release = getattr(checkpoints, "release", None)
            if claim is not None and release is not None:
                with contextlib.suppress(Exception):
                    await release(run_id, owner)

    async def _recover_claimed(
        self,
        agent: Agent,
        run_id: str,
        *,
        deps: Any = None,
        on_uncertain: str = "pause",
        timeout: float | None = None,
        session_store: SessionStore | None = None,
        stream_text: bool = False,
        cancel: CancelToken | None = None,
        _span: Any = tracing.NOOP,
        _owner: str | None = None,
    ) -> AsyncGenerator[RunEvent, None]:
        from nexura_agents.durable import (
            checkpoint_final_text,
            checkpoint_int,
            checkpoint_output_attempts,
            decode_checkpoint,
        )

        _validate_timeout(timeout)
        if on_uncertain not in ("pause", "fail", "tell_model"):
            raise SDKValidationError(
                f"on_uncertain must be 'pause', 'fail' or 'tell_model', got {on_uncertain!r}."
            )
        checkpoints = self.checkpoint_store
        if checkpoints is None:
            raise SDKValidationError("recover needs Runner(checkpoint_store=...).")
        raw = await checkpoints.load(run_id)
        if raw is None:
            raise RunRecoveryError(f"No checkpoint found for run '{run_id}'.")
        phase, interruption = decode_checkpoint(raw, run_id)
        if interruption.agent_name != agent.name:
            raise RunRecoveryError(
                f"Checkpoint of run '{run_id}' belongs to agent '{interruption.agent_name}', "
                f"not '{agent.name}'."
            )
        try:
            active = _resolve_active_agent(agent, interruption)
        except ApprovalDecisionError as exc:
            raise RunRecoveryError(str(exc), original_error=exc) from exc
        state = self._state_from_interruption(interruption, agent, active, deps, timeout, _span)
        state.checkpoint_owner = _owner
        state.cancel = cancel
        # Older checkpoints keep these counters only in the envelope.
        state.output_attempts = checkpoint_output_attempts(raw) or state.output_attempts
        state.agent_started_at = min(
            checkpoint_int(raw, "agent_started_at") or state.agent_started_at, len(state.messages)
        )
        _span.set("nexura.run_id", run_id)
        _set_run_attributes(_span, state.ctx, state.session_id)
        _span.set("nexura.recovered", True)
        _span.set("nexura.recovery.phase", phase)
        if agent.handoffs:
            _span.set(tracing.AGENT_PATH, list(state.path))
            _span.set(tracing.LAST_AGENT, active.name)

        if phase == "paused":
            # Still waiting for a human: hand the stored interruption back.
            yield RunEvent(
                type=RunEventType.RUN_PAUSED,
                run_id=run_id,
                data={
                    "pending": [
                        {"id": c.id, "name": c.name, "arguments": c.arguments}
                        for c in interruption.pending
                    ],
                    "interruption": interruption,
                    "last_agent": active,
                    "agent_path": list(state.path),
                },
            )
            return

        hooks = self.hooks or _NO_HOOKS
        if phase == "completed":
            # The run finished and its session is saved; only the cleanup was lost. Do not
            # call the model or save again: replay the final answer.
            stored = checkpoint_final_text(raw)
            if stored is not None:
                final_text = stored
            else:  # checkpoints written before final_text was stored
                final_text = state.messages[-1].content or "" if state.messages else ""
            output: Any = final_text
            if active.output_type is not None:
                with contextlib.suppress(ValueError):
                    output = _parse_output(active, final_text)
            yield RunEvent(
                type=RunEventType.FINAL_OUTPUT, run_id=run_id, data={"output": final_text}
            )
            await _hook(hooks, "on_agent_end", state.ctx, active, output)
            await hooks.on_run_end(state.ctx, output)  # at-least-once across a crash
            await self._delete_checkpoint(run_id, state.checkpoint_owner)
            for custom in _pending_custom(state):
                yield custom
            yield RunEvent(
                type=RunEventType.RUN_COMPLETED,
                run_id=run_id,
                data={
                    "output": final_text,
                    "output_value": output,
                    "turns": state.turn,
                    "tool_calls_count": state.tool_calls_count,
                    "usage": state.usage,
                    "messages": self._storage_messages(state),
                    "last_agent": active,
                    "agent_path": list(state.path),
                    "usage_by_agent": dict(state.usage_by_agent),
                    "usage_by_model": dict(state.usage_by_model),
                    "tags": list(state.ctx.tags),
                    "state": state.ctx.state,  # the run's state, as on a normal completion
                },
            )
            return

        effective_store = self._store(session_store)
        resume_batch: tuple[list[ToolCall], _Decisions] | None = None
        if phase == "tools_pending":
            calls, remaining_calls = _unanswered(state.messages)
            self._restore_handoff(state, calls, interruption)
            uncertain = [
                c
                for c in remaining_calls
                if c.name not in active.handoffs_by_name
                and (tool := active.tools_by_name.get(c.name)) is not None
                and not getattr(tool, "idempotent", False)
            ]
            decisions = _Decisions()
            if uncertain:
                names = ", ".join(f"{c.name}({c.id})" for c in uncertain)
                if on_uncertain == "fail":
                    raise RunRecoveryError(
                        f"Run '{run_id}' stopped while these tool calls may have executed: "
                        f"{names}. Their outcome is unknown."
                    )
                if on_uncertain == "tell_model":
                    decisions.unknown = {c.id for c in uncertain}
                else:
                    paused = self._make_interruption(
                        state, uncertain, copy_messages=True, reason=_UNKNOWN_REASON
                    )
                    await self._save_interruption(state, "paused", paused)
                    yield RunEvent(
                        type=RunEventType.RUN_PAUSED,
                        run_id=run_id,
                        data={
                            "pending": [
                                {"id": c.id, "name": c.name, "arguments": c.arguments}
                                for c in uncertain
                            ],
                            "interruption": paused,
                            "last_agent": active,
                            "agent_path": list(state.path),
                        },
                    )
                    return
            resume_batch = (remaining_calls, decisions)

        async for event in self._loop(
            state, effective_store, hooks, stream_text=stream_text, resume_batch=resume_batch
        ):
            yield event

    # ------------------------------------------------------------------ helpers

    def _store(self, session_store: SessionStore | None) -> SessionStore | None:
        return session_store if session_store is not None else self.session_store

    @staticmethod
    def _refuse_running_loop(method: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if loop.is_running():
            raise EventLoopError(
                f"Cannot call runner.{method}() synchronously from an active asyncio event "
                "loop. Use 'await runner.run_async(...)' or "
                "'async for event in runner.stream_async(...)'"
            )

    @staticmethod
    async def _collect(stream: AsyncGenerator[RunEvent, None]) -> RunResult:
        start = time.monotonic()
        events: list[RunEvent] = []
        final_output = ""
        output_value: Any = None
        run_id = ""
        usage = Usage()
        tool_calls_count = 0
        turns_count = 0
        final_messages: list[Message] = []
        interruption: RunInterruption | None = None
        prompt: dict[str, Any] | None = None
        last_agent: Agent | None = None
        agent_path: list[str] = []
        run_state: dict[str, Any] = {}
        tags: list[str] = []
        by_agent: dict[str, Usage] = {}
        by_model: dict[str, Usage] = {}

        async for event in stream:
            events.append(event)
            run_id = event.run_id
            if event.type == RunEventType.RUN_STARTED:
                prompt = event.data.get("prompt")
                tags = list(event.data.get("tags") or [])
            elif event.type == RunEventType.FINAL_OUTPUT:
                final_output = event.data.get("output", "")
            elif event.type == RunEventType.TOOL_COMPLETED:
                tool_calls_count += 1
            elif event.type == RunEventType.MODEL_COMPLETED:
                turns_count = event.data.get("turn", turns_count)
            elif event.type == RunEventType.RUN_COMPLETED:
                usage = event.data.get("usage", usage)
                final_messages = event.data.get("messages", [])
                output_value = event.data.get("output_value", final_output)
                last_agent = event.data.get("last_agent")
                agent_path = event.data.get("agent_path", [])
                run_state = event.data.get("state", run_state)
                by_agent = event.data.get("usage_by_agent", by_agent)
                by_model = event.data.get("usage_by_model", by_model)
                tags = list(event.data.get("tags", tags))
            elif event.type == RunEventType.RUN_PAUSED:
                interruption = event.data["interruption"]
                tags = list(interruption.tags)
                last_agent = event.data.get("last_agent")
                agent_path = event.data.get("agent_path", [])
            elif event.type == RunEventType.RUN_CANCELLED:
                # Close the stream now so its tracing span and cleanup finish before we
                # return (not whenever the generator happens to be garbage-collected).
                await stream.aclose()
                return RunResult(
                    run_id=run_id,
                    final_output="",
                    messages=event.data.get("messages", []),
                    events=events,
                    usage=event.data.get("usage", usage),
                    tool_calls_count=event.data.get("tool_calls_count", tool_calls_count),
                    turns_count=event.data.get("turns", turns_count),
                    duration_seconds=time.monotonic() - start,
                    status="cancelled",
                    prompt=prompt,
                    last_agent=event.data.get("last_agent"),
                    agent_path=event.data.get("agent_path", []),
                    state=event.data.get("state", {}),
                    usage_by_agent=event.data.get("usage_by_agent", {}),
                    usage_by_model=event.data.get("usage_by_model", {}),
                    tags=list(event.data.get("tags", tags)),
                )

        if interruption is not None:
            return RunResult(
                run_id=run_id,
                final_output="",
                messages=interruption.messages,
                events=events,
                usage=interruption.usage,
                tool_calls_count=interruption.tool_calls_count,
                turns_count=interruption.turn,
                duration_seconds=time.monotonic() - start,
                status="paused",
                interruption=interruption,
                prompt=prompt,
                last_agent=last_agent,
                agent_path=agent_path,
                state=interruption.state,
                usage_by_agent=dict(interruption.usage_by_agent),
                usage_by_model=dict(interruption.usage_by_model),
                tags=list(tags),
            )
        return RunResult(
            run_id=run_id,
            final_output=final_output,
            messages=final_messages,
            events=events,
            usage=usage,
            tool_calls_count=tool_calls_count,
            turns_count=turns_count,
            duration_seconds=time.monotonic() - start,
            status="completed",
            output=output_value,
            prompt=prompt,
            last_agent=last_agent,
            agent_path=agent_path,
            state=run_state,
            usage_by_agent=by_agent,
            usage_by_model=by_model,
            tags=list(tags),
        )
