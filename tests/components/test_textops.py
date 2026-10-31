# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Text component behavior, all modes, malformed patterns and resource limits."""

import time

import pytest
from nexura_agents.contracts.errors import SchemaValidationError


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"text": "a,b,", "separator": ","}, ["a", "b", ""]),
        ({"text": "a\r\nb\n", "mode": "line"}, ["a", "b"]),
        ({"text": "One. Two! Three?", "mode": "sentence"}, ["One.", "Two!", "Three?"]),
        ({"text": "abcdefg", "mode": "fixed", "size": 4, "overlap": 1}, ["abcd", "defg"]),
        ({"text": "abcdef", "mode": "fixed", "size": 3}, ["abc", "def"]),
        ({"text": "", "mode": "fixed"}, []),
        ({"text": "   ", "mode": "sentence"}, []),
    ],
)
def test_split(run_component, options, expected):
    assert run_component("nexura.text.split", **options) == {"items": expected}


def test_join_and_replace(run_component):
    assert run_component("nexura.text.join", items=["a", "b"], separator="-")["text"] == "a-b"
    assert run_component("nexura.text.join", items=[])["text"] == ""
    assert (
        run_component(
            "nexura.text.replace", text="aaa", pattern="a", replacement="b", max_replacements=2
        )["text"]
        == "bba"
    )
    assert (
        run_component(
            "nexura.text.replace",
            text="a1 a2",
            pattern=r"a(\d)",
            replacement=r"b\1",
            mode="regex",
            max_replacements=1,
        )["text"]
        == "b1 a2"
    )


def test_regex_modes(run_component):
    assert run_component("nexura.text.regex_extract", text="12 34", pattern=r"\d+") == {
        "matches": ["12", "34"]
    }
    result = run_component(
        "nexura.text.regex_extract",
        text="x=1 y=2",
        pattern=r"(?P<key>\w)=(?P<value>\d)",
        mode="named",
    )
    assert result == {"matches": [{"key": "x", "value": "1"}, {"key": "y", "value": "2"}]}
    assert run_component("nexura.text.regex_extract", text="none", pattern=r"\d+") == {
        "matches": []
    }


def test_regex_replacement_groups_escapes_and_expansion_limits(run_component):
    assert (
        run_component(
            "nexura.text.replace",
            text="a1",
            pattern=r"a(?P<n>\d)",
            replacement=r"\g<n>\n\g<0>",
            mode="regex",
        )["text"]
        == "1\na1"
    )
    for replacement in (r"\g<missing>", r"\2", r"\x", "x" * 65536):
        with pytest.raises(SchemaValidationError):
            run_component(
                "nexura.text.replace", text="aa", pattern="a", replacement=replacement, mode="regex"
            )
    with pytest.raises(SchemaValidationError, match="expansion"):
        run_component(
            "nexura.text.replace",
            text="a" * 40000,
            pattern="(a+)",
            replacement=r"\1\1",
            mode="regex",
        )
    with pytest.raises(SchemaValidationError, match="output"):
        run_component("nexura.text.replace", text="aa", pattern="a", replacement="x" * 40000)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("lower", "hello world"),
        ("upper", "HELLO WORLD"),
        ("title", "Hello World"),
        ("snake", "hello_world"),
        ("kebab", "hello-world"),
        ("camel", "helloWorld"),
    ],
)
def test_case(run_component, mode, expected):
    assert run_component("nexura.text.case", text="hello WORLD", mode=mode)["text"] == expected
    assert run_component("nexura.text.case", text="", mode=mode)["text"] == ""


def test_case_boundaries_and_clean_options(run_component):
    assert (
        run_component("nexura.text.case", text="HTTPResponseCode", mode="snake")["text"]
        == "http_response_code"
    )
    assert run_component("nexura.text.clean", text="  e\u0301\x00 \n a  ")["text"] == "é a"
    assert run_component("nexura.text.clean", text=" a ", trim=False)["text"] == " a "
    assert (
        run_component("nexura.text.clean", text=" a  b ", collapse_whitespace=False)["text"]
        == "a  b"
    )
    assert (
        run_component("nexura.text.clean", text="a\x00b", strip_controls=False)["text"] == "a\x00b"
    )
    assert (
        run_component("nexura.text.clean", text="e\u0301", normalize_unicode=False)["text"]
        == "e\u0301"
    )


def test_truncate_and_count(run_component):
    assert run_component("nexura.text.truncate", text="abcdef", limit=3) == {
        "text": "abc…",
        "truncated": True,
    }
    assert (
        run_component("nexura.text.truncate", text="a b c", limit=2, mode="words", ellipsis="...")[
            "text"
        ]
        == "a b..."
    )
    assert run_component("nexura.text.truncate", text="a", limit=1) == {
        "text": "a",
        "truncated": False,
    }
    assert run_component("nexura.text.truncate", text="a", limit=0, ellipsis="")["text"] == ""
    assert run_component("nexura.text.count", text="hi there\nx") == {
        "chars": 10,
        "words": 3,
        "lines": 2,
        "approximate_tokens": 3,
    }
    assert run_component("nexura.text.count", text="") == {
        "chars": 0,
        "words": 0,
        "lines": 0,
        "approximate_tokens": 0,
    }


def test_markup(run_component):
    html = "<p>A &amp; B</p><script>bad()</script><style>bad{}</style><div>C<br>D</div>"
    assert run_component("nexura.text.html_to_text", text=html)["text"] == "A & B\nC\nD"
    assert (
        run_component("nexura.text.html_to_text", text="visible<script>hidden")["text"] == "visible"
    )
    md = "# Title\n\n**bold** and _italic_ [link](https://x.test) ![alt](img)\n```py\ncode\n```"
    assert (
        run_component("nexura.text.markdown_to_text", text=md)["text"]
        == "Title\nbold and italic link alt\ncode"
    )


@pytest.mark.parametrize(
    ("name", "inputs"),
    [
        ("split", {"text": "a", "separator": ""}),
        ("split", {"text": "abcd", "mode": "fixed", "size": 2, "overlap": 2}),
        ("split", {"text": "a" * 1025, "mode": "fixed", "size": 1}),
        ("split", {"text": "," * 1024}),
        ("replace", {"text": "a", "pattern": "[", "replacement": "", "mode": "regex"}),
        ("replace", {"text": "a", "pattern": "a", "replacement": "", "max_replacements": 0}),
        ("regex_extract", {"text": "a", "pattern": "["}),
        ("regex_extract", {"text": "a", "pattern": "a", "mode": "named"}),
        ("regex_extract", {"text": "aa", "pattern": "a", "max_matches": 1}),
        ("regex_extract", {"text": "a", "pattern": "a" * 513}),
        ("count", {"text": "a" * 65537}),
        ("join", {"items": ["a"] * 1025}),
        ("join", {"items": [1]}),
        ("join", {"items": ["a"] * 1000, "separator": "x" * 1000}),
    ],
)
def test_text_errors(run_component, name, inputs):
    with pytest.raises(SchemaValidationError):
        run_component("nexura.text." + name, **inputs)


@pytest.mark.parametrize("name", ["regex_extract", "replace"])
def test_catastrophic_regex_is_interrupted(run_component, name):
    inputs = {"text": "a" * 30000 + "!", "pattern": "(a+)+$"}
    if name == "replace":
        inputs.update(mode="regex", replacement="")
    started = time.monotonic()
    with pytest.raises(SchemaValidationError, match="50ms"):
        run_component("nexura.text." + name, **inputs)
    assert time.monotonic() - started < 2
