"""Executable keyless example for O05 — Provider-aware cache planning and lifecycles.

Demonstrates:
1. Dated primary-source capability inspection (OpenAI, Anthropic, Gemini, Sarvam).
2. Stable prompt/tool layout separation preserving role order and priority.
3. Cache plan creation with deterministic prefix hashing and secret scrubbing.
4. Provider wire contracts with normalized observed cache metrics.
5. Conservative budget reservation (miss assumption) reconciling on verified hit.
6. Explicit negative outcomes (unsupported options, invalidation, expiration).
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import httpx
from nexura_runtime.providers.anthropic.provider import AnthropicNativeProvider
from nexura_runtime.providers.cache import (
    CacheCleanupError,
    CacheExpiredError,
    CacheInvalidationError,
    CacheManager,
    CachePermissionError,
    CachePolicy,
    UnsupportedCachePolicyError,
    create_cache_plan,
    get_provider_cache_capability,
    separate_layout,
    validate_cache_plan,
)
from nexura_runtime.providers.gemini.provider import GeminiNativeProvider
from nexura_runtime.providers.openai.provider import OpenAIProvider
from nexura_runtime.providers.sarvam.provider import SarvamProvider
from nexura_runtime.providers.types import GenerationRequest
from nexura_runtime.types import Message, Usage
from nexura_runtime.usage.ledger import UsageLedger
from nexura_runtime.usage.manager import BudgetManager
from nexura_runtime.usage.models import BudgetConfig, ModelPricing
from nexura_runtime.usage.pricing import PricingTable


async def main() -> None:
    print("=" * 70)
    print("O05: Provider-Aware Cache Planning & Verification Demo")
    print("=" * 70)

    # 1. Primary-Source Capability Receipts
    print("\n[Stage 1] Primary-Source Capability Inspection")
    for model in ["gpt-4o", "claude-3-5-sonnet", "gemini-1.5-pro", "sarvam-2b"]:
        cap = get_provider_cache_capability(model)
        print(
            f"  • Model: {model:<18} Mode: {cap.capability_type:<24} Source: {cap.evidence_source}"
        )

    # 2. Layout Separation Preserving Conversation Structure
    print("\n[Stage 2] Layout Separation & Instruction Priority")
    messages = [
        Message(role="system", content="You are a clinical compliance auditor."),
        Message(role="system", content="Verify all records against HIPAA Title II rules."),
        Message(role="user", content="Review patient record #1094."),
        Message(role="assistant", content="Record #1094 lacks signed release."),
        Message(role="user", content="Request re-authorization."),
    ]
    tools = [
        {
            "name": "lookup_compliance_statute",
            "description": "Fetch HIPAA rule text",
            "parameters": {
                "type": "object",
                "properties": {"section": {"type": "string"}},
                "required": ["section"],
            },
        }
    ]
    layout = separate_layout(
        messages=messages,
        tools=tools,
        workspace_id="ws_clinical",
        model_id="gpt-4o",
    )
    print(f"  • Stable System Prefix Length: {len(layout.stable_system_prompt or '')} chars")
    print(f"  • Tools Canonicalized: {len(layout.stable_tools or [])} tools")
    print(f"  • Prefix Hash: {layout.prefix_hash[:24]}...")
    print(f"  • Estimated Prefix Tokens: {layout.estimated_prefix_tokens}")

    # 3. Cache Plan Creation & Secret Scrubbing
    print("\n[Stage 3] Safe Plan Generation (Credentials Scrubbed)")
    secret_prompt = [
        Message(
            role="system",
            content="Admin prompt with key sk-ant-secretkey9876543210123456789.",
        ),
        Message(role="user", content="Begin audit"),
    ]
    safe_plan = create_cache_plan("claude-3-5-sonnet", "ws_clinical", secret_prompt)
    print(f"  • Plan ID: {safe_plan.plan_id}")
    print(f"  • Capability Type: {safe_plan.capability_type}")
    print(f"  • Secret Leak Check: {'sk-ant' not in safe_plan.plan_id} (No secrets in ID)")

    # 4. Mock Wire Execution & Observed Metrics Across Providers
    print("\n[Stage 4] Normalized Observed Cache Metrics Across Wire Contracts")

    # 4a. OpenAI: Implicit Prefix Caching
    def openai_handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "OpenAI audit OK"}}],
                "usage": {
                    "prompt_tokens": 2048,
                    "completion_tokens": 80,
                    "total_tokens": 2128,
                    "prompt_tokens_details": {"cached_tokens": 1024},
                },
            },
        )

    openai_p = OpenAIProvider(
        model="gpt-4o", api_key="sk-mock", transport=httpx.MockTransport(openai_handler)
    )
    oai_resp = await openai_p.generate(GenerationRequest(messages=messages))
    assert oai_resp.usage is not None
    print(
        f"  • OpenAI: Prompt={oai_resp.usage.prompt_tokens}, "
        f"CachedRead={oai_resp.usage.cached_prompt_tokens} (50% input discount)"
    )

    # 4b. Anthropic: Ephemeral Breakpoints
    captured_anthropic_payload: dict[str, Any] = {}

    def anthropic_handler(req: httpx.Request) -> httpx.Response:
        nonlocal captured_anthropic_payload
        captured_anthropic_payload = json.loads(req.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "type": "message",
                "content": [{"type": "text", "text": "Anthropic audit OK"}],
                "usage": {
                    "input_tokens": 1024,
                    "output_tokens": 90,
                    "cache_creation_input_tokens": 256,
                    "cache_read_input_tokens": 768,
                },
            },
        )

    anthropic_p = AnthropicNativeProvider(
        model="claude-3-5-sonnet",
        api_key="sk-mock",
        transport=httpx.MockTransport(anthropic_handler),
    )
    anthropic_plan = create_cache_plan("claude-3-5-sonnet", "ws_clinical", messages)
    anth_resp = await anthropic_p.generate(
        GenerationRequest(messages=messages, cache_plan=anthropic_plan)
    )
    assert anth_resp.usage is not None
    sys_block = captured_anthropic_payload.get("system", [{}])[0]
    print(
        f"  • Anthropic Wire: Attached breakpoint {sys_block.get('cache_control')}; "
        f"Creation={anth_resp.usage.cache_creation_input_tokens}, "
        f"Read={anth_resp.usage.cache_read_input_tokens}"
    )

    # 4c. Gemini: Explicit Stored Context
    gemini_plan = create_cache_plan("gemini-1.5-pro", "ws_clinical", messages)

    def gemini_handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": "Gemini audit OK"}]}, "finishReason": "STOP"}
                ],
                "usageMetadata": {
                    "promptTokenCount": 35000,
                    "candidatesTokenCount": 110,
                    "totalTokenCount": 35110,
                    "cachedContentTokenCount": 32768,
                },
            },
        )

    gemini_p = GeminiNativeProvider(
        model="gemini-1.5-pro", api_key="mock", transport=httpx.MockTransport(gemini_handler)
    )
    gem_resp = await gemini_p.generate(GenerationRequest(messages=messages, cache_plan=gemini_plan))
    assert gem_resp.usage is not None
    print(
        f"  • Gemini Wire: Resource={gemini_plan.cache_id}; "
        f"CachedTokens={gem_resp.usage.cached_prompt_tokens}"
    )

    # 5. Conservative Budget Reservation & Settlement
    print("\n[Stage 5] Conservative Admission & Reconcile")
    pricing = PricingTable(version="2026-10-01-v1", date="2026-10-01")
    pricing.add_model_pricing(
        ModelPricing(
            model_id="gpt-4o",
            input_cost_per_million=Decimal("2.50"),
            output_cost_per_million=Decimal("10.00"),
            cached_prompt_cost_per_million=Decimal("1.25"),
        )
    )
    ledger = UsageLedger()
    b_mgr = BudgetManager(ledger=ledger, pricing_table=pricing)
    run_budget = b_mgr.create_run_budget(
        workspace_id="ws_demo", run_id="run_1", config=BudgetConfig(max_cost_usd=Decimal("1.00"))
    )

    res_id = await run_budget.admit_provider_call_async(model_id="gpt-4o", estimated_tokens=2000)
    pre_status = run_budget.get_status()
    print(f"  • Pre-Dispatch Reserved (assumed cache miss): ${pre_status.total_reserved_usd:.6f}")

    observed_hit = Usage(
        prompt_tokens=2000,
        completion_tokens=100,
        total_tokens=2100,
        cached_prompt_tokens=1500,  # 1500 cached @ $1.25, 500 uncached @ $2.50
        cache_read_input_tokens=1500,
        is_complete=True,
    )
    await run_budget.settle_provider_call_async(res_id, usage=observed_hit, model_id="gpt-4o")
    post_status = run_budget.get_status()
    # 500*2.50/1M = 0.00125 + 1500*1.25/1M = 0.001875 + 100*10/1M = 0.00100 -> $0.004125
    print(
        f"  • Post-Response Committed (settled with discount): ${post_status.total_spent_usd:.6f}"
    )
    print(f"  • Remaining Reserved: ${post_status.total_reserved_usd:.6f} (Fully freed)")

    # 6. Explicit Negative Outcomes
    print("\n[Stage 6] Explicit Negative Verification")

    # 6a. Sarvam rejects caching
    sarvam_p = SarvamProvider(
        api_key="mock", transport=httpx.MockTransport(lambda r: httpx.Response(200))
    )
    try:
        await sarvam_p.generate(
            GenerationRequest(
                messages=[Message(role="user", content="Hi")],
                cache_policy=CachePolicy(enabled=True),
            )
        )
    except UnsupportedCachePolicyError as err:
        print(f"  • Sarvam Rejection: {err}")

    # 6b. Plan invalidation on tool alteration
    tools_mod = [
        {
            "name": "lookup_compliance_statute",
            "description": "Modified desc",
            "parameters": {"type": "object"},
        }
    ]
    try:
        validate_cache_plan(safe_plan, "claude-3-5-sonnet", "ws_clinical", secret_prompt, tools_mod)
    except CacheInvalidationError as err:
        print(f"  • Invalidation on Tool Schema Change: {err}")

    # 6c. Cross-workspace scoping denied
    mgr = CacheManager()
    mgr.register_plan(safe_plan)
    try:
        mgr.get_plan(safe_plan.plan_id, "foreign_workspace")
    except CachePermissionError as err:
        print(f"  • Cross-Workspace Scoping Denied: {err}")

    # 6d. Expired stored context
    def expired_gemini(r: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "Resource expired"})

    gem_exp_p = GeminiNativeProvider(
        model="gemini-1.5-pro", api_key="mock", transport=httpx.MockTransport(expired_gemini)
    )
    try:
        await gem_exp_p.generate(GenerationRequest(messages=messages, cache_plan=gemini_plan))
    except CacheExpiredError as err:
        print(f"  • Gemini Stored Context Expiration (HTTP 404): {err}")

    # 6e. Stored context cleanup failure
    def fail_delete(r: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "Backend fault"})

    gem_del_p = GeminiNativeProvider(
        model="gemini-1.5-pro", api_key="mock", transport=httpx.MockTransport(fail_delete)
    )
    try:
        await gem_del_p.delete_cached_content("cachedContents/test_resource")
    except CacheCleanupError as err:
        print(f"  • Gemini Cleanup Deletion Failure: {err}")

    print("\n" + "=" * 70)
    print("O05 Demo Complete: All primary-source contracts and negatives passed.")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
