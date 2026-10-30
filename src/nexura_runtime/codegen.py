# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Round trip between a workflow spec and plain Python (no lock-in).

``workflow_to_python`` writes readable Python that rebuilds a ``WorkflowSpec`` with the
public constructors; handlers are referenced by their registered name. ``python_to_workflow``
reads such a module back **without executing it**: the source is parsed with ``ast`` and only
the restricted shape the generator emits is accepted, so it is safe for untrusted input.

There is no serializable agent spec on the platform: agents are already Python
(``nexura_agents.Agent(...)``), so there is no ``agent_to_python``.
"""

from __future__ import annotations

import ast
import json
import math
import re
import textwrap
from typing import Any

from nexura_agents.errors import WorkflowValidationError

from nexura_runtime.workflow import WorkflowNode, WorkflowSpec

__all__ = [
    "MAX_SOURCE_BYTES",
    "CodeGenerationError",
    "CodeImportError",
    "python_to_workflow",
    "workflow_to_python",
]

#: Largest module ``python_to_workflow`` will parse.
MAX_SOURCE_BYTES = 512 * 1024
_MAX_DEPTH = 48
_WIDTH = 88
_REGISTRY_IMPORT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_]\w*$")
_ENGINE_NAMES = ("WorkflowEngine", "WorkflowNode", "WorkflowSpec")


class CodeGenerationError(ValueError):
    """The spec holds a value that cannot be written as a Python literal."""


class CodeImportError(WorkflowValidationError):
    """Source is not the restricted shape ``workflow_to_python`` emits.

    ``line`` and ``column`` locate the offending construct (1-based line, 0-based column).
    """

    def __init__(self, message: str, line: int | None = None, column: int | None = None) -> None:
        self.line = line
        self.column = column
        self.reason = message
        super().__init__(f"line {line}: {message}" if line else message)


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _inline(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return repr(value)
    if isinstance(value, int):
        return repr(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CodeGenerationError(f"non-finite number {value!r} cannot be written as a literal")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_inline(v) for v in value) + "]"
    if isinstance(value, tuple):
        body = ", ".join(_inline(v) for v in value)
        return "(" + body + ("," if len(value) == 1 else "") + ")"
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise CodeGenerationError(f"dict key {key!r} is not a string")
        return "{" + ", ".join(f"{_inline(k)}: {_inline(v)}" for k, v in value.items()) + "}"
    raise CodeGenerationError(f"{type(value).__name__} value cannot be written as a literal")


def _fmt(value: Any, indent: int, used: int) -> str:
    """Render a literal, wrapping one item per line when it would not fit."""
    flat = _inline(value)
    if (
        indent + used + len(flat) + 1 <= _WIDTH
        or not value
        or not isinstance(value, (list, tuple, dict))
    ):
        return flat
    pad = " " * (indent + 4)
    if isinstance(value, dict):
        items = [
            f"{pad}{_inline(k)}: {_fmt(v, indent + 4, len(_inline(k)) + 2)},"
            for k, v in value.items()
        ]
        return "{\n" + "\n".join(items) + "\n" + " " * indent + "}"
    items = [f"{pad}{_fmt(v, indent + 4, 0)}," for v in value]
    open_, close = ("[", "]") if isinstance(value, list) else ("(", ")")
    return open_ + "\n" + "\n".join(items) + "\n" + " " * indent + close


def _node_comments(node: WorkflowNode) -> list[str]:
    notes: list[str] = []
    if node.requires_approval:
        notes.append(
            f"Approval gate: the run pauses before this step until someone with role "
            f"{node.required_role!r} approves it."
        )
    if node.retry:
        attempts = node.retry.get("max_attempts", 1)
        notes.append(f"Retries: up to {attempts} attempt(s) with backoff ({_inline(node.retry)}).")
    if node.subgraph is not None:
        notes.append(f"Composite step: runs the nested workflow {json.dumps(node.subgraph.name)}.")
    if node.parallel:
        notes.append(
            f"Composite step: runs {len(node.parallel)} handlers in parallel on the same state "
            "and merges their updates with `reducers`."
        )
    if node.map_over is not None:
        notes.append(
            f"Composite step: runs once per item of state[{json.dumps(node.map_over)}] "
            f"(as {json.dumps(node.map_as)}), {node.max_concurrency} at a time."
        )
    return notes


def _node_src(node: WorkflowNode, indent: int) -> str:
    pad = " " * (indent + 4)
    lines = ["WorkflowNode("]

    def kw(key: str, value: Any) -> None:
        lines.append(f"{pad}{key}={_fmt(value, indent + 4, len(key) + 1)},")

    kw("id", node.id)
    kw("handler", node.handler)
    if node.transitions:
        kw("transitions", node.transitions)
    if node.default_transition is not None:
        kw("default_transition", node.default_transition)
    if node.requires_approval:
        kw("requires_approval", True)
    if node.required_role != "admin":
        kw("required_role", node.required_role)
    if node.module_id is not None:
        kw("module_id", node.module_id)
    if node.module_version is not None:
        kw("module_version", node.module_version)
    for key in ("config", "input_mapping", "output_mapping", "presentation", "metadata", "retry"):
        value = getattr(node, key)
        if value:
            kw(key, value)
    if node.subgraph is not None:
        lines.append(f"{pad}subgraph={_spec_src(node.subgraph, indent + 4)},")
    if node.parallel:
        kw("parallel", node.parallel)
    if node.map_over is not None:
        kw("map_over", node.map_over)
    if node.map_as != "item":
        kw("map_as", node.map_as)
    if node.reducers:
        kw("reducers", node.reducers)
    if node.max_concurrency != 10:
        kw("max_concurrency", node.max_concurrency)
    lines.append(" " * indent + ")")
    return "\n".join(lines)


def _spec_src(spec: WorkflowSpec, indent: int) -> str:
    pad = " " * (indent + 4)
    lines = ["WorkflowSpec("]

    def kw(key: str, value: Any) -> None:
        lines.append(f"{pad}{key}={_fmt(value, indent + 4, len(key) + 1)},")

    kw("name", spec.name)
    kw("entry_node", spec.entry_node)
    if spec.spec_version != 1:
        kw("spec_version", spec.spec_version)
    if not spec.nodes:
        lines.append(f"{pad}nodes={{}},")
    else:
        lines.append(f"{pad}nodes={{")
        inner = " " * (indent + 8)
        for node_id, node in spec.nodes.items():
            if node_id != node.id:
                raise CodeGenerationError(f"node key {node_id!r} does not match id {node.id!r}")
            for note in _node_comments(node):
                lines.extend(
                    f"{inner}# {part}"
                    for part in textwrap.wrap(note, width=_WIDTH - len(inner) - 2)
                )
            lines.append(
                f"{inner}{json.dumps(node_id, ensure_ascii=False)}: {_node_src(node, indent + 8)},"
            )
        lines.append(f"{pad}}},")
    if spec.terminal_nodes:
        kw("terminal_nodes", spec.terminal_nodes)
    if spec.metadata:
        kw("metadata", spec.metadata)
    if spec.presentation:
        kw("presentation", spec.presentation)
    lines.append(" " * indent + ")")
    return "\n".join(lines)


def _all_specs(spec: WorkflowSpec) -> list[WorkflowSpec]:
    found = [spec]
    for node in spec.nodes.values():
        if node.subgraph is not None:
            found.extend(_all_specs(node.subgraph))
    return found


def _main_block(registry_import: str | None) -> str:
    if registry_import is None:
        head = [
            "    from nexura_runtime.studio.builtin_modules import get_default_registry",
            "",
            "    registry = get_default_registry()",
        ]
    else:
        module, attr = registry_import.split(":")
        alias = "" if attr == "registry" else " as registry"
        head = [f"    from {module} import {attr}{alias}"]
    body = [
        'if __name__ == "__main__":',
        *head,
        "",
        "    async def main() -> None:",
        "        engine = WorkflowEngine(spec=spec, registry=registry)",
        "        result = await engine.run({})",
        "        # A handler can pause the run with interrupt(); an approval gate pauses it too.",
        '        # Status "paused" means a person must resume it (see the workflow docs).',
        '        print("status:", result.status)',
        "        print(json.dumps(result.state, indent=2, default=str))",
        "",
        "    asyncio.run(main())",
    ]
    return "\n".join(body)


def workflow_to_python(spec: WorkflowSpec, *, registry_import: str | None = None) -> str:
    """Return readable, ruff-clean Python that rebuilds ``spec``.

    Handlers stay names (strings); the generated ``__main__`` block runs the module with a
    registry: ``registry_import="package.module:attribute"`` names yours, otherwise the
    built-in registry is used. Every spec field is kept, so ``exec`` of the module gives a
    ``spec`` whose ``to_dict()`` equals ``spec.to_dict()``.
    """
    if registry_import is not None and not _REGISTRY_IMPORT.match(registry_import):
        raise ValueError("registry_import must look like 'package.module:attribute'")
    names = ["WorkflowEngine"]
    if any(s.nodes for s in _all_specs(spec)):
        names.append("WorkflowNode")
    names.append("WorkflowSpec")
    notes = "\n".join(
        f"# {line}"
        for line in (
            "Handlers are referenced by registered name; they live in your registry.",
            "Edit freely: `nexura-runtime workflow import` reads this shape back "
            "without running it.",
        )
    )
    doc = "Workflow generated by nexura_runtime.codegen."
    parts = [
        f'"""{doc}"""\n',
        "import asyncio\nimport json\n",
        f"from nexura_runtime.workflow import {', '.join(names)}\n",
        notes + "\n",
        f"spec = {_spec_src(spec, 0)}\n",
        "\n" + _main_block(registry_import) + "\n",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Safe import (ast only, never executes)
# ---------------------------------------------------------------------------


def _fail(node: ast.AST, message: str) -> CodeImportError:
    return CodeImportError(
        message, getattr(node, "lineno", None), getattr(node, "col_offset", None)
    )


def _describe(node: ast.AST) -> str:
    try:
        text = ast.unparse(node)
    except Exception:  # pragma: no cover - unparse of a parsed node does not fail
        text = type(node).__name__
    return text if len(text) <= 60 else text[:57] + "..."


def _literal(node: ast.AST, depth: int = 0) -> Any:
    """Evaluate a literal-only expression by walking the tree (never ``eval``)."""
    if depth > _MAX_DEPTH:
        raise _fail(node, "literal is nested too deeply")
    if isinstance(node, ast.Constant):
        if node.value is Ellipsis or isinstance(node.value, (bytes, complex)):
            raise _fail(node, f"{type(node.value).__name__} literals are not allowed")
        if isinstance(node.value, float) and not math.isfinite(node.value):
            raise _fail(node, "non-finite numbers (inf, nan) are not allowed")
        if isinstance(node.value, str):
            try:
                node.value.encode("utf-8")
            except UnicodeEncodeError:
                raise _fail(node, "string contains an invalid character (lone surrogate)") from None
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        operand = node.operand
        if (
            isinstance(operand, ast.Constant)
            and isinstance(operand.value, (int, float))
            and not isinstance(operand.value, bool)
        ):
            return -operand.value if isinstance(node.op, ast.USub) else operand.value
        raise _fail(node, "only plain numbers can be negated")
    if isinstance(node, ast.List):
        return [_literal(e, depth + 1) for e in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_literal(e, depth + 1) for e in node.elts)
    if isinstance(node, ast.Dict):
        result: dict[str, Any] = {}
        for key, value in zip(node.keys, node.values, strict=True):
            if key is None:
                raise _fail(value, "dict unpacking (**) is not allowed")
            k = _literal(key, depth + 1)
            if not isinstance(k, str):
                raise _fail(key, "dict keys must be strings")
            result[k] = _literal(value, depth + 1)
        return result
    if isinstance(node, ast.JoinedStr | ast.FormattedValue):
        raise _fail(node, "f-strings are not allowed; use plain string literals")
    if isinstance(node, ast.Call):
        raise _fail(node, f"calls are not allowed here: {_describe(node)}")
    if isinstance(node, ast.Name):
        raise _fail(node, f"name '{node.id}' is not allowed here; use a literal value")
    if isinstance(node, ast.Attribute):
        raise _fail(node, f"attribute access is not allowed: {_describe(node)}")
    raise _fail(node, f"{type(node).__name__} expressions are not allowed here")


_SPEC_FIELDS: dict[str, type | tuple[type, ...]] = {
    "name": str,
    "entry_node": str,
    "nodes": dict,
    "spec_version": int,
    "terminal_nodes": list,
    "metadata": dict,
    "presentation": dict,
}
_NODE_FIELDS: dict[str, type | tuple[type, ...]] = {
    "id": str,
    "handler": str,
    "transitions": dict,
    "default_transition": (str, type(None)),
    "requires_approval": bool,
    "required_role": str,
    "module_id": (str, type(None)),
    "module_version": (str, type(None)),
    "config": dict,
    "input_mapping": dict,
    "output_mapping": dict,
    "presentation": dict,
    "metadata": dict,
    "retry": dict,
    "subgraph": WorkflowSpec,
    "parallel": list,
    "map_over": (str, type(None)),
    "map_as": str,
    "reducers": dict,
    "max_concurrency": int,
}


class _Parser:
    def __init__(self) -> None:
        self.imported: set[str] = set()

    def _call_kwargs(
        self, node: ast.AST, ctor: str, fields: dict[str, Any], required: tuple[str, ...]
    ) -> dict[str, tuple[ast.expr, Any]]:
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            if isinstance(node, ast.Call):
                raise _fail(node, f"calls are not allowed here: {_describe(node)}")
            raise _fail(node, f"expected a {ctor}(...) call")
        if node.func.id != ctor:
            raise _fail(node, f"call to '{node.func.id}' is not allowed; only {ctor}(...)")
        if ctor not in self.imported:
            raise _fail(node, f"{ctor} is used but not imported from nexura_runtime.workflow")
        if node.args:
            raise _fail(node.args[0], f"{ctor}() takes keyword arguments only")
        found: dict[str, tuple[ast.expr, Any]] = {}
        for kwd in node.keywords:
            if kwd.arg is None:
                raise _fail(kwd.value, "argument unpacking (**) is not allowed")
            if kwd.arg not in fields:
                raise _fail(kwd.value, f"{ctor}() has no field '{kwd.arg}'")
            if kwd.arg in found:
                raise _fail(kwd.value, f"{ctor}() repeats '{kwd.arg}'")
            found[kwd.arg] = (kwd.value, None)
        for name in required:
            if name not in found:
                raise _fail(node, f"{ctor}() requires '{name}'")
        return found

    def _typed(self, key: str, node: ast.expr, expected: Any, value: Any) -> Any:
        if isinstance(value, bool) and expected in (int, (int,)):
            raise _fail(node, f"'{key}' must be an integer")
        if not isinstance(value, expected):
            raise _fail(node, f"'{key}' has the wrong type ({type(value).__name__})")
        return value

    def spec(self, node: ast.AST, depth: int = 0) -> WorkflowSpec:
        if depth > 8:
            raise _fail(node, "sub-workflows are nested too deeply")
        found = self._call_kwargs(
            node, "WorkflowSpec", _SPEC_FIELDS, ("name", "entry_node", "nodes")
        )
        values: dict[str, Any] = {}
        for key, (expr, _) in found.items():
            if key == "nodes":
                values[key] = self.nodes(expr, depth)
            else:
                values[key] = self._typed(key, expr, _SPEC_FIELDS[key], _literal(expr))
        if "terminal_nodes" in values:
            self._strings("terminal_nodes", found["terminal_nodes"][0], values["terminal_nodes"])
        return WorkflowSpec(**values)

    def _strings(self, key: str, node: ast.expr, items: list[Any]) -> None:
        if not all(isinstance(i, str) for i in items):
            raise _fail(node, f"'{key}' must be a list of strings")

    def nodes(self, node: ast.expr, depth: int) -> dict[str, WorkflowNode]:
        if not isinstance(node, ast.Dict):
            raise _fail(node, "'nodes' must be a dict literal of WorkflowNode(...) calls")
        result: dict[str, WorkflowNode] = {}
        for key, value in zip(node.keys, node.values, strict=True):
            if key is None:
                raise _fail(value, "dict unpacking (**) is not allowed")
            node_id = _literal(key)
            if not isinstance(node_id, str):
                raise _fail(key, "node ids must be strings")
            built = self.node(value, depth)
            if built.id != node_id:
                raise _fail(value, f"node key '{node_id}' does not match id '{built.id}'")
            if node_id in result:
                raise _fail(key, f"duplicate node id '{node_id}'")
            result[node_id] = built
        return result

    def node(self, node: ast.AST, depth: int) -> WorkflowNode:
        found = self._call_kwargs(node, "WorkflowNode", _NODE_FIELDS, ("id", "handler"))
        values: dict[str, Any] = {}
        for key, (expr, _) in found.items():
            if key == "subgraph":
                values[key] = self.spec(expr, depth + 1)
                continue
            value = self._typed(key, expr, _NODE_FIELDS[key], _literal(expr))
            if key == "parallel":
                self._strings(key, expr, value)
            if key == "transitions" and not all(isinstance(v, str) for v in value.values()):
                raise _fail(expr, "'transitions' targets must be strings")
            values[key] = value
        return WorkflowNode(**values)

    def main_block(self, node: ast.If) -> None:
        test = node.test
        ok = (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq)
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "__main__"
        )
        if not ok or node.orelse:
            raise _fail(node, 'only `if __name__ == "__main__":` is allowed')
        candidates: list[str | None] = [None]
        for stmt in node.body:
            if (
                isinstance(stmt, ast.ImportFrom)
                and stmt.level == 0
                and stmt.module
                and len(stmt.names) == 1
                and stmt.names[0].name != "*"
            ):
                candidates.append(f"{stmt.module}:{stmt.names[0].name}")
        actual = ast.dump(ast.Module(body=[node], type_ignores=[]))
        for candidate in candidates:
            expected = ast.parse(_main_block(candidate))
            if ast.dump(expected) == actual:
                return
        raise _fail(
            node,
            "the __main__ block must be exactly the one workflow_to_python writes "
            "(it is never executed, so it cannot carry custom code)",
        )

    def module(self, tree: ast.Module) -> WorkflowSpec:
        spec: WorkflowSpec | None = None
        for index, stmt in enumerate(tree.body):
            if (
                index == 0
                and isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            ):
                continue
            if isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    if alias.name not in ("asyncio", "json") or alias.asname:
                        raise _fail(stmt, f"import of '{alias.name}' is not allowed")
            elif isinstance(stmt, ast.ImportFrom):
                if stmt.module != "nexura_runtime.workflow" or stmt.level != 0:
                    raise _fail(
                        stmt, f"import from '{'.' * stmt.level}{stmt.module}' is not allowed"
                    )
                for alias in stmt.names:
                    if alias.name not in _ENGINE_NAMES or alias.asname:
                        raise _fail(stmt, f"importing '{alias.name}' is not allowed")
                    self.imported.add(alias.name)
            elif isinstance(stmt, ast.Assign):
                if (
                    len(stmt.targets) != 1
                    or not isinstance(stmt.targets[0], ast.Name)
                    or stmt.targets[0].id != "spec"
                ):
                    raise _fail(stmt, "only the assignment `spec = WorkflowSpec(...)` is allowed")
                if spec is not None:
                    raise _fail(stmt, "`spec` is assigned more than once")
                spec = self.spec(stmt.value)
            elif isinstance(stmt, ast.If):
                self.main_block(stmt)
            else:
                raise _fail(stmt, f"{type(stmt).__name__} statements are not allowed")
        if spec is None:
            raise CodeImportError("no `spec = WorkflowSpec(...)` assignment found", 1, 0)
        return spec


def python_to_workflow(source: str, *, max_bytes: int = MAX_SOURCE_BYTES) -> WorkflowSpec:
    """Rebuild a ``WorkflowSpec`` from generated Python without executing it.

    Only the shape ``workflow_to_python`` emits is accepted; anything else (other calls,
    attribute access, other imports, comprehensions, lambdas, f-strings, decorators...) raises
    ``CodeImportError`` carrying the line number.
    """
    if not isinstance(source, str):
        raise CodeImportError("source must be text")
    if len(source.encode("utf-8", errors="replace")) > max_bytes:
        raise CodeImportError(f"source is larger than {max_bytes} bytes")
    try:
        tree = ast.parse(source, filename="<workflow>", mode="exec")
    except SyntaxError as exc:
        raise CodeImportError(f"syntax error: {exc.msg}", exc.lineno, exc.offset) from exc
    except (ValueError, RecursionError, MemoryError) as exc:
        raise CodeImportError(f"source cannot be parsed: {exc}") from exc
    try:
        return _Parser().module(tree)
    except RecursionError as exc:
        raise CodeImportError("source is nested too deeply") from exc
