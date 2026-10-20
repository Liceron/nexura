"""Dict-schema output types and native structured-output mode (offline: fakes and mock HTTP)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from nexura_agents import (
    Agent,
    FakeModel,
    LiteLLMModel,
    Message,
    OutputValidationError,
    RunEventType,
    ToolCall,
    tool,
)
from nexura_agents.errors import SDKValidationError
from nexura_agents.json_schema import (
    inline_refs,
    to_gemini_schema,
    to_openai_schema,
    validate_instance,
)
from nexura_agents.models.openai import OpenAIChatModel
from nexura_agents.models.resilience import RetryModel
from nexura_agents.providers.anthropic import AnthropicNativeProvider
from nexura_agents.providers.gemini import GeminiNativeProvider
from nexura_agents.providers.openai import OpenAIProvider
from nexura_agents.teams import SequentialAgent
from pydantic import BaseModel, Field

TICKET: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1},
        "priority": {"type": "integer", "minimum": 1, "maximum": 5},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
        "owner": {"type": ["string", "null"]},
    },
    "required": ["title", "priority"],
    "additionalProperties": False,
}


class Point(BaseModel):
    x: int
    y: int


class Shape(BaseModel):
    name: str
    origin: Point
    corners: list[Point] = Field(default_factory=list)


def make(model: Any, **kwargs: Any) -> Agent:
    return Agent(name="A", instructions="Be brief.", model=model, **kwargs)


# --- dict output_type: validation ---------------------------------------------------------


def test_dict_output_type_returns_parsed_json() -> None:
    model = FakeModel(responses=['```json\n{"title": "Router", "priority": 2}\n```'])
    result = make(model, output_type=TICKET).run("go")
    assert result.output == {"title": "Router", "priority": 2}
    assert isinstance(result.output, dict)
    assert "Router" in result.final_output


def test_dict_output_prompt_contains_schema_for_plain_models() -> None:
    model = FakeModel(responses=['{"title": "t", "priority": 1}'])
    make(model, output_type=TICKET).run("go")
    system = model.calls[0]["messages"][0].content
    assert json.dumps(TICKET, ensure_ascii=False) in system
    assert model.calls[0]["response_schema"] is None


def test_dict_output_retries_with_specific_feedback() -> None:
    model = FakeModel(
        responses=[
            '{"title": "", "priority": 9, "extra": 1}',
            "not json at all",
            '{"title": "ok", "priority": 3, "tags": ["a"]}',
        ]
    )
    agent = make(model, output_type=TICKET, output_retries=2)
    result = agent.run("go")
    assert result.output == {"title": "ok", "priority": 3, "tags": ["a"]}
    assert [e.type for e in result.events].count(RunEventType.RETRY) == 2
    first = model.calls[1]["messages"][-1].content
    assert "did not match" in first
    assert "$.title: text is shorter than minLength 1" in first
    assert "$.priority: 9 is greater than the maximum 5" in first
    assert "unexpected property 'extra'" in first
    assert "not valid JSON" in model.calls[2]["messages"][-1].content


def test_dict_output_failure_after_retries() -> None:
    agent = make(FakeModel(responses=["{}", "{}"]), output_type=TICKET, output_retries=1)
    with pytest.raises(OutputValidationError) as info:
        agent.run("go")
    assert "missing required property 'title'" in str(info.value)
    assert info.value.attempts == 2


def test_validator_covers_types_enum_const_refs_and_bools() -> None:
    schema = {
        "type": "object",
        "properties": {
            "n": {"type": "number"},
            "i": {"type": "integer"},
            "e": {"enum": ["a", "b"]},
            "c": {"const": 1},
            "ref": {"$ref": "#/$defs/pt"},
            "u": {"type": ["integer", "null"]},
            "m": {"type": "object", "additionalProperties": {"type": "string"}},
        },
        "$defs": {"pt": {"type": "object", "properties": {"x": {"type": "integer"}}}},
    }
    assert validate_instance(schema, {"n": 1.5, "i": 2, "e": "a", "c": 1, "u": None}) == []
    errors = validate_instance(
        schema, {"n": True, "i": 1.5, "e": "z", "c": True, "ref": {"x": "s"}, "m": {"k": 1}}
    )
    text = "\n".join(errors)
    assert "$.n: expected number, got boolean" in text
    assert "$.i: expected integer, got number" in text
    assert "$.e: 'z' is not one of" in text
    assert "$.c: expected the constant 1" in text
    assert "$.ref.x: expected integer, got string" in text
    assert validate_instance(schema, []) == ["$: expected object, got array"]


@pytest.mark.parametrize(
    ("schema", "needle"),
    [
        ({"type": "object", "patternProperties": {}}, "patternProperties"),
        ({"type": "string", "pattern": "^a"}, "'pattern'"),
        ({"anyOf": [{"type": "string"}]}, "anyOf"),
        ({"type": "object", "properties": {"a": {"type": "string", "format": "email"}}}, "format"),
        ({"type": "object", "properties": {"a": {"$ref": "http://x/y"}}}, "$ref"),
        ({"type": "object", "properties": {"a": {"$ref": "#/$defs/missing"}}}, "missing"),
        ({"type": "strin"}, "'type'"),
        ({"type": "array", "items": {"type": "string", "uniqueItems": True}}, "uniqueItems"),
        ({"type": "object", "required": "a"}, "'required'"),
    ],
)
def test_unsupported_schema_rejected_at_construction(schema: dict[str, Any], needle: str) -> None:
    with pytest.raises(SDKValidationError) as info:
        make(FakeModel(), output_type=schema)
    assert needle in str(info.value)


def test_non_schema_output_type_rejected() -> None:
    with pytest.raises(SDKValidationError):
        make(FakeModel(), output_type="nope")  # type: ignore[arg-type]
    with pytest.raises(SDKValidationError):
        make(FakeModel(), output_type=int)  # type: ignore[arg-type]


def test_array_root_dict_schema() -> None:
    schema = {"type": "array", "items": {"type": "integer"}, "minItems": 1}
    result = make(FakeModel(responses=["[1, 2, 3]"]), output_type=schema).run("go")
    assert result.output == [1, 2, 3]


# --- native mode: runner wiring ----------------------------------------------------------


def test_pydantic_schema_reaches_opted_in_fake_model() -> None:
    model = FakeModel(responses=['{"name": "s", "origin": {"x": 1, "y": 2}}'], native_schema=True)
    result = make(model, output_type=Shape).run("go")
    assert result.output == Shape(name="s", origin=Point(x=1, y=2))
    assert model.schemas == [Shape.model_json_schema()]
    system = model.calls[0]["messages"][0].content
    assert "Be brief." in system
    assert "$defs" not in system  # short instruction, no full schema
    assert "required output schema" in system


def test_dict_schema_reaches_opted_in_fake_model() -> None:
    model = FakeModel(responses=['{"title": "t", "priority": 1}'], native_schema=True)
    result = make(model, output_type=TICKET).run("go")
    assert model.schemas == [TICKET]
    assert result.output == {"title": "t", "priority": 1}


def test_native_retry_keeps_sending_schema() -> None:
    model = FakeModel(responses=["{}", '{"title": "t", "priority": 1}'], native_schema=True)
    make(model, output_type=TICKET).run("go")
    assert model.schemas == [TICKET, TICKET]


def test_no_schema_without_output_type() -> None:
    model = FakeModel(responses=["hi"], native_schema=True)
    make(model).run("go")
    assert model.schemas == [None]


def test_non_opted_in_model_still_gets_prompt_schema() -> None:
    model = FakeModel(responses=['{"name": "s", "origin": {"x": 1, "y": 2}}'])
    make(model, output_type=Shape).run("go")
    assert model.calls[0]["response_schema"] is None
    assert json.dumps(Shape.model_json_schema(), ensure_ascii=False) in (
        model.calls[0]["messages"][0].content
    )


def test_user_defined_model_without_response_schema_keyword_still_works() -> None:
    class Plain:
        async def generate(
            self,
            messages: list[Message],
            tools: list[dict[str, Any]] | None = None,
            *,
            timeout: float | None = None,
        ) -> Any:
            return await FakeModel(responses=['{"x": 1, "y": 2}']).generate(messages, tools)

    result = make(Plain(), output_type=Point).run("go")
    assert result.output == Point(x=1, y=2)


def test_tools_keep_prompt_schema_unless_model_allows_both() -> None:
    @tool
    def lookup(q: str) -> str:
        """Look something up."""
        return q

    calls = [ToolCall(id="1", name="lookup", arguments={"q": "a"})]
    plain = FakeModel(responses=[calls, '{"x": 1, "y": 2}'], native_schema=True)
    make(plain, tools=[lookup], output_type=Point).run("go")
    assert plain.schemas == [None, None]
    assert "properties" in plain.calls[0]["messages"][0].content

    both = FakeModel(
        responses=[calls, '{"x": 1, "y": 2}'], native_schema=True, schema_with_tools=True
    )
    make(both, tools=[lookup], output_type=Point).run("go")
    assert both.schemas == [Point.model_json_schema()] * 2
    assert "properties" not in both.calls[0]["messages"][0].content


def test_retry_wrapper_forwards_opt_in() -> None:
    inner = FakeModel(responses=['{"x": 1, "y": 2}'], native_schema=True)
    result = make(RetryModel(inner), output_type=Point).run("go")
    assert result.output == Point(x=1, y=2)
    assert inner.schemas == [Point.model_json_schema()]


def test_handoff_final_agent_output_type_wins() -> None:
    specialist_model = FakeModel(responses=['{"title": "t", "priority": 2}'], native_schema=True)
    specialist = Agent(
        name="Specialist", instructions="x", model=specialist_model, output_type=TICKET
    )
    triage_model = FakeModel(
        responses=[[ToolCall(id="h", name="transfer_to_specialist", arguments={})]],
        native_schema=True,
    )
    triage = Agent(
        name="Triage",
        instructions="route",
        model=triage_model,
        output_type=Point,
        handoffs=[specialist],
    )
    result = triage.run("go")
    assert result.output == {"title": "t", "priority": 2}
    assert specialist_model.schemas == [TICKET]
    # Triage offers a handoff tool, so its own schema stays in the prompt.
    assert triage_model.schemas == [None]


def test_as_tool_and_team_return_dict_outputs() -> None:
    inner = Agent(
        name="Inner",
        instructions="x",
        model=FakeModel(responses=['{"title": "t", "priority": 2}']),
        output_type=TICKET,
    )
    outer_model = FakeModel(
        responses=[
            [ToolCall(id="1", name="inner", arguments={"request": "make one"})],
            "done",
        ]
    )
    outer = Agent(name="Outer", instructions="x", model=outer_model, tools=[inner.as_tool()])
    outer.run("go")
    tool_msg = outer_model.calls[1]["messages"][-1]
    assert tool_msg.role == "tool" and json.loads(tool_msg.content) == {
        "title": "t",
        "priority": 2,
    }

    producer = Agent(
        name="P",
        instructions="x",
        model=FakeModel(responses=['{"title": "t", "priority": 2}']),
        output_type=TICKET,
    )
    result = SequentialAgent("Solo", [producer]).run("go")
    assert result.output == {"title": "t", "priority": 2}

    fresh = Agent(
        name="P2",
        instructions="x",
        model=FakeModel(responses=['{"title": "u", "priority": 4}']),
        output_type=TICKET,
    )
    team_model = FakeModel(
        responses=[[ToolCall(id="1", name="again", arguments={"request": "x"})], "done"]
    )
    boss = Agent(
        name="Boss",
        instructions="x",
        model=team_model,
        tools=[SequentialAgent("Again", [fresh]).as_tool()],
    )
    boss.run("go")
    assert json.loads(team_model.calls[1]["messages"][-1].content) == {
        "title": "u",
        "priority": 4,
    }


# --- schema dialect helpers --------------------------------------------------------------


def test_openai_strict_chosen_only_when_safe() -> None:
    schema, strict = to_openai_schema(Shape.model_json_schema())
    assert strict is False  # `corners` has a default, so it is not required
    assert schema == Shape.model_json_schema()

    class Full(BaseModel):
        name: str
        origin: Point

    schema, strict = to_openai_schema(Full.model_json_schema())
    assert strict is True
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["Point"]["additionalProperties"] is False
    assert "additionalProperties" not in Full.model_json_schema()  # input is not mutated

    assert to_openai_schema(TICKET)[1] is False  # optional props, constraints
    assert to_openai_schema({"type": "array", "items": {"type": "string"}})[1] is False
    free = {"type": "object", "properties": {"m": {"type": "object"}}, "required": ["m"]}
    assert to_openai_schema(free)[1] is False


def test_gemini_schema_inlines_refs_and_drops_unsupported_keywords() -> None:
    class Opt(BaseModel):
        shape: Shape
        note: str | None = None
        kind: str = "a"

    converted = to_gemini_schema(Opt.model_json_schema())
    text = json.dumps(converted)
    assert "$ref" not in text and "$defs" not in text
    assert "title" not in text and "default" not in text and "additionalProperties" not in text
    assert converted["properties"]["shape"]["properties"]["origin"]["properties"]["x"] == {
        "type": "integer"
    }
    assert converted["properties"]["note"] == {"type": "string", "nullable": True}

    nullable = to_gemini_schema(
        {"type": "object", "properties": {"a": {"type": ["string", "null"], "minLength": 2}}}
    )
    assert nullable["properties"]["a"] == {"type": "string", "nullable": True}
    assert to_gemini_schema({"const": "x", "type": "string"}) == {"enum": ["x"], "type": "string"}


def test_inline_refs_breaks_recursion() -> None:
    schema = {
        "type": "object",
        "properties": {"child": {"$ref": "#/$defs/Node"}},
        "$defs": {"Node": {"type": "object", "properties": {"next": {"$ref": "#/$defs/Node"}}}},
    }
    out = inline_refs(schema)
    assert "$ref" not in json.dumps(out)
    assert out["properties"]["child"]["properties"]["next"] == {"type": "object"}


# --- provider payloads (mock HTTP) -------------------------------------------------------


def openai_reply(text: str) -> dict[str, Any]:
    return {
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]
    }


def capture(reply: dict[str, Any]) -> tuple[list[dict[str, Any]], httpx.MockTransport]:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=reply)

    return seen, httpx.MockTransport(handler)


class Full(BaseModel):
    name: str
    origin: Point


FULL_JSON = '{"name": "s", "origin": {"x": 1, "y": 2}}'


def test_openai_provider_sends_strict_response_format() -> None:
    seen, transport = capture(openai_reply(FULL_JSON))
    provider = OpenAIProvider("gpt-x", api_key="k", transport=transport)
    result = make(provider, output_type=Full).run("go")
    assert result.output == Full(name="s", origin=Point(x=1, y=2))
    fmt = seen[0]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"]["additionalProperties"] is False
    assert fmt["json_schema"]["schema"]["properties"]["name"] == {"title": "Name", "type": "string"}
    assert "$defs" in fmt["json_schema"]["schema"]
    assert "properties" not in seen[0]["messages"][0]["content"]


def test_openai_provider_dict_schema_non_strict_and_with_tools() -> None:
    seen, transport = capture(openai_reply('{"title": "t", "priority": 1}'))
    provider = OpenAIProvider("gpt-x", api_key="k", transport=transport)

    @tool
    def lookup(q: str) -> str:
        """Look something up."""
        return q

    result = make(provider, output_type=TICKET, tools=[lookup]).run("go")
    assert result.output == {"title": "t", "priority": 1}
    assert seen[0]["response_format"]["json_schema"] == {
        "name": "output_schema",
        "schema": TICKET,
        "strict": False,
    }
    assert "tools" in seen[0]


def test_openai_chat_model_response_format_and_no_tools_combination() -> None:
    seen, transport = capture(openai_reply(FULL_JSON))
    model = OpenAIChatModel("gpt-x", transport=transport)
    make(model, output_type=Full).run("go")
    assert seen[0]["response_format"]["json_schema"]["strict"] is True

    @tool
    def lookup(q: str) -> str:
        """Look something up."""
        return q

    seen2, transport2 = capture(openai_reply(FULL_JSON))
    make(OpenAIChatModel("gpt-x", transport=transport2), output_type=Full, tools=[lookup]).run("go")
    assert "response_format" not in seen2[0]
    assert "properties" in seen2[0]["messages"][0]["content"]  # schema stays in the prompt


def test_openai_chat_model_without_output_type_sends_no_response_format() -> None:
    seen, transport = capture(openai_reply("hi"))
    make(OpenAIChatModel("gpt-x", transport=transport)).run("go")
    assert "response_format" not in seen[0]


def test_gemini_provider_sends_response_schema_with_refs_inlined() -> None:
    class Doc(BaseModel):
        shape: Full
        label: str | None = Field(default=None, max_length=5)

    seen_reply = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [{"text": '{"shape": {"name": "s", "origin": {"x": 1, "y": 2}}}'}],
                },
                "finishReason": "STOP",
            }
        ]
    }
    seen, transport = capture(seen_reply)
    provider = GeminiNativeProvider("gemini-2.5-flash", api_key="k", transport=transport)
    result = make(provider, output_type=Doc).run("go")
    assert result.output is not None and result.output.shape.origin.y == 2
    config = seen[0]["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    sent = config["responseSchema"]
    text = json.dumps(sent)
    assert "$ref" not in text and "$defs" not in text
    assert "maxLength" not in text and "title" not in text and "default" not in text
    assert sent["properties"]["shape"]["properties"]["origin"]["properties"]["x"] == {
        "type": "integer"
    }
    assert sent["properties"]["label"]["nullable"] is True


def test_gemini_provider_with_tools_keeps_prompt_schema() -> None:
    reply = {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": FULL_JSON}]}, "finishReason": "STOP"}
        ]
    }
    seen, transport = capture(reply)
    provider = GeminiNativeProvider("gemini-2.5-flash", api_key="k", transport=transport)

    @tool
    def lookup(q: str) -> str:
        """Look something up."""
        return q

    make(provider, output_type=Full, tools=[lookup]).run("go")
    assert "responseSchema" not in seen[0].get("generationConfig", {})
    assert "origin" in json.dumps(seen[0]["contents"]) + json.dumps(
        seen[0].get("systemInstruction", "")
    )


def anthropic_tool_reply(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-x",
        "content": [{"type": "tool_use", "id": "tu_1", "name": "structured_output", "input": args}],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 3, "output_tokens": 4},
    }


def test_anthropic_forces_synthetic_tool_and_returns_its_arguments_as_text() -> None:
    seen, transport = capture(anthropic_tool_reply({"name": "s", "origin": {"x": 1, "y": 2}}))
    provider = AnthropicNativeProvider("claude-x", api_key="k", transport=transport)
    result = make(provider, output_type=Full).run("go")
    assert result.output == Full(name="s", origin=Point(x=1, y=2))
    assert json.loads(result.final_output) == {"name": "s", "origin": {"x": 1, "y": 2}}
    payload = seen[0]
    assert payload["tool_choice"] == {"type": "tool", "name": "structured_output"}
    assert [t["name"] for t in payload["tools"]] == ["structured_output"]
    assert payload["tools"][0]["input_schema"] == Full.model_json_schema()


def test_anthropic_wraps_non_object_schema() -> None:
    schema = {"type": "array", "items": {"type": "integer"}}
    seen, transport = capture(anthropic_tool_reply({"value": [1, 2]}))
    provider = AnthropicNativeProvider("claude-x", api_key="k", transport=transport)
    result = make(provider, output_type=schema).run("go")
    assert result.output == [1, 2]
    assert seen[0]["tools"][0]["input_schema"]["properties"] == {"value": schema}


def test_anthropic_with_tools_keeps_prompt_schema() -> None:
    reply = {
        "id": "m",
        "type": "message",
        "role": "assistant",
        "model": "claude-x",
        "content": [{"type": "text", "text": FULL_JSON}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    seen, transport = capture(reply)
    provider = AnthropicNativeProvider("claude-x", api_key="k", transport=transport)

    @tool
    def lookup(q: str) -> str:
        """Look something up."""
        return q

    result = make(provider, output_type=Full, tools=[lookup]).run("go")
    assert result.output == Full(name="s", origin=Point(x=1, y=2))
    assert "tool_choice" not in seen[0]
    assert [t["name"] for t in seen[0]["tools"]] == ["lookup"]


def test_litellm_passes_response_format() -> None:
    calls: list[dict[str, Any]] = []

    async def completion(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return openai_reply(FULL_JSON)

    model = LiteLLMModel("openai/gpt-x", completion=completion)
    result = make(model, output_type=Full).run("go")
    assert result.output == Full(name="s", origin=Point(x=1, y=2))
    fmt = calls[0]["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"]["properties"]["origin"] == {"$ref": "#/$defs/Point"}

    calls.clear()

    async def ticket_completion(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return openai_reply('{"title": "t", "priority": 1}')

    dict_model = LiteLLMModel("openai/gpt-x", completion=ticket_completion)
    make(dict_model, output_type=TICKET).run("go")
    assert calls[0]["response_format"]["json_schema"]["strict"] is False


def test_ref_siblings_are_validated() -> None:
    from nexura_agents.json_schema import validate_instance

    s = {
        "$defs": {"n": {"type": "integer"}},
        "type": "object",
        "properties": {"x": {"$ref": "#/$defs/n", "maximum": 5}},
        "required": ["x"],
    }
    assert validate_instance(s, {"x": 9})
    assert not validate_instance(s, {"x": 3})


def test_gemini_keeps_property_named_definitions() -> None:
    from nexura_agents.json_schema import to_gemini_schema

    s = {
        "type": "object",
        "properties": {"definitions": {"type": "string"}, "a": {"type": "string"}},
        "required": ["definitions", "a"],
    }
    assert set(to_gemini_schema(s)["properties"]) == {"definitions", "a"}


def test_openai_strict_property_named_properties() -> None:
    from nexura_agents.json_schema import to_openai_schema

    s = {
        "type": "object",
        "properties": {"properties": {"type": "string"}},
        "required": ["properties"],
    }
    out, strict = to_openai_schema(s)
    assert strict
    assert out["properties"] == {"properties": {"type": "string"}}
    assert out["additionalProperties"] is False


def test_anthropic_wrapped_schema_hoists_defs() -> None:
    from nexura_agents.providers.anthropic.provider import _structured_output_tool

    s = {"type": "array", "items": {"$ref": "#/$defs/x"}, "$defs": {"x": {"type": "string"}}}
    t = _structured_output_tool(s)["input_schema"]
    assert t["$defs"] == {"x": {"type": "string"}}
    assert "$defs" not in t["properties"]["value"]
