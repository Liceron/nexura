# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Core data structures and types for Nexura AgentKit."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic, Literal

from typing_extensions import TypeVar

from nexura_agents.attachments import Attachment

if TYPE_CHECKING:
    from nexura_agents.agent import Agent
    from nexura_agents.events import RunEvent

# What ``RunResult.output`` is: the ``output_type`` model, or ``str`` without one. A bare
# ``Agent`` / ``RunResult`` annotation means ``[Any]``. (typing_extensions ships with pydantic.)
OutputT = TypeVar("OutputT", default=Any)

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class ApproveWith:
    """Approve a pending tool call, but run it with ``arguments`` instead.

    ``Runner.resume(..., approve={call_id: ApproveWith(arguments={...}, digest=d)})``. The
    edit is checked against the tool's schema, and the call in the transcript is rewritten,
    so the history shows what actually ran. ``digest`` (from
    ``interruption.pending_digests()``) binds the approval to the original action the
    reviewer saw; leave it out to approve by call id only.
    """

    arguments: dict[str, Any]
    digest: str | None = None


# Name of the built-in tool added by Agent(ask_user=True).
ASK_USER_TOOL = "ask_user"


@dataclass(frozen=True)
class ToolCall:
    """Represents a tool call requested by a model."""

    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str | None = None


@dataclass(frozen=True)
class ToolResult:
    """Represents the outcome of a tool execution."""

    tool_call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass
class Message:
    """Represents a conversation message in the agent history."""

    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    # Images, documents or audio sent with this (user) message.
    attachments: list[Attachment] | None = None
    # Opaque provider data that must go back to the same provider on later calls, e.g.
    # Anthropic thinking blocks (with signatures) on an assistant message that called tools.
    # Saved in sessions and checkpoints; other providers ignore it.
    provider_data: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert message to a dictionary representation."""
        data: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            data["content"] = self.content
        if self.tool_calls is not None:
            data["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": tc.raw_arguments
                        if tc.raw_arguments is not None
                        else json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in self.tool_calls
            ]
        if self.tool_call_id is not None:
            data["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            data["name"] = self.name
        if self.attachments:
            data["attachments"] = [a.to_dict() for a in self.attachments]
        return data


@dataclass(frozen=True)
class Usage:
    """Token usage reported by an LLM provider.

    Fields are None when the provider does not report token counts.
    Values are never fabricated as zero when unknown.
    is_complete indicates whether all turns reported complete token metrics.
    """

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    is_complete: bool = True
    cached_prompt_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    # Hidden "thinking" tokens. They are already included in completion_tokens, because
    # providers bill them as output; this field only shows how many there were.
    reasoning_tokens: int | None = None

    @property
    def is_reported(self) -> bool:
        """True if at least one token metric was reported by the provider."""
        return (
            self.prompt_tokens is not None
            or self.completion_tokens is not None
            or self.total_tokens is not None
            or self.cached_prompt_tokens is not None
            or self.cache_creation_input_tokens is not None
            or self.cache_read_input_tokens is not None
        )

    def add(self, other: Usage | None) -> Usage:
        """Accumulate token counts across turns without fabricating zeros."""
        if other is None:
            return Usage(
                prompt_tokens=self.prompt_tokens,
                completion_tokens=self.completion_tokens,
                total_tokens=self.total_tokens,
                is_complete=False,
                cached_prompt_tokens=self.cached_prompt_tokens,
                cache_creation_input_tokens=self.cache_creation_input_tokens,
                cache_read_input_tokens=self.cache_read_input_tokens,
                reasoning_tokens=self.reasoning_tokens,
            )

        other_has_all_metrics = (
            other.prompt_tokens is not None
            and other.completion_tokens is not None
            and other.total_tokens is not None
        )
        new_is_complete = self.is_complete and other.is_complete and other_has_all_metrics

        def _sum_opt(a: int | None, b: int | None) -> int | None:
            if a is None and b is None:
                return None
            return (a or 0) + (b or 0)

        return Usage(
            prompt_tokens=_sum_opt(self.prompt_tokens, other.prompt_tokens),
            completion_tokens=_sum_opt(self.completion_tokens, other.completion_tokens),
            total_tokens=_sum_opt(self.total_tokens, other.total_tokens),
            is_complete=new_is_complete,
            cached_prompt_tokens=_sum_opt(self.cached_prompt_tokens, other.cached_prompt_tokens),
            cache_creation_input_tokens=_sum_opt(
                self.cache_creation_input_tokens, other.cache_creation_input_tokens
            ),
            cache_read_input_tokens=_sum_opt(
                self.cache_read_input_tokens, other.cache_read_input_tokens
            ),
            reasoning_tokens=_sum_opt(self.reasoning_tokens, other.reasoning_tokens),
        )


@dataclass(frozen=True)
class ModelResponse:
    """Standardized response from an LLM model."""

    message: Message
    usage: Usage | None = None
    finish_reason: str | None = None


@dataclass
class RunResult(Generic[OutputT]):  # noqa: UP046
    """Final result of an agent run."""

    run_id: str
    final_output: str
    messages: list[Message]
    events: list[RunEvent] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    tool_calls_count: int = 0
    turns_count: int = 0
    duration_seconds: float = 0.0
    status: Literal["completed", "failed", "cancelled", "paused"] = "completed"
    # The validated ``output_type`` object when the agent declares one, else the text.
    output: OutputT = None  # type: ignore[assignment]
    # Set when status == "paused": tool calls waiting for a human decision.
    interruption: RunInterruption | None = None
    # {"name", "version", "sha256"} when instructions came from a PromptRegistry.
    prompt: dict[str, Any] | None = None
    # The agent that produced the result (the starting agent unless a handoff happened) and
    # the names of every agent that was active, in order.
    last_agent: Agent | None = None
    agent_path: list[str] = field(default_factory=list)
    # The run's shared state (``RunContext.state``) when it ended.
    state: dict[str, Any] = field(default_factory=dict)
    # Model usage split by agent name and by model, for this run's own model calls.
    usage_by_agent: dict[str, Usage] = field(default_factory=dict)
    usage_by_model: dict[str, Usage] = field(default_factory=dict)
    # run(..., tags=, metadata=) as given.
    tags: list[str] = field(default_factory=list)


def _usage_dict(usage: Usage) -> dict[str, Any]:
    return {f.name: getattr(usage, f.name) for f in dataclasses.fields(usage)}


def _json_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Run metadata as JSON; values JSON cannot hold (dates, objects) are saved as text."""
    import json

    try:
        return dict(json.loads(json.dumps(metadata, ensure_ascii=False, default=str)))
    except (TypeError, ValueError):  # non-string keys deeper down
        return {
            str(k): v if isinstance(v, (str, int, float, bool)) else str(v)
            for k, v in metadata.items()
        }


