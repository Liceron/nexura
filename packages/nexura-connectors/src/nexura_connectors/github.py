# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""GitHub REST connector (token auth; GitHub Enterprise via config base_url)."""

from __future__ import annotations

import re
from typing import Any, ClassVar
from urllib.parse import urlsplit

from pydantic import Field

from nexura_connectors._core import (
    Call,
    Op,
    StrictParams,
    result_data,
    safe_id,
)
from nexura_connectors._rest import RestConnector
from nexura_connectors.errors import InvalidParamsError

_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


class RepoRef(StrictParams):
    """Repository coordinates."""

    owner: str = Field(min_length=1, max_length=100)
    repo: str = Field(min_length=1, max_length=100)


class ListItems(RepoRef):
    """Parameters for listing issues or pull requests."""

    state: str = Field(default="open", pattern="^(open|closed|all)$")
    per_page: int = Field(default=30, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=2048)


class CreateIssue(RepoRef):
    """Parameters for creating an issue."""

    title: str = Field(min_length=1, max_length=256)
    body: str = Field(default="", max_length=20000)
    labels: list[str] = Field(default_factory=list, max_length=20)


class CreateComment(RepoRef):
    """Parameters for commenting on an issue or pull request."""

    issue_number: int = Field(ge=1)
    body: str = Field(min_length=1, max_length=20000)


class GitHubConnector(RestConnector):
    """Read repositories, issues and pulls; create issues and comments."""

    MODULE_ID: ClassVar[str] = "nexura.connectors.github"
    TITLE: ClassVar[str] = "GitHub"
    DESCRIPTION: ClassVar[str] = "GitHub REST API with a personal or app token."
    CREDENTIAL_TYPE: ClassVar[str] = "github_token"
    DEFAULT_BASE_URL: ClassVar[str | None] = "https://api.github.com"
    EXTRA_HEADERS: ClassVar[dict[str, str]] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    OPS: ClassVar[dict[str, Op]] = {
        "get_repo": Op("get_repo", False, RepoRef),
        "list_issues": Op("list_issues", False, ListItems),
        "list_pulls": Op("list_pulls", False, ListItems),
        "create_issue": Op("create_issue", True, CreateIssue),
        "create_comment": Op("create_comment", True, CreateComment),
    }

    @staticmethod
    def _repo_path(p: RepoRef) -> str:
        return f"/repos/{safe_id(p.owner, 'owner')}/{safe_id(p.repo, 'repo')}"

    async def op_get_repo(self, p: RepoRef, call: Call) -> dict[str, Any]:
        """Fetch repository metadata (read)."""
        return result_data((await self.api(call, "GET", self._repo_path(p))).json())

    async def _list(self, p: ListItems, call: Call, kind: str) -> dict[str, Any]:
        items: list[Any] = []
        path = f"{self._repo_path(p)}/{kind}"
        if p.cursor:
            # a resume cursor is an absolute URL from an earlier next_cursor: it must stay on the
            # API origin and on this repository's list endpoint before the token is sent to it
            base = call.base_url(self.DEFAULT_BASE_URL)
            parts = urlsplit(p.cursor)
            if not call.http.same_origin(p.cursor) or not (
                urlsplit(base).path.rstrip("/") + path == parts.path
            ):
                raise InvalidParamsError("cursor does not point at this repository's list endpoint")
            resp = await self.api(call, "GET", "", url=p.cursor)
        else:
            resp = await self.api(
                call, "GET", path, params={"state": p.state, "per_page": p.per_page}
            )
        pages = 1
        while True:
            items.extend(resp.json())
            match = _NEXT.search(resp.headers.get("link", ""))
            if not match:
                return result_data(items)
            if pages >= call.max_pages:
                return result_data(items, next_cursor=match.group(1), truncated=True)
            pages += 1
            # same-origin is enforced by ConnectorHttp before any credential is sent
            resp = await self.api(call, "GET", "", url=match.group(1))

    async def op_list_issues(self, p: ListItems, call: Call) -> dict[str, Any]:
        """List issues, following Link pagination up to max_pages (read)."""
        return await self._list(p, call, "issues")

    async def op_list_pulls(self, p: ListItems, call: Call) -> dict[str, Any]:
        """List pull requests, following Link pagination up to max_pages (read)."""
        return await self._list(p, call, "pulls")

    async def op_create_issue(self, p: CreateIssue, call: Call) -> dict[str, Any]:
        """Create an issue (write)."""
        body: dict[str, Any] = {"title": p.title, "body": p.body}
        if p.labels:
            body["labels"] = p.labels
        resp = await self.api(call, "POST", f"{self._repo_path(p)}/issues", json=body, write=True)
        data = resp.json()
        return result_data({"number": data.get("number"), "html_url": data.get("html_url")})

    async def op_create_comment(self, p: CreateComment, call: Call) -> dict[str, Any]:
        """Comment on an issue or pull request (write)."""
        resp = await self.api(
            call,
            "POST",
            f"{self._repo_path(p)}/issues/{p.issue_number}/comments",
            json={"body": p.body},
            write=True,
        )
        data = resp.json()
        return result_data({"id": data.get("id"), "html_url": data.get("html_url")})
