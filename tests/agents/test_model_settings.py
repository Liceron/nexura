"""Model settings: Runner-wide and per-agent, reaching every kind of model. All offline."""

from __future__ import annotations

import json
import warnings
from typing import Any

import httpx
import pytest
from nexura_agents import Agent, FakeModel, Message, ModelResponse, Runner
from nexura_agents.errors import SDKValidationError
from nexura_agents.limits import ModelSettings
from nexura_agents.models.litellm import LiteLLMModel
from nexura_agents.models.openai import OpenAIChatModel
from nexura_agents.models.resilience import FallbackModel, RetryModel


def settings_sent(model: FakeModel, call: int = 0) -> ModelSettings | None:
    return model.calls[call]["model_settings"]


# ---------------------------------------------------------------- ModelSettings


def test_merged_prefers_fields_set_on_the_override() -> None:
    base = ModelSettings(temperature=0.2, max_tokens=500, stop_sequences=["END"])
    out = base.merged(ModelSettings(max_tokens=50))
    assert out == ModelSettings(temperature=0.2, max_tokens=50, stop_sequences=["END"])
    assert base.merged(None) is base
    assert ModelSettings().merged(ModelSettings(temperature=0.0)).temperature == 0.0


def test_to_openai_params_only_has_set_fields() -> None:
    assert ModelSettings().to_openai_params() == {}
    assert ModelSettings().is_empty()
    params = ModelSettings(temperature=0, max_tokens=10, stop_sequences=["x"]).to_openai_params()
    assert params == {"temperature": 0, "max_tokens": 10, "stop": ["x"]}


def test_agent_validates_model_settings() -> None:
    with pytest.raises(SDKValidationError, match="ModelSettings instance"):
        Agent(name="A", instructions="i", model=FakeModel(), model_settings={"temperature": 1})  # type: ignore[arg-type]


# ---------------------------------------------------------------- in the runner


async def test_agent_settings_override_runner_settings_field_by_field() -> None:
    model = FakeModel(responses=["ok"])
    agent = Agent(
        name="A", instructions="i", model=model, model_settings=ModelSettings(max_tokens=50)
    )
    runner = Runner(model_settings=ModelSettings(temperature=0.2, max_tokens=500))
    await runner.run_async(agent, "hi")
    assert settings_sent(model) == ModelSettings(temperature=0.2, max_tokens=50)


async def test_no_settings_sends_nothing() -> None:
    model = FakeModel(responses=["ok"])
    await Runner().run_async(Agent(name="A", instructions="i", model=model), "hi")
    assert settings_sent(model) is None


async def test_each_agent_in_a_handoff_team_uses_its_own_settings() -> None:
    from nexura_agents import ToolCall

    writer_model = FakeModel(responses=["a poem"])
    writer = Agent(
        name="Writer",
        instructions="Write.",
        model=writer_model,
        model_settings=ModelSettings(temperature=0.9),
    )
    triage_model = FakeModel(
        responses=[[ToolCall(id="h1", name="transfer_to_writer", arguments={})]]
    )
    triage = Agent(
        name="Triage",
        instructions="Route.",
        model=triage_model,
        handoffs=[writer],
        model_settings=ModelSettings(temperature=0.0),
    )
    result = await Runner(model_settings=ModelSettings(max_tokens=200)).run_async(triage, "hi")
    assert result.output == "a poem"
    assert settings_sent(triage_model) == ModelSettings(temperature=0.0, max_tokens=200)
    assert settings_sent(writer_model) == ModelSettings(temperature=0.9, max_tokens=200)


async def test_provider_models_get_merged_settings_in_the_request() -> None:
    seen: list[Any] = []

    class Provider:
        accepts_run_context = True

        async def generate(self, request: Any, *, context: Any = None) -> ModelResponse:
            seen.append(request)
            return ModelResponse(message=Message(role="assistant", content="ok"))

    agent = Agent(
        name="A",
        instructions="i",
        model=Provider(),
        model_settings=ModelSettings(temperature=0.1, stop_sequences=["##"]),
    )
    await Runner(model_settings=ModelSettings(max_tokens=64)).run_async(agent, "hi")
    req = seen[0]
    assert (req.temperature, req.max_tokens, req.stop_sequences) == (0.1, 64, ["##"])


async def test_model_without_settings_support_warns_instead_of_ignoring_silently() -> None:
    class OldModel:
        async def generate(
            self, messages: Any, tools: Any = None, *, timeout: Any = None
        ) -> ModelResponse:
            return ModelResponse(message=Message(role="assistant", content="ok"))

    agent = Agent(name="A", instructions="i", model=OldModel())
    with pytest.warns(RuntimeWarning, match="does not accept model_settings"):
        await Runner(model_settings=ModelSettings(temperature=0.3)).run_async(agent, "hi")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        await Runner().run_async(agent, "hi")  # nothing set: no warning


async def test_retry_and_fallback_wrappers_forward_settings() -> None:
    inner = FakeModel(responses=["ok"], repeat_last=True)
    for wrapped in (RetryModel(inner), FallbackModel(inner)):
        agent = Agent(
            name="A",
            instructions="i",
            model=wrapped,
            model_settings=ModelSettings(max_tokens=7),
        )
        await Runner().run_async(agent, "hi")
        assert inner.calls[-1]["model_settings"] == ModelSettings(max_tokens=7)


