"""O01 Example: Keyless Shared Workflow Budget Admission.

Demonstrates:
1. Setting up dated operator pricing table.
2. Creating a RunBudget with monetary cap and admission policy.
3. Pre-dispatch admission reservation preventing unauthorized provider execution.
4. Provider execution with automated reservation and idempotent post-dispatch settlement.
5. Hierarchical child budget sharing parent allocation envelope.
6. Diagnostic inspection of committed, reported, estimated, and uncertain spend.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal

from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.providers.base import BaseProvider
from nexura_runtime.providers.types import (
    EmbeddingResult,
    GenerationRequest,
    ProviderCapabilities,
    StreamDelta,
)
from nexura_runtime.types import Message, ModelResponse, Usage
from nexura_runtime.usage.errors import BudgetExceededError
from nexura_runtime.usage.manager import BudgetManager
from nexura_runtime.usage.models import BudgetConfig, ModelPricing
from nexura_runtime.usage.pricing import PricingTable


class LocalDeterministicProvider(BaseProvider):
    """Local keyless test provider that generates deterministic responses."""

    def __init__(self, model_id: str = "mock-gpt-4o") -> None:
        super().__init__(
            model_id=model_id,
            capabilities=ProviderCapabilities(
                can_generate=True,
                can_stream=True,
                can_embed=True,
                reports_usage=True,
            ),
        )

    async def _generate_internal(
        self,
        request: GenerationRequest,
        context: ExecutionContext | None,
    ) -> ModelResponse:
        prompt_len = sum(len(m.content or "") for m in request.messages if hasattr(m, "content"))
        # Deterministic token calculation based on content
        prompt_tokens = max(10, prompt_len // 4)
        completion_tokens = 25
        return ModelResponse(
            message=Message(
                role="assistant",
                content=f"Processed query with {len(request.messages)} messages.",
            ),
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
        )

    async def _stream_internal(
        self,
        request: GenerationRequest,
        context: ExecutionContext | None,
    ) -> AsyncIterator[StreamDelta]:
        yield StreamDelta(kind="text_delta", text="Hello from stream")
        yield StreamDelta(
            kind="usage_delta",
            usage=Usage(prompt_tokens=20, completion_tokens=10, total_tokens=30),
        )

    async def _embed_internal(
        self, texts: list[str], context: ExecutionContext | None
    ) -> EmbeddingResult:
        return EmbeddingResult(
            embeddings=[[0.0] * 8 for _ in texts],
            model=self.model_id,
            usage=Usage(prompt_tokens=len(texts) * 5, total_tokens=len(texts) * 5),
        )


async def main() -> None:
    print("=== O01: Shared Workflow Budget Admission Demo ===")

    # 1. Configure dated operator pricing table
    pricing_table = PricingTable(
        version="2026-10-07-standard",
        date="2026-10-07",
        source="operator_standard_pricing",
    )
    pricing_table.add_model_pricing(
        ModelPricing(
            model_id="mock-gpt-4o",
            input_cost_per_million=Decimal("2.50"),
            output_cost_per_million=Decimal("10.00"),
            cached_prompt_cost_per_million=Decimal("1.25"),
            effective_date="2026-10-07",
        )
    )

    manager = BudgetManager(pricing_table=pricing_table)

    # 2. Establish parent run budget with $0.01 limit
    parent_budget = manager.create_run_budget(
        workspace_id="ws_production",
        run_id="run_parent_001",
        config=BudgetConfig(
            max_cost_usd=Decimal("0.01"),
            max_turns=10,
            deny_unknown_pricing=True,
            pricing_version="2026-10-07-standard",
        ),
        default_model="mock-gpt-4o",
    )

    provider = LocalDeterministicProvider("mock-gpt-4o")

    # 3. Successful execution with automated pre-dispatch reservation and settlement
    ctx = ExecutionContext(
        workspace_id="ws_production",
        run_id="run_parent_001",
        node_id="analyze_node",
        budget=parent_budget,
    )

    print("\n1. Dispatching initial generation call...")
    response = await provider.generate(
        [Message(role="user", content="Analyze the telemetry and system status.")],
        context=ctx,
    )
    print(f"   Response: {response.message.content}")
    print(f"   Usage: {response.usage}")

    status1 = parent_budget.get_status()
    print(f"   Effective spend: ${status1.effective_cost_usd:.6f}")
    print(f"   Remaining cost: ${status1.remaining_cost_usd:.6f}")

    # 4. Hierarchical child budget sharing parent allowance
    print("\n2. Spawning child subagent with shared allowance...")
    child_budget = parent_budget.create_child_budget("run_child_001")
    child_ctx = ExecutionContext(
        workspace_id="ws_production",
        run_id="run_child_001",
        node_id="subagent_task",
        budget=child_budget,
    )

    child_response = await provider.generate(
        [Message(role="user", content="Fetch diagnostics sub-task.")],
        context=child_ctx,
    )
    print(f"   Child response: {child_response.message.content}")

    status2 = parent_budget.get_status()
    print(f"   Parent effective spend after child: ${status2.effective_cost_usd:.6f}")

    # 5. Admission denial under monetary cap when allowance is exceeded
    print("\n3. Testing admission denial when monetary cap is exceeded...")
    tiny_budget = manager.create_run_budget(
        workspace_id="ws_production",
        run_id="run_capped_002",
        config=BudgetConfig(
            max_cost_usd=Decimal("0.00001"),  # Extremely small cap
            deny_unknown_pricing=True,
        ),
        default_model="mock-gpt-4o",
    )
    tiny_ctx = ExecutionContext(
        workspace_id="ws_production",
        run_id="run_capped_002",
        node_id="restricted_node",
        budget=tiny_budget,
    )

    try:
        await provider.generate(
            [Message(role="user", content="This request will be denied before dispatch.")],
            context=tiny_ctx,
        )
        print("   ERROR: Request should have been denied!")
    except BudgetExceededError as exc:
        print("   SUCCESS: Denied pre-dispatch without calling provider!")
        print(f"   Reason: {exc.message}")

    # 6. Diagnostics report
    print("\n4. Final RunBudget typed diagnostic dictionary:")
    import json

    print(json.dumps(parent_budget.to_dict(), indent=2))
    print("\n=== Demo Complete ===")


if __name__ == "__main__":
    asyncio.run(main())
