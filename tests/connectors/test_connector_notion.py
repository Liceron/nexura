"""Notion connector tests (httpx.MockTransport; no network)."""

from __future__ import annotations

import httpx
from connector_helpers import Negatives, Recorder, Spec, invoke, json_response
from nexura_connectors import NotionConnector

SPEC = Spec(
    factory=lambda t, s: NotionConnector(transport=t, sleep=s),
    read_op="get_page",
    read_params={"page_id": "abc123"},
    read_body={"id": "abc123"},
    write_op="create_page",
    write_params={"parent_database_id": "db1", "properties": {"Name": {"title": []}}},
    write_body={"id": "p1", "url": "https://notion.so/p1"},
)


async def test_negative_contract(neg_case: str, negatives: Negatives) -> None:
    await negatives.check(neg_case, SPEC)


async def test_query_database_is_a_read_and_paginates_with_cap() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        return json_response({"results": [{"id": "r"}], "has_more": True, "next_cursor": "nc"})

    rec = Recorder(handler)
    mod = NotionConnector(transport=rec.transport)
    assert mod.manifest.capabilities["reads"]
    res = await invoke(
        mod, "query_database", {"database_id": "db1", "page_size": 10}, cfg={"max_pages": 2}
    )
    assert res.status == "success" and res.outputs["write"] is False  # no approval needed
    assert len(rec.requests) == 2 and res.outputs["truncated"] is True
    assert rec.requests[0].headers["notion-version"] == "2022-06-28"
    assert rec.body(1)["start_cursor"] == "nc"


async def test_query_database_post_is_retried_because_it_is_a_read() -> None:
    state = {"n": 0}

    def handler(r: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return httpx.Response(502) if state["n"] == 1 else json_response({"results": []})

    rec = Recorder(handler)

    async def nosleep(_: float) -> None:
        return None

    res = await invoke(
        NotionConnector(transport=rec.transport, sleep=nosleep),
        "query_database",
        {"database_id": "d"},
    )
    assert res.status == "success" and len(rec.requests) == 2


async def test_append_blocks_requires_approval_and_sends_patch() -> None:
    rec = Recorder(lambda r: json_response({"results": [{}, {}]}))
    mod = NotionConnector(transport=rec.transport)
    params = {"block_id": "b1", "children": [{"object": "block"}, {"object": "block"}]}
    assert (await invoke(mod, "append_blocks", params)).error_code == "APPROVAL_REQUIRED"
    res = await invoke(mod, "append_blocks", params, approved=True)
    assert res.outputs["data"] == {"count": 2} and rec.last.method == "PATCH"


async def test_oversized_children_list_rejected() -> None:
    rec = Recorder(lambda r: json_response({}))
    res = await invoke(
        NotionConnector(transport=rec.transport),
        "append_blocks",
        {"block_id": "b", "children": [{}] * 101},
        approved=True,
    )
    assert res.error_code == "INVALID_PARAMS" and rec.requests == []
