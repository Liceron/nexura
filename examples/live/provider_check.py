"""Live provider check — makes real, billed calls with YOUR API key.

Run it yourself (it is never run by tests or CI). Pick one provider per run:

    export OPENAI_API_KEY=...
    uv run python examples/live/provider_check.py --provider openai

Use ANTHROPIC_API_KEY with --provider anthropic, GEMINI_API_KEY (or GOOGLE_API_KEY) with
--provider gemini, and SARVAM_API_KEY with --provider sarvam.

Add --model NAME to choose a model. The defaults are small models; Sarvam uses the
provider's own default. Vertex AI has its
own script (vertex_check.py) because it uses Google Cloud credentials instead of a key.

scripts/live_canary.py runs the same scenario for every provider with a key, writes dated
evidence files with masked keys, and only runs when NEXURA_LIVE_CANARY=1 is set.

Each run sends up to four short requests: a plain reply, a tool call, a streamed reply
(skipped for providers without streaming) and a structured (typed) answer. Expect a few
hundred to about two thousand tokens in total. The script prints a JSON report; send it
back so the results can be recorded with the date in the evidence folder.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any

from nexura_agents import Agent, Runner, tool
from nexura_agents.types import Message
from pydantic import BaseModel

PROVIDERS: dict[str, tuple[str, tuple[str, ...], str]] = {
    # name: (default model, accepted key variables, "module:Class")
    "openai": (
        "gpt-4.1-mini",
        ("OPENAI_API_KEY",),
        "nexura_agents.providers.openai:OpenAIProvider",
    ),
    "anthropic": (
        "claude-haiku-5-5",
        ("ANTHROPIC_API_KEY",),
        "nexura_agents.providers.anthropic:AnthropicNativeProvider",
    ),
    "gemini": (
        "gemini-2.5-flash",
        ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "nexura_agents.providers.gemini:GeminiNativeProvider",
    ),
    "sarvam": ("", ("SARVAM_API_KEY",), "nexura_agents.providers.sarvam:SarvamProvider"),
}


class OrderAnswer(BaseModel):
    order_id: str
    status: str


@tool
def get_order_status(order_id: str) -> dict[str, str]:
    """Look up an order's delivery status."""
    return {"order_id": order_id, "status": "out for delivery"}


def _build(provider: str, model: str) -> Any:
    import importlib

    module_name, class_name = PROVIDERS[provider][2].split(":")
    cls = getattr(importlib.import_module(module_name), class_name)
    return cls(model) if model else cls()  # "" means the provider's own default model


def _usage(usage: Any) -> dict[str, Any] | None:
    return dict(vars(usage)) if usage is not None else None


async def main(provider_name: str, model: str) -> int:
    keys = PROVIDERS[provider_name][1]
    if not any(os.environ.get(k) for k in keys):
        print(f"Set {' or '.join(keys)} first; nothing was sent.", file=sys.stderr)
        return 2

    provider = _build(provider_name, model)
    report: dict[str, Any] = {"provider": provider_name, "model": model, "checks": {}}
    checks: dict[str, Any] = report["checks"]
    failures: list[str] = []

    t0 = time.perf_counter()
    plain = await provider.generate([Message(role="user", content="Reply with exactly: ok")])
    text = (plain.message.content or "").strip()
    checks["plain"] = {
        "text": text,
        "usage": _usage(plain.usage),
        "ms": round((time.perf_counter() - t0) * 1000),
    }
    if not text.lower().startswith("ok"):
        failures.append("plain reply was not 'ok'")

    agent = Agent(
        name="LiveCheck",
        instructions="Use the tool to answer. Be brief.",
        model=provider,
        tools=[get_order_status],
        max_turns=4,
    )
    run = await Runner().run_async(agent, "What is the status of order ORD-42?")
    checks["tool_call"] = {
        "tool_calls": run.tool_calls_count,
        "answer": run.final_output,
        "usage": _usage(run.usage),
    }
    if run.tool_calls_count < 1:
        failures.append("model did not call the tool")

    if hasattr(provider, "stream"):
        try:
            stream = provider.stream([Message(role="user", content="Count from 1 to 5.")])
            chunks = [d.text async for d in stream if d.kind == "text_delta" and d.text]
            checks["stream"] = {"chunks": len(chunks), "text": "".join(chunks).strip()}
            if not chunks:
                failures.append("no streamed text arrived")
        except Exception as exc:  # report, do not hide, an unsupported or failing stream
            checks["stream"] = {"error": f"{type(exc).__name__}: {exc}"}
            failures.append("streaming failed")
    else:
        checks["stream"] = {"skipped": "provider has no streaming"}

    typed = Agent(
        name="Typed",
        instructions="Answer about order ORD-7, which is delivered.",
        model=provider,
        output_type=OrderAnswer,
        max_turns=3,
    )
    result = await Runner().run_async(typed, "Give the order id and status.")
    output = result.output
    checks["structured"] = {
        "output": output.model_dump() if isinstance(output, BaseModel) else output,
        "usage": _usage(result.usage),
    }
    if not isinstance(output, OrderAnswer) or output.order_id != "ORD-7":
        failures.append("structured output did not validate as expected")

    close = getattr(provider, "close", None)
    if close is not None:
        await close()

    report["passed"] = not failures
    report["failures"] = failures
    report["checked_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    print("Live check passed." if not failures else f"Live check FAILED: {failures}")
    return 0 if not failures else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--provider", required=True, choices=sorted(PROVIDERS))
    parser.add_argument("--model", help="Model name (defaults to a small model per provider)")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.provider, args.model or PROVIDERS[args.provider][0])))
