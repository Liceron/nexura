# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Translate attachments into each provider's wire format, rejecting what it can't accept.

Support follows each provider's public API docs (October 2026):
- OpenAI Chat Completions: images (inline or URL), PDF files (inline), audio wav/mp3 (inline).
- Anthropic Messages: images and PDFs (inline or URL), plain text documents (inline).
- Gemini / Vertex AI: any MIME type inline; URLs as ``fileData`` (Vertex: gs:// and
  HTTPS; Gemini API: File API URIs).
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import Any

from nexura_agents.attachments import Attachment
from nexura_agents.providers.errors import CapabilityError
from nexura_agents.providers.types import ProviderMessage
from nexura_agents.types import Message

_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
_OPENAI_AUDIO = {"audio/wav": "wav", "audio/x-wav": "wav", "audio/mpeg": "mp3", "audio/mp3": "mp3"}


def attachments_in(messages: Sequence[Message | ProviderMessage]) -> list[Attachment]:
    found: list[Attachment] = []
    for msg in messages:
        legacy = msg.to_legacy() if isinstance(msg, ProviderMessage) else msg
        found.extend(legacy.attachments or [])
    return found


def _unsupported(provider: str, attachment: Attachment, reason: str) -> CapabilityError:
    return CapabilityError(
        f"{provider} cannot accept {attachment!r}: {reason}",
        provider_name=provider,
        capability=f"attachment:{attachment.mime_type}",
    )


def _reject_gs(provider: str, attachment: Attachment) -> None:
    if attachment.url is not None and attachment.url.startswith("gs://"):
        raise _unsupported(
            provider, attachment, "gs:// URLs are not fetchable; use HTTPS or inline data."
        )


def openai_content_parts(text: str | None, attachments: Sequence[Attachment]) -> list[Any]:
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})
    for a in attachments:
        _reject_gs("OpenAI", a)
        if a.mime_type in _IMAGE_TYPES:
            parts.append({"type": "image_url", "image_url": {"url": a.data_url}})
        elif a.mime_type == "application/pdf" and a.data is not None:
            parts.append(
                {
                    "type": "file",
                    "file": {"filename": a.name or "document.pdf", "file_data": a.data_url},
                }
            )
        elif a.mime_type in _OPENAI_AUDIO and a.data is not None:
            parts.append(
                {
                    "type": "input_audio",
                    "input_audio": {"data": a.data, "format": _OPENAI_AUDIO[a.mime_type]},
                }
            )
        elif a.mime_type.startswith("text/") and a.data is not None:
            body = base64.b64decode(a.data).decode("utf-8", errors="replace")
            parts.append({"type": "text", "text": f"[{a.name or 'file'}]\n{body}"})
        else:
            raise _unsupported(
                "OpenAI",
                a,
                "supported are JPEG/PNG/GIF/WebP images, inline PDF, inline WAV/MP3 and "
                "inline text files.",
            )
    return parts


def anthropic_blocks(attachments: Sequence[Attachment]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for a in attachments:
        _reject_gs("Anthropic", a)
        if a.mime_type in _IMAGE_TYPES or a.mime_type == "application/pdf":
            block_type = "image" if a.mime_type in _IMAGE_TYPES else "document"
            source = (
                {"type": "base64", "media_type": a.mime_type, "data": a.data}
                if a.data is not None
                else {"type": "url", "url": a.url}
            )
            blocks.append({"type": block_type, "source": source})
        elif a.mime_type.startswith("text/") and a.data is not None:
            text = base64.b64decode(a.data).decode("utf-8", errors="replace")
            blocks.append(
                {
                    "type": "document",
                    "source": {"type": "text", "media_type": "text/plain", "data": text},
                    "title": a.name or "file",
                }
            )
        else:
            raise _unsupported(
                "Anthropic", a, "supported are JPEG/PNG/GIF/WebP images, PDF and inline text."
            )
    return blocks


def gemini_parts(attachments: Sequence[Attachment]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for a in attachments:
        if a.data is not None:
            parts.append({"inlineData": {"mimeType": a.mime_type, "data": a.data}})
        else:
            parts.append({"fileData": {"mimeType": a.mime_type, "fileUri": a.url}})
    return parts


def reject_attachments(provider: str, messages: Sequence[Message | ProviderMessage]) -> None:
    """For providers without multimodal input: fail before sending anything."""
    found = attachments_in(messages)
    if found:
        raise _unsupported(provider, found[0], "this provider accepts text only.")
