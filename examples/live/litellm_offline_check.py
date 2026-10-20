"""Drive LiteLLMModel through the REAL litellm using its built-in mock_response (no network).

Run (not part of pytest):

    LITELLM_LOCAL_MODEL_COST_MAP=True \\
    uv run --frozen --package nexura-agents --extra litellm \\
    python examples/live/litellm_offline_check.py
"""

from __future__ import annotations

import asyncio
import os

from nexura_agents import LiteLLMModel, Message
from nexura_agents.errors import ProviderAuthError

assert os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP", "").lower() == "true", (
    "set LITELLM_LOCAL_MODEL_COST_MAP=True"
)

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "add",
        "description": "Add two numbers.",
        "parameters": {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    },
}


async def main() -> None:
    # Text reply: mock_response is forwarded to litellm.acompletion via **options.
    text_model = LiteLLMModel("openai/gpt-4o-mini", api_key="unused", mock_response="mocked pong")
    out = await text_model.generate([Message(role="user", content="ping")])
    print("text:", out.message.content, "| finish:", out.finish_reason, "| usage:", out.usage)
    assert out.message.content == "mocked pong"

    # Tool call: litellm's mock_tool_calls builds ChatCompletionMessageToolCall objects.
    tool_model = LiteLLMModel(
        "openai/gpt-4o-mini",
        api_key="unused",
        mock_response="",
        mock_tool_calls=[
            {
                "id": "call_mock_1",
                "type": "function",
                "function": {"name": "add", "arguments": '{"a": 2, "b": 3}'},
            }
        ],
    )
    out = await tool_model.generate([Message(role="user", content="2+3?")], [TOOL_DEF])
    print("tool_calls:", out.message.tool_calls)
    assert out.message.tool_calls and out.message.tool_calls[0].arguments == {"a": 2, "b": 3}

    # Streaming: litellm's mock_response also streams (fake chunks, no network).
    stream_model = LiteLLMModel("openai/gpt-4o-mini", api_key="unused", mock_response="mocked pong")
    deltas = [d async for d in stream_model.stream([Message(role="user", content="ping")])]
    streamed = "".join(d.text or "" for d in deltas if d.kind == "text_delta")
    print("streamed:", streamed, "| deltas:", len(deltas))
    assert streamed == "mocked pong"
    assert any(d.finish_reason for d in deltas)

    # Error mapping through a real litellm exception (mock_response=Exception subclass).
    import litellm

    err_model = LiteLLMModel(
        "openai/gpt-4o-mini",
        api_key="unused",
        mock_response=litellm.AuthenticationError(
            message="bad key", llm_provider="openai", model="gpt-4o-mini"
        ),
    )
    try:
        await err_model.generate([Message(role="user", content="x")])
    except ProviderAuthError as exc:
        print("auth error mapped:", type(exc).__name__)
    else:
        raise AssertionError("expected ProviderAuthError")
    from importlib.metadata import version

    print("OK (litellm", version("litellm") + ")")


asyncio.run(main())
