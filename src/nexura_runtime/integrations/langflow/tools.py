# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""SDK tools standing in for Langflow tool components. Network access is injectable."""

from __future__ import annotations

import ast
import inspect
import operator
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from nexura_agents import Tool, tool, web_fetch_tool

#: ``search(query) -> text | list | dict`` (sync or async). Pass to ``build(search_provider=...)``.
SearchProvider = Callable[[str], Any | Awaitable[Any]]

_BIN: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def safe_eval(expression: str) -> float | int:
    """Evaluate plain arithmetic. No names, calls, attributes or huge exponents."""
    if len(expression) > 200:
        raise ValueError("expression too long")
    tree = ast.parse(expression.strip(), mode="eval")

    def ev(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            if isinstance(node.value, bool):
                raise ValueError("booleans are not numbers")
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 1000:
                raise ValueError("exponent too large")
            return _BIN[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](ev(node.operand))
        raise ValueError("only numbers and + - * / // % ** are allowed")

    result = ev(tree)
    if isinstance(result, float) and result == int(result) and abs(result) < 1e15:
        return int(result)
    return result  # type: ignore[no-any-return]


def calculator_tool(name: str = "calculator") -> Tool:
    """Return a safe arithmetic calculator tool."""

    @tool(name=name, description="Evaluate an arithmetic expression such as '2 * (3 + 4)'.")
    def calculator(expression: str) -> dict[str, Any]:
        try:
            return {"result": safe_eval(expression)}
        except (ValueError, SyntaxError, ZeroDivisionError, OverflowError) as exc:
            return {"error": f"cannot evaluate: {exc}"}

    return calculator


def current_date_tool() -> Tool:
    @tool(name="get_current_date", description="Return today's date (UTC) as YYYY-MM-DD.")
    def get_current_date() -> str:
        return datetime.now(UTC).date().isoformat()

    return get_current_date


def url_tool(urls: Any, transport: httpx.AsyncBaseTransport | None) -> Tool:
    """Fetch tool. Domains named in the flow's ``urls`` field restrict where it may go."""
    domains: list[str] = []
    for entry in urls if isinstance(urls, list) else [urls]:
        host = urlsplit(entry if "//" in str(entry) else f"//{entry}").hostname if entry else None
        if host and host not in domains:
            domains.append(host)
    if not domains:
        # An empty allow-list means "unrestricted" to web_fetch_tool, so fail closed instead.
        @tool(name="fetch_url", description="Fetch a URL (no domains are allowed for this flow).")
        async def fetch_url(url: str) -> dict[str, Any]:
            return {
                "error": "refused",
                "message": "The flow's urls field names no domains, so every fetch is refused.",
            }

        return fetch_url
    return web_fetch_tool(allowed_domains=domains, transport=transport, name="fetch_url")


def web_search_tool(provider: SearchProvider | None) -> Tool:
    @tool(name="web_search", description="Search the web and return result text.")
    async def web_search(query: str) -> Any:
        if provider is None:
            return {
                "error": "web_search_not_configured",
                "message": "No search provider was configured for this imported flow.",
            }
        found = provider(query)
        if inspect.isawaitable(found):
            found = await found
        return found

    return web_search
