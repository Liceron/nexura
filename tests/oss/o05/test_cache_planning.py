"""Tests for O05 — Provider-aware cache planning, wire contracts, and lifecycles."""

from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any

import httpx
import pytest
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
from nexura_runtime.usage.models import BudgetConfig, ModelPricing, TokenUsage
from nexura_runtime.usage.pricing import PricingTable


class TestPrimarySourceCapabilityRegistry:
    """Verifies dated primary-source capability receipts for all supported providers."""

    def test_dated_capability_profiles(self) -> None:
        # OpenAI: Implicit prefix
        oai = get_provider_cache_capability("gpt-4o")
        assert oai.capability_type == "implicit_prefix"
        assert oai.min_tokens == 1024
        assert oai.supports_custom_ttl is False
        assert oai.wire_response_read_key == "prompt_tokens_details.cached_tokens"
        assert "Oct 2024" in oai.evidence_source

        # Anthropic: Ephemeral routing breakpoints
        claude = get_provider_cache_capability("claude-3-5-sonnet")
        assert claude.capability_type == "ephemeral_routing"
        assert claude.min_tokens == 1024
        assert claude.max_breakpoints == 4
        assert claude.supports_custom_ttl is False
        assert claude.default_ttl_seconds == 300.0
        assert claude.wire_response_read_key == "usage.cache_read_input_tokens"
        assert claude.wire_response_write_key == "usage.cache_creation_input_tokens"
        assert "Aug 2024" in claude.evidence_source

        # Claude Haiku has 2048 token boundary
        haiku = get_provider_cache_capability("claude-3-5-haiku")
        assert haiku.min_tokens == 2048

        # Gemini: Explicit stored context
        gemini = get_provider_cache_capability("gemini-1.5-pro")
        assert gemini.capability_type == "explicit_stored_context"
        assert gemini.min_tokens == 32768
        assert gemini.supports_custom_ttl is True
        assert gemini.wire_response_read_key == "usageMetadata.cachedContentTokenCount"
        assert "June 2024" in gemini.evidence_source

        # Sarvam: Unsupported
        sarvam = get_provider_cache_capability("sarvam-2b")
        assert sarvam.capability_type == "unsupported"
        assert sarvam.default_ttl_seconds is None


