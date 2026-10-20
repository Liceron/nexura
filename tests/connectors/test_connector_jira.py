"""Jira connector tests (httpx.MockTransport; no network)."""

from __future__ import annotations

import base64

from connector_helpers import TOKEN, Negatives, Recorder, Spec, invoke, json_response
from nexura_connectors import JiraConnector

BASE = {"base_url": "https://acme.atlassian.net"}
# the site host is allowed by the operator (constructor), never by flow config
OPERATOR_HOSTS = ["acme.atlassian.net"]
SECRET = "me@acme.com:" + TOKEN
SPEC = Spec(
    factory=lambda t, s: JiraConnector(allowed_hosts=OPERATOR_HOSTS, transport=t, sleep=s),
    read_op="get_issue",
    read_params={"key": "ABC-1"},
    read_body={"key": "ABC-1"},
    write_op="create_issue",
    write_params={"project_key": "ABC", "summary": "S"},
    write_body={"id": "1", "key": "ABC-2"},
    cfg=BASE,
)


async def test_negative_contract(neg_case: str, negatives: Negatives) -> None:
    await negatives.check(neg_case, SPEC)


async def test_base_url_is_required() -> None:
    rec = Recorder(lambda r: json_response({}))
    res = await invoke(
        JiraConnector(allowed_hosts=OPERATOR_HOSTS, transport=rec.transport),
        "get_issue",
        {"key": "ABC-1"},
    )
    assert res.error_code == "INVALID_CONFIG" and rec.requests == []


async def test_basic_auth_and_search_pagination() -> None:
    rec = Recorder(lambda r: json_response({"issues": [{"key": "A-1"}] * 2, "total": 10}))
    res = await invoke(
        JiraConnector(allowed_hosts=OPERATOR_HOSTS, transport=rec.transport),
        "search",
        {"jql": "project = ABC", "max_results": 2},
        cfg=BASE,
        secrets={"cred": SECRET},
    )
    assert res.outputs["truncated"] is True and res.outputs["next_cursor"] == "2"
    expected = "Basic " + base64.b64encode(SECRET.encode()).decode()
    assert rec.last.headers["authorization"] == expected
    assert rec.last.url.params["jql"] == "project = ABC"


async def test_credential_parts_and_base64_never_leak_in_errors() -> None:
    import httpx

    blob = base64.b64encode(SECRET.encode()).decode()
    rec = Recorder(lambda r: httpx.Response(400, text=f"{blob} {TOKEN} me@acme.com"))
    res = await invoke(
        JiraConnector(allowed_hosts=OPERATOR_HOSTS, transport=rec.transport),
        "get_issue",
        {"key": "ABC-1"},
        cfg=BASE,
        secrets={"cred": SECRET},
    )
    assert TOKEN not in (res.error_message or "") and blob not in (res.error_message or "")


async def test_issue_key_pattern_blocks_path_injection() -> None:
    rec = Recorder(lambda r: json_response({}))
    for key in ("../../x", "abc-1", "ABC-1/comment", "ABC-1?x"):
        res = await invoke(
            JiraConnector(allowed_hosts=OPERATOR_HOSTS, transport=rec.transport),
            "get_issue",
            {"key": key},
            cfg=BASE,
        )
        assert res.error_code == "INVALID_PARAMS", key
    assert rec.requests == []


async def test_create_issue_and_comment_use_adf() -> None:
    rec = Recorder(lambda r: json_response({"id": "1", "key": "ABC-2"}, 201))
    mod = JiraConnector(allowed_hosts=OPERATOR_HOSTS, transport=rec.transport)
    await invoke(
        mod,
        "create_issue",
        {"project_key": "ABC", "summary": "S", "description": "D"},
        cfg=BASE,
        approved=True,
    )
    assert rec.body()["fields"]["description"]["type"] == "doc"
    res = await invoke(mod, "add_comment", {"key": "ABC-2", "body": "hi"}, cfg=BASE, approved=True)
    assert res.status == "success" and str(rec.last.url).endswith("/issue/ABC-2/comment")


async def test_private_site_url_needs_explicit_allow_list_entry() -> None:
    rec = Recorder(lambda r: json_response({"key": "ABC-1"}))
    mod = JiraConnector(allowed_hosts=OPERATOR_HOSTS, transport=rec.transport)
    cfg = {"base_url": "https://10.1.2.3"}
    res = await invoke(mod, "get_issue", {"key": "ABC-1"}, cfg=cfg)
    assert res.error_code == "SSRF_BLOCKED"
