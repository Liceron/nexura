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
import hmac
import inspect
import json
import math
import re
import time
import uuid
import warnings
from collections import OrderedDict
from collections.abc import AsyncGenerator, Awaitable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from nexura_agents import policy, tracing
from nexura_agents._lazy import LazyModule
from nexura_agents.agent import Agent
from nexura_agents.approvals import ApprovalEnvelope, ApprovalStore
from nexura_agents.approvals import check_expiry as _check_approval_expiry
from nexura_agents.approvals import payload as _approval_payload
from nexura_agents.approvals import signature as _approval_signature
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
from nexura_agents.guardrails import (
    GuardrailResult,
    check_tool_input,
    check_tool_output,
    decision_record,
    run_guardrail,
)
from nexura_agents.handoffs import Handoff
from nexura_agents.hooks import AgentHooks, ComposedHooks, approval_vote, is_hooks_object
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
from nexura_agents.usage import admission, linkage

if TYPE_CHECKING:
    import asyncio

    from nexura_agents._agent_journal import AgentJournal
    from nexura_agents.contracts.execution import ReconcileResult, StepIntent, StepKey
    from nexura_agents.durable import ResolvedOutcome, RunCheckpointStore
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


class _RunStopToken(CancelToken):
    """The run's cancel token in a journaled durable run: it fires when the caller's token
    fires, or when the run lease is lost (``stop_for_lost_lease``), so an in-flight model
    call or async tool is interrupted. A sync tool thread cannot be stopped."""

    def __init__(self, parent: CancelToken | None) -> None:
        super().__init__()
        self.parent = parent
        self.lease_lost = False

    @property
    def cancelled(self) -> bool:
        return self._event.is_set() or (self.parent is not None and self.parent.cancelled)

    @property
    def reason(self) -> str | None:
        if self._own_reason is None and self.parent is not None:
            return self.parent.reason
        return self._own_reason

    @reason.setter
    def reason(self, value: str | None) -> None:
        self._own_reason = value

    def stop_for_lost_lease(self) -> None:
        self.lease_lost = True
        self.cancel("run lease lost")

    async def wait(self) -> None:
        if self.parent is None:
            await super().wait()
            return
        waits = {
            asyncio.ensure_future(super().wait()),
            asyncio.ensure_future(self.parent.wait()),
        }
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waits:
                waiter.cancel()


def _raise_if_lease_lost(state: _RunState) -> None:
    """A stop caused by a lost run lease ends the run with ``LeaseLostError``, not cancel."""
    if state.journal is not None:
        state.journal.check()
    if isinstance(state.cancel, _RunStopToken) and state.cancel.lease_lost:
        from nexura_agents.contracts.execution import LeaseLostError

        raise LeaseLostError(f"Run '{state.run_id}': the run lease was lost; stopped.")


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


def _gate_key(turn: int, call_id: str) -> str:
    """The recorded form of a hook approval gate: the call's first step key."""
    from nexura_agents._agent_journal import call_step_key

    return str(call_step_key(turn, call_id))


def _recorded_gates(state: _RunState) -> set[str]:
    """Hook gates of this run: this process's, and those in the run's journal."""
    gates = set(state.hook_gates)
    journal = state.journal
    if journal is not None:
        stored = journal.state.get("hook_gates")
        if isinstance(stored, list):
            gates.update(item for item in stored if isinstance(item, str))
    return gates


def _record_gate(state: _RunState, key: str) -> None:
    """Remember that a hook gated this call. With a journal the gate goes in its state, so
    the commit that pauses the run (or any earlier one) persists it before the call can be
    sent; gates of earlier turns are dropped (only the current batch can be recovered)."""
    prefix = key.split("/", 1)[0] + "/"
    state.hook_gates = {g for g in state.hook_gates if g.startswith(prefix)} | {key}
    journal = state.journal
    if journal is not None:
        stored = journal.state.get("hook_gates")
        kept = [
            g
            for g in (stored if isinstance(stored, list) else [])
            if isinstance(g, str) and g.startswith(prefix)
        ]
        journal.state["hook_gates"] = sorted({*kept, *state.hook_gates})


def _compose_hooks(hooks: Any) -> AgentHooks | None:
    """``Runner(hooks=...)``: one hooks object as is, a list as ``ComposedHooks``."""
    if hooks is None:
        return None
    if isinstance(hooks, Sequence) and not isinstance(hooks, (str, bytes)):
        return ComposedHooks(*hooks) if len(hooks) else None
    if not is_hooks_object(hooks):
        # A set, dict, generator, class or other non-hooks value fails at construction,
        # not on the run's first hook call.
        raise SDKValidationError(
            "hooks must be a hooks object (an AgentHooks instance or an object with its "
            f"methods) or a list of them, got {type(hooks).__name__}."
        )
    return hooks  # type: ignore[no-any-return]


def _decision_event(
    state: _RunState,
    stage: str,
    name: str,
    action: str,
    started: float,
    *,
    info: Any = None,
    error_type: str | None = None,
) -> RunEvent:
    """A ``GUARDRAIL_DECISION`` event for an agent input/output guardrail, also recorded as
    ``nexura.guardrail.decision`` on the run span (redacted: see ``decision_record``)."""
    record = decision_record(
        stage,
        name,
        action,
        info=info,
        latency_ms=(time.monotonic() - started) * 1000,
        agent=state.agent.name,
        error_type=error_type,
    )
    tracing.guardrail_decision(state.span, record)
    return RunEvent(type=RunEventType.GUARDRAIL_DECISION, run_id=state.run_id, data=record)


async def _agent_guardrail(state: _RunState, check: Any, value: Any) -> GuardrailResult:
    """Run one agent input/output guardrail within the run's timeout, stopping early with
    ``_RunCancelled`` when the run's CancelToken fires (as tool guardrails do)."""

    async def bounded() -> GuardrailResult:
        # The guardrail's coroutine is created only once it can run: never left unawaited
        # when the run is already cancelled or past its deadline.
        remaining = state.remaining()  # ModelTimeoutError once the deadline has passed
        work = run_guardrail(check, state.ctx, value)
        return await (asyncio.wait_for(work, timeout=remaining) if remaining else work)

    try:
        result: GuardrailResult = await _until_cancelled(bounded(), state.cancel)
    except TimeoutError as exc:
        elapsed = time.monotonic() - state.start_time
        limit = state.effective_timeout
        if limit is None or elapsed + 0.05 < limit:
            raise  # the guardrail's own TimeoutError, before the run's deadline: as before
        raise ModelTimeoutError(limit, elapsed) from exc  # the run's deadline passed
    return result


def _drain_nested_usage(state: _RunState) -> bool:
    """Count usage recorded with ``ctx.add_usage`` (judge calls in guardrails, validators).

    Only the run's total: ``usage_by_agent`` / ``usage_by_model`` cover the agents' own
    model calls, as for nested usage from tools. True when anything was added.
    """
    nested_usage = state.ctx.take_nested_usage()
    for nested in nested_usage:
        state.usage = state.usage.add(nested)
    state.ctx.usage = state.usage
    state.span.set_usage(state.usage)
    return bool(nested_usage)


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
    # Calls with an unknown outcome resolved by a person (resume outcomes=) or, during
    # recovery, already resolved in the execution journal (``journal_resolved``).
    outcomes: dict[str, ResolvedOutcome] = field(default_factory=dict)
    journal_resolved: set[str] = field(default_factory=set)
    # Calls to send again that need a fresh approval first (not applied, gated tool).
    deferred: set[str] = field(default_factory=set)
    # In-doubt calls a person approved to send again (journaled as not_applied first).
    resend: set[str] = field(default_factory=set)
    # Who decided (recorded on journal resolutions).
    actor: str = "sdk:resume"
    # The approval (interruption id) these decisions consume, and the calls it approved:
    # their intents carry it (A6 S11, design §2.7 "claim for dispatch").
    approval_id: str | None = None
    claimed: dict[str, str] = field(default_factory=dict)  # call id -> approval id
    # Journal-capable stores: the claim recorded in the run's journal (fenced) before the
    # approval is consumed, and the (interruption, envelope) still to consume.
    claim: dict[str, Any] | None = None
    consume: tuple[Any, str] | None = None
    # The calls the interruption asked about (this resume decided exactly these).
    pending_ids: set[str] = field(default_factory=set)


_UNKNOWN_REASON = "outcome_unknown_after_crash"
# A call whose earlier dispatch did not happen waits for a fresh approval to be sent again.
_REAPPROVE_REASON = "effect_not_applied"
_UNKNOWN_RESULT = (
    "The outcome of this action is unknown: the process stopped while it was running. "
    "It was not retried."
)


def _approval_claim(
    interruption: RunInterruption,
    decisions: _Decisions,
    reject: dict[str, str],
    outcomes: dict[str, Any],
    edits: dict[str, ApproveWith],
    actor: str,
) -> dict[str, Any] | None:
    """The decisions of a resume, as recorded in the run's journal before consumption.

    ``None`` when they cannot be stored exactly as JSON: a journaled resume then refuses
    them; one without a journal consumes the approval at once.
    """
    digests = interruption.pending_digests()
    claim = {
        "approval_id": interruption.interruption_id,
        "turn": getattr(interruption, "turn", None),
        "expires_at": interruption.expires_at,
        "approve": {
            call_id: {
                "digest": digests.get(call_id),
                "arguments": edits[call_id].arguments if call_id in edits else None,
            }
            for call_id in sorted(decisions.approved)
        },
        "reject": dict(reject),
        "answers": dict(decisions.answers),
        "outcomes": {
            call_id: {"result": o.result, "resolution": o.resolution, "reason": o.reason}
            for call_id, o in outcomes.items()
        },
        "actor": actor,
    }
    try:
        # Exactly representable as JSON (no tuples, int keys or NaN), or not recorded (m3).
        if json.loads(json.dumps(claim, allow_nan=False)) != claim:
            return None
    except (TypeError, ValueError):
        return None
    return claim