async def test_wrapper_around_an_unsupporting_model_warns_and_still_runs() -> None:
    class OldModel:
        async def generate(
            self, messages: Any, tools: Any = None, *, timeout: Any = None
        ) -> ModelResponse:
            return ModelResponse(message=Message(role="assistant", content="ok"))

    agent = Agent(
        name="A",
        instructions="i",
        model=RetryModel(OldModel()),  # type: ignore[arg-type]
        model_settings=ModelSettings(max_tokens=7),
    )
    with pytest.warns(RuntimeWarning):
        result = await Runner().run_async(agent, "hi")
    assert result.output == "ok"


# ---------------------------------------------------------------- real model classes


def _openai(seen: list[dict[str, Any]], streaming: bool = False) -> OpenAIChatModel:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if streaming:
            body = (
                'data: {"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":null}]}\n\n'
                'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
                "data: [DONE]\n\n"
            )
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=body.encode()
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

    return OpenAIChatModel("m", transport=httpx.MockTransport(handler))


async def test_openai_chat_model_sends_settings() -> None:
    seen: list[dict[str, Any]] = []
    agent = Agent(
        name="A",
        instructions="i",
        model=_openai(seen),
        model_settings=ModelSettings(temperature=0.4, max_tokens=99, stop_sequences=["##"]),
    )
    await Runner().run_async(agent, "hi")
    # api.openai.com (the default base_url) takes max_completion_tokens (P37 B3 review R7).
    assert (seen[0]["temperature"], seen[0]["max_completion_tokens"], seen[0]["stop"]) == (
        0.4,
        99,
        ["##"],
    )
    assert "max_tokens" not in seen[0]


async def test_openai_chat_model_streaming_sends_settings() -> None:
    seen: list[dict[str, Any]] = []
    agent = Agent(
        name="A",
        instructions="i",
        model=_openai(seen, streaming=True),
        model_settings=ModelSettings(max_tokens=12),
    )
    events = [e async for e in Runner().stream_async(agent, "hi", stream_text=True)]
    assert events
    assert seen[0]["stream"] is True and seen[0]["max_completion_tokens"] == 12
    assert "max_tokens" not in seen[0]


async def test_openai_chat_model_without_settings_sends_no_extra_fields() -> None:
    seen: list[dict[str, Any]] = []
    await Runner().run_async(Agent(name="A", instructions="i", model=_openai(seen)), "hi")
    assert not {"temperature", "max_tokens", "stop"} & set(seen[0])


async def test_litellm_settings_override_constructor_options() -> None:
    calls: list[dict[str, Any]] = []

    async def completion(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    model = LiteLLMModel("ollama/x", completion=completion, temperature=0.9, top_p=0.5)
    agent = Agent(
        name="A", instructions="i", model=model, model_settings=ModelSettings(temperature=0.1)
    )
    await Runner().run_async(agent, "hi")
    assert calls[0]["temperature"] == 0.1  # the agent's setting wins for this call
    assert calls[0]["top_p"] == 0.5  # other constructor options are kept


async def test_legacy_stream_without_settings_support_warns() -> None:
    from nexura_agents.providers.types import ProviderCapabilities, StreamDelta

    class OldStreamer:
        capabilities = ProviderCapabilities(can_stream=True)

        async def generate(self, messages: Any, tools: Any = None, **kw: Any) -> ModelResponse:
            raise AssertionError("stream_text should use stream()")

        async def stream(self, messages: Any, tools: Any = None, *, timeout: Any = None) -> Any:
            yield StreamDelta(kind="text_delta", text="ok")
            yield StreamDelta(kind="finish", finish_reason="stop")

    agent = Agent(
        name="A",
        instructions="i",
        model=OldStreamer(),
        model_settings=ModelSettings(temperature=0.5),
    )
    with pytest.warns(RuntimeWarning, match=r"OldStreamer\.stream does not accept"):
        events = [e async for e in Runner().stream_async(agent, "hi", stream_text=True)]
    assert events


# ---------------------------------------------------------------- validation (audit 4)


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"stop_sequences": "END"}, "list of strings"),
        ({"stop_sequences": ["END", ""]}, "non-empty strings"),
        ({"stop_sequences": [1]}, "non-empty strings"),
        ({"temperature": -0.1}, "temperature"),
        ({"temperature": True}, "temperature"),
        ({"temperature": "0.5"}, "temperature"),
        ({"max_tokens": 0}, "max_tokens"),
        ({"max_tokens": 1.5}, "max_tokens"),
        ({"max_tokens": True}, "max_tokens"),
    ],
)
def test_invalid_model_settings_are_rejected(kwargs: dict[str, Any], match: str) -> None:
    with pytest.raises(SDKValidationError, match=match):
        ModelSettings(**kwargs)


def test_stop_sequences_are_copied() -> None:
    stops = ["END"]
    settings = ModelSettings(stop_sequences=stops)
    stops.append("MORE")
    assert settings.stop_sequences == ["END"]
    assert ModelSettings(stop_sequences=("A", "B")).stop_sequences == ["A", "B"]
