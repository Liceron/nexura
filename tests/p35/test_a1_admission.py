"""P35 A1: local fake models / mock HTTP only; no paid provider calls."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from nexura_agents import Agent, FakeModel, Message, Runner, ToolCall, Usage, tool
from nexura_agents.errors import ProviderRequestError
from nexura_agents.models.litellm import LiteLLMModel
from nexura_agents.models.openai import OpenAIChatModel
from nexura_agents.models.ratelimit import RateLimitedModel, RateLimiter
from nexura_agents.models.resilience import FallbackModel, RetryModel
from nexura_agents.providers.base import BaseProvider
from nexura_agents.providers.types import (
    EmbeddingResult,
    GenerationRequest,
    ProviderCapabilities,
    StreamDelta,
)
from nexura_agents.types import ModelResponse
from nexura_agents.usage import BudgetConfig, BudgetManager, UsageLedger
from nexura_agents.usage.errors import BudgetExceededError, InvalidPricingError, UsageError
from nexura_agents.usage.models import ModelPricing

REPORTED = Usage(prompt_tokens=7, completion_tokens=3, total_tokens=10)


class LocalProvider(BaseProvider):
    def __init__(self, dispatched: list[int]) -> None:
        super().__init__("local-test", ProviderCapabilities(can_stream=True, can_embed=True))
        self.dispatched = dispatched

    async def _generate_internal(self, request: GenerationRequest, context: Any) -> ModelResponse:
        self.dispatched.append(1)
        return ModelResponse(Message("assistant", "ok"), usage=REPORTED)

    async def _stream_internal(
        self, request: GenerationRequest, context: Any
    ) -> AsyncIterator[StreamDelta]:
        self.dispatched.append(1)
        yield StreamDelta(kind="text_delta", text="ok")
        yield StreamDelta(kind="usage_delta", usage=REPORTED)

    async def _embed_internal(self, texts: list[str], context: Any) -> EmbeddingResult:
        self.dispatched.append(1)
        return EmbeddingResult(embeddings=[[1.0]], model=self.model_id, usage=REPORTED)


def budget(**kwargs: Any) -> Any:
    manager = BudgetManager()
    if "max_cost_usd" not in kwargs:
        for model_id in ("local-test", "fake-model"):
            manager.pricing_table.add_model_pricing(
                ModelPricing(
                    model_id=model_id,
                    input_cost_per_million=Decimal("0"),
                    output_cost_per_million=Decimal("0"),
                )
            )
    return manager.create_run_budget("w", "r", config=BudgetConfig(**kwargs))


def model_fixture(kind: str, dispatched: list[int], check: Any = None) -> Any:
    def observe() -> None:
        if check:
            check()
        dispatched.append(1)

    response = {
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }
    chunks = [
        {"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": response["usage"]},
    ]
    if kind == "openai":

        def transport(request: httpx.Request) -> httpx.Response:
            observe()
            if json.loads(request.content).get("stream"):
                body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
                return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json=response)

        return OpenAIChatModel("local-test", transport=httpx.MockTransport(transport))
    if kind == "litellm":

        async def completion(**kwargs: Any) -> Any:
            observe()
            if kwargs.get("stream"):

                async def iterate() -> AsyncIterator[Any]:
                    for chunk in chunks:
                        yield chunk

                return iterate()
            return response

        return LiteLLMModel("local-test", completion=completion)
    if kind == "fake":

        def handler(messages: Any, tools: Any) -> str:
            observe()
            return "ok"

        return FakeModel(handler=handler, default_usage=REPORTED, stream=True)
    provider = LocalProvider(dispatched)
    if check:
        original = provider._generate_internal

        async def generate(request: GenerationRequest, context: Any) -> ModelResponse:
            check()
            return await original(request, context)

        provider._generate_internal = generate  # type: ignore[method-assign]
    return provider


@pytest.mark.parametrize("kind", ["openai", "litellm", "fake", "provider"])
async def test_each_model_reserves_settles_and_refuses(kind: str) -> None:
    allowed = budget(max_tokens=10000)
    dispatched: list[int] = []

    def reserved() -> None:
        assert allowed.get_status().total_reserved_tokens > 0

    model = model_fixture(kind, dispatched, reserved)
    agent = Agent(name="A", instructions="", model=model)
    result = await Runner(budget=allowed).run_async(agent, "hello")
    assert result.output == "ok"
    assert dispatched == [1]
    assert allowed.get_status().total_reserved_tokens == 0
    assert allowed.get_status().total_tokens == 10
    with pytest.raises(BudgetExceededError):
        await Runner(budget=budget(max_tokens=1)).run_async(agent, "refuse")
    with pytest.raises(UsageError, match="cannot reserve"):
        await Runner(
            budget=SimpleNamespace(record_turn=lambda: None, settle_provider_call=lambda: None)
        ).run_async(agent, "no")
    with pytest.raises(InvalidPricingError):
        await Runner(budget=budget(max_cost_usd=Decimal("1"))).run_async(agent, "unknown price")
    assert dispatched == [1]
    close = getattr(model, "close", None)
    if close:
        await close()


async def test_unidentified_legacy_model_refused_before_dispatch() -> None:
    class Unidentified:
        async def generate(self, *args: Any, **kwargs: Any) -> ModelResponse:
            pytest.fail("unidentified model dispatched")

    with pytest.raises(UsageError, match="model_id"):
        await Runner(budget=budget(max_tokens=10000)).run_async(
            Agent(name="A", instructions="", model=Unidentified()), "no"
        )


@pytest.mark.parametrize("kind", ["openai", "litellm", "fake", "provider"])
async def test_stream_admission_and_settlement(kind: str) -> None:
    allowed = budget(max_tokens=10000)
    dispatched: list[int] = []
    agent = Agent(name="A", instructions="", model=model_fixture(kind, dispatched))
    events = [
        event
        async for event in Runner(budget=allowed).stream_async(agent, "hello", stream_text=True)
    ]
    assert events
    assert dispatched == [1]
    assert allowed.get_status().total_tokens == 10
    assert allowed.get_status().total_reserved_tokens == 0
    with pytest.raises(BudgetExceededError):
        async for _ in Runner(budget=budget(max_tokens=1)).stream_async(
            agent, "refuse", stream_text=True
        ):
            pass
    assert dispatched == [1]
    close = getattr(agent.model, "close", None)
    if close:
        await close()


@pytest.mark.parametrize("kind", ["openai", "litellm", "fake", "provider"])
async def test_failure_finalizes_reservation(kind: str) -> None:
    allowed = budget(max_tokens=10000)

    def fail() -> None:
        assert allowed.get_status().total_reserved_tokens > 0
        raise ProviderRequestError("local fake failed after admission")

    model = model_fixture(kind, [], fail)
    with pytest.raises(ProviderRequestError):
        await Runner(budget=allowed).run_async(Agent(name="A", instructions="", model=model), "go")
    assert allowed.get_status().total_reserved_tokens == 0
    assert allowed.get_status().total_tokens > 0
    close = getattr(model, "close", None)
    if close:
        await close()


async def test_judge_team_handoff_and_refused_embedding_inherit_budget() -> None:
    from nexura_agents import Case, ParallelAgent
    from nexura_agents.evals import llm_judge
    from nexura_agents.memory import ProviderEmbedder

    allowed = budget(max_tokens=40000)
    judge_model = FakeModel(responses=['{"score": 1, "reason": "ok"}'], default_usage=REPORTED)
    judged = llm_judge(judge_model, "test rubric")
    target = Agent(
        name="target",
        instructions="",
        model=FakeModel(responses=["target"], default_usage=REPORTED),
    )
    source = Agent(
        name="source",
        instructions="",
        handoffs=[target],
        model=FakeModel(
            responses=[[ToolCall("h", "transfer_to_target", {"reason": "test"})]],
            default_usage=REPORTED,
        ),
    )
    team = ParallelAgent(name="team", agents=[source])
    dispatched: list[int] = []

    @tool
    async def auxiliary() -> str:
        """Run an offline team, judge and embedding inside a run."""
        await team.run_async("go")
        await judged(Case(name="local", input="x"), SimpleNamespace(final_output="x"))
        await ProviderEmbedder(LocalProvider(dispatched)).embed(["x"])
        return "ok"

    parent = Agent(
        name="parent",
        instructions="",
        tools=[auxiliary],
        model=FakeModel(
            responses=[[ToolCall("a", "auxiliary", {})], "done"],
            default_usage=REPORTED,
        ),
    )
    await Runner(budget=allowed).run_async(parent, "go")
    assert allowed.get_status().total_tokens == 60
    assert len(judge_model.calls) == 1 and dispatched == [1]


async def test_wrapped_retry_and_fallback_admit_every_attempt() -> None:
    allowed = budget(max_tokens=30000)
    reservations: list[int] = []

    def failing(messages: Any, tools: Any) -> str:
        reservations.append(allowed.get_status().total_reserved_tokens)
        raise ProviderRequestError("offline failure")

    primary = FakeModel(handler=failing)
    secondary = FakeModel(responses=["ok"], default_usage=REPORTED)
    wrapped = FallbackModel(
        RetryModel(RateLimitedModel(primary, RateLimiter()), max_attempts=2, base_delay=0),
        secondary,
    )
    await Runner(budget=allowed).run_async(Agent(name="A", instructions="", model=wrapped), "go")
    assert len(reservations) == 2
    assert all(n > 0 for n in reservations)
    assert allowed.get_status().total_reserved_tokens == 0
    # Failed, potentially billed calls retain a conservative debit, not an active reservation.
    assert allowed.get_status().total_tokens > 10
    assert len(primary.calls) == 2 and len(secondary.calls) == 1


async def test_nested_agent_as_tool_and_embeddings_inherit_budget() -> None:
    allowed = budget(max_tokens=30000)
    dispatched: list[int] = []
    provider = LocalProvider(dispatched)

    @tool
    async def embed() -> str:
        """Embed with the local fake provider inside a run."""
        await provider.embed(["test"])
        return "embedded"

    child_model = FakeModel(responses=["child"], default_usage=REPORTED)
    child = Agent(name="child", instructions="", model=child_model)
    parent = Agent(
        name="parent",
        instructions="",
        tools=[child.as_tool(), embed],
        model=FakeModel(
            responses=[
                [ToolCall("c", "child", {"request": "go"}), ToolCall("e", "embed", {})],
                "done",
            ],
            default_usage=REPORTED,
        ),
    )
    await Runner(budget=allowed).run_async(parent, "go")
    assert allowed.get_status().total_tokens == 40
    assert len(child_model.calls) == 1 and dispatched == [1]


async def test_restart_restores_policy_and_spend(tmp_path: Path) -> None:
    from nexura_runtime.service.api.runs import _budget_manager, _build_engine
    from nexura_runtime.workflow import ModuleRegistry, WorkflowNode, WorkflowSpec

    path = str(tmp_path / "local-service.sqlite")
    state = SimpleNamespace(store=SimpleNamespace(db_path=path))
    manager = _budget_manager(state)
    manager.set_workspace_budget("w", BudgetConfig(max_tokens=10000))
    original = manager.create_run_budget("w", "r", config=BudgetConfig(max_tokens=5000))
    call = original.admit_provider_call("fake-model", estimated_tokens=4900)
    original.settle_provider_call(call, usage=Usage(total_tokens=4900))
    registry = ModuleRegistry()
    registry.register("local", lambda state: state)
    restarted_state = SimpleNamespace(
        store=SimpleNamespace(db_path=path, checkpoint_store=None),
        registry=registry,
    )
    restarted = _budget_manager(restarted_state)
    restored = restarted.get_run_budget("r")
    assert restored.config.max_tokens == 5000
    assert restarted.get_workspace_config("w").max_tokens == 10000
    assert restored.get_status().total_tokens == 4900
    engine, _ = _build_engine(
        SimpleNamespace(app=SimpleNamespace(state=restarted_state)),
        WorkflowSpec(
            name="test",
            entry_node="local",
            nodes={
                "local": WorkflowNode(id="local", handler="local"),
            },
        ),
        "r",
        "w",
    )
    assert engine.budget is restored
    with pytest.raises(BudgetExceededError):
        await Runner(budget=restored).run_async(
            Agent(name="A", instructions="", model=FakeModel(responses=["never"])), "go"
        )
    independent = BudgetManager(ledger=UsageLedger(path))
    assert independent.create_run_budget("w", "r", config=BudgetConfig()).config.max_tokens == 5000


async def test_custom_memory_embedding_fails_closed_without_identity() -> None:
    from nexura_agents.memory import InMemoryMemoryStore

    class UnknownEmbedder:
        async def embed(self, texts: list[str]) -> list[list[float]]:
            pytest.fail("unidentified embedding dispatched")

    memory = InMemoryMemoryStore(UnknownEmbedder())

    @tool
    async def remember() -> str:
        """Exercise the SDK's custom embedder path under an active run budget."""
        with pytest.raises(UsageError, match="embedding model needs"):
            await memory.add("local", "test")
        return "refused"

    parent = Agent(
        name="A",
        instructions="",
        tools=[remember],
        model=FakeModel(
            responses=[[ToolCall("m", "remember", {})], "done"],
            default_usage=REPORTED,
        ),
    )
    await Runner(budget=budget(max_tokens=15000)).run_async(parent, "go")


