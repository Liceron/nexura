# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Slack Web API connector (bot token)."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from nexura_connectors._core import (
    Call,
    Op,
    StrictParams,
    clamp,
    result_data,
)
from nexura_connectors._rest import RestConnector
from nexura_connectors.errors import (
    AuthFailedError,
    RateLimitedError,
    UpstreamError,
)


class PostMessage(StrictParams):
    """Parameters for chat.postMessage."""

    channel: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=4000)
    thread_ts: str | None = Field(default=None, max_length=32)


class ListChannels(StrictParams):
    """Parameters for conversations.list."""

    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None


class History(StrictParams):
    """Parameters for conversations.history."""

    channel: str = Field(min_length=1, max_length=64)
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None


def _check_ok(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise UpstreamError("unexpected Slack response shape")
    if payload.get("ok") is True:
        return payload
    err = str(payload.get("error", "unknown_error"))[:60]
    if err in {"invalid_auth", "not_authed", "token_revoked", "account_inactive", "token_expired"}:
        raise AuthFailedError(f"Slack rejected the credential ({err})")
    if err == "ratelimited":
        raise RateLimitedError("Slack rate limit reached")
    raise UpstreamError(f"Slack API error: {err}")


class SlackConnector(RestConnector):
    """Post messages and read channels and history in Slack."""

    MODULE_ID: ClassVar[str] = "nexura.connectors.slack"
    TITLE: ClassVar[str] = "Slack"
    DESCRIPTION: ClassVar[str] = "Slack Web API with a bot token held in the secrets store."
    CREDENTIAL_TYPE: ClassVar[str] = "slack_bot_token"
    DEFAULT_BASE_URL: ClassVar[str | None] = "https://slack.com/api"
    OPS: ClassVar[dict[str, Op]] = {
        "post_message": Op("post_message", True, PostMessage),
        "list_channels": Op("list_channels", False, ListChannels),
        "read_history": Op("read_history", False, History),
    }

    async def op_post_message(self, p: PostMessage, call: Call) -> dict[str, Any]:
        """Post a message to a channel (write)."""
        body = {"channel": p.channel, "text": p.text}
        if p.thread_ts:
            body["thread_ts"] = p.thread_ts
        resp = await self.api(call, "POST", "/chat.postMessage", json=body, write=True)
        data = _check_ok(resp.json())
        return result_data({"channel": data.get("channel"), "ts": data.get("ts")})

    async def _paged(
        self, call: Call, path: str, params: dict[str, Any], key: str, limit: int
    ) -> dict[str, Any]:
        items: list[Any] = []
        cursor = params.get("cursor")
        for _ in range(call.max_pages):
            query = {**params, "limit": clamp(limit, 1, 200)}
            if cursor:
                query["cursor"] = cursor
            data = _check_ok((await self.api(call, "GET", path, params=query)).json())
            items.extend(data.get(key, []))
            cursor = (data.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break
        return result_data(items, next_cursor=cursor, truncated=bool(cursor))

    async def op_list_channels(self, p: ListChannels, call: Call) -> dict[str, Any]:
        """List channels with cursor pagination (read)."""
        return await self._paged(
            call, "/conversations.list", {"cursor": p.cursor}, "channels", p.limit
        )

    async def op_read_history(self, p: History, call: Call) -> dict[str, Any]:
        """Read recent messages of a channel (read)."""
        return await self._paged(
            call,
            "/conversations.history",
            {"channel": p.channel, "cursor": p.cursor},
            "messages",
            p.limit,
        )
