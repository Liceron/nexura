# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Notion API connector (integration token)."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from nexura_connectors._core import (
    Call,
    Op,
    StrictParams,
    clamp,
    pick_cursor,
    result_data,
    safe_id,
)
from nexura_connectors._rest import RestConnector


class QueryDatabase(StrictParams):
    """Parameters for querying a database."""

    database_id: str = Field(min_length=1, max_length=64)
    filter: dict[str, Any] | None = None
    page_size: int = Field(default=50, ge=1, le=100)
    start_cursor: str | None = None  # alias of ``cursor`` (kept for compatibility)
    cursor: str | None = None


class GetPage(StrictParams):
    """Parameters for retrieving a page."""

    page_id: str = Field(min_length=1, max_length=64)


class CreatePage(StrictParams):
    """Parameters for creating a page under a database or page."""

    parent_database_id: str = Field(min_length=1, max_length=64)
    properties: dict[str, Any]
    children: list[dict[str, Any]] = Field(default_factory=list, max_length=100)


class AppendBlocks(StrictParams):
    """Parameters for appending blocks to a page."""

    block_id: str = Field(min_length=1, max_length=64)
    children: list[dict[str, Any]] = Field(min_length=1, max_length=100)


class NotionConnector(RestConnector):
    """Query databases, read pages, create pages and append blocks."""

    MODULE_ID: ClassVar[str] = "nexura.connectors.notion"
    TITLE: ClassVar[str] = "Notion"
    DESCRIPTION: ClassVar[str] = "Notion API with an internal integration token."
    CREDENTIAL_TYPE: ClassVar[str] = "notion_token"
    DEFAULT_BASE_URL: ClassVar[str | None] = "https://api.notion.com/v1"
    EXTRA_HEADERS: ClassVar[dict[str, str]] = {"Notion-Version": "2022-06-28"}
    OPS: ClassVar[dict[str, Op]] = {
        "query_database": Op("query_database", False, QueryDatabase),
        "get_page": Op("get_page", False, GetPage),
        "create_page": Op("create_page", True, CreatePage),
        "append_blocks": Op("append_blocks", True, AppendBlocks),
    }

    async def op_query_database(self, p: QueryDatabase, call: Call) -> dict[str, Any]:
        """Query a database with cursor pagination; a POST that is a read (read)."""
        results: list[Any] = []
        cursor = pick_cursor(p.cursor, p.start_cursor, "start_cursor")
        for _ in range(call.max_pages):
            body: dict[str, Any] = {"page_size": clamp(p.page_size, 1, 100)}
            if p.filter:
                body["filter"] = p.filter
            if cursor:
                body["start_cursor"] = cursor
            resp = await self.api(
                call, "POST", f"/databases/{safe_id(p.database_id)}/query", json=body
            )
            data = resp.json()
            results.extend(data.get("results", []))
            cursor = data.get("next_cursor") if data.get("has_more") else None
            if not cursor:
                break
        return result_data(results, next_cursor=cursor, truncated=bool(cursor))

    async def op_get_page(self, p: GetPage, call: Call) -> dict[str, Any]:
        """Retrieve a page (read)."""
        return result_data((await self.api(call, "GET", f"/pages/{safe_id(p.page_id)}")).json())

    async def op_create_page(self, p: CreatePage, call: Call) -> dict[str, Any]:
        """Create a page in a database (write)."""
        body = {
            "parent": {"database_id": p.parent_database_id},
            "properties": p.properties,
            "children": p.children,
        }
        data = (await self.api(call, "POST", "/pages", json=body, write=True)).json()
        return result_data({"id": data.get("id"), "url": data.get("url")})

    async def op_append_blocks(self, p: AppendBlocks, call: Call) -> dict[str, Any]:
        """Append child blocks to a page or block (write)."""
        resp = await self.api(
            call,
            "PATCH",
            f"/blocks/{safe_id(p.block_id)}/children",
            json={"children": p.children},
            write=True,
        )
        return result_data({"count": len(resp.json().get("results", []))})
