# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""B00/B01 module contract adapter for usage tracking, cost estimation, and budget enforcement."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import EffectPolicy, ModuleManifest
from nexura_agents.contracts.result import ModuleResult
from nexura_agents.types import Usage
from nexura_agents.usage.errors import BudgetExceededError, UsageError
from nexura_agents.usage.ledger import ledger_call
from nexura_agents.usage.manager import BudgetManager, budget_call
from nexura_agents.usage.models import BudgetConfig, FXRate


class UsageBudgetModule(BaseModule):
    """B00/B01 governance module enforcing resource budgets and calculating token costs."""

    def __init__(
        self,
        manager: BudgetManager | None = None,
        *,
        module_id: str = "nexura.usage.budget",
        version: str = "1.0.0",
        # reserve and record_usage add a reservation or count usage again when repeated:
        # not idempotent, so a durable run pauses instead of repeating them after a crash.
        effect_policy: EffectPolicy = "non_idempotent_write",
    ) -> None:
        self.manager = manager or BudgetManager()

        self._manifest = ModuleManifest(
            schema_version=1,
            module_id=module_id,
            version=version,
            kind="governance",
            title="Usage, Cost, and Budget Governance",
            description=(
                "Enforces pre-dispatch reservations, post-response reconciliation, "
                "hierarchical workspace/run budgets, turn/tool call caps, and dated FX conversions."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": [
                            "check_budget",
                            "reserve",
                            "reconcile",
                            "record_usage",
                            "convert_currency",
                            "get_ledger",
                            "set_budget",
                        ],
                    },
                    "workspace_id": {"type": "string"},
                    "run_id": {"type": "string"},
                    "reservation_id": {"type": "string"},
                    "model_id": {"type": "string"},
                    "estimated_tokens": {"type": "integer"},
                    "usage": {"type": "object"},
                    "is_cancelled": {"type": "boolean"},
                    "is_pre_dispatch": {"type": "boolean"},
                    "amount_usd": {"type": ["number", "string"]},
                    "target_currency": {"type": "string"},
                    "max_cost_usd": {"type": ["number", "string"]},
                    "max_tokens": {"type": "integer"},
                    "max_turns": {"type": "integer"},
                    "max_tool_calls": {"type": "integer"},
                },
                "required": ["operation"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "result": {"type": "object"},
                },
                "required": ["status"],
            },
            config_schema={"type": "object", "properties": {}, "additionalProperties": True},
            capabilities={"network": False, "streaming": False},
            effect_policy=effect_policy,
        )

    @property
    def manifest(self) -> ModuleManifest:
        return self._manifest

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any],
    ) -> ModuleResult:
        operation = inputs.get("operation")
        workspace_id = inputs.get("workspace_id") or context.workspace_id
        run_id = inputs.get("run_id") or context.run_id

        try:
            if operation == "set_budget":
                max_cost = (
                    Decimal(str(inputs["max_cost_usd"])) if "max_cost_usd" in inputs else None
                )
                b_cfg = BudgetConfig(
                    max_cost_usd=max_cost,
                    max_tokens=inputs.get("max_tokens"),
                    max_turns=inputs.get("max_turns"),
                    max_tool_calls=inputs.get("max_tool_calls"),
                )
                self.manager.set_workspace_budget(workspace_id, b_cfg)
                return ModuleResult.success(
                    {
                        "status": "budget_configured",
                        "workspace_id": workspace_id,
                    }
                )

            elif operation == "check_budget":
                run_budget = self.manager.get_run_budget(run_id)
                # Ledger reads may wait on a database-wide lock (Postgres): off the loop.
                if run_budget:
                    status = await run_budget.get_status_async()
                else:
                    status = await ledger_call(
                        self.manager.ledger, self.manager.get_workspace_status, workspace_id
                    )
                return ModuleResult.success(
                    {
                        "status": "ok",
                        "budget_status": status.model_dump(mode="json"),
                    }
                )

            elif operation == "reserve":
                estimated_tokens = inputs.get("estimated_tokens", 1000)
                model_id = inputs.get("model_id", "gpt-4o")
                run_budget = self.manager.get_run_budget(run_id)
                if run_budget is None:
                    run_budget = self.manager.create_run_budget(
                        workspace_id=workspace_id,
                        run_id=run_id,
                    )
                res = await run_budget.reserve_async(
                    estimated_tokens=estimated_tokens,
                    model_id=model_id,
                )
                return ModuleResult.success(
                    {
                        "status": "reserved",
                        "reservation": res.model_dump(mode="json"),
                    }
                )

            elif operation == "reconcile":
                res_id = inputs.get("reservation_id")
                if not res_id:
                    return ModuleResult.failure(
                        "MISSING_ARGUMENT",
                        "reservation_id is required for reconcile",
                    )

                raw_usage = inputs.get("usage")
                usage: Usage | None = None
                if raw_usage:
                    usage = Usage(
                        prompt_tokens=raw_usage.get("prompt_tokens"),
                        completion_tokens=raw_usage.get("completion_tokens"),
                        total_tokens=raw_usage.get("total_tokens"),
                        is_complete=raw_usage.get("is_complete", True),
                    )

                run_budget = self.manager.get_run_budget(run_id)
                if run_budget is None:
                    run_budget = self.manager.create_run_budget(
                        workspace_id=workspace_id,
                        run_id=run_id,
                    )

                estimate = await budget_call(
                    run_budget,
                    run_budget.reconcile,
                    res_id,
                    usage=usage,
                    model_id=inputs.get("model_id"),
                    is_cancelled=inputs.get("is_cancelled", False),
                    is_pre_dispatch=inputs.get("is_pre_dispatch", False),
                )
                return ModuleResult.success(
                    {
                        "status": "reconciled",
                        "cost_estimate": estimate.model_dump(mode="json"),
                    }
                )

            elif operation == "record_usage":
                raw_usage = inputs.get("usage", {})
                usage = Usage(
                    prompt_tokens=raw_usage.get("prompt_tokens"),
                    completion_tokens=raw_usage.get("completion_tokens"),
                    total_tokens=raw_usage.get("total_tokens"),
                    is_complete=raw_usage.get("is_complete", True),
                )
                run_budget = self.manager.get_run_budget(run_id)
                if run_budget is None:
                    run_budget = self.manager.create_run_budget(
                        workspace_id=workspace_id,
                        run_id=run_id,
                        default_model=inputs.get("model_id", "gpt-4o"),
                    )
                await run_budget.record_usage_async(usage)
                status = await run_budget.get_status_async()
                return ModuleResult.success(
                    {
                        "status": "usage_recorded",
                        "budget_status": status.model_dump(mode="json"),
                    }
                )

            elif operation == "convert_currency":
                amount_str = str(inputs.get("amount_usd", "0"))
                amount_usd = Decimal(amount_str)
                target = inputs.get("target_currency", "INR").upper()

                if "fx_rate" in inputs:
                    rate_val = Decimal(str(inputs["fx_rate"]))
                    fx_rate = FXRate(
                        from_currency="USD",
                        to_currency=target,
                        rate=rate_val,
                        date="operator_specified",
                        source="input_override",
                    )
                    converted = fx_rate.convert(amount_usd)
                    return ModuleResult.success(
                        {
                            "status": "converted",
                            "converted_amount": str(converted),
                            "currency": target,
                            "is_estimate": True,
                            "fx_rate": str(rate_val),
                        }
                    )

                if self.manager.fx_converter:
                    converted, rate = self.manager.fx_converter.convert_usd_to_inr(amount_usd)
                    return ModuleResult.success(
                        {
                            "status": "converted",
                            "converted_amount": str(converted),
                            "currency": target,
                            "is_estimate": True,
                            "fx_rate": str(rate.rate),
                            "fx_date": rate.date,
                            "fx_source": rate.source,
                        }
                    )

                return ModuleResult.failure("NO_FX_RATE", "No FX converter or rate supplied")

            elif operation == "get_ledger":
                entries = self.manager.ledger.get_entries(workspace_id=workspace_id, run_id=run_id)
                return ModuleResult.success(
                    {
                        "status": "ok",
                        "entries": [e.model_dump(mode="json") for e in entries],
                    }
                )

            else:
                return ModuleResult.failure("UNKNOWN_OPERATION", f"Unknown operation: {operation}")

        except BudgetExceededError as exc:
            return ModuleResult.failure(
                "BUDGET_EXCEEDED",
                exc.message,
                metadata={
                    "limit_type": exc.limit_type,
                    "current_value": str(exc.current_value),
                    "max_value": str(exc.max_value),
                },
            )
        except UsageError as exc:
            return ModuleResult.failure("USAGE_ERROR", exc.message)
        except Exception as exc:
            return ModuleResult.failure("INTERNAL_ERROR", str(exc))
