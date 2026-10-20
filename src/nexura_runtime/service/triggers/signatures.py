# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Cryptographic HMAC signature generation and replay window verification for webhooks."""

from __future__ import annotations

import hmac
import json
import math
import re
import time
from hashlib import sha256
from typing import Any

from nexura_runtime.service.triggers.errors import (
    InvalidWebhookSignatureError,
    ReplayWindowExceededError,
)
from nexura_runtime.service.triggers.models import WebhookEvent


def serialize_payload(payload: dict[str, Any] | bytes) -> bytes:
    """Normalize payload dictionary into deterministic canonical JSON bytes."""
    if isinstance(payload, bytes):
        return payload
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_:\-]{1,200}$")


def is_valid_event_id(event_id: str) -> bool:
    """Event ids: 1-200 chars of ``[A-Za-z0-9_:-]`` (no ``.``: unambiguous signed string)."""
    return bool(_EVENT_ID_RE.match(event_id))


def compute_webhook_signature(
    secret: str,
    timestamp: float,
    payload: dict[str, Any] | bytes,
    event_id: str | None = None,
) -> str:
    """Compute the HMAC-SHA256 webhook signature (hex).

    With ``event_id`` this is the v2 scheme: HMAC over
    ``v2.<int timestamp>.<event_id>.<canonical body>``, binding the deduplication id to the
    signature. Without ``event_id`` it is the legacy v1 scheme (``v1.<int timestamp>.<body>``),
    which only remains verifiable for endpoints that explicitly allow it.
    """
    payload_bytes = serialize_payload(payload)
    if event_id is None:
        to_sign = f"v1.{int(timestamp)}.".encode() + payload_bytes
    else:
        if not is_valid_event_id(event_id):
            raise ValueError("event_id must be 1-200 characters of [A-Za-z0-9_:-]")
        to_sign = f"v2.{int(timestamp)}.{event_id}.".encode() + payload_bytes
    return hmac.new(secret.encode("utf-8"), to_sign, sha256).hexdigest()


def verify_webhook_signature(
    secret: str,
    event: WebhookEvent,
    *,
    replay_window_seconds: int = 300,
    current_time: float | None = None,
    allow_legacy: bool = False,
) -> None:
    """Verify webhook signature and enforce replay window protection.

    The v2 scheme (covering ``event_id``) is always accepted; the legacy v1 scheme only when
    ``allow_legacy`` is true.

    Raises:
        ReplayWindowExceededError: If the timestamp is outside the replay window.
        InvalidWebhookSignatureError: If the HMAC signature does not match.
    """
    now = current_time if current_time is not None else time.time()
    if not math.isfinite(event.timestamp) or abs(now - event.timestamp) > replay_window_seconds:
        raise ReplayWindowExceededError(
            f"Webhook timestamp {event.timestamp} is outside the acceptable "
            f"{replay_window_seconds}s replay window (server time: {now})."
        )

    # Compare as UTF-8 bytes: str compare_digest raises TypeError on non-ASCII input.
    supplied = event.signature.encode("utf-8", "replace")
    ok = False
    if is_valid_event_id(event.event_id):
        expected_v2 = compute_webhook_signature(
            secret, event.timestamp, event.payload, event.event_id
        )
        ok = hmac.compare_digest(expected_v2.encode("ascii"), supplied)
    if not ok and allow_legacy:
        expected_v1 = compute_webhook_signature(secret, event.timestamp, event.payload)
        ok = hmac.compare_digest(expected_v1.encode("ascii"), supplied)
    if not ok:
        raise InvalidWebhookSignatureError("Webhook HMAC signature verification failed.")
