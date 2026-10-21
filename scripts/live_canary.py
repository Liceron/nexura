# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Opt-in live canaries: one tiny fixed scenario per model provider, against the real API.

This script makes REAL, BILLED calls. It does nothing unless BOTH are true:

* ``NEXURA_LIVE_CANARY=1`` is set in the environment, and
* the provider's credential is set (``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``,
  ``GEMINI_API_KEY``/``GOOGLE_API_KEY``, ``SARVAM_API_KEY``). Vertex AI uses Google
  Application Default Credentials and also needs ``NEXURA_CANARY_VERTEX_PROJECT``; a local
  Ollama needs ``NEXURA_CANARY_OLLAMA_MODEL`` (for example ``qwen3:0.6b``).

Without them each provider is reported as skipped and nothing is sent. CI never sets
``NEXURA_LIVE_CANARY`` (``tests/providers/test_live_canary_guard.py`` checks every workflow),
so the normal test suite stays keyless.

Scenario per provider (four requests, each capped at 256 output tokens):

1. plain reply ("Reply with exactly: ok"),
2. one tool call and the follow-up answer (an agent run: two model calls),
3. a streamed reply ("Count from 1 to 5."),
4. a structured answer validated against a JSON schema.

Cost: roughly 600 to 2,000 tokens per provider per run (system text, the tool schema and the
answers; reasoning models add their thinking tokens). At October 2026 list prices of the
default models (gpt-5.4-nano, claude-haiku-5-5, gemini-3.8-flash, sarvam-105b) that is well
under one US cent per provider. Ollama runs locally and costs nothing.

Usage::

    export NEXURA_LIVE_CANARY=1 OPENAI_API_KEY=...
    uv run python scripts/live_canary.py                  # every provider with a key
    uv run python scripts/live_canary.py --provider anthropic --model claude-haiku-5-5

Each provider that runs writes ``<out-dir>/live_canary_<provider>_<YYYY-MM-DD>.json`` (default
``evidence/live_canary``): the model, every check with its usage, pass/fail and the UTC time.
Credentials are never written; the evidence names the variable and a masked value
(``sk-...(164 chars)``). Send the file back so docs/providers.md can record the live date.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENABLE_VAR = "NEXURA_LIVE_CANARY"
MAX_TOKENS = 256


@dataclass(frozen=True)
class Target:
    name: str
    default_model: str
    credential_vars: tuple[str, ...]
    build: Callable[[str], Any]


def _openai(model: str) -> Any:
    from nexura_agents.providers.openai import OpenAIProvider

    return OpenAIProvider(model, timeout=60)


def _openai_responses(model: str) -> Any:
    from nexura_agents.providers.openai import OpenAIResponsesProvider

    return OpenAIResponsesProvider(model, timeout=60)


def _anthropic(model: str) -> Any:
    from nexura_agents.providers.anthropic import AnthropicNativeProvider

    return AnthropicNativeProvider(model, timeout=60)


def _gemini(model: str) -> Any:
    from nexura_agents.providers.gemini import GeminiNativeProvider

    return GeminiNativeProvider(model, timeout=60)


def _sarvam(model: str) -> Any:
    from nexura_agents.providers.sarvam import SarvamProvider

    return SarvamProvider(model, timeout=60)


def _ollama(model: str) -> Any:
    from nexura_agents.providers.openai import OpenAIProvider

    base = os.environ.get("NEXURA_CANARY_OLLAMA_URL", "http://localhost:11434/v1")
    return OpenAIProvider(model, base_url=base, api_key="ollama", timeout=120)


def _vertex(model: str) -> Any:
    from nexura_vertex import VertexAIProvider

    return VertexAIProvider(
        model,
        project=os.environ["NEXURA_CANARY_VERTEX_PROJECT"],
        location=os.environ.get("NEXURA_CANARY_VERTEX_LOCATION", "global"),
        timeout=60,
    )


TARGETS: dict[str, Target] = {
    t.name: t
    for t in (
        Target("openai", "gpt-5.4-nano", ("OPENAI_API_KEY",), _openai),
        Target("openai-responses", "gpt-5.4-nano", ("OPENAI_API_KEY",), _openai_responses),
        Target("anthropic", "claude-haiku-5-5", ("ANTHROPIC_API_KEY",), _anthropic),
        Target("gemini", "gemini-3.8-flash", ("GEMINI_API_KEY", "GOOGLE_API_KEY"), _gemini),
        Target("sarvam", "sarvam-105b", ("SARVAM_API_KEY",), _sarvam),
        Target("vertex", "gemini-3.8-flash", ("NEXURA_CANARY_VERTEX_PROJECT",), _vertex),
        Target("ollama", "", ("NEXURA_CANARY_OLLAMA_MODEL",), _ollama),
    )
}


def mask(value: str) -> str:
    """A credential as ``abc...(N chars)``: enough to tell keys apart, never the key."""
    return f"{value[:3]}...({len(value)} chars)" if value else ""


def enabled(env: dict[str, str] | None = None) -> bool:
    return (env if env is not None else os.environ).get(ENABLE_VAR) == "1"


def credential(target: Target, env: dict[str, str] | None = None) -> tuple[str, str] | None:
    source = env if env is not None else os.environ
    for var in target.credential_vars:
        if source.get(var):
            return var, source[var]
    return None


def _usage(usage: Any) -> dict[str, Any] | None:
    return dict(vars(usage)) if usage is not None else None


