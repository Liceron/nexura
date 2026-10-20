"""Google Sheets connector tests (httpx.MockTransport; no network)."""

from __future__ import annotations

from connector_helpers import Negatives, Recorder, Spec, invoke, json_response
from nexura_connectors import GoogleSheetsConnector

SPEC = Spec(
    factory=lambda t, s: GoogleSheetsConnector(transport=t, sleep=s),
    read_op="read_range",
    read_params={"spreadsheet_id": "sheet1", "range": "A1:B2"},
    read_body={"values": [["a", "b"]]},
    write_op="append_rows",
    write_params={"spreadsheet_id": "sheet1", "range": "A1", "values": [["x", 1]]},
    write_body={"updates": {"updatedRange": "A3:B3", "updatedRows": 1}},
)


async def test_negative_contract(neg_case: str, negatives: Negatives) -> None:
    await negatives.check(neg_case, SPEC)


async def test_manifest_marks_reads_and_writes() -> None:
    m = GoogleSheetsConnector().manifest
    ops = {e["operation"]: e["write"] for e in (m.display.examples if m.display else [])}
    assert ops == {"read_range": False, "append_rows": True, "update_range": True}


async def test_read_range_encodes_range() -> None:
    rec = Recorder(lambda r: json_response({"values": [["1"]]}))
    res = await invoke(
        GoogleSheetsConnector(transport=rec.transport),
        "read_range",
        {"spreadsheet_id": "s1", "range": "Sheet 1!A1:B2"},
    )
    assert res.outputs["data"] == [["1"]]
    assert rec.last.method == "GET" and "Sheet%201%21A1%3AB2" in str(rec.last.url)


async def test_append_and_update_bodies() -> None:
    rec = Recorder(lambda r: json_response({"updates": {"updatedRange": "A1", "updatedRows": 2}}))
    mod = GoogleSheetsConnector(transport=rec.transport)
    p = {"spreadsheet_id": "s1", "range": "A1", "values": [["a"], ["b"]]}
    res = await invoke(mod, "append_rows", p, approved=True)
    assert res.outputs["data"]["updated_rows"] == 2
    assert rec.last.method == "POST" and str(rec.last.url).split("?")[0].endswith(":append")
    assert rec.body() == {"values": [["a"], ["b"]]}
    res = await invoke(mod, "update_range", p, approved=True)
    assert res.status == "success" and rec.last.method == "PUT"


async def test_row_cap_and_id_validation() -> None:
    rec = Recorder(lambda r: json_response({}))
    mod = GoogleSheetsConnector(transport=rec.transport)
    rows = [["x"]] * 1001
    res = await invoke(
        mod, "append_rows", {"spreadsheet_id": "s", "range": "A1", "values": rows}, approved=True
    )
    assert res.error_code == "INVALID_PARAMS"
    res = await invoke(mod, "read_range", {"spreadsheet_id": "a/../b", "range": "A1"})
    assert res.error_code == "INVALID_PARAMS" and rec.requests == []
