# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Google Drive v3 connector (OAuth access token)."""

from __future__ import annotations

import base64
from typing import Any, ClassVar

from pydantic import Field

from nexura_connectors._auth import RefreshingRestConnector
from nexura_connectors._core import (
    Call,
    Op,
    StrictParams,
    pick_cursor,
    result_data,
    safe_id,
)
from nexura_connectors._pagination import page_items, partial, resume
from nexura_connectors.errors import InvalidParamsError, UpstreamError

_BOUNDARY = "nexura-connector-boundary-7c1e"


class ListFiles(StrictParams):
    """Parameters for listing files."""

    query: str | None = Field(default=None, max_length=1000)
    page_size: int = Field(default=50, ge=1, le=100)
    page_token: str | None = None  # alias of ``cursor`` (kept for compatibility)
    cursor: str | None = None
    max_items: int | None = Field(default=None, ge=1, le=10000)


class FileId(StrictParams):
    """Parameters addressing one file."""

    file_id: str = Field(min_length=1, max_length=128)


class UploadText(StrictParams):
    """Parameters for uploading a small text file."""

    name: str = Field(min_length=1, max_length=255)
    content: str = Field(max_length=1_000_000)
    mime_type: str = Field(default="text/plain", pattern=r"^[a-z]+/[a-z0-9.+-]+$")
    parent_id: str | None = Field(default=None, max_length=128)
    file_id: str | None = Field(default=None, min_length=1, max_length=128)


class GoogleDriveConnector(RefreshingRestConnector):
    """List, inspect and download files; upload small text files."""

    MODULE_ID: ClassVar[str] = "nexura.connectors.gdrive"
    TITLE: ClassVar[str] = "Google Drive"
    DESCRIPTION: ClassVar[str] = "Google Drive v3 with an OAuth access token."
    CREDENTIAL_TYPE: ClassVar[str] = "google_oauth_token"
    DEFAULT_BASE_URL: ClassVar[str | None] = "https://www.googleapis.com"
    AUTH_FAMILY: ClassVar[str] = "google"
    TOKEN_URL: ClassVar[str] = "https://oauth2.googleapis.com/token"
    OPS: ClassVar[dict[str, Op]] = {
        "list_files": Op("list_files", False, ListFiles, effect_class="pure"),
        "get_file": Op("get_file", False, FileId, effect_class="pure"),
        "download_file": Op("download_file", False, FileId, effect_class="pure"),
        "upload_text": Op("upload_text", True, UploadText, effect_class="non_idempotent"),
    }

    async def op_list_files(self, p: ListFiles, call: Call) -> dict[str, Any]:
        """List files with page-token pagination (read)."""
        files: list[Any] = []
        token, offset, size = resume(
            pick_cursor(p.cursor, p.page_token, "page_token"), p.page_size, 100
        )
        seen: set[str | None] = set()
        for _ in range(call.max_pages):
            current = token
            seen.add(current)
            query: dict[str, Any] = {
                "pageSize": size,
                "fields": (
                    "nextPageToken,incompleteSearch,files(id,name,mimeType,size,modifiedTime)"
                ),
            }
            if p.query:
                query["q"] = p.query
            if token:
                query["pageToken"] = token
            data = (await self.api(call, "GET", "/drive/v3/files", params=query)).json()
            page = page_items(data, "files", offset)
            remaining = len(page) if p.max_items is None else p.max_items - len(files)
            files.extend(page[offset : offset + remaining])
            if offset + remaining < len(page):
                return result_data(
                    files, next_cursor=partial(current, offset + remaining, size), truncated=True
                )
            offset = 0
            token = data.get("nextPageToken")
            if token is not None and not isinstance(token, str):
                raise UpstreamError("Drive nextPageToken must be a string")
            if token and token in seen:
                return {**result_data(files, truncated=True), "pagination_loop": True}
            if data.get("incompleteSearch"):
                out = result_data(files, next_cursor=token, truncated=True)
                out["incomplete_search"] = True
                return out
            if not token or (p.max_items is not None and len(files) >= p.max_items):
                break
        return result_data(files, next_cursor=token, truncated=bool(token))

    async def op_get_file(self, p: FileId, call: Call) -> dict[str, Any]:
        """Fetch file metadata (read)."""
        resp = await self.api(
            call,
            "GET",
            f"/drive/v3/files/{safe_id(p.file_id)}",
            params={"fields": "id,name,mimeType,size,modifiedTime"},
        )
        return result_data(resp.json())

    async def op_download_file(self, p: FileId, call: Call) -> dict[str, Any]:
        """Download file bytes, capped by max_response_bytes; returned base64 (read)."""
        resp = await self.api(
            call, "GET", f"/drive/v3/files/{safe_id(p.file_id)}", params={"alt": "media"}
        )
        return result_data(
            {"size_bytes": len(resp.body), "content_base64": base64.b64encode(resp.body).decode()}
        )

    async def op_upload_text(self, p: UploadText, call: Call) -> dict[str, Any]:
        """Upload a small text file via multipart upload (write)."""
        import json

        meta: dict[str, Any] = {"name": p.name, "mimeType": p.mime_type}
        if p.file_id:
            # Drive accepts IDs reserved by files.generateIds, not arbitrary UUIDs.
            meta["id"] = safe_id(p.file_id, "pre-generated Drive file id")
        if p.parent_id:
            meta["parents"] = [safe_id(p.parent_id, "parent id")]
        if _BOUNDARY in p.content or _BOUNDARY in json.dumps(meta):
            raise InvalidParamsError("multipart boundary occurs in file content or metadata")
        body = (
            f"--{_BOUNDARY}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(meta)}\r\n--{_BOUNDARY}\r\nContent-Type: {p.mime_type}\r\n\r\n"
            f"{p.content}\r\n--{_BOUNDARY}--"
        ).encode()
        resp = await self.api(
            call,
            "POST",
            "/upload/drive/v3/files",
            params={"uploadType": "multipart", "fields": "id,name"},
            content=body,
            headers={"Content-Type": f"multipart/related; boundary={_BOUNDARY}"},
            write=True,
        )
        data = resp.json()
        return result_data({"id": data.get("id"), "name": data.get("name")})
