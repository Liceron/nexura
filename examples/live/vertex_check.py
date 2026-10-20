"""Live Vertex AI check — makes real, billed calls to your Google Cloud project.

Run it yourself (it is never run by tests or CI):

    gcloud auth application-default login                       # once per machine
    gcloud auth application-default set-quota-project PROJECT    # bill your project
    uv run python examples/live/vertex_check.py --project PROJECT

Uses Application Default Credentials. Without a login on a GCE VM these fall back to
the VM's service account, which usually belongs to a different project.

Each run sends three short requests (plain text, a tool call, and a streamed reply),
roughly a few hundred tokens in total.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

from nexura_agents import Agent, Runner, tool
from nexura_agents.types import Message
from nexura_vertex import VertexAIProvider


@tool
def get_order_status(order_id: str) -> dict[str, str]:
    """Look up an order's delivery status."""
    return {"order_id": order_id, "status": "out for delivery"}


async def main(model: str, location: str, project: str | None) -> None:
    provider = VertexAIProvider(model, location=location, project=project)
    print(f"Calling Vertex AI in project '{provider.project}' using {provider.credential_source}.")
    report: dict[str, object] = {"model": model, "project": provider.project, "location": location}

    t0 = time.perf_counter()
    plain = await provider.generate([Message(role="user", content="Reply with exactly: ok")])
    report["plain"] = {
        "text": (plain.message.content or "").strip(),
        "usage": vars(plain.usage) if plain.usage else None,
        "ms": round((time.perf_counter() - t0) * 1000),
    }

    agent = Agent(
        name="LiveCheck",
        instructions="Use the tool to answer. Be brief.",
        model=provider,
        tools=[get_order_status],
        max_turns=4,
    )
    run = await Runner().run_async(agent, "What is the status of order ORD-42?")
    report["tool_call"] = {
        "tool_calls": run.tool_calls_count,
        "answer": run.final_output,
        "usage": vars(run.usage) if run.usage else None,
    }

    stream = provider.stream([Message(role="user", content="Count from 1 to 5.")])
    chunks = [d.text async for d in stream if d.kind == "text_delta" and d.text]
    report["stream"] = {"chunks": len(chunks), "text": "".join(chunks).strip()}

    await provider.close()
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    assert report["plain"]["text"].lower().startswith("ok"), "plain reply was unexpected"  # type: ignore[index]
    assert run.tool_calls_count >= 1, "model did not call the tool"
    assert chunks, "no streamed text arrived"
    print("Live Vertex AI check passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gemini-2.5-flash")
    parser.add_argument("--location", default="global")
    parser.add_argument("--project", help="Google Cloud project (default: GOOGLE_CLOUD_PROJECT)")
    args = parser.parse_args()
    asyncio.run(main(args.model, args.location, args.project))
