# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Arithmetic sandbox, replayable time and deterministic encoding utilities."""

import asyncio
import hashlib
import uuid

import pytest
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.errors import SchemaValidationError

from nexura_runtime.modules import default_registry


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("1 + 2 * 3 - 4 / 2", 5),
        ("7 // 2 + 7 % 2", 4),
        ("2 ** 3", 8),
        ("-x + +2", -1),
        ("max(1, x, 2) + min(4, 2)", 5),
        ("round(1.234, 2) + abs(-2)", 3.23),
        ("round(1.2)", 1),
        ("1 < x <= 3", True),
        ("x == 3", True),
        ("x != 3", False),
        ("x >= 3", True),
        ("x > 4", False),
    ],
)
def test_math(run_component, expression, expected):
    assert (
        run_component("nexura.utils.math", expression=expression, variables={"x": 3})["value"]
        == expected
    )


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os')",
        "x.real",
        "[1, 2]",
        "x[0]",
        "lambda: 1",
        "2 ** 100",
        "1 / 0",
        "9 ** 16 ** 16",
        "1e101",
        "True + 1",
        "missing + 1",
        "min()",
        "round(1, 1000)",
        "abs(x=1)",
        "(-1) ** 0.5",
        "1 is 1",
        "1 +",
        "+".join(["1"] * 65),
    ],
)
def test_math_rejects_unsafe_and_excessive_expressions(run_component, expression):
    with pytest.raises(SchemaValidationError):
        run_component("nexura.utils.math", expression=expression, variables={"x": 1})


def test_datetime_options_and_injected_clock(run_component):
    text = "2026-01-02T03:04:05Z"
    assert (
        run_component("nexura.utils.datetime", operation="parse", text=text)["iso"]
        == "2026-01-02T03:04:05+00:00"
    )
    assert (
        run_component("nexura.utils.datetime", operation="parse", text="2026-01-02")["iso"]
        == "2026-01-02T00:00:00+00:00"
    )
    assert (
        run_component(
            "nexura.utils.datetime", operation="format", text=text, format="%Y/%m/%d %% %H:%M"
        )["text"]
        == "2026/01/02 % 03:04"
    )
    for operation, day in [("add", "03"), ("subtract", "01")]:
        assert run_component(
            "nexura.utils.datetime", operation=operation, text=text, duration={"days": 1}
        )["iso"].startswith("2026-01-" + day)
    assert (
        run_component(
            "nexura.utils.datetime",
            operation="add",
            text=text,
            duration={"hours": 1, "minutes": 2, "seconds": 3},
        )["iso"]
        == "2026-01-02T04:06:08+00:00"
    )
    assert (
        run_component(
            "nexura.utils.datetime", operation="timezone", text=text, timezone="Asia/Kolkata"
        )["iso"]
        == "2026-01-02T08:34:05+05:30"
    )
    assert run_component("nexura.utils.datetime", operation="now", text=text)["iso"].endswith(
        "+00:00"
    )
    module = default_registry.get("nexura.utils.datetime")
    context = ExecutionContext(run_id="r", node_id="n", attributes={"now": text})
    result = asyncio.run(module.invoke(context, {"operation": "now"}))
    assert result == asyncio.run(module.invoke(context, {"operation": "now"}))
    assert result.outputs["iso"] == "2026-01-02T03:04:05+00:00"


@pytest.mark.parametrize("algorithm", ["sha256", "sha1", "md5"])
def test_hash(run_component, algorithm):
    assert (
        run_component("nexura.utils.hash", text="hello", algorithm=algorithm)["digest"]
        == hashlib.new(algorithm, b"hello", usedforsecurity=False).hexdigest()
    )


@pytest.mark.parametrize("url_safe", [False, True])
def test_base64_roundtrip(run_component, url_safe):
    text = "héllo?世界"
    encoded = run_component(
        "nexura.utils.base64", text=text, operation="encode", url_safe=url_safe
    )["text"]
    assert (
        run_component("nexura.utils.base64", text=encoded, operation="decode", url_safe=url_safe)[
            "text"
        ]
        == text
    )


def test_url_and_uuid(run_component):
    query = [{"key": "q", "value": "a & b"}, {"key": "q", "value": ""}]
    result = run_component(
        "nexura.utils.url",
        operation="build",
        scheme="https",
        netloc="example.test:443",
        path="/search",
        query=query,
        fragment="top",
    )
    assert result["url"] == "https://example.test:443/search?q=a+%26+b&q=#top"
    assert (
        run_component("nexura.utils.url", operation="parse", url=result["url"])["parts"]
        == result["parts"]
    )
    assert result["parts"]["query"] == query
    for namespace in ("dns", "url", "oid", "x500", str(uuid.NAMESPACE_DNS)):
        result = run_component("nexura.utils.uuid", namespace=namespace, name="example.test")[
            "uuid"
        ]
        assert uuid.UUID(result).version == 5
    assert run_component("nexura.utils.uuid", namespace="dns", name="example.test")["uuid"] == str(
        uuid.uuid5(uuid.NAMESPACE_DNS, "example.test")
    )


@pytest.mark.parametrize(
    ("name", "inputs"),
    [
        ("datetime", {"operation": "now"}),
        ("datetime", {"operation": "parse", "text": "bad"}),
        ("datetime", {"operation": "format", "text": "2026-01-01", "format": "%c"}),
        ("datetime", {"operation": "format", "text": "2026-01-01", "format": "%"}),
        ("datetime", {"operation": "timezone", "text": "2026-01-01", "timezone": "No/SuchZone"}),
        ("datetime", {"operation": "timezone", "text": "2026-01-01", "timezone": "../secret"}),
        ("datetime", {"operation": "add", "text": "2026-01-01"}),
        ("datetime", {"operation": "timezone", "text": "2026-01-01"}),
        ("base64", {"operation": "decode", "text": "!bad!"}),
        ("base64", {"operation": "decode", "text": "/w=="}),
        ("base64", {"operation": "decode", "text": "aGk"}),
        ("url", {"operation": "parse", "url": "http://x.test:99999"}),
        ("url", {"operation": "parse"}),
        ("uuid", {"namespace": "bad", "name": "a"}),
        ("math", {"expression": "x", "variables": {"x": True}}),
    ],
)
def test_utils_errors(run_component, name, inputs):
    with pytest.raises(SchemaValidationError):
        run_component("nexura.utils." + name, **inputs)
