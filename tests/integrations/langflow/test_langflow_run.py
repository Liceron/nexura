"""Build and run imported flows with FakeModel and mock transports. No network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from nexura_agents import Agent, FakeModel, MemorySessionStore, ProviderError, ToolCall

from nexura_runtime.integrations.langflow import (
    FlowBuildError,
    MissingVariablesError,
    ModelMappingError,
    ModelRef,
    TweakRefusedError,
    build,
    load,
    run_flow,
    run_flow_async,
    safe_eval,
)

FIX = Path(__file__).parent / "fixtures"


def imp(name: str, **kw: Any) -> Any:
    return load(json.loads((FIX / f"{name}.json").read_text(encoding="utf-8")), **kw)


def seen_by(model: FakeModel) -> list[str]:
    """Text of the user messages the model received in its last call."""
    return [m.content or "" for m in model.calls[-1]["messages"] if m.role == "user"]


async def test_minimal_chat() -> None:
    model = FakeModel(["Hello there"])
    built = build(imp("f1_minimal_chat"), models=model)
    result = await built.run_async("hi")
    assert result.output == "Hello there"
    assert result.outputs == {"ChatOutput-z9": "Hello there"}
    assert seen_by(model) == ["hi"]
    assert built.agents["Agent-b2"].instructions == "You are helpful."


def test_sync_helpers() -> None:
    assert run_flow(imp("f1_minimal_chat"), "x", models=FakeModel(["ok"])) == "ok"


async def test_run_flow_async_helper() -> None:
    out = await run_flow_async(imp("f1_minimal_chat"), "x", models=FakeModel(["fine"]))
    assert out == "fine"


async def test_chained_agents_are_a_pipeline() -> None:
    first, second = FakeModel(["facts: A, B"]), FakeModel(["Summary of A and B"])
    built = build(imp("f3_chained_agents"), models={"Agent-b2": first, "Agent-c3": second})
    result = await built.run_async("topic")
    assert result.output == "Summary of A and B"
    assert seen_by(second) == ["facts: A, B"]  # first agent's response is second's input
    assert built.order == ["ChatInput-a1", "Agent-b2", "Agent-c3", "ChatOutput-z9"]
    assert built.agents["Agent-c3"].handoffs == [] or not built.agents["Agent-c3"].handoffs


async def test_handle_variants_run() -> None:
    m1, m2 = FakeModel(["one"]), FakeModel(["two"])
    r = await build(imp("f5_handle_variants"), models={"Agent-b2": m1, "Agent-c3": m2}).run_async(
        "q"
    )
    assert r.output == "two" and seen_by(m2) == ["one"]


async def test_prompt_text_input_and_parser_wiring() -> None:
    model = FakeModel(["the answer"])
    r = await build(imp("f4_prompt_agent"), models=model).run_async("why?")
    assert seen_by(model) == ["Answer in a brief way: why?"]
    assert r.output == "FINAL: the answer"
    assert r.node_outputs["Prompt-p1"]["prompt"] == "Answer in a brief way: why?"


async def test_tools_calculator_url_and_search() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="<html><body>docs page</body></html>", headers={"content-type": "text/html"}
        )

    calls = [
        [ToolCall("c1", "calculator", {"expression": "2 * (3 + 4)"})],
        [ToolCall("c2", "web_search", {"query": "nexura"})],
        "done",
    ]
    model = FakeModel(calls)
    built = build(imp("f2_tools"), models=model, http_transport=httpx.MockTransport(handler))
    assert sorted(t.name for t in built.agents["Agent-b2"].tools) == [
        "calculator",
        "fetch_url",
        "web_search",
    ]
    r = await built.run_async("go")
    assert r.output == "done"
    tool_msgs = [m.content for m in model.calls[-1]["messages"] if m.role == "tool"]
    assert '"result": 14' in tool_msgs[0]
    assert "web_search_not_configured" in tool_msgs[1]  # default search refuses


async def test_search_provider_is_pluggable() -> None:
    seen: list[str] = []

    async def provider(query: str) -> list[dict[str, str]]:
        seen.append(query)
        return [{"title": "T", "url": "https://example.test"}]

    model = FakeModel([[ToolCall("c1", "web_search", {"query": "q1"})], "ok"])
    await build(imp("f2_tools"), models=model, search_provider=provider).run_async("go")
    assert seen == ["q1"]


async def test_url_tool_restricted_to_flow_domains() -> None:
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

    model = FakeModel([[ToolCall("c1", "fetch_url", {"url": "https://evil.test/x"})], "done"])
    await build(
        imp("f2_tools"), models=model, http_transport=httpx.MockTransport(handler)
    ).run_async("go")
    assert hits == []  # domain not in the flow's urls list: refused before any request


async def test_tool_override_by_name() -> None:
    from nexura_agents import tool

    @tool(name="calculator")
    def fake_calc(expression: str) -> str:
        return "always 42"

    model = FakeModel([[ToolCall("c1", "calculator", {"expression": "1+1"})], "ok"])
    await build(imp("f2_tools"), models=model, tools={"CalculatorComponent": fake_calc}).run_async(
        "x"
    )
    assert any("always 42" in (m.content or "") for m in model.calls[-1]["messages"])


def test_safe_eval_negative() -> None:
    assert safe_eval("2**3 + 7 // 2") == 11
    for bad in ["__import__('os')", "a + 1", "2 ** 100000", "(1).real", "1 +"]:
        with pytest.raises((ValueError, SyntaxError)):
            safe_eval(bad)


async def test_language_model_step_and_structured_output() -> None:
    lm = FakeModel(["one line"])
    r = await build(imp("f9_language_model_step"), models=lm).run_async("hello")
    assert r.output == "one line"
    assert lm.calls[-1]["messages"][0].content == "Answer in one line."

    structured = FakeModel(['{"name": "Ada", "age": 36}'])
    built = build(imp("f9_structured_lm"), models=structured)
    r2 = await built.run_async("Ada is 36")
    assert json.loads(r2.output) == {"name": "Ada", "age": 36}
    schema = built.agents["StructuredOutput-s1"].output_schema
    assert schema is not None and schema["required"] == ["name", "age"]
    assert "LM-m1" in built.passive  # the model node only configures the extractor


async def test_sessions_keep_history() -> None:
    model = FakeModel(["a1", "a2"])
    store = MemorySessionStore()
    built = build(imp("f1_minimal_chat"), models=model)
    await built.run_async("first", session_id="s1", session_store=store)
    await built.run_async("second", session_id="s1", session_store=store)
    assert seen_by(model) == ["first", "second"]
    other = FakeModel(["x"])
    await build(imp("f1_minimal_chat"), models=other).run_async(
        "third", session_id="s2", session_store=store
    )
    assert seen_by(other) == ["third"]


async def test_session_without_store_uses_built_in_memory() -> None:
    model = FakeModel(["a1", "a2"])
    built = build(imp("f1_minimal_chat"), models=model)
    await built.run_async("first", session_id="s")
    await built.run_async("second", session_id="s")
    assert seen_by(model) == ["first", "second"]


async def test_no_session_means_no_history() -> None:
    model = FakeModel(["a1", "a2"])
    built = build(imp("f1_minimal_chat"), models=model)
    await built.run_async("first")
    await built.run_async("second")
    assert seen_by(model) == ["second"]


# ------------------------------------------------------------------ models and variables
def test_default_mapping_needs_key_variable_and_never_reads_flow() -> None:
    flow = imp("f1_minimal_chat")
    with pytest.raises(MissingVariablesError) as err:
        build(flow)
    assert err.value.names == ["OPENAI_API_KEY"]
    built = build(flow, variables={"OPENAI_API_KEY": "sk-test"})
    assert built.model_refs["Agent-b2"].spec == "openai:gpt-4o-mini"
    assert "sk-test" not in repr(built.model_refs["Agent-b2"])


def test_factory_receives_ref_with_supplied_key() -> None:
    got: list[ModelRef] = []

    def factory(ref: ModelRef) -> FakeModel:
        got.append(ref)
        return FakeModel(["x"])

    build(imp("f1_minimal_chat"), models=factory, variables={"OPENAI_API_KEY": "k"})
    assert (got[0].provider, got[0].name, got[0].api_key_variable, got[0].api_key) == (
        "OpenAI",
        "gpt-4o-mini",
        "OPENAI_API_KEY",
        "k",
    )
    got.clear()
    build(imp("f1_minimal_chat"), models=factory)  # a factory needs no key variable
    assert got[0].api_key is None


def test_unknown_provider_has_clear_error() -> None:
    doc = json.loads((FIX / "f1_minimal_chat.json").read_text(encoding="utf-8"))
    doc["data"]["nodes"][1]["data"]["node"]["template"]["model"]["value"][0]["provider"] = "Acme"
    with pytest.raises(ModelMappingError, match="Acme"):
        build(load(doc), variables={"OPENAI_API_KEY": "k"})
    build(load(doc), models={"Agent-b2": FakeModel(["x"])})  # mapping bypasses the table


def test_structured_flow_uses_anthropic_table() -> None:
    built = build(imp("f9_structured_lm"), variables={"ANTHROPIC_API_KEY": "k"})
    assert built.model_refs["StructuredOutput-s1"].spec == "anthropic:claude-3-5-haiku"


# ------------------------------------------------------------------ tweaks
def f7(**tw: Any) -> Any:
    return build(
        imp("f7_tweaks", strict=False),
        models=FakeModel(["ok"]),
        variables={"TONE_VAR": "calm"},
        tweaks=tw or None,
    )


async def test_tweaks_by_id_and_display_name() -> None:
    model = FakeModel(["ok"])
    built = build(
        imp("f7_tweaks", strict=False),
        models=model,
        variables={"TONE_VAR": "calm"},
        tweaks={"Agent-b2": {"system_prompt": "Be terse."}, "Prompt": {"template": "{question}!"}},
    )
    assert built.agents["Agent-b2"].instructions == "Be terse."
    await built.run_async("hi")
    assert seen_by(model) == ["hi!"]


async def test_tweak_literal_replaces_load_from_db_and_variable_required_otherwise() -> None:
    flow = imp("f7_tweaks", strict=False)
    with pytest.raises(MissingVariablesError, match="TONE_VAR"):
        build(flow, models=FakeModel(["x"]))
    model = FakeModel(["ok"])
    built = build(flow, models=model, tweaks={"TextInput-t1": {"input_value": "loud"}})
    await built.run_async("q")
    assert seen_by(model) == ["Q: q Tone: loud"]


async def test_global_non_dict_tweak_applies_where_field_exists() -> None:
    built = build(
        imp("f7_tweaks", strict=False),
        models=FakeModel(["ok"]),
        variables={"TONE_VAR": "c"},
        tweaks={"system_prompt": "Global.", "no_such_field_anywhere": 1},
    )
    assert built.agents["Agent-b2"].instructions == "Global."


def test_refusals_are_reported_together() -> None:
    with pytest.raises(TweakRefusedError) as err:
        f7(
            **{
                "Agent-b2": {"code": "print(1)", "system_prompt": "ok", "api_key": "sk-x"},
                "SQL-q1": {"database_url": "sqlite://x"},
                "Ghost-1": {"x": 1},
                "Prompt": {"nonexistent": 2},
                "query": "select",
            }
        )
    keys = dict(err.value.refused)
    assert set(keys) == {
        "Agent-b2.code",
        "Agent-b2.api_key",
        "SQL-q1",  # unsupported node: cannot be tweaked, and not silently ignored
        "Ghost-1",
        "Prompt.nonexistent",
        "query",
    }
    assert "protected" in keys["Agent-b2.code"] and "secret" in keys["Agent-b2.api_key"]


def test_ambiguous_display_name_refused() -> None:
    doc = json.loads((FIX / "f3_chained_agents.json").read_text(encoding="utf-8"))
    for node in doc["data"]["nodes"]:
        if node["id"].startswith("Agent"):
            node["data"]["node"]["display_name"] = "Twin"
    with pytest.raises(TweakRefusedError, match="several nodes"):
        build(
            load(doc),
            models=FakeModel(["x"]),
            variables={"OPENAI_API_KEY": "k"},
            tweaks={"Twin": {"system_prompt": "x"}},
        )


# ------------------------------------------------------------------ security
def test_malicious_code_is_never_executed(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = Path("/tmp/langflow-import-pwned")
    sentinel.unlink(missing_ok=True)
    import builtins

    def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("exec/compile/eval called")

    monkeypatch.setattr(builtins, "exec", boom)
    monkeypatch.setattr(builtins, "compile", boom)
    flow = imp("f6_malicious_code")
    assert all("code" not in n.fields for n in flow.nodes.values())
    result = build(flow, models=FakeModel(["fine"])).run("hi")
    assert result.output == "fine"
    assert not sentinel.exists()


# ------------------------------------------------------------------ build errors
def test_build_requires_chat_output_and_connected_input() -> None:
    doc = json.loads((FIX / "f1_minimal_chat.json").read_text(encoding="utf-8"))
    doc["data"]["edges"].pop()  # Agent no longer feeds ChatOutput
    with pytest.raises(FlowBuildError, match="no connected input"):
        build(load(doc), models=FakeModel(["x"]))


async def test_non_strict_build_runs_supported_part() -> None:
    flow = imp("f6_unsupported_file", strict=False)
    built = build(flow, models=FakeModel(["partial ok"]))
    assert any("File-f1" in w for w in built.warnings)
    assert (await built.run_async("x")).output == "partial ok"


async def test_agent_failure_surfaces_sdk_error() -> None:
    class Boom(FakeModel):
        async def generate(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("model exploded")

    with pytest.raises(ProviderError, match="model exploded"):
        await build(imp("f1_minimal_chat"), models=Boom()).run_async("x")


def test_agent_objects_are_plain_sdk_agents() -> None:
    built = build(imp("f2_tools"), models=FakeModel(["x"]))
    assert isinstance(built.agents["Agent-b2"], Agent)