class TestLayoutSeparationAndSanitization:
    """Verifies layout separation preserves message order, roles, and strips secrets."""

    def test_layout_separation_preserves_order_and_priority(self) -> None:
        messages = [
            Message(role="system", content="You are a financial advisor."),
            Message(role="system", content="Strictly cite tax laws."),
            Message(role="user", content="What is Section 80C?"),
            Message(role="assistant", content="Section 80C allows deductions up to 1.5 Lakh."),
            Message(role="user", content="Does it apply to ELSS?"),
        ]
        tools = [
            {
                "name": "lookup_tax_code",
                "description": "Look up tax section",
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
            workspace_id="ws_fin",
            model_id="gpt-4o",
        )

        assert layout.stable_system_prompt == (
            "You are a financial advisor.\n\nStrictly cite tax laws."
        )
        assert layout.stable_tools is not None
        assert len(layout.stable_tools) == 1
        assert layout.prefix_hash is not None
        assert layout.estimated_prefix_tokens > 0

    def test_layout_secret_scrubbing(self) -> None:
        messages = [
            Message(
                role="system",
                content="Use secret sk-ant-api03-abcdef123456789012345678 to authenticate.",
            ),
            Message(role="user", content="Ping"),
        ]
        layout = separate_layout(
            messages=messages,
            workspace_id="ws_sec",
            model_id="claude-3-5-sonnet",
        )
        plan = create_cache_plan(
            model_id="claude-3-5-sonnet",
            workspace_id="ws_sec",
            messages=messages,
        )
        # Prefix hash and plan ID should never expose raw key
        assert "sk-ant" not in plan.plan_id
        assert "sk-ant" not in layout.prefix_hash


class TestOpenAIWireContract:
    """Verifies OpenAI wire contract: implicit prefix caching and cached token metrics."""

    @pytest.mark.asyncio
    async def test_openai_implicit_caching_wire_contract(self) -> None:
        captured_payload: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal captured_payload
            captured_payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-test-123",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Tax summary generated."},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 2048,
                        "completion_tokens": 128,
                        "total_tokens": 2176,
                        "prompt_tokens_details": {
                            "cached_tokens": 1024,
                            "audio_tokens": 0,
                        },
                    },
                },
            )

        transport = httpx.MockTransport(handler)
        provider = OpenAIProvider(
            model="gpt-4o",
            api_key="sk-mock-key",
            transport=transport,
        )

        messages = [
            Message(role="system", content="A long system prompt exceeding 1024 tokens..."),
            Message(role="user", content="Summarize deductions."),
        ]
        plan = create_cache_plan("gpt-4o", "ws_test", messages)
        req = GenerationRequest(messages=messages, cache_plan=plan)

        resp = await provider.generate(req)
        assert resp.usage is not None
        assert resp.usage.prompt_tokens == 2048
        assert resp.usage.completion_tokens == 128
        assert resp.usage.cached_prompt_tokens == 1024
        assert resp.usage.cache_read_input_tokens == 1024

    @pytest.mark.asyncio
    async def test_openai_unsupported_cache_policy_rejected(self) -> None:
        transport = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
        provider = OpenAIProvider(model="gpt-4o", api_key="sk-mock", transport=transport)

        # 1. Ephemeral routing requested on OpenAI fails explicitly
        req1 = GenerationRequest(
            messages=[Message(role="user", content="Hi")],
            cache_policy=CachePolicy(preferred_mode="ephemeral_routing"),
        )
        with pytest.raises(UnsupportedCachePolicyError) as exc_info:
            await provider.generate(req1)
        assert "ephemeral_routing" in str(exc_info.value)

        # 2. Custom TTL requested on OpenAI fails explicitly
        req2 = GenerationRequest(
            messages=[Message(role="user", content="Hi")],
            cache_policy=CachePolicy(ttl_seconds=600.0),
        )
        with pytest.raises(UnsupportedCachePolicyError) as exc_info2:
            await provider.generate(req2)
        assert exc_info2.value.option == "ttl_seconds"


class TestAnthropicWireContract:
    """Verifies Anthropic wire contract: ephemeral breakpoints and read/write metrics."""

    @pytest.mark.asyncio
    async def test_anthropic_ephemeral_wire_contract(self) -> None:
        captured_payload: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal captured_payload
            captured_payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={
                    "id": "msg_01XyZ",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Claude response"}],
                    "model": "claude-3-5-sonnet-20241022",
                    "stop_reason": "end_turn",
                    "usage": {
                        "input_tokens": 1024,
                        "output_tokens": 150,
                        "cache_creation_input_tokens": 512,
                        "cache_read_input_tokens": 512,
                    },
                },
            )

        transport = httpx.MockTransport(handler)
        provider = AnthropicNativeProvider(
            model="claude-3-5-sonnet",
            api_key="sk-ant-test",
            transport=transport,
        )

        messages = [
            Message(role="system", content="Stable system prompt for Anthropic."),
            Message(role="user", content="Hello Claude"),
        ]
        plan = create_cache_plan("claude-3-5-sonnet", "ws_anthropic", messages)
        req = GenerationRequest(messages=messages, cache_plan=plan)

        resp = await provider.generate(req)

        # Verify wire request attached cache_control breakpoint
        assert "system" in captured_payload
        sys_blocks = captured_payload["system"]
        assert isinstance(sys_blocks, list)
        assert sys_blocks[0]["cache_control"] == {"type": "ephemeral"}

        # Verify observed response metrics
        assert resp.usage is not None
        # P37 B3: Anthropic input_tokens excludes cache reads/writes; prompt_tokens is the
        # full prompt (input + cache_creation + cache_read), per the prompt-caching docs.
        assert resp.usage.prompt_tokens == 1024 + 512 + 512
        assert resp.usage.completion_tokens == 150
        assert resp.usage.cache_creation_input_tokens == 512
        assert resp.usage.cache_read_input_tokens == 512
        assert resp.usage.cached_prompt_tokens == 512

    @pytest.mark.asyncio
    async def test_anthropic_unsupported_cache_policy_rejected(self) -> None:
        transport = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
        provider = AnthropicNativeProvider(
            model="claude-3-5-sonnet", api_key="sk-ant", transport=transport
        )

        # Stored context mode on Anthropic fails explicitly
        req1 = GenerationRequest(
            messages=[Message(role="user", content="Hi")],
            cache_policy=CachePolicy(preferred_mode="explicit_stored_context"),
        )
        with pytest.raises(UnsupportedCachePolicyError):
            await provider.generate(req1)

        # Custom TTL != 300 on Anthropic fails explicitly
        req2 = GenerationRequest(
            messages=[Message(role="user", content="Hi")],
            cache_policy=CachePolicy(ttl_seconds=3600.0),
        )
        with pytest.raises(UnsupportedCachePolicyError):
            await provider.generate(req2)


