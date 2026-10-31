# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Offline data manipulation and malformed input coverage."""

import pytest
from nexura_agents.contracts.errors import SchemaValidationError


def test_json_options(run_component):
    assert run_component("nexura.data.json_parse", text='{"x": [1, null]}') == {
        "value": {"x": [1, None]}
    }
    for value in (None, True, 1, "hello", [1, 2]):
        text = run_component("nexura.data.json_stringify", value=value)["text"]
        assert run_component("nexura.data.json_parse", text=text)["value"] == value
    assert (
        run_component("nexura.data.json_stringify", value={"b": 2, "a": 1}, sort_keys=True)["text"]
        == '{"a":1,"b":2}'
    )
    assert (
        run_component("nexura.data.json_stringify", value={"a": 1}, pretty=True)["text"]
        == '{\n  "a": 1\n}'
    )


def test_csv_options_roundtrip(run_component):
    text = 'name;note\nAda;"hello;\nworld"\n'
    items = run_component("nexura.data.csv_parse", text=text, delimiter=";", max_rows=1)["items"]
    assert items == [{"name": "Ada", "note": "hello;\nworld"}]
    assert (
        run_component("nexura.data.csv_build", items=items, fields=["name", "note"], delimiter=";")[
            "text"
        ]
        == text
    )
    assert (
        run_component("nexura.data.csv_build", items=[{"b": 2}, {"a": 1}])["text"]
        == "a,b\n,2\n1,\n"
    )
    assert run_component("nexura.data.csv_build", items=[])["text"] == ""
    assert run_component("nexura.data.csv_parse", text="a,b\n")["items"] == []


@pytest.mark.parametrize(
    ("operator", "value", "expected"),
    [
        ("eq", 2, [2]),
        ("ne", 2, [1, 3]),
        ("gt", 2, [3]),
        ("gte", 2, [2, 3]),
        ("lt", 2, [1]),
        ("lte", 2, [1, 2]),
        ("in", [1, 3], [1, 3]),
    ],
)
def test_filter_operators(run_component, operator, value, expected):
    assert (
        run_component("nexura.data.filter", items=[1, 2, 3], operator=operator, value=value)[
            "items"
        ]
        == expected
    )


def test_collections_and_fields(run_component):
    items = [
        {"user": {"score": 2}, "id": "first"},
        {"user": {"score": 1}, "id": "second"},
        {"user": {"score": 2}, "id": "third"},
    ]
    assert run_component(
        "nexura.data.filter", items=items, field="user.score", operator="eq", value=2
    )["items"] == [items[0], items[2]]
    assert run_component("nexura.data.filter", items=["ab", "cd"], operator="contains", value="a")[
        "items"
    ] == ["ab"]
    assert run_component("nexura.data.filter", items=["ab", "cd"], operator="regex", value="^a")[
        "items"
    ] == ["ab"]
    assert run_component("nexura.data.sort", items=items, field="user.score")["items"] == [
        items[1],
        items[0],
        items[2],
    ]
    assert run_component("nexura.data.sort", items=items, field="user.score", order="desc")[
        "items"
    ] == [items[0], items[2], items[1]]
    assert run_component("nexura.data.sort", items=[2, 1])["items"] == [1, 2]
    assert run_component("nexura.data.pick", data=items[0], fields=["user.score", "missing"])[
        "data"
    ] == {"user": {"score": 2}, "missing": None}
    assert run_component("nexura.data.set_field", data=items[0], field="user.name", value="Ada")[
        "data"
    ]["user"] == {"score": 2, "name": "Ada"}
    assert (
        run_component("nexura.data.dedupe", items=items, field="user.score")["items"] == items[:2]
    )
    assert run_component("nexura.data.dedupe", items=[{"a": 1, "b": 2}, {"b": 2, "a": 1}, True, 1])[
        "items"
    ] == [{"a": 1, "b": 2}, True, 1]
    assert run_component("nexura.data.group_by", items=items, field="user.score")["groups"] == [
        {"key": 2, "items": [items[0], items[2]], "count": 2},
        {"key": 1, "items": [items[1]], "count": 1},
    ]


def test_flatten_roundtrip(run_component):
    data = {"a": {"b": 1, "empty": {}}, "list": [{"x": 2}], "null": None}
    flat = run_component("nexura.data.flatten", data=data)["data"]
    assert flat == {"a.b": 1, "a.empty": {}, "list": [{"x": 2}], "null": None}
    assert run_component("nexura.data.flatten", data=flat, mode="unflatten")["data"] == data


def test_flatten_nonadjacent_parent_collision(run_component):
    with pytest.raises(SchemaValidationError, match="conflict"):
        run_component("nexura.data.flatten", mode="unflatten", data={"a": {}, "a-b": 2, "a.b": 3})


def test_collection_regex_budget_and_typed_groups(run_component):
    with pytest.raises(SchemaValidationError, match="50ms"):
        run_component(
            "nexura.data.filter", items=["a" * 30000 + "!"], operator="regex", value="(a+)+$"
        )
    assert run_component("nexura.data.group_by", items=[{"k": 1}, {"k": "1"}, {}], field="k")[
        "groups"
    ] == [
        {"key": 1, "items": [{"k": 1}], "count": 1},
        {"key": "1", "items": [{"k": "1"}], "count": 1},
        {"key": None, "items": [{}], "count": 1},
    ]


@pytest.mark.parametrize(
    ("name", "inputs"),
    [
        ("json_parse", {"text": "{broken"}),
        ("json_parse", {"text": "NaN"}),
        ("json_parse", {"text": "[" * 2000}),
        ("csv_parse", {"text": ""}),
        ("csv_parse", {"text": "a,a\n1,2"}),
        ("csv_parse", {"text": "a,b\n1"}),
        ("csv_parse", {"text": 'a\n"unclosed'}),
        ("csv_parse", {"text": "a\n1\n2", "max_rows": 1}),
        ("csv_parse", {"text": "a", "delimiter": "\n"}),
        ("csv_build", {"items": [{"a": []}]}),
        ("csv_build", {"items": [], "fields": ["a", "a"]}),
        ("sort", {"items": [{}, {}], "field": "x"}),
        ("sort", {"items": [1, "a"]}),
        ("filter", {"items": [1], "operator": "gt", "value": "bad"}),
        ("filter", {"items": ["a"], "operator": "regex", "value": "["}),
        ("set_field", {"data": {"a": 1}, "field": "a.b", "value": 2}),
        ("set_field", {"data": {}, "field": "a..b", "value": 2}),
        ("flatten", {"data": {"a.b": 1}}),
        ("flatten", {"data": {"a": 1, "a.b": 2}, "mode": "unflatten"}),
    ],
)
def test_data_errors(run_component, name, inputs):
    with pytest.raises(SchemaValidationError):
        run_component("nexura.data." + name, **inputs)
