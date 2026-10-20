# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""A2A wire format: constants, error codes and part/message/task codecs.

Implements A2A protocol version 1.0 (JSON-RPC binding, proto3 JSON field names), plus the
0.3 method names and payload shapes. Field names and error codes were taken from the
official ``a2a-sdk`` 1.2.2 source (``a2a/types/a2a_pb2.pyi``, ``a2a/utils/errors.py``,
``a2a/utils/constants.py``, ``a2a/server/routes/jsonrpc_dispatcher.py`` and
``a2a/compat/v0_3/types.py``). Nothing from that package is imported or copied.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import UTC, datetime
from typing import Any

from nexura_agents.errors import NexuraError

PROTOCOL_VERSION = "1.0"
LEGACY_PROTOCOL_VERSION = "0.3"
SUPPORTED_VERSIONS = (PROTOCOL_VERSION, LEGACY_PROTOCOL_VERSION)
VERSION_HEADER = "A2A-Version"
CARD_PATH = "/.well-known/agent-card.json"
BINDING = "JSONRPC"

# JSON-RPC method names: current (1.0) and the 0.3 equivalents.
SEND = "SendMessage"
STREAM = "SendStreamingMessage"
GET_TASK = "GetTask"
CANCEL_TASK = "CancelTask"
METHODS_V03 = {
    "message/send": SEND,
    "message/stream": STREAM,
    "tasks/get": GET_TASK,
    "tasks/cancel": CANCEL_TASK,
}
# Known A2A methods this implementation answers with a specific "not supported" error.
PUSH_METHODS = {
    "CreateTaskPushNotificationConfig",
    "GetTaskPushNotificationConfig",
    "ListTaskPushNotificationConfigs",
    "DeleteTaskPushNotificationConfig",
    "tasks/pushNotificationConfig/set",
    "tasks/pushNotificationConfig/get",
    "tasks/pushNotificationConfig/list",
    "tasks/pushNotificationConfig/delete",
}
UNSUPPORTED_METHODS = {"ListTasks", "SubscribeToTask", "tasks/resubscribe"}
EXTENDED_CARD_METHODS = {"GetExtendedAgentCard", "agent/getAuthenticatedExtendedCard"}

# JSON-RPC and A2A error codes (a2a/utils/errors.py JSON_RPC_ERROR_CODE_MAP).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002
PUSH_NOT_SUPPORTED = -32003
UNSUPPORTED_OPERATION = -32004
CONTENT_TYPE_NOT_SUPPORTED = -32005
INVALID_AGENT_RESPONSE = -32006
EXTENDED_CARD_NOT_CONFIGURED = -32007
EXTENSION_SUPPORT_REQUIRED = -32008
VERSION_NOT_SUPPORTED = -32009

# Task states (proto enum names, as serialized by proto3 JSON).
SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
COMPLETED = "TASK_STATE_COMPLETED"
FAILED = "TASK_STATE_FAILED"
CANCELED = "TASK_STATE_CANCELED"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
REJECTED = "TASK_STATE_REJECTED"
AUTH_REQUIRED = "TASK_STATE_AUTH_REQUIRED"
TERMINAL_STATES = frozenset({COMPLETED, FAILED, CANCELED, REJECTED})

_STATE_TO_V03 = {
    SUBMITTED: "submitted",
    WORKING: "working",
    COMPLETED: "completed",
    FAILED: "failed",
    CANCELED: "canceled",
    INPUT_REQUIRED: "input-required",
    REJECTED: "rejected",
    AUTH_REQUIRED: "auth-required",
}
_V03_TO_STATE = {v: k for k, v in _STATE_TO_V03.items()}

ROLE_USER = "ROLE_USER"
ROLE_AGENT = "ROLE_AGENT"

_ID = re.compile(r"^[\x21-\x7e]{1,128}$")


