"""Comprehensive acceptance and unit tests for Module B12: Native Anthropic Adapter."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from nexura_runtime.agent import Agent
from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.errors import (
    ProviderAuthError,
    ProviderRequestError,
    ProviderResponseError,
)
from nexura_runtime.providers.anthropic.module import AnthropicProviderModule
from nexura_runtime.providers.anthropic.provider import AnthropicNativeProvider
from nexura_runtime.providers.anthropic.types import (
    extract_anthropic_response,
    map_anthropic_stop_reason,
    to_anthropic_messages,
)
from nexura_runtime.providers.errors import CapabilityError, StreamCancelledError
from nexura_runtime.providers.types import (
    GenerationRequest,
    ProviderCapabilities,
    ProviderMessage,
    TextPart,
    ToolCallPart,
)
from nexura_runtime.runner import Runner
from nexura_runtime.tools import tool
from nexura_runtime.types import Message, ToolCall

from nexura_runtime.modules.registry import default_registry


@pytest.mark.asyncio
async def test_mixed_text_and_tool_blocks_survive_replay() -> None:
    """Acceptance case: Mixed text and tool_use blocks survive replay across turns."""
    # Turn 1: User asks a question
    user_msg = Message(role="user", content="Calculate latency for node 3.")

    # Assistant generates text followed by tool call
    asst_msg = Message(
        role="assistant",
        content="I will check the latency diagnostics.",
        tool_calls=[
            ToolCall(
                id="toolu_lat_100",
                name="measure_latency",
                arguments={"node_id": 3},
            )
        ],
    )

    # Tool execution output
    tool_msg = Message(
        role="tool",
        tool_call_id="toolu_lat_100",
        name="measure_latency",
        content=json.dumps({"latency_ms": 4.5, "status": "nominal"}),
    )

    history = [user_msg, asst_msg, tool_msg]
    sys_prompt, anth_messages = to_anthropic_messages(history)

    assert sys_prompt is None
    # Expect 3 turns: user turn, assistant turn (mixed text + tool_use), user turn (tool_result)
    assert len(anth_messages) == 3

    # Turn 1 user
    assert anth_messages[0]["role"] == "user"
    assert anth_messages[0]["content"][0]["text"] == "Calculate latency for node 3."

    # Turn 2 assistant (mixed text + tool_use)
    assert anth_messages[1]["role"] == "assistant"
    asst_blocks = anth_messages[1]["content"]
    assert len(asst_blocks) == 2
    assert asst_blocks[0] == {
        "type": "text",
        "text": "I will check the latency diagnostics.",
    }
    assert asst_blocks[1] == {
        "type": "tool_use",
        "id": "toolu_lat_100",
        "name": "measure_latency",
        "input": {"node_id": 3},
    }

    # Turn 3 user containing tool_result
    assert anth_messages[2]["role"] == "user"
    tool_blocks = anth_messages[2]["content"]
    assert len(tool_blocks) == 1
    assert tool_blocks[0]["type"] == "tool_result"
    assert tool_blocks[0]["tool_use_id"] == "toolu_lat_100"
    assert "latency_ms" in tool_blocks[0]["content"]

    # Also verify using ProviderMessage with ContentParts directly
    prov_asst = ProviderMessage(
        role="assistant",
        parts=[
            TextPart(text="Analyzing telemetry."),
            ToolCallPart(id="toolu_tel_200", name="get_telemetry", arguments={"metric": "cpu"}),
        ],
    )
    _, prov_anth = to_anthropic_messages([prov_asst])
    assert len(prov_anth[0]["content"]) == 2
    assert prov_anth[0]["content"][0]["type"] == "text"
    assert prov_anth[0]["content"][1]["type"] == "tool_use"


@pytest.mark.asyncio
async def test_missing_or_duplicate_tool_ids_fail() -> None:
    """Acceptance case: Missing or duplicate tool IDs fail with explicit errors."""
    # 1. Missing tool ID in request tool call
    bad_msg1 = Message(
        role="assistant",
        content="calling tool",
        tool_calls=[ToolCall(id="", name="test_tool", arguments={})],
    )
    with pytest.raises(ProviderRequestError, match="missing valid non-empty ID"):
        to_anthropic_messages([bad_msg1])

    # 2. Duplicate tool IDs in request
    dup_tc1 = ToolCall(id="toolu_duplicate", name="tool_a", arguments={})
    dup_tc2 = ToolCall(id="toolu_duplicate", name="tool_b", arguments={})
    bad_msg2 = Message(
        role="assistant",
        content="calling two tools",
        tool_calls=[dup_tc1, dup_tc2],
    )
    with pytest.raises(ProviderRequestError, match="Duplicate tool use ID 'toolu_duplicate'"):
        to_anthropic_messages([bad_msg2])

    # 3. Missing tool_use_id in tool result message
    bad_tool_msg = Message(role="tool", tool_call_id="", content="result")
    with pytest.raises(ProviderRequestError, match="missing valid.*tool"):
        to_anthropic_messages([bad_tool_msg])

    # 4. Response with missing tool ID
    bad_response_payload: dict[str, Any] = {
        "id": "msg_err_01",
        "type": "message",
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "",
                "name": "tool_a",
                "input": {},
            }
        ],
    }
    with pytest.raises(ProviderResponseError, match="missing or empty ID"):
        extract_anthropic_response(bad_response_payload)

    # 5. Response with duplicate tool IDs
    dup_resp_payload: dict[str, Any] = {
        "id": "msg_err_02",
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "toolu_same", "name": "t1", "input": {}},
            {"type": "tool_use", "id": "toolu_same", "name": "t2", "input": {}},
        ],
    }
    with pytest.raises(ProviderResponseError, match="Duplicate tool use ID 'toolu_same'"):
        extract_anthropic_response(dup_resp_payload)


@pytest.mark.asyncio
async def test_max_token_and_refusal_termination_not_silently_success() -> None:
    """Acceptance case: Max-token and refusal termination is not silently full success."""
    # 1. max_tokens stop reason maps to 'length'
    assert map_anthropic_stop_reason("max_tokens") == "length"
    assert map_anthropic_stop_reason("max_tokens") != "stop"

    # 2. refusal stop reason maps to 'refusal'
    assert map_anthropic_stop_reason("refusal") == "refusal"

    # 3. tool_use stop reason maps to 'tool_calls'
    assert map_anthropic_stop_reason("tool_use") == "tool_calls"

    # 4. end_turn maps to 'stop'
    assert map_anthropic_stop_reason("end_turn") == "stop"

    # Verify through provider generation
    max_tokens_payload = {
        "id": "msg_len_01",
        "type": "message",
        "role": "assistant",
        "model": "claude-3-5-sonnet-20241022",
        "content": [{"type": "text", "text": "Incomplete sentence due to limit"}],
        "stop_reason": "max_tokens",
        "usage": {"input_tokens": 10, "output_tokens": 50},
    }
    provider = AnthropicNativeProvider(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=max_tokens_payload))
    )
    resp = await provider.generate(
        GenerationRequest(messages=[Message(role="user", content="Ping")])
    )
    assert resp.finish_reason == "length"
    assert resp.message.content == "Incomplete sentence due to limit"


@pytest.mark.asyncio
async def test_stream_cancellation_closes_owned_transports() -> None:
    """Acceptance case: Stream cancellation closes owned transports."""

    class MockClosingAsyncByteStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield (
                b"event: message_start\n"
                b'data: {"type": "message_start", "message": {"id": "msg_01", '
                b'"type": "message", "role": "assistant", '
                b'"model": "claude-3-5-sonnet-20241022", "usage": {"input_tokens": 5}}}\n\n'
            )
            yield (
                b"event: content_block_start\n"
                b'data: {"type": "content_block_start", "index": 0, '
                b'"content_block": {"type": "text", "text": ""}}\n\n'
            )
            for i in range(100):
                chunk_text = f"chunk_{i} "
                yield (
                    f"event: content_block_delta\n"
                    f'data: {{"type": "content_block_delta", "index": 0, '
                    f'"delta": {{"type": "text_delta", "text": "{chunk_text}"}}}}\n\n'
                ).encode()

        async def aclose(self) -> None:
            self.closed = True

    stream_obj = MockClosingAsyncByteStream()

    def stream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=stream_obj,
        )

    provider = AnthropicNativeProvider(
        transport=httpx.MockTransport(stream_handler),
    )

    ctx = ExecutionContext(run_id="run_cancel_b12", node_id="node_cancel")
    deltas_received = 0

    with pytest.raises(StreamCancelledError):
        async for delta in provider.stream(
            GenerationRequest(messages=[Message(role="user", content="Ping")]),
            context=ctx,
        ):
            if delta.kind == "text_delta":
                deltas_received += 1
                if deltas_received >= 2:
                    ctx.cancel()

    assert deltas_received >= 2
    assert stream_obj.closed is True

    # Test explicit aclose()
    await provider.aclose()


@pytest.mark.asyncio
async def test_unsupported_feature_rejection() -> None:
    """Acceptance: Unsupported audio or vector embedding requests reject eagerly."""
    provider = AnthropicNativeProvider()

    # Audio rejection
    req_audio = GenerationRequest(
        messages=[Message(role="user", content="Listen")],
        provider_metadata={"audio": True},
    )
    with pytest.raises(CapabilityError, match="does not support native audio"):
        await provider.generate(req_audio)

    # Embedding rejection
    with pytest.raises(CapabilityError, match="does not support vector embeddings"):
        await provider.embed(["test vector"])

    # Tools capability disabled
    no_tools_caps = ProviderCapabilities(can_generate=True, can_tools=False)
    no_tools_prov = AnthropicNativeProvider(capabilities=no_tools_caps)
    with pytest.raises(CapabilityError, match="does not support tool calling"):
        await no_tools_prov.generate(
            GenerationRequest(
                messages=[Message(role="user", content="Hi")],
                tools=[{"name": "t1", "description": "d", "parameters": {}}],
            )
        )


@pytest.mark.asyncio
async def test_anthropic_cache_metadata_preserved() -> None:
    """Acceptance: Prompt caching metrics are preserved without data loss."""
    cached_payload = {
        "id": "msg_cached_01",
        "type": "message",
        "role": "assistant",
        "model": "claude-3-5-sonnet-20241022",
        "content": [{"type": "text", "text": "Prompt cache hit answer"}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 50,
            "output_tokens": 20,
            "cache_creation_input_tokens": 1000,
            "cache_read_input_tokens": 2500,
        },
    }
    msg, usage, finish_reason, metadata = extract_anthropic_response(cached_payload)
    assert usage is not None
    assert usage.prompt_tokens == 50
    assert usage.completion_tokens == 20
    # Total includes input + output + cache_creation + cache_read
    assert usage.total_tokens == 50 + 20 + 1000 + 2500
    assert metadata.cache_creation_input_tokens == 1000
    assert metadata.cache_read_input_tokens == 2500


@pytest.mark.asyncio
async def test_secret_masking_in_repr_and_errors() -> None:
    """Acceptance: Credentials are redacted from representations and error messages."""
    fake_secret = "sk-ant-testsecret98765432101234567890"
    provider = AnthropicNativeProvider(api_key=fake_secret)
    repr_str = repr(provider)
    assert fake_secret not in repr_str
    assert "***" in repr_str

    # HTTP 401 error masking
    def auth_err_handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error": {
                    "type": "authentication_error",
                    "message": f"Invalid key {fake_secret}",
                }
            },
        )

    auth_prov = AnthropicNativeProvider(
        api_key=fake_secret,
        transport=httpx.MockTransport(auth_err_handler),
    )
    with pytest.raises(ProviderAuthError) as exc_info:
        await auth_prov.generate(GenerationRequest(messages=[Message(role="user", content="Ping")]))
    err_msg = str(exc_info.value)
    assert fake_secret not in err_msg
    assert "[REDACTED_ANTHROPIC_KEY]" in err_msg


@pytest.mark.asyncio
async def test_anthropic_agent_two_turn_integration() -> None:
    """Acceptance: Common Agent and Runner executes two-turn tool flow with Anthropic."""

    @tool
    def add_numbers(a: int, b: int) -> str:
        """Add two integers."""
        return str(a + b)

    turn_count = 0

    def mock_agent_handler(request: httpx.Request) -> httpx.Response:
        nonlocal turn_count
        body = json.loads(request.content.decode("utf-8"))
        msgs = body.get("messages", [])

        if len(msgs) == 1:
            turn_count += 1
            return httpx.Response(
                200,
                json={
                    "id": "msg_agent_01",
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_calc_01",
                            "name": "add_numbers",
                            "input": {"a": 40, "b": 2},
                        }
                    ],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 20, "output_tokens": 15},
                },
            )

        turn_count += 1
        return httpx.Response(
            200,
            json={
                "id": "msg_agent_02",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "The sum is 42."}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 45, "output_tokens": 10},
            },
        )

    provider = AnthropicNativeProvider(transport=httpx.MockTransport(mock_agent_handler))
    agent = Agent(
        name="CalculatorAgent",
        instructions="Perform arithmetic accurately.",
        model=provider,
        tools=[add_numbers],
    )
    runner = Runner()
    result = await runner.run_async(agent, "What is 40 + 2?")

    assert result.status == "completed"
    assert result.turns_count == 2
    assert result.tool_calls_count == 1
    assert "42" in result.final_output
    assert result.usage.total_tokens == 20 + 15 + 45 + 10


@pytest.mark.asyncio
async def test_anthropic_provider_module_contract() -> None:
    """Acceptance: AnthropicProviderModule conforms to B00/B01 module contracts."""
    module = default_registry.get("nexura.models.anthropic")
    assert isinstance(module, AnthropicProviderModule)
    assert module.manifest.module_id == "nexura.models.anthropic"
    assert module.manifest.kind == "model_adapter"

    ctx = ExecutionContext(run_id="run_b12_test", node_id="node_b12")

    # Generate operation
    res_gen = await module.execute(
        ctx,
        {
            "operation": "generate",
            "messages": [{"role": "user", "content": "Hello Anthropic"}],
        },
        {},
    )
    assert res_gen.status == "success"
    assert "Keyless mock response" in res_gen.outputs["content"]

    # Stream operation
    res_stream = await module.execute(
        ctx,
        {
            "operation": "stream",
            "messages": [{"role": "user", "content": "Stream Anthropic"}],
        },
        {},
    )
    assert res_stream.status == "success"
    assert "Keyless mock response" in res_stream.outputs["content"]  # a real SSE stream

    # Embed operation (fails cleanly with capability unsupported)
    res_embed = await module.execute(
        ctx,
        {"operation": "embed", "texts": ["sample"]},
        {},
    )
    assert res_embed.status == "failure"
    assert res_embed.error_code == "CAPABILITY_UNSUPPORTED"
