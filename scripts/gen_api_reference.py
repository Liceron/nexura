"""Generate ``docs/api-reference.md`` from the public API's docstrings.

    uv run python scripts/gen_api_reference.py          # rewrite the page
    uv run python scripts/gen_api_reference.py --check  # exit 1 if it is out of date
    uv --directory enterprise run python ../scripts/gen_api_reference.py --edition enterprise

Covers everything in ``nexura_agents.__all__`` and ``nexura_vertex.__all__`` plus the workflow
API in ``nexura_runtime.workflow``. Output is deterministic so the test suite can compare it.
Docstrings are shown in ``~~~text`` fences, which the docs example runner does not execute.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "api-reference.md"

SECTIONS: list[tuple[str, str, list[str] | None]] = [
    ("nexura_agents.repro", "Failure reproduction (`nexura_agents.repro`)", None),
    ("nexura_agents", "Agent SDK (`nexura_agents`)", None),
    ("nexura_agents.redteam", "Red-team eval pack (`nexura_agents.redteam`)", None),
    ("nexura_agents.skills", "Skills (`nexura_agents.skills`)", None),
    ("nexura_agents.network", "Network policy (`nexura_agents.network`)", None),
    (
        "nexura_agents.execution_store",
        "Execution stores (`nexura_agents.execution_store`, used by `DurableExecutionEngine`)",
        None,
    ),
    (
        "nexura_postgres.execution_store",
        "Postgres execution store (`nexura_postgres.execution_store`)",
        ["PostgresExecutionStore"],
    ),
    (
        "nexura_agents.contracts",
        "Execution Contract v1 (`nexura_agents.contracts`, being implemented)",
        [
            "EffectClass",
            "ExecutionId",
            "StepKey",
            "StepIntent",
            "StepOutcome",
            "EffectRecord",
            "EffectStatus",
            "EffectResolution",
            "ReconcileResult",
            "Applied",
            "NotApplied",
            "Unknown",
            "LeaseLostError",
            "DefinitionMismatchError",
            "derive_effect_id",
            "default_idempotency_key",
            "effect_class_from_policy",
            "effect_class_from_idempotent",
        ],
    ),
    (
        "nexura_agents.providers.errors",
        "Provider errors (`nexura_agents.providers.errors`)",
        [
            "ProviderError",
            "CapabilityError",
            "StreamCancelledError",
            "MalformedToolCallArgumentsError",
            "ProviderTimeoutError",
            "ProviderAuthError",
            "ProviderServerError",
            "ProviderClientError",
            "ProviderContextLengthError",
            "ProviderRateLimitedError",
            "is_context_length_message",
            "parse_retry_after",
        ],
    ),
    ("nexura_vertex", "Vertex AI provider (`nexura_vertex`, package `nexura-vertex`)", None),
    ("nexura_runtime.templates", "Templates (`nexura_runtime.templates`)", None),
    (
        "nexura_runtime.builder_assistant",
        "Builder assistant (`nexura_runtime.builder_assistant`)",
        None,
    ),
    ("nexura_runtime.codegen", "Code generation (`nexura_runtime.codegen`)", None),
    (
        "nexura_runtime.project",
        "Project scaffolding (`nexura_runtime.project`, `nexura-runtime init/dev/deploy`)",
        None,
    ),
    (
        "nexura_runtime.integrations.nexura_v2",
        "Nexura V2 integration (`nexura_runtime.integrations.nexura_v2`)",
        None,
    ),
    (
        "nexura_runtime.integrations.langflow",
        "Langflow integration (`nexura_runtime.integrations.langflow`)",
        None,
    ),
    (
        "nexura_runtime.workflow",
        "Workflows (`nexura_runtime.workflow`)",
        [
            "WorkflowSpec",
            "WorkflowNode",
            "ModuleRegistry",
            "WorkflowEngine",
            "MemoryWorkflowStore",
            "SQLiteWorkflowStore",
            "NodeRetryPolicy",
            "interrupt",
            "request_approval",
            "approved_request",
            "WorkflowInterrupt",
            "HandlerOutput",
            "WorkflowCheckpoint",
            "WorkflowRunResult",
            "PendingApproval",
            "REDUCERS",
        ],
    ),
]


def _signature(obj: Any) -> str:
    try:
        text = str(inspect.signature(obj))
    except (TypeError, ValueError):
        return ""
    text = re.sub(r" at 0x[0-9a-f]+", "", text)  # object reprs in defaults vary per run
    # set reprs depend on the hash seed: print their items in sorted order
    return re.sub(
        r"frozenset\(\{([^{}]*)\}\)",
        lambda m: (
            "frozenset({" + ", ".join(sorted(s.strip() for s in m.group(1).split(","))) + "})"
        ),
        text,
    )


def _doc(obj: Any) -> str:
    return inspect.cleandoc(obj.__doc__) if getattr(obj, "__doc__", None) else ""


def _public_methods(cls: type) -> list[tuple[str, Any]]:
    methods = []
    for name, member in vars(cls).items():
        if name.startswith("_"):
            continue
        func = member.__func__ if isinstance(member, (classmethod, staticmethod)) else member
        if inspect.isfunction(func):
            methods.append((name, func))
    return sorted(methods)


def missing_docstrings() -> list[str]:
    """Public names (and their public methods) that have no docstring."""
    missing = []
    for module_name, _, names in SECTIONS:
        module = importlib.import_module(module_name)
        for name in names or [n for n in module.__all__ if not n.startswith("_")]:
            obj = getattr(module, name)
            if not (inspect.isclass(obj) or inspect.isfunction(obj)):
                continue
            if not _doc(obj):
                missing.append(f"{module_name}.{name}")
            if inspect.isclass(obj):
                missing += [
                    f"{module_name}.{name}.{m}" for m, f in _public_methods(obj) if not _doc(f)
                ]
    return missing


def render(
    sections: list[tuple[str, str, list[str] | None]] | None = None,
    *,
    edition: str = "oss",
) -> str:
    out = [
        "# API reference",
        "",
        "Generated from docstrings by `scripts/gen_api_reference.py`; do not edit by hand.",
        "The guides in this folder explain how the pieces fit together; this page lists",
        "the selected public exports with their signatures.",
        (
            "MIT open-source edition: single user, this machine only; for a team or a "
            "public endpoint use the hosted or enterprise edition."
            if edition == "oss"
            else "Closed enterprise edition: additions to the MIT API reference. "
            "This reference does not establish deployment readiness or an external security audit."
        ),
        "",
    ]
    for module_name, title, names in SECTIONS if sections is None else sections:
        module = importlib.import_module(module_name)
        listed = names or [n for n in module.__all__ if not n.startswith("_")]
        out += [f"## {title}", ""]
        for name in listed:
            obj = getattr(module, name)
            if inspect.isclass(obj):
                kind = "class"
            elif inspect.isfunction(obj):
                kind = "function"
            else:
                out += [f"### `{name}`", "", f"Constant: `{obj!r}`", ""]
                continue
            out += [f"### `{name}`", "", f"{kind} `{name}{_signature(obj)}`", ""]
            if doc := _doc(obj):
                out += ["~~~text", doc, "~~~", ""]
            if inspect.isclass(obj):
                for method_name, func in _public_methods(obj):
                    out.append(f"- `{method_name}{_signature(func)}`")
                    if first_line := _doc(func).split("\n", 1)[0]:
                        out.append(f"  {first_line}")
                out.append("")
    index = "README.md" if edition == "oss" else "../README.md"
    out += ["## See also", "", f"- [Documentation index]({index})", ""]
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--edition", choices=("oss", "enterprise"), default="oss")
    args = parser.parse_args()
    output = OUTPUT
    sections = None
    if args.edition == "enterprise":
        output = ROOT / "enterprise" / "docs" / "api-reference.md"
        manifest = output.with_name("api-sections.json")
        sections = json.loads(manifest.read_text(encoding="utf-8"))
    text = render(sections, edition=args.edition)
    if args.check:
        if not output.exists() or output.read_text(encoding="utf-8") != text:
            print(
                f"{output} is out of date; run scripts/gen_api_reference.py "
                f"--edition {args.edition}",
                file=sys.stderr,
            )
            return 1
        return 0
    output.write_text(text, encoding="utf-8")
    print(f"Wrote {output.relative_to(ROOT)} ({text.count(chr(10))} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
