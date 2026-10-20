"""Standalone demonstration for Module B15: Prompts, Templates and Data Transforms.

Runs 100% keylessly and offline:
  uv run python examples/backend/b15.py
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from typing import Any

from nexura_runtime.contracts.context import ExecutionContext

from nexura_runtime.modules.data.errors import ConflictReducerRequiredError
from nexura_runtime.modules.data.module import DataTransformModule
from nexura_runtime.modules.data.transform import (
    classify_route,
    json_merge,
    json_select,
)
from nexura_runtime.modules.registry import default_registry
from nexura_runtime.modules.text.errors import (
    TemplateExpansionLimitError,
    TemplateSecurityError,
    TemplateVariableMissingError,
)
from nexura_runtime.modules.text.module import PromptTemplateModule
from nexura_runtime.modules.text.template import PromptTemplate


async def main() -> None:
    print("=== B15: Prompts, Templates and Data Transforms Demonstration ===\n")

    # Sample e-commerce order record
    order_record = {
        "id": "ORD-IND-9021",
        "customer": {
            "name": "राजेश शर्मा",
            "email": "rajesh@example.in",
            "is_vip": True,
            "address": {
                "city": "Bengaluru",
                "state": "Karnataka",
                "pincode": "560001",
            },
        },
        "items": [
            {"sku": "NX-01", "name": "Mechanical Keyboard", "price": 4500, "qty": 1},
            {"sku": "NX-02", "name": "Wireless Ergonomic Mouse", "price": 2200, "qty": 1},
        ],
        "subtotal": 6700,
        "tax": 1206,
        "total": 7906,
        "status": "confirmed",
    }

    # 1. PromptTemplate Rendering with Loops, Conditionals, and Unicode
    print("1. Prompt Template Rendering with Unicode and Iteration...")
    template_text = (
        "नमस्ते {{ customer.name }}!\n"
        "आपके ऑर्डर #{{ id }} की पुष्टि हो गई है।\n\n"
        "आइटम विवरण:\n"
        "{% for item in items %}"
        " - [{{ loop.index1 }}] {{ item.name | upper }}: ₹{{ item.price }} x {{ item.qty }}\n"
        "{% endfor %}\n"
        "कुल देय राशि: ₹{{ total }}\n"
        "{% if customer.is_vip %}"
        "★ वीआईपी प्राथमिकता शिपिंग लागू है (डिलीवरी: 24 घंटे)।\n"
        "{% else %}"
        "मानक डिलीवरी (3-5 कार्य दिवस)।\n"
        "{% endif %}"
        "डिलीवरी शहर: {{ customer.address.city | title }}"
    )

    prompt = PromptTemplate(template_text)
    rendered_prompt = prompt.render(order_record)
    print("--- Rendered Prompt ---")
    print(rendered_prompt)
    print("-----------------------\n")

    # 2. Strict Missing Variable Enforcement
    print("2. Demonstrating Missing Variable Rejection...")
    broken_template = PromptTemplate(
        "Hello {{ customer.name }}, your tracking code is {{ tracking_number }}."
    )
    try:
        broken_template.render(order_record)
        print("  [ERROR] Failed to reject missing variable!")
    except TemplateVariableMissingError as exc:
        print(f"  [SUCCESS] Eagerly caught missing variable: {exc}\n")

    # 3. Security Boundary: Zero Eval and Dunder Traversal Prohibition
    print("3. Demonstrating Security Boundary (Prohibition of Private/Dunder Attributes)...")
    malicious_template = PromptTemplate("Attempting escape: {{ customer.__class__ }}")
    try:
        malicious_template.render(order_record)
        print("  [ERROR] Failed to block dunder access!")
    except TemplateSecurityError as exc:
        print(f"  [SUCCESS] Blocked unsafe attribute access: {exc}\n")

    # 4. Bounded Output Expansion
    print("4. Demonstrating Bounded Template Expansion...")
    bounded_template = PromptTemplate(
        "Repeating items: {% for i in items %}{{ i.name }} {% endfor %}",
        max_output_length=20,
    )
    try:
        bounded_template.render(order_record)
        print("  [ERROR] Failed to enforce expansion limit!")
    except TemplateExpansionLimitError as exc:
        print(f"  [SUCCESS] Blocked oversized template expansion: {exc}\n")

    # 5. JSON Path Selection & Wildcard Projection
    print("5. Demonstrating JSON Path Selection & Wildcard Projections...")
    city = json_select(order_record, "customer.address.city")
    first_item_price = json_select(order_record, "items[0].price")
    all_item_names = json_select(order_record, "items[*].name")
    print(f"  Selected customer city: {city}")
    print(f"  Selected first item price: ₹{first_item_price}")
    print(f"  Wildcard projected item names: {all_item_names}\n")

    # 6. JSON Merge with Conflict Reducer Guarantees
    print("6. Demonstrating JSON Deep Merge & Conflict Reducer Policies...")
    order_update = {
        "status": "dispatched",
        "tracking_code": "IN-DEL-99120",
        "customer": {
            "contact_verified": True,
        },
    }

    # Merging without collision is automatic
    clean_merge = json_merge(order_record, order_update, conflict_policy="overwrite")
    print(f"  Status updated to: {clean_merge['status']}")
    print(f"  Nested contact_verified: {clean_merge['customer']['contact_verified']}")

    # Merge conflict requiring reducer
    colliding_update = {"total": 8500}
    try:
        json_merge(order_record, colliding_update, conflict_policy="error")
        print("  [ERROR] Failed to enforce reducer requirement on collision!")
    except ConflictReducerRequiredError as exc:
        print(f"  [SUCCESS] Correctly required conflict reducer on collision: {exc}")

    # Merge conflict resolved via custom reducer
    def max_value_reducer(key: str, val_a: object, val_b: object) -> object:
        if isinstance(val_a, (int, float)) and isinstance(val_b, (int, float)):
            return max(val_a, val_b)
        return val_b

    resolved_merge = json_merge(order_record, colliding_update, custom_reducer=max_value_reducer)
    print(f"  Resolved total via max reducer: ₹{resolved_merge['total']}\n")

    # 7. Classification Routing
    print("7. Demonstrating Classification Routing...")
    routing_rules: list[dict[str, Any]] = [
        {"field": "total", "operator": "gte", "value": 10000, "route": "high_value_escrow"},
        {"field": "customer.is_vip", "operator": "eq", "value": True, "route": "vip_express"},
        {"field": "status", "operator": "eq", "value": "confirmed", "route": "standard"},
    ]
    target_route = classify_route(order_record, routing_rules, default_route="manual_review")
    print(f"  Order classified into route: '{target_route}'\n")

    # 8. B00/B01 Module Invocations via Registry
    print("8. Executing Modules via B00/B01 Module Registry...")
    ctx = ExecutionContext(run_id="run_b15_demo", node_id="node_transforms")

    # Execute PromptTemplateModule
    prompt_mod = default_registry.get("nexura.prompt.template")
    assert isinstance(prompt_mod, PromptTemplateModule)
    p_result = await prompt_mod.execute(
        ctx,
        {
            "template": "Order {{ id }} is ready for {{ customer.name }}.",
            "variables": order_record,
        },
        {},
    )
    print(f"  Prompt module output: {p_result.outputs['rendered']}")

    # Execute DataTransformModule
    data_mod = default_registry.get("nexura.data.transform")
    assert isinstance(data_mod, DataTransformModule)
    d_result = await data_mod.execute(
        ctx,
        {
            "operation": "classify",
            "data": order_record,
            "rules": routing_rules,
        },
        {},
    )
    print(f"  Data module classified route: {d_result.outputs['route']}\n")

    # 9. CLI Execution via subprocess
    print("9. Executing via B01 CLI...")
    cli_payload = json.dumps(
        {
            "template": "Hello {{ name }}, your total is ₹{{ amount }}",
            "variables": {"name": "Aarav", "amount": 1500},
        }
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "nexura_runtime.modules",
            "run",
            "nexura.prompt.template",
            "--input",
            cli_payload,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    print(proc.stdout.strip())
    if proc.returncode == 0:
        print("\nCLI Execution: SUCCESS (code 0)")
    else:
        print(f"\nCLI Execution: FAILED (code {proc.returncode}): {proc.stderr}")

    print("\n=== B15 Example Completed Successfully ===")


if __name__ == "__main__":
    asyncio.run(main())
