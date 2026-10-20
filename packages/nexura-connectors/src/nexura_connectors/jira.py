# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Jira Cloud REST connector (email:api_token basic auth; base_url is required)."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from nexura_connectors._core import (
    Call,
    Op,
    StrictParams,
    basic,
    result_data,
)
from nexura_connectors._rest import RestConnector
from nexura_connectors.errors import InvalidParamsError


class Search(StrictParams):
    """Parameters for a JQL search."""

    jql: str = Field(min_length=1, max_length=2000)
    max_results: int = Field(default=50, ge=1, le=100)
    start_at: int = Field(default=0, ge=0)  # alias of ``cursor`` (kept for compatibility)
    cursor: str | None = Field(default=None, pattern=r"^[0-9]{1,9}$")


class IssueKey(StrictParams):
    """Parameters addressing one issue."""

    key: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,31}-[0-9]{1,9}$")


class CreateIssue(StrictParams):
    """Parameters for creating an issue."""

    project_key: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,31}$")
    summary: str = Field(min_length=1, max_length=255)
    issue_type: str = Field(default="Task", min_length=1, max_length=60)
    description: str = Field(default="", max_length=20000)


class AddComment(IssueKey):
    """Parameters for commenting on an issue."""

    body: str = Field(min_length=1, max_length=20000)


def _adf(text: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


class JiraConnector(RestConnector):
    """Search and read issues; create issues and comments."""

    MODULE_ID: ClassVar[str] = "nexura.connectors.jira"
    TITLE: ClassVar[str] = "Jira"
    DESCRIPTION: ClassVar[str] = (
        "Jira Cloud REST v3. Credential is 'email:api_token'; config base_url "
        "(https://<site>.atlassian.net) is required and passes the SSRF policy."
    )
    CREDENTIAL_TYPE: ClassVar[str] = "jira_basic"
    OPS: ClassVar[dict[str, Op]] = {
        "search": Op("search", False, Search),
        "get_issue": Op("get_issue", False, IssueKey),
        "create_issue": Op("create_issue", True, CreateIssue),
        "add_comment": Op("add_comment", True, AddComment),
    }

    def auth_headers(self, secret: str) -> dict[str, str]:
        """Basic auth from the stored ``email:api_token``."""
        return basic(secret)

    async def op_search(self, p: Search, call: Call) -> dict[str, Any]:
        """Run a JQL search (read)."""
        start_at = p.start_at
        if p.cursor is not None:
            if p.start_at and p.start_at != int(p.cursor):
                raise InvalidParamsError("cursor and start_at disagree; pass only cursor")
            start_at = int(p.cursor)
        resp = await self.api(
            call,
            "GET",
            "/rest/api/3/search",
            params={"jql": p.jql, "maxResults": p.max_results, "startAt": start_at},
        )
        data = resp.json()
        issues = data.get("issues", [])
        end = start_at + len(issues)
        more = end < int(data.get("total", end))
        return result_data(issues, next_cursor=str(end) if more else None, truncated=more)

    async def op_get_issue(self, p: IssueKey, call: Call) -> dict[str, Any]:
        """Fetch one issue (read)."""
        return result_data((await self.api(call, "GET", f"/rest/api/3/issue/{p.key}")).json())

    async def op_create_issue(self, p: CreateIssue, call: Call) -> dict[str, Any]:
        """Create an issue (write)."""
        fields: dict[str, Any] = {
            "project": {"key": p.project_key},
            "summary": p.summary,
            "issuetype": {"name": p.issue_type},
        }
        if p.description:
            fields["description"] = _adf(p.description)
        resp = await self.api(
            call, "POST", "/rest/api/3/issue", json={"fields": fields}, write=True
        )
        data = resp.json()
        return result_data({"id": data.get("id"), "key": data.get("key")})

    async def op_add_comment(self, p: AddComment, call: Call) -> dict[str, Any]:
        """Comment on an issue (write)."""
        resp = await self.api(
            call,
            "POST",
            f"/rest/api/3/issue/{p.key}/comment",
            json={"body": _adf(p.body)},
            write=True,
        )
        return result_data({"id": resp.json().get("id")})
