"""A6 S7: built-in modules default their idempotency key from the execution context.

``nexura.http.request`` sends the workflow step's key on writes and may then retry a POST;
without any key a POST/PATCH retry is still refused. The email and WhatsApp channels use
the step's key as the outbox de-duplication key; connectors send it as
``Idempotency-Key`` on writes (S3, which signs its headers, opts out). Everything runs on
``httpx.MockTransport`` or the dry-run outbox; no network.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.execution import ExecutionId, default_idempotency_key

from nexura_runtime.integrations.channels.module import EmailChannelModule, WhatsAppChannelModule
from nexura_runtime.integrations.http.errors import NonIdempotentRetryForbiddenError
from nexura_runtime.integrations.http.module import MAX_HTTP_RETRIES, HttpRequestModule
from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "connectors"))
from connector_helpers import Recorder, json_response, make_context  # noqa: E402
from nexura_connectors import S3Connector, SlackConnector, WebhookConnector  # noqa: E402

STEP_KEY = "send#3"


def step_ctx(**kwargs: Any) -> ExecutionContext:
    return ExecutionContext(
        run_id="run-s7", node_id="send", approval_granted=True, step_key=STEP_KEY, **kwargs
    )


EXPECTED = default_idempotency_key(ExecutionId("default", "run-s7"), STEP_KEY)


class Flaky:
    """Fails the first ``failures`` calls (an exception or a status), then answers 201."""

    def __init__(self, failures: int, *, status: int | None = None) -> None:
        self.failures = failures
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if len(self.requests) <= self.failures:
            if self.status is None:
                raise httpx.ConnectError("connection reset", request=request)
            return httpx.Response(self.status, headers={"retry-after": "0"})
        return httpx.Response(201, json={"created": True})


async def no_sleep(_: float) -> None:
    return None


# ---------------------------------------------------------------------------- http module


async def test_http_post_retries_with_the_step_key_from_the_context() -> None:
    flaky = Flaky(2)
    mod = HttpRequestModule(transport=httpx.MockTransport(flaky), sleep=no_sleep)
    res = await mod.execute(
        step_ctx(), {"method": "POST", "url": "https://api.example.com/orders", "retries": 3}, {}
    )
    assert res.status == "success" and res.outputs["status_code"] == 201
    assert res.metadata["attempts"] == 3
    assert [r.headers["idempotency-key"] for r in flaky.requests] == [EXPECTED] * 3


async def test_http_retries_a_5xx_answer_and_returns_the_last_one_when_exhausted() -> None:
    flaky = Flaky(5, status=503)
    mod = HttpRequestModule(transport=httpx.MockTransport(flaky), sleep=no_sleep)
    res = await mod.execute(
        step_ctx(), {"method": "POST", "url": "https://api.example.com/x", "retries": 2}, {}
    )
    assert res.outputs["status_code"] == 503 and len(flaky.requests) == 3


async def test_http_transport_error_is_raised_after_the_last_retry() -> None:
    flaky = Flaky(10)
    mod = HttpRequestModule(transport=httpx.MockTransport(flaky), sleep=no_sleep)
    with pytest.raises(httpx.ConnectError):
        await mod.execute(
            step_ctx(), {"method": "PUT", "url": "https://api.example.com/x", "retries": 1}, {}
        )
    assert len(flaky.requests) == 2


async def test_http_post_retry_without_any_key_is_still_refused() -> None:
    ctx = ExecutionContext(run_id="run-s7", node_id="send", approval_granted=True)
    assert ctx.idempotency_key() is None
    flaky = Flaky(0)
    mod = HttpRequestModule(transport=httpx.MockTransport(flaky), sleep=no_sleep)
    for method in ("POST", "PATCH"):
        with pytest.raises(NonIdempotentRetryForbiddenError):
            await mod.execute(
                ctx, {"method": method, "url": "https://api.example.com/x", "retries": 1}, {}
            )
    assert flaky.requests == []  # refused before anything was sent
    # Without retries the call goes out once, with no key header.
    res = await mod.execute(ctx, {"method": "POST", "url": "https://api.example.com/x"}, {})
    assert res.status == "success" and "idempotency-key" not in flaky.requests[0].headers


async def test_http_explicit_keys_win_and_reads_get_no_default_key() -> None:
    flaky = Flaky(0)
    mod = HttpRequestModule(transport=httpx.MockTransport(flaky), sleep=no_sleep)
    await mod.execute(
        step_ctx(),
        {"method": "POST", "url": "https://api.example.com/x", "idempotency_key": "mine"},
        {},
    )
    await mod.execute(
        step_ctx(),
        {
            "method": "POST",
            "url": "https://api.example.com/x",
            "headers": {"Idempotency-Key": "from-header"},
            "retries": 1,
        },
        {},
    )
    await mod.execute(step_ctx(), {"method": "GET", "url": "https://api.example.com/x"}, {})
    keys = [r.headers.get("idempotency-key") for r in flaky.requests]
    assert keys == ["mine", "from-header", None]


async def test_http_retries_are_capped_and_a_success_is_not_repeated() -> None:
    flaky = Flaky(100, status=500)
    mod = HttpRequestModule(transport=httpx.MockTransport(flaky), sleep=no_sleep)
    await mod.execute(
        step_ctx(), {"method": "POST", "url": "https://api.example.com/x", "retries": 99}, {}
    )
    assert len(flaky.requests) == MAX_HTTP_RETRIES + 1
    once = Flaky(0)
    mod = HttpRequestModule(transport=httpx.MockTransport(once), sleep=no_sleep)
    await mod.execute(
        step_ctx(), {"method": "POST", "url": "https://api.example.com/x", "retries": 3}, {}
    )
    assert len(once.requests) == 1


async def test_http_module_in_a_workflow_step_sends_the_journaled_key() -> None:
    flaky = Flaky(1)
    reg = ModuleRegistry()
    reg.register("http", HttpRequestModule(transport=httpx.MockTransport(flaky), sleep=no_sleep))
    spec = WorkflowSpec(
        name="post",
        entry_node="send",
        nodes={
            "send": WorkflowNode(
                id="send",
                handler="http",
                config={"allowed_hosts": ["api.example.com"]},
            )
        },
    )
    store = MemoryWorkflowStore()
    engine = WorkflowEngine(spec, reg, store=store, approval_required_handlers=set())
    paused = await engine.run(
        {"method": "POST", "url": "https://api.example.com/orders", "retries": 1},
        workflow_run_id="w1",
    )
    assert paused.status == "paused"  # a POST is a write: approval first
    done = await engine.resume("w1", paused.pending_approval.approval_id)
    assert done.status == "completed" and done.state["status_code"] == 201
    journal = await store.open_execution_store()
    snap = await journal.get(ExecutionId("default", "w1"))
    expected = snap.steps["send#1"].intent.idempotency_key
    assert [r.headers["idempotency-key"] for r in flaky.requests] == [expected] * 2


# ------------------------------------------------------------------------------- channels


@pytest.mark.parametrize(
    ("module", "inputs"),
    [
        (EmailChannelModule, {"operation": "send_email", "recipient": "a@example.com"}),
        (
            WhatsAppChannelModule,
            {
                "operation": "send_message",
                "recipient": "15551234567",
                "text_content": "hi",
                "consent_verified": True,
                "is_service_window_active": True,
            },
        ),
    ],
)
async def test_channels_use_the_step_key_and_deduplicate_a_repeat(
    module: Any, inputs: dict[str, Any]
) -> None:
    mod = module()
    first = await mod.execute(step_ctx(), dict(inputs), {})
    assert first.status == "success", first.error_message
    assert first.outputs["result"]["message_id"] == EXPECTED
    again = await mod.execute(step_ctx(), dict(inputs), {})
    assert again.outputs["result"]["receipt_id"] == first.outputs["result"]["receipt_id"]
    # Another step (another key) is a different message; an explicit key still wins.
    other = await mod.execute(
        ExecutionContext(run_id="run-s7", node_id="send", approval_granted=True, step_key="send#4"),
        dict(inputs),
        {},
    )
    assert other.outputs["result"]["message_id"] != EXPECTED
    mine = await mod.execute(step_ctx(), {**inputs, "idempotency_key": "mine"}, {})
    assert mine.outputs["result"]["message_id"] == "mine"


# ----------------------------------------------------------------------------- connectors


def connector_ctx(step_key: str | None = STEP_KEY) -> ExecutionContext:
    base = make_context(approval_granted=True)
    return ExecutionContext(
        run_id="run-s7",
        node_id="send",
        credential_resolver=base.credential_resolver,
        approval_granted=True,
        step_key=step_key,
    )


async def test_connector_writes_carry_the_step_key_and_reads_do_not() -> None:
    rec = Recorder(lambda r: json_response({"ok": True, "channel": "C1", "ts": "1.2"}))
    mod = SlackConnector(transport=rec.transport)
    write = {"operation": "post_message", "params": {"channel": "C1", "text": "hi"}}
    res = await mod.execute(connector_ctx(), write, {"credential_ref": "cred"})
    assert res.status == "success", res.error_message
    assert rec.last.headers["idempotency-key"] == EXPECTED
    rec.handler = lambda r: json_response({"ok": True, "channels": []})
    await mod.execute(
        connector_ctx(), {"operation": "list_channels", "params": {}}, {"credential_ref": "cred"}
    )
    assert "idempotency-key" not in rec.last.headers
    # Outside a workflow step there is no key and no header (unchanged behaviour).
    rec.handler = lambda r: json_response({"ok": True, "channel": "C1", "ts": "1.2"})
    await mod.execute(connector_ctx(None), write, {"credential_ref": "cred"})
    assert "idempotency-key" not in rec.last.headers


async def test_webhook_param_key_wins_and_s3_opts_out() -> None:
    rec = Recorder(lambda r: httpx.Response(200))
    mod = WebhookConnector(transport=rec.transport)
    params = {"url": "https://hooks.example.com/in", "payload": {"k": "v"}}
    await mod.execute(connector_ctx(), {"operation": "send", "params": params}, {})
    assert rec.last.headers["idempotency-key"] == EXPECTED
    await mod.execute(
        connector_ctx(),
        {"operation": "send", "params": {**params, "idempotency_key": "abc"}},
        {},
    )
    assert rec.last.headers["idempotency-key"] == "abc"
    assert S3Connector.SENDS_IDEMPOTENCY_KEY is False
    assert S3Connector().build_http({}, "", connector_ctx()).idempotency_key is None
    assert SlackConnector().build_http({}, "", connector_ctx()).idempotency_key == EXPECTED