async def run_scenario(provider: Any) -> tuple[dict[str, Any], list[str]]:
    """The fixed four-step scenario. Returns (checks, failures)."""
    from nexura_agents import Agent, ModelSettings, Runner, tool
    from nexura_agents.providers.types import GenerationRequest
    from nexura_agents.types import Message

    @tool
    def get_order_status(order_id: str) -> dict[str, str]:
        """Look up an order's delivery status."""
        return {"order_id": order_id, "status": "out for delivery"}

    settings = ModelSettings(max_tokens=MAX_TOKENS)
    checks: dict[str, Any] = {}
    failures: list[str] = []

    started = time.perf_counter()
    plain = await provider.generate(
        GenerationRequest(
            messages=[Message(role="user", content="Reply with exactly: ok")],
            max_tokens=MAX_TOKENS,
        )
    )
    text = (plain.message.content or "").strip()
    checks["plain"] = {
        "text": text[:200],
        "finish_reason": plain.finish_reason,
        "usage": _usage(plain.usage),
        "ms": round((time.perf_counter() - started) * 1000),
    }
    if not text.lower().startswith("ok"):
        failures.append("plain reply was not 'ok'")
    if plain.usage is None or not plain.usage.is_reported:
        failures.append("plain reply reported no usage")

    agent = Agent(
        name="Canary",
        instructions="Use the tool to answer. Be brief.",
        model=provider,
        tools=[get_order_status],
        model_settings=settings,
        max_turns=4,
    )
    run = await Runner().run_async(agent, "What is the status of order ORD-42?")
    checks["tool_call"] = {
        "tool_calls": run.tool_calls_count,
        "answer": str(run.final_output)[:200],
        "usage": _usage(run.usage),
    }
    if run.tool_calls_count < 1:
        failures.append("model did not call the tool")

    if getattr(getattr(provider, "capabilities", None), "can_stream", False):
        try:
            chunks: list[str] = []
            last_usage = None
            async for delta in provider.stream(
                GenerationRequest(
                    messages=[Message(role="user", content="Count from 1 to 5.")],
                    max_tokens=MAX_TOKENS,
                )
            ):
                if delta.kind == "text_delta" and delta.text:
                    chunks.append(delta.text)
                if delta.usage is not None:
                    last_usage = delta.usage
            checks["stream"] = {
                "chunks": len(chunks),
                "text": "".join(chunks).strip()[:200],
                "usage": _usage(last_usage),
            }
            if not chunks:
                failures.append("no streamed text arrived")
        except Exception as exc:  # report, never hide, a failing stream
            checks["stream"] = {"error": f"{type(exc).__name__}: {exc}"[:500]}
            failures.append("streaming failed")
    else:
        checks["stream"] = {"skipped": "provider does not stream"}

    schema = {
        "type": "object",
        "properties": {"order_id": {"type": "string"}, "status": {"type": "string"}},
        "required": ["order_id", "status"],
    }
    typed = Agent(
        name="Typed",
        instructions="Answer about order ORD-7, which is delivered.",
        model=provider,
        output_type=schema,
        model_settings=settings,
        max_turns=3,
    )
    result = await Runner().run_async(typed, "Give the order id and status.")
    checks["structured"] = {"output": result.output, "usage": _usage(result.usage)}
    if not isinstance(result.output, dict) or result.output.get("order_id") != "ORD-7":
        failures.append("structured output did not validate as expected")
    return checks, failures


async def run_target(target: Target, model: str, out_dir: Path) -> dict[str, Any]:
    cred = credential(target)
    report: dict[str, Any] = {"provider": target.name, "model": model}
    if not enabled() or cred is None:
        report["skipped"] = (
            f"set {ENABLE_VAR}=1" if not enabled() else f"set {' or '.join(target.credential_vars)}"
        )
        return report
    var, value = cred
    report["credential"] = {"variable": var, "value": mask(value)}
    provider = target.build(model)
    try:
        checks, failures = await run_scenario(provider)
    except Exception as exc:
        checks, failures = {}, [f"{type(exc).__name__}: {exc}"[:500]]
    finally:
        for name in ("aclose", "close"):
            closer = getattr(provider, name, None)
            if closer is not None:
                result = closer()
                if asyncio.iscoroutine(result):
                    await result
                break
    report.update(
        checks=checks,
        passed=not failures,
        failures=failures,
        checked_at_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    for secret_var in target.credential_vars:
        secret = os.environ.get(secret_var)
        if secret and len(secret) >= 8 and secret in text:  # belt and braces
            text = text.replace(secret, mask(secret))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"live_canary_{target.name}_{time.strftime('%Y-%m-%d', time.gmtime())}.json"
    path.write_text(text + "\n", encoding="utf-8")
    report["evidence"] = str(path)
    return report


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--provider", choices=sorted(TARGETS), action="append")
    parser.add_argument("--model", help="Model for a single --provider (default per provider)")
    parser.add_argument("--out-dir", default="evidence/live_canary", type=Path)
    args = parser.parse_args(argv)
    names = args.provider or sorted(TARGETS)
    if args.model and len(names) != 1:
        parser.error("--model needs exactly one --provider")
    if not enabled():
        print(f"{ENABLE_VAR} is not 1: live canaries are off, nothing was sent.")
    failed = False
    for name in names:
        target = TARGETS[name]
        model = args.model or target.default_model
        if name == "ollama":
            model = args.model or os.environ.get("NEXURA_CANARY_OLLAMA_MODEL", "")
        report = await run_target(target, model, args.out_dir)
        if "skipped" in report:
            print(f"{name}: skipped ({report['skipped']})")
            continue
        status = "passed" if report["passed"] else f"FAILED {report['failures']}"
        print(f"{name} ({model}): {status} -> {report['evidence']}")
        failed = failed or not report["passed"]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
