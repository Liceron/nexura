# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Slack Web API connector (bot token)."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from nexura_connectors._auth import RefreshingRestConnector
from nexura_connectors._core import (
    Call,
    Op,
    StrictParams,
    clamp,
    result_data,
)
from nexura_connectors._pagination import page_items, partial, resume
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
    client_msg_id: str | None = Field(
        default=None, pattern=r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$"
    )


class ListChannels(StrictParams):
    """Parameters for conversations.list."""

    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None
    max_items: int | None = Field(default=None, ge=1, le=10000)


class History(StrictParams):
    """Parameters for conversations.history."""

    channel: str = Field(min_length=1, max_length=64)
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None
    max_items: int | None = Field(default=None, ge=1, le=10000)


def _check_ok(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise UpstreamError("unexpected Slack response shape")
    if payload.get("ok") is True:
        return payload
    err = payload.get("error")
    if not isinstance(err, str):
        raise UpstreamError("Slack returned an invalid error; check scopes and parameters")
    if err in {"invalid_auth", "not_authed", "token_revoked", "account_inactive", "token_expired"}:
        raise AuthFailedError(f"Slack rejected the credential ({err})")
    if err == "ratelimited":
        raise RateLimitedError("Slack rate limit reached")
    if err in {
        "channel_not_found",
        "not_in_channel",
        "missing_scope",
        "is_archived",
        "invalid_arguments",
    }:
        raise UpstreamError(
            f"Slack API error: {err}; check channel membership, scopes and parameters"
        )
    raise UpstreamError(
        "Slack API rejected the operation; check token scopes, channel and parameters"
    )


class SlackConnector(RefreshingRestConnector):
    """Post messages and read channels and history in Slack."""

    MODULE_ID: ClassVar[str] = "nexura.connectors.slack"
    TITLE: ClassVar[str] = "Slack"
    DESCRIPTION: ClassVar[str] = "Slack Web API with a bot token held in the secrets store."
    CREDENTIAL_TYPE: ClassVar[str] = "slack_bot_token"
    DEFAULT_BASE_URL: ClassVar[str | None] = "https://slack.com/api"
    AUTH_FAMILY: ClassVar[str] = "slack"
    TOKEN_URL: ClassVar[str] = "https://slack.com/api/oauth.v2.access"
    OPS: ClassVar[dict[str, Op]] = {
        "post_message": Op("post_message", True, PostMessage, effect_class="non_idempotent"),
        "list_channels": Op("list_channels", False, ListChannels, effect_class="pure"),
        "read_history": Op("read_history", False, History, effect_class="pure"),
    }

    async def op_post_message(self, p: PostMessage, call: Call) -> dict[str, Any]:
        """Post a message to a channel (write)."""
        body = {"channel": p.channel, "text": p.text}
        if p.thread_ts:
            body["thread_ts"] = p.thread_ts
        if p.client_msg_id:
            body["client_msg_id"] = p.client_msg_id
        elif call.context.idempotency_key():
            from uuid import NAMESPACE_URL, uuid5

            body["client_msg_id"] = str(uuid5(NAMESPACE_URL, call.context.idempotency_key() or ""))
        resp = await self.api(call, "POST", "/chat.postMessage", json=body, write=True)
        data = _check_ok(resp.json())
        return result_data({"channel": data.get("channel"), "ts": data.get("ts")})

    async def _paged(
        self,
        call: Call,
        path: str,
        params: dict[str, Any],
        key: str,
        limit: int,
        max_items: int | None,
    ) -> dict[str, Any]:
        items: list[Any] = []
        cursor, offset, limit = resume(params.get("cursor"), limit, 200)
        seen: set[str | None] = set()
        for _ in range(call.max_pages):
            current = cursor
            seen.add(current)
            query = {k: v for k, v in params.items() if k != "cursor"}
            query["limit"] = clamp(limit, 1, 200)
            if cursor:
                query["cursor"] = cursor
            data = _check_ok((await self.api(call, "GET", path, params=query)).json())
            page = page_items(data, key, offset)
            remaining = len(page) if max_items is None else max_items - len(items)
            items.extend(page[offset : offset + remaining])
            if offset + remaining < len(page):
                return result_data(
                    items, next_cursor=partial(current, offset + remaining, limit), truncated=True
                )
            offset = 0
            metadata = data.get("response_metadata") or {}
            if not isinstance(metadata, dict):
                raise UpstreamError("Slack pagination metadata must be an object")
            cursor = metadata.get("next_cursor") or None
            if cursor is not None and not isinstance(cursor, str):
                raise UpstreamError("Slack next_cursor must be a string")
            cursor = cursor.strip() if cursor else None
            if cursor and cursor in seen:
                return {
                    **result_data(items, next_cursor=cursor, truncated=True),
                    "pagination_loop": True,
                }
            if data.get("has_more") and not cursor:
                return result_data(items, truncated=True)
            if not cursor or (max_items is not None and len(items) >= max_items):
                break
        return result_data(items, next_cursor=cursor, truncated=bool(cursor))

    async def op_list_channels(self, p: ListChannels, call: Call) -> dict[str, Any]:
        """List channels with cursor pagination (read)."""
        return await self._paged(
            call, "/conversations.list", {"cursor": p.cursor}, "channels", p.limit, p.max_items
        )

    async def op_read_history(self, p: History, call: Call) -> dict[str, Any]:
        """Read recent messages of a channel (read)."""
        return await self._paged(
            call,
            "/conversations.history",
            {"channel": p.channel, "cursor": p.cursor},
            "messages",
            p.limit,
            p.max_items,
        )
