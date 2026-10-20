"""Typed results: Agent[Output] / RunResult[Output] for static checkers, no runtime change."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from nexura_agents import Agent, FakeModel, Runner
from nexura_agents.types import RunResult
from pydantic import BaseModel

CASES = Path(__file__).parent / "typing_cases"
ROOT = Path(__file__).resolve().parents[2]


class Invoice(BaseModel):
    number: str
    total: float


def _agent(**kwargs: object) -> Agent:
    return Agent(
        name="a",
        instructions="x",
        model=FakeModel(responses=['{"number": "A1", "total": 2.5}']),
        **kwargs,  # type: ignore[arg-type]
    )


def test_output_is_model_with_output_type() -> None:
    result = Runner().run(_agent(output_type=Invoice), "go")
    assert isinstance(result, RunResult)
    assert isinstance(result.output, Invoice)
    assert result.output.total == 2.5


def test_output_is_text_without_output_type() -> None:
    result = Runner().run(_agent(), "go")
    assert isinstance(result.output, str)
    assert result.output == result.final_output


async def test_async_run() -> None:
    result = await Runner().run_async(_agent(output_type=Invoice), "go")
    assert isinstance(result.output, Invoice)


def test_agent_shortcut() -> None:
    assert isinstance(_agent(output_type=Invoice).run("go").output, Invoice)


def test_no_extra_runtime_state() -> None:
    agent = _agent(output_type=Invoice)
    assert type(agent) is Agent
    assert not hasattr(agent, "__orig_class__")
    assert agent.output_type is Invoice
    assert set(vars(RunResult(run_id="r", final_output="", messages=[]))) >= {"output"}


def test_subscripting_and_subclass_and_clone() -> None:
    class Support(Agent[Invoice]):
        pass

    agent = Support(
        name="s",
        instructions="x",
        model=FakeModel(responses=['{"number": "A1", "total": 1}']),
        output_type=Invoice,
    )
    assert type(agent) is Support
    assert isinstance(agent.clone(name="t"), Support)
    assert isinstance(agent.run("go").output, Invoice)
    with agent.override(name="o"):
        assert agent.name == "o"


def test_bad_output_type_still_rejected() -> None:
    with pytest.raises(Exception, match="output_type"):
        _agent(output_type=int)


def _mypy(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "mypy", "--no-incremental", "--no-error-summary", str(path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def test_static_types_check_with_mypy() -> None:
    pytest.importorskip("mypy")
    done = _mypy(CASES / "typed_ok.py")
    assert done.returncode == 0, done.stdout + done.stderr


def test_static_check_catches_wrong_output_type() -> None:
    pytest.importorskip("mypy")
    done = _mypy(CASES / "typed_bad.py")
    assert done.returncode == 1, done.stdout + done.stderr
    assert "Invoice" in done.stdout
