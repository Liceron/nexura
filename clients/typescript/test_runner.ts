/**
 * Node.js TypeScript verification script testing NexuraClient end-to-end.
 */

import { NexuraClient } from "./dist/index.js";

async function main() {
  const baseUrl = process.argv[2] || "http://127.0.0.1:8000";
  console.log(`Connecting TypeScript client to ${baseUrl}...`);

  const client = new NexuraClient({ baseUrl, workspaceId: "ts_workspace" });

  // 1. Create a workflow
  const spec = {
    name: "TSApprovalWorkflow",
    entry_node: "validate_order",
    nodes: {
      validate_order: {
        id: "validate_order",
        handler: "validate_order",
        default_transition: "manager_approval",
      },
      manager_approval: {
        id: "manager_approval",
        handler: "manager_approval",
        requires_approval: true,
        default_transition: "fulfill_order",
      },
      fulfill_order: {
        id: "fulfill_order",
        handler: "fulfill_order",
      },
    },
    terminal_nodes: ["fulfill_order"],
  };

  const wf = await client.workflows.create(spec, { workflowId: "wf_ts_test" });
  if (wf.workflow_id !== "wf_ts_test") {
    throw new Error(`Expected workflow_id wf_ts_test, got ${wf.workflow_id}`);
  }
  console.log(`Created workflow: ${wf.workflow_id} (rev ${wf.revision})`);

  // 2. Start workflow run
  const initialRun = await client.runs.start({
    workflow_id: wf.workflow_id,
    initial_state: { amount: 250 },
  });
  console.log(`Started run ${initialRun.run_id}, status: ${initialRun.status}`);

  if (initialRun.status !== "pending_approval" || !initialRun.pending_approval) {
    throw new Error(`Expected run to pause for approval, got ${initialRun.status}`);
  }

  // 3. Submit approval decision
  const approvalId = initialRun.pending_approval.approval_id;
  const completedRun = await client.runs.decide(initialRun.run_id, {
    approval_id: approvalId,
    decision: "approve",
  });
  console.log(`Submitted approval, final status: ${completedRun.status}`);

  if (completedRun.status !== "completed") {
    throw new Error(`Expected completed status, got ${completedRun.status}`);
  }
  if (!completedRun.current_state.fulfilled) {
    throw new Error("Expected state to contain fulfilled: true");
  }

  // 4. Test workspace switching
  const tenantClient = client.withWorkspace("tenant_secondary");
  if (tenantClient.workspaceId !== "tenant_secondary") {
    throw new Error(`Expected workspaceId tenant_secondary, got ${tenantClient.workspaceId}`);
  }
  const scopedWfs = await tenantClient.request("GET", `/workspaces/${tenantClient.workspaceId}/workflows`);
  if (!Array.isArray(scopedWfs.items)) {
    throw new Error("Expected scoped items array from workspace route");
  }

  console.log("SUCCESS: TypeScript client verified end-to-end.");
}

main().catch((err) => {
  console.error("TypeScript client test failed:", err);
  process.exit(1);
});
