"""P38 C7 wire contracts. Every request uses MockTransport; no live credentials/calls."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any
from urllib.parse import parse_qs
from uuid import UUID

import httpx
import pytest
from connector_helpers import Recorder, Sleeps, invoke, json_response, make_context
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.module_tools import module_effect
from nexura_connectors import (
    GitHubConnector,
    GoogleDriveConnector,
    GoogleSheetsConnector,
    SlackConnector,
)

from nexura_runtime.credentials.store import ScopedCredentialStore, WorkspaceCredentialResolver

NOW = 1_800_000_000.0
OLD = "old-access-secret-value"
NEW = "new-access-secret-value"
REFRESH = "refresh-secret-value"
ROTATED = "rotated-refresh-secret-value"
CLIENT = "client-secret-value"
REPO = {"owner": "acme", "repo": "widgets"}
CASES = [
    (GitHubConnector, "get_repo", REPO, {"name": "widgets"}),
    (SlackConnector, "list_channels", {}, {"ok": True, "channels": []}),
    (GoogleDriveConnector, "list_files", {}, {"files": []}),
    (GoogleSheetsConnector, "read_range", {"spreadsheet_id": "s", "range": "A1"}, {"values": []}),
]


def auth_record(**updates: Any) -> dict[str, Any]:
    return {
        "auth_type": "oauth2",
        "access_token": OLD,
        "expires_at": NOW - 1,
        "refresh_token": REFRESH,
        "client_id": "test-client",
        "client_secret": CLIENT,
        **updates,
    }


def stored_context(
    cls: Any, record: dict[str, Any]
) -> tuple[ExecutionContext, ScopedCredentialStore]:
    store = ScopedCredentialStore()
    store.store(
        "workspace",
        "cred",
        cls.CREDENTIAL_TYPE,
        json.dumps(record),
        provider="test-provider",
        description="test description",
    )
    return ExecutionContext(
        run_id="r",
        node_id="n",
        workspace_id="workspace",
        credential_resolver=WorkspaceCredentialResolver(store, "workspace"),
    ), store


async def execute(module: Any, ctx: ExecutionContext, op: str, params: dict[str, Any]) -> Any:
    return await module.execute(
        ctx, {"operation": op, "params": params}, {"credential_ref": "cred"}
    )


@pytest.mark.parametrize("cls,op,params,payload", CASES)
async def test_expiry_refresh_rotation_and_persistence(
    cls: Any,
    op: str,
    params: dict[str, Any],
    payload: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW)
    ctx, store = stored_context(cls, auth_record())

    def handler(r: httpx.Request) -> httpx.Response:
        if str(r.url) == cls.TOKEN_URL:
            assert r.method == "POST"
            form = parse_qs(r.content.decode())
            assert form == {
                "grant_type": ["refresh_token"],
                "refresh_token": [REFRESH],
                "client_id": ["test-client"],
                "client_secret": [CLIENT],
            }
            assert "authorization" not in r.headers
            return json_response(
                {"access_token": NEW, "expires_in": 3600, "refresh_token": ROTATED}
            )
        assert r.headers["authorization"] == f"Bearer {NEW}"
        assert json.loads(store.resolve("workspace", "cred").reveal())["access_token"] == NEW
        return json_response(payload)

    rec = Recorder(handler)
    module = cls(transport=rec.transport)
    for _ in range(2):
        result = await execute(module, ctx, op, params)
        assert result.status == "success", result
    assert len(rec.requests) == 3
    saved = json.loads(store.resolve("workspace", "cred").reveal())
    assert saved["expires_at"] == NOW + 3600 and saved["refresh_token"] == ROTATED
    assert store.get_metadata("workspace", "cred").provider == "test-provider"
    assert store.get_metadata("workspace", "cred").description == "test description"


@pytest.mark.parametrize("expires,refreshes", [(NOW + 3600, 0), (NOW + 10, 1)])
async def test_refresh_skew_and_unexpired_token(
    expires: float,
    refreshes: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW)
    ctx, _ = stored_context(GitHubConnector, auth_record(expires_at=expires))
    rec = Recorder(
        lambda r: json_response(
            {"access_token": NEW, "expires_in": 3600}
            if str(r.url) == GitHubConnector.TOKEN_URL
            else {}
        )
    )
    result = await execute(GitHubConnector(transport=rec.transport), ctx, "get_repo", REPO)
    assert result.status == "success"
    assert len(rec.requests) == refreshes + 1
    assert rec.last.headers["authorization"] == f"Bearer {NEW if refreshes else OLD}"


@pytest.mark.parametrize("failure", [False, True])
async def test_concurrent_single_flight_across_instances_and_google_services(
    failure: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW)
    ctx, _ = stored_context(GoogleDriveConnector, auth_record())
    entered, release = asyncio.Event(), asyncio.Event()
    requests: list[httpx.Request] = []

    async def handler(r: httpx.Request) -> httpx.Response:
        requests.append(r)
        if str(r.url) == GoogleDriveConnector.TOKEN_URL:
            entered.set()
            await release.wait()
            return (
                json_response({"error": "invalid_grant"}, 400)
                if failure
                else json_response({"access_token": NEW, "expires_in": 3600})
            )
        assert r.headers["authorization"] == f"Bearer {NEW}"
        return json_response({"files": [], "values": []})

    transport = httpx.MockTransport(handler)
    tasks = [
        asyncio.create_task(
            execute(
                (GoogleDriveConnector if i % 2 else GoogleSheetsConnector)(transport=transport),
                ctx,
                "list_files" if i % 2 else "read_range",
                {} if i % 2 else {"spreadsheet_id": "s", "range": "A1"},
            )
        )
        for i in range(12)
    ]
    await entered.wait()
    # All tasks have joined the flight before its HTTP exchange is released.
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*tasks)
    assert len([r for r in requests if r.method == "POST"]) == 1
    assert all(r.error_code == "AUTH_FAILED" if failure else r.status == "success" for r in results)
    if failure:
        assert len(requests) == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"error": f"invalid_grant {REFRESH} {CLIENT}"}),
        httpx.Response(200, json={"access_token": NEW}),
        httpx.Response(200, json={"access_token": NEW, "expires_in": -10}),
        httpx.Response(200, json={"ok": False, "error": "invalid_refresh_token"}),
        httpx.Response(200, text="not json"),
        httpx.Response(302, headers={"location": "https://evil.example"}),
        httpx.Response(429, headers={"Retry-After": "60"}),
    ],
)
async def test_refresh_failures_never_send_resource_request_or_leak_secrets(
    response: httpx.Response,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW)
    caplog.set_level(logging.DEBUG)
    ctx, store = stored_context(GitHubConnector, auth_record())
    rec = Recorder(lambda r: response)
    sleeps = Sleeps()
    result = await execute(
        GitHubConnector(transport=rec.transport, sleep=sleeps), ctx, "get_repo", REPO
    )
    expected = "RATE_LIMITED" if response.status_code == 429 else "AUTH_FAILED"
    assert result.error_code == expected and len(rec.requests) == 1
    assert sleeps.delays == []
    assert json.loads(store.resolve("workspace", "cred").reveal())["access_token"] == OLD
    text = str(result) + caplog.text
    assert all(s not in text for s in [OLD, NEW, REFRESH, CLIENT])


async def test_read_only_resolver_refuses_refresh_before_consuming_token() -> None:
    rec = Recorder(lambda r: json_response({}))
    result = await invoke(
        GitHubConnector(transport=rec.transport),
        "get_repo",
        REPO,
        secrets={"cred": json.dumps(auth_record(expires_at=0))},
    )
    assert result.error_code == "INVALID_CONFIG" and "writable" in result.error_message
    assert rec.requests == []


async def test_custom_persister_and_persist_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW)

    class Resolver:
        secret = json.dumps(auth_record())
        fail = False

        def resolve(self, ref: Any) -> str:
            return self.secret

        def update_secret(self, ref: Any, secret: str) -> None:
            if self.fail:
                raise RuntimeError(NEW)
            self.secret = secret

    for fail in [False, True]:
        resolver = Resolver()
        resolver.fail = fail
        ctx = ExecutionContext(run_id="r", node_id="n", credential_resolver=resolver)
        rec = Recorder(lambda r: json_response({"access_token": NEW, "expires_in": 3600}))
        result = await execute(GitHubConnector(transport=rec.transport), ctx, "get_repo", REPO)
        assert result.error_code == "AUTH_FAILED" if fail else result.status == "success"
        assert len(rec.requests) == (1 if fail else 2)
        assert NEW not in str(result)


async def test_github_installation_token_and_jwt_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW)
    ctx, store = stored_context(
        GitHubConnector,
        {
            "auth_type": "github_app",
            "installation_id": 123,
            "expires_at": 0,
        },
    )
    jwt = "signed-app-jwt-secret"

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path.startswith("/app/"):
            assert r.url.path == "/app/installations/123/access_tokens"
            assert r.headers["authorization"] == f"Bearer {jwt}"
            return json_response({"token": NEW, "expires_at": "2027-01-16T09:00:00Z"})
        assert r.headers["authorization"] == f"Bearer {NEW}"
        return json_response({})

    rec = Recorder(handler)
    module = GitHubConnector(transport=rec.transport, app_jwt_provider=lambda: jwt)
    result = await execute(module, ctx, "get_repo", REPO)
    assert result.status == "success", result
    assert len(rec.requests) == 2
    assert json.loads(store.resolve("workspace", "cred").reveal())["access_token"] == NEW


@pytest.mark.parametrize("cls,op,params,payload", CASES)
async def test_retry_after_is_honoured_entirely_and_writes_never_retry(
    cls: Any,
    op: str,
    params: dict[str, Any],
    payload: Any,
) -> None:
    for date in [False, True]:
        count = 0

        def handler(r: httpx.Request, date: bool = date) -> httpx.Response:
            nonlocal count
            count += 1
            after = format_datetime(datetime.fromtimestamp(time.time() + 12, UTC)) if date else "12"
            return (
                json_response({}, 429, **{"Retry-After": after})
                if count == 1
                else json_response(payload)
            )

        rec, sleeps = Recorder(handler), Sleeps()
        result = await invoke(cls(transport=rec.transport, sleep=sleeps), op, params)
        assert result.status == "success" and len(rec.requests) == 2
        assert 11 <= sum(sleeps.delays) <= 13.2 and all(d <= 5 for d in sleeps.delays)
    write = next(o for o in cls.OPS.values() if o.write)
    write_params = {
        "create_issue": {**REPO, "title": "bug"},
        "post_message": {"channel": "C", "text": "hello"},
        "upload_text": {"name": "a.txt", "content": "a"},
        "append_rows": {"spreadsheet_id": "s", "range": "A1", "values": [[1]]},
    }[write.name]
    rec, sleeps = Recorder(lambda r: json_response({}, 429, **{"Retry-After": "12"})), Sleeps()
    result = await invoke(
        cls(transport=rec.transport, sleep=sleeps), write.name, write_params, approved=True
    )
    assert result.error_code == "RATE_LIMITED" and len(rec.requests) == 1 and sleeps.delays == []


@pytest.mark.parametrize(
    "headers,body,minimum",
    [
        ({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(NOW + 17)}, {}, 17),
        ({"Retry-After": "13"}, {"message": "secondary rate limit"}, 13),
        ({}, {"message": "You have exceeded a secondary rate limit"}, 60),
        ({}, {"message": "abuse detection"}, 60),
    ],
)
async def test_github_primary_and_secondary_limits(
    headers: dict[str, str],
    body: Any,
    minimum: float,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW)
    rec = Recorder(lambda r: json_response(body, 403, **headers))
    sleeps = Sleeps()
    result = await invoke(
        GitHubConnector(transport=rec.transport, sleep=sleeps),
        "get_repo",
        REPO,
        cfg={"max_retries": 1},
    )
    assert result.error_code == "RATE_LIMITED"
    if minimum >= 30:
        assert len(rec.requests) == 1 and sleeps.delays == []
        assert result.metadata["retry_after_seconds"] >= minimum
    else:
        assert len(rec.requests) == 2
        assert minimum <= sum(sleeps.delays) <= minimum * 1.1
    rec.requests.clear()
    sleeps.delays.clear()
    result = await invoke(
        GitHubConnector(transport=rec.transport, sleep=sleeps),
        "create_issue",
        {**REPO, "title": "bug"},
        approved=True,
    )
    assert result.error_code == "RATE_LIMITED" and len(rec.requests) == 1 and not sleeps.delays


@pytest.mark.parametrize("cls", [GoogleDriveConnector, GoogleSheetsConnector])
@pytest.mark.parametrize("reason", ["rateLimitExceeded", "userRateLimitExceeded"])
async def test_google_403_jittered_backoff(cls: Any, reason: str) -> None:
    rec = Recorder(lambda r: json_response({"error": {"errors": [{"reason": reason}]}}, 403))
    sleeps = Sleeps()
    _, op, params, _ = next(c for c in CASES if c[0] is cls)
    result = await invoke(
        cls(transport=rec.transport, sleep=sleeps), op, params, cfg={"max_retries": 2}
    )
    assert result.error_code == "RATE_LIMITED" and len(rec.requests) == 3
    assert 0.5 <= sleeps.delays[0] <= 1 and 1 <= sleeps.delays[1] <= 2
    rec.requests.clear()
    sleeps.delays.clear()
    op, params = (
        ("upload_text", {"name": "a.txt", "content": "hi"})
        if cls is GoogleDriveConnector
        else ("append_rows", {"spreadsheet_id": "s", "range": "A1", "values": [[1]]})
    )
    result = await invoke(cls(transport=rec.transport, sleep=sleeps), op, params, approved=True)
    assert result.error_code == "RATE_LIMITED" and len(rec.requests) == 1
    assert sleeps.delays == []


async def test_slack_http_200_rate_limit_retry() -> None:
    rec = Recorder(
        lambda r: json_response({"ok": False, "error": "ratelimited"}, **{"Retry-After": "7"})
    )
    sleeps = Sleeps()
    result = await invoke(
        SlackConnector(transport=rec.transport, sleep=sleeps),
        "list_channels",
        {},
        cfg={"max_retries": 1},
    )
    assert result.error_code == "RATE_LIMITED" and 7 <= sum(sleeps.delays) <= 7.7
    rec.requests.clear()
    sleeps.delays.clear()
    result = await invoke(
        SlackConnector(transport=rec.transport, sleep=sleeps),
        "post_message",
        {"channel": "C", "text": "hi"},
        approved=True,
    )
    assert result.error_code == "RATE_LIMITED" and len(rec.requests) == 1
    assert sleeps.delays == []


@pytest.mark.parametrize("service", ["github", "slack", "gdrive"])
async def test_pagination_max_items_resumes_inside_page_without_loss(service: str) -> None:
    pages = [[{"id": i} for i in range(3)], [{"id": i} for i in range(3, 6)]]

    def handler(r: httpx.Request) -> httpx.Response:
        page = (
            int(r.url.params.get("page", "1")) - 1
            if service == "github"
            else int(r.url.params.get("cursor" if service == "slack" else "pageToken", "0"))
        )
        if service == "github":
            assert r.url.params["per_page"] == "3"
            link = (
                "<https://api.github.com/repos/acme/widgets/issues?page=2&per_page=3>; "
                'type="application/json"; rel="next"'
            )
            return json_response(pages[page], **({"Link": link} if page == 0 else {}))
        if service == "slack":
            assert r.url.params["limit"] == "3"
            return json_response(
                {
                    "ok": True,
                    "channels": pages[page],
                    "response_metadata": {"next_cursor": "1" if page == 0 else ""},
                }
            )
        assert r.url.params["pageSize"] == "3"
        return json_response(
            {"files": pages[page], **({"nextPageToken": "1"} if page == 0 else {})}
        )

    cls, op, params = {
        "github": (GitHubConnector, "list_issues", {**REPO, "per_page": 3}),
        "slack": (SlackConnector, "list_channels", {"limit": 3}),
        "gdrive": (GoogleDriveConnector, "list_files", {"page_size": 3}),
    }[service]
    rec = Recorder(handler)
    module = cls(transport=rec.transport)
    first = await invoke(module, op, {**params, "max_items": 4})
    assert first.status == "success", first
    assert [i["id"] for i in first.outputs["data"]] == [0, 1, 2, 3]
    assert first.outputs["truncated"] is True
    second = await invoke(module, op, {**params, "cursor": first.outputs["next_cursor"]})
    assert second.status == "success", second
    assert [i["id"] for i in second.outputs["data"]] == [4, 5]
    assert second.outputs["truncated"] is False
    # A cap inside the very first page also retains that page's original request.
    first = await invoke(module, op, {**params, "max_items": 1})
    second = await invoke(module, op, {**params, "cursor": first.outputs["next_cursor"]})
    assert [i["id"] for i in first.outputs["data"] + second.outputs["data"]] == list(range(6))


async def test_google_empty_pages_and_incomplete_search() -> None:
    rec = Recorder(
        lambda r: json_response(
            {"files": [{"id": "last"}]}
            if r.url.params.get("pageToken")
            else {"nextPageToken": "next"}
        )
    )
    result = await invoke(GoogleDriveConnector(transport=rec.transport), "list_files", {})
    assert result.outputs["data"] == [{"id": "last"}] and not result.outputs["truncated"]
    rec = Recorder(lambda r: json_response({"files": [], "incompleteSearch": True}))
    result = await invoke(GoogleDriveConnector(transport=rec.transport), "list_files", {})
    assert result.outputs["truncated"] and result.outputs["incomplete_search"]


async def test_sheets_row_limit_is_explicit_and_no_fake_page_token() -> None:
    rec = Recorder(lambda r: json_response({"values": [[1], [2], [3]]}))
    result = await invoke(
        GoogleSheetsConnector(transport=rec.transport),
        "read_range",
        {"spreadsheet_id": "s", "range": "A1:A3", "max_items": 2},
    )
    assert result.outputs["data"] == [[1], [2]] and result.outputs["truncated"]
    assert "next_cursor" not in result.outputs and len(rec.requests) == 1


async def test_slack_has_more_without_cursor_does_not_report_complete() -> None:
    rec = Recorder(
        lambda r: json_response({"ok": True, "messages": [{"ts": "1"}], "has_more": True})
    )
    result = await invoke(SlackConnector(transport=rec.transport), "read_history", {"channel": "C"})
    assert result.status == "success" and result.outputs["truncated"]
    assert "next_cursor" not in result.outputs


async def test_supported_write_keys_and_conservative_effect_declarations() -> None:
    rec = Recorder(lambda r: json_response({"ok": True, "id": "file", "channel": "C", "ts": "1"}))
    key = "d3abebd2-e494-4d2d-812a-38d5dcfce10a"
    result = await invoke(
        SlackConnector(transport=rec.transport),
        "post_message",
        {"channel": "C", "text": "hi", "client_msg_id": key},
        approved=True,
    )
    assert result.status == "success" and rec.body()["client_msg_id"] == key
    ctx = make_context(approval_granted=True)
    ctx.step_key = "send#1"
    for _ in range(2):
        await execute(
            SlackConnector(transport=rec.transport),
            ctx,
            "post_message",
            {"channel": "C", "text": "hi"},
        )
    assert UUID(rec.body()["client_msg_id"]) == UUID(rec.body(-2)["client_msg_id"])
    assert rec.last.headers["idempotency-key"] == ctx.idempotency_key()
    result = await invoke(
        GoogleDriveConnector(transport=rec.transport),
        "upload_text",
        {"name": "a.txt", "content": "a", "file_id": "pre_generated_drive_id"},
        approved=True,
    )
    assert result.status == "success" and b'"id": "pre_generated_drive_id"' in rec.last.content
    for cls, _, _, _ in CASES:
        assert module_effect(cls()) == "non_idempotent"
        assert "effect_policy" in cls().manifest.model_fields_set
        examples = {e["operation"]: e for e in cls().manifest.display.examples}
        for op in cls.OPS.values():
            if op.write:
                assert op.effect_class == "non_idempotent"
                assert examples[op.name]["effect_class"] == "non_idempotent"


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "AUTH_FAILED"),
        (403, "AUTH_FAILED"),
        (400, "UPSTREAM_ERROR"),
        (404, "UPSTREAM_ERROR"),
        (409, "UPSTREAM_ERROR"),
        (422, "UPSTREAM_ERROR"),
    ],
)
async def test_actionable_errors_omit_untrusted_response_bodies(status: int, code: str) -> None:
    rec = Recorder(lambda r: httpx.Response(status, text=NEW + CLIENT + REFRESH))
    result = await invoke(GitHubConnector(transport=rec.transport), "get_repo", REPO)
    assert result.error_code == code
    assert "check" in result.error_message or status == 409
    assert all(secret not in str(result) for secret in [NEW, CLIENT, REFRESH])


async def test_refreshed_token_echo_is_redacted_from_outputs_and_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW)
    caplog.set_level(logging.DEBUG)
    ctx, _ = stored_context(GitHubConnector, auth_record())
    rec = Recorder(
        lambda r: json_response(
            {"access_token": NEW, "expires_in": 3600, "refresh_token": ROTATED}
            if r.method == "POST"
            else {"debug": " ".join([NEW, ROTATED, OLD, CLIENT, REFRESH])}
        )
    )
    result = await execute(GitHubConnector(transport=rec.transport), ctx, "get_repo", REPO)
    assert result.status == "success"
    assert all(
        secret not in str(result) + caplog.text for secret in [NEW, ROTATED, OLD, CLIENT, REFRESH]
    )


@pytest.mark.parametrize("change", ["revoked", "replaced", "deleted"])
async def test_refresh_cannot_restore_revoked_or_overwrite_changed_credentials(
    change: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW)
    ctx, store = stored_context(GitHubConnector, auth_record())

    def handler(r: httpx.Request) -> httpx.Response:
        if change == "revoked":
            store.revoke("workspace", "cred")
        elif change == "deleted":
            store.delete("workspace", "cred")
        else:
            store.store(
                "workspace", "cred", GitHubConnector.CREDENTIAL_TYPE, "operator-replacement"
            )
        return json_response({"access_token": NEW, "expires_in": 3600})

    rec = Recorder(handler)
    result = await execute(GitHubConnector(transport=rec.transport), ctx, "get_repo", REPO)
    assert result.error_code == "AUTH_FAILED" and len(rec.requests) == 1
    if change == "revoked":
        assert store.get_metadata("workspace", "cred").status == "revoked"
    elif change == "replaced":
        assert store.resolve("workspace", "cred").reveal() == "operator-replacement"


async def test_cancelling_one_waiter_keeps_refresh_for_other_waiters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW)
    ctx, _ = stored_context(GitHubConnector, auth_record())
    entered, release = asyncio.Event(), asyncio.Event()
    requests: list[httpx.Request] = []

    async def handler(r: httpx.Request) -> httpx.Response:
        requests.append(r)
        if r.method == "POST":
            entered.set()
            await release.wait()
            return json_response({"access_token": NEW, "expires_in": 3600})
        return json_response({})

    module = GitHubConnector(transport=httpx.MockTransport(handler))
    first = asyncio.create_task(execute(module, ctx, "get_repo", REPO))
    await entered.wait()
    second = asyncio.create_task(execute(module, ctx, "get_repo", REPO))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    result = await second
    assert result.status == "success"
    assert len([r for r in requests if r.method == "POST"]) == 1


@pytest.mark.parametrize(
    "record,code",
    [
        (auth_record(expires_at="not-a-time"), "INVALID_CONFIG"),
        (auth_record(expires_at=float("inf")), "INVALID_CONFIG"),
        (auth_record(refresh_expires_at=0), "AUTH_FAILED"),
        (auth_record(token_url="https://evil.example/token"), "SSRF_BLOCKED"),
        (auth_record(token_url="https://github.com/token?secret=do-not-log"), "INVALID_CONFIG"),
    ],
)
async def test_invalid_refresh_records_are_rejected_before_http(
    record: Any,
    code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW)
    ctx, _ = stored_context(GitHubConnector, record)
    rec = Recorder(lambda r: json_response({}))
    result = await execute(GitHubConnector(transport=rec.transport), ctx, "get_repo", REPO)
    assert result.error_code == code and not rec.requests
    assert "do-not-log" not in str(result)


async def test_unapproved_write_cannot_even_trigger_refresh() -> None:
    ctx, _ = stored_context(SlackConnector, auth_record(expires_at=0))
    rec = Recorder(lambda r: json_response({}))
    result = await execute(
        SlackConnector(transport=rec.transport), ctx, "post_message", {"channel": "C", "text": "hi"}
    )
    assert result.error_code == "APPROVAL_REQUIRED" and not rec.requests


async def test_very_long_server_delay_defers_without_early_retry() -> None:
    sleeps = Sleeps()
    rec = Recorder(lambda r: json_response({}, 429, **{"Retry-After": "1e300"}))
    result = await invoke(GitHubConnector(transport=rec.transport, sleep=sleeps), "get_repo", REPO)
    assert result.error_code == "RATE_LIMITED" and len(rec.requests) == 1
    assert sleeps.delays == []


async def test_refresh_between_pages_uses_latest_token(monkeypatch: pytest.MonkeyPatch) -> None:
    current = [NOW]
    monkeypatch.setattr(time, "time", lambda: current[0])
    ctx, _ = stored_context(GoogleDriveConnector, auth_record(expires_at=NOW + 100))

    def handler(r: httpx.Request) -> httpx.Response:
        if r.method == "POST":
            return json_response(
                {"access_token": NEW, "expires_in": 3600, "refresh_token_expires_in": 86400}
            )
        if r.url.params.get("pageToken"):
            assert r.headers["authorization"] == f"Bearer {NEW}"
            return json_response({"files": [{"id": 2}]})
        assert r.headers["authorization"] == f"Bearer {OLD}"
        current[0] += 101
        return json_response({"files": [{"id": 1}], "nextPageToken": "next"})

    rec = Recorder(handler)
    result = await execute(GoogleDriveConnector(transport=rec.transport), ctx, "list_files", {})
    assert result.status == "success" and result.outputs["data"] == [{"id": 1}, {"id": 2}]
    assert len(rec.requests) == 3


async def test_refresh_after_rate_limit_wait_uses_latest_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [NOW]
    monkeypatch.setattr(time, "time", lambda: current[0])
    ctx, _ = stored_context(GitHubConnector, auth_record(expires_at=NOW + 40))
    count = 0

    async def sleep(seconds: float) -> None:
        current[0] += seconds

    def handler(r: httpx.Request) -> httpx.Response:
        nonlocal count
        if r.method == "POST":
            return json_response({"access_token": NEW, "expires_in": 3600})
        count += 1
        assert r.headers["authorization"] == f"Bearer {OLD if count == 1 else NEW}"
        return json_response({}, 429, **{"Retry-After": "12"}) if count == 1 else json_response({})

    rec = Recorder(handler)
    result = await execute(
        GitHubConnector(transport=rec.transport, sleep=sleep), ctx, "get_repo", REPO
    )
    assert result.status == "success" and len(rec.requests) == 3
