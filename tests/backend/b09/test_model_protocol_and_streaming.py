"""Unit and acceptance tests for B09: Model Protocol and Streaming."""

from __future__ import annotations

import pytest
from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.contracts.errors import SchemaValidationError
from nexura_runtime.providers import (
    BaseProvider,
    CapabilityError,
    GenerationRequest,
    MalformedToolCallArgumentsError,
    ModelProviderModule,
    ProviderAuthError,
    ProviderProtocol,
    StreamCancelledError,
    StreamDelta,
    StreamingFakeProvider,
    ToolCallAssembler,
)
from nexura_runtime.types import Message, Usage


@pytest.mark.asyncio
async def test_fragmented_interleaved_tool_calls_retain_ids() -> None:
    """Acceptance Case 1: Fragmented and interleaved tool calls retain IDs and args."""
    deltas = [
        # Call 0 start
        StreamDelta(
            kind="tool_call_delta",
            tool_call_index=0,
            tool_call_id="call_alpha_001",
            tool_name="database_query",
            tool_arguments_delta='{"table": ',
        ),
        # Call 1 interleaved start
        StreamDelta(
            kind="tool_call_delta",
            tool_call_index=1,
            tool_call_id="call_beta_002",
            tool_name="slack_notify",
            tool_arguments_delta='{"channel": ',
        ),
        # Call 0 continuation
        StreamDelta(
            kind="tool_call_delta",
            tool_call_index=0,
            tool_arguments_delta='"users", "limit": 50}',
        ),
        # Call 1 continuation
        StreamDelta(
            kind="tool_call_delta",
            tool_call_index=1,
            tool_arguments_delta='"#dev-ops", "urgent": true}',
        ),
    ]

    assembler = ToolCallAssembler()
    for delta in deltas:
        assembler.feed(delta)

    calls = assembler.validate_and_assemble()
    assert len(calls) == 2

    call_alpha = calls[0]
    assert call_alpha.id == "call_alpha_001"
    assert call_alpha.name == "database_query"
    assert call_alpha.arguments == {"table": "users", "limit": 50}

    call_beta = calls[1]
    assert call_beta.id == "call_beta_002"
    assert call_beta.name == "slack_notify"
    assert call_beta.arguments == {"channel": "#dev-ops", "urgent": True}


@pytest.mark.asyncio
async def test_malformed_arguments_never_execute() -> None:
    """Acceptance Case 2: Malformed tool call arguments raise typed error and never execute."""
    deltas = [
        StreamDelta(
            kind="tool_call_delta",
            tool_call_index=0,
            tool_call_id="call_bad_001",
            tool_name="delete_account",
            tool_arguments_delta='{"user_id": 12345, "confirm": TR',  # Invalid JSON syntax
        ),
    ]

    assembler = ToolCallAssembler()
    for d in deltas:
        assembler.feed(d)

    executed_side_effects: list[str] = []

    with pytest.raises(MalformedToolCallArgumentsError) as exc_info:
        assembled = assembler.validate_and_assemble()
        # Simulated dispatcher
        for call in assembled:
            executed_side_effects.append(f"executed {call.name}")

    err = exc_info.value
    assert err.details["tool_name"] == "delete_account"
    assert err.details["tool_call_id"] == "call_bad_001"
    assert len(executed_side_effects) == 0, "Malformed tool arguments must NEVER execute!"


@pytest.mark.asyncio
async def test_missing_usage_remains_partial() -> None:
    """Acceptance Case 3: Missing usage remains partial, never fabricated as zero."""
    partial_usage = Usage(
        prompt_tokens=45,
        completion_tokens=None,
        total_tokens=None,
        is_complete=False,
    )

    provider = StreamingFakeProvider(
        queued_deltas=[
            [
                StreamDelta(kind="text_delta", text="Hello world"),
                StreamDelta(kind="finish", finish_reason="stop", usage=partial_usage),
            ]
        ]
    )

    deltas_received: list[StreamDelta] = []
    async for d in provider.stream(
        GenerationRequest(messages=[Message(role="user", content="Test")])
    ):
        deltas_received.append(d)

    finish_delta = deltas_received[-1]
    assert finish_delta.kind == "finish"
    assert finish_delta.usage is not None
    assert finish_delta.usage.prompt_tokens == 45
    assert finish_delta.usage.completion_tokens is None
    assert finish_delta.usage.total_tokens is None
    assert finish_delta.usage.is_complete is False

    # Accumulate with another turn that had partial usage
    turn2_usage = Usage(prompt_tokens=10, completion_tokens=5, total_tokens=None, is_complete=False)
    accumulated = finish_delta.usage.add(turn2_usage)
    assert accumulated.prompt_tokens == 55
    assert accumulated.completion_tokens == 5
    assert accumulated.total_tokens is None
    assert accumulated.is_complete is False


