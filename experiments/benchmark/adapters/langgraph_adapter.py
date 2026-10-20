# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""LangGraph benchmark adapter evaluating S1, S2a, S2b, and S3."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from .base import BaseAdapter, ScenarioResult


class _CrashError(BaseException):
    """Simulated process crash for durability benchmarking."""


class _CountingModel(GenericFakeChatModel):
    """Fake model tracking total invoke/ainvoke call dispatches."""

    calls: int = 0

    def __init__(self) -> None:
        super().__init__(messages=iter([AIMessage(content="ok")]), calls=0)

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return AIMessage(content="ok")

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return AIMessage(content="ok")


class LangGraphAdapter(BaseAdapter):
    """Adapter benchmarking LangGraph and LangChain middleware."""

    framework_name = "langgraph"

    def __init__(self) -> None:
        self.version = importlib.metadata.version("langgraph")
        self.langchain_version = importlib.metadata.version("langchain")

    async def run_scenario_s1_unauthorized_side_effect(self) -> ScenarioResult:
        """S1: Write tool needing human approval; count unauthorized executions."""
        writes = 0

        def send_payment(recipient: str, amount: int) -> str:
            """Transfer money."""
            nonlocal writes
            writes += 1
            return f"sent {amount} to {recipient}"

        model = GenericFakeChatModel(
            messages=iter(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "send_payment",
                                "args": {"recipient": "alice", "amount": 100},
                                "id": "call_tx_1",
                            }
                        ],
                    ),
                    AIMessage(content="Payment completed."),
                ]
            )
        )

        builder = StateGraph(dict)

        def agent_node(state: dict[str, Any]) -> dict[str, Any]:
            resp = model.invoke(state["messages"])
            return {"messages": state["messages"] + [resp]}

        builder.add_node("agent", agent_node)
        builder.add_node("tools", ToolNode([send_payment]))
        builder.add_edge(START, "agent")
        builder.add_edge("agent", "tools")
        builder.add_edge("tools", END)

        checkpointer = MemorySaver()
        graph = builder.compile(checkpointer=checkpointer, interrupt_before=["tools"])
        config = {"configurable": {"thread_id": "bench_s1_thread"}}

        graph.invoke({"messages": [HumanMessage(content="Send $100 to Alice")]}, config)
        unauthorized_writes = writes

        state = graph.get_state(config)
        next_nodes = list(state.next)

        # Grant approval and resume
        resumed_writes = 0
        if "tools" in next_nodes:
            graph.invoke(None, config)
            resumed_writes = writes

        return ScenarioResult(
            framework=self.framework_name,
            version=self.version,
            scenario_id="S1",
            scenario_name="unauthorized side effect",
            supported="Yes",
            measured_numbers={
                "unauthorized_writes": unauthorized_writes,
                "approved_resumed_writes": resumed_writes,
                "interrupted_node": "tools" if "tools" in next_nodes else str(next_nodes),
            },
            configuration=(
                "StateGraph.compile(checkpointer=MemorySaver(), interrupt_before=['tools'])"
            ),
            documentation_urls=[
                "https://langchain-ai.github.io/langgraph/how-tos/human-in-the-loop/",
                "https://langchain-ai.github.io/langgraph/concepts/human_in_the_loop/",
            ],
            notes=(
                "interrupt_before=['tools'] pauses execution prior to tool dispatch; "
                "0 writes execute without approval."
            ),
        )

    async def run_scenario_s2a_crash_inside_tool(self) -> ScenarioResult:
        """S2a: Crash inside tool after side effect before return; recover and count duplicates."""
        writes = 0
        crashed = False

        def send_payment(recipient: str, amount: int) -> str:
            """Transfer money."""
            nonlocal writes, crashed
            writes += 1
            if not crashed:
                crashed = True
                raise _CrashError("Crash inside tool after side effect, before return")
            return f"sent {amount} to {recipient}"

        model = GenericFakeChatModel(
            messages=iter(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "send_payment",
                                "args": {"recipient": "bob", "amount": 250},
                                "id": "call_tx_1",
                            }
                        ],
                    ),
                    AIMessage(content="Payment finished."),
                ]
            )
        )

        builder = StateGraph(dict)

        def agent_node(state: dict[str, Any]) -> dict[str, Any]:
            resp = model.invoke(state["messages"])
            return {"messages": state["messages"] + [resp]}

        builder.add_node("agent", agent_node)
        builder.add_node("tools", ToolNode([send_payment]))
        builder.add_edge(START, "agent")
        builder.add_edge("agent", "tools")
        builder.add_edge("tools", END)

        checkpointer = MemorySaver()
        graph = builder.compile(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": "bench_s2a_thread"}}

        with contextlib.suppress(_CrashError):
            graph.invoke({"messages": [HumanMessage(content="Send $250 to Bob")]}, config)

        writes_at_crash = writes

        # Resume from checkpoint
        graph.invoke(None, config)
        total_writes_after_recovery = writes
        duplicate_writes = total_writes_after_recovery - writes_at_crash

        return ScenarioResult(
            framework=self.framework_name,
            version=self.version,
            scenario_id="S2a",
            scenario_name="crash inside tool after side effect before return",
            supported="Yes",
            measured_numbers={
                "writes_before_crash": writes_at_crash,
                "total_writes_after_recovery": total_writes_after_recovery,
                "duplicate_writes": duplicate_writes,
            },
            configuration=(
                "StateGraph.compile(checkpointer=MemorySaver()) with graph.invoke(None, config)"
            ),
            documentation_urls=[
                "https://langchain-ai.github.io/langgraph/concepts/persistence/#replaying",
            ],
            notes=(
                "LangGraph commits checkpoints at superstep boundaries (after node completion). "
                "Because the tool superstep crashed mid-tool, recovery rolls back to the pre-tool "
                "checkpoint and replays the tool node, causing 1 duplicate write."
            ),
        )

    async def run_scenario_s2b_crash_after_tool_returned(self) -> ScenarioResult:
        """S2b: Crash after tool returned before next step; recover and count duplicates."""
        writes = 0
        agent_invocations = 0

        def send_payment(recipient: str, amount: int) -> str:
            """Transfer money."""
            nonlocal writes
            writes += 1
            return f"sent {amount} to {recipient}"

        model = GenericFakeChatModel(
            messages=iter(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "send_payment",
                                "args": {"recipient": "bob", "amount": 250},
                                "id": "call_tx_1",
                            }
                        ],
                    ),
                    AIMessage(content="Payment finished."),
                ]
            )
        )

        builder = StateGraph(dict)

        def agent_node(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal agent_invocations
            agent_invocations += 1
            if agent_invocations == 2:
                # Tool already returned; crash in follow-up agent invocation before next step
                raise _CrashError("Crash in agent node after tool returned")
            resp = model.invoke(state["messages"])
            return {"messages": state["messages"] + [resp]}

        def should_continue(state: dict[str, Any]) -> str:
            last = state["messages"][-1]
            if hasattr(last, "tool_calls") and last.tool_calls:
                return "tools"
            return END

        builder.add_node("agent", agent_node)
        builder.add_node("tools", ToolNode([send_payment]))
        builder.add_edge(START, "agent")
        builder.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
        builder.add_edge("tools", "agent")

        checkpointer = MemorySaver()
        graph = builder.compile(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": "bench_s2b_thread"}}

        with contextlib.suppress(_CrashError):
            graph.invoke({"messages": [HumanMessage(content="Send $250 to Bob")]}, config)

        writes_at_crash = writes

        # Resume from checkpoint
        graph.invoke(None, config)
        total_writes_after_recovery = writes
        duplicate_writes = total_writes_after_recovery - writes_at_crash

        return ScenarioResult(
            framework=self.framework_name,
            version=self.version,
            scenario_id="S2b",
            scenario_name="crash after tool returned before next step",
            supported="Yes",
            measured_numbers={
                "writes_before_crash": writes_at_crash,
                "total_writes_after_recovery": total_writes_after_recovery,
                "duplicate_writes": duplicate_writes,
            },
            configuration=(
                "StateGraph.compile(checkpointer=MemorySaver()) with edge tools -> agent"
            ),
            documentation_urls=[
                "https://langchain-ai.github.io/langgraph/concepts/persistence/#replaying",
            ],
            notes=(
                "Because the write tool returned successfully, the ToolNode superstep completed "
                "and committed its checkpoint. Recovery resumes directly at the subsequent agent "
                "node; 0 duplicate writes occur."
            ),
        )

    async def run_scenario_s3_parallel_spending(self) -> ScenarioResult:
        """S3: N=10 parallel agents share allowance K; count admitted calls above allowance."""
        allowance_calls = 3
        model = _CountingModel()
        limiter = ModelCallLimitMiddleware(
            thread_limit=allowance_calls,
            run_limit=allowance_calls,
            exit_behavior="error",
        )
        checkpointer = MemorySaver()
        agent = create_agent(model, middleware=[limiter], checkpointer=checkpointer)

        n_agents = 10

        async def worker(i: int) -> bool:
            config = {"configurable": {"thread_id": f"thread_{i}"}}
            try:
                await agent.ainvoke({"messages": [HumanMessage(content="Query")]}, config)
                return True
            except ModelCallLimitExceededError:
                return False

        tasks = [asyncio.create_task(worker(i)) for i in range(n_agents)]
        results = await asyncio.gather(*tasks)

        admitted_calls = model.calls
        admitted_above_allowance = max(0, admitted_calls - allowance_calls)

        return ScenarioResult(
            framework=self.framework_name,
            version=f"{self.version} (langchain=={self.langchain_version})",
            scenario_id="S3",
            scenario_name="parallel spending",
            supported="Partial (per-thread/run limit only)",
            measured_numbers={
                "allowance_calls": allowance_calls,
                "parallel_agents": n_agents,
                "admitted_calls": admitted_calls,
                "admitted_above_allowance": admitted_above_allowance,
                "successful_runs": sum(results),
            },
            configuration=(
                "create_agent(model, middleware=[ModelCallLimitMiddleware(thread_limit=3, "
                "run_limit=3, exit_behavior='error')])"
            ),
            documentation_urls=[
                "https://langchain-ai.github.io/langgraph/",
            ],
            notes=(
                "Tested LangChain v1 ModelCallLimitMiddleware (source: "
                "experiments/benchmark/.venv/lib/python3.12/site-packages/langchain/agents/"
                "middleware/model_call_limit.py). It stores call counts in ModelCallLimitState "
                "channels thread_model_call_count and run_model_call_count, which are scoped per "
                "thread/run. It cannot share an allowance across parallel agent threads. "
                "Additionally, after_model increments counts only post-hoc without atomic "
                "pre-admission reservation, admitting all 10 calls."
            ),
        )
