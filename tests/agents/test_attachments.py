"""Image and file inputs: prompt building, persistence and each provider's wire format."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from nexura_agents import (
    Agent,
    AnthropicProvider,
    Attachment,
    FakeModel,
    GeminiProvider,
    OpenAIProvider,
    RunInterruption,
    Runner,
    SarvamProvider,
    ToolCall,
    tool,
)
from nexura_agents.errors import SDKValidationError
from nexura_agents.providers.errors import CapabilityError
from nexura_agents.session import deserialize_session, serialize_session
from nexura_agents.types import Message

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
PNG_B64 = base64.b64encode(PNG).decode()


@pytest.fixture
def photo(tmp_path: Path) -> Attachment:
    path = tmp_path / "receipt.png"
    path.write_bytes(PNG)
    return Attachment.from_path(path)


# --------------------------------------------------------------------------- the type


def test_attachment_construction_and_safety(photo: Attachment, tmp_path: Path) -> None:
    assert (photo.mime_type, photo.kind, photo.name, photo.size_bytes) == (
        "image/png",
        "image",
        "receipt.png",
        len(PNG),
    )
    assert photo.data_url == f"data:image/png;base64,{PNG_B64}"
    assert PNG_B64 not in repr(photo)  # never dump payloads into logs

    pdf = Attachment.from_url("https://example.com/files/terms.pdf")
    assert (pdf.mime_type, pdf.kind, pdf.data) == ("application/pdf", "document", None)

    with pytest.raises(SDKValidationError, match="exactly one"):
        Attachment(mime_type="image/png")
    with pytest.raises(SDKValidationError, match="scheme"):
        Attachment.from_url("file:///etc/passwd")
    unknown = tmp_path / "blob.unknownext"
    unknown.write_bytes(b"x")
    with pytest.raises(SDKValidationError, match="mime_type"):
        Attachment.from_path(unknown)
    with pytest.raises(SDKValidationError, match="limited to"):
        Attachment.from_bytes(b"x" * (20 * 1024 * 1024 + 1), "image/png")


async def test_prompt_with_attachments_reaches_the_model(photo: Attachment) -> None:
    model = FakeModel(responses=["A receipt for ₹450."])
    agent = Agent(name="Vision", instructions="Read receipts.", model=model)
    result = await agent.run_async(["What is the total?", photo])

    sent = model.calls[0]["messages"][-1]
    assert (sent.role, sent.content, sent.attachments) == ("user", "What is the total?", [photo])
    assert result.output == "A receipt for ₹450."

    with pytest.raises(SDKValidationError, match="not both"):
        await agent.run_async(["text", Message(role="user", content="x")])
    with pytest.raises(SDKValidationError, match="Unsupported prompt item"):
        await agent.run_async(["text", 42])  # type: ignore[list-item]


def test_sessions_and_paused_runs_keep_attachments(photo: Attachment) -> None:
    messages = [Message(role="user", content="see", attachments=[photo])]
    _, _, restored = deserialize_session(serialize_session("s", 1, messages))
    assert restored[0].attachments == [photo]

    pending = ToolCall(id="c", name="t", arguments={})
    paused = RunInterruption(
        run_id="r",
        agent_name="a",
        messages=[*messages, Message(role="assistant", tool_calls=[pending])],
        pending=[pending],
    )
    assert RunInterruption.from_json(paused.to_json()).messages[0].attachments == [photo]


# --------------------------------------------------------------------------- providers


def capture(reply: dict[str, Any]) -> tuple[list[dict[str, Any]], httpx.MockTransport]:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=reply)

    return bodies, httpx.MockTransport(handler)


OPENAI_REPLY = {
    "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]
}
ANTHROPIC_REPLY = {
    "id": "m",
    "type": "message",
    "role": "assistant",
    "model": "claude",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}
GEMINI_REPLY = {
    "candidates": [
        {"content": {"role": "model", "parts": [{"text": "ok"}]}, "finishReason": "STOP"}
    ]
}


async def test_openai_image_pdf_and_text_parts(photo: Attachment) -> None:
    bodies, transport = capture(OPENAI_REPLY)
    pdf = Attachment.from_bytes(b"%PDF-1.4", "application/pdf", name="terms.pdf")
    note = Attachment.from_bytes(b"refund within 7 days", "text/plain", name="policy.txt")
    url_image = Attachment.from_url("https://cdn.example.com/a.jpg")
    agent = Agent(
        name="O",
        instructions="",
        model=OpenAIProvider("gpt-4.1-mini", api_key="k", transport=transport),
    )
    await agent.run_async(["Check these", photo, pdf, note, url_image])

    content = bodies[0]["messages"][-1]["content"]
    assert content[0] == {"type": "text", "text": "Check these"}
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{PNG_B64}"},
    }
    assert content[2]["type"] == "file" and content[2]["file"]["filename"] == "terms.pdf"
    assert content[3] == {"type": "text", "text": "[policy.txt]\nrefund within 7 days"}
    assert content[4]["image_url"]["url"] == "https://cdn.example.com/a.jpg"


async def test_openai_rejects_unsupported_types_before_sending() -> None:
    bodies, transport = capture(OPENAI_REPLY)
    video = Attachment.from_bytes(b"\x00", "video/mp4", name="clip.mp4")
    agent = Agent(
        name="O",
        instructions="",
        model=OpenAIProvider("gpt-4.1-mini", api_key="k", transport=transport),
    )
    with pytest.raises(CapabilityError, match="OpenAI cannot accept"):
        await agent.run_async(["watch", video])
    assert bodies == []


async def test_anthropic_image_and_document_blocks_come_first(photo: Attachment) -> None:
    bodies, transport = capture(ANTHROPIC_REPLY)
    pdf_url = Attachment.from_url("https://example.com/contract.pdf")
    agent = Agent(
        name="A",
        instructions="Be careful.",
        model=AnthropicProvider(api_key="k", transport=transport),
    )
    await agent.run_async(["Summarise", photo, pdf_url])

    blocks = bodies[0]["messages"][-1]["content"]
    assert blocks[0] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64},
    }
    assert blocks[1] == {
        "type": "document",
        "source": {"type": "url", "url": "https://example.com/contract.pdf"},
    }
    assert blocks[2] == {"type": "text", "text": "Summarise"}
    assert bodies[0]["system"] == "Be careful."


async def test_gemini_inline_and_file_uri_parts(photo: Attachment) -> None:
    bodies, transport = capture(GEMINI_REPLY)
    audio = Attachment.from_bytes(b"RIFF", "audio/wav", name="call.wav")
    stored = Attachment.from_url("gs://bucket/video.mp4")
    agent = Agent(name="G", instructions="", model=GeminiProvider(api_key="k", transport=transport))
    await agent.run_async(["Transcribe and describe", photo, audio, stored])

    parts = bodies[0]["contents"][-1]["parts"]
    assert parts[0] == {"text": "Transcribe and describe"}
    assert parts[1] == {"inlineData": {"mimeType": "image/png", "data": PNG_B64}}
    assert parts[2]["inlineData"]["mimeType"] == "audio/wav"
    assert parts[3] == {"fileData": {"mimeType": "video/mp4", "fileUri": "gs://bucket/video.mp4"}}


async def test_text_only_provider_refuses_attachments(photo: Attachment) -> None:
    bodies, transport = capture(OPENAI_REPLY)
    agent = Agent(name="S", instructions="", model=SarvamProvider(api_key="k", transport=transport))
    with pytest.raises(CapabilityError, match="text only"):
        await agent.run_async(["What is this?", photo])
    assert bodies == []


async def test_attachments_raise_the_budget_reservation(photo: Attachment) -> None:
    from nexura_agents.providers.types import GenerationRequest

    provider = OpenAIProvider("gpt-4.1-mini", api_key="k")
    plain = GenerationRequest(messages=[Message(role="user", content="hi")])
    with_image = GenerationRequest(
        messages=[Message(role="user", content="hi", attachments=[photo])]
    )
    assert provider._estimate_tokens(with_image) >= provider._estimate_tokens(plain) + 1600


async def test_tools_still_work_alongside_attachments(photo: Attachment) -> None:
    @tool
    def lookup(order_id: str) -> str:
        """Find an order."""
        return "paid"

    model = FakeModel(
        responses=[
            [ToolCall(id="1", name="lookup", arguments={"order_id": "A1"})],
            "Order A1 is paid.",
        ]
    )
    result = await Runner().run_async(
        Agent(name="T", instructions="", model=model, tools=[lookup]), ["Status?", photo]
    )
    assert result.output == "Order A1 is paid."
    second_turn = model.calls[1]["messages"]
    user = next(m for m in second_turn if m.role == "user")
    assert user.attachments == [photo]  # the image stays in history for later turns
