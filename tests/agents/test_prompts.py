"""Versioned prompts: registry, stores, safe rendering, agent integration, tracing."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import nexura_agents
import pytest
from nexura_agents import (
    Agent,
    FakeModel,
    FilePromptStore,
    InMemoryPromptStore,
    PromptRegistry,
    PromptVersion,
    Runner,
)
from nexura_agents import tracing as nexura_tracing
from nexura_agents.errors import SDKValidationError
from nexura_agents.prompts import (
    PromptError,
    PromptNotFoundError,
    PromptSyntaxError,
    PromptVariableError,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


def system_of(model: FakeModel, call: int = -1) -> str:
    return str(model.calls[call]["messages"][0].content)


def test_exports_are_lazy_and_listed() -> None:
    for name in ("PromptRegistry", "PromptVersion", "InMemoryPromptStore", "FilePromptStore"):
        assert name in nexura_agents.__all__
        assert getattr(nexura_agents, name) is not None


def test_versioning_dedupe_and_immutability() -> None:
    reg = PromptRegistry()
    v1 = reg.register("p", "Hello {{ name }}", metadata={"author": "a"})
    assert (v1.version, v1.labels) == (1, ("latest",))
    assert (
        reg.register("p", "Hello {{ name }}") is v1 or reg.register("p", "Hello {{ name }}") == v1
    )
    v2 = reg.register("p", "Hi {{ name }}")
    assert v2.version == 2
    assert [v.version for v in reg.versions("p")] == [1, 2]
    assert reg.get("p").version == 2
    assert reg.get("p", version=1).template == "Hello {{ name }}"
    assert reg.get("p", version=1).labels == ()  # latest moved
    with pytest.raises(FrozenInstanceError):
        v1.template = "x"  # type: ignore[misc]
    assert v1.sha256 and v1.metadata == {"author": "a"}
    # A template identical to an older (non-newest) version is a new version.
    assert reg.register("p", "Hello {{ name }}").version == 3
    assert reg.names() == ["p"]


def test_lookup_errors() -> None:
    reg = PromptRegistry()
    reg.register("p", "x")
    with pytest.raises(PromptNotFoundError):
        reg.get("missing")
    with pytest.raises(PromptNotFoundError):
        reg.get("p", version=9)
    with pytest.raises(PromptNotFoundError):
        reg.get("p", label="production")
    with pytest.raises(SDKValidationError):
        reg.get("p", version=1, label="latest")
    with pytest.raises(PromptNotFoundError):
        reg.set_label("p", "production", 5)


def test_labels_move() -> None:
    reg = PromptRegistry()
    reg.register("p", "one")
    reg.register("p", "two", labels=("staging",))
    assert reg.get("p", label="staging").version == 2
    reg.set_label("p", "production", 1)
    assert reg.get("p", label="production").version == 1
    reg.set_label("p", "production", 2)
    assert reg.get("p", label="production").version == 2
    assert "production" not in reg.get("p", version=1).labels


def test_version_round_trip_and_tamper() -> None:
    v = PromptVersion(name="p", version=1, template="a {{ b.c }}", labels=("x",))
    assert PromptVersion.from_dict(v.to_dict()) == v
    bad = v.to_dict() | {"template": "changed"}
    with pytest.raises(PromptError):
        PromptVersion.from_dict(bad)


def test_rendering() -> None:
    v = PromptVersion(name="p", version=1, template="Hi {{ user.name }}, {{a}} and {{ a }}")
    assert v.variables == ("user.name", "a")
    assert v.render({"user": {"name": "Ada"}, "a": 5, "extra": 1}) == "Hi Ada, 5 and 5"
    with pytest.raises(PromptVariableError) as info:
        v.render({"user": {}})
    assert "user.name" in str(info.value) and "a" in info.value.missing
    with pytest.raises(PromptVariableError):
        v.render({"user": "text", "a": 1})  # dotted access only on mappings


def test_no_attribute_access_or_expressions() -> None:
    reg = PromptRegistry()
    for bad in (
        "{{ obj.__class__ }}",
        "{{ obj.__class__.__mro__ }}",
        "{{ f() }}",
        "{{ a | upper }}",
        "{{ a + b }}",
        "{{ }}",
        "{{ 1 }}",
        "{{ a }",
        "{{ a }} }}",
        "{{ {{ a }} }}",
    ):
        with pytest.raises(PromptSyntaxError):
            reg.register("p", bad)
    assert reg.names() == []

    class Obj:
        secret = "leak"

    v = PromptVersion(name="p", version=1, template="{{ obj.secret }}")
    with pytest.raises(PromptVariableError):
        v.render({"obj": Obj()})


def test_no_double_expansion() -> None:
    v = PromptVersion(name="p", version=1, template="A={{ a }} B={{ b }}")
    assert v.render({"a": "{{ b }}", "b": "x"}) == "A={{ b }} B=x"
    assert PromptVersion(name="p", version=1, template="{ {x} } {{ a }}").render({"a": 1}) == (
        "{ {x} } 1"
    )


def test_promotion_affects_next_run_of_existing_agent() -> None:
    reg = PromptRegistry()
    reg.register("support", "v1 for {{ who }}")
    reg.set_label("support", "production", 1)
    model = FakeModel(responses=["a", "b", "c"])
    agent = Agent(
        name="s", model=model, instructions=reg.instructions("support", variables={"who": "Ada"})
    )
    runner = Runner()
    r1 = runner.run(agent, "hi")
    assert system_of(model) == "v1 for Ada"
    assert r1.prompt is not None and r1.prompt["name"] == "support" and r1.prompt["version"] == 1
    reg.register("support", "v2 for {{ who }}")
    assert runner.run(agent, "hi").prompt["version"] == 1  # type: ignore[index]  # not promoted
    reg.set_label("support", "production", 2)
    r3 = runner.run(agent, "hi")
    assert system_of(model) == "v2 for Ada"
    assert r3.prompt is not None and r3.prompt["version"] == 2
    assert r3.prompt["sha256"] == reg.get("support", version=2).sha256


def test_variables_callable_gets_context_and_context_records_prompt() -> None:
    reg = PromptRegistry()
    reg.register("p", "run {{ rid }} agent {{ agent }}")
    seen: list[Any] = []

    def variables(ctx: Any) -> dict[str, str]:
        seen.append(ctx)
        return {"rid": ctx.run_id, "agent": ctx.agent_name}

    model = FakeModel(responses=["ok"])
    agent = Agent(
        name="bot",
        model=model,
        instructions=reg.instructions("p", label="latest", variables=variables),
    )
    result = Runner().run(agent, "x")
    assert system_of(model) == f"run {result.run_id} agent bot"
    assert seen[0].prompt == {"name": "p", "version": 1, "sha256": reg.get("p").sha256}


def test_plain_agent_has_no_prompt_and_missing_var_fails_run() -> None:
    plain = Runner().run(Agent(name="a", model=FakeModel(responses=["x"]), instructions="hi"), "q")
    assert plain.prompt is None
    reg = PromptRegistry()
    reg.register("p", "{{ need }}")
    agent = Agent(
        name="a",
        model=FakeModel(responses=["x"]),
        instructions=reg.instructions("p", label="latest"),
    )
    with pytest.raises(PromptVariableError):
        Runner().run(agent, "q")


def test_pinned_mode_and_explicit_version() -> None:
    reg = PromptRegistry()
    reg.register("p", "one")
    model = FakeModel(responses=["a", "b", "c"])
    pinned = Agent(
        name="a", model=model, instructions=reg.instructions("p", label="latest", pin=True)
    )
    fixed = Agent(name="b", model=model, instructions=reg.instructions("p", version=1))
    reg.register("p", "two")
    Runner().run(pinned, "q")
    assert system_of(model) == "one"
    Runner().run(fixed, "q")
    assert system_of(model) == "one"
    with pytest.raises(PromptNotFoundError):
        reg.instructions("p", label="production", pin=True)  # fails at creation
    with pytest.raises(SDKValidationError):
        reg.instructions("p", label="latest", version=1)


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    nexura_tracing.instrument(tracer_provider=provider)
    yield memory
    nexura_tracing.uninstrument()


def test_tracing_attributes(exporter: InMemorySpanExporter) -> None:
    reg = PromptRegistry()
    reg.register("p", "a")
    reg.register("p", "b")
    agent = Agent(
        name="t",
        model=FakeModel(responses=["ok"]),
        instructions=reg.instructions("p", label="latest"),
    )
    Runner().run(agent, "q")
    run = next(s for s in exporter.get_finished_spans() if s.name.startswith("invoke_agent"))
    assert run.attributes is not None
    assert run.attributes["gen_ai.prompt.name"] == "p"
    assert run.attributes["gen_ai.prompt.version"] == 2


def test_file_store_round_trip(tmp_path: Path) -> None:
    directory = tmp_path / "does" / "not" / "exist"
    reg = PromptRegistry(FilePromptStore(directory))
    assert reg.names() == []
    reg.register("greet", "Hello {{ name }}", metadata={"k": [1]})
    reg.register("greet", "Hi {{ name }}")
    reg.set_label("greet", "production", 1)
    assert (directory / "greet.json").is_file()
    data = json.loads((directory / "greet.json").read_text())
    assert [v["version"] for v in data["versions"]] == [1, 2]
    again = PromptRegistry(FilePromptStore(directory))
    assert again.names() == ["greet"]
    assert again.get("greet", label="production").template == "Hello {{ name }}"
    assert again.get("greet").version == 2
    assert again.get("greet", version=1).metadata == {"k": [1]}
    assert not list(directory.glob("*.tmp"))


def test_file_store_atomic_write_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg = PromptRegistry(FilePromptStore(tmp_path))
    reg.register("p", "first")
    before = (tmp_path / "p.json").read_text()

    def boom(*_a: Any, **_k: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        reg.register("p", "second")
    monkeypatch.undo()
    assert (tmp_path / "p.json").read_text() == before
    assert sorted(p.name for p in tmp_path.iterdir() if p.name != ".prompts.lock") == ["p.json"]
    # First write failing leaves nothing behind either.
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        reg.register("q", "x")
    monkeypatch.undo()
    assert sorted(p.name for p in tmp_path.iterdir() if p.name != ".prompts.lock") == ["p.json"]


@pytest.mark.parametrize(
    "name",
    ["../evil", "a/b", "a\\b", "..", ".", ".hidden", "a..b", "", "x" * 101, "sp ace", "nul\0"],
)
def test_bad_names_rejected(tmp_path: Path, name: str) -> None:
    reg = PromptRegistry(FilePromptStore(tmp_path / "d"))
    with pytest.raises(SDKValidationError):
        reg.register(name, "x")
    with pytest.raises(SDKValidationError):
        reg.get(name)
    assert not (tmp_path / "evil.json").exists()
    assert not (tmp_path / "d").exists() or not list((tmp_path / "d").iterdir())


def test_corrupt_file_is_clear_error(tmp_path: Path) -> None:
    (tmp_path / "p.json").write_text("{not json")
    with pytest.raises(PromptError, match="corrupt"):
        PromptRegistry(FilePromptStore(tmp_path)).get("p")


@pytest.mark.parametrize("kind", ["memory", "file"])
def test_concurrent_registers_are_unique(tmp_path: Path, kind: str) -> None:
    store = InMemoryPromptStore() if kind == "memory" else FilePromptStore(tmp_path)
    reg = PromptRegistry(store)
    results: list[int] = []
    errors: list[BaseException] = []

    def work(i: int) -> None:
        try:
            results.append(reg.register("p", f"template {i}").version)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(results) == list(range(1, 17))
    assert len(reg.versions("p")) == 16
    assert sum("latest" in v.labels for v in reg.versions("p")) == 1


def test_prompt_metadata_not_claimed_when_history_has_system() -> None:
    from nexura_agents import Agent, FakeModel, Runner
    from nexura_agents.types import Message

    reg = PromptRegistry()
    reg.register("p", "NEW")
    model = FakeModel(responses=["a"])
    agent = Agent(name="a", model=model, instructions=reg.instructions("p", label="latest"))
    result = Runner().run(agent, "hi", history=[Message(role="system", content="OLD")])
    assert str(model.calls[0]["messages"][0].content) == "OLD"
    assert result.prompt is None


def test_two_stores_same_directory_unique_versions(tmp_path: Path) -> None:
    regs = [PromptRegistry(FilePromptStore(tmp_path)) for _ in range(2)]
    out: list[int] = []

    def work(reg: PromptRegistry, k: int) -> None:
        for i in range(25):
            out.append(reg.register("p", f"t {k} {i}").version)

    threads = [threading.Thread(target=work, args=(r, k)) for k, r in enumerate(regs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(out) == list(range(1, 51))


def test_unclosed_tag_is_fast_and_size_capped() -> None:
    import time

    start = time.monotonic()
    with pytest.raises(PromptSyntaxError):
        PromptRegistry().register("p", "{{" * 200_000)
    with pytest.raises(PromptSyntaxError):
        PromptRegistry().register("p", "}}" * 100_000)
    with pytest.raises(PromptSyntaxError):
        PromptRegistry().register("p", "x" * 300_000)
    assert time.monotonic() - start < 2


def test_escaped_braces_are_literal() -> None:
    reg = PromptRegistry()
    v = reg.register("p", 'Reply as {"a": {"b": 1\\}} to \\{{ {{ who }}')
    assert v.variables == ("who",)
    assert v.render({"who": "bob"}) == 'Reply as {"a": {"b": 1}} to {{ bob'
