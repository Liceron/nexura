# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""TEST ONLY: seed a workspace with one of everything and count what is left of it (P46).

``seed_workspace`` writes a row into every open-source store a workspace can own (a run that
waits for approval, started through the API so it has checkpoints and journal rows, a legacy
run whose blob is filed under ``default/``, a workflow, a session, an agent, a skill and its
counter, evaluation datasets, runs and traces, a Studio eval, a knowledge document, an
integration registration, a credential, webhooks, schedules, trigger jobs and receipts, an
outbox receipt and a blob) and returns what it made. ``workspace_footprint`` counts the rows
and blobs of a workspace that remain, per table, on SQLite or Postgres. Both use only local
fakes (no network, no model call).
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any

#: A one-node workflow that waits for approval before running a local synthetic module.
APPROVAL_SPEC: dict[str, Any] = {
    "name": "p46-wait",
    "entry_node": "gate",
    "nodes": {
        "gate": {"id": "gate", "handler": "synthetic_order_lookup", "requires_approval": True}
    },
    "terminal_nodes": ["gate"],
}
BIG = "x" * 20000
SKILL_MD = "---\nname: refunds\ndescription: How to handle refunds.\n---\nBe kind.\n"

#: Tables keyed by ``workspace_id`` in the open-source stores (SQLite name, Postgres name).
WORKSPACE_TABLES: tuple[tuple[str, str], ...] = (
    ("nexura_service_agents", "nexura_service_agents"),
    ("nexura_service_skills", "nexura_service_skills"),
    ("nexura_service_skill_counters", "nexura_service_skill_counters"),
    ("eval_datasets", "eval_datasets"),
    ("eval_runs", "eval_runs"),
    ("eval_traces", "eval_traces"),
    ("studio_eval_datasets", "studio_eval_datasets"),
    ("studio_eval_runs", "studio_eval_runs"),
    ("hk_documents", "hk_documents"),
    ("hk_chunks", "hk_chunks"),
    ("hk_lineage", "hk_lineage"),
    ("hk_fts", "hk_collection_versions"),
    ("nexura_service_integration_registrations", "nexura_service_integration_registrations"),
    ("nexura_secrets", "nexura_secrets"),
    ("webhook_configs", "nexura_trigger_webhooks"),
    ("webhook_receipts", "nexura_trigger_receipts"),
    ("schedule_configs", "nexura_trigger_schedules"),
    ("durable_trigger_jobs", "nexura_trigger_jobs"),
    ("channel_outbox", "channel_outbox"),
    ("nexura_workflow_checkpoints", "nexura_workflow_checkpoints"),
    ("nexura_exec_runs", "nexura_exec_runs"),
    ("nexura_exec_events", "nexura_exec_events"),
    ("nexura_exec_steps", "nexura_exec_steps"),
    ("nexura_exec_effects", "nexura_exec_effects"),
    ("nexura_service_resource_ownership", "nexura_service_resource_ownership"),
)


@dataclass
class Seeded:
    """What ``seed_workspace`` created."""

    workspace_id: str
    run_ids: list[str] = field(default_factory=list)
    workflow_ids: list[str] = field(default_factory=list)
    session_ids: list[str] = field(default_factory=list)
    blob_keys: list[str] = field(default_factory=list)
    module_ids: list[str] = field(default_factory=list)


