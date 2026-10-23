"""Additional C7 review contracts. All HTTP uses local MockTransport only."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from connector_helpers import Recorder, Sleeps, invoke, json_response
from nexura_connectors import (
    GitHubConnector,
    GoogleDriveConnector,
    GoogleSheetsConnector,
    SlackConnector,
)
from nexura_connectors._core import Call, HttpResult
from nexura_connectors.gdrive import _BOUNDARY
from test_c7_review_fixes_repros import NOW, REPO, ctx_for, record, run


@pytest.mark.parametrize("kind", ["issues", "pulls"])
@pytest.mark.parametrize("form", ["absolute", "path", "relative", "query"])
async def test_github_repository_id_links_and_enterprise_prefix(kind: str, form: str) -> None:
    base = "https://git.example/api/v3"
    repo_path = f"/api/v3/repos/acme/widgets/{kind}"
    id_path = f"/api/v3/repositories/1300192/{kind}"

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.params.get("page") == "2":
            assert r.url.path == (repo_path if form == "query" else id_path)
            return json_response([{"id": 2}])
        link = {
            "absolute": f"https://git.example{id_path}?page=2",
            "path": f"{id_path}?page=2",
            "relative": f"../../../repositories/1300192/{kind}?page=2",
            "query": "?page=2",
        }[form]
        return json_response([{"id": 1}], Link=f'<{link}>; rel="next"')

    rec = Recorder(handler)
    result = await invoke(
        GitHubConnector(allowed_hosts=["git.example"], transport=rec.transport),
        f"list_{kind}",
        REPO,
        cfg={"base_url": base},
    )
    assert result.status == "success", result
    assert result.outputs["data"] == [{"id": 1}, {"id": 2}]
    assert not result.outputs["truncated"] and len(rec.requests) == 2


async def test_repository_id_native_and_partial_cursors_resume_without_loss() -> None:
    native = "https://api.github.com/repositories/1300192/issues?page=2"
    rec = Recorder(lambda r: json_response([{"id": 2}, {"id": 3}]))
    module = GitHubConnector(transport=rec.transport)
    first = await invoke(module, "list_issues", {**REPO, "cursor": native, "max_items": 1})
    assert first.status == "success" and first.outputs["data"] == [{"id": 2}]
    cursor = first.outputs["next_cursor"]
    final = await invoke(module, "list_issues", {**REPO, "cursor": cursor})
    assert final.outputs["data"] == [{"id": 3}] and not final.outputs["truncated"]
    assert str(rec.last.url) == native
    bad = await invoke(module, "list_issues", {**REPO, "cursor": cursor, "state": "closed"})
    assert bad.error_code == "INVALID_PARAMS" and len(rec.requests) == 2


@pytest.mark.parametrize(
    "path",
    [
        "/users/acme",
        "/repos/other/widgets/issues",
        "/repositories/not-an-id/issues",
        "/repositories/1300192/pulls",
        "/repositories/1300192/issues/comments",
        "/api/v3/repositories/1300192/issues",
        "/repositories/1300192/issues#fragment",
    ],
)
async def test_github_unrelated_next_paths_refused_without_losing_items(path: str) -> None:
    rec = Recorder(
        lambda r: json_response([{"id": 1}], Link=f'<https://api.github.com{path}>; rel="next"')
    )
    result = await invoke(GitHubConnector(transport=rec.transport), "list_issues", REPO)
    assert result.error_code == "SSRF_BLOCKED" and len(rec.requests) == 1
    assert result.outputs["data"] == [{"id": 1}] and result.outputs["truncated"]
    rec.requests.clear()
    result = await invoke(
        GitHubConnector(transport=rec.transport),
        "list_issues",
        {
            **REPO,
            "cursor": "https://api.github.com" + path,
        },
    )
    assert result.error_code == "INVALID_PARAMS" and not rec.requests


async def test_github_later_page_failure_keeps_previously_fetched_items() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.params.get("page"):
            return json_response({}, 503)
        return json_response([{"id": 1}], Link='</repositories/123/issues?page=2>; rel="next"')

    rec = Recorder(handler)
    result = await invoke(
        GitHubConnector(transport=rec.transport), "list_issues", REPO, cfg={"max_retries": 0}
    )
    assert result.error_code == "UPSTREAM_ERROR" and len(rec.requests) == 2
    assert result.outputs["data"] == [{"id": 1}] and result.outputs["truncated"]


async def test_secret_fields_only_and_api_data_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW)
    secret_values = {
        "access_token": "access:secret-value-a",
        "refresh_token": "refresh:secret-value-b",
        "client_secret": "client:secret-value-c",
        "app_jwt": "jwt:secret-value-d",
        "id_token": "idtoken:secret-value-e",
        "private_key": "private:secret-value-f",
    }
    ctx, _ = ctx_for(GitHubConnector, record(expires_at=NOW + 3600, **secret_values))
    real = {
        "auth_type": "oauth2",
        "client_id": "test-client",
        "token_type": "Bearer",
        "scope": "chat:write,channels:history,channels:read",
        "text": "access refresh client jwt private chat read secret-value-a brass instruments",
        "data": "order 4111; release notes; room 123-45",
    }
    rec = Recorder(lambda r: json_response({**real, "echoes": list(secret_values.values())}))
    result = await run(GitHubConnector(transport=rec.transport), ctx, "get_repo", REPO)
    assert result.status == "success"
    assert {k: result.outputs["data"][k] for k in real} == real
    assert result.outputs["data"]["echoes"] == ["[REDACTED]"] * len(secret_values)


@pytest.mark.parametrize("timeout", [0.5, 10, 120])
async def test_retry_budget_total_and_manifest_bound(timeout: float) -> None:
    sleeps = Sleeps()
    rec = Recorder(lambda r: json_response({}, 429, **{"Retry-After": "11"}))
    result = await invoke(
        GitHubConnector(transport=rec.transport, sleep=sleeps),
        "get_repo",
        REPO,
        cfg={"max_retries": 3, "timeout_seconds": timeout},
    )
    assert result.error_code == "RATE_LIMITED" and result.metadata["retryable"]
    assert 11 <= result.metadata["retry_after_seconds"] <= 12.1
    assert sum(sleeps.delays) < min(timeout, 30)
    assert len(rec.requests) == (3 if timeout == 120 else 1)


async def test_retry_budget_shared_across_pages() -> None:
    sleeps = Sleeps()
    seen: dict[str, int] = {}

    def handler(r: httpx.Request) -> httpx.Response:
        page = r.url.params.get("page", "1")
        seen[page] = seen.get(page, 0) + 1
        if seen[page] == 1:
            return json_response({}, 429, **{"Retry-After": "18"})
        return json_response([{"id": 1}], Link='</repositories/123/issues?page=2>; rel="next"')

    rec = Recorder(handler)
    result = await invoke(
        GitHubConnector(transport=rec.transport, sleep=sleeps), "list_issues", REPO
    )
    assert result.error_code == "RATE_LIMITED" and len(rec.requests) == 3
    assert 18 <= sum(sleeps.delays) <= 19.8
    assert result.outputs["data"] == [{"id": 1}]
    assert result.metadata["retry_after_seconds"] >= 18


async def test_github_self_loop_different_query_order_and_implicit_page() -> None:
    rec = Recorder(
        lambda r: json_response(
            [{"id": 1}],
            Link=(
                "<https://api.github.com/repos/acme/widgets/issues?per_page=30&page=1&state=open>; "
                'rel="next"'
            ),
        )
    )
    result = await invoke(GitHubConnector(transport=rec.transport), "list_issues", REPO)
    assert len(rec.requests) == 1 and result.outputs["data"] == [{"id": 1}]
    assert result.outputs["pagination_loop"] and result.outputs["truncated"]
    assert "next_cursor" not in result.outputs


async def test_drive_cursor_loop_stops_before_fetching_page_twice() -> None:
    rec = Recorder(lambda r: json_response({"files": [], "nextPageToken": "same"}))
    result = await invoke(GoogleDriveConnector(transport=rec.transport), "list_files", {})
    assert len(rec.requests) == 2 and result.outputs["data"] == []
    assert result.outputs["pagination_loop"] and result.outputs["truncated"]
    assert "next_cursor" not in result.outputs


@pytest.mark.parametrize("failure", ["timeout", "connect", 500, 502, 503, 504])
async def test_transient_token_failures_retryable_without_replaying_exchange(
    failure: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW)
    ctx, _ = ctx_for(GoogleDriveConnector, record())

    def handler(r: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("mock timeout", request=r)
        if failure == "connect":
            raise httpx.ConnectError("mock transport error", request=r)
        return json_response({"echo": "refresh-secret-value"}, failure)

    rec = Recorder(handler)
    result = await run(GoogleDriveConnector(transport=rec.transport), ctx, "list_files", {})
    assert len(rec.requests) == 1 and str(rec.last.url) == GoogleDriveConnector.TOKEN_URL
    assert result.error_code == "UPSTREAM_UNAVAILABLE" and result.metadata["retryable"]
    assert "rotated" in result.error_message and "re-authorize" in result.error_message
    assert "refresh-secret-value" not in str(result)


@pytest.mark.parametrize("status", [408, 500, 502, 503, 504])
async def test_write_error_effect_unknown_and_never_retried(status: int) -> None:
    rec, sleeps = Recorder(lambda r: json_response({}, status)), Sleeps()
    result = await invoke(
        GitHubConnector(transport=rec.transport, sleep=sleeps),
        "create_issue",
        {**REPO, "title": "mock"},
        approved=True,
    )
    assert result.status == "indeterminate" and result.error_code == "INDETERMINATE_EFFECT"
    assert len(rec.requests) == 1 and not sleeps.delays


def test_helpers_and_call_repr_hide_secret_and_refresh_callback() -> None:
    ctx, _ = ctx_for(GitHubConnector, record())
    secret = json.dumps(record())
    http = GitHubConnector().build_http({}, secret)
    http.refresh_auth = lambda: None  # type: ignore[assignment,return-value]
    call = Call(secret, http, {"debug_secret": secret}, ctx)
    for value in record().values():
        if isinstance(value, str) and "secret" in value:
            assert value not in repr(call) + repr(http)
    assert "refresh_auth=" not in repr(http)
    response = HttpResult(200, httpx.Headers({"echo": secret}), secret.encode())
    assert "refresh-secret-value" not in repr(response)


@pytest.mark.parametrize("zone", ["UTC", "EST5EDT"])
async def test_app_naive_expiry_always_utc(
    zone: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW)
    ctx, store = ctx_for(
        GitHubConnector,
        {"auth_type": "github_app", "installation_id": 7, "app_jwt": "expired-mock-jwt-secret"},
    )
    old_zone = __import__("os").environ.get("TZ")
    monkeypatch.setenv("TZ", zone)
    time.tzset()
    try:
        rec = Recorder(
            lambda r: (
                json_response(
                    {"token": "mock-installation-token", "expires_at": "2027-01-15T09:00:00"}
                )
                if r.method == "POST"
                else json_response({})
            )
        )
        result = await run(GitHubConnector(transport=rec.transport), ctx, "get_repo", REPO)
        assert result.status == "success"
        saved = json.loads(store.resolve("ws", "cred").reveal())
        assert saved["expires_at"] == datetime(2027, 1, 15, 9, tzinfo=UTC).timestamp()
        assert "app_jwt" not in saved
    finally:
        if old_zone is None:
            monkeypatch.delenv("TZ")
        else:
            monkeypatch.setenv("TZ", old_zone)
        time.tzset()


async def test_app_signer_runs_off_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW)
    ctx, _ = ctx_for(GitHubConnector, {"auth_type": "github_app", "installation_id": 7})
    loop_thread = threading.get_ident()

    def signer() -> str:
        assert threading.get_ident() != loop_thread
        return "mock-provider-jwt-secret"

    rec = Recorder(
        lambda r: (
            json_response(
                {"token": "mock-installation-token", "expires_at": "2027-01-15T09:00:00Z"}
            )
            if r.method == "POST"
            else json_response({})
        )
    )
    result = await run(
        GitHubConnector(transport=rec.transport, app_jwt_provider=signer), ctx, "get_repo", REPO
    )
    assert result.status == "success" and len(rec.requests) == 2


@pytest.mark.parametrize(
    "cls", [GitHubConnector, SlackConnector, GoogleDriveConnector, GoogleSheetsConnector]
)
def test_all_selected_operations_have_explicit_effects(cls: Any) -> None:
    examples = {e["operation"]: e for e in cls().manifest.display.examples}
    for op in cls.OPS.values():
        assert op.effect_class == ("non_idempotent" if op.write else "pure")
        assert examples[op.name]["effect_class"] == op.effect_class


@pytest.mark.parametrize("write", [False, True])
@pytest.mark.parametrize("always_401", [False, True])
async def test_unexpired_401_refresh_and_replay_once_for_reads_only(
    write: bool,
    always_401: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW)
    ctx, _ = ctx_for(GitHubConnector, record(expires_at=NOW + 3600), approval_granted=write)

    def handler(r: httpx.Request) -> httpx.Response:
        if str(r.url) == GitHubConnector.TOKEN_URL:
            return json_response({"access_token": "new-mock-access-token", "expires_in": 3600})
        if always_401 or r.headers["Authorization"] == "Bearer old-access-secret-value":
            return json_response({}, 401)
        return json_response({"name": "mock"})

    rec = Recorder(handler)
    result = await run(
        GitHubConnector(transport=rec.transport),
        ctx,
        "create_issue" if write else "get_repo",
        {**REPO, **({"title": "mock"} if write else {})},
    )
    assert len(rec.requests) == (1 if write else 3)
    assert sum(str(r.url) == GitHubConnector.TOKEN_URL for r in rec.requests) == (0 if write else 1)
    assert result.error_code == ("AUTH_FAILED" if write or always_401 else None)


async def test_concurrent_revoked_reads_share_one_forced_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW)
    ctx, _ = ctx_for(GitHubConnector, record(expires_at=NOW + 3600))
    requests: list[httpx.Request] = []

    async def handler(r: httpx.Request) -> httpx.Response:
        requests.append(r)
        await asyncio.sleep(0)
        if str(r.url) == GitHubConnector.TOKEN_URL:
            return json_response({"access_token": "new-mock-access-token", "expires_in": 3600})
        return json_response(
            {}, 401 if r.headers["Authorization"] == "Bearer old-access-secret-value" else 200
        )

    module = GitHubConnector(transport=httpx.MockTransport(handler))
    results = await asyncio.gather(*(run(module, ctx, "get_repo", REPO) for _ in range(5)))
    assert all(r.status == "success" for r in results)
    assert sum(str(r.url) == GitHubConnector.TOKEN_URL for r in requests) == 1


async def test_github_past_reset_uses_small_bounded_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW)
    rec = Recorder(
        lambda r: json_response(
            {}, 403, **{"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(NOW - 1)}
        )
    )
    sleeps = Sleeps()
    result = await invoke(
        GitHubConnector(transport=rec.transport, sleep=sleeps),
        "get_repo",
        REPO,
        cfg={"max_retries": 1},
    )
    assert result.error_code == "RATE_LIMITED" and len(rec.requests) == 2
    assert 0.1 <= sum(sleeps.delays) <= 0.55


async def test_large_binary_download_not_json_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_json(_: Any) -> Any:
        pytest.fail("binary download was JSON parsed")

    monkeypatch.setattr(HttpResult, "json", forbidden_json)
    content = b"\xff\x00mock-binary" * 200_000
    rec = Recorder(
        lambda r: httpx.Response(
            200, content=content, headers={"Content-Type": "application/octet-stream"}
        )
    )
    result = await invoke(
        GoogleDriveConnector(transport=rec.transport),
        "download_file",
        {"file_id": "f1"},
        cfg={"max_response_bytes": 10_000_000},
    )
    assert result.status == "success" and len(rec.requests) == 1
    assert base64.b64decode(result.outputs["data"]["content_base64"]) == content


@pytest.mark.parametrize("field", ["content", "name"])
async def test_drive_multipart_boundary_collision_refused_before_http(field: str) -> None:
    rec = Recorder(lambda r: json_response({"id": "should-not-send"}))
    result = await invoke(
        GoogleDriveConnector(transport=rec.transport),
        "upload_text",
        {"name": "file.txt", "content": "text", field: f"before{_BOUNDARY}after"},
        approved=True,
    )
    assert result.error_code == "INVALID_PARAMS" and not rec.requests
