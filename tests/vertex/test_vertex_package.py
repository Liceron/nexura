"""P28 step 10 stage 4b: the Vertex AI provider is its own package, ``nexura-vertex``.

It depends on ``nexura-agents`` only. ``"vertex:<model>"`` strings build ``VertexAIProvider``
when it is installed; without it ``nexura_agents`` still imports, every other model string
works, and ``"vertex:..."`` fails with "pip install nexura-vertex". Absence is simulated in a
subprocess with ``sys.modules["nexura_vertex"] = None``; the gate's
``vertex-standalone-consumer`` step checks the same with real wheels.
"""

from __future__ import annotations

import ast
import asyncio
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "packages" / "nexura-vertex" / "src" / "nexura_vertex"
EXPECTED = (
    "'vertex:' models are provided by the 'nexura-vertex' package, which is not installed. "
    "Install it with: pip install nexura-vertex"
)

ABSENT_PROBE = textwrap.dedent(
    """
    import sys
    sys.modules["nexura_vertex"] = None  # the package is not installed
    import json
    import nexura_agents, nexura_agents.providers
    from nexura_agents import OpenAIProvider, infer_model
    from nexura_agents.errors import SDKValidationError

    try:
        infer_model("vertex:gemini-2.5-flash")
        error = None
    except SDKValidationError as exc:
        error = str(exc)
    print(json.dumps({
        "error": error,
        "openai": isinstance(infer_model("ollama:llama3.1"), OpenAIProvider),
        "exported": "VertexAIProvider" in nexura_agents.__all__
        or "VertexAIProvider" in nexura_agents.providers.__all__,
    }))
    """
)


def test_agents_without_vertex_package_name_the_install_command() -> None:
    out = subprocess.run(
        [sys.executable, "-c", ABSENT_PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    assert data == {"error": EXPECTED, "openai": True, "exported": False}


def test_installed_package_builds_vertex_model_strings(monkeypatch: pytest.MonkeyPatch) -> None:
    import nexura_vertex.provider as provider
    from nexura_agents import infer_model
    from nexura_vertex import VertexAIProvider

    class FakeAdc:
        """Stands in for Application Default Credentials (no google-auth call, no network)."""

        project = "demo-project"
        quota_project = None
        description = "test credentials"

        async def __call__(self) -> str:
            return "token"

    monkeypatch.setattr(provider, "_AdcTokenSource", FakeAdc)
    model = infer_model("vertex:gemini-2.5-flash")
    assert type(model) is VertexAIProvider
    assert model.project == "demo-project"
    assert model.credential_source == "test credentials"


def test_vertex_package_depends_on_the_agent_sdk_only() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "nexura_runtime"
            ):
                offenders.append(f"{path.name}: {node.module}")
            if isinstance(node, ast.Import):
                offenders += [
                    f"{path.name}: {a.name}"
                    for a in node.names
                    if a.name.startswith("nexura_runtime")
                ]
    assert offenders == []
    assert not (ROOT / "packages/nexura-agents/src/nexura_agents/providers/vertex").exists()


def test_model_request_blocking_still_covers_vertex() -> None:
    import httpx
    from nexura_agents.testing import ModelRequestsBlockedError, block_model_requests
    from nexura_agents.types import Message
    from nexura_vertex import VertexAIProvider

    model = VertexAIProvider(
        project="p",
        token_provider=lambda: "t",
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    with block_model_requests(), pytest.raises(ModelRequestsBlockedError, match="VertexAIProvider"):
        asyncio.run(model.generate([Message(role="user", content="hi")]))
