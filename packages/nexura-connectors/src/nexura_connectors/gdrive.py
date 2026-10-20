# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Google Drive v3 connector (OAuth access token)."""

from __future__ import annotations

import base64
from typing import Any, ClassVar

from pydantic import Field

from nexura_connectors._core import (
    Call,
    Op,
    StrictParams,
    pick_cursor,
    result_data,
    safe_id,
)
from nexura_connectors._rest import RestConnector

_BOUNDARY = "nexura-connector-boundary-7c1e"


class ListFiles(StrictParams):
    """Parameters for listing files."""

    query: str | None = Field(default=None, max_length=1000)
    page_size: int = Field(default=50, ge=1, le=100)
    page_token: str | None = None  # alias of ``cursor`` (kept for compatibility)
    cursor: str | None = None


class FileId(StrictParams):
    """Parameters addressing one file."""

    file_id: str = Field(min_length=1, max_length=128)


class UploadText(StrictParams):
    """Parameters for uploading a small text file."""

    name: str = Field(min_length=1, max_length=255)
    content: str = Field(max_length=1_000_000)
    mime_type: str = Field(default="text/plain", pattern=r"^[a-z]+/[a-z0-9.+-]+$")
    parent_id: str | None = Field(default=None, max_length=128)


class GoogleDriveConnector(RestConnector):
    """List, inspect and download files; upload small text files."""

    MODULE_ID: ClassVar[str] = "nexura.connectors.gdrive"
    TITLE: ClassVar[str] = "Google Drive"
    DESCRIPTION: ClassVar[str] = "Google Drive v3 with an OAuth access token."
    CREDENTIAL_TYPE: ClassVar[str] = "google_oauth_token"
    DEFAULT_BASE_URL: ClassVar[str | None] = "https://www.googleapis.com"
    OPS: ClassVar[dict[str, Op]] = {
        "list_files": Op("list_files", False, ListFiles),
        "get_file": Op("get_file", False, FileId),
        "download_file": Op("download_file", False, FileId),
        "upload_text": Op("upload_text", True, UploadText),
    }

    async def op_list_files(self, p: ListFiles, call: Call) -> dict[str, Any]:
        """List files with page-token pagination (read)."""
        files: list[Any] = []
        token = pick_cursor(p.cursor, p.page_token, "page_token")
        for _ in range(call.max_pages):
            query: dict[str, Any] = {
                "pageSize": p.page_size,
                "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime)",
            }
            if p.query:
                query["q"] = p.query
            if token:
                query["pageToken"] = token
            data = (await self.api(call, "GET", "/drive/v3/files", params=query)).json()
            files.extend(data.get("files", []))
            token = data.get("nextPageToken")
            if not token:
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
        if p.parent_id:
            meta["parents"] = [p.parent_id]
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
