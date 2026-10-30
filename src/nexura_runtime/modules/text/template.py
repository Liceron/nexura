# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Deterministic safe prompt template engine without eval."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from nexura_runtime.modules.text.errors import (
    TemplateError,
    TemplateExpansionLimitError,
    TemplateSecurityError,
    TemplateSyntaxError,
    TemplateVariableMissingError,
)

_VAR_PATTERN = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_TAG_PATTERN = re.compile(r"(\{%.*?%\}|\{\{.*?\}\})", re.DOTALL)
_SAFE_IDENT_REGEX = re.compile(r"^[a-zA-Z0-9_\[\].'\"]+$")


@dataclass
class _Node:
    """Base AST node."""


@dataclass
class _TextNode(_Node):
    text: str


@dataclass
class _VarNode(_Node):
    raw_expr: str


@dataclass
class _IfNode(_Node):
    condition_expr: str
    true_nodes: list[_Node]
    false_nodes: list[_Node]


@dataclass
class _ForNode(_Node):
    var_name: str
    iterable_expr: str
    body_nodes: list[_Node]


class PromptTemplate:
    """Deterministic, injection-safe template engine for agent prompt construction.

    Guarantees:
    - Zero eval/exec: Never evaluates untrusted Python code strings or expressions.
    - Security boundaries: Prohibits private and dunder attribute traversals.
    - Strict variables: Eagerly raises on missing variables unless filtered by default.
    - Bounded expansion: Strictly enforces max character limits against expansion attacks.
    - Unicode fidelity: Round-trips Indic text, emojis, and multiline blocks uncorrupted.
    """

    def __init__(
        self,
        template: str,
        *,
        default_variables: dict[str, Any] | None = None,
        max_output_length: int = 500_000,
        name: str = "prompt_template",
    ) -> None:
        self.template = template
        self.default_variables = default_variables or {}
        self.max_output_length = max_output_length
        self.name = name
        self._root_nodes = self._parse(template)

    def get_variables(self) -> set[str]:
        """Extract all unique top-level variable identifiers referenced in template."""
        vars_found: set[str] = set()
        matches = _VAR_PATTERN.findall(self.template)
        for raw in matches:
            clean = raw.strip().split("|")[0].strip()
            root = re.split(r"[.\[]", clean)[0].strip()
            if root and not root.startswith("_"):
                vars_found.add(root)

        for match in re.finditer(r"\{%\s*if\s+(.*?)\s*%\}", self.template):
            cond = match.group(1).strip()
            tokens = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", cond)
            for tok in tokens:
                if tok not in ("not", "in", "and", "or", "true", "false", "null", "none"):
                    vars_found.add(tok)

        for match in re.finditer(r"\{%\s*for\s+(\w+)\s+in\s+([a-zA-Z0-9_.]+)\s*%\}", self.template):
            root = match.group(2).split(".")[0].strip()
            vars_found.add(root)

        return vars_found

    def _parse(self, template: str) -> list[_Node]:
        """Parse template text into a deterministic AST."""
        tokens = _TAG_PATTERN.split(template)
        root_nodes: list[_Node] = []
        stack: list[tuple[list[_Node], str, Any]] = [(root_nodes, "root", None)]

        for token in tokens:
            if not token:
                continue

            current_list = stack[-1][0]

            if token.startswith("{{") and token.endswith("}}"):
                expr = token[2:-2].strip()
                current_list.append(_VarNode(raw_expr=expr))
            elif token.startswith("{%") and token.endswith("%}"):
                tag_content = token[2:-2].strip()
                parts = tag_content.split(maxsplit=1)
                tag_name = parts[0] if parts else ""
                tag_arg = parts[1].strip() if len(parts) > 1 else ""

                if tag_name == "if":
                    if not tag_arg:
                        raise TemplateSyntaxError("Empty '{% if %}' statement.")
                    if_node = _IfNode(condition_expr=tag_arg, true_nodes=[], false_nodes=[])
                    current_list.append(if_node)
                    stack.append((if_node.true_nodes, "if", if_node))
                elif tag_name == "else":
                    if not stack or stack[-1][1] != "if":
                        raise TemplateSyntaxError("Unexpected '{% else %}' without matching if.")
                    _, _, if_node = stack.pop()
                    stack.append((if_node.false_nodes, "else", if_node))
                elif tag_name == "endif":
                    if not stack or stack[-1][1] not in ("if", "else"):
                        raise TemplateSyntaxError("Unexpected '{% endif %}' without matching if.")
                    stack.pop()
                elif tag_name == "for":
                    match = re.match(r"^(\w+)\s+in\s+([a-zA-Z0-9_.]+)$", tag_arg)
                    if not match:
                        raise TemplateSyntaxError(
                            f"Invalid for loop syntax: '{{% for {tag_arg} %}}'. "
                            "Expected: '{% for item in items %}'."
                        )
                    item_var = match.group(1)
                    iter_expr = match.group(2)
                    for_node = _ForNode(var_name=item_var, iterable_expr=iter_expr, body_nodes=[])
                    current_list.append(for_node)
                    stack.append((for_node.body_nodes, "for", for_node))
                elif tag_name == "endfor":
                    if not stack or stack[-1][1] != "for":
                        raise TemplateSyntaxError("Unexpected '{% endfor %}' without matching for.")
                    stack.pop()
                else:
                    raise TemplateSyntaxError(f"Unsupported template tag: '{tag_name}'.")
            else:
                current_list.append(_TextNode(text=token))

        if len(stack) > 1:
            unclosed_kind = stack[-1][1]
            raise TemplateSyntaxError(f"Unclosed block: '{{% {unclosed_kind} %}}'.")

        return root_nodes

    def render(self, variables: dict[str, Any] | None = None) -> str:
        """Render template with provided variables, enforcing bounds and strictness."""
        combined_vars: dict[str, Any] = dict(self.default_variables)
        if variables:
            combined_vars.update(variables)

        out_chunks: list[str] = []
        total_len = 0

        self._render_nodes(self._root_nodes, combined_vars, out_chunks, total_len)
        return "".join(out_chunks)

    def _render_nodes(
        self,
        nodes: list[_Node],
        context: dict[str, Any],
        out_chunks: list[str],
        total_len: int,
    ) -> int:
        for node in nodes:
            if isinstance(node, _TextNode):
                chunk = node.text
            elif isinstance(node, _VarNode):
                chunk = self._eval_var(node.raw_expr, context)
            elif isinstance(node, _IfNode):
                cond_val = self._eval_condition(node.condition_expr, context)
                sub_nodes = node.true_nodes if cond_val else node.false_nodes
                total_len = self._render_nodes(sub_nodes, context, out_chunks, total_len)
                continue
            elif isinstance(node, _ForNode):
                iter_val = self._resolve_path(node.iterable_expr, context, has_default=False)
                if not isinstance(iter_val, Iterable) or isinstance(iter_val, (str, bytes, dict)):
                    raise TemplateError(
                        f"Target of for loop '{node.iterable_expr}' is not a list or iterable."
                    )
                items = list(iter_val)
                num_items = len(items)
                for idx, item in enumerate(items):
                    child_context = dict(context)
                    child_context[node.var_name] = item
                    child_context["loop"] = {
                        "index": idx,
                        "index1": idx + 1,
                        "first": idx == 0,
                        "last": idx == num_items - 1,
                        "length": num_items,
                    }
                    total_len = self._render_nodes(
                        node.body_nodes, child_context, out_chunks, total_len
                    )
                continue
            else:
                chunk = ""

            total_len += len(chunk)
            if total_len > self.max_output_length:
                raise TemplateExpansionLimitError(total_len, self.max_output_length)
            out_chunks.append(chunk)

        return total_len

    def _eval_var(self, raw_expr: str, context: dict[str, Any]) -> str:
        """Resolve a variable expression with optional filter pipeline."""
        parts = [p.strip() for p in raw_expr.split("|")]
        path_expr = parts[0]
        filters = parts[1:]

        has_default = any(f.startswith("default(") or f.startswith("default:") for f in filters)
        val = self._resolve_path(path_expr, context, has_default=has_default)

        for flt in filters:
            flt_name = flt.split("(")[0].split(":")[0].strip()
            if flt_name == "default":
                if val is None or val == "":
                    # Extract default argument
                    m = re.search(r"default\((.*?)\)|default:\s*(.*)", flt)
                    if m:
                        arg_str = (m.group(1) or m.group(2) or "").strip().strip("'\"")
                        val = arg_str
            elif flt_name == "upper":
                val = str(val).upper()
            elif flt_name == "lower":
                val = str(val).lower()
            elif flt_name == "trim":
                val = str(val).strip()
            elif flt_name == "title":
                val = str(val).title()
            elif flt_name == "json":
                val = json.dumps(val, ensure_ascii=False, indent=2)
            else:
                raise TemplateSyntaxError(f"Unsupported template filter '{flt_name}'.")

        if val is None:
            return ""
        if isinstance(val, bool):
            return "true" if val else "false"
        if isinstance(val, (dict, list)):
            return json.dumps(val, ensure_ascii=False)
        return str(val)

    def _resolve_path(self, path: str, context: dict[str, Any], has_default: bool = False) -> Any:
        """Safely traverse dot/index notation without arbitrary code execution."""
        clean_path = path.strip()
        if not clean_path:
            raise TemplateSyntaxError("Empty variable expression.")

        # Guard against private / dunder attribute traversal
        has_private = any(part.startswith("_") for part in re.split(r"[.\[\]]", clean_path))
        if "__" in clean_path or has_private:
            raise TemplateSecurityError(
                f"Prohibited access to private or dunder attribute in '{clean_path}'."
            )

        # Parse segments e.g. order.items[0].price -> ['order', 'items', 0, 'price']
        segments: list[str | int] = []
        raw_tokens = re.findall(r"([a-zA-Z0-9_]+|\[\d+\])", clean_path)
        for tok in raw_tokens:
            if tok.startswith("[") and tok.endswith("]"):
                segments.append(int(tok[1:-1]))
            else:
                segments.append(tok)

        if not segments:
            raise TemplateSyntaxError(f"Invalid path expression: '{clean_path}'")

        root_key = segments[0]
        if not isinstance(root_key, str):
            raise TemplateSyntaxError(f"Root variable identifier must be a string, got: {root_key}")

        if root_key not in context:
            if has_default:
                return None
            raise TemplateVariableMissingError(root_key)

        current: Any = context[root_key]

        for seg in segments[1:]:
            if current is None:
                if has_default:
                    return None
                raise TemplateVariableMissingError(clean_path)

            if isinstance(seg, int):
                if isinstance(current, (list, tuple)):
                    if 0 <= seg < len(current):
                        current = current[seg]
                    else:
                        if has_default:
                            return None
                        raise TemplateVariableMissingError(
                            f"{clean_path} (index {seg} out of range)"
                        )
                else:
                    if has_default:
                        return None
                    raise TemplateVariableMissingError(
                        f"Cannot index non-sequence with {seg} in '{clean_path}'"
                    )
            else:
                if isinstance(current, dict):
                    if seg in current:
                        current = current[seg]
                    elif has_default:
                        return None
                    else:
                        raise TemplateVariableMissingError(f"{clean_path} (key '{seg}' not found)")
                elif hasattr(current, seg):
                    current = getattr(current, seg)
                elif has_default:
                    return None
                else:
                    msg = f"{clean_path} (attribute '{seg}' not found)"
                    raise TemplateVariableMissingError(msg)

        return current

    def _eval_condition(self, expr: str, context: dict[str, Any]) -> bool:
        """Safely evaluate boolean condition with comparison operators without eval()."""
        clean = expr.strip()

        # Handle negation
        if clean.startswith("not "):
            return not self._eval_condition(clean[4:].strip(), context)

        # Comparison operators
        ops = ["==", "!=", ">=", "<=", ">", "<", " in "]
        for op in ops:
            if op in clean:
                left_str, right_str = clean.split(op, 1)
                left_val = self._eval_operand(left_str.strip(), context)
                right_val = self._eval_operand(right_str.strip(), context)

                op_clean = op.strip()
                if op_clean == "==":
                    return bool(left_val == right_val)
                if op_clean == "!=":
                    return bool(left_val != right_val)
                if op_clean == ">=":
                    return bool(left_val >= right_val)
                if op_clean == "<=":
                    return bool(left_val <= right_val)
                if op_clean == ">":
                    return bool(left_val > right_val)
                if op_clean == "<":
                    return bool(left_val < right_val)
                if op_clean == "in":
                    if isinstance(right_val, (list, tuple, set, str, dict)):
                        return bool(left_val in right_val)
                    return False

        # Single truthiness check
        val = self._eval_operand(clean, context)
        return bool(val)

    def _eval_operand(self, token: str, context: dict[str, Any]) -> Any:
        """Resolve a single operand: literal string, int, float, boolean, or variable."""
        # String literal
        if (token.startswith("'") and token.endswith("'")) or (
            token.startswith('"') and token.endswith('"')
        ):
            return token[1:-1]

        # Numeric literal
        if token.isdigit():
            return int(token)
        try:
            return float(token)
        except ValueError:
            pass

        # Boolean / Null literal
        if token.lower() in ("true", "yes"):
            return True
        if token.lower() in ("false", "no"):
            return False
        if token.lower() in ("null", "none"):
            return None

        # Variable lookup
        try:
            return self._resolve_path(token, context, has_default=True)
        except TemplateError:
            return None