async def seed_workspace(
    state: Any, client: Any, headers: dict[str, str], workspace_id: str, owner: str
) -> Seeded:
    """Seed ``workspace_id`` (see the module docstring); ``client``/``headers`` start the
    approval run through ``POST /api/v1/runs`` as a member of the workspace."""
    from nexura_agents.types import Message

    from nexura_runtime.agents_spec import AgentSpec
    from nexura_runtime.evaluation.models import (
        EvaluationDataset,
        EvaluationSummary,
        StoredTrace,
    )
    from nexura_runtime.integrations.channels.models import OutboxReceipt
    from nexura_runtime.service.triggers.models import (
        DurableTriggerJob,
        ScheduleConfig,
        WebhookConfig,
        WebhookReceipt,
    )
    from nexura_runtime.workflow import WorkflowSpec

    seeded = Seeded(workspace_id)
    tag = uuid.uuid4().hex[:8]
    repo = state.workspace_repo
    store = state.store

    started = await client.post(
        "/api/v1/runs", headers=headers, json={"spec": APPROVAL_SPEC, "initial_state": {}}
    )
    assert started.status_code == 201, started.text
    run_id = started.json()["run_id"]
    assert started.json()["status"] == "pending_approval", started.text
    seeded.run_ids.append(run_id)
    # A large event: its payload goes to the blob store under the workspace.
    await store.record_run_event(run_id, "NOTE", data={"big": BIG})

    spec = WorkflowSpec.from_dict(APPROVAL_SPEC)
    created = await store.create_workflow(spec, workflow_id=f"wf-{tag}")
    seeded.workflow_ids.append(created["workflow_id"])
    repo.record_resource_ownership("workflow", created["workflow_id"], workspace_id, owner)
    # A legacy run without a journal workspace: its blobs are filed under "default/".
    legacy = f"run-legacy-{tag}"
    await store.record_run_start(
        run_id=legacy, workflow_id=None, spec=spec, initial_state={"big": BIG}
    )
    await store.update_run(
        run_id=legacy,
        status="completed",
        current_node_id=None,
        current_state={},
        step_count=1,
    )
    repo.record_resource_ownership("run", legacy, workspace_id, owner)
    seeded.run_ids.append(legacy)

    session_id = f"sess-{tag}"
    await state.session_store.save(session_id, [Message(role="user", content="hi")], 0)
    repo.record_resource_ownership("session", session_id, workspace_id, owner)
    seeded.session_ids.append(session_id)

    await asyncio.to_thread(state.agent_store.create, workspace_id, AgentSpec(name="helper"))
    await asyncio.to_thread(state.skills_store.create, workspace_id, SKILL_MD)
    await asyncio.to_thread(state.skills_store.remove, workspace_id, "refunds")
    await asyncio.to_thread(state.skills_store.create, workspace_id, SKILL_MD)

    evals = state.eval_store
    await asyncio.to_thread(
        evals.save_dataset, EvaluationDataset(dataset_id="d1", version="1"), workspace_id
    )
    await asyncio.to_thread(
        evals.save_run, EvaluationSummary(dataset_id="d1", dataset_version="1"), workspace_id
    )
    await asyncio.to_thread(
        evals.save_trace,
        StoredTrace(run_id=f"t-{tag}", timestamp=time.time(), output=BIG),
        workspace_id,
    )
    studio = getattr(state, "studio_eval_store", None)
    if studio is None:
        studio = state.service_stores.studio_evaluations()
        state.studio_eval_store = studio
    await asyncio.to_thread(
        lambda: studio.save_dataset(
            workspace_id,
            "sd1",
            name="n",
            description="",
            cases=[],
            content_hash="h",
            expected_revision=None,
        )
    )
    await asyncio.to_thread(
        lambda: studio.save_run(
            workspace_id,
            run_id=f"sr-{tag}",
            dataset_id="sd1",
            dataset_revision=1,
            dataset_hash="h",
            label="l",
            target={},
            scorers=[],
            summary={},
            report={},
        )
    )

    index = getattr(state, "hybrid_knowledge_index", None)
    if index is None:
        index = state.service_stores.knowledge_index(state.vector_store, state.knowledge_embedder)
        state.hybrid_knowledge_index = index
    outcome = await index.ingest(
        workspace_id=workspace_id, filename="notes.txt", data=b"refunds take five days"
    )
    repo.record_resource_ownership("document", outcome.document.document_id, workspace_id, owner)

    module_id = f"p46.tool.{tag}"
    await asyncio.to_thread(
        state.integration_registrations.save, workspace_id, [module_id], "mcp", None, "srv"
    )
    repo.record_resource_ownership("integration_tool", module_id, workspace_id, owner)
    seeded.module_ids.append(module_id)
    await asyncio.to_thread(
        state.credential_store.store, workspace_id, "cred1", "api_key", "sk-test-value"
    )

    triggers = state.trigger_store
    wf_id = created["workflow_id"]
    endpoint = f"ep-{tag}"
    await asyncio.to_thread(
        triggers.save_webhook_config,
        WebhookConfig(
            endpoint_id=endpoint,
            workspace_id=workspace_id,
            workflow_id=wf_id,
            release_id="r1",
            secret="s" * 32,
        ),
    )
    await asyncio.to_thread(
        triggers.save_schedule,
        ScheduleConfig(
            schedule_id=f"sc-{tag}",
            workspace_id=workspace_id,
            workflow_id=wf_id,
            release_id="r1",
            interval_seconds=3600,
            next_run_at=time.time() + 3600,
        ),
    )
    await asyncio.to_thread(
        triggers.enqueue_trigger_job,
        DurableTriggerJob(
            job_id=f"job-{tag}",
            workspace_id=workspace_id,
            trigger_type="schedule",
            trigger_source_id=f"sc-{tag}",
            workflow_id=wf_id,
            release_id="r1",
            # Far in the future: no worker of the test claims it.
            created_at=time.time() + 86400,
        ),
    )
    await asyncio.to_thread(
        triggers.record_webhook_receipt_and_enqueue_job,
        WebhookReceipt(
            event_id=f"ev-{tag}", endpoint_id=endpoint, workspace_id=workspace_id, job_id="j2"
        ),
        DurableTriggerJob(
            job_id=f"job2-{tag}",
            workspace_id=workspace_id,
            trigger_type="webhook",
            trigger_source_id=endpoint,
            workflow_id=wf_id,
            release_id="r1",
            created_at=time.time() + 86400,
        ),
    )
    await asyncio.to_thread(
        state.channel_store.record_receipt,
        OutboxReceipt(
            receipt_id=f"rc-{tag}",
            message_id=f"m-{tag}",
            channel="email",
            recipient="a@example.com",
            status="dry_run",
            dry_run=True,
            workspace_id=workspace_id,
        ),
    )
    ref = await asyncio.to_thread(
        state.blob_store.put, workspace_id, "run-events", b"{}", "application/json"
    )
    seeded.blob_keys.append(ref.key)
    seeded.blob_keys.extend(await store.run_blob_keys(seeded.run_ids))
    return seeded


