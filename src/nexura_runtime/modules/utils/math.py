# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Small arithmetic AST interpreter; no Python execution."""

import ast
import math
import operator
from typing import Any

from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.errors import SchemaValidationError

from nexura_runtime.modules._component import OBJECT, ComponentModule, manifest

_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_COMPARE = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


def _number(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaValidationError("Arithmetic requires numeric values (not booleans).")
    if abs(value) > 1e100 or not math.isfinite(value):
        raise SchemaValidationError("Arithmetic magnitude exceeds 1e100 or is non-finite.")
    return int(value) if isinstance(value, int) else float(value)


def _calculate(node: ast.AST, variables: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return _number(node.value)
    if isinstance(node, ast.Name):
        if node.id not in variables:
            raise SchemaValidationError(f"Unknown arithmetic variable: {node.id}")
        return _number(variables[node.id])
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _number(_calculate(node.operand, variables))
        return _number(value if isinstance(node.op, ast.UAdd) else -value)
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        left = _number(_calculate(node.left, variables))
        right = _number(_calculate(node.right, variables))
        if isinstance(node.op, ast.Pow) and abs(right) > 16:
            raise SchemaValidationError("Arithmetic exponent exceeds 16 in magnitude.")
        return _number(_BINARY[type(node.op)](left, right))
    if isinstance(node, ast.Compare):
        left = _number(_calculate(node.left, variables))
        matched = True
        for op, rhs in zip(node.ops, node.comparators, strict=True):
            if type(op) not in _COMPARE:
                raise SchemaValidationError("Unsupported arithmetic comparison.")
            right = _number(_calculate(rhs, variables))
            matched = matched and _COMPARE[type(op)](left, right)
            left = right
        return matched
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
        name = node.func.id
        args = [_number(_calculate(arg, variables)) for arg in node.args]
        if name in {"min", "max"} and 1 <= len(args) <= 16:
            return (min if name == "min" else max)(args)
        if name == "abs" and len(args) == 1:
            return abs(args[0])
        if name == "round" and 1 <= len(args) <= 2:
            digits = args[1] if len(args) == 2 else 0
            if not isinstance(digits, int) or abs(digits) > 16:
                raise SchemaValidationError("Round digits must be an integer from -16 to 16.")
            return round(args[0], digits)
    raise SchemaValidationError(f"Unsupported arithmetic syntax: {type(node).__name__}")


class MathModule(ComponentModule):
    manifest = manifest(
        "utils",
        "math",
        "Interpret numeric arithmetic and comparisons with min/max/round/abs; exponent cap 16.",
        {"expression": {"type": "string", "minLength": 1, "maxLength": 1024}, "variables": OBJECT},
        ["expression"],
        {"value": {"type": ["number", "boolean"]}},
        {"expression": "round(price * 1.2, 2)", "variables": {"price": 10}},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        variables = inputs.get("variables", {})
        for name, value in variables.items():
            if not name.isidentifier():
                raise SchemaValidationError("Arithmetic variables must have identifier names.")
            _number(value)
        try:
            tree = ast.parse(inputs["expression"], mode="eval")
        except (SyntaxError, RecursionError) as exc:
            raise SchemaValidationError("Invalid arithmetic expression.") from exc
        if len(list(ast.walk(tree))) > 128:
            raise SchemaValidationError("Arithmetic expression exceeds 128 AST nodes.")
        return {"value": _calculate(tree.body, variables)}
