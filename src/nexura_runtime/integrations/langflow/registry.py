# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Which Langflow component classes the importer understands, and which ports they have.

Behaviour is resolved by class name only. Component source (the ``code`` field) is never
executed or even compiled.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Canonical kinds.
CHAT_INPUT = "ChatInput"
CHAT_OUTPUT = "ChatOutput"
TEXT_INPUT = "TextInput"
PROMPT = "Prompt"
PARSER = "Parser"
LANGUAGE_MODEL = "LanguageModel"
AGENT = "Agent"
STRUCTURED_OUTPUT = "StructuredOutput"
CALCULATOR = "Calculator"
URL = "URL"
WEB_SEARCH = "WebSearch"

# Extended (V3) kinds, only recognised when ``load(..., extended=True)``.
FILE = "File"
MEMORY = "Memory"
KNOWLEDGE = "Knowledge"
LOOP = "Loop"
SQL = "Sql"
SPLIT_TEXT = "SplitText"

TOOL_KINDS = frozenset({CALCULATOR, URL, WEB_SEARCH})
#: Extended kinds that become an Agent tool when wired from ``component_as_tool``.
TOOLABLE_KINDS = frozenset({MEMORY, KNOWLEDGE, SQL})
EXTENDED_KINDS = frozenset({FILE, MEMORY, KNOWLEDGE, LOOP, SQL, SPLIT_TEXT})


@dataclass(frozen=True)
class ComponentSpec:
    kind: str
    #: Fields that accept an edge (``None`` means any field, used for templates).
    inputs: frozenset[str] | None
    #: Output names an edge may leave from.
    outputs: frozenset[str]


def _s(*names: str) -> frozenset[str]:
    return frozenset(names)


_TOOL_OUT = _s("component_as_tool")

_SPECS: dict[str, ComponentSpec] = {
    "ChatInput": ComponentSpec(CHAT_INPUT, _s("input_value"), _s("message")),
    "ChatOutput": ComponentSpec(CHAT_OUTPUT, _s("input_value"), _s()),
    "TextInput": ComponentSpec(TEXT_INPUT, _s("input_value"), _s("text")),
    "TextInputComponent": ComponentSpec(TEXT_INPUT, _s("input_value"), _s("text")),
    "Prompt": ComponentSpec(PROMPT, None, _s("prompt")),
    "Prompt Template": ComponentSpec(PROMPT, None, _s("prompt")),
    "ParserComponent": ComponentSpec(PARSER, None, _s("parsed_text", "parsed", "message", "text")),
    "parser": ComponentSpec(PARSER, None, _s("parsed_text", "parsed", "message", "text")),
    "LanguageModelComponent": ComponentSpec(
        LANGUAGE_MODEL, _s("input_value", "system_message"), _s("text_output", "model_output")
    ),
    "Agent": ComponentSpec(
        AGENT,
        _s("input_value", "tools", "model", "system_prompt", "format_instructions"),
        _s("response", "structured_response"),
    ),
    "StructuredOutput": ComponentSpec(
        STRUCTURED_OUTPUT,
        _s("input_value", "input", "model", "system_prompt"),
        _s("structured_output"),
    ),
    "CalculatorComponent": ComponentSpec(CALCULATOR, _s(), _TOOL_OUT),
    "Calculator": ComponentSpec(CALCULATOR, _s(), _TOOL_OUT),
    "URLComponent": ComponentSpec(URL, _s(), _TOOL_OUT),
    "URL": ComponentSpec(URL, _s(), _TOOL_OUT),
    "UnifiedWebSearch": ComponentSpec(WEB_SEARCH, _s(), _TOOL_OUT),
}

_EXTENDED_SPECS: dict[str, ComponentSpec] = {
    "File": ComponentSpec(
        FILE,
        _s("path", "file_path"),
        _s("message", "data", "dataframe", "raw_content"),
    ),
    "MemoryBase": ComponentSpec(
        MEMORY, _s("search_query"), _s("component_as_tool", "messages", "message")
    ),
    "KnowledgeBase": ComponentSpec(
        KNOWLEDGE,
        _s("search_query"),
        _s("component_as_tool", "results", "dataframe", "message"),
    ),
    "Knowledge": ComponentSpec(
        KNOWLEDGE,
        _s("search_query"),
        _s("component_as_tool", "results", "dataframe", "message"),
    ),
    "LoopComponent": ComponentSpec(LOOP, _s("data", "item"), _s("item", "done")),
    "SQLComponent": ComponentSpec(
        SQL, _s("query"), _s("component_as_tool", "run_sql_query", "message")
    ),
    "SplitText": ComponentSpec(
        SPLIT_TEXT,
        _s("data_inputs", "data", "input_value"),
        _s("chunks", "dataframe", "message"),
    ),
}

#: Fields each extended class understands. Other fields with a non-empty, visible value are
#: reported as unsupported options (see ``unsupported_options``).
HANDLED_FIELDS: dict[str, frozenset[str]] = {
    "File": _s("path", "file_path", "files"),
    "MemoryBase": _s("search_query", "n_messages"),
    "KnowledgeBase": _s(
        "search_query", "knowledge_base", "knowledge_base_name", "kb_name", "top_k"
    ),
    "Knowledge": _s("search_query", "knowledge_base", "knowledge_base_name", "kb_name", "top_k"),
    "LoopComponent": _s("data"),
    "SQLComponent": _s("query", "database_url"),
    "SplitText": _s("data_inputs", "chunk_size", "chunk_overlap", "separator"),
}

