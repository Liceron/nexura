"""Airtable connector tests (httpx.MockTransport; no network)."""

from __future__ import annotations

from connector_helpers import Negatives, Recorder, Spec, invoke, json_response
from nexura_connectors import AirtableConnector

T = {"base_id": "appX1", "table": "Tasks"}
SPEC = Spec(
    factory=lambda t, s: AirtableConnector(transport=t, sleep=s),
    read_op="get_record",
    read_params={**T, "record_id": "rec1"},
    read_body={"id": "rec1"},
    write_op="create_records",
    write_params={**T, "records": [{"fields": {"Name": "a"}}]},
    write_body={"records": [{"id": "rec9"}]},
)


async def test_negative_contract(neg_case: str, negatives: Negatives) -> None:
    await negatives.check(neg_case, SPEC)


async def test_list_records_offset_pagination_capped() -> None:
    rec = Recorder(lambda r: json_response({"records": [{"id": "r"}], "offset": "off"}))
    res = await invoke(
        AirtableConnector(transport=rec.transport),
        "list_records",
        {**T, "page_size": 10, "filter_by_formula": "{A}=1"},
        cfg={"max_pages": 2},
    )
    assert len(res.outputs["data"]) == 2 and res.outputs["truncated"] is True
    assert rec.requests[1].url.params["offset"] == "off"
    assert rec.requests[0].url.params["filterByFormula"] == "{A}=1"


async def test_writes_limited_to_ten_records_and_need_approval() -> None:
    rec = Recorder(lambda r: json_response({"records": [{"id": "a"}]}))
    mod = AirtableConnector(transport=rec.transport)
    big = {**T, "records": [{"fields": {}}] * 11}
    assert (await invoke(mod, "create_records", big, approved=True)).error_code == "INVALID_PARAMS"
    ok = {**T, "records": [{"id": "rec1", "fields": {"Name": "b"}}]}
    assert (await invoke(mod, "update_records", ok)).error_code == "APPROVAL_REQUIRED"
    res = await invoke(mod, "update_records", ok, approved=True)
    assert res.outputs["data"] == ["a"] and rec.last.method == "PATCH"


async def test_table_name_cannot_escape_path() -> None:
    rec = Recorder(lambda r: json_response({"records": []}))
    res = await invoke(
        AirtableConnector(transport=rec.transport),
        "list_records",
        {"base_id": "app", "table": "../meta"},
    )
    assert res.status == "failure" and rec.requests == []


async def test_request_side_record_payload_is_capped() -> None:
    rec = Recorder(lambda r: json_response({"records": [{"id": "a"}]}))
    mod = AirtableConnector(transport=rec.transport)
    big = {**T, "records": [{"fields": {"blob": "x" * 300_000}}]}
    res = await invoke(mod, "create_records", big, approved=True)
    assert res.error_code == "INVALID_PARAMS"
    upd = {**T, "records": [{"id": "rec1", "fields": {"blob": "x" * 300_000}}]}
    assert (await invoke(mod, "update_records", upd, approved=True)).error_code == "INVALID_PARAMS"
    assert not rec.requests
