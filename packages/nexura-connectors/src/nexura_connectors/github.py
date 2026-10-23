# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""GitHub REST connector (token auth; GitHub Enterprise via config base_url)."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, ClassVar
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import Field

from nexura_connectors._auth import RefreshingRestConnector
from nexura_connectors._core import (
    Call,
    Op,
    StrictParams,
    result_data,
    safe_id,
)
from nexura_connectors._pagination import page_items, partial, resume
from nexura_connectors.errors import ConnectorError, InvalidParamsError, SsrfBlockedError


class RepoRef(StrictParams):
    """Repository coordinates."""

    owner: str = Field(min_length=1, max_length=100)
    repo: str = Field(min_length=1, max_length=100)


class ListItems(RepoRef):
    """Parameters for listing issues or pull requests."""

    state: str = Field(default="open", pattern="^(open|closed|all)$")
    per_page: int = Field(default=30, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=2048)
    max_items: int | None = Field(default=None, ge=1, le=10000)


class CreateIssue(RepoRef):
    """Parameters for creating an issue."""

    title: str = Field(min_length=1, max_length=256)
    body: str = Field(default="", max_length=20000)
    labels: list[str] = Field(default_factory=list, max_length=20)


class CreateComment(RepoRef):
    """Parameters for commenting on an issue or pull request."""

    issue_number: int = Field(ge=1)
    body: str = Field(min_length=1, max_length=20000)


class GitHubConnector(RefreshingRestConnector):
    """Read repositories, issues and pulls; create issues and comments."""

    MODULE_ID: ClassVar[str] = "nexura.connectors.github"
    TITLE: ClassVar[str] = "GitHub"
    DESCRIPTION: ClassVar[str] = "GitHub REST API with a personal or app token."
    CREDENTIAL_TYPE: ClassVar[str] = "github_token"
    DEFAULT_BASE_URL: ClassVar[str | None] = "https://api.github.com"
    AUTH_FAMILY: ClassVar[str] = "github"
    TOKEN_URL: ClassVar[str] = "https://github.com/login/oauth/access_token"
    EXTRA_HEADERS: ClassVar[dict[str, str]] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    OPS: ClassVar[dict[str, Op]] = {
        "get_repo": Op("get_repo", False, RepoRef, effect_class="pure"),
        "list_issues": Op("list_issues", False, ListItems, effect_class="pure"),
        "list_pulls": Op("list_pulls", False, ListItems, effect_class="pure"),
        "create_issue": Op("create_issue", True, CreateIssue, effect_class="non_idempotent"),
        "create_comment": Op("create_comment", True, CreateComment, effect_class="non_idempotent"),
    }

    def __init__(
        self, *args: Any, app_jwt_provider: Callable[[], str] | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.app_jwt_provider = app_jwt_provider

    @staticmethod
    def _repo_path(p: RepoRef) -> str:
        return f"/repos/{safe_id(p.owner, 'owner')}/{safe_id(p.repo, 'repo')}"

    async def op_get_repo(self, p: RepoRef, call: Call) -> dict[str, Any]:
        """Fetch repository metadata (read)."""
        return result_data((await self.api(call, "GET", self._repo_path(p))).json())

    async def _list(self, p: ListItems, call: Call, kind: str) -> dict[str, Any]:
        items: list[Any] = []
        try:
            return await self._list_pages(p, call, kind, items)
        except ConnectorError as exc:
            if items:
                exc.partial_outputs = result_data(items, truncated=True)
            raise

    async def _list_pages(
        self, p: ListItems, call: Call, kind: str, items: list[Any]
    ) -> dict[str, Any]:
        path = f"{self._repo_path(p)}/{kind}"
        base = call.base_url(self.DEFAULT_BASE_URL)
        binding = {"path": path, "state": p.state}
        cursor, offset, size = resume(p.cursor, p.per_page, 100, binding=binding)
        prefix = urlsplit(base).path.rstrip("/")
        seen: set[str] = set()

        def page_key(url: str) -> str:
            parsed = httpx.URL(url)
            query = parsed.params
            for key, default in (("page", "1"), ("state", p.state), ("per_page", str(size))):
                if key not in query:
                    query = query.set(key, default)
            return str(
                parsed.copy_with(query=str(httpx.QueryParams(sorted(query.multi_items()))).encode())
            )

        def validate(url: str, *, upstream: bool = False) -> None:
            parts = urlsplit(url)
            if (
                not call.http.same_origin(url)
                or parts.username
                or parts.password
                or parts.fragment
                or not (
                    parts.path == prefix + path
                    or re.fullmatch(re.escape(prefix) + r"/repositories/[0-9]+/" + kind, parts.path)
                )
            ):
                error = SsrfBlockedError if upstream else InvalidParamsError
                raise error("cursor does not point at this repository's list endpoint")

        if cursor:
            validate(cursor)
        for _ in range(call.max_pages):
            current = cursor
            resp = await self.api(
                call,
                "GET",
                path,
                url=current,
                params=None if current else {"state": p.state, "per_page": size},
            )
            # Compare normalised request URLs, including the initial query parameters.
            seen.add(page_key(current or base + path))
            page = page_items(resp.json(), offset=offset)
            remaining = len(page) if p.max_items is None else p.max_items - len(items)
            items.extend(page[offset : offset + remaining])
            if offset + remaining < len(page):
                return result_data(
                    items,
                    next_cursor=partial(current, offset + remaining, size, binding=binding),
                    truncated=True,
                )
            offset = 0
            links = httpx.Response(200, headers=resp.headers).links
            link = links.get("next", {}).get("url")
            cursor = urljoin(current or base + path, link) if link else None
            if cursor:
                validate(cursor, upstream=True)
                if page_key(cursor) in seen:
                    return {**result_data(items, truncated=True), "pagination_loop": True}
            if not cursor or (p.max_items is not None and len(items) >= p.max_items):
                break
        return result_data(items, next_cursor=cursor, truncated=bool(cursor))

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