def _sqlite_counts(path: str, seeded: Seeded) -> dict[str, int]:
    ws = seeded.workspace_id
    counts: dict[str, int] = {}
    with closing(sqlite3.connect(path, timeout=10)) as conn:
        present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}

        def count(table: str, where: str, args: list[Any]) -> None:
            if table in present:
                counts[table] = counts.get(table, 0) + int(
                    conn.execute(f"SELECT count(*) FROM {table} WHERE {where}", args).fetchone()[0]
                )

        for table, _ in WORKSPACE_TABLES:
            count(table, "workspace_id = ?", [ws])
        for run_id in seeded.run_ids:
            for table in ("nexura_studio_runs", "nexura_studio_run_events"):
                count(table, "run_id = ?", [run_id])
            for table in ("nexura_workflow_checkpoints", "nexura_workflow_checkpoint_history"):
                where = "workflow_run_id = ? OR workflow_run_id LIKE ?"
                count(table, where, [run_id, f"{run_id}/%"])
        for wf in seeded.workflow_ids:
            count("nexura_studio_workflows", "workflow_id = ?", [wf])
        for sid in seeded.session_ids:
            count("nexura_sessions", "session_id = ?", [sid])
            count("nexura_session_tombstones", "session_id = ?", [sid])
    return counts


def _postgres_counts(database: Any, seeded: Seeded) -> dict[str, int]:
    ws = seeded.workspace_id

    async def run(conn: Any) -> dict[str, int]:
        counts: dict[str, int] = {}

        async def count(table: str, where: str, *args: Any) -> None:
            if await conn.fetchval("SELECT to_regclass($1)", table) is None:
                return
            n = await conn.fetchval(f"SELECT count(*) FROM {table} WHERE {where}", *args)
            counts[table] = counts.get(table, 0) + int(n)

        for _, table in WORKSPACE_TABLES:
            await count(table, "workspace_id = $1", ws)
        await count("nexura_workflow_checkpoint_history", "workspace_id = $1", ws)
        ids = seeded.run_ids
        await count("nexura_studio_runs", "run_id = ANY($1::text[])", ids)
        await count("nexura_studio_run_events", "run_id = ANY($1::text[])", ids)
        await count(
            "nexura_workflow_checkpoints", "split_part(workflow_run_id, '/', 1) = ANY($1)", ids
        )
        await count(
            "nexura_workflow_checkpoint_history",
            "split_part(workflow_run_id, '/', 1) = ANY($1)",
            ids,
        )
        await count("nexura_studio_workflows", "workflow_id = ANY($1::text[])", seeded.workflow_ids)
        await count("nexura_sessions", "session_id = ANY($1::text[])", seeded.session_ids)
        await count("nexura_session_tombstones", "session_id = ANY($1::text[])", seeded.session_ids)
        return counts

    counts: dict[str, int] = database.run(run)
    return counts


async def workspace_footprint(state: Any, seeded: Seeded) -> dict[str, int]:
    """Rows left per table, plus ``"blobs"`` (the workspace's prefix and the seeded keys)."""
    stores = state.service_stores
    if getattr(stores, "database_url", None):
        counts = await asyncio.to_thread(_postgres_counts, stores.open_database(), seeded)
    else:
        counts = await asyncio.to_thread(_sqlite_counts, str(state.store.db_path), seeded)
    from nexura_runtime.blobs.base import workspace_segment

    blob_store = state.blob_store

    def blobs() -> int:
        prefix = f"{workspace_segment(seeded.workspace_id)}/"
        listed = {info.key for info in blob_store.list(prefix)}
        listed |= {key for key in seeded.blob_keys if blob_store.exists(key)}
        return len(listed)

    counts["blobs"] = await asyncio.to_thread(blobs)
    return counts


__all__ = ["APPROVAL_SPEC", "Seeded", "seed_workspace", "workspace_footprint"]
