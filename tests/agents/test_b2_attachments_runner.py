"""Regression tests: resume request limits, attachment size/URL handling, guardrail input."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from nexura_agents import Agent, Attachment, FakeModel, Runner, ToolCall, tool
from nexura_agents.attachments import MAX_INLINE_BYTES
from nexura_agents.errors import NexuraError, SDKValidationError
from nexura_agents.guardrails import GuardrailResult
from nexura_agents.limits import UsageLimits
from nexura_agents.providers.errors import CapabilityError
from nexura_agents.providers.multimodal import openai_content_parts
from nexura_agents.types import RunInterruption


async def _paused_run() -> tuple[Runner, Agent, RunInterruption]:
    @tool(requires_approval=True)
    def pay(amount: int) -> str:
        return "ok"

    model = FakeModel(responses=[[ToolCall(id="c1", name="pay", arguments={"amount": 1})], "done"])
    agent = Agent(name="A", instructions="", model=model, tools=[pay])
    runner = Runner(usage_limits=UsageLimits(request_limit=1, allow_unknown_usage=True))
    paused = await runner.run_async(agent, "go")
    assert paused.interruption is not None
    return runner, agent, paused.interruption


async def test_request_limit_survives_resume_and_json_round_trip() -> None:
    runner, agent, interruption = await _paused_run()
    assert interruption.requests == 1
    restored = RunInterruption.from_json(interruption.to_json())
    assert restored.requests == 1
    with pytest.raises(NexuraError):
        await runner.resume_async(agent, restored, approve=True)


async def test_interruption_from_older_payload_defaults_requests_to_zero() -> None:
    import json

    _, _, interruption = await _paused_run()
    raw = json.loads(interruption.to_json())
    del raw["requests"]
    assert RunInterruption.from_json(json.dumps(raw)).requests == 0


def test_from_path_rejects_oversized_file_without_reading_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = tmp_path / "big.pdf"
    with open(big, "wb") as handle:
        handle.truncate(MAX_INLINE_BYTES + 1)  # sparse

    def boom(self: Path) -> bytes:
        raise AssertionError("whole oversized file was read into memory")

    monkeypatch.setattr(Path, "read_bytes", boom)
    with pytest.raises(SDKValidationError, match="inline attachments are limited"):
        Attachment.from_path(big)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_from_path_rejects_non_regular_files(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe.pdf"
    os.mkfifo(fifo)
    with pytest.raises(SDKValidationError, match="not a regular file"):
        Attachment.from_path(fifo)


async def test_input_guardrail_gets_a_string_for_attachment_only_prompt() -> None:
    seen: list[object] = []

    def check(ctx: object, value: str) -> GuardrailResult:
        seen.append(value)
        return GuardrailResult.passed()

    agent = Agent(
        name="A", instructions="", model=FakeModel(responses=["ok"]), input_guardrails=[check]
    )
    await Runner().run_async(agent, [Attachment.from_bytes(b"x", "image/png")])
    assert seen == [""]


def test_presigned_url_secrets_are_not_leaked() -> None:
    url = "https://user:PASSWD@bucket.s3.amazonaws.com/a.zip?X-Amz-Signature=SECRETSIG"
    attachment = Attachment.from_url(url, mime_type="application/zip")
    assert attachment.name == "a.zip"
    for text in (repr(attachment), attachment.name):
        assert "SECRETSIG" not in text
        assert "PASSWD" not in text
    with pytest.raises(CapabilityError) as info:
        openai_content_parts("hi", [attachment])
    assert "SECRETSIG" not in str(info.value)
    assert "PASSWD" not in str(info.value)
