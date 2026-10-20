"""Regressions from the F46 adversarial review: prompt defaults, stale URI list, blob size."""

import asyncio
import json

from nexura_agents import MCPServer
from nexura_agents.mcp import McpInMemoryTransport, McpServerSession
from nexura_agents.run_context import RunContext


async def make(cls=McpServerSession):
    c, s = McpInMemoryTransport.create_pair()
    srv = cls(s)
    srv.register_resource("file:///a.txt", "a", "hello")
    task = asyncio.create_task(srv.serve_loop())
    return MCPServer(c, name="d"), srv, task


async def test_prompt_argument_required_defaults_false_per_spec():
    mcp, srv, task = await make()
    srv._prompts_raw = None
    orig = srv.handle_request

    async def handle(req):
        if req.get("method") == "prompts/list":
            return {
                "jsonrpc": "2.0",
                "id": req["id"],
                "result": {"prompts": [{"name": "p", "arguments": [{"name": "opt"}]}]},
            }
        if req.get("method") == "prompts/get":
            return {
                "jsonrpc": "2.0",
                "id": req["id"],
                "result": {
                    "messages": [{"role": "user", "content": {"type": "text", "text": "x"}}]
                },
            }
        return await orig(req)

    srv.handle_request = handle
    async with mcp:
        msgs = await mcp.get_prompt("p", {})
        assert msgs[0].content == "x"
    task.cancel()


async def test_blob_size_estimate_accounts_for_padding():
    mcp, srv, task = await make()
    orig = srv.handle_request

    async def handle(req):
        if req.get("method") == "resources/read":
            return {
                "jsonrpc": "2.0",
                "id": req["id"],
                "result": {
                    "contents": [{"uri": "file:///a.txt", "mimeType": "image/png", "blob": "AAA="}]
                },
            }  # 2 bytes
        return await orig(req)

    srv.handle_request = handle
    async with mcp:
        assert "about 2 bytes" in await mcp.read_resource("file:///a.txt")
        srv_blob = "iVBORw0KGgo="  # 8 bytes
        assert len(__import__("base64").b64decode(srv_blob)) == 8
    task.cancel()


async def test_stale_listed_uris_stay_readable_after_list_fails():
    mcp, srv, task = await make()
    async with mcp:
        lst, read = await mcp.resource_tools()
        orig = srv.handle_request

        async def handle(req):
            if req.get("method") == "resources/list":
                return {"jsonrpc": "2.0", "id": req["id"], "error": {"code": -1, "message": "x"}}
            return await orig(req)

        srv.handle_request = handle
        out = await lst.execute({}, RunContext(deps=None, run_id="r", agent_name="a"))
        assert json.loads(out)["error"] == "MCP_JSONRPC_ERROR"
        # cache must not stay authorizing URIs after the server stops listing them
        out = await read.execute(
            {"uri": "file:///a.txt"}, RunContext(deps=None, run_id="r", agent_name="a")
        )
        assert json.loads(out)["error"] == "RESOURCE_NOT_LISTED"
    task.cancel()
