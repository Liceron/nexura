# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Shared hosted connection settings; no dependency on the runtime or enterprise edition."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit


def credentials_path() -> Path:
    return Path.home() / ".config" / "nexura" / "credentials.json"


def validate_url(url: str) -> str:
    parts = urlsplit(url)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError("Nexura URL must be an HTTP(S) service URL without credentials or query.")
    if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Hosted Nexura requires HTTPS; HTTP is supported only on loopback.")
    return url.rstrip("/")


def resolve_connection(url: str | None = None, api_key: str | None = None) -> tuple[str, str]:
    """Explicit arguments, then environment, then the CLI login file.

    A saved key is never sent to a different URL supplied through arguments/environment.
    """
    saved: dict[str, str] = {}
    path = credentials_path()
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                saved = {k: v for k, v in value.items() if isinstance(v, str)}
        except (OSError, ValueError):
            pass
    target = url if url is not None else os.environ.get("NEXURA_URL", saved.get("url", ""))
    if not target:
        raise ValueError(
            "Not logged in. Run nexura-runtime login --url URL, "
            "or set NEXURA_URL and NEXURA_API_KEY."
        )
    target = validate_url(target)
    key = api_key if api_key is not None else os.environ.get("NEXURA_API_KEY")
    if key is None and saved.get("url", "").rstrip("/") == target:
        key = saved.get("api_key")
    if not key or not key.strip() or "\n" in key or "\r" in key:
        raise ValueError("Provide NEXURA_API_KEY or run nexura-runtime login --url URL.")
    return target, key.strip()
