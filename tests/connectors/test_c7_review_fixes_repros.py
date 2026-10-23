"""P38 C7 adversarial review repros. MockTransport only; no network.

Each original review scenario asserts the repaired contract. Safe-behaviour
confirmations remain intact.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import httpx
import pytest
from connector_helpers import Recorder, Sleeps, invoke, json_response
from nexura_agents.contracts.context import ExecutionContext
from nexura_connectors import GitHubConnector, GoogleDriveConnector, SlackConnector

from nexura_runtime.credentials.store import (
    ScopedCredentialStore,
    WorkspaceCredentialResolver,
)

NOW = 1_800_000_000.0
REPO = {"owner": "acme", "repo": "widgets"}


def record(**updates: Any) -> dict[str, Any]:
    return {
        "auth_type": "oauth2",
        "access_token": "old-access-secret-value",
        "expires_at": NOW - 1,
        "refresh_token": "refresh-secret-value",
        "client_id": "test-client",
        "client_secret": "client-secret-value",
        **updates,
    }


def ctx_for(cls: Any, rec: dict[str, Any], **kw: Any) -> tuple[ExecutionContext, Any]:
    store = ScopedCredentialStore()
    store.store("ws", "cred", cls.CREDENTIAL_TYPE, json.dumps(rec), provider="p")
    ctx = ExecutionContext(
        run_id="r",
        node_id="n",
        workspace_id="ws",
        credential_resolver=WorkspaceCredentialResolver(store, "ws"),
        **kw,
    )
    return ctx, store


async def run(module: Any, ctx: ExecutionContext, op: str, params: dict[str, Any], **cfg: Any):
    return await module.execute(
        ctx, {"operation": op, "params": params}, {"credential_ref": "cred", **cfg}
    )


# --- MAJOR 1: non-secret record/token-response fields are added to the redaction list ---


async def test_record_metadata_corrupts_unrelated_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """auth_type/client_id/token_type/scope values are scrubbed out of real API data."""
    monkeypatch.setattr(time, "time", lambda: NOW)
    ctx, _ = ctx_for(GitHubConnector, record(expires_at=NOW + 3600))
    rec = Recorder(lambda r: json_response({"name": "oauth2 helpers for test-client apps"}))
    res = await run(GitHubConnector(transport=rec.transport), ctx, "get_repo", REPO)
    assert res.status == "success"
    assert res.outputs["data"]["name"] == "oauth2 helpers for test-client apps"


async def test_slack_scope_words_redacted_from_history(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW)
    ctx, _ = ctx_for(SlackConnector, record())

    def handler(r: httpx.Request) -> httpx.Response:
        if str(r.url) == SlackConnector.TOKEN_URL:
            return json_response(
                {
                    "ok": True,
                    "access_token": "xoxe.xoxb-new-access",
                    "expires_in": 43200,
                    "token_type": "Bearer",
                    "scope": "chat:write,channels:history,channels:read",
                }
            )
        return json_response(
            {"ok": True, "messages": [{"text": "Bearer of news: read the chat history"}]}
        )

    rec = Recorder(handler)
    res = await run(SlackConnector(transport=rec.transport), ctx, "read_history", {"channel": "C1"})
    assert res.status == "success"
    text = res.outputs["data"][0]["text"]
    assert text == "Bearer of news: read the chat history", text


async def test_google_token_type_bearer_redacted_from_file_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW)
    ctx, _ = ctx_for(GoogleDriveConnector, record())

    def handler(r: httpx.Request) -> httpx.Response:
        if str(r.url) == GoogleDriveConnector.TOKEN_URL:
            return json_response(
                {
                    "access_token": "ya29.new-access",
                    "expires_in": 3599,
                    "token_type": "Bearer",
                    "scope": "https://www.googleapis.com/auth/drive.readonly",
                }
            )
        return json_response({"id": "f1", "name": "Quarterly chat read-out.txt"})

    rec = Recorder(handler)
    res = await run(
        GoogleDriveConnector(transport=rec.transport), ctx, "get_file", {"file_id": "f1"}
    )
    assert res.outputs["data"]["name"] == "Quarterly chat read-out.txt"


# --- MAJOR 2: total wait for one read is effectively unbounded (hours) ---


async def test_read_waits_hours_on_retry_after() -> None:
    sleeps = Sleeps()
    rec = Recorder(lambda r: json_response({}, 429, **{"Retry-After": "3600"}))
    res = await invoke(
        GitHubConnector(transport=rec.transport, sleep=sleeps),
        "get_repo",
        REPO,
        cfg={"max_retries": 3},
    )
    assert res.error_code == "RATE_LIMITED"
    assert len(rec.requests) == 1 and sleeps.delays == []
    assert res.metadata["retryable"] is True
    assert 3600 <= res.metadata["retry_after_seconds"] <= 3960


async def test_secondary_limit_without_timing_waits_60s_each_retry() -> None:
    sleeps = Sleeps()
    rec = Recorder(
        lambda r: json_response({"message": "You have exceeded a secondary rate limit"}, 403)
    )
    res = await invoke(
        GitHubConnector(transport=rec.transport, sleep=sleeps),
        "get_repo",
        REPO,
        cfg={"max_retries": 3},
    )
    assert sleeps.delays == [] and len(rec.requests) == 1
    assert res.error_code == "RATE_LIMITED" and res.metadata["retry_after_seconds"] >= 60


# --- MINOR: pagination loop / duplicate items ---


async def test_github_self_referencing_next_link_duplicates_items() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        url = "https://api.github.com/repos/acme/widgets/issues?state=open&per_page=1&page=1"
        return httpx.Response(200, json=[{"id": 1}], headers={"Link": f'<{url}>; rel="next"'})

    rec = Recorder(handler)
    res = await invoke(
        GitHubConnector(transport=rec.transport), "list_issues", REPO, cfg={"max_pages": 5}
    )
    assert [i["id"] for i in res.outputs["data"]] == [1, 1]
    assert len(rec.requests) == 2
    assert res.outputs["pagination_loop"] is True and res.outputs["truncated"] is True


async def test_slack_repeated_cursor_duplicates_items() -> None:
    rec = Recorder(
        lambda r: json_response(
            {"ok": True, "channels": [{"id": "C1"}], "response_metadata": {"next_cursor": "same"}}
        )
    )
    res = await invoke(
        SlackConnector(transport=rec.transport), "list_channels", {}, cfg={"max_pages": 4}
    )
    assert [c["id"] for c in res.outputs["data"]] == ["C1"] * 2
    assert len(rec.requests) == 2
    assert res.outputs["pagination_loop"] is True and res.outputs["truncated"] is True


# --- confirmations (expected-safe behaviour) ---


async def test_github_next_link_to_other_host_is_refused() -> None:
    rec = Recorder(
        lambda r: httpx.Response(
            200,
            json=[{"id": 1}],
            headers={"Link": '<https://evil.example/repos/acme/widgets/issues?page=2>; rel="next"'},
        )
    )
    res = await invoke(GitHubConnector(transport=rec.transport), "list_issues", REPO)
    assert res.error_code == "SSRF_BLOCKED" and len(rec.requests) == 1
    assert res.outputs["data"] == [{"id": 1}] and res.outputs["truncated"]


async def test_user_cursor_to_other_host_is_refused_before_send() -> None:
    rec = Recorder(lambda r: json_response([]))
    res = await invoke(
        GitHubConnector(transport=rec.transport),
        "list_issues",
        {**REPO, "cursor": "https://evil.example/repos/acme/widgets/issues"},
    )
    assert res.error_code == "INVALID_PARAMS" and not rec.requests


async def test_huge_and_nonfinite_retry_after() -> None:
    for raw in ("1e308", "inf", "nan", "-5", "Wed, 21 Oct 2015 07:28:00 GMT"):
        sleeps = Sleeps()
        n = {"i": 0}

        def handler(r: httpx.Request, raw: str = raw, n: dict[str, int] = n) -> httpx.Response:
            n["i"] += 1
            return (
                json_response({}, 429, **{"Retry-After": raw}) if n["i"] == 1 else json_response({})
            )

        rec = Recorder(handler)
        res = await invoke(GitHubConnector(transport=rec.transport, sleep=sleeps), "get_repo", REPO)
        if raw == "1e308":
            assert res.error_code == "RATE_LIMITED" and len(rec.requests) == 1
        else:
            assert res.status == "success" and all(d <= 30 for d in sleeps.delays), (
                raw,
                sleeps.delays,
            )


# --- MINOR: transient token-endpoint failure reported as AUTH_FAILED, non-retryable ---


async def test_token_endpoint_timeout_is_auth_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW)
    ctx, _ = ctx_for(GoogleDriveConnector, record())

    def handler(r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=r)

    res = await run(
        GoogleDriveConnector(transport=httpx.MockTransport(handler)), ctx, "list_files", {}
    )
    assert res.error_code == "UPSTREAM_UNAVAILABLE"
    assert res.metadata.get("retryable") is True
    assert "rotated" in res.error_message and "re-authorize" in res.error_message


# --- MINOR (pre-existing): 5xx after a write is not reported as indeterminate ---


async def test_write_502_is_upstream_error_not_indeterminate() -> None:
    rec = Recorder(lambda r: json_response({}, 502))
    res = await invoke(
        GitHubConnector(transport=rec.transport),
        "create_issue",
        {**REPO, "title": "t"},
        approved=True,
    )
    assert res.error_code == "INDETERMINATE_EFFECT" and len(rec.requests) == 1
    assert res.status == "indeterminate"


# --- MINOR: dataclass repr exposes refresh token / client secret ---


def test_connector_http_repr_contains_refresh_token() -> None:
    secret = json.dumps(record())
    http = GitHubConnector().build_http({}, secret)
    assert all(s not in repr(http) for s in ["refresh-secret-value", "client-secret-value"])


# --- MINOR: GitHub App stored-JWT mode keeps a stale JWT and the expiry may be naive ---


async def test_github_app_naive_expiry_is_local_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW)
    ctx, store = ctx_for(
        GitHubConnector,
        {"auth_type": "github_app", "installation_id": 7, "app_jwt": "eyJhbGciOi.jwt.sig"},
    )

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path.endswith("/access_tokens"):
            return json_response(
                {"token": "ghs_installation", "expires_at": "2027-01-15T09:00:00"}, 201
            )
        return json_response({"name": "w"})

    res = await run(GitHubConnector(transport=Recorder(handler).transport), ctx, "get_repo", REPO)
    assert res.status == "success", (res.error_code, res.error_message)
    saved = json.loads(store.resolve("ws", "cred").reveal())
    assert "app_jwt" not in saved
    from datetime import UTC, datetime

    assert saved["expires_at"] == datetime(2027, 1, 15, 9, tzinfo=UTC).timestamp()


# --- MINOR/limitation: no single-flight across threads/event loops ---


def test_two_event_loops_both_refresh_and_one_fails_after_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW)
    ctx, store = ctx_for(GitHubConnector, record())
    # Synchronise callers before dispatch rather than deadlocking the correctly
    # serialised token endpoint waiting for a forbidden second refresh POST.
    barrier = threading.Barrier(2, timeout=10)
    posts: list[str] = []
    lock = threading.Lock()

    def handler(r: httpx.Request) -> httpx.Response:
        if str(r.url) == GitHubConnector.TOKEN_URL:
            with lock:
                posts.append(r.content.decode())
                n = len(posts)
            time.sleep(0.05)
            return json_response(
                {"access_token": f"new-{n}", "expires_in": 3600, "refresh_token": f"rot-{n}"}
            )
        return json_response({"name": "w"})

    transport = httpx.MockTransport(handler)
    results: list[Any] = [None, None]

    def worker(i: int) -> None:
        barrier.wait()
        results[i] = asyncio.run(run(GitHubConnector(transport=transport), ctx, "get_repo", REPO))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert all(not t.is_alive() for t in threads)
    assert len(posts) == 1
    codes = sorted(str(r.error_code) for r in results)
    assert codes == ["None", "None"], codes
    assert json.loads(store.resolve("ws", "cred").reveal())["refresh_token"] == "rot-1"


# --- BLOCKER: GitHub's documented Link form /repositories/{id}/issues is refused ---


async def test_github_repository_id_next_link_fails_whole_listing() -> None:
    """GitHub's pagination docs show next links as /repositories/1300192/issues?page=N for a
    /repos/{owner}/{repo}/issues request. C7 validates upstream next links against the
    /repos/{owner}/{repo} path, so a second page aborts the read and page 1 is lost."""

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path.startswith("/repositories/"):
            return json_response([{"id": 3}])
        return httpx.Response(
            200,
            json=[{"id": 1}, {"id": 2}],
            headers={
                "Link": '<https://api.github.com/repositories/1300192/issues?page=2>; rel="next", '
                '<https://api.github.com/repositories/1300192/issues?page=9>; rel="last"'
            },
        )

    rec = Recorder(handler)
    res = await invoke(GitHubConnector(transport=rec.transport), "list_issues", REPO)
    assert res.status == "success" and len(rec.requests) == 2
    assert res.outputs["data"] == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert not res.outputs["truncated"]
    rec2 = Recorder(handler)
    res2 = await invoke(
        GitHubConnector(transport=rec2.transport), "list_issues", REPO, cfg={"max_pages": 1}
    )
    assert res2.status == "success" and len(rec2.requests) == 1
    assert res2.outputs["data"] == [{"id": 1}, {"id": 2}]
    assert res2.outputs["truncated"]
    assert (
        res2.outputs["next_cursor"] == "https://api.github.com/repositories/1300192/issues?page=2"
    )


async def test_generic_pattern_redaction_still_masks_connector_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C7 re-review R2-M1: outputs keep the platform's generic redaction (bearer tokens,
    API keys, passwords), not only the connector's own known secret values."""
    monkeypatch.setattr(time, "time", lambda: NOW)
    ctx, _ = ctx_for(GoogleDriveConnector, record())
    leaked = (
        "Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz0123456789 password=hunter2hunter2"
    )

    def handler(r: httpx.Request) -> httpx.Response:
        if str(r.url) == GoogleDriveConnector.TOKEN_URL:
            return json_response(
                {"access_token": "ya29.new-access", "expires_in": 3599, "token_type": "Bearer"}
            )
        return json_response({"id": "f1", "name": leaked})

    rec = Recorder(handler)
    res = await run(
        GoogleDriveConnector(transport=rec.transport), ctx, "get_file", {"file_id": "f1"}
    )
    name = res.outputs["data"]["name"]
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in name
    assert "hunter2hunter2" not in name