class TestGeminiWireContract:
    """Verifies Gemini wire contract: explicit stored context, expiration, and cleanup."""

    @pytest.mark.asyncio
    async def test_gemini_stored_context_wire_contract(self) -> None:
        captured_payload: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal captured_payload
            captured_payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [{"text": "Gemini context response"}],
                                "role": "model",
                            },
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 35000,
                        "candidatesTokenCount": 200,
                        "totalTokenCount": 35200,
                        "cachedContentTokenCount": 32768,
                    },
                },
            )

        transport = httpx.MockTransport(handler)
        provider = GeminiNativeProvider(
            model="gemini-1.5-pro",
            api_key="mock-gemini-key",
            transport=transport,
        )

        messages = [
            Message(role="system", content="A large 32k corpus..."),
            Message(role="user", content="Query"),
        ]
        plan = create_cache_plan("gemini-1.5-pro", "ws_gem", messages)
        req = GenerationRequest(messages=messages, cache_plan=plan)

        resp = await provider.generate(req)

        # Verify cachedContent was sent in wire request
        assert "cachedContent" in captured_payload
        assert captured_payload["cachedContent"] == plan.cache_id

        # Verify usage extracted cachedContentTokenCount
        assert resp.usage is not None
        assert resp.usage.prompt_tokens == 35000
        assert resp.usage.cached_prompt_tokens == 32768
        assert resp.usage.cache_read_input_tokens == 32768

    @pytest.mark.asyncio
    async def test_gemini_expired_stored_context_raises_cache_expired_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                404,
                json={
                    "error": {
                        "code": 404,
                        "message": "Resource 'cachedContents/123' was not found",
                        "status": "NOT_FOUND",
                    }
                },
            )

        transport = httpx.MockTransport(handler)
        provider = GeminiNativeProvider(
            model="gemini-1.5-pro",
            api_key="mock-key",
            transport=transport,
        )

        messages = [Message(role="user", content="Hi")]
        plan = create_cache_plan("gemini-1.5-pro", "ws_gem", messages)
        req = GenerationRequest(messages=messages, cache_plan=plan)

        with pytest.raises(CacheExpiredError) as exc_info:
            await provider.generate(req)
        assert exc_info.value.cache_id == plan.cache_id

    @pytest.mark.asyncio
    async def test_gemini_cleanup_deletion_and_failure(self) -> None:
        deleted_ids: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "DELETE":
                if "fail" in request.url.path:
                    return httpx.Response(500, json={"error": "Internal Error"})
                deleted_ids.append(request.url.path)
                return httpx.Response(200, json={})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        provider = GeminiNativeProvider(
            model="gemini-1.5-pro",
            api_key="mock-key",
            transport=transport,
        )

        # Successful deletion
        res = await provider.delete_cached_content("cachedContents/good_id")
        assert res is True
        assert any("good_id" in p for p in deleted_ids)

        # Failed deletion raises CacheCleanupError
        with pytest.raises(CacheCleanupError) as exc_info:
            await provider.delete_cached_content("cachedContents/fail_id")
        assert "fail_id" in str(exc_info.value)


