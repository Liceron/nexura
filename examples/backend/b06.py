"""B06 Standalone Example: Workflow Compiler, Typed Ports, and Control Flow.

Demonstrates:
1. Registering typed modules with manifests into VersionedModuleRegistry.
2. Building and validating a branching workflow specification with port bindings.
3. Compiling the workflow into an immutable release bundle with release_digest.
4. Serializing/exporting and importing the compiled plan.
5. Executing the compiled plan with CompiledPlanExecutor under fuel accounting.
6. Deterministic branch join verification regardless of branch resolution order.
7. Bounded fanout parallel map execution respecting semaphore concurrency.
8. Invoking the compiler module via B01's CLI interface.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from nexura_runtime.contracts import (
    BaseModule,
    ExecutionContext,
    ModuleManifest,
    ModuleResult,
)

from nexura_runtime.modules.cli import _run_command
from nexura_runtime.modules.registry import VersionedModuleRegistry
from nexura_runtime.runtime.compiler import (
    CompiledPlanExecutor,
    WorkflowCompiler,
    WorkflowCompilerModule,
)


# 1. Define sample typed modules
class IngestCustomerModule(BaseModule):
    manifest = ModuleManifest(
        schema_version=1,
        module_id="example.customer.ingest",
        version="1.0.0",
        kind="ingest",
        title="Ingest Customer Data",
        description="Ingests customer loan application.",
        input_schema={
            "type": "object",
            "properties": {
                "customer_name": {"type": "string"},
                "cibil_score": {"type": "integer"},
                "loan_amount": {"type": "number"},
            },
            "required": ["customer_name", "cibil_score", "loan_amount"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "applicant": {"type": "string"},
                "score": {"type": "integer"},
                "amount": {"type": "number"},
                "tier": {"type": "string"},
            },
            "required": ["applicant", "score", "amount", "tier"],
            "additionalProperties": False,
        },
        effect_policy="pure",
    )

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any],
    ) -> ModuleResult:
        score = inputs["cibil_score"]
        tier = "prime" if score >= 750 else "standard"
        branch = "prime_branch" if tier == "prime" else "standard_branch"
        return ModuleResult(
            outputs={
                "applicant": inputs["customer_name"],
                "score": score,
                "amount": inputs["loan_amount"],
                "tier": tier,
            },
            branch=branch,
        )


class PrimeOfferModule(BaseModule):
    manifest = ModuleManifest(
        schema_version=1,
        module_id="example.offers.prime",
        version="1.0.0",
        kind="offer",
        title="Prime Loan Offer",
        description="Calculates discounted interest rates for prime borrowers.",
        input_schema={
            "type": "object",
            "properties": {
                "applicant": {"type": "string"},
                "amount": {"type": "number"},
            },
            "required": ["applicant", "amount"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "offer_rate_pct": {"type": "number"},
                "max_tenure_months": {"type": "integer"},
                "branch_name": {"type": "string"},
            },
            "required": ["offer_rate_pct", "max_tenure_months", "branch_name"],
            "additionalProperties": False,
        },
        effect_policy="pure",
    )

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any],
    ) -> ModuleResult:
        return ModuleResult(
            outputs={
                "offer_rate_pct": 8.75,
                "max_tenure_months": 84,
                "branch_name": "prime",
            }
        )


class StandardOfferModule(BaseModule):
    manifest = ModuleManifest(
        schema_version=1,
        module_id="example.offers.standard",
        version="1.0.0",
        kind="offer",
        title="Standard Loan Offer",
        description="Calculates standard interest rates.",
        input_schema={
            "type": "object",
            "properties": {
                "applicant": {"type": "string"},
                "amount": {"type": "number"},
            },
            "required": ["applicant", "amount"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "offer_rate_pct": {"type": "number"},
                "max_tenure_months": {"type": "integer"},
                "branch_name": {"type": "string"},
            },
            "required": ["offer_rate_pct", "max_tenure_months", "branch_name"],
            "additionalProperties": False,
        },
        effect_policy="pure",
    )

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any],
    ) -> ModuleResult:
        return ModuleResult(
            outputs={
                "offer_rate_pct": 11.5,
                "max_tenure_months": 60,
                "branch_name": "standard",
            }
        )


class CheckDocumentItemModule(BaseModule):
    manifest = ModuleManifest(
        schema_version=1,
        module_id="example.doc.verify_item",
        version="1.0.0",
        kind="verifier",
        title="Document Item Verifier",
        description="Verifies a single KYC document item.",
        input_schema={
            "type": "object",
            "properties": {
                "doc_name": {"type": "string"},
            },
            "required": ["doc_name"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "verified_doc": {"type": "string"},
                "valid": {"type": "boolean"},
            },
            "required": ["verified_doc", "valid"],
            "additionalProperties": False,
        },
        effect_policy="pure",
    )

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any],
    ) -> ModuleResult:
        doc = inputs["doc_name"]
        return ModuleResult(
            outputs={
                "verified_doc": doc,
                "valid": True,
            }
        )


async def main() -> None:
    print("=== B06: Workflow Compiler, Ports and Control Flow Demonstration ===")

    # 1. Register modules in a fresh registry
    registry = VersionedModuleRegistry()
    registry.register(IngestCustomerModule)
    registry.register(PrimeOfferModule)
    registry.register(StandardOfferModule)
    registry.register(CheckDocumentItemModule)
    registry.register(WorkflowCompilerModule(registry=registry))

    # 2. Define workflow specification dictionary
    spec = {
        "workflow_id": "loan_origination_flow",
        "version": "1.0.0",
        "entry_node": "ingest_node",
        "terminal_nodes": ["prime_offer_node", "standard_offer_node"],
        "nodes": {
            "ingest_node": {
                "module_id": "example.customer.ingest",
                "fuel_cost": 2,
                "transitions": {
                    "prime_branch": "prime_offer_node",
                    "standard_branch": "standard_offer_node",
                },
            },
            "prime_offer_node": {
                "module_id": "example.offers.prime",
                "fuel_cost": 3,
                "bindings": [
                    {
                        "source_node": "ingest_node",
                        "source_port": "applicant",
                        "target_port": "applicant",
                    },
                    {
                        "source_node": "ingest_node",
                        "source_port": "amount",
                        "target_port": "amount",
                    },
                ],
            },
            "standard_offer_node": {
                "module_id": "example.offers.standard",
                "fuel_cost": 3,
                "bindings": [
                    {
                        "source_node": "ingest_node",
                        "source_port": "applicant",
                        "target_port": "applicant",
                    },
                    {
                        "source_node": "ingest_node",
                        "source_port": "amount",
                        "target_port": "amount",
                    },
                ],
            },
            "doc_check_node": {
                "module_id": "example.doc.verify_item",
                "fuel_cost": 1,
            },
        },
        "parallel_maps": {
            "verify_kyc_docs": {
                "items_field": "kyc_documents",
                "item_var": "doc_name",
                "worker_node_id": "doc_check_node",
                "max_concurrency": 3,
                "output_field": "verified_documents",
            }
        },
        "joins": {
            "sync_join": {
                "incoming_branches": ["branch_beta", "branch_alpha", "branch_gamma"],
                "reducer": "sorted_merge",
            }
        },
        "default_fuel_budget": 50,
    }

    # 3. Compile specification into an immutable plan
    compiler = WorkflowCompiler(registry=registry)
    plan = compiler.compile(spec)

    print(f"Workflow ID:     {plan.workflow_id}")
    print(f"Version:         {plan.version}")
    print(f"Release Digest:  {plan.release_digest}")
    print(f"Compiled Nodes:  {list(plan.nodes.keys())}")
    assert plan.release_digest, "Release digest must not be empty"

    # 4. Test export and re-import
    exported_json = plan.model_dump_json(indent=2)
    loaded_plan = plan.model_validate_json(exported_json)
    assert loaded_plan.release_digest == plan.release_digest
    print("Serialization & Deserialization: VERIFIED (Digest matches exactly)")

    # 5. Execute compiled plan
    executor = CompiledPlanExecutor(registry=registry)
    context = ExecutionContext(
        workspace_id="ws_delhi_fin",
        principal_id="user_credit_officer",
        run_id="run_flow_001",
        node_id="root",
    )

    initial_input = {
        "customer_name": "Aarav Gupta",
        "cibil_score": 780,  # Routes to prime_offer_node
        "loan_amount": 500000.0,
    }

    result_state = await executor.execute(plan, context, initial_state=initial_input)
    print("\nExecution Output (Prime applicant):")
    print(f"  Applicant:       {result_state['applicant']}")
    print(f"  Tier:            {result_state['tier']}")
    print(f"  Branch Name:     {result_state['branch_name']}")
    print(f"  Offer Rate:      {result_state['offer_rate_pct']}%")
    print(f"  Fuel Consumed:   {result_state['_fuel_consumed']}")
    print(f"  Fuel Remaining:  {result_state['_fuel_remaining']}")
    assert result_state["branch_name"] == "prime"
    assert result_state["offer_rate_pct"] == 8.75
    assert result_state["_fuel_consumed"] == 5  # 2 + 3

    # 6. Execute bounded fanout parallel map
    concurrency_tracker: list[int] = []
    map_state: dict[str, Any] = {
        "kyc_documents": ["PAN_CARD.pdf", "AADHAAR.pdf", "SALARY_SLIP_1.pdf", "SALARY_SLIP_2.pdf"]
    }
    mapped_docs = await executor.execute_parallel_map(
        plan,
        "verify_kyc_docs",
        context,
        map_state,
        peak_concurrency_tracker=concurrency_tracker,
    )
    print(f"\nBounded Fanout Verification ({len(mapped_docs)} items):")
    print(f"  Processed Items: {mapped_docs}")
    print(f"  Concurrency Seen: {concurrency_tracker}")
    assert max(concurrency_tracker) <= 3, "Fanout concurrency exceeded maximum limit"
    assert len(mapped_docs) == 4

    # 7. Deterministic Join Verification
    # Reordered arrival: gamma, alpha, beta
    reordered_payloads = {
        "branch_gamma": {"gamma_stat": 30},
        "branch_alpha": {"alpha_stat": 10},
        "branch_beta": {"beta_stat": 20},
    }
    joined = executor.execute_deterministic_join("sync_join", plan, reordered_payloads)
    print("\nDeterministic Join (Reordered completion):")
    print(f"  Ordered Branches: {joined['_joined_branches_ordered']}")
    assert joined["_joined_branches_ordered"] == ["branch_alpha", "branch_beta", "branch_gamma"]

    # 8. Invoke WorkflowCompilerModule via B01 CLI
    print("\nInvoking Workflow Compiler via B01 CLI interface:")
    cli_args = argparse.Namespace(
        module_id="nexura.runtime.compiler",
        version="1.0.0",
        input=json.dumps({"spec": spec}),
        config="{}",
        workspace="ws_delhi_fin",
        principal="cli_admin",
        run_id="cli_compile_run",
        node_id="cli_compiler_node",
    )
    cli_code = await _run_command(registry, cli_args)
    assert cli_code == 0, "CLI execution failed"
    print("CLI Execution: SUCCESS (code 0)")
    print("\n=== B06 Example Completed Successfully ===")


if __name__ == "__main__":
    asyncio.run(main())
