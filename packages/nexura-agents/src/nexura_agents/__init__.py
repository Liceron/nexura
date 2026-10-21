# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Nexura Agents: build AI agents that stay on budget, ask before risky actions and
survive restarts.

Everything you need is importable from here::

    from nexura_agents import Agent, Runner, tool
"""

from __future__ import annotations

import importlib
import importlib.util
from typing import TYPE_CHECKING, Any

__version__ = "0.4.0"

_EXPORTS: dict[str, str] = {
    # Build and run agents
    "Agent": "nexura_agents.agent",
    "Runner": "nexura_agents.runner",
    "Handoff": "nexura_agents.handoffs",
    "handoff": "nexura_agents.handoffs",
    "SequentialAgent": "nexura_agents.teams",
    "ParallelAgent": "nexura_agents.teams",
    "LoopAgent": "nexura_agents.teams",
    "Team": "nexura_agents.teams",
    "TeamResult": "nexura_agents.teams",
    "TeamStep": "nexura_agents.teams",
    "TeamInterruption": "nexura_agents.teams",
    "tool": "nexura_agents.tools",
    "Tool": "nexura_agents.tools",
    "RunContext": "nexura_agents.run_context",
    "CancelToken": "nexura_agents.cancel",
    "PromptRegistry": "nexura_agents.prompts",
    "PromptVersion": "nexura_agents.prompts",
    "InMemoryPromptStore": "nexura_agents.prompts",
    "FilePromptStore": "nexura_agents.prompts",
    "Skill": "nexura_agents.skills",
    "SkillLibrary": "nexura_agents.skills",
    "ModelRetry": "nexura_agents.errors",
    "RunResult": "nexura_agents.types",
    "RunInterruption": "nexura_agents.types",
    "RunEvent": "nexura_agents.events",
    "RunEventType": "nexura_agents.events",
    "Message": "nexura_agents.types",
    "Attachment": "nexura_agents.attachments",
    "ToolCall": "nexura_agents.types",
    "Usage": "nexura_agents.types",
    "ModelResponse": "nexura_agents.types",
    # Control
    "UsageLimits": "nexura_agents.limits",
    "ModelSettings": "nexura_agents.limits",
    "AgentHooks": "nexura_agents.hooks",
    "GuardrailResult": "nexura_agents.guardrails",
    "detect_pii": "nexura_agents.guardrails_builtin",
    "redact_pii": "nexura_agents.guardrails_builtin",
    "block_pii": "nexura_agents.guardrails_builtin",
    "redact_output_pii": "nexura_agents.guardrails_builtin",
    "no_prompt_injection": "nexura_agents.guardrails_builtin",
    "max_length": "nexura_agents.guardrails_builtin",
    "allowed_topics": "nexura_agents.guardrails_builtin",
    "json_output_only": "nexura_agents.guardrails_builtin",
    # Models
    "ModelProtocol": "nexura_agents.models.base",
    "FakeModel": "nexura_agents.models.fake",
    "OpenAIChatModel": "nexura_agents.models.openai",
    "LiteLLMModel": "nexura_agents.models.litellm",
    "RetryModel": "nexura_agents.models.resilience",
    "FallbackModel": "nexura_agents.models.resilience",
    "infer_model": "nexura_agents.models.infer",
    "OpenAIProvider": "nexura_agents.providers.openai",
    "AnthropicProvider": "nexura_agents.providers.anthropic:AnthropicNativeProvider",
    "GeminiProvider": "nexura_agents.providers.gemini:GeminiNativeProvider",
    "SarvamProvider": "nexura_agents.providers.sarvam",
    # Tools from MCP servers, OpenAPI specs and modules
    "MCPServer": "nexura_agents.mcp.toolset",
    "AgentMCPServer": "nexura_agents.mcp.serve",
    "A2AServer": "nexura_agents.a2a.server",
    "A2AClient": "nexura_agents.a2a.client",
    "remote_agent_tool": "nexura_agents.a2a.client",
    "mcp_tools": "nexura_agents.mcp.toolset",
    "openapi_tools": "nexura_agents.openapi.toolset",
    "module_tool": "nexura_agents.module_tools",
    "SchemaTool": "nexura_agents.tools",
    "StopAtTools": "nexura_agents.tools",
    "ToolRetry": "nexura_agents.tools",
    "ToolOutput": "nexura_agents.tools",
    # Memory
    "SessionStore": "nexura_agents.session",
    "MemorySessionStore": "nexura_agents.session",
    "SQLiteSessionStore": "nexura_agents.session",
    "fork_session": "nexura_agents.session",
    "delete_session": "nexura_agents.session",
    "list_sessions": "nexura_agents.session",
    "pop_last_turn": "nexura_agents.session",
    "retry_last_turn": "nexura_agents.session",
    "RateLimiter": "nexura_agents.models.ratelimit",
    "RateLimitedModel": "nexura_agents.models.ratelimit",
    "GroupChat": "nexura_agents.teams",
    "GroupChatTurn": "nexura_agents.teams",
    "MaxTurns": "nexura_agents.teams",
    "TextMention": "nexura_agents.teams",
    "TerminationCondition": "nexura_agents.teams",
    "web_fetch_tool": "nexura_agents.web_tools",
    "RunCheckpointStore": "nexura_agents.durable",
    "keep_last_turns": "nexura_agents.history",
    "fit_token_budget": "nexura_agents.history",
    "summarize_older_turns": "nexura_agents.history",
    "MemoryCheckpointStore": "nexura_agents.durable",
    "SQLiteCheckpointStore": "nexura_agents.durable",
    "ResolvedOutcome": "nexura_agents.durable",
    "RunRecoveryError": "nexura_agents.errors",
    "ApproveWith": "nexura_agents.types",
    "CheckpointLeaseLostError": "nexura_agents.errors",
    "Memory": "nexura_agents.memory",
    "MemoryRecord": "nexura_agents.memory",
    "MemoryStore": "nexura_agents.memory",
    "InMemoryMemoryStore": "nexura_agents.memory",
    "HashingEmbedder": "nexura_agents.memory",
    # Budgets
    "BudgetManager": "nexura_agents.usage.manager",
    "BudgetConfig": "nexura_agents.usage.models",
    "ModelPricing": "nexura_agents.usage.models",
    "PricingTable": "nexura_agents.usage.pricing",
    # Offline evaluation (scorers live in nexura_agents.evals)
    "Case": "nexura_agents.evals",
    "CaseResult": "nexura_agents.evals",
    "Score": "nexura_agents.evals",
    "EvalOutput": "nexura_agents.evals",
    "EvalReport": "nexura_agents.evals",
    "EvalComparison": "nexura_agents.evals",
    "EvalRegressionError": "nexura_agents.evals",
    "evaluate": "nexura_agents.evals",
    "evaluate_async": "nexura_agents.evals",
    "assert_no_regressions": "nexura_agents.evals",
    # Errors
    "NexuraError": "nexura_agents.errors",
    "ToolError": "nexura_agents.errors",
    "ProviderError": "nexura_agents.errors",
    "ModelTimeoutError": "nexura_agents.errors",
    "MaxTurnsExceededError": "nexura_agents.errors",
    "MaxHandoffsExceededError": "nexura_agents.errors",
    "UsageLimitExceededError": "nexura_agents.errors",
    "OutputValidationError": "nexura_agents.errors",
    "GuardrailTripwireTriggered": "nexura_agents.errors",
    "ApprovalDecisionError": "nexura_agents.errors",
}

__all__ = [
    "__version__",
    "Agent",
    "Runner",
    "Handoff",
    "handoff",
    "SequentialAgent",
    "ParallelAgent",
    "LoopAgent",
    "Team",
    "TeamResult",
    "TeamStep",
    "TeamInterruption",
    "tool",
    "Tool",
    "RunContext",
    "CancelToken",
    "PromptRegistry",
    "PromptVersion",
    "InMemoryPromptStore",
    "FilePromptStore",
    "Skill",
    "SkillLibrary",
    "ModelRetry",
    "RunResult",
    "RunInterruption",
    "RunEvent",
    "RunEventType",
    "Message",
    "Attachment",
    "ToolCall",
    "Usage",
    "ModelResponse",
    "UsageLimits",
    "ModelSettings",
    "AgentHooks",
    "GuardrailResult",
    "detect_pii",
    "redact_pii",
    "block_pii",
    "redact_output_pii",
    "no_prompt_injection",
    "max_length",
    "allowed_topics",
    "json_output_only",
    "ModelProtocol",
    "FakeModel",
    "OpenAIChatModel",
    "LiteLLMModel",
    "RetryModel",
    "FallbackModel",
    "infer_model",
    "OpenAIProvider",
    "AnthropicProvider",
    "GeminiProvider",
    "SarvamProvider",
    "MCPServer",
    "AgentMCPServer",
    "A2AServer",
    "A2AClient",
    "remote_agent_tool",
    "mcp_tools",
    "openapi_tools",
    "module_tool",
    "SchemaTool",
    "StopAtTools",
    "ToolRetry",
    "ToolOutput",
    "SessionStore",
    "MemorySessionStore",
    "SQLiteSessionStore",
    "fork_session",
    "delete_session",
    "list_sessions",
    "pop_last_turn",
    "retry_last_turn",
    "RateLimiter",
    "RateLimitedModel",
    "GroupChat",
    "GroupChatTurn",
    "MaxTurns",
    "TextMention",
    "TerminationCondition",
    "web_fetch_tool",
    "RunCheckpointStore",
    "keep_last_turns",
    "fit_token_budget",
    "summarize_older_turns",
    "MemoryCheckpointStore",
    "SQLiteCheckpointStore",
    "ResolvedOutcome",
    "RunRecoveryError",
    "ApproveWith",
    "CheckpointLeaseLostError",
    "Memory",
    "MemoryRecord",
    "MemoryStore",
    "InMemoryMemoryStore",
    "HashingEmbedder",
    "BudgetManager",
    "BudgetConfig",
    "ModelPricing",
    "PricingTable",
    "Case",
    "CaseResult",
    "Score",
    "EvalOutput",
    "EvalReport",
    "EvalComparison",
    "EvalRegressionError",
    "evaluate",
    "evaluate_async",
    "assert_no_regressions",
    "NexuraError",
    "ToolError",
    "ProviderError",
    "ModelTimeoutError",
    "MaxTurnsExceededError",
    "MaxHandoffsExceededError",
    "UsageLimitExceededError",
    "OutputValidationError",
    "GuardrailTripwireTriggered",
    "ApprovalDecisionError",
]


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        if not name.startswith("_") and importlib.util.find_spec(f"{__name__}.{name}") is not None:
            return importlib.import_module(f"{__name__}.{name}")
        raise AttributeError(f"module 'nexura_agents' has no attribute {name!r}")
    module_name, _, attr = target.partition(":")
    value = getattr(importlib.import_module(module_name), attr or name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)


if TYPE_CHECKING:
    from nexura_agents.a2a.client import A2AClient, remote_agent_tool
    from nexura_agents.a2a.server import A2AServer
    from nexura_agents.agent import Agent
    from nexura_agents.attachments import Attachment
    from nexura_agents.cancel import CancelToken
    from nexura_agents.durable import (
        MemoryCheckpointStore,
        ResolvedOutcome,
        RunCheckpointStore,
        SQLiteCheckpointStore,
    )
    from nexura_agents.errors import (
        ApprovalDecisionError,
        CheckpointLeaseLostError,
        GuardrailTripwireTriggered,
        MaxTurnsExceededError,
        ModelRetry,
        ModelTimeoutError,
        NexuraError,
        OutputValidationError,
        ProviderError,
        RunRecoveryError,
        ToolError,
        UsageLimitExceededError,
    )
    from nexura_agents.evals import (
        Case,
        CaseResult,
        EvalComparison,
        EvalOutput,
        EvalRegressionError,
        EvalReport,
        Score,
        assert_no_regressions,
        evaluate,
        evaluate_async,
    )
    from nexura_agents.events import RunEvent, RunEventType
    from nexura_agents.guardrails import GuardrailResult
    from nexura_agents.guardrails_builtin import (
        allowed_topics,
        block_pii,
        detect_pii,
        json_output_only,
        max_length,
        no_prompt_injection,
        redact_output_pii,
        redact_pii,
    )
    from nexura_agents.history import fit_token_budget, keep_last_turns, summarize_older_turns
    from nexura_agents.hooks import AgentHooks
    from nexura_agents.limits import ModelSettings, UsageLimits
    from nexura_agents.mcp.serve import AgentMCPServer
    from nexura_agents.mcp.toolset import MCPServer, mcp_tools
    from nexura_agents.memory import (
        HashingEmbedder,
        InMemoryMemoryStore,
        Memory,
        MemoryRecord,
        MemoryStore,
    )
    from nexura_agents.models.base import ModelProtocol
    from nexura_agents.models.fake import FakeModel
    from nexura_agents.models.infer import infer_model
    from nexura_agents.models.litellm import LiteLLMModel
    from nexura_agents.models.openai import OpenAIChatModel
    from nexura_agents.models.ratelimit import RateLimitedModel, RateLimiter
    from nexura_agents.models.resilience import FallbackModel, RetryModel
    from nexura_agents.module_tools import module_tool
    from nexura_agents.openapi.toolset import openapi_tools
    from nexura_agents.prompts import (
        FilePromptStore,
        InMemoryPromptStore,
        PromptRegistry,
        PromptVersion,
    )
    from nexura_agents.providers.anthropic import AnthropicNativeProvider as AnthropicProvider
    from nexura_agents.providers.gemini import GeminiNativeProvider as GeminiProvider
    from nexura_agents.providers.openai import OpenAIProvider
    from nexura_agents.providers.sarvam import SarvamProvider
    from nexura_agents.run_context import RunContext
    from nexura_agents.runner import Runner
    from nexura_agents.session import (
        MemorySessionStore,
        SessionStore,
        SQLiteSessionStore,
        delete_session,
        fork_session,
        list_sessions,
        pop_last_turn,
        retry_last_turn,
    )
    from nexura_agents.skills import Skill, SkillLibrary
    from nexura_agents.teams import (
        GroupChat,
        GroupChatTurn,
        LoopAgent,
        MaxTurns,
        ParallelAgent,
        SequentialAgent,
        Team,
        TeamInterruption,
        TeamResult,
        TeamStep,
        TerminationCondition,
        TextMention,
    )
    from nexura_agents.tools import SchemaTool, StopAtTools, Tool, ToolOutput, ToolRetry, tool
    from nexura_agents.types import (
        ApproveWith,
        Message,
        ModelResponse,
        RunInterruption,
        RunResult,
        ToolCall,
        Usage,
    )
    from nexura_agents.usage.manager import BudgetManager
    from nexura_agents.usage.models import BudgetConfig, ModelPricing
    from nexura_agents.usage.pricing import PricingTable
    from nexura_agents.web_tools import web_fetch_tool
