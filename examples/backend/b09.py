"""B09: Model Protocol and Streaming Example.

Demonstrates:
1. Streaming text tokens in real-time through the public protocol.
2. Streaming split/fragmented tool call deltas and assembling them via ToolCallAssembler.
3. Preserving partial/unknown usage metrics without fabricating zeros.
4. Early rejection of streaming on final-only providers (CapabilityError).
5. Interoperability with the B00/B01 module contract and CLI.
"""

from __future__ import annotations

import asyncio
import json
import sys

from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.providers import (
    CapabilityError,
    GenerationRequest,
    StreamDelta,
    StreamingFakeProvider,
    ToolCallAssembler,
)
from nexura_runtime.types import Message, Usage

from nexura_runtime.modules.cli import main as cli_main
from nexura_runtime.modules.registry import default_registry


async def run_streaming_demo() -> None:
    print("=== B09: Model Protocol and Streaming Demonstration ===\n")

    # 1. Initialize Streaming Provider
    print("1. Initializing StreamingFakeProvider...")
    provider = StreamingFakeProvider(model_id="gemini-fake-stream")
    print(f"  Provider initialized: {provider!r}")
    print(f"  Streaming capability: {provider.capabilities.can_stream}")
    print(f"  Tool-calling capability: {provider.capabilities.can_tools}\n")

    # 2. Streaming Real-Time Text Tokens
    print("2. Streaming text tokens through ProviderProtocol...")
    req_text = GenerationRequest(
        messages=[Message(role="user", content="Explain quantum computing in one sentence.")]
    )

    assembled_text: list[str] = []
    print("  Received stream tokens: ", end="", flush=True)
    async for delta in provider.stream(req_text):
        if delta.kind == "text_delta" and delta.text:
            assembled_text.append(delta.text)
            print(delta.text, end="", flush=True)
    print("\n  Complete streamed text assembled successfully!\n")

    # 3. Streaming Split/Fragmented Tool Call
    print("3. Streaming fragmented/interleaved tool call deltas...")
    tool_call_deltas = [
        StreamDelta(
            kind="tool_call_delta",
            tool_call_index=0,
            tool_call_id="call_weather_99",
            tool_name="get_current_weather",
            tool_arguments_delta='{"city": "Bangalore", ',
        ),
        StreamDelta(
            kind="tool_call_delta",
            tool_call_index=0,
            tool_arguments_delta='"units": "metric", ',
        ),
        StreamDelta(
            kind="tool_call_delta",
            tool_call_index=0,
            tool_arguments_delta='"include_forecast": true}',
        ),
        StreamDelta(
            kind="finish",
            finish_reason="tool_calls",
            usage=Usage(
                prompt_tokens=42,
                completion_tokens=None,
                total_tokens=None,
                is_complete=False,
            ),
        ),
    ]

    tool_provider = StreamingFakeProvider(
        model_id="tool-streamer",
        queued_deltas=[tool_call_deltas],
    )

    assembler = ToolCallAssembler()
    final_usage = None

    weather_msg = Message(role="user", content="What is the weather in Bangalore?")
    async for delta in tool_provider.stream(GenerationRequest(messages=[weather_msg])):
        if delta.kind == "tool_call_delta":
            chunk_repr = repr(delta.tool_arguments_delta)
            print(f"  [Delta Received] idx={delta.tool_call_index} chunk={chunk_repr}")
            assembler.feed(delta)
        elif delta.kind == "finish":
            final_usage = delta.usage

    assembled_calls = assembler.validate_and_assemble()
    print(f"\n  Assembled Tool Calls Count: {len(assembled_calls)}")
    for tc in assembled_calls:
        print(f"    Tool ID: {tc.id}")
        print(f"    Tool Name: {tc.name}")
        print(f"    Validated JSON Arguments: {json.dumps(tc.arguments)}")
        assert tc.id == "call_weather_99"
        assert tc.name == "get_current_weather"
        assert tc.arguments["city"] == "Bangalore"
        assert tc.arguments["units"] == "metric"
    print("  Fragmented tool calls successfully assembled and validated.\n")

    # 4. Verifying Partial Usage Metrics
    print("4. Inspecting stream token usage reporting...")
    assert final_usage is not None
    print(f"  Prompt tokens: {final_usage.prompt_tokens}")
    print(f"  Completion tokens: {final_usage.completion_tokens} (omitted by provider)")
    print(f"  Total tokens: {final_usage.total_tokens} (omitted by provider)")
    print(f"  is_complete: {final_usage.is_complete}")
    assert final_usage.is_complete is False, "Missing usage must remain partial, never fabricated!"
    print("  Usage completeness verified: missing tokens stay None and partial.\n")

    # 5. Early Capability Enforcement on Final-Only Provider
    print("5. Verifying early rejection of streaming on final-only provider...")
    final_only = StreamingFakeProvider.final_only(model_id="batch-llm-v1")
    print(f"  Final-only capabilities: can_stream={final_only.capabilities.can_stream}")
    try:
        async for _ in final_only.stream(req_text):
            pass
        raise RuntimeError("Final-only provider unexpectedly allowed streaming!")
    except CapabilityError as exc:
        print(f"  Caught expected CapabilityError: {exc.message}")
        print(f"  Error details: {exc.details}")
    print(
        "  Early capability check passed: final-only providers are not advertised as streaming.\n"
    )

    # 6. B01 CLI Module Invocations
    print("6. Executing tool call assembly via B01 Module Interface...")
    module = default_registry.get("nexura.models.provider")
    ctx = ExecutionContext(run_id="run_b09_demo", node_id="node_demo")
    result = await module.execute(
        ctx,
        {
            "operation": "assemble_tool_calls",
            "deltas": [
                {
                    "index": 0,
                    "id": "call_calc_01",
                    "name": "calculator",
                    "arguments_delta": '{"expr": ',
                },
                {"index": 0, "arguments_delta": '"2 + 2"}'},
            ],
        },
        {},
    )
    print(f"  Module invocation status: {result.status}")
    print(f"  Assembled output: {json.dumps(result.outputs)}")

    print("\n7. Executing generation via B01 CLI...")
    cli_args = [
        "run",
        "nexura.models.provider",
        "--input",
        json.dumps(
            {
                "operation": "generate",
                "messages": [{"role": "user", "content": "Ping"}],
            }
        ),
    ]
    orig_argv = sys.argv
    sys.argv = ["nexura-module"] + cli_args
    try:
        cli_code = cli_main()
        print(f"\nCLI Execution: {'SUCCESS' if cli_code == 0 else 'FAILURE'} (code {cli_code})")
    finally:
        sys.argv = orig_argv

    print("\n=== B09 Example Completed Successfully ===")


def main() -> None:
    asyncio.run(run_streaming_demo())


if __name__ == "__main__":
    main()
