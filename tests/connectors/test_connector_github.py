"""GitHub connector tests (httpx.MockTransport; no network)."""

from __future__ import annotations

import httpx
from connector_helpers import TOKEN, Negatives, Recorder, Sleeps, Spec, invoke, json_response
from nexura_connectors import GitHubConnector

REPO = {"owner": "acme", "repo": "widgets"}
SPEC = Spec(
    factory=lambda t, s: GitHubConnector(transport=t, sleep=s),
    read_op="get_repo",
    read_params=REPO,
    read_body={"full_name": "acme/widgets"},
    write_op="create_issue",
    write_params={**REPO, "title": "Bug"},
    write_body={"number": 7, "html_url": "https://github.com/acme/widgets/issues/7"},
)


async def test_negative_contract(neg_case: str, negatives: Negatives) -> None:
    await negatives.check(neg_case, SPEC)


async def test_get_repo_headers_and_path() -> None:
    rec = Recorder(lambda r: json_response({"full_name": "acme/widgets"}))
    res = await invoke(GitHubConnector(transport=rec.transport), "get_repo", REPO)
    assert res.outputs["data"]["full_name"] == "acme/widgets" and res.outputs["write"] is False
    assert str(rec.last.url) == "https://api.github.com/repos/acme/widgets"
    assert rec.last.headers["authorization"] == f"Bearer {TOKEN}"
    assert rec.last.headers["x-github-api-version"] == "2022-11-28"


async def test_path_injection_in_owner_is_rejected() -> None:
    rec = Recorder(lambda r: json_response({}))
    for owner in ("../admin", "a/b", "a?x=1", ""):
        res = await invoke(
            GitHubConnector(transport=rec.transport), "get_repo", {"owner": owner, "repo": "r"}
        )
        assert res.status == "failure", owner
    assert rec.requests == []


async def test_list_issues_follows_link_header_up_to_cap() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        n = int(r.url.params.get("page", "1"))
        link = f'<https://api.github.com/repos/acme/widgets/issues?page={n + 1}>; rel="next"'
        return httpx.Response(200, json=[{"number": n}], headers={"link": link})

    rec = Recorder(handler)
    res = await invoke(
        GitHubConnector(transport=rec.transport), "list_issues", REPO, cfg={"max_pages": 3}
    )
    assert [i["number"] for i in res.outputs["data"]] == [1, 2, 3]
    assert res.outputs["truncated"] is True and len(rec.requests) == 3


async def test_link_header_to_other_origin_never_receives_the_token() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=[{"number": 1}], headers={"link": '<https://evil.example/steal>; rel="next"'}
        )

    rec = Recorder(handler)
    res = await invoke(GitHubConnector(transport=rec.transport), "list_pulls", REPO)
    assert res.error_code == "SSRF_BLOCKED"
    assert all(r.url.host == "api.github.com" for r in rec.requests)


async def test_link_header_to_metadata_ip_blocked() -> None:
    link = '<https://169.254.169.254/latest>; rel="next"'
    rec = Recorder(lambda r: httpx.Response(200, json=[], headers={"link": link}))
    res = await invoke(GitHubConnector(transport=rec.transport), "list_issues", REPO)
    assert res.error_code == "SSRF_BLOCKED" and len(rec.requests) == 1


async def test_create_issue_and_comment_bodies() -> None:
    rec = Recorder(lambda r: json_response({"number": 7, "id": 3, "html_url": "u"}, 201))
    mod = GitHubConnector(transport=rec.transport)
    res = await invoke(
        mod, "create_issue", {**REPO, "title": "T", "body": "B", "labels": ["bug"]}, approved=True
    )
    assert res.outputs["data"]["number"] == 7
    assert rec.body() == {"title": "T", "body": "B", "labels": ["bug"]}
    res = await invoke(
        mod, "create_comment", {**REPO, "issue_number": 7, "body": "hi"}, approved=True
    )
    assert res.status == "success"
    assert str(rec.last.url).endswith("/issues/7/comments") and rec.last.method == "POST"


async def test_token_echoed_by_upstream_is_scrubbed_from_output() -> None:
    rec = Recorder(lambda r: json_response({"full_name": "x", "debug": f"auth={TOKEN}"}))
    res = await invoke(GitHubConnector(transport=rec.transport), "get_repo", REPO)
    assert TOKEN not in str(res.outputs)


async def test_enterprise_base_url_uses_ssrf_policy() -> None:
    rec = Recorder(lambda r: json_response({"full_name": "x"}))
    res = await invoke(
        GitHubConnector(transport=rec.transport, allowed_hosts=["ghe.example.com"]),
        "get_repo",
        REPO,
        cfg={"base_url": "https://ghe.example.com/api/v3"},
    )
    assert res.status == "success"
    assert str(rec.last.url) == "https://ghe.example.com/api/v3/repos/acme/widgets"


async def test_retries_only_on_reads_counted_in_metadata() -> None:
    state = {"n": 0}

    def handler(r: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return httpx.Response(500) if state["n"] == 1 else json_response({"full_name": "x"})

    rec = Recorder(handler)
    res = await invoke(GitHubConnector(transport=rec.transport, sleep=Sleeps()), "get_repo", REPO)
    assert res.status == "success" and res.metadata["attempts"] == 2


async def test_list_ops_resume_from_cursor_and_refuse_other_origins() -> None:
    rec = Recorder(lambda r: json_response([{"number": 1}]))
    mod = GitHubConnector(transport=rec.transport)
    good = "https://api.github.com/repos/acme/widgets/issues?page=2"
    res = await invoke(mod, "list_issues", {**REPO, "cursor": good})
    assert res.status == "success" and str(rec.last.url) == good
    n = len(rec.requests)
    for bad in (
        "https://evil.example/repos/acme/widgets/issues?page=2",
        "https://api.github.com/repos/other/widgets/issues?page=2",
        "/repos/acme/widgets/issues?page=2",
    ):
        res = await invoke(mod, "list_issues", {**REPO, "cursor": bad})
        assert res.status == "failure", bad
    assert len(rec.requests) == n


async def test_cursor_and_legacy_alias_must_agree() -> None:
    from nexura_connectors import NotionConnector

    rec = Recorder(lambda r: json_response({"results": [], "has_more": False}))
    mod = NotionConnector(transport=rec.transport)
    res = await invoke(
        mod, "query_database", {"database_id": "db1", "cursor": "a", "start_cursor": "b"}
    )
    assert res.error_code == "INVALID_PARAMS" and rec.requests == []
    res = await invoke(mod, "query_database", {"database_id": "db1", "cursor": "a"})
    assert res.status == "success" and rec.body()["start_cursor"] == "a"
