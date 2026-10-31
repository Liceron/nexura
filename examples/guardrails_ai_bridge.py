# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Guardrails AI bridge with local regex checks and an explicitly local FakeModel.

Run after installing nexura-guardrails-ai. No Hub downloads, LLM calls or network.
"""

from __future__ import annotations

import asyncio
import os

os.environ["GUARDRAILS_PROCESS_COUNT"] = "1"
os.environ["OTEL_SDK_DISABLED"] = "true"

from guardrails import Guard, OnFailAction  # noqa: E402
from guardrails.settings import settings  # noqa: E402
from nexura_agents import Agent, FakeModel, Runner  # noqa: E402
from nexura_guardrails import as_guardrail, pii  # noqa: E402
from nexura_guardrails_ai import GuardrailsAICheck, as_validator  # noqa: E402


async def main() -> None:
    # Process-local test settings, never written to ~/.guardrailsrc.
    settings.rc.enable_metrics = False
    settings.rc.use_remote_inferencing = False
    settings.use_server = False

    validator = as_validator(pii(action="redact"), on_fail=OnFailAction.FIX)
    guard = Guard(use_server=False).use(validator)
    check = GuardrailsAICheck(guard, on_fail="fix", deterministic=True)
    agent = Agent(
        name="local-fake-demo",
        instructions="Offline demonstration using a local FakeModel.",
        model=FakeModel(responses=["Contact bob@example.com"]),
        output_guardrails=[as_guardrail(check, stage="output")],
    )
    result = await Runner().run_async(agent, "Show the contact")
    assert "bob@example.com" not in result.output
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
