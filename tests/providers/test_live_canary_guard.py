"""Guards for scripts/live_canary.py: CI never enables it, and it is inert without opt-in.

The end-to-end check below runs the canary scenario against an explicitly labelled LOCAL
FAKE (an ``httpx.MockTransport`` that scripts OpenAI-shaped replies); no network is used.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
SECRET = "sk-proj-LOCALFAKEKEY0123456789abcdefghijklmnop"


def _script() -> ModuleType:
    name = "nexura_live_canary_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "live_canary.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _workflows() -> list[Path]:
    found = sorted(ROOT.glob(".github/workflows/*.y*ml"))
    found += sorted(ROOT.glob("enterprise/.github/workflows/*.y*ml"))
    return found


def test_ci_workflows_never_enable_live_canaries() -> None:
    workflows = _workflows()
    assert workflows, "expected CI workflow files"
    for path in workflows:
        text = path.read_text(encoding="utf-8")
        assert "NEXURA_LIVE_CANARY" not in text, f"{path} enables live canaries"
        assert "live_canary" not in text, f"{path} runs the live canary script"
        for key in (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "SARVAM_API_KEY",
        ):
            assert key not in text, f"{path} passes {key} to CI"


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("NEXURA_LIVE_CANARY", "NEXURA_CANARY_VERTEX_PROJECT", "NEXURA_CANARY_OLLAMA_MODEL"):
        monkeypatch.delenv(var, raising=False)
    for target in _script().TARGETS.values():
        for var in target.credential_vars:
            monkeypatch.delenv(var, raising=False)


def _forbid_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _script()

    def explode(model: str) -> Any:
        raise AssertionError("a provider was built although the canary is not enabled")

    for name, target in list(script.TARGETS.items()):
        monkeypatch.setitem(
            script.TARGETS, name, script.Target(name, "m", target.credential_vars, explode)
        )


async def test_keys_without_opt_in_send_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    _forbid_builds(monkeypatch)
    assert await _script().main(["--out-dir", str(tmp_path)]) == 0
    assert "nothing was sent" in capsys.readouterr().out
    assert not list(tmp_path.iterdir())


async def test_opt_in_without_keys_skips_every_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("NEXURA_LIVE_CANARY", "1")
    _forbid_builds(monkeypatch)
    assert await _script().main(["--out-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.count("skipped") == len(_script().TARGETS)
    assert not list(tmp_path.iterdir())


def _local_fake_openai(request: httpx.Request) -> httpx.Response:
    """LOCAL FAKE of the OpenAI chat API for the canary scenario (no network)."""
    body = json.loads(request.content)
    usage = {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}
    if body.get("stream"):
        chunks = [
            {"choices": [{"index": 0, "delta": {"content": "1, 2, 3, 4, 5"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": usage},
        ]
        sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(
            200, content=sse.encode(), headers={"content-type": "text/event-stream"}
        )
    messages = body["messages"]
    if body.get("response_format"):
        message: dict[str, Any] = {"content": '{"order_id": "ORD-7", "status": "delivered"}'}
    elif body.get("tools") and not any(m["role"] == "tool" for m in messages):
        message = {
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_order_status", "arguments": '{"order_id": "ORD-42"}'},
                }
            ],
        }
    elif body.get("tools"):
        message = {"content": "ORD-42 is out for delivery."}
    else:
        message = {"content": "ok"}
    return httpx.Response(
        200,
        json={
            "choices": [
                {"index": 0, "message": {"role": "assistant", **message}, "finish_reason": "stop"}
            ],
            "usage": usage,
        },
    )


async def test_opt_in_with_key_writes_masked_dated_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from nexura_agents.providers.openai import OpenAIProvider

    script = _script()
    _clear(monkeypatch)
    monkeypatch.setenv("NEXURA_LIVE_CANARY", "1")
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)

    def local_fake(model: str) -> OpenAIProvider:
        return OpenAIProvider(model, transport=httpx.MockTransport(_local_fake_openai))

    monkeypatch.setitem(
        script.TARGETS,
        "openai",
        script.Target("openai", "gpt-5.4-nano", ("OPENAI_API_KEY",), local_fake),
    )
    assert await script.main(["--provider", "openai", "--out-dir", str(tmp_path)]) == 0
    [evidence] = list(tmp_path.iterdir())
    assert evidence.name.startswith("live_canary_openai_20") and evidence.suffix == ".json"
    text = evidence.read_text(encoding="utf-8")
    assert SECRET not in text
    report = json.loads(text)
    assert report["passed"] is True, report["failures"]
    assert report["credential"] == {
        "variable": "OPENAI_API_KEY",
        "value": f"sk-...({len(SECRET)} chars)",
    }
    assert set(report["checks"]) == {"plain", "tool_call", "stream", "structured"}
    assert report["checks"]["tool_call"]["tool_calls"] == 1
    assert report["checks"]["structured"]["output"] == {"order_id": "ORD-7", "status": "delivered"}
