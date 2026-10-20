"""B10: OpenAI-Compatible and Local Model Adapter Example.

Demonstrates:
1. Running a tool-backed agent against an OpenAI-compatible endpoint using MockTransport.
2. Real-time streaming token and tool-call deltas via SSE parser.
3. Connecting to local model stubs (e.g., Ollama / vLLM / LocalAI) without API keys.
4. Documenting real production environment configuration without executing paid calls.
5. Invoking the adapter through the B01 module CLI interface.
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx
from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.providers import GenerationRequest
from nexura_runtime.providers.openai import OpenAIProvider, OpenAIProviderModule
from nexura_runtime.types import Message

from nexura_runtime.modules.cli import main as cli_main
from nexura_runtime.modules.registry import default_registry


async def run_b10_demo() -> None:
    print("=== B10: OpenAI-Compatible and Local Model Adapter Demonstration ===\n")

    # 1. Setup MockTransport for Tool-Backed Agent Flow
    print("1. Initializing OpenAIProvider with MockTransport (Keyless)...")

    turn_count = 0

    def mock_agent_handler(request: httpx.Request) -> httpx.Response:
        nonlocal turn_count
        turn_count += 1
        data = json.loads(request.content)
        messages = data.get("messages", [])

        if turn_count == 1:
            # Turn 1: Model requests a tool call to lookup weather
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-agent-turn1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_weather_blr",
                                        "type": "function",
                                        "function": {
                                            "name": "get_current_weather",
                                            "arguments": json.dumps({"city": "Bengaluru"}),
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 30, "completion_tokens": 15, "total_tokens": 45},
                },
            )
        else:
            # Turn 2: Model consumes tool result and returns final answer
            assert any(m.get("role") == "tool" for m in messages)
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-agent-turn2",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": (
                                    "The capital of Karnataka is Bengaluru. "
                                    "Current weather: 24°C with partly cloudy skies."
                                ),
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 65, "completion_tokens": 20, "total_tokens": 85},
                },
            )

    provider = OpenAIProvider(
        model="gpt-4o",
        api_key="sk-mock-demo-key",
        transport=httpx.MockTransport(mock_agent_handler),
    )
    print(f"  Provider initialized: {provider!r}")

    # 2. Execute Tool-Backed Agent Flow
    print("\n2. Executing Tool-Backed Agent Conversation...")
    history: list[Message] = [
        Message(
            role="user",
            content="What is the capital of Karnataka and what is its current weather?",
        )
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_current_weather",
                "description": "Fetch current weather conditions for an Indian city.",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }
    ]

    # Turn 1
    resp1 = await provider.generate(GenerationRequest(messages=history, tools=tools))
    print(f"  Turn 1 Assistant: Tool Call Requested -> {resp1.message.tool_calls}")
    assert resp1.message.tool_calls is not None
    tc = resp1.message.tool_calls[0]
    history.append(resp1.message)

    # Execute simulated local tool
    tool_output = json.dumps(
        {"city": "Bengaluru", "temperature": "24°C", "condition": "Partly Cloudy"}
    )
    history.append(
        Message(
            role="tool",
            content=tool_output,
            tool_call_id=tc.id,
            name=tc.name,
        )
    )
    print(f"  Tool Execution: {tc.name}({tc.arguments}) -> {tool_output}")

    # Turn 2
    resp2 = await provider.generate(GenerationRequest(messages=history, tools=tools))
    print(f"\n  Turn 2 Assistant Response: {resp2.message.content}")
    print(f"  Cumulative Usage: {resp2.usage}")

    # 3. Stream Demonstration with SSE Mock
    print("\n3. Streaming real-time response chunks...")
    sse_content = (
        'data: {"choices": [{"index": 0, "delta": {"content": "Nexura "}}]}\n\n'
        'data: {"choices": [{"index": 0, "delta": {"content": "AgentKit "}}]}\n\n'
        'data: {"choices": [{"index": 0, "delta": {"content": "Streaming "}}]}\n\n'
        'data: {"choices": [{"index": 0, "delta": {"content": "Verified."}}]}\n\n'
        'data: {"usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}}\n\n'
        "data: [DONE]\n\n"
    )

    def sse_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=sse_content.encode("utf-8"),
        )

    stream_provider = OpenAIProvider(
        model="gpt-4o-mini",
        transport=httpx.MockTransport(sse_handler),
    )

    stream_tokens: list[str] = []
    print("  Streaming output: ", end="", flush=True)
    async for delta in stream_provider.stream(
        GenerationRequest(messages=[Message(role="user", content="Ping")])
    ):
        if delta.kind == "text_delta" and delta.text:
            stream_tokens.append(delta.text)
            print(delta.text, end="", flush=True)
    print("\n  Stream completed cleanly.")

    # 4. Local Compatible Stub Demonstration (Ollama / vLLM / LocalAI)
    print("\n4. Demonstrating local compatible stub configuration (Ollama/vLLM)...")

    def local_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Hello from local Ollama endpoint!",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11},
            },
        )

    local_provider = OpenAIProvider(
        model="llama3.1:8b",
        base_url="http://localhost:11434/v1",
        api_key=None,  # Local instances require no credentials
        transport=httpx.MockTransport(local_handler),
    )
    local_resp = await local_provider.generate(
        GenerationRequest(messages=[Message(role="user", content="Local ping")])
    )
    print(f"  Local Provider Response: {local_resp.message.content}")

    # 5. Production Environment Configuration Guide
    print("\n5. Production Real Invocation Documentation:")
    print("   To run against real OpenAI or local services in production:")
    print("   ==============================================================")
    print("   # For OpenAI:")
    print("   export OPENAI_API_KEY='sk-proj-...'")
    print("   provider = OpenAIProvider(model='gpt-4o', api_key=os.environ['OPENAI_API_KEY'])")
    print("   ")
    print("   # For local Ollama:")
    print("   provider = OpenAIProvider(model='llama3', base_url='http://localhost:11434/v1')")
    print("   ")
    print("   # For local vLLM:")
    print(
        "   provider = OpenAIProvider(model='meta-llama/Meta-Llama-3-8B-Instruct', base_url='http://localhost:8000/v1')"
    )
    print("   ==============================================================")

    # 6. Execute via B01 Module Interface & CLI
    print("\n6. Executing OpenAIProviderModule via B01 Module Registry...")
    module = default_registry.get("nexura.models.openai")
    ctx = ExecutionContext(run_id="run_b10_demo", node_id="node_b10")

    # Verify module instance in default registry
    assert isinstance(module, OpenAIProviderModule)
    # Execute generation through module
    mod_result = await module.execute(
        ctx,
        {
            "operation": "generate",
            "messages": [{"role": "user", "content": "What is the capital of Karnataka?"}],
        },
        {},
    )
    print(f"  Module invocation status: {mod_result.status}")

    print("\n7. Executing via B01 CLI...")
    cli_args = [
        "run",
        "nexura.models.openai",
        "--input",
        json.dumps(
            {
                "operation": "generate",
                "messages": [{"role": "user", "content": "CLI ping"}],
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

    print("\n=== B10 Example Completed Successfully ===")


def main() -> None:
    asyncio.run(run_b10_demo())


if __name__ == "__main__":
    main()
