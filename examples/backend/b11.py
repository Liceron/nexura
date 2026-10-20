"""B11: Native Gemini Adapter Example.

Demonstrates:
1. Native Gemini tool round-trip with functionCall and functionResponse translation.
2. Preserving model reasoning/thought parts in assistant interactions.
3. Keyless mock transport for local offline execution.
4. Production credentialed smoke command documentation.
5. Invocation through B01 Module Interface and standalone CLI.
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx
from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.providers import GenerationRequest
from nexura_runtime.providers.gemini import GeminiNativeProvider, GeminiProviderModule
from nexura_runtime.types import Message

from nexura_runtime.modules.cli import main as cli_main
from nexura_runtime.modules.registry import default_registry


async def run_b11_demo() -> None:
    print("=== B11: Native Gemini Adapter Demonstration ===\n")

    # 1. Setup MockTransport for Gemini Tool Round-Trip
    print("1. Initializing GeminiNativeProvider with MockTransport (Keyless)...")

    turn_count = 0

    def gemini_agent_handler(request: httpx.Request) -> httpx.Response:
        nonlocal turn_count
        turn_count += 1
        data = json.loads(request.content)
        contents = data.get("contents", [])

        if turn_count == 1:
            # Turn 1: Model emits thought and calls lookup tool
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [
                                    {
                                        "thought": (
                                            "Inquiring about Mumbai Metro Line 3 (Aqua Line). "
                                            "Invoking get_infrastructure_status."
                                        )
                                    },
                                    {
                                        "functionCall": {
                                            "name": "get_infrastructure_status",
                                            "args": {"project": "Mumbai Metro Line 3"},
                                        }
                                    },
                                ],
                            },
                            "finishReason": "STOP",
                            "index": 0,
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 28,
                        "candidatesTokenCount": 32,
                        "totalTokenCount": 60,
                    },
                    "modelVersion": "gemini-2.5-flash",
                },
            )
        else:
            # Turn 2: Consume functionResponse and return final text
            assert any(any("functionResponse" in p for p in c.get("parts", [])) for c in contents)
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [
                                    {
                                        "text": (
                                            "Mumbai Metro Line 3 (Aqua Line) Phase 1 is fully "
                                            "operational between Aarey JVLR and BKC, serving "
                                            "over 50,000 commuters daily."
                                        )
                                    }
                                ],
                            },
                            "finishReason": "STOP",
                            "index": 0,
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 85,
                        "candidatesTokenCount": 30,
                        "totalTokenCount": 115,
                    },
                    "modelVersion": "gemini-2.5-flash",
                },
            )

    provider = GeminiNativeProvider(
        model="gemini-2.5-flash",
        api_key="gm-mock-demo-key",
        transport=httpx.MockTransport(gemini_agent_handler),
    )
    print(f"  Provider initialized: {provider!r}\n")

    # 2. Execute Multi-Turn Tool Interaction
    print("2. Executing Native Gemini Tool Flow...")
    messages: list[Message] = [
        Message(
            role="user",
            content="What is the current status of Mumbai Metro Line 3?",
        )
    ]
    tools = [
        {
            "function": {
                "name": "get_infrastructure_status",
                "description": (
                    "Check current operational status of an Indian public transit project."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"project": {"type": "string"}},
                    "required": ["project"],
                },
            }
        }
    ]

    # Turn 1
    resp1 = await provider.generate(GenerationRequest(messages=messages, tools=tools))
    assert resp1.message.tool_calls is not None
    tc = resp1.message.tool_calls[0]
    print(f"  Turn 1 Assistant: Tool Call Requested -> {tc.name}({tc.arguments})")
    messages.append(resp1.message)

    # Simulated tool execution
    tool_payload = {
        "status": "Operational Phase 1 (Aarey to BKC)",
        "daily_ridership": 50000,
    }
    messages.append(
        Message(
            role="tool",
            name=tc.name,
            tool_call_id=tc.id,
            content=json.dumps(tool_payload),
        )
    )
    print(f"  Tool Execution Result: {tool_payload}")

    # Turn 2
    resp2 = await provider.generate(GenerationRequest(messages=messages, tools=tools))
    print(f"\n  Turn 2 Assistant Response:\n  {resp2.message.content}")
    print(f"  Cumulative Usage: {resp2.usage}\n")

    # 3. Stream Demonstration
    print("3. Demonstrating Gemini SSE Streaming...")
    sse_body = (
        'data: {"candidates": [{"content": {"parts": [{"text": "Gemini "}]}}]}\n\n'
        'data: {"candidates": [{"content": {"parts": [{"text": "streaming "}]}}]}\n\n'
        'data: {"candidates": [{"content": {"parts": [{"text": "verified!"}]}}]}\n\n'
        'data: {"candidates": [{"finishReason": "STOP"}], '
        '"usageMetadata": {"totalTokenCount": 12}}\n\n'
        "data: [DONE]\n\n"
    )

    def stream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=sse_body.encode("utf-8"),
        )

    stream_provider = GeminiNativeProvider(
        model="gemini-2.5-flash",
        transport=httpx.MockTransport(stream_handler),
    )

    print("  Streaming output: ", end="", flush=True)
    async for delta in stream_provider.stream(
        GenerationRequest(messages=[Message(role="user", content="Ping")])
    ):
        if delta.kind == "text_delta" and delta.text:
            print(delta.text, end="", flush=True)
    print("\n  Gemini stream complete.\n")

    # 4. Document Production Smoke Invocation
    print("4. Production Credentialed Smoke Invocation:")
    print("   ==============================================================")
    print("   export GEMINI_API_KEY='AIzaSy...'")
    print('   python -c "')
    print("   import os, asyncio")
    print("   from nexura_runtime.providers import GenerationRequest")
    print("   from nexura_runtime.providers.gemini import GeminiNativeProvider")
    print("   from nexura_runtime.types import Message")
    print("   async def run():")
    print("       key = os.environ['GEMINI_API_KEY']")
    print("       p = GeminiNativeProvider(model='gemini-2.5-flash', api_key=key)")
    print("       req = GenerationRequest(messages=[Message(role='user', content='Hello')])")
    print("       r = await p.generate(req)")
    print("       print(r.message.content)")
    print("   asyncio.run(run())")
    print('   "')
    print("   ==============================================================\n")

    # 5. Execute via B01 Module Interface & CLI
    print("5. Executing GeminiProviderModule via B01 Module Registry...")
    module = default_registry.get("nexura.models.gemini")
    assert isinstance(module, GeminiProviderModule)
    ctx = ExecutionContext(run_id="run_b11_demo", node_id="node_b11")

    mod_result = await module.execute(
        ctx,
        {
            "operation": "generate",
            "messages": [{"role": "user", "content": "Ping Gemini"}],
        },
        {},
    )
    print(f"  Module invocation status: {mod_result.status}")

    print("\n6. Executing via B01 CLI...")
    cli_args = [
        "run",
        "nexura.models.gemini",
        "--input",
        json.dumps(
            {
                "operation": "generate",
                "messages": [{"role": "user", "content": "CLI Gemini Ping"}],
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

    print("\n=== B11 Example Completed Successfully ===")


def main() -> None:
    asyncio.run(run_b11_demo())


if __name__ == "__main__":
    main()
