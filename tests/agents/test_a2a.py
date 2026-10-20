"""A2A (Agent2Agent) server and client, connected in-process with ASGI transports."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from nexura_agents import (
    A2AClient,
    A2AServer,
    Agent,
    FakeModel,
    SequentialAgent,
    ToolCall,
    remote_agent_tool,
    tool,
)
from nexura_agents.a2a import protocol as p
from nexura_agents.a2a.protocol import A2AError
from nexura_agents.errors import SDKValidationError
from pydantic import BaseModel

URL = "http://a2a.test"
TOKEN = "s3cret"


def agent(*responses: Any, name: str = "Support", **kwargs: Any) -> Agent:
    return Agent(
        name=name,
        description="Answers questions about orders.",
        instructions="Help.",
        model=FakeModel(responses=list(responses)),
        **kwargs,
    )


def call(name: str, call_id: str = "c1", **arguments: Any) -> list[ToolCall]:
    return [ToolCall(id=call_id, name=name, arguments=arguments)]


def serve(
    target: Any, *, token: str | None = TOKEN, version: str = "1.0.0", **app_options: Any
) -> tuple[Any, A2AClient]:
    server = A2AServer(target, url=URL + "/", version=version)
    app = server.http_app(token=token, **app_options)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    client = A2AClient(URL, headers=headers, transport=httpx.ASGITransport(app=app))
    return app, client


@asynccontextmanager
async def raw(app: Any, **headers: str | bytes) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=URL, headers=headers
    ) as http:
        yield http


def request(method: str, params: Any, request_id: Any = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def user_message(text: str, **extra: Any) -> dict[str, Any]:
    return {
        "messageId": "m1",
        "role": "ROLE_USER",
        "parts": [{"text": text}],
        **extra,
    }


AUTH = {"Authorization": f"Bearer {TOKEN}"}


# ------------------------------------------------------------------ conformance
# Required fields below come from the official a2a-sdk 1.2.2 proto messages (field_behavior
# REQUIRED in src/a2a/types/a2a_pb2.py, names as in a2a_pb2.pyi, JSON in camelCase).

CARD_REQUIRED = [  # AgentCard
    "name",
    "description",
    "supportedInterfaces",
    "version",
    "capabilities",
    "defaultInputModes",
    "defaultOutputModes",
    "skills",
]
INTERFACE_REQUIRED = ["url", "protocolBinding", "protocolVersion"]  # AgentInterface
SKILL_REQUIRED = ["id", "name", "description", "tags"]  # AgentSkill
TASK_REQUIRED = ["id", "status"]  # Task
STATUS_REQUIRED = ["state"]  # TaskStatus
MESSAGE_REQUIRED = ["messageId", "role", "parts"]  # Message
ARTIFACT_REQUIRED = ["artifactId", "parts"]  # Artifact
STATUS_EVENT_REQUIRED = ["taskId", "contextId", "status"]  # TaskStatusUpdateEvent
ARTIFACT_EVENT_REQUIRED = ["taskId", "contextId", "artifact"]  # TaskArtifactUpdateEvent
# TaskState enum names (a2a_pb2.pyi); proto3 JSON serializes enums by name.
TASK_STATES = {
    "TASK_STATE_SUBMITTED",
    "TASK_STATE_WORKING",
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_INPUT_REQUIRED",
    "TASK_STATE_REJECTED",
    "TASK_STATE_AUTH_REQUIRED",
}
# JSON_RPC_ERROR_CODE_MAP in a2a/utils/errors.py.
ERROR_CODES = {
    "TaskNotFound": -32001,
    "TaskNotCancelable": -32002,
    "PushNotificationNotSupported": -32003,
    "UnsupportedOperation": -32004,
    "ContentTypeNotSupported": -32005,
    "InvalidAgentResponse": -32006,
    "ExtendedAgentCardNotConfigured": -32007,
    "ExtensionSupportRequired": -32008,
    "VersionNotSupported": -32009,
    "InvalidParams": -32602,
    "InvalidRequest": -32600,
    "MethodNotFound": -32601,
    "InternalError": -32603,
    "JSONParse": -32700,
}


def has(obj: dict[str, Any], required: list[str]) -> None:
    missing = [f for f in required if f not in obj]
    assert not missing, f"missing required fields {missing} in {obj}"


def check_task(task: dict[str, Any]) -> None:
    has(task, TASK_REQUIRED)
    has(task["status"], STATUS_REQUIRED)
    assert task["status"]["state"] in TASK_STATES
    for artifact in task.get("artifacts", []):
        has(artifact, ARTIFACT_REQUIRED)
    for message in task.get("history", []):
        has(message, MESSAGE_REQUIRED)
        assert message["role"] in ("ROLE_USER", "ROLE_AGENT")


def test_constants_match_the_reference_sdk() -> None:
    assert p.PROTOCOL_VERSION == "1.0"  # a2a/utils/constants.py PROTOCOL_VERSION_1_0
    assert p.CARD_PATH == "/.well-known/agent-card.json"  # AGENT_CARD_WELL_KNOWN_PATH
    assert p.VERSION_HEADER == "A2A-Version"
    # JsonRpcDispatcher.METHOD_TO_MODEL and compat/v0_3/jsonrpc_adapter.py method names.
    assert (p.SEND, p.STREAM, p.GET_TASK, p.CANCEL_TASK) == (
        "SendMessage",
        "SendStreamingMessage",
        "GetTask",
        "CancelTask",
    )
    assert p.METHODS_V03 == {
        "message/send": "SendMessage",
        "message/stream": "SendStreamingMessage",
        "tasks/get": "GetTask",
        "tasks/cancel": "CancelTask",
    }
    assert {
        "TaskNotFound": p.TASK_NOT_FOUND,
        "TaskNotCancelable": p.TASK_NOT_CANCELABLE,
        "PushNotificationNotSupported": p.PUSH_NOT_SUPPORTED,
        "UnsupportedOperation": p.UNSUPPORTED_OPERATION,
        "ContentTypeNotSupported": p.CONTENT_TYPE_NOT_SUPPORTED,
        "InvalidAgentResponse": p.INVALID_AGENT_RESPONSE,
        "ExtendedAgentCardNotConfigured": p.EXTENDED_CARD_NOT_CONFIGURED,
        "ExtensionSupportRequired": p.EXTENSION_SUPPORT_REQUIRED,
        "VersionNotSupported": p.VERSION_NOT_SUPPORTED,
        "InvalidParams": p.INVALID_PARAMS,
        "InvalidRequest": p.INVALID_REQUEST,
        "MethodNotFound": p.METHOD_NOT_FOUND,
        "InternalError": p.INTERNAL_ERROR,
        "JSONParse": p.PARSE_ERROR,
    } == ERROR_CODES


async def test_card_has_every_required_field_and_no_secrets() -> None:
    app, _ = serve(agent("x"), version="2.3.4")
    async with raw(app) as http:  # the card is public: no Authorization header
        response = await http.get("/.well-known/agent-card.json")
    assert response.status_code == 200
    card = response.json()
    has(card, CARD_REQUIRED)
    assert card["name"] == "Support"
    assert card["description"] == "Answers questions about orders."
    assert card["version"] == "2.3.4"
    iface = card["supportedInterfaces"][0]
    has(iface, INTERFACE_REQUIRED)
    assert iface == {"url": URL + "/", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
    assert card["capabilities"]["streaming"] is True
    assert card["capabilities"]["pushNotifications"] is False
    has(card["skills"][0], SKILL_REQUIRED)
    # Bearer auth is advertised, the token itself never is.
    assert card["securitySchemes"]["bearer"]["httpAuthSecurityScheme"]["scheme"] == "Bearer"
    assert card["securityRequirements"] == [{"schemes": {"bearer": {"list": []}}}]
    assert TOKEN not in json.dumps(card)


async def test_card_without_token_has_no_security_and_custom_skills() -> None:
    server = A2AServer(
        agent("x"),
        name="Orders",
        description="Order desk",
        url=URL,
        skills=[{"id": "lookup", "name": "Lookup", "tags": ["orders"]}],
    )
    card = server.card()
    has(card, CARD_REQUIRED)
    assert "securitySchemes" not in card and card["name"] == "Orders"
    assert card["skills"] == [
        {"id": "lookup", "name": "Lookup", "tags": ["orders"], "description": "Lookup"}
    ]
    with pytest.raises(SDKValidationError):
        A2AServer(agent("x"), url="ftp://nope")
    with pytest.raises(SDKValidationError):
        A2AServer(agent("x"), url=URL, skills=[{"name": "no id"}])


# ------------------------------------------------------------------- send / get


async def test_send_returns_completed_task_with_text_artifact() -> None:
    app, client = serve(agent("Order A1 is shipped."))
    task = await client.send("Where is A1?")
    assert task.state == "completed" and task.is_completed
    assert task.text == "Order A1 is shipped." and task.output == "Order A1 is shipped."
    assert task.id and task.context_id
    check_task(task.raw)
    assert task.raw["artifacts"][0]["parts"] == [{"text": "Order A1 is shipped."}]
    assert task.raw["history"][0]["role"] == "ROLE_USER"

    fetched = await client.get_task(task.id)
    assert fetched.text == task.text and fetched.state == "completed"
    short = await client.get_task(task.id, history_length=0)
    assert "history" not in short.raw


async def test_raw_send_message_payload_shape() -> None:
    app, _ = serve(agent("hi"))
    async with raw(app, **AUTH) as http:
        response = await http.post("/", json=request("SendMessage", {"message": user_message("a")}))
    body = response.json()
    assert body["jsonrpc"] == "2.0" and body["id"] == 1
    task = body["result"]["task"]  # SendMessageResponse oneof payload
    check_task(task)
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert len(task["id"]) >= 20  # unguessable


class Verdict(BaseModel):
    ok: bool
    reason: str


async def test_output_type_is_a_data_part() -> None:
    structured = agent('{"ok": true, "reason": "fine"}', output_type=Verdict)
    _, client = serve(structured)
    task = await client.send("check")
    assert task.data == {"ok": True, "reason": "fine"}
    assert task.output == {"ok": True, "reason": "fine"} and task.text == ""
    assert task.raw["artifacts"][0]["parts"] == [{"data": {"ok": True, "reason": "fine"}}]


async def test_data_and_file_parts_reach_the_agent() -> None:
    model = FakeModel(responses=["seen"])
    bot = Agent(name="Bot", instructions="", model=model)
    _, client = serve(bot)
    png = base64.b64encode(b"\x89PNG-bytes").decode()
    task = await client.send(
        [
            "Describe",
            {"data": {"order": "A1"}},
            {"raw": png, "mediaType": "image/png", "filename": "a.png"},
            {"url": "https://files.example.com/b.pdf", "mediaType": "application/pdf"},
        ]
    )
    assert task.state == "completed"
    user = [m for m in model.calls[0]["messages"] if m.role == "user"][0]
    assert user.content == 'Describe\n\n{"order": "A1"}'
    kinds = [(a.mime_type, a.name, a.url is not None) for a in user.attachments or []]
    assert kinds == [("image/png", "a.png", False), ("application/pdf", "b.pdf", True)]
    # Inline bytes are not kept in the task history.
    assert "raw" not in json.dumps(task.raw["history"])


async def test_failed_run_reports_failed_without_internal_details() -> None:
    @tool
    def boom() -> str:
        """Always fails."""
        raise RuntimeError("secret-path /etc/shadow")

    broken = agent(call("boom"), "unused", tools=[boom])
    broken.model = FakeModel(handler=lambda m, t: (_ for _ in ()).throw(RuntimeError("db://pw")))
    _, client = serve(broken)
    task = await client.send("go")
    assert task.state == "failed"
    assert task.status_text == "The agent run failed."
    assert "db://pw" not in json.dumps(task.raw)


# -------------------------------------------------------------------- streaming


async def test_streaming_event_order() -> None:
    _, client = serve(agent("done"))
    events = [e async for e in client.stream("go")]
    assert [(e.kind, e.state) for e in events] == [
        ("task", "submitted"),
        ("status", "working"),
        ("artifact", None),
        ("status", "completed"),
    ]
    assert events[2].text == "done"
    assert len({e.task_id for e in events}) == 1
    for event in events:
        if event.kind == "task":
            check_task(event.raw["task"])
        elif event.kind == "status":
            has(event.raw["statusUpdate"], STATUS_EVENT_REQUIRED)
        else:
            has(event.raw["artifactUpdate"], ARTIFACT_EVENT_REQUIRED)
            assert event.raw["artifactUpdate"]["lastChunk"] is True


async def test_streaming_raw_sse_framing() -> None:
    app, _ = serve(agent("done"))
    async with raw(app, **AUTH) as http:
        response = await http.post(
            "/", json=request("SendStreamingMessage", {"message": user_message("a")}, "s1")
        )
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = [f for f in response.text.split("\n\n") if f]
    assert all(f.startswith("data: ") for f in frames) and len(frames) == 4
    first = json.loads(frames[0][6:])
    assert first["id"] == "s1" and first["result"]["task"]["status"]["state"] == (
        "TASK_STATE_SUBMITTED"
    )


async def test_streaming_validation_error_is_a_plain_json_rpc_error() -> None:
    _, client = serve(agent("x"))
    with pytest.raises(A2AError) as exc:
        async for _ in client.stream("hi", task_id="nope"):
            pass
    assert exc.value.code == p.TASK_NOT_FOUND


# ------------------------------------------------------------- cancel and errors


async def test_cancel_stops_an_inflight_run() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    @tool
    async def slow() -> str:
        """Takes forever."""
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "late"

    _, client = serve(agent(call("slow"), "late", tools=[slow]))
    sent = await client.send("go", return_immediately=True)
    assert sent.state in ("submitted", "working")
    await asyncio.wait_for(started.wait(), 5)
    canceled = await client.cancel(sent.id or "")
    assert canceled.state == "canceled"
    await asyncio.wait_for(cancelled.wait(), 5)
    assert (await client.get_task(sent.id or "")).state == "canceled"
    with pytest.raises(A2AError) as exc:
        await client.cancel(sent.id or "")
    assert exc.value.code == p.TASK_NOT_CANCELABLE


async def test_get_and_cancel_unknown_task() -> None:
    _, client = serve(agent("x"))
    for operation in (client.get_task, client.cancel):
        with pytest.raises(A2AError) as exc:
            await operation("missing")
        assert exc.value.code == p.TASK_NOT_FOUND


async def rpc_error(app: Any, payload: Any, **headers: str) -> tuple[int, dict[str, Any]]:
    async with raw(app, **{**AUTH, **headers}) as http:
        response = await http.post("/", **payload)
    return response.status_code, response.json()


async def test_jsonrpc_error_codes() -> None:
    app, _ = serve(agent("x"))
    status, body = await rpc_error(app, {"json": request("Nope", {})})
    assert (status, body["error"]["code"], body["id"]) == (200, -32601, 1)
    _, body = await rpc_error(app, {"content": b"{not json"})
    assert body["error"]["code"] == -32700 and body["id"] is None
    _, body = await rpc_error(app, {"json": {"jsonrpc": "1.0", "id": 1, "method": "GetTask"}})
    assert body["error"]["code"] == -32600
    _, body = await rpc_error(app, {"json": [request("GetTask", {})]})
    assert body["error"]["code"] == -32600
    _, body = await rpc_error(app, {"json": {"jsonrpc": "2.0", "method": "GetTask"}})
    assert body["error"]["code"] == -32600  # no id
    for params in (
        {},  # no message
        {"message": "text"},
        {"message": {**user_message("a"), "role": "ROLE_AGENT"}},
        {"message": {**user_message("a"), "parts": []}},
        {"message": {**user_message("a"), "parts": [{"text": "a", "data": {}}]}},
        {"message": {**user_message("a"), "parts": [{"raw": "!!!notbase64"}]}},
        {"message": {"role": "ROLE_USER", "parts": [{"text": "a"}]}},  # no messageId
        {"message": user_message("a", taskId="bad id with spaces")},
    ):
        _, body = await rpc_error(app, {"json": request("SendMessage", params)})
        assert body["error"]["code"] == -32602, params
    _, body = await rpc_error(app, {"json": request("GetTask", {"id": 5})})
    assert body["error"]["code"] == -32602
    _, body = await rpc_error(app, {"json": request("GetTask", {"id": "x", "historyLength": -1})})
    assert body["error"]["code"] == -32602
    _, body = await rpc_error(app, {"json": request("GetTask", [1])})
    assert body["error"]["code"] == -32600
    # Known A2A features this server does not offer have their own codes.
    for method, code in [
        ("CreateTaskPushNotificationConfig", -32003),
        ("ListTasks", -32004),
        ("SubscribeToTask", -32004),
        ("GetExtendedAgentCard", -32007),
    ]:
        _, body = await rpc_error(app, {"json": request(method, {})})
        assert body["error"]["code"] == code, method
    _, body = await rpc_error(
        app, {"json": request("GetTask", {"id": "x"})}, **{"A2A-Version": "9.0"}
    )
    assert body["error"]["code"] == -32009
    _, body = await rpc_error(
        app, {"json": request("GetTask", {"id": "x"})}, **{"A2A-Version": "1.0"}
    )
    assert body["error"]["code"] == -32001


async def test_internal_errors_hide_details(monkeypatch: pytest.MonkeyPatch) -> None:
    server = A2AServer(agent("x"), url=URL)

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("/home/secret/path")

    monkeypatch.setattr(server, "_get_task", explode)
    app = server.http_app()
    async with raw(app) as http:
        response = await http.post("/", json=request("GetTask", {"id": "x"}))
    assert response.json()["error"] == {"code": -32603, "message": "Internal error"}
    assert "secret" not in response.text


async def test_auth_origin_body_limit_and_methods() -> None:
    app, _ = serve(agent("x"), allowed_origins=["https://ok.example"], max_body_bytes=2048)
    payload = request("GetTask", {"id": "x"})
    async with raw(app) as http:
        response = await http.post("/", json=payload)
        assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer"
        assert TOKEN not in response.text
    async with raw(app, Authorization="Bearer wrong") as http:
        assert (await http.post("/", json=payload)).status_code == 401
    async with raw(
        app, Authorization="Bearer s3cret\u00e9".encode("latin-1")
    ) as http:  # non-ASCII: no crash
        assert (await http.post("/", json=payload)).status_code == 401
    async with raw(app, **AUTH, Origin="https://evil.example") as http:
        assert (await http.post("/", json=payload)).status_code == 403
    async with raw(app, **AUTH, Origin="https://ok.example") as http:
        assert (await http.post("/", json=payload)).status_code == 200
        big = request("SendMessage", {"message": user_message("x" * 5000)})
        response = await http.post("/", json=big)
        assert response.status_code == 413 and response.json()["error"]["code"] == -32600
        assert (await http.get("/")).status_code == 405
        assert (await http.post("/elsewhere", json=payload)).status_code == 404
    # Chunked bodies without a Content-Length are bounded too.

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(10):
            yield b" " * 1000

    async with raw(app, **AUTH) as http:
        assert (await http.post("/", content=chunks())).status_code == 413


# --------------------------------------------------------------------- approvals


def approval_agent() -> tuple[Agent, list[str]]:
    sent: list[str] = []

    @tool(requires_approval=True)
    def refund(order_id: str) -> str:
        """Refund an order."""
        sent.append(order_id)
        return f"refunded {order_id}"

    bot = agent(call("refund", "r1", order_id="A1"), "Refund done.", name="Refunds", tools=[refund])
    return bot, sent


async def test_approval_round_trip_with_digest_binding() -> None:
    bot, sent = approval_agent()
    app, client = serve(bot)
    task = await client.send("refund A1")
    assert task.state == "input-required" and task.is_input_required
    assert sent == []
    (pending,) = task.pending_approvals
    assert pending["key"] == "r1" and pending["tool"] == "refund"
    assert pending["arguments"] == {"order_id": "A1"} and len(pending["digest"]) == 64
    assert "refund" in task.status_text and "r1" in task.status_text
    check_task(task.raw)

    # A wrong digest is refused and the task stays open.
    with pytest.raises(A2AError) as exc:
        await client.decide(task.id or "", approve={"r1": "0" * 64})
    assert exc.value.code == p.INVALID_PARAMS and "does not match" in exc.value.message
    # So is a partial or unknown decision.
    for decision in ({"approve": ["zzz"]}, {"approve": []}, {"approve": "yes"}):
        with pytest.raises(A2AError) as exc:
            await client.send([{"data": decision}], task_id=task.id)
        assert exc.value.code == p.INVALID_PARAMS
    with pytest.raises(A2AError):  # plain text is not a decision
        await client.send("yes please", task_id=task.id)
    assert (await client.get_task(task.id or "")).state == "input-required"
    assert sent == []

    done = await client.decide(task.id or "", approve={"r1": pending["digest"]})
    assert done.id == task.id and done.state == "completed"
    assert done.text == "Refund done." and sent == ["A1"]
    with pytest.raises(A2AError) as exc:  # a finished task takes no more messages
        await client.decide(task.id or "", approve=True)
    assert exc.value.code == p.INVALID_PARAMS


async def test_approve_true_and_reject() -> None:
    bot, sent = approval_agent()
    _, client = serve(bot)
    task = await client.send("refund A1")
    done = await client.decide(task.id or "", approve=True)
    assert done.state == "completed" and sent == ["A1"]

    bot2, sent2 = approval_agent()
    _, client2 = serve(bot2)
    task = await client2.send("refund A1")
    done = await client2.decide(task.id or "", reject={"r1": "not allowed"})
    assert done.state == "completed" and sent2 == []
    tool_msgs = [m for m in bot2.model.calls[-1]["messages"] if m.role == "tool"]  # type: ignore[attr-defined]
    assert "not allowed" in (tool_msgs[0].content or "")


async def test_approval_over_streaming_and_cancel_while_paused() -> None:
    bot, sent = approval_agent()
    _, client = serve(bot)
    events = [e async for e in client.stream("refund A1")]
    assert events[-1].is_input_required and events[-1].kind == "status"
    task_id = events[0].task_id or ""
    resumed = [e async for e in client.stream([{"data": {"approve": True}}], task_id=task_id)]
    assert [(e.kind, e.state) for e in resumed] == [
        ("task", "working"),
        ("artifact", None),
        ("status", "completed"),
    ]
    assert sent == ["A1"]

    bot2, sent2 = approval_agent()
    _, client2 = serve(bot2)
    paused = await client2.send("refund A1")
    assert (await client2.cancel(paused.id or "")).state == "canceled"
    assert sent2 == []


async def test_team_approval_uses_path_keys() -> None:
    bot, sent = approval_agent()
    team = SequentialAgent("Desk", [bot], description="Refund desk")
    _, client = serve(team)
    task = await client.send("refund A1")
    assert task.state == "input-required"
    (pending,) = task.pending_approvals
    assert pending["key"] == "0:r1"
    done = await client.decide(task.id or "", approve={pending["key"]: pending["digest"]})
    assert done.state == "completed" and sent == ["A1"]


# ----------------------------------------------------------- contexts and teams


async def test_follow_up_in_same_context_keeps_history() -> None:
    model = FakeModel(responses=["Your name is Ada.", "You told me: Ada."])
    bot = Agent(name="Bot", instructions="Chat.", model=model)
    _, client = serve(bot)
    first = await client.send("My name is Ada.")
    second = await client.send("What did I say?", context_id=first.context_id)
    assert second.context_id == first.context_id and second.id != first.id
    seen = [m.content for m in model.calls[1]["messages"]]
    assert "My name is Ada." in seen and "Your name is Ada." in seen
    other = await client.send("Hello?")  # a different context starts clean
    assert other.context_id != first.context_id
    assert "My name is Ada." not in [m.content for m in model.calls[2]["messages"]]


async def test_team_served_over_a2a_and_context_history() -> None:
    first_model = FakeModel(responses=["draft one", "draft two"])
    writer = Agent(name="Writer", instructions="Write.", model=first_model)
    editor = Agent(
        name="Editor", instructions="Edit.", model=FakeModel(responses=["edit 1", "edit 2"])
    )
    team = SequentialAgent("Pipeline", [writer, editor], description="Write then edit.")
    server = A2AServer(team, url=URL)
    assert server.card()["description"] == "Write then edit."
    assert server.card()["skills"][0]["tags"] == ["team"]
    app = server.http_app()
    client = A2AClient(URL, transport=httpx.ASGITransport(app=app))
    one = await client.send("topic")
    assert one.state == "completed" and one.text == "edit 1"
    two = await client.send("again", context_id=one.context_id)
    assert two.text == "edit 2"
    seen = [m.content for m in first_model.calls[1]["messages"]]
    assert "topic" in seen and "edit 1" in seen  # the team saw the earlier turn


# ------------------------------------------------------------------ task bounds


async def test_task_store_is_bounded_lru_and_ttl() -> None:
    _, client = serve(
        agent("a", "b", "c", "d"),
        max_tasks=2,
    )
    a = await client.send("1")
    b = await client.send("2")
    await client.get_task(a.id or "")  # a is now more recent than b
    c = await client.send("3")
    assert (await client.get_task(a.id or "")).state == "completed"
    with pytest.raises(A2AError) as exc:
        await client.get_task(b.id or "")
    assert exc.value.code == p.TASK_NOT_FOUND
    assert (await client.get_task(c.id or "")).state == "completed"

    _, short = serve(agent("a"), task_ttl_seconds=0.05)
    task = await short.send("1")
    await asyncio.sleep(0.15)
    with pytest.raises(A2AError):
        await short.get_task(task.id or "")


async def test_running_tasks_are_not_evicted_and_new_ones_are_refused() -> None:
    gate = asyncio.Event()

    @tool
    async def hold() -> str:
        """Waits."""
        await gate.wait()
        return "ok"

    _, client = serve(agent(call("hold"), "done", tools=[hold]), max_tasks=1)
    running = await client.send("go", return_immediately=True)
    with pytest.raises(A2AError) as exc:
        await client.send("another", return_immediately=True)
    assert "busy" in exc.value.message.lower()
    gate.set()
    for _ in range(100):
        if (await client.get_task(running.id or "")).state == "completed":
            break
        await asyncio.sleep(0.02)
    assert (await client.get_task(running.id or "")).state == "completed"


# ------------------------------------------------------------------- 0.3 names


async def test_v03_method_names_and_shapes() -> None:
    app, _ = serve(agent("ok", "ok2"))
    message = {
        "kind": "message",
        "messageId": "m1",
        "role": "user",
        "parts": [
            {"kind": "text", "text": "hi"},
            {"kind": "data", "data": {"a": 1}},
            {
                "kind": "file",
                "file": {"uri": "https://x.example/f.pdf", "mimeType": "application/pdf"},
            },
        ],
    }
    async with raw(app, **AUTH) as http:
        body = (await http.post("/", json=request("message/send", {"message": message}))).json()
        task = body["result"]
        assert task["kind"] == "task" and task["status"]["state"] == "completed"
        assert task["artifacts"][0]["parts"] == [{"kind": "text", "text": "ok"}]
        got = (await http.post("/", json=request("tasks/get", {"id": task["id"]}))).json()
        assert got["result"]["id"] == task["id"] and got["result"]["kind"] == "task"
        gone = (await http.post("/", json=request("tasks/cancel", {"id": task["id"]}))).json()
        assert gone["error"]["code"] == -32002
        stream = await http.post("/", json=request("message/stream", {"message": message}))
    results = [json.loads(f[6:])["result"] for f in stream.text.split("\n\n") if f]
    assert [r["kind"] for r in results] == [
        "task",
        "status-update",
        "artifact-update",
        "status-update",
    ]
    assert [r.get("final") for r in results if r["kind"] == "status-update"] == [False, True]
    bad = dict(message, role="agent")
    async with raw(app, **AUTH) as http:
        error = (await http.post("/", json=request("message/send", {"message": bad}))).json()
    assert error["error"]["code"] == -32602


# ------------------------------------------------------------------ client side


async def test_client_validates_card_and_falls_back_to_v03_card() -> None:
    with pytest.raises(SDKValidationError):
        A2AClient("file:///etc/passwd")

    legacy_card = {
        "name": "Old",
        "description": "d",
        "url": "http://legacy.test/rpc",
        "preferredTransport": "JSONRPC",
        "protocolVersion": "0.3.0",
        "skills": [],
    }
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/.well-known/agent-card.json":
            return httpx.Response(200, json=legacy_card)
        seen.append(json.loads(req.content))
        assert req.headers["a2a-version"] == "0.3"
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": seen[-1]["id"],
                "result": {
                    "kind": "task",
                    "id": "t1",
                    "contextId": "c1",
                    "status": {"state": "completed"},
                    "artifacts": [
                        {"artifactId": "a", "parts": [{"kind": "text", "text": "old ok"}]}
                    ],
                },
            },
        )

    client = A2AClient("http://legacy.test", transport=httpx.MockTransport(handler))
    task = await client.send("hi")
    assert task.text == "old ok" and task.state == "completed"
    assert seen[0]["method"] == "message/send"
    assert seen[0]["params"]["message"]["parts"] == [{"kind": "text", "text": "hi"}]
    assert seen[0]["params"]["message"]["role"] == "user"


async def test_client_accepts_message_replies_and_rejects_garbage() -> None:
    card = {
        "name": "Chatty",
        "supportedInterfaces": [
            {"url": "http://chat.test/", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ],
    }
    replies: list[httpx.Response] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("agent-card.json"):
            return httpx.Response(200, json=card)
        return replies.pop(0)

    client = A2AClient("http://chat.test", transport=httpx.MockTransport(handler))
    message = {
        "messageId": "m",
        "role": "ROLE_AGENT",
        "parts": [{"text": "direct"}, {"data": {"k": 1}}],
    }
    replies.append(
        httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"message": message}})
    )
    reply = await client.send("hi")
    assert reply.id is None and reply.text == "direct" and reply.data == {"k": 1}
    replies.append(httpx.Response(200, text="<html>nope</html>"))
    with pytest.raises(A2AError) as exc:
        await client.send("hi")
    assert exc.value.code == p.INVALID_AGENT_RESPONSE
    replies.append(httpx.Response(500, text="boom"))
    with pytest.raises(A2AError):
        await client.send("hi")
    replies.append(
        httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"task": {"id": 3}}})
    )
    with pytest.raises(A2AError):
        await client.send("hi")


async def test_client_refuses_a_card_pointing_at_another_host_and_blocked_addresses() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "name": "Evil",
                "supportedInterfaces": [
                    {
                        "url": "http://169.254.169.254/latest",
                        "protocolBinding": "JSONRPC",
                        "protocolVersion": "1.0",
                    }
                ],
            },
        )

    client = A2AClient("http://evil.test", transport=httpx.MockTransport(handler))
    with pytest.raises(A2AError, match="allowed_hosts"):
        await client.send("hi")

    # The operator can allow a specific other host explicitly.
    from nexura_agents.network import DestinationPolicy

    allowed = A2AClient(
        "http://evil.test",
        policy=DestinationPolicy(allowed_hosts=["169.254.169.254"]),
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
    )
    allowed._endpoint = None
    assert allowed.policy.allowed_hosts == ["169.254.169.254", "evil.test"]

    # Redirects are never followed, so a 302 cannot bounce a call to an internal service.
    def redirect(req: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1:6379/"})

    bouncer = A2AClient("http://bounce.test", transport=httpx.MockTransport(redirect))
    with pytest.raises(A2AError, match="HTTP 302"):
        await bouncer.get_card()


# ------------------------------------------------------------ remote_agent_tool


async def test_remote_agent_tool_inside_an_agent() -> None:
    _, remote = serve(agent("Order A1 is shipped."))
    await remote.get_card()
    tool_ = remote_agent_tool(remote)
    assert tool_.name == "Support"
    assert "Answers questions about orders." in tool_.description
    assert "Skills:" in tool_.description

    caller = Agent(
        name="Concierge",
        instructions="Delegate.",
        model=FakeModel(
            responses=[call(tool_.name, "t1", request="Where is A1?"), "It is shipped."]
        ),
        tools=[tool_],
    )
    result = await caller.run_async("Where is order A1?")
    assert result.output == "It is shipped."
    out = [m for m in result.messages if m.role == "tool"][0].content
    assert out == "Order A1 is shipped."


async def test_remote_agent_tool_returns_structured_output_and_uses_client_options() -> None:
    structured = agent('{"ok": true, "reason": "r"}', output_type=Verdict)
    server = A2AServer(structured, url=URL)
    app = server.http_app()
    tool_ = remote_agent_tool(
        URL, name="verdicts", description="Gives verdicts", transport=httpx.ASGITransport(app=app)
    )
    assert (tool_.name, tool_.description) == ("verdicts", "Gives verdicts")
    assert await tool_.arun(request="check") == {"ok": True, "reason": "r"}
    assert remote_agent_tool(URL, requires_approval=True).requires_approval is True
    assert remote_agent_tool(A2AClient("http://h.test")).description.startswith("Ask the remote")


async def test_remote_input_required_failure_and_unreachable_become_tool_errors() -> None:
    from nexura_agents.errors import ToolError

    bot, sent = approval_agent()
    _, client = serve(bot)
    tool_ = remote_agent_tool(client, name="refunds", description="Refunds")
    with pytest.raises(ToolError, match="needs input or approval") as exc:
        await tool_.arun(request="refund A1")
    assert "r1" in str(exc.value) and sent == []

    failing = agent("x")
    failing.model = FakeModel(handler=lambda m, t: (_ for _ in ()).throw(RuntimeError("x")))
    _, client = serve(failing)
    with pytest.raises(ToolError, match="failed"):
        await remote_agent_tool(client, name="f", description="f").arun(request="go")

    def refuse(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host 10.0.0.9")

    down = A2AClient("http://down.test", transport=httpx.MockTransport(refuse))
    with pytest.raises(ToolError, match="could not be reached"):
        await remote_agent_tool(down, name="d", description="d").arun(request="go")

    _, client = serve(agent("x"), token=TOKEN)
    wrong = A2AClient(URL, headers={"Authorization": "Bearer no"}, transport=client._transport)
    with pytest.raises(ToolError, match="returned an error"):
        await remote_agent_tool(wrong, name="w", description="w").arun(request="go")


def test_exports_are_lazy_and_listed() -> None:
    import subprocess
    import sys

    import nexura_agents

    for name in ("A2AServer", "A2AClient", "remote_agent_tool"):
        assert name in nexura_agents.__all__
    code = (
        "import sys, nexura_agents; "
        "from nexura_agents import Agent; "
        "assert 'nexura_agents.a2a' not in sys.modules; "
        "from nexura_agents import A2AServer; "
        "assert 'nexura_agents.a2a.server' in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


# ------------------------------------------------------------------ review regressions


async def test_deeply_nested_json_body_is_a_parse_error_not_a_crash() -> None:
    app, _ = serve(agent("hi"), token=None)
    async with raw(app) as http:
        response = await http.post("/", content=b"[" * 3_000_000)
    assert response.status_code == 200
    assert response.json()["error"]["code"] in (p.PARSE_ERROR, p.INVALID_REQUEST)


async def test_client_stops_reading_an_oversized_card() -> None:
    from nexura_agents.a2a.client import MAX_RESPONSE_BYTES

    sent = 0

    class Endless(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            nonlocal sent
            for _ in range(256):
                sent += 1024 * 1024
                yield b"x" * (1024 * 1024)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Endless(), headers={"content-type": "application/json"})

    client = A2AClient("http://big.test", transport=httpx.MockTransport(handler))
    with pytest.raises(A2AError):
        await client.get_card()
    assert sent <= MAX_RESPONSE_BYTES + 1024 * 1024


@pytest.mark.parametrize(
    "advertised", ["http://agent.test:6379/rpc", "http://agent.test/rpc", "https://other.test/rpc"]
)
async def test_bearer_token_is_not_sent_to_another_origin(advertised: str) -> None:
    seen: list[tuple[str, str | None]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((str(req.url), req.headers.get("authorization")))
        if req.url.path.endswith("agent-card.json"):
            return httpx.Response(
                200,
                json={
                    "name": "A",
                    "supportedInterfaces": [
                        {"url": advertised, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
                    ],
                },
            )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

    client = A2AClient(
        "https://agent.test",
        headers={"Authorization": "Bearer SECRET"},
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(Exception):  # noqa: B017 - any refusal is fine, only the leak matters
        await client.send("hi")
    assert all(url.endswith("agent-card.json") for url, _ in seen), seen


@pytest.mark.parametrize("version", ["1.0.0", "1.1", "0.3.0"])
async def test_version_header_patch_level_is_accepted(version: str) -> None:
    app, _ = serve(agent("hi"), token=None)
    legacy = version.startswith("0")
    body = request(
        "message/send" if legacy else "SendMessage",
        {
            "message": {"messageId": "m", "role": "user", "parts": [{"kind": "text", "text": "x"}]}
            if legacy
            else user_message("x")
        },
    )
    async with raw(app, **{"A2A-Version": version}) as http:
        response = await http.post("/", json=body)
    assert "error" not in response.json(), response.json()


async def test_v03_blocking_false_returns_immediately() -> None:
    gate = asyncio.Event()

    @tool
    async def slow() -> str:
        """Wait."""
        await gate.wait()
        return "x"

    app, _ = serve(agent(call("slow"), "done", tools=[slow]), token=None)
    body = request(
        "message/send",
        {
            "message": {"messageId": "m", "role": "user", "parts": [{"kind": "text", "text": "x"}]},
            "configuration": {"blocking": False},
        },
    )
    async with raw(app) as http:
        response = await asyncio.wait_for(http.post("/", json=body), 3)
        gate.set()
    assert response.json()["result"]["status"]["state"] in ("submitted", "working")


async def test_concurrent_tasks_in_one_context_keep_both_turns() -> None:
    model = FakeModel(responses=["a1", "a2", "a3"])
    bot = Agent(name="Bot", instructions="x", model=model)
    app, _ = serve(bot, token=None)
    async with raw(app) as http:
        sends = [
            http.post(
                "/",
                json=request(
                    "SendMessage",
                    {"message": {**user_message(text), "messageId": mid, "contextId": "ctx1"}},
                    i,
                ),
            )
            for i, (text, mid) in enumerate([("first", "a"), ("second", "b")], 1)
        ]
        await asyncio.gather(*sends)
        await http.post(
            "/",
            json=request(
                "SendMessage",
                {"message": {**user_message("third"), "messageId": "c", "contextId": "ctx1"}},
                3,
            ),
        )
    seen = [m.content for m in model.calls[2]["messages"]]
    assert "first" in seen and "second" in seen, seen
