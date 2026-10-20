"""A minimal MCP server over stdio, written from the protocol spec (no SDK code).

Behaves like real servers in the ways that break naive clients: it sends a log
notification and a ``ping`` request before answering ``tools/call``.
"""

import json
import sys

TOOLS = [
    {
        "name": "get_weather",
        "description": "Current weather for a city.",
        "inputSchema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "delete_file",
        "description": "Delete a file.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
]


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        msg = json.loads(line)
        method, msg_id = msg.get("method"), msg.get("id")
        if method is None or msg_id is None:
            continue  # notifications and our own ping replies
        if method == "initialize":
            result = {
                "protocolVersion": msg["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fixture", "version": "1.0"},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            name, args = msg["params"]["name"], msg["params"]["arguments"]
            send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/message",
                    "params": {"level": "info", "data": f"calling {name}"},
                }
            )
            send({"jsonrpc": "2.0", "id": "srv-ping-1", "method": "ping"})
            if name == "get_weather":
                result = {"content": [{"type": "text", "text": f"Sunny in {args['city']}"}]}
            else:
                result = {
                    "content": [{"type": "text", "text": "permission denied"}],
                    "isError": True,
                }
        else:
            send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "unknown"}})
            continue
        send({"jsonrpc": "2.0", "id": msg_id, "result": result})


if __name__ == "__main__":
    main()
