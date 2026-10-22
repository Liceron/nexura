# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Lifecycle hooks: subclass ``AgentHooks`` and override what you need."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nexura_agents.run_context import RunContext
from nexura_agents.types import Message, ModelResponse, ToolCall

if TYPE_CHECKING:
    from nexura_agents.agent import Agent


class AgentHooks:
    """Async callbacks around model and tool calls.

    Hooks observe by default. ``before_tool`` may raise ``ModelRetry`` to refuse a call
    and tell the model why, or any other exception to fail the run.

    The model and tool hooks may also take over by returning a value (``None`` changes
    nothing):

    - ``before_model`` returns a ``ModelResponse`` to skip the model call (a cache hit, a
      policy refusal). Nothing is billed: requests and usage are not counted.
    - ``after_model`` returns a ``ModelResponse`` to replace the reply. Usage still counts
      what the provider billed.
    - ``before_tool`` returns a string to skip the tool and use it as the result. The
      result is not cached; ``after_tool`` and output guardrails still run.
    - ``after_tool`` returns a string to replace the result. Files the tool returned are
      then not sent, as when a guardrail rewrites a result.
    """

    async def on_run_start(self, ctx: RunContext[Any]) -> None:
        """Called once when a run starts; does nothing by default."""
        return None

    async def before_model(
        self, ctx: RunContext[Any], messages: list[Message]
    ) -> ModelResponse | None:
        """Called before each model request with the outgoing messages.

        Return a ``ModelResponse`` to use it instead of calling the model.
        """
        return None

    async def after_model(
        self, ctx: RunContext[Any], response: ModelResponse
    ) -> ModelResponse | None:
        """Called after each model response. Return a ``ModelResponse`` to replace it."""
        return None

    async def before_tool(self, ctx: RunContext[Any], call: ToolCall) -> str | None:
        """Called before a tool runs; may raise to refuse the call.

        Return a string to skip the tool and use the string as its result.
        """
        return None

    async def after_tool(self, ctx: RunContext[Any], call: ToolCall, result: str) -> str | None:
        """Called after a tool returns its result. Return a string to replace the result."""
        return None

    async def on_run_end(self, ctx: RunContext[Any], output: Any) -> None:
        """Called when the run finishes with its output; does nothing by default."""
        return None

    async def on_agent_start(self, ctx: RunContext[Any], agent: Agent) -> None:
        """Called when an agent becomes active: at the start of a run, and for the target
        of each handoff. Does nothing by default."""
        return None

    async def on_agent_end(self, ctx: RunContext[Any], agent: Agent, output: Any) -> None:
        """Called when an agent stops being active: with the final output when the run
        ends with it, or ``None`` when it hands off. Does nothing by default."""
        return None

    async def on_handoff(self, ctx: RunContext[Any], source: Agent, target: Agent) -> None:
        """Called when control moves from ``source`` to ``target``; does nothing by default."""
        return None
