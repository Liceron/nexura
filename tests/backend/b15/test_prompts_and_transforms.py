"""Comprehensive acceptance and unit tests for Module B15:
Prompts, Templates and Data Transforms.
"""

from __future__ import annotations

import pytest
from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.contracts.errors import SchemaValidationError

from nexura_runtime.modules.data.errors import (
    ClassificationRoutingError,
    ConflictReducerRequiredError,
    JsonSelectError,
)
from nexura_runtime.modules.data.module import DataTransformModule
from nexura_runtime.modules.data.transform import (
    classify_route,
    json_merge,
    json_select,
    reduce_data,
)
from nexura_runtime.modules.registry import default_registry
from nexura_runtime.modules.text.errors import (
    TemplateExpansionLimitError,
    TemplateSecurityError,
    TemplateSyntaxError,
    TemplateVariableMissingError,
)
from nexura_runtime.modules.text.module import PromptTemplateModule
from nexura_runtime.modules.text.template import PromptTemplate

# ==============================================================================
# 1. PromptTemplate Acceptance Tests
# ==============================================================================


def test_prompt_template_unicode_round_trip() -> None:
    """Acceptance case: Unicode characters, Indic scripts, and currencies round-trip."""
    tpl = PromptTemplate("नमस्ते {{ user.name }}! आपका शेष ₹{{ balance }} है। 🚀")
    result = tpl.render({"user": {"name": "अनन्या"}, "balance": 12500})
    assert result == "नमस्ते अनन्या! आपका शेष ₹12500 है। 🚀"


def test_prompt_template_missing_variables_fail() -> None:
    """Acceptance case: Referencing missing variables eagerly fails with typed error."""
    tpl = PromptTemplate("Hello {{ first_name }} {{ last_name }}!")
    with pytest.raises(TemplateVariableMissingError) as exc_info:
        tpl.render({"first_name": "Vikram"})
    assert exc_info.value.variable_name == "last_name"


def test_prompt_template_default_filter_fallback() -> None:
    """Acceptance case: Default filter provides safe fallback for absent variables."""
    tpl = PromptTemplate(
        "Role: {{ user.role | default('guest') }} | Status: {{ user.status | default: 'active' }}"
    )
    # Empty context
    result = tpl.render({})
    assert result == "Role: guest | Status: active"

    # Provided context
    result2 = tpl.render({"user": {"role": "admin", "status": "suspended"}})
    assert result2 == "Role: admin | Status: suspended"


def test_prompt_template_no_eval_and_dunder_rejection() -> None:
    """Acceptance case: Zero eval; accessing private or dunder attributes is prohibited."""
    tpl = PromptTemplate("Attempt: {{ data.__class__ }}")
    with pytest.raises(TemplateSecurityError, match="Prohibited access"):
        tpl.render({"data": {"key": "val"}})

    tpl_private = PromptTemplate("Attempt: {{ data._secret }}")
    with pytest.raises(TemplateSecurityError, match="Prohibited access"):
        tpl_private.render({"data": {"_secret": "classified"}})


def test_prompt_template_oversized_expansion_bounded() -> None:
    """Acceptance case: Rendered output exceeding maximum character bound fails eagerly."""
    tpl = PromptTemplate("Welcome to {{ portal_name }}!", max_output_length=20)
    with pytest.raises(TemplateExpansionLimitError, match="exceeds maximum allowed limit"):
        tpl.render({"portal_name": "Nexura Enterprise Autonomous Agent Platform"})


def test_prompt_template_loops_and_conditionals() -> None:
    """Acceptance case: Deterministic loop rendering and boolean conditionals."""
    template_str = (
        "{% if enabled %}"
        "Items:\n"
        "{% for item in items %}"
        "{{ loop.index1 }}. {{ item.name }} ({{ item.qty }})\n"
        "{% endfor %}"
        "{% else %}"
        "Disabled"
        "{% endif %}"
    )
    tpl = PromptTemplate(template_str)

    # Enabled branch
    out_enabled = tpl.render(
        {
            "enabled": True,
            "items": [{"name": "A", "qty": 2}, {"name": "B", "qty": 5}],
        }
    )
    assert "1. A (2)" in out_enabled
    assert "2. B (5)" in out_enabled

    # Disabled branch
    out_disabled = tpl.render({"enabled": False, "items": []})
    assert out_disabled.strip() == "Disabled"