class TestSarvamAIRejection:
    """Verifies that Sarvam AI fails explicitly when prompt caching is attempted."""

    @pytest.mark.asyncio
    async def test_sarvam_cache_policy_fails_explicitly(self) -> None:
        transport = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
        provider = SarvamProvider(api_key="mock-sarvam", transport=transport)

        # 1. create_cache_plan fails on sarvam model
        with pytest.raises(UnsupportedCachePolicyError) as exc1:
            create_cache_plan(
                model_id="sarvam-2b",
                workspace_id="ws_sarvam",
                messages=[Message(role="user", content="Hi")],
            )
        assert "sarvam" in str(exc1.value).lower()

        # 2. Passing cache_policy to SarvamProvider fails explicitly
        req = GenerationRequest(
            messages=[Message(role="user", content="Hi")],
            cache_policy=CachePolicy(enabled=True),
        )
        with pytest.raises(UnsupportedCachePolicyError) as exc2:
            await provider.generate(req)
        assert "Sarvam AI" in str(exc2.value)


class TestCacheInvalidationAndNegativeCases:
    """Verifies cache plan invalidation, TTL expiration, and scope permissions."""

    def test_model_mismatch_invalidates_plan(self) -> None:
        messages = [
            Message(role="system", content="System instruction"),
            Message(role="user", content="Hello"),
        ]
        plan = create_cache_plan("gpt-4o", "ws_1", messages)

        # Invoking plan under different model invalidates plan
        with pytest.raises(CacheInvalidationError) as exc:
            validate_cache_plan(plan, "gpt-4o-mini", "ws_1", messages)
        assert exc.value.reason == "model_mismatch"
        assert plan.status == "invalidated"

    def test_tool_schema_change_invalidates_plan(self) -> None:
        messages = [
            Message(role="system", content="System instruction"),
            Message(role="user", content="Hello"),
        ]
        tools_v1 = [{"name": "calculator", "description": "Calc v1"}]
        tools_v2 = [{"name": "calculator", "description": "Calc v2 modified"}]

        plan = create_cache_plan("gpt-4o", "ws_1", messages, tools_v1)

        # Changing tool schema invalidates plan
        with pytest.raises(CacheInvalidationError) as exc:
            validate_cache_plan(plan, "gpt-4o", "ws_1", messages, tools_v2)
        assert exc.value.reason == "prefix_mismatch"
        assert plan.status == "invalidated"

    def test_system_prompt_change_invalidates_plan(self) -> None:
        messages_v1 = [
            Message(role="system", content="System prompt v1"),
            Message(role="user", content="Hello"),
        ]
        messages_v2 = [
            Message(role="system", content="System prompt v2 edited"),
            Message(role="user", content="Hello"),
        ]
        plan = create_cache_plan("gpt-4o", "ws_1", messages_v1)

        with pytest.raises(CacheInvalidationError) as exc:
            validate_cache_plan(plan, "gpt-4o", "ws_1", messages_v2)
        assert exc.value.reason == "prefix_mismatch"
        assert plan.status == "invalidated"

    def test_ttl_expiration_invalidates_plan(self) -> None:
        messages = [Message(role="user", content="Hello")]
        plan = create_cache_plan("gemini-1.5-pro", "ws_1", messages)
        # Fast-forward expiration
        plan.expires_at = time.time() - 10.0

        with pytest.raises(CacheExpiredError):
            validate_cache_plan(plan, "gemini-1.5-pro", "ws_1", messages)
        assert plan.status == "expired"

    def test_cross_workspace_plan_access_denied(self) -> None:
        messages = [Message(role="user", content="Hello")]
        plan = create_cache_plan("gpt-4o", "workspace_alice", messages)

        manager = CacheManager()
        manager.register_plan(plan)

        # Attempting access from workspace_bob raises CachePermissionError
        with pytest.raises(CachePermissionError) as exc:
            manager.get_plan(plan.plan_id, "workspace_bob")
        assert exc.value.workspace_id == "workspace_bob"
        assert exc.value.plan_workspace_id == "workspace_alice"

        with pytest.raises(CachePermissionError):
            validate_cache_plan(plan, "gpt-4o", "workspace_bob", messages)


