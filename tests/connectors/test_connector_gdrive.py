"""Google Drive connector tests (httpx.MockTransport; no network)."""

from __future__ import annotations

import base64

import httpx
from connector_helpers import Negatives, Recorder, Sleeps, Spec, invoke, json_response
from nexura_connectors import GoogleDriveConnector

SPEC = Spec(
    factory=lambda t, s: GoogleDriveConnector(transport=t, sleep=s),
    read_op="list_files",
    read_params={},
    read_body={"files": [{"id": "f1"}]},
    write_op="upload_text",
    write_params={"name": "a.txt", "content": "hi"},
    write_body={"id": "f2", "name": "a.txt"},
)


async def test_negative_contract(neg_case: str, negatives: Negatives) -> None:
    await negatives.check(neg_case, SPEC)


async def test_manifest_marks_reads_and_writes() -> None:
    m = GoogleDriveConnector().manifest
    ops = {e["operation"]: e["write"] for e in (m.display.examples if m.display else [])}
    assert ops == {
        "list_files": False,
        "get_file": False,
        "download_file": False,
        "upload_text": True,
    }
    assert m.required_credential_types == ["google_oauth_token"]


async def test_list_files_paginates_with_cap() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        return json_response({"files": [{"id": "x"}], "nextPageToken": "t"})

    rec = Recorder(handler)
    res = await invoke(
        GoogleDriveConnector(transport=rec.transport, sleep=Sleeps()),
        "list_files",
        {"query": "name contains 'a'"},
        cfg={"max_pages": 2},
    )
    assert res.status == "success"
    assert len(rec.requests) == 2 and res.outputs["truncated"] is True
    assert rec.requests[1].url.params["pageToken"] == "t"
    assert rec.last.headers["authorization"].startswith("Bearer ")


async def test_download_returns_base64_and_obeys_cap() -> None:
    rec = Recorder(lambda r: httpx.Response(200, content=b"hello"))
    res = await invoke(
        GoogleDriveConnector(transport=rec.transport), "download_file", {"file_id": "abc"}
    )
    assert res.outputs["data"]["content_base64"] == base64.b64encode(b"hello").decode()
    assert rec.last.url.params["alt"] == "media"


async def test_file_id_cannot_inject_path() -> None:
    rec = Recorder(lambda r: json_response({}))
    for bad in ("../about", "a/b", "a?x=1"):
        res = await invoke(
            GoogleDriveConnector(transport=rec.transport), "get_file", {"file_id": bad}
        )
        assert res.error_code == "INVALID_PARAMS"
    assert rec.requests == []


async def test_upload_is_multipart_post() -> None:
    rec = Recorder(lambda r: json_response({"id": "n1", "name": "a.txt"}))
    res = await invoke(
        GoogleDriveConnector(transport=rec.transport),
        "upload_text",
        {"name": "a.txt", "content": "body-text", "parent_id": "p1"},
        approved=True,
    )
    assert res.outputs["data"] == {"id": "n1", "name": "a.txt"}
    assert rec.last.method == "POST"
    assert rec.last.headers["content-type"].startswith("multipart/related")
    assert b"body-text" in rec.last.content and b'"parents": ["p1"]' in rec.last.content
