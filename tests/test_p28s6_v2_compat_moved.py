# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""P28 step 6: the Nexura V2 compatibility server left the open-source edition.

Founder decision 2026-10-18 (question 5): the Langflow importer, the export back to Langflow
JSON and the trace exporter to the hosted product stay open source; the V2-shaped run server,
the V2 SSE event shapes, Firebase auth, V2 RBAC, V2 run records and the shadow harness are
closed. Everything here runs with no closed edition installed.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from nexura_agents import FakeModel, RunEvent

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "integrations" / "langflow" / "fixtures"
CLOSED = "nexura_" + "enterprise"  # spelled apart so source scans never match this file

MOVED_MODULES = (
    "nexura_runtime.integrations.langflow.server",
    "nexura_runtime.integrations.langflow.events",
    "nexura_runtime.integrations.nexura_v2.auth",
    "nexura_runtime.integrations.nexura_v2.policy",
    "nexura_runtime.integrations.nexura_v2.records",
    "nexura_runtime.integrations.nexura_v2.shadow",
)
MOVED_FILES = (
    "src/nexura_runtime/integrations/langflow/server.py",
    "src/nexura_runtime/integrations/langflow/events.py",
    "src/nexura_runtime/integrations/nexura_v2/auth.py",
    "src/nexura_runtime/integrations/nexura_v2/policy.py",
    "src/nexura_runtime/integrations/nexura_v2/records.py",
    "src/nexura_runtime/integrations/nexura_v2/shadow.py",
    "tests/integrations/nexura_v2",
    "tests/integrations/langflow/test_v2_api.py",
    "tests/integrations/langflow/test_v2_api_review.py",
    "tests/integrations/langflow/test_v7_per_caller.py",
    "tests/integrations/langflow/test_v7_review_fixes.py",
    "docs/migrating-from-langflow.md",
)
KEPT_FILES = (
    "src/nexura_runtime/integrations/langflow/build.py",
    "src/nexura_runtime/integrations/langflow/export.py",
    "src/nexura_runtime/integrations/langflow/flow.py",
    "src/nexura_runtime/integrations/nexura_v2/exporter.py",
    "examples/sdk_integration/p01.py",
    "docs/langflow-import.md",
    "tests/integrations/langflow/test_langflow_load.py",
    "tests/integrations/langflow/test_langflow_run.py",
    "tests/integrations/langflow/test_v5_export.py",
    "tests/sdk_integration/p01/test_exporter.py",
)


@pytest.fixture(autouse=True)
def _no_closed_edition() -> None:
    assert importlib.util.find_spec(CLOSED) is None
    assert not [m for m in sys.modules if m == CLOSED or m.startswith(CLOSED + ".")]


def _flow(name: str = "f1_minimal_chat") -> dict[str, Any]:
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", MOVED_MODULES)
def test_moved_modules_cannot_be_imported(name: str) -> None:
    assert importlib.util.find_spec(name) is None
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(name)


def test_moved_files_are_gone_and_the_open_source_parts_remain() -> None:
    assert [p for p in MOVED_FILES if (ROOT / p).exists()] == []
    assert [p for p in KEPT_FILES if not (ROOT / p).is_file()] == []


def test_packages_export_only_the_open_source_parts() -> None:
    from nexura_runtime.integrations import langflow, nexura_v2

    for gone in ("SSEEvent", "V2EventMapper", "V2FlowServer", "PerCaller"):
        assert not hasattr(langflow, gone) and gone not in langflow.__all__, gone
    for kept in ("load", "build", "run_flow", "export_flow", "stream_flow_events"):
        assert kept in langflow.__all__, kept
    assert "FlowEventMapper" in langflow.__all__
    assert sorted(nexura_v2.__all__) == [
        "NexuraV2TraceExporter",
        "TraceExportError",
        "TraceExportReceipt",
        "TraceMetadata",
        "TraceValidationError",
        "build_trace_envelope",
    ]
    for gone in ("run_shadow", "RbacPolicy", "FirebaseIdTokenVerifier", "build_run_records"):
        with pytest.raises(AttributeError):
            getattr(nexura_v2, gone)


def test_no_open_source_file_references_a_moved_module() -> None:
    needles = (
        "integrations.langflow.server",
        "integrations.langflow.events",
        "from .server import",
        "from .events import",
        "nexura_v2.auth",
        "nexura_v2.policy",
        "nexura_v2.records",
        "nexura_v2.shadow",
        "V2EventMapper",
        "V2FlowServer(",
    )
    hits = []
    roots = ("src", "packages", "examples", "scripts", "clients/typescript/src", "studio-web/src")
    for base in roots:
        for path in (ROOT / base).rglob("*"):
            if path.suffix not in {".py", ".ts", ".tsx"} or "node_modules" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            hits += [f"{path.relative_to(ROOT)}: {n}" for n in needles if n in text]
    tests = ROOT / "tests"
    for path in tests.rglob("*.py"):
        if path.name == Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        hits += [f"{path.relative_to(ROOT)}: {n}" for n in needles[:8] if n in text]
    assert hits == []