class TestBudgetReservationAndAccounting:
    """Verifies conservative miss/write reservations and reconciliation on hit."""

    @pytest.mark.asyncio
    async def test_conservative_reservation_assumes_cache_miss_reconciles_on_hit(
        self,
        tmp_path: Any,
    ) -> None:
        db_path = str(tmp_path / "cache_budget.db")
        ledger = UsageLedger(db_path=db_path)

        pricing = PricingTable(version="2026-10-01-v1", date="2026-10-01")
        pricing.add_model_pricing(
            ModelPricing(
                model_id="gpt-4o",
                input_cost_per_million=Decimal("2.50"),  # $2.50/M uncached
                output_cost_per_million=Decimal("10.00"),  # $10.00/M output
                cached_prompt_cost_per_million=Decimal("1.25"),  # $1.25/M cached (50% discount)
            )
        )

        b_mgr = BudgetManager(ledger=ledger, pricing_table=pricing)
        run_budget = b_mgr.create_run_budget(
            workspace_id="ws_cache",
            run_id="run_cache_1",
            config=BudgetConfig(max_cost_usd=Decimal("1.00")),
        )

        # Step 1: Pre-dispatch admission assumes full cache miss (2000 prompt tokens)
        res_id = await run_budget.admit_provider_call_async(
            model_id="gpt-4o",
            estimated_tokens=2000,
        )
        assert res_id is not None

        # Effective cost reserved based on full uncached rate
        status_reserved = run_budget.get_status()
        reserved_usd = status_reserved.total_reserved_usd
        assert reserved_usd > Decimal("0")

        # Step 2: Post-response settlement with verified 1000 cached tokens
        observed_usage = Usage(
            prompt_tokens=2000,
            completion_tokens=200,
            total_tokens=2200,
            cached_prompt_tokens=1000,  # 1000 cached, 1000 uncached
            cache_read_input_tokens=1000,
            is_complete=True,
        )

        await run_budget.settle_provider_call_async(
            res_id,
            usage=observed_usage,
            model_id="gpt-4o",
        )

        # Step 3: Verified spend reflects 50% discount on the 1000 cached tokens
        # Cost math: (1000 * 2.50 + 1000 * 1.25 + 200 * 10.00) / 1_000_000
        # = (0.0025 + 0.00125 + 0.0020) = $0.00575
        status_settled = run_budget.get_status()
        expected_cost = Decimal("0.00575000")
        assert status_settled.total_spent_usd == expected_cost
        assert status_settled.total_reserved_usd == Decimal("0")

    def test_missing_cache_counters_remain_unknown_without_synthesizing_hits(self) -> None:
        pricing = PricingTable(version="v1", date="2026-10-01")
        pricing.add_model_pricing(
            ModelPricing(
                model_id="gpt-4o",
                input_cost_per_million=Decimal("2.50"),
                output_cost_per_million=Decimal("10.00"),
                cached_prompt_cost_per_million=Decimal("1.25"),
            )
        )

        # Provider reports usage WITHOUT cache breakdown
        usage_no_cache = TokenUsage(
            prompt_tokens=1000,
            completion_tokens=100,
            total_tokens=1100,
            cached_prompt_tokens=None,
            is_complete=True,
        )

        estimate = pricing.calculate_cost("gpt-4o", usage_no_cache)
        # Billed at full uncached rate ($2.50/M) -> 1000 * 2.50 / 1M = $0.0025
        assert estimate.details["cached_prompt_tokens"] == 0
        assert estimate.details["uncached_prompt_tokens"] == 1000
        expected_usd = Decimal("0.00350000")  # (0.0025 input + 0.0010 output)
        assert estimate.amount_usd == expected_usd
