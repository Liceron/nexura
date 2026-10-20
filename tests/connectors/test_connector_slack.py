"""Slack connector tests (httpx.MockTransport; no network)."""

from __future__ import annotations

import httpx
from connector_helpers import Negatives, Recorder, Sleeps, Spec, invoke, json_response
from nexura_connectors import SlackConnector

SPEC = Spec(
    factory=lambda t, s: SlackConnector(transport=t, sleep=s),
    read_op="list_channels",
    read_params={},
    read_body={"ok": True, "channels": [{"id": "C1"}]},
    write_op="post_message",
    write_params={"channel": "C1", "text": "hi"},
    write_body={"ok": True, "channel": "C1", "ts": "1.2"},
)


async def test_negative_contract(neg_case: str, negatives: Negatives) -> None:
    await negatives.check(neg_case, SPEC)


async def test_manifest_marks_reads_and_writes() -> None:
    m = SlackConnector().manifest
    assert m.module_id == "nexura.connectors.slack"
    assert m.capabilities["writes"] and m.capabilities["requires_approval"]
    ops = {e["operation"]: e["write"] for e in (m.display.examples if m.display else [])}
    assert ops == {"post_message": True, "list_channels": False, "read_history": False}
    assert m.effect_policy == "non_idempotent_write"
    assert "credential_ref" in m.config_schema["properties"]


async def test_post_message_sends_bearer_and_body() -> None:
    rec = Recorder(lambda r: json_response({"ok": True, "channel": "C1", "ts": "9.9"}))
    res = await invoke(
        SlackConnector(transport=rec.transport),
        "post_message",
        {"channel": "C1", "text": "hello", "thread_ts": "1.1"},
        approved=True,
    )
    assert res.status == "success", res.error_message
    assert res.outputs["data"] == {"channel": "C1", "ts": "9.9"} and res.outputs["write"] is True
    assert str(rec.last.url) == "https://slack.com/api/chat.postMessage"
    assert rec.last.headers["authorization"].startswith("Bearer tok_")
    assert rec.body() == {"channel": "C1", "text": "hello", "thread_ts": "1.1"}


async def test_slack_ok_false_maps_to_typed_errors() -> None:
    for error, code in [("invalid_auth", "AUTH_FAILED"), ("ratelimited", "RATE_LIMITED")]:
        rec = Recorder(lambda r, e=error: json_response({"ok": False, "error": e}))
        res = await invoke(SlackConnector(transport=rec.transport), "list_channels", {})
        assert res.error_code == code
    rec = Recorder(lambda r: json_response({"ok": False, "error": "channel_not_found"}))
    res = await invoke(SlackConnector(transport=rec.transport), "read_history", {"channel": "C9"})
    assert res.error_code == "UPSTREAM_ERROR" and "channel_not_found" in (res.error_message or "")


async def test_pagination_is_capped_by_max_pages() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        return json_response(
            {"ok": True, "channels": [{"id": "c"}], "response_metadata": {"next_cursor": "more"}}
        )

    rec = Recorder(handler)
    res = await invoke(
        SlackConnector(transport=rec.transport, sleep=Sleeps()),
        "list_channels",
        {"limit": 5},
        cfg={"max_pages": 2},
    )
    assert res.status == "success"
    assert len(rec.requests) == 2 and len(res.outputs["data"]) == 2
    assert res.outputs["truncated"] is True and res.outputs["next_cursor"] == "more"
    assert rec.requests[1].url.params["cursor"] == "more"


async def test_limit_above_hard_cap_is_rejected() -> None:
    rec = Recorder(lambda r: json_response({"ok": True, "channels": []}))
    res = await invoke(SlackConnector(transport=rec.transport), "list_channels", {"limit": 5000})
    assert res.error_code == "INVALID_PARAMS" and rec.requests == []