def test_prompt_template_unclosed_block_syntax_error() -> None:
    """Acceptance case: Malformed or unclosed template blocks raise TemplateSyntaxError."""
    with pytest.raises(TemplateSyntaxError, match="Unclosed block"):
        PromptTemplate("{% if condition %}Text without endif")

    with pytest.raises(TemplateSyntaxError, match="Unexpected '{% else %}'"):
        PromptTemplate("Text {% else %} without if")


# ==============================================================================
# 2. JSON Selection and Merging Acceptance Tests
# ==============================================================================


def test_json_select_nested_and_wildcard() -> None:
    """Acceptance case: JSON selection supports dot notation, array indexes, and wildcards."""
    payload = {
        "order": {
            "customer": {"name": "Priya", "cities": ["Mumbai", "Pune"]},
            "lines": [
                {"sku": "SKU-1", "price": 100},
                {"sku": "SKU-2", "price": 250},
            ],
        }
    }
    assert json_select(payload, "order.customer.name") == "Priya"
    assert json_select(payload, "order.customer.cities[1]") == "Pune"
    assert json_select(payload, "order.lines[*].price") == [100, 250]
    assert json_select(payload, "order.lines[*].sku") == ["SKU-1", "SKU-2"]
    assert json_select(payload, "nonexistent.path", default="fallback") == "fallback"


def test_json_select_strict_mode() -> None:
    """Acceptance case: Strict mode raises JsonSelectError on missing paths."""
    payload = {"user": {"id": 10}}
    with pytest.raises(JsonSelectError, match="Key 'email' not found"):
        json_select(payload, "user.email", strict=True)


def test_json_merge_conflicts_require_reducer() -> None:
    """Acceptance case: Colliding keys without a reducer policy
    raise ConflictReducerRequiredError.
    """
    base = {"config": {"retries": 3, "timeout": 30}, "owner": "alice"}
    override = {"config": {"retries": 5}, "owner": "bob"}

    # Fails by default when conflict_policy="error"
    with pytest.raises(ConflictReducerRequiredError) as exc_info:
        json_merge(base, override, conflict_policy="error")
    assert exc_info.value.key == "retries" or exc_info.value.key == "owner"


def test_json_merge_reducer_policies() -> None:
    """Acceptance case: Deterministic merge conflict reducer policies."""
    base = {
        "tags": ["alpha", "beta"],
        "score": 10,
        "mode": "manual",
    }
    override = {
        "tags": ["gamma"],
        "score": 25,
        "mode": "auto",
    }

    # 'overwrite' policy
    m_overwrite = json_merge(base, override, conflict_policy="overwrite")
    assert m_overwrite["score"] == 25
    assert m_overwrite["mode"] == "auto"

    # 'keep' policy
    m_keep = json_merge(base, override, conflict_policy="keep")
    assert m_keep["score"] == 10
    assert m_keep["mode"] == "manual"

    # 'concat' policy
    m_concat = json_merge(base, override, conflict_policy="concat")
    assert m_concat["tags"] == ["alpha", "beta", "gamma"]

    # Custom reducer function
    def add_scores(key: str, a: object, b: object) -> object:
        if key == "score" and isinstance(a, int) and isinstance(b, int):
            return a + b
        return b

    m_custom = json_merge(base, override, custom_reducer=add_scores)
    assert m_custom["score"] == 35


# ==============================================================================
# 3. Classification Routing Acceptance Tests
# ==============================================================================


def test_classify_route_priority_and_operators() -> None:
    """Acceptance case: Deterministic classification routing with priority rules."""
    rules = [
        {"field": "user.vip", "operator": "eq", "value": True, "route": "vip_queue"},
        {"field": "cart.total", "operator": "gte", "value": 5000, "route": "high_value_queue"},
        {"field": "cart.total", "operator": "lt", "value": 5000, "route": "standard_queue"},
    ]

    # VIP takes precedence
    data_vip = {"user": {"vip": True}, "cart": {"total": 1000}}
    assert classify_route(data_vip, rules) == "vip_queue"

    # High value non-VIP
    data_high = {"user": {"vip": False}, "cart": {"total": 8000}}
    assert classify_route(data_high, rules) == "high_value_queue"

    # Standard
    data_std = {"user": {"vip": False}, "cart": {"total": 2000}}
    assert classify_route(data_std, rules) == "standard_queue"