@pytest.mark.parametrize("kind", ["fake", "provider", "retry", "fallback"])
async def test_recording_wrappers_preserve_identity_and_admit_once(kind: str) -> None:
    from nexura_agents.repro import ReproRecorder, _RecordingModel
    from nexura_agents.usage import admission

    allowed = budget(max_tokens=20000)
    dispatched: list[int] = []
    inner = model_fixture("provider" if kind == "provider" else "fake", dispatched)
    if kind == "retry":
        inner = RetryModel(inner)
    elif kind == "fallback":
        inner = FallbackModel(inner)
    original = Agent(name="A", instructions="", model=inner)
    first = ReproRecorder()
    first.data["model_responses"] = []  # normally initialized by run_agent
    wrapped = _RecordingModel(first, original)
    second = ReproRecorder()
    second.data["model_responses"] = []
    wrapped_twice = _RecordingModel(second, original.clone(model=wrapped))
    # Nested recorders still dispatch through both hooks; admission belongs to the leaf.
    token = admission.active_budget.set(allowed)
    try:
        response = await admission.generate(wrapped_twice, [Message(role="user", content="local")])
    finally:
        admission.active_budget.reset(token)
    assert response.message.content == "ok" and dispatched == [1]
    assert len(first.data["model_responses"]) == len(second.data["model_responses"]) == 1
    entries = allowed.manager.ledger.get_entries(workspace_id="w", run_id="r")
    assert sum(entry.entry_type == "reservation" for entry in entries) == 1
    assert sum(entry.entry_type == "reconcile" for entry in entries) == 1
    assert allowed.get_status().total_tokens == 10
