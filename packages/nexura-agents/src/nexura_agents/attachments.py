# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Images, documents and audio passed to a model alongside text.

from nexura_agents import Attachment

agent.run(["What is wrong with this invoice?", Attachment.from_path("invoice.pdf")])
"""

from __future__ import annotations

import base64
import mimetypes
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from nexura_agents.errors import SDKValidationError

AttachmentKind = Literal["image", "document", "audio", "video", "text"]

# Inline data is base64-encoded into every request; keep it below provider limits
# (OpenAI and Anthropic reject larger requests). Use a URL for bigger files.
MAX_INLINE_BYTES = 20 * 1024 * 1024

_EXTRA_TYPES = {".webp": "image/webp", ".md": "text/markdown", ".heic": "image/heic"}


def _kind(mime_type: str) -> AttachmentKind:
    major = mime_type.split("/", 1)[0]
    if major in ("image", "audio", "video", "text"):
        return major  # type: ignore[return-value]
    return "document"


def _redact_url(url: str) -> str:
    """Drop userinfo, query and fragment: presigned URLs carry secrets there."""
    parts = urlsplit(url)
    host = parts.netloc.rpartition("@")[2]
    return f"{parts.scheme}://{host}{parts.path}" + ("?..." if parts.query else "")


@dataclass(frozen=True)
class Attachment:
    """A file given to the model: inline bytes (base64) or a URL the provider fetches.

    Build one with ``from_path``, ``from_bytes`` or ``from_url``. Which kinds a model
    accepts depends on the provider; unsupported ones fail before any request is sent.
    """

    mime_type: str
    data: str | None = None  # base64, no "data:" prefix
    url: str | None = None
    name: str | None = None

    def __post_init__(self) -> None:
        if (self.data is None) == (self.url is None):
            raise SDKValidationError("Attachment needs exactly one of data or url.")
        if "/" not in self.mime_type:
            raise SDKValidationError(f"Invalid MIME type for attachment: {self.mime_type!r}.")

    @property
    def kind(self) -> AttachmentKind:
        return _kind(self.mime_type)

    @property
    def data_url(self) -> str:
        """``data:<mime>;base64,<data>`` for inline attachments, else the URL."""
        if self.data is None:
            assert self.url is not None
            return self.url
        return f"data:{self.mime_type};base64,{self.data}"

    @property
    def size_bytes(self) -> int | None:
        """Decoded size of inline data; ``None`` for URLs."""
        if self.data is None:
            return None
        return len(self.data) * 3 // 4 - self.data.count("=")

    @classmethod
    def from_bytes(cls, data: bytes, mime_type: str, *, name: str | None = None) -> Attachment:
        """Base64-encode bytes into an inline attachment; reject data over the size limit."""
        if len(data) > MAX_INLINE_BYTES:
            raise SDKValidationError(
                f"Attachment {name or ''} is {len(data):,} bytes; inline attachments are "
                f"limited to {MAX_INLINE_BYTES:,}. Pass a URL with Attachment.from_url()."
            )
        return cls(mime_type=mime_type, data=base64.b64encode(data).decode("ascii"), name=name)

    @classmethod
    def from_path(cls, path: str | Path, *, mime_type: str | None = None) -> Attachment:
        """Read a local file; the MIME type is guessed from the extension."""
        file = Path(path)
        guessed = mime_type or _EXTRA_TYPES.get(file.suffix.lower())
        guessed = guessed or mimetypes.guess_type(file.name)[0]
        if guessed is None:
            raise SDKValidationError(
                f"Cannot tell the type of '{file.name}'; pass mime_type=, e.g. 'application/pdf'."
            )
        too_big = SDKValidationError(
            f"Attachment {file.name} is larger than {MAX_INLINE_BYTES:,} bytes; inline "
            "attachments are limited. Pass a URL with Attachment.from_url()."
        )
        # Check the size before reading so a huge file is never loaded into memory.
        info = file.stat()
        if not stat.S_ISREG(info.st_mode):
            raise SDKValidationError(f"'{file.name}' is not a regular file.")
        if info.st_size > MAX_INLINE_BYTES:
            raise too_big
        with file.open("rb") as handle:
            data = handle.read(MAX_INLINE_BYTES + 1)  # the file may have grown since stat
        if len(data) > MAX_INLINE_BYTES:
            raise too_big
        return cls.from_bytes(data, guessed, name=file.name)

    @classmethod
    def from_url(cls, url: str, *, mime_type: str | None = None) -> Attachment:
        """A file the provider downloads itself (``https://``, or ``gs://`` on Vertex AI)."""
        if not url.startswith(("https://", "http://", "gs://")):
            raise SDKValidationError(f"Unsupported attachment URL scheme: {url!r}.")
        guessed = mime_type or mimetypes.guess_type(url.split("?", 1)[0])[0]
        if guessed is None:
            raise SDKValidationError(f"Cannot tell the type of {url!r}; pass mime_type=.")
        return cls(
            mime_type=guessed,
            url=url,
            name=url.split("?", 1)[0].split("#", 1)[0].rsplit("/", 1)[-1] or None,
        )

    def to_dict(self) -> dict[str, str]:
        """Return a dict of the attachment's set fields (``mime_type`` plus data, url, name)."""
        out = {"mime_type": self.mime_type}
        for key in ("data", "url", "name"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, str]) -> Attachment:
        """Rebuild an attachment from the dict produced by ``to_dict``."""
        return cls(
            mime_type=raw["mime_type"],
            data=raw.get("data"),
            url=raw.get("url"),
            name=raw.get("name"),
        )

    def __repr__(self) -> str:  # never print the base64 payload
        source = (
            f"url={_redact_url(self.url)!r}" if self.url else f"{self.size_bytes:,} bytes inline"
        )
        return f"Attachment({self.mime_type}, {source}, name={self.name!r})"