def _json_state(state: dict[str, Any]) -> dict[str, Any]:
    """``state`` checked to be JSON-serialisable (it is stored, so it must survive JSON)."""
    import json

    from nexura_agents.errors import StorageSerializationError

    try:
        json.dumps(state, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise StorageSerializationError(
            f"RunContext.state must be JSON-serialisable to be saved: {exc}", original_error=exc
        ) from exc
    return state


@dataclass
class RunInterruption:
    """A run paused before tool calls that need approval.

    Persist it with ``to_json()`` and continue later, even in another process, with
    ``Runner.resume_async(agent, interruption, approve=[...], reject={...})``.
    """

    run_id: str
    agent_name: str
    messages: list[Message]
    pending: list[ToolCall]
    usage: Usage = field(default_factory=Usage)
    turn: int = 0
    tool_calls_count: int = 0
    session_id: str | None = None
    expected_revision: int = 0
    requests: int = 0  # model requests so far, so UsageLimits.request_limit survives resume
    memory_block: str = ""  # recalled memories for this run, so a resume does not recall again
    # Handoffs: ``agent_name`` stays the agent that started the run. ``active_agent`` is the one
    # waiting for the decision (None means the starting agent), ``agent_path`` all agents so far.
    active_agent: str | None = None
    agent_path: list[str] = field(default_factory=list)
    handoffs_count: int = 0
    root_system: Message | None = None  # starting agent's system message, kept for storage
    # Durable runs: why the run stopped (``"outcome_unknown_after_crash"`` when the pending calls
    # may already have executed before a crash) and a handoff chosen but not yet applied
    # (``{"tool_call_id", "reason"}``).
    reason: str | None = None
    pending_handoff: dict[str, str] | None = None
    state: dict[str, Any] = field(default_factory=dict)  # RunContext.state at the pause
    # Pending calls that are Agent(ask_user=True) questions (set when the run pauses).
    question_ids: list[str] = field(default_factory=list)
    usage_by_agent: dict[str, Usage] = field(default_factory=dict)
    usage_by_model: dict[str, Usage] = field(default_factory=dict)
    # Run context carried across the pause, so resume continues the same run.
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    output_attempts: int = 0
    agent_started_at: int = 0

    @staticmethod
    def action_digest(call: ToolCall) -> str:
        """SHA-256 of the exact action (tool name + canonical arguments)."""
        from nexura_agents.contracts.canonical import canonical_digest

        return canonical_digest({"tool": call.name, "arguments": call.arguments})

    @property
    def questions(self) -> list[dict[str, str]]:
        """Pending ``ask_user`` questions: ``[{"id": call_id, "question": text}]``.

        Answer them with ``Runner.resume_async(agent, interruption, answers={id: text})``.
        """
        asked = set(self.question_ids)
        return [
            {"id": call.id, "question": str(call.arguments.get("question", ""))}
            for call in self.pending
            if call.id in asked
        ]

    def pending_digests(self) -> dict[str, str]:
        """Show these to the reviewer; pass them back as ``approve={id: digest}``."""
        return {call.id: self.action_digest(call) for call in self.pending}

    def to_json(self) -> str:
        """Serialize the paused run (transcript, pending calls, usage, session state) to JSON."""
        import json

        from nexura_agents.session import serialize_session

        return json.dumps(
            {
                "schema_version": 1,
                "run_id": self.run_id,
                "agent_name": self.agent_name,
                "transcript": serialize_session(self.run_id, 0, self.messages),
                "pending": [
                    {"id": c.id, "name": c.name, "arguments": c.arguments} for c in self.pending
                ],
                "usage": {
                    "prompt_tokens": self.usage.prompt_tokens,
                    "completion_tokens": self.usage.completion_tokens,
                    "total_tokens": self.usage.total_tokens,
                    "is_complete": self.usage.is_complete,
                    "cached_prompt_tokens": self.usage.cached_prompt_tokens,
                    "cache_creation_input_tokens": self.usage.cache_creation_input_tokens,
                    "cache_read_input_tokens": self.usage.cache_read_input_tokens,
                    "reasoning_tokens": self.usage.reasoning_tokens,
                },
                "turn": self.turn,
                "tool_calls_count": self.tool_calls_count,
                "session_id": self.session_id,
                "expected_revision": self.expected_revision,
                "requests": self.requests,
                "memory_block": self.memory_block,
                "active_agent": self.active_agent,
                "agent_path": list(self.agent_path),
                "handoffs_count": self.handoffs_count,
                "reason": self.reason,
                "pending_handoff": self.pending_handoff,
                "state": _json_state(self.state),
                "question_ids": list(self.question_ids),
                "usage_by_agent": {k: _usage_dict(v) for k, v in self.usage_by_agent.items()},
                "usage_by_model": {k: _usage_dict(v) for k, v in self.usage_by_model.items()},
                "tags": list(self.tags),
                "metadata": _json_metadata(self.metadata),
                "output_attempts": self.output_attempts,
                "agent_started_at": self.agent_started_at,
                "root_system": (
                    serialize_session(self.run_id, 0, [self.root_system])
                    if self.root_system is not None
                    else None
                ),
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, data: str) -> RunInterruption:
        """Rebuild an interruption from ``to_json`` output; reject unknown schema versions."""
        import json

        from nexura_agents.session import deserialize_session

        raw = json.loads(data)
        if raw.get("schema_version") != 1:
            raise ValueError(f"Unsupported interruption schema {raw.get('schema_version')!r}.")
        _, _, messages = deserialize_session(raw["transcript"])
        return cls(
            run_id=raw["run_id"],
            agent_name=raw["agent_name"],
            messages=messages,
            pending=[
                ToolCall(id=c["id"], name=c["name"], arguments=c["arguments"])
                for c in raw["pending"]
            ],
            usage=Usage(**raw["usage"]),
            turn=raw["turn"],
            tool_calls_count=raw["tool_calls_count"],
            session_id=raw.get("session_id"),
            expected_revision=raw.get("expected_revision", 0),
            requests=raw.get("requests", 0),
            memory_block=raw.get("memory_block", ""),
            active_agent=raw.get("active_agent"),
            agent_path=list(raw.get("agent_path") or []),
            handoffs_count=raw.get("handoffs_count", 0),
            reason=raw.get("reason"),
            pending_handoff=raw.get("pending_handoff"),
            state=raw.get("state") if isinstance(raw.get("state"), dict) else {},
            question_ids=[str(i) for i in raw.get("question_ids") or []],
            usage_by_agent={
                str(k): Usage(**v) for k, v in (raw.get("usage_by_agent") or {}).items()
            },
            usage_by_model={
                str(k): Usage(**v) for k, v in (raw.get("usage_by_model") or {}).items()
            },
            tags=[str(t) for t in raw.get("tags") or []],
            metadata=raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
            output_attempts=int(raw.get("output_attempts") or 0),
            agent_started_at=int(raw.get("agent_started_at") or 0),
            root_system=(
                deserialize_session(raw["root_system"])[2][0] if raw.get("root_system") else None
            ),
        )
