# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Independent storage and native-expression evaluators used by the wire fakes."""

from __future__ import annotations

import ast
import json
import math
import re
from typing import Any

import httpx


class FakeAPI:
    def __init__(self, metric: str = "cosine") -> None:
        self.rows: dict[str, dict[str, dict[str, Any]]] = {}
        self.schemas: dict[str, dict[str, Any]] = {}
        self.metric = metric
        self.fail_status: int | None = None
        self.fail_at: int | None = None
        self.fail_code: Any = "OFFLINE_FAILURE"
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_status and (self.fail_at is None or self.fail_at == len(self.requests)):
            status, self.fail_status = self.fail_status, None
            return httpx.Response(
                status,
                json={
                    "code": self.fail_code,
                    "message": "s3cr3t request body is private",
                    "error": {"type": self.fail_code, "reason": "s3cr3t"},
                },
            )
        body = (
            json.loads(request.content) if request.content and request.url.path != "/_bulk" else {}
        )
        return self.handle(request, body)

    def handle(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        raise NotImplementedError

    def ranked(
        self,
        name: str,
        vector: list[float],
        limit: int,
        predicate: Any = None,
    ) -> list[dict[str, Any]]:
        def score(row: dict[str, Any]) -> float:
            other = row["vector"]
            if self.metric == "dot_product":
                return sum(a * b for a, b in zip(vector, other, strict=True))
            if self.metric == "euclidean":
                return -sum((a - b) ** 2 for a, b in zip(vector, other, strict=True))
            norm = math.sqrt(sum(a * a for a in vector) * sum(b * b for b in other))
            return sum(a * b for a, b in zip(vector, other, strict=True)) / norm if norm else 0

        rows = [r for r in self.rows[name].values() if predicate is None or predicate(r)]
        return sorted(rows, key=score, reverse=True)[:limit]


def mongo_match(row: dict[str, Any], expression: dict[str, Any]) -> bool:
    for key, operations in expression.items():
        if key == "$and":
            if not all(mongo_match(row, sub) for sub in operations):
                return False
        elif key == "$or":
            if not any(mongo_match(row, sub) for sub in operations):
                return False
        else:
            operations = operations if isinstance(operations, dict) else {"$eq": operations}
            for op, target in operations.items():
                value = row.get(key)
                if op == "$eq" and value != target:
                    return False
                if op == "$ne" and value == target:
                    return False
                if op == "$in" and value not in target:
                    return False
                if op == "$nin" and value in target:
                    return False
                if op in ("$gt", "$gte", "$lt", "$lte"):
                    if value is None:
                        return False
                    if not {
                        "$gt": value > target,
                        "$gte": value >= target,
                        "$lt": value < target,
                        "$lte": value <= target,
                    }[op]:
                        return False
    return True


def elastic_match(row: dict[str, Any], expr: dict[str, Any]) -> bool:
    if "match_all" in expr:
        return True
    if "term" in expr:
        return all(row.get(k) == v for k, v in expr["term"].items())
    if "range" in expr:
        return all(
            mongo_match(row, {k: {"$" + op: v for op, v in ops.items()}})
            for k, ops in expr["range"].items()
        )
    if "bool" in expr:
        clause = expr["bool"]
        for key in ("filter", "must"):
            for item in clause.get(key, []):
                if not elastic_match(row, item):
                    return False
        if any(elastic_match(row, item) for item in clause.get("must_not", [])):
            return False
        return not (
            "should" in clause
            and sum(elastic_match(row, e) for e in clause["should"])
            < clause.get("minimum_should_match", 1)
        )
    raise AssertionError(expr)


def milvus_match(row: dict[str, Any], expression: str) -> bool:
    if not expression:
        return True
    tree = ast.parse(re.sub(r"\bfalse\b", "False", expression), mode="eval")

    def visit(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Name):
            return row.get(node.id)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.List):
            return [visit(item) for item in node.elts]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -visit(node.operand)
        if isinstance(node, ast.BoolOp):
            values = [visit(v) for v in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.Compare):
            left, right = visit(node.left), visit(node.comparators[0])
            op = node.ops[0]
            if isinstance(op, ast.In):
                return left in right
            if isinstance(op, ast.Eq):
                return left == right
            if left is None:
                return False
            if isinstance(op, ast.Gt):
                return left > right
            if isinstance(op, ast.GtE):
                return left >= right
            if isinstance(op, ast.Lt):
                return left < right
            if isinstance(op, ast.LtE):
                return left <= right
        raise AssertionError(ast.dump(node))

    return bool(visit(tree))


def graphql_value(text: str, start: int = 0) -> tuple[Any, int]:
    """Parse a GraphQL input value, independently of the adapter serializer."""
    pos = start
    while pos < len(text) and (text[pos].isspace() or text[pos] == ","):
        pos += 1
    if text[pos] == "{":
        result = {}
        pos += 1
        while True:
            while text[pos].isspace() or text[pos] == ",":
                pos += 1
            if text[pos] == "}":
                return result, pos + 1
            key = re.match(r"\w+", text[pos:])
            assert key
            pos += len(key.group())
            assert text[pos] == ":"
            value, pos = graphql_value(text, pos + 1)
            result[key.group()] = value
    if text[pos] == "[":
        values = []
        pos += 1
        while True:
            while text[pos].isspace() or text[pos] == ",":
                pos += 1
            if text[pos] == "]":
                return values, pos + 1
            value, pos = graphql_value(text, pos)
            values.append(value)
    if (
        text[pos] == '"'
        or text[pos] in "-0123456789"
        or text.startswith(("true", "false", "null"), pos)
    ):
        value, length = json.JSONDecoder().raw_decode(text[pos:])
        return value, pos + length
    name = re.match(r"\w+", text[pos:])
    assert name
    return name.group(), pos + len(name.group())


def weaviate_match(row: dict[str, Any], where: dict[str, Any]) -> bool:
    op = where["operator"]
    if op in ("And", "Or"):
        values = [weaviate_match(row, e) for e in where["operands"]]
        return all(values) if op == "And" else any(values)
    value = next(v for k, v in where.items() if k.startswith("value"))
    operation = {
        "Equal": "$eq",
        "GreaterThan": "$gt",
        "GreaterThanEqual": "$gte",
        "LessThan": "$lt",
        "LessThanEqual": "$lte",
    }[op]
    return mongo_match(row, {where["path"][0]: {operation: value}})