class A2AError(NexuraError):
    """A JSON-RPC error from an A2A peer, or one this server answers with."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


def invalid_params(message: str) -> A2AError:
    return A2AError(INVALID_PARAMS, message)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def check_id(value: Any, what: str) -> str:
    """Identifiers are printable ASCII without spaces, at most 128 characters."""
    if not isinstance(value, str) or not _ID.match(value):
        raise invalid_params(f"{what} must be a string of 1-128 printable characters.")
    return value


# --------------------------------------------------------------------- parts


def text_part(text: str) -> dict[str, Any]:
    return {"text": text}


def data_part(value: Any) -> dict[str, Any]:
    return {"data": value}


def _opt(part: dict[str, Any], key: str) -> str | None:
    value = part.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise invalid_params(f"Part field '{key}' must be a string.")
    return value


def parse_part(raw: Any, *, v03: bool) -> dict[str, Any]:
    """Validate one incoming part and return it in the current (1.0) shape."""
    if not isinstance(raw, dict):
        raise invalid_params("Each part must be an object.")
    if v03:
        kind = raw.get("kind")
        if kind == "text":
            raw = {"text": raw.get("text")}
        elif kind == "data":
            raw = {"data": raw.get("data")}
        elif kind == "file" and isinstance(raw.get("file"), dict):
            file = raw["file"]
            common = {"mediaType": file.get("mimeType"), "filename": file.get("name")}
            raw = (
                {"raw": file.get("bytes"), **common}
                if "bytes" in file
                else {"url": file.get("uri"), **common}
            )
        else:
            raise invalid_params("Part kind must be 'text', 'data' or 'file'.")
    present = [k for k in ("text", "raw", "url", "data") if k in raw]
    if len(present) != 1:
        raise invalid_params("A part needs exactly one of text, raw, url or data.")
    key = present[0]
    out: dict[str, Any]
    if key == "text":
        if not isinstance(raw["text"], str):
            raise invalid_params("Part 'text' must be a string.")
        out = {"text": raw["text"]}
    elif key == "data":
        out = {"data": raw["data"]}
    elif key == "raw":
        if not isinstance(raw["raw"], str):
            raise invalid_params("Part 'raw' must be a base64 string.")
        out = {"raw": raw["raw"]}
    else:
        if not isinstance(raw["url"], str) or not raw["url"]:
            raise invalid_params("Part 'url' must be a non-empty string.")
        out = {"url": raw["url"]}
    for field in ("mediaType", "filename"):
        value = _opt(raw, field)
        if value is not None:
            out[field] = value
    return out


def decode_raw(part: dict[str, Any]) -> bytes:
    try:
        return base64.b64decode(part["raw"], validate=True)
    except (binascii.Error, ValueError):
        raise invalid_params("Part 'raw' is not valid base64.") from None


def parse_message(raw: Any, *, v03: bool) -> dict[str, Any]:
    """Validate an incoming client message; return it in the current (1.0) shape."""
    if not isinstance(raw, dict):
        raise invalid_params("'message' must be an object.")
    message_id = check_id(raw.get("messageId"), "messageId")
    role = raw.get("role")
    if role not in (("user",) if v03 else (ROLE_USER, 1)):
        raise invalid_params("Messages from a client must have role ROLE_USER.")
    parts = raw.get("parts")
    if not isinstance(parts, list) or not parts:
        raise invalid_params("'parts' must be a non-empty list.")
    out: dict[str, Any] = {
        "messageId": message_id,
        "role": ROLE_USER,
        "parts": [parse_part(p, v03=v03) for p in parts],
    }
    for key in ("contextId", "taskId"):
        if raw.get(key) not in (None, ""):
            out[key] = check_id(raw[key], key)
    return out


def message_text(parts: list[dict[str, Any]]) -> str:
    return "\n".join(p["text"] for p in parts if isinstance(p.get("text"), str))


# ------------------------------------------------------------------ builders


def status(state: str, message: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"state": state, "timestamp": now_iso()}
    if message is not None:
        out["message"] = message
    return out


def agent_message(
    message_id: str, parts: list[dict[str, Any]], context_id: str, task_id: str
) -> dict[str, Any]:
    return {
        "messageId": message_id,
        "contextId": context_id,
        "taskId": task_id,
        "role": ROLE_AGENT,
        "parts": parts,
    }


def jsonable(value: Any) -> Any:
    """Plain JSON data for any agent output (Pydantic models become dicts)."""

    def default(obj: Any) -> Any:
        dump = getattr(obj, "model_dump", None)
        return dump(mode="json") if callable(dump) else str(obj)

    return json.loads(json.dumps(value, default=default, ensure_ascii=False))


# ----------------------------------------------------------- 0.3 conversion


def part_to_v03(part: dict[str, Any]) -> dict[str, Any]:
    if "text" in part:
        return {"kind": "text", "text": part["text"]}
    if "data" in part:
        data = part["data"]
        return {"kind": "data", "data": data if isinstance(data, dict) else {"value": data}}
    file: dict[str, Any] = {}
    file["bytes" if "raw" in part else "uri"] = part.get("raw", part.get("url"))
    if "mediaType" in part:
        file["mimeType"] = part["mediaType"]
    if "filename" in part:
        file["name"] = part["filename"]
    return {"kind": "file", "file": file}


def message_to_v03(message: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in message.items() if k in ("messageId", "contextId", "taskId")}
    out["kind"] = "message"
    out["role"] = "agent" if message.get("role") == ROLE_AGENT else "user"
    out["parts"] = [part_to_v03(p) for p in message.get("parts", [])]
    return out


def _status_v03(st: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"state": _STATE_TO_V03.get(st["state"], "unknown")}
    if "timestamp" in st:
        out["timestamp"] = st["timestamp"]
    if "message" in st:
        out["message"] = message_to_v03(st["message"])
    return out


def _artifact_v03(artifact: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in artifact.items() if k in ("artifactId", "name", "description")}
    out["parts"] = [part_to_v03(p) for p in artifact.get("parts", [])]
    return out


def task_to_v03(task: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "kind": "task",
        "id": task["id"],
        "contextId": task["contextId"],
        "status": _status_v03(task["status"]),
    }
    if task.get("artifacts"):
        out["artifacts"] = [_artifact_v03(a) for a in task["artifacts"]]
    if task.get("history"):
        out["history"] = [message_to_v03(m) for m in task["history"]]
    return out


def event_to_v03(event: dict[str, Any]) -> dict[str, Any]:
    """Convert one current-shape stream response into the 0.3 stream result."""
    if "task" in event:
        return task_to_v03(event["task"])
    if "message" in event:
        return message_to_v03(event["message"])
    if "statusUpdate" in event:
        update = event["statusUpdate"]
        state = update["status"]["state"]
        return {
            "kind": "status-update",
            "taskId": update["taskId"],
            "contextId": update["contextId"],
            "status": _status_v03(update["status"]),
            "final": state in TERMINAL_STATES or state in (INPUT_REQUIRED, AUTH_REQUIRED),
        }
    update = event["artifactUpdate"]
    return {
        "kind": "artifact-update",
        "taskId": update["taskId"],
        "contextId": update["contextId"],
        "artifact": _artifact_v03(update["artifact"]),
        "append": update.get("append", False),
        "lastChunk": update.get("lastChunk", True),
    }


def normalize_state(value: Any) -> str:
    """Map a 1.0 or 0.3 state spelling to the 1.0 enum name; unknown values stay as given."""
    if isinstance(value, str):
        return _V03_TO_STATE.get(value, value)
    return str(value)


def short_state(state: str) -> str:
    """``TASK_STATE_INPUT_REQUIRED`` -> ``input-required`` (the 0.3 spelling)."""
    return _STATE_TO_V03.get(state, state)
