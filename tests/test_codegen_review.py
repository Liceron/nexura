"""Regressions from the P5 review: hostile literals in Python import."""

import contextlib
import tempfile
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from nexura_runtime.codegen import CodeImportError, python_to_workflow, workflow_to_python
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.workflow import WorkflowNode, WorkflowSpec

HDR = "from nexura_runtime.workflow import WorkflowNode, WorkflowSpec\n"


def src(meta):
    return (
        HDR
        + 'spec = WorkflowSpec(name="x", entry_node="a", '
        + 'nodes={"a": WorkflowNode(id="a", handler="h")}, '
        + f"metadata={meta})\n"
    )


def test_non_finite_literal_rejected():
    # 1e999 parses to inf; generator refuses non-finite, importer must too
    with pytest.raises(CodeImportError):
        python_to_workflow(src('{"v": 1e999}'))


def test_lone_surrogate_rejected_cleanly():
    with pytest.raises(CodeImportError):
        python_to_workflow(src('{"v": "\\ud800"}'))


def test_roundtrip_nan_inf_generation():
    s = WorkflowSpec(
        name="x",
        entry_node="a",
        nodes={"a": WorkflowNode(id="a", handler="h")},
        metadata={"v": 1.5},
    )
    assert python_to_workflow(workflow_to_python(s)).to_dict() == s.to_dict()


@pytest.mark.asyncio
@pytest.mark.parametrize("meta", ['{"v": "\\ud800"}', '{"v": 1e999}'])
async def test_endpoint_hostile_literals(meta):
    body = src(meta).replace('handler="h"', 'handler="synthetic_router"')
    with tempfile.TemporaryDirectory() as tmp:
        app = create_api_app(db_path=str(Path(tmp) / "c.sqlite3"), profile="local-owner")
        c = httpx.AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://t"
        )
        r = await c.post("/api/v1/workflows/import/python", json={"source": body})
        assert r.status_code == 400, (r.status_code, r.text[:100])


@pytest.mark.parametrize(
    "payload",
    [
        "x" * 10,
        "(" * 100000,
        "[" * 100000 + "]" * 100000,
        "-" * 100000 + "1",
        "a" + ".b" * 50000,
        "1" + "+1" * 100000,
        "\x00",
        "spec = " + "0" * 5000,
        "# coding: utf-7\n+ADs-",
    ],
)
def test_dos_inputs_only_import_error(payload):
    with contextlib.suppress(CodeImportError):
        python_to_workflow(HDR + payload)


def test_deep_dict_in_metadata():
    m = "{'a':" * 40 + "1" + "}" * 40
    python_to_workflow(src(m))


def test_unary_chain_in_literal():
    with pytest.raises(CodeImportError):
        python_to_workflow(src("{'a': --1}"))
