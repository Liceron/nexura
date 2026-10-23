# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Lossless resume cursors when a caller's item cap stops inside a provider page."""

from __future__ import annotations

import base64
import json
from typing import Any

from nexura_connectors.errors import InvalidParamsError, UpstreamError

_PREFIX = "nexura-page-v1:"


def resume(
    cursor: str | None, page_size: int, maximum: int, *, binding: dict[str, str] | None = None
) -> tuple[str | None, int, int]:
    """Decode a partial-page cursor, or pass through a native provider cursor."""
    if not cursor or not cursor.startswith(_PREFIX):
        return cursor, 0, page_size
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor[len(_PREFIX) :]))
        if binding is not None and data.get("binding") != binding:
            raise InvalidParamsError(
                "pagination cursor resource or filters changed; restart the listing"
            )
        native, offset, size = data["cursor"], data["offset"], data["size"]
        if (
            (native is None or isinstance(native, str))
            and type(offset) is int
            and 0 < offset <= 1000
            and type(size) is int
            and 1 <= size <= maximum
        ):
            return native, offset, size
    except (ValueError, KeyError, TypeError):
        pass
    raise InvalidParamsError("invalid pagination cursor; use the returned next_cursor unchanged")


def partial(
    cursor: str | None, offset: int, size: int, *, binding: dict[str, str] | None = None
) -> str:
    """Keep the provider's current page and offset rather than skipping unreturned items."""
    raw = json.dumps(
        {"cursor": cursor, "offset": offset, "size": size, "binding": binding}
    ).encode()
    return _PREFIX + base64.urlsafe_b64encode(raw).decode()


def page_items(data: Any, key: str | None = None, offset: int = 0) -> list[Any]:
    """Reject malformed list payloads rather than returning incomplete success."""
    if key is not None:
        if not isinstance(data, dict):
            raise UpstreamError("upstream pagination response must be an object")
        data = data.get(key, [])
    if not isinstance(data, list):
        raise UpstreamError("upstream pagination items must be an array")
    if offset > len(data):
        raise UpstreamError(
            "upstream page changed since the cursor was issued; restart the listing"
        )
    return data