#: Visible-by-default fields that carry no behaviour for the SDK (not reported).
NOISE_FIELDS: frozenset[str] = _s(
    "delete_server_file_after_processing",
    "ignore_unsupported_extensions",
    "ignore_unspecified_files",
    "use_multithreading",
    "concurrency_multithreading",
    "tool_mode",
)

#: Fields that hold a connection string; a literal value is blanked and never used.
CONNECTION_FIELDS: frozenset[str] = _s("database_url", "connection_string")

#: Classes that are known but deliberately unsupported, with the reason reported.
KNOWN_UNSUPPORTED: dict[str, str] = {
    "File": "file loading needs a file store (not supported in V1)",
    "MemoryBase": "long-term memory component (not supported in V1)",
    "KnowledgeBase": "knowledge base retrieval (not supported in V1)",
    "Knowledge": "knowledge component (not supported in V1)",
    "KnowledgeIngestion": "knowledge ingestion (not supported in V1)",
    "AstraDB": "vector store bundle (hidden in V2, not supported in V1)",
    "SQLComponent": "SQL execution needs a security review (not supported in V1)",
    "SaveToFile": "writing files is not supported in V1",
    "ArXivComponent": "arXiv search is not supported in V1",
    "AssemblyAITranscriber": "audio transcription is not supported in V1",
    "BatchRunComponent": "batch runs are not supported in V1",
    "LoopComponent": "Loop is not supported in V1 (planned as a map step)",
    "SplitText": "text splitting is not supported in V1",
}


def spec_for(class_name: str, extended: bool = False) -> ComponentSpec | None:
    if extended and class_name in _EXTENDED_SPECS:
        return _EXTENDED_SPECS[class_name]
    return _SPECS.get(class_name)


def is_feedback_edge(target_kind: str | None, target_field: str) -> bool:
    """An edge that returns an iteration result to a Loop (its ``item`` handle)."""
    return target_kind == LOOP and target_field == "item"


def unsupported_options(class_name: str, fields: Mapping[str, Any], hidden: set[str]) -> list[str]:
    """Names of visible, non-empty fields of an extended class the importer does not use."""
    handled = HANDLED_FIELDS.get(class_name, frozenset())
    found = []
    for name, value in fields.items():
        if name in handled or name in NOISE_FIELDS or name in hidden or name == "code":
            continue
        if value in (None, "", False, 0, [], {}):
            continue
        found.append(name)
    return found


#: Counted node instances in the 27 V2 starter flows, from V0 (class -> occurrences).
V0_COUNTS: dict[str, int] = {
    "Agent": 33,
    "ChatOutput": 28,
    "ChatInput": 22,
    "Prompt": 10,
    "Prompt Template": 1,
    "UnifiedWebSearch": 8,
    "URLComponent": 6,
    "File": 5,
    "ParserComponent": 4,
    "parser": 3,
    "LanguageModelComponent": 4,
    "MemoryBase": 4,
    "CalculatorComponent": 2,
    "SplitText": 2,
    "KnowledgeBase": 2,
    "AstraDB": 2,
    "Knowledge": 1,
    "KnowledgeIngestion": 1,
    "StructuredOutput": 1,
    "SQLComponent": 1,
    "ArXivComponent": 1,
    "LoopComponent": 1,
    "TextInput": 1,
    "BatchRunComponent": 1,
    "SaveToFile": 1,
    "AssemblyAITranscriber": 1,
}


def coverage(extended: bool, counts: Mapping[str, int] | None = None) -> dict[str, Any]:
    """Which counted V0 node instances the importer supports, counted BY CLASS.

    "Supported" means the importer accepts the class name. It does not mean every instance
    works: an instance can set options the importer ignores (reported as load warnings, see
    ``unsupported_options``), and ``options_ignored_classes`` lists the extended classes that
    have such ignored options. The counts also come from the V0 inventory, not from real flows.
    """
    data = dict(V0_COUNTS if counts is None else counts)
    by_class = {name: (spec_for(name, extended) is not None) for name in data}
    total = sum(data.values())
    ok = sum(n for name, n in data.items() if by_class[name])
    return {
        "basis": "node instances counted by class; instances with unsupported options still count",
        "options_ignored_classes": sorted(HANDLED_FIELDS) if extended else [],
        "total": total,
        "supported": ok,
        "by_class": {name: (data[name], by_class[name]) for name in data},
    }


def _main() -> None:  # pragma: no cover - reporting helper
    """``python -m nexura_runtime.integrations.langflow.registry``: V0 coverage table."""
    before, after = coverage(False), coverage(True)
    print("class | occurrences | V1 | after V3")
    for name, (count, ok) in sorted(after["by_class"].items(), key=lambda kv: -kv[1][0]):
        print(
            f"{name} | {count} | {'yes' if before['by_class'][name][1] else 'no'} | "
            f"{'yes' if ok else 'no'}"
        )
    print(f"note: {after['basis']}")
    print(
        f"total supported: V1 {before['supported']}/{before['total']}, "
        f"V3 {after['supported']}/{after['total']}"
    )


if __name__ == "__main__":
    _main()