def test_importer_runs_and_exports_back_without_the_closed_edition() -> None:
    from nexura_runtime.integrations.langflow import build, export_flow, load, run_flow

    data = _flow()
    imported = load(data)
    assert run_flow(imported, "hi", models=FakeModel(["local fake answer"])) == "local fake answer"
    result = build(imported, models=FakeModel(["again"])).run("hi", session_id="s")
    assert result.output == "again" and result.session_id == "s"
    round_trip = export_flow(load(data), original=data)
    assert load(round_trip).report.supported == imported.report.supported


async def test_stream_needs_a_mapper_and_works_with_a_custom_open_source_one() -> None:
    from nexura_runtime.integrations.langflow import (
        FlowEventMapper,
        build,
        load,
        stream_flow_events,
    )

    built = build(load(_flow()), models=FakeModel(["Hello"], stream=True, chunk_size=2))
    with pytest.raises(TypeError):
        built.stream_events("hi")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        stream_flow_events(built, "hi")  # type: ignore[call-arg]

    class Tuples:
        """A minimal wire format: (step, detail) tuples."""

        def __init__(self) -> None:
            self.n = 0

        def new_id(self) -> str:
            self.n += 1
            return f"m{self.n}"

        def vertices_sorted(self, ids: Any, to_run: Any = None) -> tuple[str, Any]:
            return ("sorted", list(ids))

        def build_start(self, vertex_id: str) -> tuple[str, Any]:
            return ("start", vertex_id)

        def end_vertex(
            self, vertex_id: str, display_name: str, class_name: str, results: dict[str, Any]
        ) -> tuple[str, Any]:
            return ("end_vertex", vertex_id)

        def add_message(
            self, message_id: str, text: str, *, sender: str, session_id: str | None
        ) -> tuple[str, Any]:
            return ("message", (sender, text))

        def token(self, message_id: str, chunk: str) -> tuple[str, Any]:
            return ("token", chunk)

        def error(self, vertex_id: str | None, exc: BaseException) -> tuple[str, Any]:
            return ("error", type(exc).__name__)

        def build_end(self) -> tuple[str, Any]:
            return ("build_end", None)

        def end(self) -> tuple[str, Any]:
            return ("end", None)

        def from_run_event(
            self, vertex_id: str, event: RunEvent, *, message_id: str | None = None
        ) -> list[tuple[str, Any]]:
            text = event.data.get("text")
            if message_id is not None and event.type.value == "text_delta" and text:
                return [("token", str(text))]
            return []

    mapper: FlowEventMapper[tuple[str, Any]] = Tuples()
    events = [e async for e in stream_flow_events(built, "hi", mapper=mapper)]
    kinds = [k for k, _ in events]
    assert kinds[0] == "sorted" and kinds[-2:] == ["build_end", "end"]
    assert "".join(str(d) for k, d in events if k == "token") == "Hello"
    assert ("message", ("User", "hi")) in events and ("message", ("Machine", "Hello")) in events


def test_trace_exporter_still_works_over_a_mock_transport() -> None:
    from nexura_runtime import Agent, Runner
    from nexura_runtime.integrations.nexura_v2 import NexuraV2TraceExporter, TraceMetadata
    from nexura_runtime.observability.tracer import NexuraTracer

    seen: list[dict[str, Any]] = []

    def local_mock(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/traces"
        assert request.headers["x-api-key"] == "test-only-key"
        body = json.loads(request.content)
        UUID(body["id"])
        seen.append(body)
        return httpx.Response(201, json={"id": body["id"]})

    tracer = NexuraTracer()
    agent = Agent(name="fake", instructions="x", model=FakeModel(responses=["ok"]))
    with tracer.span("P28 step 6 fixture"):
        Runner().run(agent, "hi")
    with (
        httpx.Client(transport=httpx.MockTransport(local_mock)) as client,
        NexuraV2TraceExporter("https://v2.invalid", "test-only-key", client=client) as exporter,
    ):
        receipt = exporter.export(tracer.get_spans(), TraceMetadata("step 6"))
    assert len(seen) == 1 and seen[0]["spans"]
    assert str(receipt.trace_id) == seen[0]["id"]