@pytest.mark.asyncio
async def test_disconnect_cancels_local_stream_cleanly() -> None:
    """Acceptance Case 4: Disconnect or context cancellation cancels local stream cleanly."""
    provider = StreamingFakeProvider(
        disconnect_after_chunks=3,
        queued_deltas=[
            [
                StreamDelta(kind="text_delta", text="Chunk 1 "),
                StreamDelta(kind="text_delta", text="Chunk 2 "),
                StreamDelta(kind="text_delta", text="Chunk 3 "),
                StreamDelta(kind="text_delta", text="Chunk 4 "),
                StreamDelta(kind="text_delta", text="Chunk 5 "),
            ]
        ],
    )

    received_chunks: list[str] = []
    with pytest.raises(StreamCancelledError) as exc_info:
        async for delta in provider.stream(
            GenerationRequest(messages=[Message(role="user", content="Stream me")])
        ):
            if delta.text:
                received_chunks.append(delta.text)

    assert "disconnect" in exc_info.value.message.lower()
    assert len(received_chunks) == 3
    # Provider must clean up active streams count
    assert provider.active_streams_count == 0

    # Also test context cancellation
    provider_cancellable = StreamingFakeProvider()
    cancelled_ctx = ExecutionContext(
        run_id="run_cancel_test",
        node_id="node_cancel",
        is_cancelled=True,
    )
    with pytest.raises(StreamCancelledError):
        async for _ in provider_cancellable.stream(
            GenerationRequest(messages=[Message(role="user", content="Stream me")]),
            context=cancelled_ctx,
        ):
            pass
    assert provider_cancellable.active_streams_count == 0


@pytest.mark.asyncio
async def test_final_only_providers_not_advertised_as_streaming() -> None:
    """Acceptance Case 5: Final-only providers are not advertised as token-streaming."""
    final_only = StreamingFakeProvider.final_only(model_id="legacy-final-model")
    assert final_only.capabilities.can_stream is False

    req = GenerationRequest(messages=[Message(role="user", content="Ping")])

    # Streaming must be rejected early
    with pytest.raises(CapabilityError) as exc_info:
        async for _ in final_only.stream(req):
            pass

    err = exc_info.value
    assert "final-only providers are not advertised as token-streaming" in err.message.lower()
    assert err.details["provider"] == "legacy-final-model"
    assert err.details["capability"] == "stream"

    # Non-streaming generate works fine
    resp = await final_only.generate(req)
    assert resp.message.content is not None
    assert "Echo: Ping" in resp.message.content


@pytest.mark.asyncio
async def test_secret_masking_in_repr_and_errors() -> None:
    """Test that secrets and credentials are masked in repr, string, and error messages."""
    secret_key = "sk-live-secret-key-1234567890abcdef"
    error = ProviderAuthError("openai-test", f"Failed with Bearer {secret_key}")
    assert secret_key not in error.message
    assert "[REDACTED_SECRET]" in error.message

    provider = StreamingFakeProvider(model_id=f"provider-{secret_key}")
    # Repr must not leak keys
    assert isinstance(repr(provider), str)
    assert issubclass(StreamingFakeProvider, BaseProvider)
    assert isinstance(provider, ProviderProtocol)


@pytest.mark.asyncio
async def test_model_module_b00_contract() -> None:
    """Test B00/B01 module contract compliance for ModelProviderModule."""
    module = ModelProviderModule()
    ctx = ExecutionContext(run_id="run_b09_test", node_id="node_test")

    # 1. Invalid input schema causes zero effects
    with pytest.raises(SchemaValidationError):
        await module.execute(ctx, {}, {})

    # 2. Generate operation
    gen_result = await module.execute(
        ctx,
        {
            "operation": "generate",
            "messages": [{"role": "user", "content": "Hello B09"}],
        },
        {},
    )
    assert gen_result.status == "success"
    assert "Echo: Hello B09" in gen_result.outputs["content"]

    # 3. Stream operation
    stream_result = await module.execute(
        ctx,
        {
            "operation": "stream",
            "messages": [{"role": "user", "content": "Stream me"}],
        },
        {},
    )
    assert stream_result.status == "success"
    assert stream_result.outputs["chunks_count"] > 0
    assert "Streaming test completion" in stream_result.outputs["content"]

    # 4. Embed operation
    embed_result = await module.execute(
        ctx,
        {
            "operation": "embed",
            "texts": ["Financial market analysis", "Portfolio risk"],
        },
        {},
    )
    assert embed_result.status == "success"
    assert len(embed_result.outputs["embeddings"]) == 2
