"""Example configuring Agent with a real OpenAI-compatible provider.

This example is configured exclusively via environment variables:
- OPENAI_API_KEY: Provider authentication key (required for live execution).
- OPENAI_BASE_URL: Endpoint URL (defaults to https://api.openai.com/v1).
- OPENAI_MODEL: Model identifier (defaults to gpt-4o-mini).

If OPENAI_API_KEY is not set, this script exits cleanly with instructions.
No paid API calls are made during automated tests.
"""

from __future__ import annotations

import asyncio
import os
import sys

from nexura_runtime import Agent, OpenAIChatModel, Runner, tool


@tool
def calculate(expression: str) -> str:
    """Evaluate a mathematical expression safely."""
    # Simple safe arithmetic demonstration
    allowed_chars = set("0123456789+-*/(). ")
    if not all(c in allowed_chars for c in expression):
        return "Error: Unsupported characters in mathematical expression."
    try:
        # Evaluate simple math safely
        result = eval(expression, {"__builtins__": {}}, {})
        return str(result)
    except Exception as exc:
        return f"Calculation error: {exc}"


async def main() -> None:
    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    model_name = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    if not api_key:
        print("=" * 60)
        print("Real Provider Example (OpenAI-compatible HTTP adapter)")
        print("=" * 60)
        print("Notice: OPENAI_API_KEY environment variable is not set.")
        print("To run against a live endpoint (e.g. OpenAI, vLLM, Ollama):")
        print("  export OPENAI_API_KEY='sk-...'")
        print("  export OPENAI_BASE_URL='https://api.openai.com/v1'")
        print("  export OPENAI_MODEL='gpt-4o-mini'")
        print("  uv run python examples/real_provider.py")
        print("=" * 60)
        sys.exit(0)

    # Initialize model with credentials from environment
    model = OpenAIChatModel(
        model=model_name,
        api_key=api_key,
        base_url=base_url,
    )

    # Verify secret masking
    print(f"Model configured: {model!r}")
    assert api_key not in repr(model), "API key leaked in repr!"

    agent = Agent(
        name="MathAssistant",
        instructions="You are a math helper. Use calculate tool for arithmetic.",
        model=model,
        tools=[calculate],
    )

    runner = Runner()
    print("Running agent with prompt: 'What is (34 * 12) + 50?'")
    result = await runner.run_async(agent, "What is (34 * 12) + 50?")

    print(f"Status: {result.status}")
    print(f"Tool calls: {result.tool_calls_count}")
    print(f"Usage reported: {result.usage.is_reported}")
    print(f"Answer: {result.final_output}")

    await model.close()


if __name__ == "__main__":
    asyncio.run(main())