def test_classify_route_unmatched_default() -> None:
    """Acceptance case: Unmatched classification returns default or raises error."""
    rules = [{"field": "status", "operator": "eq", "value": "paid", "route": "fulfill"}]

    data = {"status": "pending"}
    assert classify_route(data, rules, default_route="review") == "review"

    with pytest.raises(ClassificationRoutingError):
        classify_route(data, rules, default_route=None)


def test_reduce_data_operations() -> None:
    """Acceptance case: Safe deterministic array reduction operations."""
    items = [{"val": 10}, {"val": 20}, {"val": 30}, {"val": 20}]

    assert reduce_data(items, "sum", field="val") == 80
    assert reduce_data(items, "avg", field="val") == 20.0
    assert reduce_data(items, "min", field="val") == 10
    assert reduce_data(items, "max", field="val") == 30
    assert reduce_data(items, "count") == 4
    assert reduce_data(items, "dedup", field="val") == [10, 20, 30]


# ==============================================================================
# 4. B00/B01 Module Integration Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_prompt_template_module_execution() -> None:
    """Acceptance case: PromptTemplateModule executed via B00/B01 contracts."""
    mod = default_registry.get("nexura.prompt.template")
    assert isinstance(mod, PromptTemplateModule)

    ctx = ExecutionContext(run_id="run-p1", node_id="node-p1")
    inputs = {
        "template": "Hello {{ name }}! Your order #{{ order_id }} is ready.",
        "variables": {"name": "Dev", "order_id": "ORD-101"},
    }
    config = {
        "max_output_length": 5000,
    }

    result = await mod.execute(ctx, inputs, config)
    assert result.status == "success"
    assert result.outputs["rendered"] == "Hello Dev! Your order #ORD-101 is ready."
    assert result.outputs["length"] == len(result.outputs["rendered"])
    assert "name" in result.outputs["variables_used"]


@pytest.mark.asyncio
async def test_prompt_template_user_content_cannot_alter_config() -> None:
    """Acceptance case: User variables cannot override module execution configuration."""
    mod = PromptTemplateModule()
    ctx = ExecutionContext(run_id="run-p2", node_id="node-p2")

    # In config, max_output_length is set to 25
    config = {"max_output_length": 25}
    # In user variables, malicious payload attempts to specify large max_output_length
    inputs = {
        "template": "Very long content: {{ text }}",
        "variables": {
            "text": "This is a very long string that definitely exceeds 25 characters limit.",
            "max_output_length": 1_000_000,  # Should be ignored!
        },
    }

    with pytest.raises(TemplateExpansionLimitError):
        await mod.execute(ctx, inputs, config)


@pytest.mark.asyncio
async def test_data_transform_module_execution() -> None:
    """Acceptance case: DataTransformModule executed via B00/B01 contracts."""
    mod = default_registry.get("nexura.data.transform")
    assert isinstance(mod, DataTransformModule)

    ctx = ExecutionContext(run_id="run-d1", node_id="node-d1")

    # Test select operation
    res_select = await mod.execute(
        ctx,
        {"operation": "select", "data": {"a": {"b": [10, 20]}}, "path": "a.b[1]"},
        {},
    )
    assert res_select.outputs["result"] == 20

    # Test merge operation
    res_merge = await mod.execute(
        ctx,
        {
            "operation": "merge",
            "base": {"x": 1, "y": 2},
            "override": {"y": 3, "z": 4},
            "conflict_policy": "overwrite",
        },
        {},
    )
    assert res_merge.outputs["result"] == {"x": 1, "y": 3, "z": 4}

    # Test classify operation
    res_classify = await mod.execute(
        ctx,
        {
            "operation": "classify",
            "data": {"status": "refunded"},
            "rules": [{"field": "status", "operator": "eq", "value": "refunded", "route": "audit"}],
        },
        {},
    )
    assert res_classify.outputs["route"] == "audit"


@pytest.mark.asyncio
async def test_modules_schema_validation_failures() -> None:
    """Acceptance case: Invalid schemas fail validation eagerly."""
    p_mod = PromptTemplateModule()
    d_mod = DataTransformModule()
    ctx = ExecutionContext(run_id="run-err", node_id="node-err")

    # Missing template in PromptTemplateModule
    with pytest.raises(SchemaValidationError):
        await p_mod.execute(ctx, {}, {})

    # Missing operation in DataTransformModule
    with pytest.raises(SchemaValidationError):
        await d_mod.execute(ctx, {}, {})

    # Invalid operation in DataTransformModule
    with pytest.raises(SchemaValidationError):
        await d_mod.execute(ctx, {"operation": "invalid_op"}, {})
