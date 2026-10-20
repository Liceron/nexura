"""B12: Native Anthropic Adapter Standalone Example.

Demonstrates:
1. Keyless AnthropicNativeProvider initialized with deterministic httpx.MockTransport.
2. Deterministic two-turn Claude-style tool execution through the common Agent & Runner API.
3. Mixed text and tool_use blocks surviving replay across conversation turns.
4. Stop reason mapping (max_tokens -> length, end_turn -> stop).
5. Anthropic SSE Streaming with content-block deltas and usage.
6. Documented real production credentialed smoke invocation.
7. Execution via B01 Module Interface and CLI.
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx
from nexura_runtime.agent import Agent
from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.providers.anthropic.module import AnthropicProviderModule
from nexura_runtime.providers.anthropic.provider import AnthropicNativeProvider
from nexura_runtime.providers.types import GenerationRequest
from nexura_runtime.runner import Runner
from nexura_runtime.tools import tool
from nexura_runtime.types import Message

from nexura_runtime.modules.registry import default_registry


# 1. Mock Transport for Deterministic Two-Turn Agent Execution
def create_two_turn_mock_transport() -> httpx.MockTransport:
    turn_counter = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal turn_counter
        body = json.loads(request.content.decode("utf-8"))
        messages = body.get("messages", [])

        # Turn 1: Claude decides to invoke tool
        if len(messages) == 1:
            turn_counter += 1
            return httpx.Response(
                200,
                json={
                    "id": "msg_turn1_123",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-3-5-sonnet-20241022",
                    "content": [
                        {
                            "type": "text",
                            "text": "I will inspect the Bangalore production cluster status.",
                        },
                        {
                            "type": "tool_use",
                            "id": "toolu_blr_cluster_99",
                            "name": "check_cluster_health",
                            "input": {"cluster_name": "blr-prod-01"},
                        },
                    ],
                    "stop_reason": "tool_use",
                    "usage": {
                        "input_tokens": 45,
                        "output_tokens": 32,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
            )

        # Turn 2: Claude receives tool result and provides final answer
        turn_counter += 1
        return httpx.Response(
            200,
            json={
                "id": "msg_turn2_456",
                "type": "message",
                "role": "assistant",
                "model": "claude-3-5-sonnet-20241022",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Cluster blr-prod-01 is fully healthy (status: OK, nodes: 16/16, "
                            "latency: 1.2ms, region: ap-south-1)."
                        ),
                    }
                ],
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 88,
                    "output_tokens": 28,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        )

    return httpx.MockTransport(handler)


async def run_b12_demo() -> None:
    print("=== B12: Native Anthropic Adapter Demonstration ===\n")

    # 1. Keyless Provider Initialization
    print("1. Initializing AnthropicNativeProvider with MockTransport (Keyless)...")
    mock_transport = create_two_turn_mock_transport()
    provider = AnthropicNativeProvider(
        model="claude-3-5-sonnet-20241022",
        transport=mock_transport,
    )
    print(f"  Provider initialized: {provider}")

    # 2. Tool Definition
    @tool
    def check_cluster_health(cluster_name: str) -> str:
        """Check status and health metrics of a deployment cluster."""
        return json.dumps(
            {
                "cluster": cluster_name,
                "status": "OK",
                "active_nodes": 16,
                "latency_ms": 1.2,
                "region": "ap-south-1",
            }
        )

    # 3. Two-Turn Claude Execution via common Agent and Runner
    print("\n2. Executing Two-Turn Agent Workflow via Common Runner...")
    agent = Agent(
        name="InfrastructureInspector",
        instructions="You are an infrastructure SRE assistant.",
        model=provider,
        tools=[check_cluster_health],
    )
    runner = Runner()
    result = await runner.run_async(agent, "Check the health of our Bangalore cluster.")

    print(f"  Agent Run ID: {result.run_id}")
    print(f"  Final Output: {result.final_output}")
    print(f"  Turns Count: {result.turns_count}")
    print(f"  Tool Calls Count: {result.tool_calls_count}")
    print(f"  Reported Usage: {result.usage}")
    print(f"  Status: {result.status}\n")

    # 4. Stop Reason Mapping Demonstration
    print("3. Demonstrating Stop Reason Mapping (max_tokens -> length)...")
    max_tokens_transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            json={
                "id": "msg_trunc_001",
                "type": "message",
                "role": "assistant",
                "model": "claude-3-5-sonnet-20241022",
                "content": [{"type": "text", "text": "Partial text truncated by limit..."}],
                "stop_reason": "max_tokens",
                "usage": {"input_tokens": 10, "output_tokens": 100},
            },
        )
    )
    trunc_provider = AnthropicNativeProvider(
        model="claude-3-5-sonnet-20241022",
        transport=max_tokens_transport,
    )
    trunc_resp = await trunc_provider.generate(
        GenerationRequest(messages=[Message(role="user", content="Generate long report")])
    )
    print(f"  finish_reason: '{trunc_resp.finish_reason}' (expected 'length', not 'stop')")
    assert trunc_resp.finish_reason == "length"

    # 5. Anthropic SSE Streaming Demonstration
    print("\n4. Demonstrating Anthropic SSE Streaming...")
    sse_body = (
        "event: message_start\n"
        'data: {"type": "message_start", "message": {"id": "msg_stream_01", "type": "message", '
        '"role": "assistant", "model": "claude-3-5-sonnet-20241022", '
        '"usage": {"input_tokens": 15}}}\n\n'
        "event: content_block_start\n"
        'data: {"type": "content_block_start", "index": 0, '
        '"content_block": {"type": "text", "text": ""}}\n\n'
        "event: content_block_delta\n"
        'data: {"type": "content_block_delta", "index": 0, '
        '"delta": {"type": "text_delta", "text": "Claude "}}\n\n'
        "event: content_block_delta\n"
        'data: {"type": "content_block_delta", "index": 0, '
        '"delta": {"type": "text_delta", "text": "streaming "}}\n\n'
        "event: content_block_delta\n"
        'data: {"type": "content_block_delta", "index": 0, '
        '"delta": {"type": "text_delta", "text": "verified!"}}\n\n'
        "event: content_block_stop\n"
        'data: {"type": "content_block_stop", "index": 0}\n\n'
        "event: message_delta\n"
        'data: {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, '
        '"usage": {"output_tokens": 6}}\n\n'
        "event: message_stop\ndata: {}\n\n"
    )
    stream_content = sse_body.encode("utf-8")

    stream_transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=stream_content,
        )
    )
    stream_provider = AnthropicNativeProvider(
        model="claude-3-5-sonnet-20241022",
        transport=stream_transport,
    )

    print("  Streaming output: ", end="", flush=True)
    async for delta in stream_provider.stream(
        GenerationRequest(messages=[Message(role="user", content="Ping")])
    ):
        if delta.kind == "text_delta" and delta.text:
            print(delta.text, end="", flush=True)
    print("\n  Anthropic stream complete.\n")

    # 6. Production Credentialed Smoke Invocation
    print("5. Production Credentialed Smoke Invocation:")
    print("   ==============================================================")
    print("   export ANTHROPIC_API_KEY='sk-ant-api03-...'")
    print('   python -c "')
    print("   import os, asyncio")
    print("   from nexura_runtime.providers import GenerationRequest")
    print("   from nexura_runtime.providers.anthropic import AnthropicNativeProvider")
    print("   from nexura_runtime.types import Message")
    print("   async def run():")
    print("       key = os.environ['ANTHROPIC_API_KEY']")
    print("       p = AnthropicNativeProvider(api_key=key)")
    print("       req = GenerationRequest(messages=[Message(role='user', content='Hello Claude')])")
    print("       r = await p.generate(req)")
    print("       print(r.message.content)")
    print("   asyncio.run(run())")
    print('   "')
    print("   ==============================================================\n")

    # 7. Execute via B01 Module Interface & CLI
    print("6. Executing AnthropicProviderModule via B01 Module Registry...")
    module = default_registry.get("nexura.models.anthropic")
    assert isinstance(module, AnthropicProviderModule)
    ctx = ExecutionContext(run_id="run_b12_demo", node_id="node_b12")
    mod_result = await module.execute(
        ctx,
        {
            "operation": "generate",
            "messages": [{"role": "user", "content": "Hello Anthropic"}],
        },
        {},
    )
    print(f"  Module invocation status: {mod_result.status}")
    assert mod_result.status == "success"

    print("\n7. Executing via B01 CLI...")
    from nexura_runtime.modules.cli import main as cli_main

    cli_args = [
        "run",
        "nexura.models.anthropic",
        "--input",
        json.dumps(
            {
                "operation": "generate",
                "messages": [{"role": "user", "content": "CLI Anthropic Ping"}],
            }
        ),
    ]
    orig_argv = sys.argv
    sys.argv = ["nexura-module"] + cli_args
    try:
        cli_code = cli_main()
        print(f"\nCLI Execution: {'SUCCESS' if cli_code == 0 else 'FAILURE'} (code {cli_code})")
        assert cli_code == 0
    finally:
        sys.argv = orig_argv

    print("\n=== B12 Example Completed Successfully ===")


def main() -> None:
    asyncio.run(run_b12_demo())


if __name__ == "__main__":
    main()