def _json_normal(value: Any) -> Any:
    """``value`` as plain JSON (tuples become lists); ``ValueError`` when it has none."""
    if isinstance(value, (list, tuple)):
        return [_json_normal(item) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("a mapping has a key that is not a string")
        return {key: _json_normal(item) for key, item in value.items()}
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("a number is not finite (NaN or infinity)")
        return value
    raise ValueError(f"a {type(value).__name__} is not JSON")


def _normalised_decisions(
    approve: Any, outcomes: dict[str, ResolvedOutcome]
) -> tuple[Any, dict[str, ResolvedOutcome]]:
    """Edits and outcome results as plain JSON where they have one (round 3, F3-B1).

    Tuples become lists, so what is recorded in the run's journal is what runs. Values
    without a JSON form are left as they are; a resume that must record its claim
    refuses them (see ``_approval_claim``).
    """
    if isinstance(approve, Mapping):
        normal: dict[str, Any] = {}
        for call_id, decision in approve.items():
            if isinstance(decision, ApproveWith):
                with contextlib.suppress(ValueError):
                    decision = dataclasses.replace(
                        decision, arguments=_json_normal(decision.arguments)
                    )
            normal[call_id] = decision
        approve = normal
    results: dict[str, ResolvedOutcome] = {}
    for call_id, outcome in outcomes.items():
        with contextlib.suppress(ValueError):
            outcome = dataclasses.replace(outcome, result=_json_normal(outcome.result))
        results[call_id] = outcome
    return approve, results


def _refuse_claimed(
    state: Mapping[str, Any], steps: Mapping[str, Any], status: str, interruption: Any
) -> None:
    """Raise when a resume may not use this approval on a run with this journal (M2)."""
    approval_id = interruption.interruption_id
    if status in ("completed", "failed"):
        raise ApprovalDecisionError("Interruption already consumed or expired; refusing replay.")
    used = any(
        isinstance(item, dict) and item.get("approval_id") == approval_id
        for item in state.get("consumed_approvals") or []
    ) or any(
        getattr(step, "intent", None) is not None and step.intent.approval_id == approval_id
        for step in steps.values()
    )
    if used:
        raise ApprovalDecisionError("Interruption already consumed or expired; refusing replay.")
    held = state.get("approval_claim")
    if isinstance(held, dict) and held.get("approval_id") == approval_id:
        raise ApprovalDecisionError(
            "This approval was claimed by an earlier resume that stopped before it "
            "dispatched anything; continue the run with recover()."
        )
    if getattr(interruption, "reason", None) not in (_UNKNOWN_REASON, _REAPPROVE_REASON):
        # Round 3 (F3-B1): an approval pause asks about calls that were never sent. A call
        # of its turn with an intent was sent under another approval: never twice.
        from nexura_agents._agent_journal import parse_call_step_key

        turn = getattr(interruption, "turn", None)
        pending = {c.id for c in getattr(interruption, "pending", [])}
        for key, step in steps.items():
            if getattr(step, "intent", None) is None:
                continue
            try:
                step_turn, call_id, _ = parse_call_step_key(str(key))
            except ValueError:
                continue
            if step_turn == turn and call_id in pending:
                raise ApprovalDecisionError(
                    f"Call '{call_id}' of this interruption was already dispatched; refusing "
                    "to approve it again."
                )


def _run_workspace(state: _RunState) -> str:
    """The execution workspace of a run (its journal's; ``default`` without one)."""
    return state.journal.eid.workspace_id if state.journal is not None else "default"


def _lease_token(state: _RunState) -> int | None:
    """The fencing token of the run lease this process holds (``None`` without one)."""
    return state.journal.fencing_token if state.journal is not None else None


def _model_frame(state: _RunState) -> tuple[str, str, str]:
    """Dispatch scope of this turn's model call: ``turn:<n>/model`` (pure: no intent)."""
    return _run_workspace(state), state.run_id, f"turn:{max(int(state.turn), 0)}/model"


def _trace_approval_requested(state: _RunState, interruption: Any, pending: list[ToolCall]) -> None:
    """``nexura.approval.requested`` on the run span (opt-in tracing; never raises)."""
    tracing.approval_requested(
        state.span,
        interruption.interruption_id,
        kind="tool_call",
        reason=interruption.pause_reason,
        tools=[c.name for c in pending],
    )


def _trace_approval_decided(span: Any, interruption: Any, decisions: _Decisions) -> None:
    """``nexura.approval.decided`` once the decisions passed every check."""
    approved = len(decisions.approved)
    declined = len(decisions.rejected) + len(decisions.unknown)
    tracing.approval_decided(
        span,
        interruption.interruption_id,
        kind="tool_call",
        decision=(
            "mixed"
            if approved and declined
            else "approve"
            if approved
            else "reject"
            if declined
            else "answer"
        ),
        approved_count=approved,
        rejected_count=declined,
        answered_count=len(decisions.answers),
        resolved_count=len(decisions.outcomes),
    )


def _warn_definition_changed(run_id: str, agent: Agent) -> None:
    warnings.warn(
        f"Agent '{agent.name}' differs from the definition run '{run_id}' started with "
        "(Agent.definition_digest(): tools, schemas, effect classes, instructions, handoffs "
        "or output type changed). Continuing; a later release refuses this with "
        "DefinitionMismatchError.",
        RuntimeWarning,
        stacklevel=3,
    )


_EFFECT_STRICTNESS = ("pure", "idempotent", "reconcilable", "non_idempotent")


def _stricter_effect(persisted: Any, current: Any, tool: Tool) -> Any:
    """The stricter of the persisted intent's and the tool's effect class; a reconcilable
    class without a ``reconcile`` function on the tool counts as non_idempotent."""
    from nexura_agents.contracts.execution import EffectClass

    order = _EFFECT_STRICTNESS
    chosen = max(EffectClass(persisted), EffectClass(current), key=lambda e: order.index(e.value))
    if chosen is EffectClass.RECONCILABLE and tool.reconcile is None:
        return EffectClass.NON_IDEMPOTENT
    return chosen


def _result_text(tool: Tool | None, result: Any) -> str:
    """A person's or a reconcile function's result as the tool result text."""
    if isinstance(result, str):
        return result
    if tool is not None:
        return tool._serialize_result(result)
    return json.dumps(result, ensure_ascii=False, default=str)


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
    # A terminal sweep whose lease check failed twice: (lease token, time), run after the
    # terminal commit succeeds (A6 S11, F4-m1).
    deferred_sweep: tuple[int | None, float] | None = None
    checkpoint_owner: str | None = None  # recovery lease holder; fences checkpoint writes
    checkpoint_interruption_id: str | None = None
    checkpoint_expires_at: float | None = None
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
    # Durable runs with a journal-capable store: the run's lease and effect journal.
    journal: AgentJournal | None = None
    # tool_call_id -> step key of a dispatched call whose outcome is not committed yet.
    dispatch_keys: dict[str, StepKey] = field(default_factory=dict)
    # tool_call_id -> step key of a call whose outcome is unknown after a crash.
    in_doubt: dict[str, StepKey] = field(default_factory=dict)
    # tool_call_id -> GUARDRAIL_DECISION events of that call's tool guardrails, yielded
    # with its result.
    guard_events: dict[str, list[RunEvent]] = field(default_factory=dict)
    # Index in messages of a final answer that has not passed its output validators and
    # guardrails yet; a cancelled run drops it from the messages it returns.
    unchecked_answer: int | None = None
    # The run's hooks: inherited, process default and the runner's own (P39 G2).
    hooks: AgentHooks | None = None
    # Hook approval gates of this turn ("turn:<n>/call:<id>"); with a journal they are
    # also in its state ("hook_gates") and survive a crash (P39 G2, MIN7).
    hook_gates: set[str] = field(default_factory=set)

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
        hooks: AgentHooks | Sequence[AgentHooks] | None = None,
        usage_limits: UsageLimits | None = None,
        model_settings: ModelSettings | None = None,
        budget: Any | None = None,
        max_handoffs: int | None = None,
        checkpoint_store: RunCheckpointStore | None = None,
        approval_ttl: float = 3600.0,
        approval_signing_secret: str | bytes | None = None,
        approval_store: ApprovalStore | None = None,
        lease_ttl: float = 30.0,
        default_hooks: bool = True,
    ) -> None:
        if not isinstance(default_hooks, bool):
            raise SDKValidationError("default_hooks must be True or False.")
        # False leaves out the process default hooks (set_default_hooks). For tests only:
        # hooks inherited from an enclosing run still apply, and a locked default refuses it.
        policy.check_opt_out(default_hooks)
        self.use_default_hooks = default_hooks
        if (
            isinstance(lease_ttl, bool)
            or not isinstance(lease_ttl, (int, float))
            or not math.isfinite(lease_ttl)
            or lease_ttl <= 0
        ):
            raise SDKValidationError("lease_ttl must be a finite positive number of seconds.")
        # Durable runs: the run lease lasts lease_ttl seconds and is renewed every third of it.
        self.lease_ttl = float(lease_ttl)
        if (
            isinstance(approval_ttl, bool)
            or not isinstance(approval_ttl, (int, float))
            or not math.isfinite(approval_ttl)
            or approval_ttl <= 0
        ):
            raise SDKValidationError("approval_ttl must be a finite positive number of seconds.")
        if approval_signing_secret is not None and (
            not isinstance(approval_signing_secret, (str, bytes)) or not approval_signing_secret
        ):
            raise SDKValidationError("approval_signing_secret must be nonempty str or bytes.")
        self.approval_ttl = float(approval_ttl)
        self._approval_secret = (
            approval_signing_secret.encode("utf-8")
            if isinstance(approval_signing_secret, str)
            else approval_signing_secret
        )
        self.approval_store: Any = (
            approval_store if approval_store is not None else checkpoint_store
        )
        # Convenience for same-runner approval. This trusted copy never comes from the client.
        self._approval_snapshots: OrderedDict[str, str] = OrderedDict()
        self._approval_snapshot_expiries: dict[str, float] = {}
        if max_handoffs is not None and (
            not isinstance(max_handoffs, int) or isinstance(max_handoffs, bool) or max_handoffs < 0
        ):
            raise SDKValidationError(f"max_handoffs must be an integer >= 0, got {max_handoffs!r}.")
        # Overrides ``Agent.max_handoffs`` of the starting agent when set.
        self.max_handoffs = max_handoffs
        self.session_store = session_store
        # Durable runs: when set, the run is checkpointed and can be recovered (see durable.py).
        self.checkpoint_store = checkpoint_store
        if checkpoint_store is not None and not callable(getattr(checkpoint_store, "claim", None)):
            warnings.warn(
                f"{type(checkpoint_store).__name__} has no claim(run_id, owner, lease_seconds): "
                "recovery cannot be made exclusive and the run holds no lease. Checkpoint "
                "stores without claim are deprecated and will not be accepted in 0.6; use "
                "MemoryCheckpointStore, SQLiteCheckpointStore or nexura_postgres."
                "PostgresCheckpointStore, or implement claim/release and save(owner=...).",
                DeprecationWarning,
                stacklevel=2,
            )
        # One hooks object, or a list composed in order (see ComposedHooks).
        self.hooks = hooks
        self.usage_limits = usage_limits
        self.model_settings = model_settings
        # A usage RunBudget; provider models admit and settle every call against it.
        self._configured_budget = budget

    @property
    def hooks(self) -> AgentHooks | None:
        """This runner's own hooks: the object given, a ``ComposedHooks`` for a list, or
        ``None``. A run uses them after the inherited and default hooks
        (see :meth:`effective_hooks`)."""
        return self._hooks

    @hooks.setter
    def hooks(self, value: AgentHooks | Sequence[AgentHooks] | None) -> None:
        self._hooks = _compose_hooks(value)

    def effective_hooks(self) -> AgentHooks | None:
        """The hooks a run started now would use: the hooks inherited from an enclosing
        run, the process default (``set_default_hooks``; left out with
        ``Runner(default_hooks=False)``), then this runner's own ``hooks``, each object
        once. ``hooks=`` adds to the default; it never replaces it."""
        return policy.effective_hooks(self._hooks, use_default=self.use_default_hooks)

    def _scoped(self, stream: AsyncGenerator[RunEvent, None]) -> AsyncGenerator[RunEvent, None]:
        """The run's event stream with its hooks active (inherited by nested runs) and
        inside its budget."""
        return admission.budget_stream(
            policy.hooks_stream(stream, self.effective_hooks()), self.budget
        )

    def _needs_approval(self, state: _RunState, tool: Tool, call: ToolCall) -> bool:
        """Whether a call waits for human approval: the tool's ``requires_approval`` flag or
        rule, or a hook gate recorded for it in the run's journal, or else any hook's
        ``tool_needs_approval``. Hooks add gates; they cannot remove the tool's own (it is
        checked first and wins), and a gate a hook added stays after a crash (P39 G2,
        MIN7): it is recorded with the run, so a recovery or resume without that hook
        still waits for a person."""
        if tool.needs_approval(state.ctx, call.arguments):
            return True
        key = _gate_key(state.turn, call.id)
        if key in _recorded_gates(state):
            return True
        hooks = state.hooks
        if hooks is None:
            return False
        ctx = state.ctx.for_tool(
            call.id, call.name, state.tool_retries.get(call.name, 0), tool=tool.info()
        )
        # Copies: a hook cannot change the call or the tool (for example lift its gate).
        if not approval_vote(hooks, ctx, copy.copy(tool), copy.deepcopy(call.arguments)):
            return False
        _record_gate(state, key)
        return True

    @property
    def budget(self) -> Any:
        """The configured budget, or the inherited active allowance when unset."""
        return (
            self._configured_budget
            if self._configured_budget is not None
            else admission.active_budget.get()
        )

    @budget.setter
    def budget(self, value: Any) -> None:
        self._configured_budget = value

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
        outcomes: Mapping[str, ResolvedOutcome] | None = None,
        actor: str | None = None,
    ) -> RunResult[OutputT]:
        """Continue a paused run once every pending tool call has a decision.

        ``approve`` is ``True`` (approve everything not in ``reject``), a list of call IDs,
        or a mapping of call ID -> the action digest the reviewer saw
        (``interruption.pending_digests()``) so approval binds to the exact arguments.
        Approved calls execute; rejected calls are reported to the model with the reason.
        True/list approvals and digest-free ApproveWith require a verified signed or
        stored copy. SDK-issued in-memory interruptions carry a trusted snapshot across
        runners; JSON omits it, while the originating Runner retains its own copy.
        Fresh unsigned JSON resumes need reviewed digests. Interruptions expire after
        approval_ttl. Without an approval/checkpoint store, single use is the caller's
        responsibility; signatures provide authenticity, not replay prevention.

        When ``interruption.pause_reason == "effect_unknown"`` (calls that may have run
        before a crash), ``outcomes={call_id: ResolvedOutcome(result)}`` records that the
        action happened (``result`` is the tool result), ``ResolvedOutcome.not_applied()``
        sends it again (a tool that requires approval pauses for a fresh approval first) and
        ``ResolvedOutcome.failed(reason)`` fails the run. ``approve`` sends it again now
        (journaled as ``not_applied``) and ``reject`` tells the model the outcome is unknown.
        ``actor`` names who decided; it is recorded on journal resolutions (default
        ``"sdk:resume"``).
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
                outcomes=outcomes,
                actor=actor,
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
        return self._scoped(stream)

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
            hooks=policy.active_hooks.get(),  # this run's hooks (set by _scoped)
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

        hooks = state.hooks or _NO_HOOKS
        await hooks.on_run_start(ctx)
        await _hook(hooks, "on_agent_start", ctx, agent)
        yield RunEvent(
            type=RunEventType.AGENT_STARTED,
            run_id=run_id,
            data={"agent": agent.name, "path": [agent.name]},
        )

        # Input guardrails see the user's latest message before any model call. Each one is
        # bounded by the run's timeout and stops when the run is cancelled.
        user_text = next((m.content or "" for m in reversed(messages) if m.role == "user"), "")
        for check in agent.input_guardrails:
            name = getattr(check, "__name__", "guardrail")
            started = time.monotonic()
            try:
                outcome = await _agent_guardrail(state, check, user_text)
            except _RunCancelled:
                _drain_nested_usage(state)  # judges that finished before the cancel count
                yield _cancelled(state)
                return
            except Exception as exc:
                _drain_nested_usage(state)
                yield _decision_event(
                    state, "input", name, "fail", started, error_type=type(exc).__name__
                )
                if isinstance(exc, ModelTimeoutError):
                    yield _failed(run_id, exc)
                raise  # any other error propagates as before
            if outcome.replacement is not None:
                _drain_nested_usage(state)
                bad = TypeError(
                    f"Input guardrail '{name}' returned rewritten(); only output guardrails "
                    "can rewrite."
                )
                yield _decision_event(state, "input", name, "fail", started, error_type="TypeError")
                yield _failed(run_id, bad)
                raise bad
            if outcome.tripwire:
                _drain_nested_usage(state)  # the judge that tripped cost money too
                tripped = GuardrailTripwireTriggered(name, "input", outcome.info)
                yield _decision_event(state, "input", name, "fail", started, info=outcome.info)
                yield _failed(run_id, tripped)
                raise tripped
        # Model calls made inside the guardrails (a judge, ctx.add_usage) count now, and
        # against UsageLimits before any model call.
        if _drain_nested_usage(state) and self.usage_limits is not None:
            try:
                self.usage_limits.check(state.requests, state.usage)
            except NexuraError as limit_err:
                yield _failed(run_id, limit_err)
                raise

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
        outcomes: Mapping[str, ResolvedOutcome] | None = None,
        actor: str | None = None,
    ) -> AsyncGenerator[RunEvent, None]:
        """Event-stream form of :meth:`resume_async`."""
        kwargs: dict[str, Any] = {
            "cancel": cancel,
            "approve": approve,
            "reject": reject,
            "answers": answers,
            "outcomes": outcomes,
            "actor": actor,
            "deps": deps,
            "timeout": timeout,
            "session_store": session_store,
            "stream_text": stream_text,
        }
        if tracing._STATE is None:
            return self._scoped(self._resume_stream_impl(agent, interruption, **kwargs))
        return self._scoped(
            self._traced(
                tracing.start_run,
                agent.name,
                True,
                self._resume_stream_impl,
                agent,
                interruption,
                **kwargs,
            )
        )

    async def _resume_stream_impl(
        self,
        agent: Agent,
        interruption: RunInterruption,
        *,
        _reclaim: tuple[AgentJournal, str | None] | None = None,
        approve: bool | Iterable[str] | Mapping[str, str | ApproveWith] = (),
        reject: Mapping[str, str] | None = None,
        answers: Mapping[str, str] | None = None,
        deps: Any = None,
        timeout: float | None = None,
        session_store: SessionStore | None = None,
        stream_text: bool = False,
        cancel: CancelToken | None = None,
        outcomes: Mapping[str, ResolvedOutcome] | None = None,
        actor: str | None = None,
        _span: Any = tracing.NOOP,
    ) -> AsyncGenerator[RunEvent, None]:
        from nexura_agents.durable import ResolvedOutcome

        _validate_timeout(timeout)
        if actor is None:
            actor = "sdk:resume"
        elif not isinstance(actor, str) or not actor.strip():
            raise ApprovalDecisionError("actor must be a nonempty string naming who decided.")
        # Detach before awaiting storage: a client must not mutate arguments during admission.
        interruption = copy.deepcopy(interruption)
        resolved = dict(outcomes or {})
        if resolved:
            if interruption.reason != _UNKNOWN_REASON:
                raise ApprovalDecisionError(
                    "outcomes= resolves calls whose outcome is unknown after a crash; this "
                    "interruption is waiting for approval."
                )
            for call_id, value in resolved.items():
                if not isinstance(value, ResolvedOutcome):
                    raise ApprovalDecisionError(
                        f"outcomes['{call_id}'] must be a ResolvedOutcome, not "
                        f"{type(value).__name__}."
                    )
        # What is recorded in the run's journal is what runs: plain JSON (F3-B1).
        approve, resolved = _normalised_decisions(approve, resolved)
        if interruption.agent_name != agent.name:
            raise ApprovalDecisionError(
                f"Interruption belongs to agent '{interruption.agent_name}', not '{agent.name}'."
            )
        # Only calls to the agent's built-in ask_user tool are questions; a user tool that
        # happens to be named "ask_user" is approved or rejected like any other.
        active = _resolve_active_agent(agent, interruption)
        envelope, authenticated = await self._verify_approval(interruption)
        if interruption.action_digests and (
            interruption.action_digests != interruption.pending_digests()
        ):
            raise ApprovalDecisionError(
                "Interruption action digest differs from the pending action; "
                "the arguments changed after review."
            )
        questions = {
            c.id
            for c in interruption.pending
            if getattr(active.tools_by_name.get(c.name), "asks_user", False)
        }
        answers = dict(answers or {})
        approve_all = approve is True
        if approve is True:
            # Approve everything still pending, bound to the actions in this interruption.
            # Questions are answered, never approved (running ask_user would mean no answer).
            approve = {
                call_id: digest
                for call_id, digest in interruption.pending_digests().items()
                if call_id not in (reject or {})
                and call_id not in questions
                and call_id not in resolved
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
        overlap = set(resolved) & (decisions.approved | set(decisions.rejected) | set(answers))
        if overlap:
            raise ApprovalDecisionError(
                f"Give each call one decision; {sorted(overlap)} also have an outcome."
            )
        decisions.outcomes = resolved
        if isinstance(approve, Mapping):
            expected = interruption.pending_digests()
            for call_id, digest in approve.items():
                if isinstance(digest, ApproveWith):
                    if digest.digest is None:
                        if not authenticated:
                            raise ApprovalDecisionError(
                                "Pass the reviewed action digest for ApproveWith, or use a "
                                "signed/stored interruption."
                            )
                        continue
                    digest = digest.digest
                if expected.get(call_id) != digest:
                    raise ApprovalDecisionError(
                        f"Approval for '{call_id}' does not match the pending action; "
                        "the arguments changed after review."
                    )
        decided = decisions.approved | set(decisions.rejected) | set(answers) | set(resolved)
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
        if interruption.reason == _UNKNOWN_REASON:
            # Calls that may already have run before a crash: a rejection tells the model the
            # outcome is unknown instead of claiming a human refused.
            decisions.unknown, decisions.rejected = set(decisions.rejected), {}
            # Approving one is the decision to send it again: journal it as not applied.
            decisions.resend = set(decisions.approved)
        decisions.actor = actor
        _span.set("nexura.run_id", interruption.run_id)
        _set_run_attributes(_span, state.ctx, state.session_id)
        if root.handoffs:
            _span.set(tracing.AGENT_PATH, list(state.path))
            _span.set(tracing.LAST_AGENT, agent.name)
        hooks = state.hooks or _NO_HOOKS
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
        if decisions.approved and not isinstance(approve, Mapping) and not authenticated:
            raise ApprovalDecisionError(
                "Pass approve={call_id: reviewed_digest}, or use a signed/stored interruption."
            )
        # approve=True is normalized to a mapping above; it must not self-certify its digests.
        if approve_all and decisions.approved and not authenticated:
            raise ApprovalDecisionError(
                "approve=True requires a signed/stored interruption; pass reviewed digests."
            )
        decisions.approval_id = interruption.interruption_id
        decisions.claimed = {
            call_id: interruption.interruption_id for call_id in decisions.approved
        }
        decisions.pending_ids = {c.id for c in interruption.pending}
        if _reclaim is not None:
            # Recovery of a claim a stopped resume recorded (A6 S11): the run is ours under
            # the journal lease, the approval was checked again (digests, expiry) above.
            state.journal, state.checkpoint_owner = _reclaim
            if await self._reconsume_approval(interruption, envelope):
                # The stopped resume never consumed it, so it recorded no decision event.
                _trace_approval_decided(_span, interruption, decisions)
        else:
            claim = _approval_claim(
                interruption, decisions, dict(reject or {}), resolved, edits, actor
            )
            from nexura_agents._agent_journal import journal_capable

            if journal_capable(self.checkpoint_store):
                if claim is None:
                    # One approval path (round 3, F3-B1): the claim must record exactly what
                    # runs. Refused before the run is taken or anything is written.
                    raise ApprovalDecisionError(
                        "Approval decisions must be representable as JSON to be recorded "
                        "with the run: ApproveWith arguments, rejection reasons, answers and "
                        "ResolvedOutcome results may hold only strings, numbers (finite), "
                        "booleans, None, lists and string-keyed mappings. Nothing was "
                        "consumed; resume again with JSON values."
                    )
                # Consumed only after the claim is in the run's journal (see _durable_loop).
                _check_approval_expiry(interruption)
                decisions.claim, decisions.consume = claim, (interruption, envelope)
            else:
                await self._consume_approval(interruption, envelope)
                _trace_approval_decided(_span, interruption, decisions)
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
            events = self._swept(
                state, self._loop_body(state, store, hooks, stream_text, resume_batch)
            )
        else:
            events = self._durable_loop(state, store, hooks, stream_text, resume_batch)
        # A6 S11 (review M4): every reservation the run makes is linked to it, also those
        # outside a model or tool call (history processors, instructions, guardrails).
        # Model and tool calls open their own scope, which replaces this one.
        return linkage.scoped_stream(
            events,
            self._planned_workspace(state),
            state.run_id,
            "run",
            fencing_token=lambda: _lease_token(state),
        )

    def _planned_workspace(self, state: _RunState) -> str:
        """The execution workspace of a run, also before its journal is opened."""
        from nexura_agents._agent_journal import journal_capable

        if state.journal is not None:
            return state.journal.eid.workspace_id
        if journal_capable(self.checkpoint_store):
            return str(getattr(self.checkpoint_store, "workspace_id", None) or "default")
        return "default"

    async def _swept(
        self, state: _RunState, events: AsyncGenerator[RunEvent, None]
    ) -> AsyncGenerator[RunEvent, None]:
        """A run without a checkpoint store: end its open reservations when it finishes."""
        try:
            async for event in events:
                if event.type == RunEventType.RUN_COMPLETED:
                    await self._settle_reservations(state, "terminal")
                yield event
        except Exception:
            await self._settle_reservations(state, "terminal")
            raise

    async def _settle_reservations(
        self,
        state: _RunState,
        context: str,
        *,
        proven: tuple[int | None, float] | None = None,
    ) -> None:
        """End the run's open budget reservations (design §2.7, A6 S11).

        On a takeover (``context="takeover"``), before any step runs: errors propagate, so
        nothing is dispatched while the ledger cannot be read. When the run reaches a
        terminal status (``"terminal"``) an error is reported as a warning only.
        """
        from nexura_agents.usage.recovery import settle_run_reservations_async

        budgets = (self._configured_budget, admission.active_budget.get())
        if all(b is None for b in budgets):
            return
        journal = state.journal
        max_token: int | None = None
        confirmed_at: float | None = None
        if proven is not None:
            # After a successful fenced terminal commit (round 4, F4-m1): the commit proved
            # this owner held the run under this token up to that moment.
            max_token, confirmed_at = proven
        elif context == "terminal" and journal is not None:
            # Fenced (round 2, R2-M1): renew the lease first, so a stale owner stops here,
            # and end only reservations made under a lease token no newer than ours.
            from nexura_agents.contracts.execution import LeaseLostError

            if journal.lease is None:
                return
            confirmed = False
            for attempt in (1, 2):  # a transient store error is retried once (F4-m1)
                try:
                    await journal.renew()
                    confirmed = True
                    break
                except LeaseLostError:
                    return
                except Exception as exc:
                    # A transient store error must not fail a finished run (round 3).
                    warnings.warn(
                        f"Run '{state.run_id}' finished, but its run lease could not be "
                        f"confirmed ({exc}); "
                        + (
                            "retrying once."
                            if attempt == 1
                            else "its open budget reservations are settled after the "
                            "terminal commit, or left for recovery if that fails."
                        ),
                        RuntimeWarning,
                        stacklevel=2,
                    )
            if not confirmed:
                # The fenced terminal commit that follows proves the lease; the same
                # token-filtered sweep then runs after it (see _settle_deferred).
                state.deferred_sweep = (journal.fencing_token, time.time())
                return
            max_token = journal.fencing_token
            # A newer owner can reserve only after this lease expires (at least a ttl from
            # now); the residual is an owner frozen here for longer than its ttl together
            # with host clock skew larger than the ttl.
            confirmed_at = time.time()
        from nexura_agents.usage.recovery import budget_scope

        workspace = _run_workspace(state)
        # Unlinked reservations of a budget scope named after this run are its own (the
        # service convention); a terminal sweep ends those older than its lease check.
        own_scope = any(
            budget_scope(b) == {"workspace_id": workspace, "run_id": state.run_id}
            for b in admission._budgets(admission.combine_budgets(*budgets))
        )
        try:
            await settle_run_reservations_async(
                budgets,
                workspace_id=workspace,
                run_id=state.run_id,
                steps=journal.steps if journal is not None else None,
                include_unlinked=own_scope,
                context=context,
                max_fencing_token=max_token,
                unlinked_before=confirmed_at,
            )
        except Exception as exc:
            if context != "terminal":
                raise
            warnings.warn(
                f"Run '{state.run_id}' finished, but its open budget reservations could not "
                f"be settled: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )

    async def _settle_deferred(self, state: _RunState, journal: AgentJournal | None) -> None:
        """Run a deferred terminal sweep once the fenced terminal commit has succeeded.

        If that commit failed too (the journal is not terminal), the reservations stay
        open and recovery ends them (round 4, F4-m1).
        """
        deferred, state.deferred_sweep = state.deferred_sweep, None
        if deferred is None or journal is None or journal.status not in ("completed", "failed"):
            return
        await self._settle_reservations(state, "terminal", proven=deferred)

    def _check_budget_scope(self, run_id: str, journal_state: Mapping[str, Any] | None) -> None:
        """A run started under a ``RunBudget`` is continued only under a budget (fail closed).

        Called with the journal state read before the run is taken, so a refusal writes
        nothing (round 2, minor 3).
        """
        from nexura_agents.usage.errors import UsageError
        from nexura_agents.usage.recovery import budget_scope

        recorded = journal_state.get("budget_scope") if journal_state is not None else None
        if not isinstance(recorded, dict):
            return
        budget = self.budget
        if budget is None:
            raise UsageError(
                f"Budget admission refused: run '{run_id}' was started under budget scope "
                f"'{recorded.get('run_id')}' (workspace '{recorded.get('workspace_id')}'); "
                "continue it with Runner(budget=manager.get_run_budget(...)) for that scope."
            )
        # Look through combined budgets (round 2, minor 1): the recorded scope must be one
        # of the attached allowances, or the run continues without its policy.
        scopes = [b for b in map(budget_scope, admission._budgets(budget)) if b is not None]
        if recorded not in scopes:
            under = (
                ", ".join(str(sc) for sc in scopes)
                if scopes
                else "a budget without a RunBudget scope"
            )
            warnings.warn(
                f"Run '{run_id}' was started under budget scope {recorded}; it continues "
                f"under {under}, without the recorded scope's policy.",
                RuntimeWarning,
                stacklevel=3,
            )

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
        With a journal-capable store the run holds a fenced lease from its first write
        (renewed by a heartbeat) and gives it back when this generator ends.
        """
        from nexura_agents._agent_journal import AgentJournal, journal_capable
        from nexura_agents.contracts.execution import LeaseLostError
        from nexura_agents.durable import new_lease_owner

        assert self.checkpoint_store is not None
        journal = state.journal
        if journal is None and journal_capable(self.checkpoint_store):
            # The original run, or resume: take the run before its first write.
            from nexura_agents.usage.recovery import budget_scope

            journal = await AgentJournal.open(self.checkpoint_store, state.run_id, self.lease_ttl)
            if resume_batch is not None:
                # Before any journal write: a missing budget is refused here.
                stored = await journal.peek()
                self._check_budget_scope(
                    state.run_id, stored.run.state if stored is not None else None
                )
            pending = resume_batch[1].consume if resume_batch is not None else None
            if pending is not None:
                # Before any journal write: a spent or claimed approval is refused here.
                await self._refuse_spent_approval(journal, pending[0])
            try:
                await journal.begin(
                    new_lease_owner(),
                    state.root.definition_digest(),
                    state.root.name,
                    # resume of a run checkpointed before it had a journal (H1)
                    pre_journal_turn=state.turn if resume_batch is not None else None,
                    budget_scope=budget_scope(self._configured_budget),
                )
            except Exception:
                if pending is not None:
                    # The run cannot be taken: the approval is not consumed (review M1). A
                    # spent, claimed or expired one is reported as such; otherwise the
                    # approval stays usable and the error says the run is busy.
                    await self._refuse_spent_approval(journal, pending[0])
                raise
            if journal.stored_digest != state.root.definition_digest():
                _warn_definition_changed(state.run_id, state.root)
            state.journal = journal
            journal.trace_span = state.span  # nexura.effect.* events go on the run span
            state.checkpoint_owner = journal.owner
        try:
            if journal is not None:
                if not isinstance(state.cancel, _RunStopToken):
                    state.cancel = _RunStopToken(state.cancel)
                journal.on_lost = state.cancel.stop_for_lost_lease
                if journal.lost is not None:
                    state.cancel.stop_for_lost_lease()
                if resume_batch is not None:
                    # Calls of the batch this resume did not decide (B1): an earlier claim's
                    # rejections and answers hold; an undecided gated call is asked again.
                    self._guard_resumed_batch(state, resume_batch)
                if resume_batch is not None and resume_batch[1].consume is not None:
                    await self._claim_approval(state, journal, resume_batch[1])
                await journal.ensure_running()
                journal.start_heartbeat()
            try:
                async for event in self._loop_body(state, store, hooks, stream_text, resume_batch):
                    if event.type == RunEventType.RUN_COMPLETED:
                        # Every step is finished: end open reservations before the terminal
                        # commit, so a crash between the two cannot leak them (review m1).
                        await self._settle_reservations(state, "terminal")
                        # The run is done and persisted: a failing delete must not turn it
                        # into an error. A stale "completed" checkpoint is finished by recover.
                        await self._delete_checkpoint(
                            state.run_id, state.checkpoint_owner, journal, "completed"
                        )
                        await self._settle_deferred(state, journal)
                    yield event
            except Exception as exc:
                if isinstance(exc, LeaseLostError):
                    state.checkpoint_store_failed = True  # another worker owns the run now
                # A storage error is not a run failure: the previous checkpoint stays the truth.
                if not state.checkpoint_store_failed:
                    await self._settle_reservations(state, "terminal")
                    await self._delete_checkpoint(
                        state.run_id, state.checkpoint_owner, journal, "failed"
                    )
                    await self._settle_deferred(state, journal)
                raise
        finally:
            if journal is not None:
                await journal.close()

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
                if decisions.outcomes or decisions.resend:
                    async for event in self._apply_outcomes(state, resumed_agent, calls, decisions):
                        yield event
                run_now = [c for c in calls if c.id not in decisions.deferred]
                async for event in self._run_tools(resumed_agent, state, hooks, run_now, decisions):
                    yield event
                if decisions.deferred:
                    async for event in self._pause_for_reapproval(state, calls, decisions):
                        yield event
                    return
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
                state.unchecked_answer = None  # an earlier answer was finished or retried
                if state.cancel is not None and state.cancel.cancelled:
                    _raise_if_lease_lost(state)
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
                tracing.record_prompt(chat, messages)  # opt-in content only
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
                tracing.record_response(chat, response)
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
                    # Not returned by a cancelled run until its validators and output
                    # guardrails have passed (see the _RunCancelled handler below).
                    state.unchecked_answer = len(messages) - 1
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
                    and self._needs_approval(state, tool, tc)
                ]
                if pending:
                    for waiting in pending:
                        pause_span = tracing.start_tool(state.span, waiting, pending_approval=True)
                        pause_span.content(
                            "gen_ai.tool.call.arguments",
                            "gen_ai.tool.call.arguments",
                            waiting.arguments,
                        )
                        tracing.record_tool_content(
                            pause_span, tracing.TOOL_CALL_ARGUMENTS, waiting.arguments
                        )
                        pause_span.end()
                    # The whole batch waits; on resume the other calls run with the approved ones.
                    interruption = self._make_interruption(state, pending, copy_messages=True)
                    await self._remember_interruption(interruption)
                    _trace_approval_requested(state, interruption, pending)
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
            _raise_if_lease_lost(state)
            unchecked = state.unchecked_answer
            if (
                unchecked is not None
                and unchecked == len(state.messages) - 1
                and state.messages[unchecked].role == "assistant"
                and not state.messages[unchecked].tool_calls
            ):
                # A final answer interrupted in its output validators or guardrails never
                # passed them: the cancelled result does not return it.
                state.messages.pop()
            state.unchecked_answer = None
            _drain_nested_usage(state)  # guardrail judges that finished before the cancel
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
                legacy_stream = linkage.scoped_stream(
                    admission.stream(
                        model, snapshot, tools_schema, timeout=remaining_timeout, **stream_extra
                    ),
                    *_model_frame(state),
                    fencing_token=_lease_token(state),
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
            call = admission.generate(
                model, messages=snapshot, tools=tools_schema, timeout=remaining_timeout, **extra
            )
            scope = linkage.enter_dispatch_scope(
                *_model_frame(state), fencing_token=_lease_token(state)
            )
            try:
                with chat.activated():
                    if remaining_timeout is not None:
                        holder.append(
                            await _until_cancelled(
                                asyncio.wait_for(call, timeout=remaining_timeout), state.cancel
                            )
                        )
                    else:
                        holder.append(await _until_cancelled(call, state.cancel))
            finally:
                linkage.leave_dispatch_scope(scope)
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
            call = admission.generate(model, request, context=exec_ctx)
            scope = linkage.enter_dispatch_scope(
                *_model_frame(state), fencing_token=_lease_token(state)
            )
            try:
                with chat.activated():
                    if remaining_timeout is not None:
                        holder.append(
                            await _until_cancelled(
                                asyncio.wait_for(call, timeout=remaining_timeout), state.cancel
                            )
                        )
                    else:
                        holder.append(await _until_cancelled(call, state.cancel))
            finally:
                linkage.leave_dispatch_scope(scope)
            return

        stream = linkage.scoped_stream(
            admission.stream(model, request, context=exec_ctx),
            *_model_frame(state),
            fencing_token=_lease_token(state),
        )
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
            name = getattr(check, "__name__", "guardrail")
            started = time.monotonic()
            try:
                outcome = await _agent_guardrail(state, check, output)
            except _RunCancelled:
                raise  # the loop drops the unchecked answer and counts finished judges
            except Exception as exc:
                _drain_nested_usage(state)
                yield _decision_event(
                    state, "output", name, "fail", started, error_type=type(exc).__name__
                )
                if isinstance(exc, ModelTimeoutError):
                    yield _failed(run_id, exc)
                raise  # any other error propagates as before
            if outcome.tripwire:
                _drain_nested_usage(state)  # the judge that tripped cost money too
                tripped = GuardrailTripwireTriggered(name, "output", outcome.info)
                yield _decision_event(state, "output", name, "fail", started, info=outcome.info)
                yield _failed(run_id, tripped)
                raise tripped
            if outcome.replacement is not None:
                if agent.output_type is not None:
                    bad = TypeError(
                        "An output guardrail returned a rewritten text for an "
                        "agent with output_type; rewriting supports text only."
                    )
                    _drain_nested_usage(state)
                    yield _decision_event(
                        state, "output", name, "fail", started, error_type="TypeError"
                    )
                    yield _failed(run_id, bad)
                    raise bad
                yield _decision_event(state, "output", name, "scrub", started, info=outcome.info)
                final_text = output = outcome.replacement
                if not from_tool:
                    # Keep the stored history in step with what the caller gets.
                    messages[-1] = dataclasses.replace(messages[-1], content=final_text)
        state.unchecked_answer = None  # passed its validators and output guardrails
        # Usage recorded since the last model turn (judge calls in output guardrails,
        # output validators) is part of the run's usage, and is checked against
        # UsageLimits like a model response: past a hard cap the run fails.
        if _drain_nested_usage(state) and self.usage_limits is not None:
            try:
                self.usage_limits.check(state.requests, state.usage)
            except NexuraError as limit_err:
                yield _failed(run_id, limit_err)
                raise

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

    async def _apply_outcomes(
        self, state: _RunState, agent: Agent, calls: list[ToolCall], decisions: _Decisions
    ) -> AsyncGenerator[RunEvent, None]:
        """Record resolved outcomes of in-doubt calls before the batch continues.

        Confirmed: the result becomes the tool result. Not applied: the call is sent again
        as a new attempt, after a fresh approval when its tool requires one. Failed: the
        run fails before anything else in the batch runs.
        """
        from nexura_agents.durable import ResolvedOutcome
        from nexura_agents.execution_store import EffectAlreadyResolvedError

        journal = state.journal
        failed: list[tuple[ToolCall, ResolvedOutcome]] = []
        for call in calls:
            outcome = decisions.outcomes.get(call.id)
            if outcome is None and call.id in decisions.resend:
                outcome = ResolvedOutcome.not_applied()  # approved: sent again now
            if outcome is None:
                continue
            tool = _callable_tool(agent, state, call.name)
            text = _result_text(tool, outcome.result) if outcome.resolution == "confirmed" else None
            key: StepKey | None = None
            if journal is not None:
                _, key, step = journal.latest(state.turn, call.id)
                if step is None or step.intent is None:
                    key = None
                elif call.id not in decisions.journal_resolved:
                    try:
                        await journal.resolve(
                            key,
                            outcome.resolution,
                            actor=decisions.actor,
                            result=text if text is not None else outcome.reason or None,
                        )
                    except EffectAlreadyResolvedError as exc:
                        bad = ApprovalDecisionError(
                            f"Outcome of '{call.id}' was already resolved differently: {exc}"
                        )
                        yield _failed(state.run_id, bad)
                        raise bad from exc
            if outcome.resolution == "confirmed":
                assert text is not None
                decisions.answers[call.id] = text
                if key is not None:
                    state.dispatch_keys[call.id] = key
            elif outcome.resolution == "failed":
                failed.append((call, outcome))
            elif call.id in decisions.resend:
                pass  # approved in this resume: that is its fresh approval
            elif tool is not None and self._needs_approval(state, tool, call):
                decisions.deferred.add(call.id)  # not applied: send again after approval
        if failed:
            call, outcome = failed[0]
            err = ToolExecutionError(
                tool_name=call.name,
                original_error=RuntimeError(
                    f"tool call '{call.id}' was resolved as failed"
                    + (f": {outcome.reason}" if outcome.reason else "")
                ),
            )
            yield _failed(state.run_id, err)
            raise err

    async def _pause_for_reapproval(
        self, state: _RunState, calls: list[ToolCall], decisions: _Decisions
    ) -> AsyncGenerator[RunEvent, None]:
        """Pause for a fresh approval of calls whose earlier dispatch did not happen."""
        pending = [c for c in calls if c.id in decisions.deferred]
        interruption = self._make_interruption(
            state, pending, copy_messages=True, reason=_REAPPROVE_REASON
        )
        await self._remember_interruption(interruption)
        _trace_approval_requested(state, interruption, pending)
        if self.checkpoint_store is not None:
            await self._save_interruption(state, "paused", interruption)
        yield RunEvent(
            type=RunEventType.RUN_PAUSED,
            run_id=state.run_id,
            data={
                "pending": [
                    {"id": c.id, "name": c.name, "arguments": c.arguments} for c in pending
                ],
                "interruption": interruption,
                "last_agent": state.agent,
                "agent_path": list(state.path),
            },
        )

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
        hooks = state.hooks or _NO_HOOKS
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
            for decision in state.guard_events.pop(tc.id, ()):
                yield decision  # its tool guardrails' decisions, before its result
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
            dispatched = state.dispatch_keys.pop(tc.id, None)
            if dispatched is not None and state.journal is not None:
                # The result is in the checkpoint: close the call's intent in the journal.
                await state.journal.record_outcome(
                    dispatched, result_text, uncertain=retry_reason == "tool_timeout"
                )
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
        tracing.record_tool_content(span, tracing.TOOL_CALL_ARGUMENTS, tc.arguments)
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
            tracing.record_tool_content(span, tracing.TOOL_CALL_RESULT, result)
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
        step_key = self._dispatch_key(state, tc)
        tool_ctx = state.ctx.for_tool(
            tc.id,
            tc.name,
            retries,
            idempotency_key=self._idempotency_key(state, step_key),
            fencing_token=state.journal.fencing_token if state.journal is not None else None,
            tool=tool.info(),
        )
        limit = tool.max_retries if tool.max_retries is not None else 1

        def on_decision(record: dict[str, Any]) -> None:
            tracing.guardrail_decision(None, record)  # on the current execute_tool span
            state.guard_events.setdefault(tc.id, []).append(
                RunEvent(type=RunEventType.GUARDRAIL_DECISION, run_id=state.run_id, data=record)
            )

        if tool.input_guardrails:
            blocked = await _until_cancelled(
                _bounded(
                    state,
                    check_tool_input(tool, tool_ctx, tc.arguments, on_decision=on_decision),
                ),
                state.cancel,
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
                    # A copy: a hook cannot change the approved, checked arguments.
                    await _until_cancelled(
                        hooks.before_tool(tool_ctx, policy.hook_copy(tc)), state.cancel
                    ),
                    str,
                )
                if answered is not None:
                    result = answered  # the hook answered: the tool does not run
                else:
                    result = await self._execute_with_retry(
                        state,
                        tc,
                        tool,
                        tool_ctx,
                        step_key=step_key,
                        approval_id=(
                            decisions.claimed.get(tc.id) if decisions is not None else None
                        ),
                    )
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
                await _until_cancelled(
                    hooks.after_tool(tool_ctx, policy.hook_copy(tc), result), state.cancel
                ),
                str,
            )
            files = list(tool_ctx.tool_attachments)
            if rewritten is not None:
                result, files = rewritten, []  # the tool's files are not sent with a rewrite
            if cache_key is not None and answered is None:
                state.tool_cache[cache_key] = (result, files)
        if tool.output_guardrails:
            checked, rejected = await _until_cancelled(
                _bounded(state, check_tool_output(tool, tool_ctx, result, on_decision=on_decision)),
                state.cancel,
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
        self,
        state: _RunState,
        tc: ToolCall,
        tool: Tool,
        tool_ctx: RunContext[Any],
        *,
        step_key: StepKey | None = None,
        approval_id: str | None = None,
    ) -> Any:
        """Run the tool (with its own timeout), retrying transient errors per ``tool.retry``.

        In a journaled durable run, a non-pure call's intent is committed first (fenced by
        the run lease), so a crash before this point is known to have dispatched nothing.
        """
        tracked = False
        if state.journal is not None:
            tracked = await self._record_intent(
                state, tc, tool, tool_ctx, step_key, approval_id=approval_id
            )
        # A6 S11: budget reservations made by the tool are linked to this call's step.
        key = step_key if step_key is not None else self._dispatch_key(state, tc)
        scope = linkage.enter_dispatch_scope(
            _run_workspace(state),
            state.run_id,
            str(key),
            tracked=tracked,
            fencing_token=_lease_token(state),
        )
        try:
            return await self._execute_attempts(state, tc, tool, tool_ctx)
        finally:
            linkage.leave_dispatch_scope(scope)

    async def _execute_attempts(
        self, state: _RunState, tc: ToolCall, tool: Tool, tool_ctx: RunContext[Any]
    ) -> Any:
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
    def _dispatch_key(state: _RunState, tc: ToolCall) -> StepKey:
        """The journal step key of this call's next dispatch (``turn:<n>/call:<id>[#k]``)."""
        from nexura_agents._agent_journal import call_step_key

        if state.journal is not None:
            return state.journal.dispatch_key(state.turn, tc.id)
        return call_step_key(state.turn, tc.id)

    @staticmethod
    def _idempotency_key(state: _RunState, step_key: StepKey) -> str:
        """``nx-`` + 32 hex of sha256(workspace, run, step key, 0): stable across takeover."""
        from nexura_agents.contracts.execution import ExecutionId, default_idempotency_key

        return default_idempotency_key(ExecutionId(_run_workspace(state), state.run_id), step_key)

    async def _record_intent(
        self,
        state: _RunState,
        tc: ToolCall,
        tool: Tool,
        tool_ctx: RunContext[Any],
        step_key: StepKey | None,
        *,
        approval_id: str | None = None,
    ) -> bool:
        """Commit the call's intent (non-pure tools); True when an intent is on record.

        A call approved by a person carries that approval (``approval_id``): the journal
        records it as consumed by this dispatch in the same fenced commit as the intent.
        """
        from nexura_agents.contracts.execution import EffectClass, StepIntent

        journal = state.journal
        assert journal is not None
        journal.check()  # assert_lease: a worker that lost the run dispatches nothing
        effect = tool.effect_class()
        if effect is EffectClass.PURE:
            return False
        key = step_key if step_key is not None else self._dispatch_key(state, tc)
        intent = StepIntent(
            step_key=key,
            payload_digest=RunInterruption.action_digest(tc),
            idempotency_key=tool_ctx.idempotency_key or self._idempotency_key(state, key),
            effect_class=effect,
            approval_id=approval_id,
        )
        await journal.record_intent(intent, tc.name)
        state.dispatch_keys[tc.id] = key
        return True

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
            checkpoint_interruption_id=interruption.interruption_id,
            checkpoint_expires_at=interruption.expires_at,
            hooks=policy.active_hooks.get(),  # this run's hooks (set by _scoped)
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
        fresh_approval = copy_messages or reason is not None
        if fresh_approval or state.checkpoint_interruption_id is None:
            state.checkpoint_interruption_id = str(uuid.uuid4())
            state.checkpoint_expires_at = time.time() + self.approval_ttl
        interruption = RunInterruption(
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
            interruption_id=state.checkpoint_interruption_id,
            expires_at=state.checkpoint_expires_at or 0.0,
        )
        interruption.action_digests = interruption.pending_digests()
        if self._approval_secret is not None:
            interruption.signature = _approval_signature(interruption, self._approval_secret)
        return interruption

    def _check_approval_store(self) -> None:
        if not all(
            callable(getattr(self.approval_store, method, None))
            for method in ("save_approval", "load_approval", "consume_approval")
        ):
            raise SDKValidationError(
                "Checkpoint store needs atomic approval methods; provide approval_store=... "
                "implementing nexura_agents.approvals.ApprovalStore."
            )

    def _prune_approval_snapshots(self) -> None:
        now = time.time()
        for key, expiry in list(self._approval_snapshot_expiries.items()):
            if expiry <= now or key not in self._approval_snapshots:
                self._approval_snapshots.pop(key, None)
                self._approval_snapshot_expiries.pop(key, None)
        while len(self._approval_snapshots) > 256:
            key, _ = self._approval_snapshots.popitem(last=False)
            self._approval_snapshot_expiries.pop(key, None)

    async def _verify_approval(self, interruption: ApprovalEnvelope) -> tuple[str, bool]:
        self._prune_approval_snapshots()
        _check_approval_expiry(interruption)
        envelope = _approval_payload(interruption)
        authenticated = False
        if self._approval_secret is not None:
            if (
                not isinstance(interruption.signature, str)
                or re.fullmatch(r"[0-9a-f]{64}", interruption.signature) is None
                or not hmac.compare_digest(
                    interruption.signature, _approval_signature(interruption, self._approval_secret)
                )
            ):
                raise ApprovalDecisionError(
                    "Invalid interruption signature; refusing tampered approval."
                )
            authenticated = True
        approval_store = self.approval_store
        if approval_store is not None:
            self._check_approval_store()
            stored = await approval_store.load_approval(interruption.interruption_id)
            if stored != envelope:
                raise ApprovalDecisionError(
                    "Stored interruption differs or is missing; refusing resume."
                )
            authenticated = True
        elif interruption.interruption_id in self._approval_snapshots:
            if self._approval_snapshots[interruption.interruption_id] != envelope:
                raise ApprovalDecisionError("Stored interruption differs from the reviewed action.")
            authenticated = True
            self._approval_snapshots.move_to_end(interruption.interruption_id)
        elif interruption._approval_snapshot is not None:
            if interruption._approval_snapshot != envelope:
                raise ApprovalDecisionError("Stored interruption differs from the reviewed action.")
            authenticated = True
        return envelope, authenticated

    async def _consume_approval(self, interruption: ApprovalEnvelope, envelope: str) -> None:
        _check_approval_expiry(interruption)
        if self.approval_store is not None and not await self.approval_store.consume_approval(
            interruption.interruption_id, envelope
        ):
            raise ApprovalDecisionError(
                "Interruption already consumed or expired; refusing replay."
            )
        self._approval_snapshots.pop(interruption.interruption_id, None)
        self._approval_snapshot_expiries.pop(interruption.interruption_id, None)

    async def _refuse_spent_approval(self, journal: AgentJournal, interruption: Any) -> None:
        """Refuse, without consuming or writing, an approval that cannot be used here.

        Raises ``ApprovalDecisionError`` when it expired, the run already finished, a
        dispatch was made under it, or an earlier resume claimed it (then ``recover()``
        continues the run). Otherwise returns: the approval is still usable.
        """
        _check_approval_expiry(interruption)
        snapshot = await journal.peek()
        if snapshot is not None:
            _refuse_claimed(
                dict(snapshot.run.state), dict(snapshot.steps), snapshot.run.status, interruption
            )

    def _guard_resumed_batch(
        self, state: _RunState, resume_batch: tuple[list[ToolCall], _Decisions]
    ) -> None:
        """Calls of a resumed batch this resume did not decide (review B1).

        The batch of an ``effect_unknown`` pause holds every unanswered call; only the ones
        in doubt were asked about. For the others the journal and the earlier claim decide:
        an outcome on record answers the call, the claim's rejection or answer holds, its
        approval lets the call run under it, and a call to a tool that requires approval
        with none of these is asked for again instead of being sent.
        """
        calls, decisions = resume_batch
        if state.journal is None:
            return
        undecided = [c for c in calls if c.id not in decisions.pending_ids]
        if undecided:
            self._apply_claim(state, state.agent, undecided, decisions, answer_outcomes=True)
            if decisions.claim is not None:
                # The new claim keeps the earlier decisions it builds on.
                decisions.claim["reject"] = {
                    **{
                        k: v
                        for k, v in decisions.rejected.items()
                        if k not in decisions.pending_ids
                    },
                    **decisions.claim.get("reject", {}),
                }
                decisions.claim["answers"] = {
                    **{
                        k: v for k, v in decisions.answers.items() if k not in decisions.pending_ids
                    },
                    **decisions.claim.get("answers", {}),
                }

    async def _reconsume_approval(self, interruption: ApprovalEnvelope, envelope: str) -> bool:
        """Consume an approval a stopped owner claimed (it may have consumed it already).

        True when this call consumed it (the stopped owner had not).
        """
        consumed = False
        if self.approval_store is not None:
            with contextlib.suppress(Exception):
                consumed = bool(
                    await self.approval_store.consume_approval(
                        interruption.interruption_id, envelope
                    )
                )
        self._approval_snapshots.pop(interruption.interruption_id, None)
        self._approval_snapshot_expiries.pop(interruption.interruption_id, None)
        return consumed

    async def _claim_approval(
        self, state: _RunState, journal: AgentJournal, decisions: _Decisions
    ) -> None:
        """Record the claim in the run's journal (fenced), then consume the approval.

        A takeover then tells a claimed approval that was used for a dispatch (an intent
        carries it) from one that was not (the claim alone): see ``_recover_claimed``.
        """
        assert decisions.consume is not None and decisions.claim is not None
        interruption, envelope = decisions.consume
        decisions.consume = None
        # Checked again under the lease, before any journal write (review M2).
        _refuse_claimed(journal.state, journal.steps, journal.status, interruption)
        held = journal.state.get("approval_claim")
        was_paused = journal.status == "paused"
        if interruption.reason != _UNKNOWN_REASON:
            # Every call an approval pause asks about was gated (by its tool, a hook or a
            # re-approval). Recorded with the claim, before any dispatch, so a recovery
            # without the hook still asks again once the claim expires, even when the
            # pause commit itself was lost (P39 G2 review M1).
            for waiting in interruption.pending:
                if waiting.id not in interruption.question_ids:
                    _record_gate(state, _gate_key(state.turn, waiting.id))
        await journal.record_claim(decisions.claim)
        try:
            await self._consume_approval(interruption, envelope)
        except ApprovalDecisionError:
            # Not ours to use: the earlier claim stands, and a run that was waiting for a
            # person waits again. A run that was not paused keeps its status (it stays
            # recoverable).
            if was_paused:
                journal.state["approval_claim"] = held if isinstance(held, dict) else None
                keys = [
                    journal.latest(state.turn, c.id)[1]
                    for c in interruption.pending
                    if interruption.reason == _UNKNOWN_REASON
                ]
                with contextlib.suppress(Exception):
                    await journal.pause(interruption.pause_reason, keys)
            else:
                with contextlib.suppress(Exception):
                    await journal.restore_claim(held if isinstance(held, dict) else None)
            raise
        _trace_approval_decided(state.span, interruption, decisions)  # consumed: decided

    async def _remember_interruption(self, interruption: ApprovalEnvelope) -> None:
        self._prune_approval_snapshots()
        envelope = _approval_payload(interruption)
        if self.approval_store is not None:
            self._check_approval_store()
            await self.approval_store.save_approval(
                interruption.interruption_id, envelope, interruption.expires_at
            )
        else:
            # Store-backed grants must be resumed through their configured store;
            # do not issue an in-memory shortcut that could skip atomic consumption.
            interruption._approval_snapshot = envelope
            self._approval_snapshots[interruption.interruption_id] = envelope
            self._approval_snapshots.move_to_end(interruption.interruption_id)
            self._approval_snapshot_expiries[interruption.interruption_id] = interruption.expires_at
            self._prune_approval_snapshots()

    async def _save_interruption(
        self, state: _RunState, phase: str, interruption: RunInterruption
    ) -> None:
        from nexura_agents.durable import accepts_fencing, encode_checkpoint

        assert self.checkpoint_store is not None
        if phase == "paused":
            await self._remember_interruption(interruption)
        final = state.final_text if phase == "completed" else None
        data = encode_checkpoint(
            phase,
            interruption.to_json(),
            final_text=final,
            output_attempts=state.output_attempts,
            agent_started_at=state.agent_started_at,
        )
        try:
            if state.journal is not None:
                state.journal.check()  # a worker that lost the run lease writes nothing
            if accepts_fencing(self.checkpoint_store.save):
                # Refused when another caller holds the recovery lease, so a stalled or
                # superseded writer can never overwrite newer progress.
                await self.checkpoint_store.save(  # type: ignore[call-arg]
                    state.run_id, data, owner=state.checkpoint_owner
                )
            else:
                await self.checkpoint_store.save(state.run_id, data)
            if phase == "paused" and state.journal is not None and state.journal.held:
                keys = [
                    state.in_doubt[c.id] for c in interruption.pending if c.id in state.in_doubt
                ]
                await state.journal.pause(interruption.pause_reason, keys)
        except Exception:
            state.checkpoint_store_failed = True
            raise

    async def _delete_checkpoint(
        self,
        run_id: str,
        owner: str | None,
        journal: AgentJournal | None = None,
        status: str = "failed",
    ) -> None:
        """Best-effort cleanup; fenced so a stale caller cannot delete a recoverer's work.

        With a journal, the run's status becomes ``status`` first (fenced), then the
        checkpoint is deleted: a crash in between leaves a terminal journal row and a
        checkpoint that ``recover`` finishes (a ``completed`` one is replayed), never a
        ``running`` row without a checkpoint. A refused journal write deletes nothing.
        """
        from nexura_agents.durable import accepts_fencing

        store = self.checkpoint_store
        assert store is not None
        if journal is not None:
            await journal.stop_heartbeat()
            if journal.held:
                try:
                    await journal.finish(status)
                except Exception:
                    return  # stale or failing journal: keep the checkpoint
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
            return self._scoped(self._recover_stream_impl(agent, run_id, **kwargs))

        def start(name: str, *, resumed: bool) -> Any:
            return tracing.start_recover(name)

        return self._scoped(
            self._traced(
                start, agent.name, True, self._recover_stream_impl, agent, run_id, **kwargs
            )
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
        from nexura_agents._agent_journal import AgentJournal, journal_capable

        if journal_capable(checkpoints):
            from nexura_agents.durable import new_lease_owner

            # The run lease: free, expired, or held by a process on this host that is gone.
            journal = await AgentJournal.open(checkpoints, run_id, self.lease_ttl)
            # A6 S11: a run started under a budget needs one, refused before the takeover.
            stored = await journal.peek()
            self._check_budget_scope(run_id, stored.run.state if stored is not None else None)
            lease_owner = new_lease_owner()
            if not await journal.takeover(lease_owner, agent.definition_digest(), agent.name):
                raise RunRecoveryError(
                    f"Run '{run_id}' is already being recovered by another caller (or its "
                    "process still holds the run lease)."
                )
            journal.start_heartbeat()  # reconcile probes run before the loop
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
                    _owner=lease_owner,
                    _journal=journal,
                ):
                    yield event
            finally:
                await journal.close()
            return
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
        _journal: AgentJournal | None = None,
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
        if (
            _journal is not None
            and _journal.status in ("completed", "failed")
            and phase != "completed"
        ):
            # Its terminal status was committed before a lost checkpoint cleanup.
            await self._delete_checkpoint(run_id, _owner)
            raise RunRecoveryError(
                f"Run '{run_id}' has already {_journal.status}; only its lost cleanup "
                "could be finished, and its checkpoint is not a completed one."
            )
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
        if _journal is not None and _journal.lease is not None:
            state.journal = _journal
            _journal.trace_span = state.span  # nexura.effect.* events go on the run span
            if _journal.stored_digest != agent.definition_digest():
                _warn_definition_changed(run_id, agent)
        # A6 S11, before any step runs (the budget was checked before the takeover): the
        # reservations the stopped owner left open are settled (dispatched) or released.
        await self._settle_reservations(state, "terminal" if phase == "completed" else "takeover")
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

        if state.journal is not None:
            await self._heal_journal(state, phase, interruption)
        claim = (
            state.journal.unused_claim(interruption.interruption_id)
            if phase == "paused" and state.journal is not None and state.journal.held
            else None
        )
        if claim is not None:
            # A resume claimed this approval and stopped before dispatching under it (K8).
            async for event in self._recover_approval_claim(
                agent,
                interruption,
                state,
                claim,
                deps=deps,
                timeout=timeout,
                session_store=session_store,
                stream_text=stream_text,
                cancel=cancel,
                _span=_span,
            ):
                yield event
            return
        if phase == "paused" and not await self._paused_effects_resolved(state, interruption):
            # Still waiting for a human: hand the stored interruption back.
            if (
                state.journal is not None
                and state.journal.held
                and state.journal.status != "paused"
            ):
                # The pause reached the checkpoint but not the journal (a crash between
                # the two): write it now, so the journal says paused / indeterminate.
                if interruption.reason != _UNKNOWN_REASON:
                    # Every call this pause asks a person about stays gated after a later
                    # crash, whichever hooks recover the run (the hook gates recorded at
                    # the pause were in the lost journal commit; MIN7).
                    for waiting in interruption.pending:
                        if waiting.id not in interruption.question_ids:
                            _record_gate(state, _gate_key(state.turn, waiting.id))
                keys = [
                    state.journal.latest(state.turn, c.id)[1]
                    for c in interruption.pending
                    if interruption.reason == _UNKNOWN_REASON
                ]
                await state.journal.pause(interruption.pause_reason, keys)
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

        hooks = state.hooks or _NO_HOOKS
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
            await self._delete_checkpoint(
                run_id, state.checkpoint_owner, state.journal, "completed"
            )
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
        if phase in ("tools_pending", "paused"):
            calls, remaining_calls = _unanswered(state.messages)
            self._restore_handoff(state, calls, interruption)
            if state.journal is not None:
                await state.journal.ensure_running()
            decisions = _Decisions()
            self._continue_claim(state, active, remaining_calls, decisions)
            uncertain: list[ToolCall] = []
            for c in remaining_calls:
                if c.name in active.handoffs_by_name:
                    continue
                if (
                    c.id in decisions.rejected
                    or c.id in decisions.answers
                    or c.id in decisions.outcomes
                    or c.id in decisions.deferred
                ):
                    continue  # decided by the claim, or asked again (B1)
                tool = active.tools_by_name.get(c.name)
                if tool is not None and not await self._settle_in_doubt(state, tool, c, decisions):
                    uncertain.append(c)
            if uncertain:
                names = ", ".join(f"{c.name}({c.id})" for c in uncertain)
                if on_uncertain != "pause" and state.journal is not None:
                    await state.journal.mark_indeterminate(
                        [state.in_doubt[c.id] for c in uncertain if c.id in state.in_doubt]
                    )
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
                    _trace_approval_requested(state, paused, uncertain)
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

    async def _recover_approval_claim(
        self,
        agent: Agent,
        interruption: RunInterruption,
        state: _RunState,
        claim: dict[str, Any],
        **kwargs: Any,
    ) -> AsyncGenerator[RunEvent, None]:
        """Continue a claimed, unused approval under the same approval (design §2.7).

        The recorded decisions are applied as the stopped resume would have, after checking
        the approval again (stored envelope, action digests, expiry). An expired approval
        is asked for again: the run pauses with a fresh interruption for the same calls.
        """
        from nexura_agents.durable import ResolvedOutcome

        journal = state.journal
        assert journal is not None
        expires = claim.get("expires_at")
        if not isinstance(expires, (int, float)) or expires <= time.time():
            # Before asking again, the old approval is spent (best effort): one call never
            # has two live approvals (round 3, F3-B1).
            await self._reconsume_approval(interruption, _approval_payload(interruption))
            journal.state["approval_claim"] = None
            fresh = self._make_interruption(
                state, list(interruption.pending), copy_messages=True, reason=interruption.reason
            )
            await self._save_interruption(state, "paused", fresh)
            yield RunEvent(
                type=RunEventType.RUN_PAUSED,
                run_id=state.run_id,
                data={
                    "pending": [
                        {"id": c.id, "name": c.name, "arguments": c.arguments}
                        for c in fresh.pending
                    ],
                    "interruption": fresh,
                    "last_agent": state.agent,
                    "agent_path": list(state.path),
                },
            )
            return
        approve: dict[str, str | ApproveWith] = {}
        for call_id, item in (claim.get("approve") or {}).items():
            digest = item.get("digest")
            arguments = item.get("arguments")
            approve[call_id] = (
                ApproveWith(arguments=arguments, digest=digest) if arguments is not None else digest
            )
        outcomes = {
            call_id: ResolvedOutcome(**value)
            for call_id, value in (claim.get("outcomes") or {}).items()
        }
        async for event in self._resume_stream_impl(
            agent,
            interruption,
            approve=approve,
            reject=claim.get("reject") or None,
            answers=claim.get("answers") or None,
            outcomes=outcomes or None,
            actor=claim.get("actor") or None,
            _reclaim=(journal, state.checkpoint_owner),
            **kwargs,
        ):
            yield event

    def _continue_claim(
        self, state: _RunState, agent: Agent, calls: list[ToolCall], decisions: _Decisions
    ) -> None:
        """Recovery of a checkpointed batch: the claim's decisions hold (design §2.7, B1)."""
        self._apply_claim(state, agent, calls, decisions, answer_outcomes=False)

    def _apply_claim(
        self,
        state: _RunState,
        agent: Agent,
        calls: list[ToolCall],
        decisions: _Decisions,
        *,
        answer_outcomes: bool,
    ) -> None:
        """Decide calls nobody decided in this process from the journal and the claim.

        For a call that was never dispatched (no intent): the claim's rejection or answer
        holds; a call it approved runs under that approval (its intent carries it), or is
        asked for again once the approval expired; any other call to a tool that requires
        approval is asked for again. A person never sees a call they rejected, or did not
        approve, sent after a crash. A dispatched call follows the effect-class rules; a
        resolution the claim recorded for it applies (and ``answer_outcomes``: its outcome
        on record answers it). Runs checkpointed before they had a journal keep the old
        rules for the turns before it.
        """
        from nexura_agents.contracts.execution import EffectStatus
        from nexura_agents.durable import ResolvedOutcome

        journal = state.journal
        if journal is None:
            return
        claim = journal.state.get("approval_claim")
        if not isinstance(claim, dict) or claim.get("turn", state.turn) != state.turn:
            claim = None  # a claim of another turn says nothing about these calls (m2)
        claim = claim or {}
        approved = set(claim.get("approve") or {})
        rejected = dict(claim.get("reject") or {})
        answers = dict(claim.get("answers") or {})
        outcomes = dict(claim.get("outcomes") or {})
        expires = claim.get("expires_at")
        expired = not isinstance(expires, (int, float)) or expires <= time.time()
        claim_id = claim.get("approval_id")
        for call in calls:
            if (
                call.id in decisions.rejected
                or call.id in decisions.answers
                or call.id in decisions.outcomes
                or call.id in decisions.unknown
                or call.id in decisions.deferred
                or call.name in agent.handoffs_by_name
            ):
                continue
            tool = _callable_tool(agent, state, call.name)
            _, key, step = journal.latest(state.turn, call.id)
            if step is not None and step.intent is not None:
                effect = journal.effects.get(str(key))
                if step.outcome is not None:
                    if answer_outcomes and tool is not None:
                        decisions.answers[call.id] = _result_text(tool, step.outcome.output)
                elif call.id in outcomes and (
                    effect is None or effect.status != EffectStatus.RESOLVED
                ):
                    decisions.outcomes[call.id] = ResolvedOutcome(**outcomes[call.id])
                continue  # dispatched: the effect-class rules decide
            if journal.unjournaled_dispatch_possible(state.turn):
                continue  # may have been sent before the run had a journal (H1)
            gated = tool is not None and self._needs_approval(state, tool, call)
            if call.id in rejected:
                decisions.rejected[call.id] = str(rejected[call.id] or "")
            elif call.id in answers:
                decisions.answers[call.id] = str(answers[call.id])
            elif call.id in approved and isinstance(claim_id, str):
                if expired and gated:
                    decisions.deferred.add(call.id)
                else:
                    decisions.claimed[call.id] = claim_id
            elif gated:
                decisions.deferred.add(call.id)  # no decision on record: ask again

    async def _heal_journal(
        self, state: _RunState, phase: str, interruption: RunInterruption
    ) -> None:
        """Close intents whose result reached the checkpoint but not the journal.

        A crash between saving a tool result and committing its outcome leaves the effect
        ``pending``; the checkpointed result is the outcome, so record it now.
        """
        from nexura_agents.contracts.execution import EffectStatus

        journal = state.journal
        if journal is None or not journal.held or phase == "completed":
            return
        calls, remaining = _unanswered(state.messages)
        open_ids = {c.id for c in remaining}
        results = {m.tool_call_id: m.content or "" for m in state.messages if m.role == "tool"}
        for call in calls:
            if call.id in open_ids or call.id not in results:
                continue
            _, key, step = journal.latest(state.turn, call.id)
            effect = journal.effects.get(str(key))
            if (
                step is not None
                and step.intent is not None
                and step.outcome is None
                and effect is not None
                and effect.status == EffectStatus.PENDING
            ):
                await journal.record_outcome(key, results[call.id])

    async def _paused_effects_resolved(
        self, state: _RunState, interruption: RunInterruption
    ) -> bool:
        """A crash pause whose in-doubt effects were all resolved in the journal (for
        example through the effects API) continues on recover; the stale pause is consumed."""
        from nexura_agents.contracts.execution import EffectStatus

        journal = state.journal
        if journal is None or interruption.reason != _UNKNOWN_REASON or not interruption.pending:
            return False
        for call in interruption.pending:
            _, key, step = journal.latest(state.turn, call.id)
            effect = journal.effects.get(str(key))
            if step is None or effect is None or effect.status != EffectStatus.RESOLVED:
                return False
        store = self.approval_store
        if store is not None and callable(getattr(store, "consume_approval", None)):
            with contextlib.suppress(Exception):
                await store.consume_approval(
                    interruption.interruption_id, _approval_payload(interruption)
                )
        return True

    async def _settle_in_doubt(
        self, state: _RunState, tool: Tool, call: ToolCall, decisions: _Decisions
    ) -> bool:
        """Decide what recovery does with an unanswered call; False means its outcome is unknown.

        With a journal, a call without an intent was never dispatched and simply runs. A
        dispatched one: pure and idempotent calls run again with the same key, a
        reconcilable one is probed, a resolved effect applies its resolution, and a
        non-idempotent one is in doubt. Without a journal every unanswered call may have
        been dispatched.
        """
        from nexura_agents.contracts.execution import (
            Applied,
            EffectClass,
            EffectStatus,
            NotApplied,
            StepIntent,
        )
        from nexura_agents.durable import ResolvedOutcome

        effect = tool.effect_class()
        journal = state.journal
        step = None
        if journal is not None:
            _, key, step = journal.latest(state.turn, call.id)
            if (step is None or step.intent is None) and not journal.unjournaled_dispatch_possible(
                state.turn
            ):
                return True  # K1: nothing was dispatched
            # Otherwise without an intent: checkpointed before the run had a journal (H1).
        if journal is not None and step is not None and step.intent is not None:
            if step.outcome is not None:
                decisions.answers[call.id] = _result_text(tool, step.outcome.output)
                return True
            record = journal.effects.get(str(key))
            if record is not None and record.status == EffectStatus.RESOLVED:
                assert record.resolution is not None
                decisions.outcomes[call.id] = ResolvedOutcome(
                    result=record.result,
                    resolution=record.resolution.value,
                    reason=str(record.result or "") if record.resolution == "failed" else "",
                )
                decisions.journal_resolved.add(call.id)
                return True
            if record is not None and record.status == EffectStatus.CONFIRMED:
                decisions.answers[call.id] = _result_text(tool, record.result)
                state.dispatch_keys[call.id] = key
                return True
            intent = step.intent
            # The promise the call was sent under decides (H3): a tool re-declared after the
            # crash can only make recovery stricter, never looser.
            effect = _stricter_effect(intent.effect_class, effect, tool)
        else:
            key = self._dispatch_key(state, call)
            intent = StepIntent(
                step_key=key,
                payload_digest=RunInterruption.action_digest(call),
                idempotency_key=self._idempotency_key(state, key),
                effect_class=effect,
            )
        if effect in (EffectClass.PURE, EffectClass.IDEMPOTENT):
            return True  # re-run with the same idempotency key
        if effect is EffectClass.RECONCILABLE:
            probe = await self._reconcile(state, tool, call, intent)
            if isinstance(probe, Applied):
                decisions.answers[call.id] = _result_text(tool, probe.result)
                if journal is not None:
                    state.dispatch_keys[call.id] = key
                return True
            if isinstance(probe, NotApplied):
                if self._needs_approval(state, tool, call):
                    decisions.deferred.add(call.id)  # its approval was used by the first try
                return True
        state.in_doubt[call.id] = key
        return False

    async def _reconcile(
        self, state: _RunState, tool: Tool, call: ToolCall, intent: StepIntent
    ) -> ReconcileResult:
        """Run the tool's ``reconcile(ctx, intent)``; an error or bad answer means Unknown."""
        from nexura_agents.contracts.execution import Applied, NotApplied, Unknown

        ctx = state.ctx.for_tool(
            call.id,
            call.name,
            0,
            idempotency_key=intent.idempotency_key,
            fencing_token=state.journal.fencing_token if state.journal is not None else None,
            tool=tool.info(),
        )
        assert tool.reconcile is not None
        try:
            probe = tool.reconcile(ctx, intent)
            if inspect.isawaitable(probe):
                probe = await probe
        except Exception as exc:
            warnings.warn(
                f"reconcile of tool '{tool.name}' failed ({exc!r}); its outcome stays unknown.",
                RuntimeWarning,
                stacklevel=2,
            )
            return Unknown()
        if not isinstance(probe, (Applied, NotApplied, Unknown)):
            warnings.warn(
                f"reconcile of tool '{tool.name}' must return Applied(result), NotApplied() or "
                f"Unknown(), not {type(probe).__name__}; its outcome stays unknown.",
                RuntimeWarning,
                stacklevel=2,
            )
            return Unknown()
        return probe

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
