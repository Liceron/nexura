/**
 * Nexura Studio - Local Interactive Frontend
 * Strictly vanilla JavaScript without third-party frameworks.
 * All dynamic content is rendered using textContent / createElement (zero innerHTML).
 */

(function () {
  "use strict";

  // Application State
  const state = {
    modules: [],
    workflows: [],
    currentWorkflowId: null,
    currentRevision: 1,
    currentSpec: null,
    runs: [],
    selectedRunId: null,
    runPollingInterval: null,
  };

  // Helper: DOM Elements lookup
  const el = {
    // Navigation
    tabs: document.querySelectorAll(".nav-tab"),
    tabContents: document.querySelectorAll(".tab-content"),

    // Workflows View
    wfSelect: document.getElementById("workflow-select"),
    btnLoadTemplate: document.getElementById("btn-load-template"),
    btnNewWf: document.getElementById("btn-new-workflow"),
    btnImportJson: document.getElementById("btn-import-json"),
    btnExportJson: document.getElementById("btn-export-json"),
    btnValidateWf: document.getElementById("btn-validate-workflow"),
    btnSaveWf: document.getElementById("btn-save-workflow"),
    wfAlert: document.getElementById("workflow-alert"),

    wfName: document.getElementById("wf-name"),
    wfEntryNode: document.getElementById("wf-entry-node"),
    wfTerminalNodes: document.getElementById("wf-terminal-nodes"),
    wfRevisionLabel: document.getElementById("wf-revision-label"),
    wfHashLabel: document.getElementById("wf-hash-label"),

    visualGraph: document.getElementById("visual-graph"),
    nodesContainer: document.getElementById("nodes-container"),
    btnAddNode: document.getElementById("btn-add-node"),

    inputStateJson: document.getElementById("input-state-json"),
    btnFormatJson: document.getElementById("btn-format-json"),
    btnStartRun: document.getElementById("btn-start-run"),
    runLaunchStatus: document.getElementById("run-launch-status"),

    // Modules View
    modulesGrid: document.getElementById("modules-grid"),

    // Runs View
    btnRefreshRuns: document.getElementById("btn-refresh-runs"),
    runsTbody: document.getElementById("runs-tbody"),
    runEmptyPlaceholder: document.getElementById("run-empty-placeholder"),
    runDetailContent: document.getElementById("run-detail-content"),

    detailRunId: document.getElementById("detail-run-id"),
    detailStatusBadge: document.getElementById("detail-status-badge"),
    detailCurrentNode: document.getElementById("detail-current-node"),
    detailStepCount: document.getElementById("detail-step-count"),
    detailCreatedAt: document.getElementById("detail-created-at"),
    detailUpdatedAt: document.getElementById("detail-updated-at"),

    pendingApprovalBox: document.getElementById("pending-approval-box"),
    approvalNodeId: document.getElementById("approval-node-id"),
    approvalRequestId: document.getElementById("approval-request-id"),
    approvalInputState: document.getElementById("approval-input-state"),
    approvalNotes: document.getElementById("approval-notes"),
    btnGrantApproval: document.getElementById("btn-grant-approval"),
    btnRejectApproval: document.getElementById("btn-reject-approval"),

    detailErrorCard: document.getElementById("detail-error-card"),
    detailErrorMessage: document.getElementById("detail-error-message"),
    eventsTimeline: document.getElementById("events-timeline"),
    detailCurrentState: document.getElementById("detail-current-state"),
    btnCopyState: document.getElementById("btn-copy-state"),

    // Modal
    jsonModal: document.getElementById("json-modal"),
    modalTitle: document.getElementById("modal-title"),
    modalJsonTextarea: document.getElementById("modal-json-textarea"),
    modalActionBtn: document.getElementById("btn-modal-action"),
    btnCloseModal: document.getElementById("btn-close-modal"),
  };

  // Per-session token (X-Nexura-Session). The server puts it in a meta tag when this page is
  // loaded from this machine; otherwise (Docker, NEXURA_STUDIO_TOKEN) open the page as
  // http://127.0.0.1:8765/#token=<your token>. The fragment never reaches the server.
  const SESSION_KEY = "nexura-session";
  function initialSessionToken() {
    const hash = new URLSearchParams(window.location.hash.slice(1));
    const fromHash = hash.get("token");
    if (fromHash) {
      sessionStorage.setItem(SESSION_KEY, fromHash);
      history.replaceState(null, "", window.location.pathname + window.location.search);
      return fromHash;
    }
    const meta = document.querySelector('meta[name="nexura-session"]');
    if (meta && meta.content) return meta.content;
    return sessionStorage.getItem(SESSION_KEY) || "";
  }
  let sessionToken = initialSessionToken();

  async function refreshSessionToken() {
    // Same-origin read; the server refuses it for other sites' pages.
    const res = await fetch("/api/v1/local-session", {
      headers: sessionToken ? { "Authorization": `Bearer ${sessionToken}` } : {},
    });
    if (!res.ok) return false;
    const data = await res.json();
    sessionToken = data.token || "";
    return Boolean(sessionToken);
  }

  // Safe API Client
  async function api(path, options = {}, retried = false) {
    const opts = {
      ...options,
      headers: {
        "Accept": "application/json",
        ...(options.headers || {}),
      },
    };
    if (sessionToken) {
      opts.headers["X-Nexura-Session"] = sessionToken;
      opts.headers["Authorization"] = `Bearer ${sessionToken}`;
    }
    if (opts.body && typeof opts.body === "object") {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    const res = await fetch(path, opts);
    let data;
    try {
      data = await res.json();
    } catch {
      data = null;
    }
    if (!res.ok) {
      const code = (data && data.error && data.error.code) || "HTTP_ERROR";
      if (
        !retried &&
        res.status === 403 &&
        (code === "SESSION_TOKEN_REQUIRED" || code === "SESSION_TOKEN_INVALID") &&
        (await refreshSessionToken())
      ) {
        // The server restarted (new token): read it again and retry once.
        return api(path, options, true);
      }
      const message = (data && data.error && data.error.message) || res.statusText || "Request failed";
      const err = new Error(message);
      err.code = code;
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  // Navigation Logic
  function switchTab(tabId) {
    el.tabs.forEach((tab) => {
      const active = tab.dataset.tab === tabId;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
    el.tabContents.forEach((content) => {
      content.classList.toggle("active", content.id === `tab-${tabId}`);
    });
    if (tabId === "runs") {
      loadRuns();
    }
  }

  el.tabs.forEach((tab) => {
    tab.addEventListener("click", () => switchTab(tab.dataset.tab));
  });

  // Safe Alert Helper
  function showAlert(msg, isError = false) {
    el.wfAlert.classList.remove("hidden", "alert-danger", "alert-success");
    el.wfAlert.classList.add(isError ? "alert-danger" : "alert-success");
    el.wfAlert.textContent = msg;
  }

  function hideAlert() {
    el.wfAlert.classList.add("hidden");
  }

  // Load Initial Data
  async function init() {
    try {
      const [modData, wfData] = await Promise.all([
        api("/api/v1/modules"),
        api("/api/v1/workflows"),
      ]);
      state.modules = modData.modules || [];
      renderModulesView();

      state.workflows = wfData.workflows || [];
      renderWorkflowSelect();

      // Automatically load the seeded template or first workflow
      const seeded = state.workflows.find((w) => w.workflow_id === "wf-order-support");
      const targetId = seeded ? seeded.workflow_id : state.workflows[0]?.workflow_id;
      if (targetId) {
        await selectWorkflow(targetId);
      } else {
        createNewWorkflow();
      }
    } catch (err) {
      showAlert(`Initial load failed: ${err.message}`, true);
    }
  }

  // Render Modules Tab
  function renderModulesView() {
    while (el.modulesGrid.firstChild) {
      el.modulesGrid.removeChild(el.modulesGrid.firstChild);
    }

    state.modules.forEach((mod) => {
      const card = document.createElement("div");
      card.className = "module-card";

      const header = document.createElement("div");
      header.className = "module-header";

      const title = document.createElement("div");
      title.className = "module-title";
      title.textContent = mod.title || mod.name;

      const badge = document.createElement("span");
      badge.className = "synthetic-badge";
      badge.textContent = mod.is_synthetic ? "Synthetic / Local" : "Custom";

      header.appendChild(title);
      header.appendChild(badge);
      card.appendChild(header);

      const desc = document.createElement("div");
      desc.className = "module-desc";
      desc.textContent = mod.description || "No description provided.";
      card.appendChild(desc);

      const disclaimer = document.createElement("p");
      disclaimer.className = "hint-text";
      disclaimer.style.marginBottom = "8px";
      disclaimer.textContent = mod.disclaimer;
      card.appendChild(disclaimer);

      const tags = document.createElement("div");
      tags.className = "module-tags";

      const nameTag = document.createElement("span");
      nameTag.className = "tag";
      nameTag.textContent = `Handler: ${mod.name}`;
      tags.appendChild(nameTag);

      if (mod.branches && mod.branches.length > 0) {
        const branchTag = document.createElement("span");
        branchTag.className = "tag";
        branchTag.textContent = `Branches: ${mod.branches.join(", ")}`;
        tags.appendChild(branchTag);
      }

      card.appendChild(tags);
      el.modulesGrid.appendChild(card);
    });
  }

  // Workflows Selection & Sync
  function renderWorkflowSelect() {
    while (el.wfSelect.firstChild) {
      el.wfSelect.removeChild(el.wfSelect.firstChild);
    }

    state.workflows.forEach((w) => {
      const opt = document.createElement("option");
      opt.value = w.workflow_id;
      opt.textContent = `${w.name} (Rev ${w.revision})`;
      el.wfSelect.appendChild(opt);
    });

    if (state.currentWorkflowId) {
      el.wfSelect.value = state.currentWorkflowId;
    }
  }

  async function selectWorkflow(workflowId) {
    try {
      const wf = await api(`/api/v1/workflows/${workflowId}`);
      state.currentWorkflowId = wf.workflow_id;
      state.currentRevision = wf.revision;
      state.currentSpec = wf.spec;
      syncSpecToEditor();
      hideAlert();
      renderWorkflowSelect();
    } catch (err) {
      showAlert(`Failed to load workflow: ${err.message}`, true);
    }
  }

  el.wfSelect.addEventListener("change", (e) => {
    if (e.target.value) {
      selectWorkflow(e.target.value);
    }
  });

  // Seeded Template Construction
  function createDefaultSpec() {
    return {
      spec_version: 1,
      name: "OrderSupportWorkflow",
      entry_node: "lookup",
      nodes: {
        lookup: {
          id: "lookup",
          handler: "synthetic_order_lookup",
          transitions: {},
          default_transition: "route",
          requires_approval: false,
        },
        route: {
          id: "route",
          handler: "synthetic_router",
          transitions: {
            refund: "approval_gate",
            standard_support: "agent_assist",
          },
          default_transition: "approval_gate",
          requires_approval: false,
        },
        approval_gate: {
          id: "approval_gate",
          handler: "synthetic_ticket_creator",
          transitions: {},
          default_transition: "agent_assist",
          requires_approval: true,
        },
        agent_assist: {
          id: "agent_assist",
          handler: "synthetic_fake_agent",
          transitions: {},
          default_transition: null,
          requires_approval: false,
        },
      },
      terminal_nodes: ["agent_assist"],
    };
  }

  el.btnLoadTemplate.addEventListener("click", () => {
    state.currentWorkflowId = "wf-order-support";
    state.currentRevision = 1;
    state.currentSpec = createDefaultSpec();
    syncSpecToEditor();
    showAlert("Loaded default seeded Order Support template.", false);
  });

  function createNewWorkflow() {
    state.currentWorkflowId = null;
    state.currentRevision = 1;
    state.currentSpec = {
      spec_version: 1,
      name: "NewWorkflow",
      entry_node: "node_1",
      nodes: {
        node_1: {
          id: "node_1",
          handler: state.modules[0]?.name || "synthetic_order_lookup",
          transitions: {},
          default_transition: null,
          requires_approval: false,
        },
      },
      terminal_nodes: ["node_1"],
    };
    syncSpecToEditor();
    showAlert("Created new blank workflow specification.", false);
  }

  el.btnNewWf.addEventListener("click", createNewWorkflow);

  // Sync Spec into UI Form
  function syncSpecToEditor() {
    const spec = state.currentSpec;
    if (!spec) return;

    el.wfName.value = spec.name || "";
    el.wfTerminalNodes.value = (spec.terminal_nodes || []).join(", ");
    el.wfRevisionLabel.textContent = String(state.currentRevision || 1);
    el.wfHashLabel.textContent = spec.canonical_hash || "Calculated on Save";

    renderNodesList();
    renderVisualGraph();
  }

  // Read Form Back to Spec Object
  function readSpecFromEditor() {
    const nodes = {};
    const nodeCards = el.nodesContainer.querySelectorAll(".node-card");

    nodeCards.forEach((card) => {
      const nid = card.dataset.nodeId;
      const handlerSelect = card.querySelector(".node-handler-select");
      const approvalCheck = card.querySelector(".node-approval-check");
      const defTransSelect = card.querySelector(".node-default-trans");

      const transitions = {};
      const branchRows = card.querySelectorAll(".branch-row");
      branchRows.forEach((row) => {
        const branchKey = row.querySelector(".branch-key-input").value.trim();
        const branchTarget = row.querySelector(".branch-target-select").value.trim();
        if (branchKey && branchTarget) {
          transitions[branchKey] = branchTarget;
        }
      });

      const defTrans = defTransSelect.value ? defTransSelect.value : null;

      nodes[nid] = {
        id: nid,
        handler: handlerSelect.value,
        transitions: transitions,
        default_transition: defTrans,
        requires_approval: Boolean(approvalCheck.checked),
      };
    });

    const terminals = el.wfTerminalNodes.value
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);

    return {
      spec_version: 1,
      name: el.wfName.value.trim() || "UntitledWorkflow",
      entry_node: el.wfEntryNode.value || Object.keys(nodes)[0] || "",
      nodes: nodes,
      terminal_nodes: terminals,
    };
  }

  // Render Visual Graph Cards
  function renderVisualGraph() {
    while (el.visualGraph.firstChild) {
      el.visualGraph.removeChild(el.visualGraph.firstChild);
    }

    const spec = state.currentSpec;
    if (!spec || !spec.nodes) return;

    const nodeIds = Object.keys(spec.nodes);
    nodeIds.forEach((nid, idx) => {
      const node = spec.nodes[nid];
      const box = document.createElement("div");
      box.className = "graph-node-box";
      if (nid === spec.entry_node) box.classList.add("is-entry");
      if (node.requires_approval) box.classList.add("is-gated");

      const nameEl = document.createElement("div");
      nameEl.className = "graph-node-name";
      nameEl.textContent = nid;
      box.appendChild(nameEl);

      const handlerEl = document.createElement("div");
      handlerEl.className = "graph-node-handler";
      handlerEl.textContent = node.handler;
      box.appendChild(handlerEl);

      if (node.requires_approval) {
        const gate = document.createElement("span");
        gate.className = "gate-badge";
        gate.textContent = "Approval Gate";
        box.appendChild(gate);
      }

      el.visualGraph.appendChild(box);

      if (idx < nodeIds.length - 1) {
        const arrow = document.createElement("span");
        arrow.className = "graph-arrow";
        arrow.textContent = "→";
        el.visualGraph.appendChild(arrow);
      }
    });
  }

  // Render Node Editor Cards
  function renderNodesList() {
    while (el.nodesContainer.firstChild) {
      el.nodesContainer.removeChild(el.nodesContainer.firstChild);
    }
    while (el.wfEntryNode.firstChild) {
      el.wfEntryNode.removeChild(el.wfEntryNode.firstChild);
    }

    const spec = state.currentSpec;
    if (!spec || !spec.nodes) return;

    const nodeIds = Object.keys(spec.nodes);

    // Populate Entry Node Options
    nodeIds.forEach((nid) => {
      const opt = document.createElement("option");
      opt.value = nid;
      opt.textContent = nid;
      el.wfEntryNode.appendChild(opt);
    });
    el.wfEntryNode.value = spec.entry_node || nodeIds[0] || "";

    // Build Node Cards
    nodeIds.forEach((nid) => {
      const node = spec.nodes[nid];
      const card = document.createElement("div");
      card.className = "node-card";
      card.dataset.nodeId = nid;

      // Header Row
      const cardHeader = document.createElement("div");
      cardHeader.className = "node-card-header";

      const title = document.createElement("div");
      title.className = "node-card-title";
      title.textContent = `Node: ${nid}`;
      if (node.requires_approval) {
        const badge = document.createElement("span");
        badge.className = "gate-badge";
        badge.textContent = "Gated (Pauses Before Run)";
        title.appendChild(badge);
      }

      const btnDelete = document.createElement("button");
      btnDelete.className = "btn btn-ghost btn-sm";
      btnDelete.textContent = "Remove";
      btnDelete.addEventListener("click", () => {
        delete state.currentSpec.nodes[nid];
        syncSpecToEditor();
      });

      cardHeader.appendChild(title);
      cardHeader.appendChild(btnDelete);
      card.appendChild(cardHeader);

      // Handler Selection & Approval Toggle Row
      const row1 = document.createElement("div");
      row1.className = "form-row";

      const gHandler = document.createElement("div");
      gHandler.className = "form-group flex-2";
      const lblHandler = document.createElement("label");
      lblHandler.textContent = "Assigned Module Handler";
      const selHandler = document.createElement("select");
      selHandler.className = "form-select node-handler-select";

      state.modules.forEach((mod) => {
        const opt = document.createElement("option");
        opt.value = mod.name;
        opt.textContent = `${mod.name} (${mod.is_synthetic ? "Synthetic" : "Custom"})`;
        selHandler.appendChild(opt);
      });
      selHandler.value = node.handler;
      selHandler.addEventListener("change", () => {
        state.currentSpec = readSpecFromEditor();
        renderVisualGraph();
      });

      gHandler.appendChild(lblHandler);
      gHandler.appendChild(selHandler);
      row1.appendChild(gHandler);

      const gApproval = document.createElement("div");
      gApproval.className = "form-group flex-1";
      const lblApproval = document.createElement("label");
      lblApproval.textContent = "Approval Gate";
      const toggleLabel = document.createElement("label");
      toggleLabel.className = "approval-toggle-label";
      const chkApproval = document.createElement("input");
      chkApproval.type = "checkbox";
      chkApproval.className = "node-approval-check";
      chkApproval.checked = Boolean(node.requires_approval);
      chkApproval.addEventListener("change", () => {
        state.currentSpec = readSpecFromEditor();
        renderVisualGraph();
      });

      const spanText = document.createElement("span");
      spanText.textContent = "Require Approval";

      toggleLabel.appendChild(chkApproval);
      toggleLabel.appendChild(spanText);
      gApproval.appendChild(lblApproval);
      gApproval.appendChild(toggleLabel);
      row1.appendChild(gApproval);

      card.appendChild(row1);

      // Default Transition
      const gDefTrans = document.createElement("div");
      gDefTrans.className = "form-group";
      const lblDefTrans = document.createElement("label");
      lblDefTrans.textContent = "Default Transition";
      const selDefTrans = document.createElement("select");
      selDefTrans.className = "form-select node-default-trans";

      const optNone = document.createElement("option");
      optNone.value = "";
      optNone.textContent = "None (Terminal or Explicit Branches)";
      selDefTrans.appendChild(optNone);

      nodeIds.forEach((tId) => {
        if (tId !== nid) {
          const opt = document.createElement("option");
          opt.value = tId;
          opt.textContent = tId;
          selDefTrans.appendChild(opt);
        }
      });
      selDefTrans.value = node.default_transition || "";
      selDefTrans.addEventListener("change", () => {
        state.currentSpec = readSpecFromEditor();
        renderVisualGraph();
      });

      gDefTrans.appendChild(lblDefTrans);
      gDefTrans.appendChild(selDefTrans);
      card.appendChild(gDefTrans);

      // Named Branches Container
      const branchBox = document.createElement("div");
      branchBox.className = "transitions-box";
      const branchTitle = document.createElement("label");
      branchTitle.textContent = "Named Branches (Branch Name → Target Node)";
      branchBox.appendChild(branchTitle);

      const branchList = document.createElement("div");
      branchList.className = "branch-list";

      const transitions = node.transitions || {};
      Object.keys(transitions).forEach((bKey) => {
        const bRow = createBranchRow(bKey, transitions[bKey], nodeIds, nid);
        branchList.appendChild(bRow);
      });
      branchBox.appendChild(branchList);

      const btnAddBranch = document.createElement("button");
      btnAddBranch.className = "btn btn-ghost btn-sm";
      btnAddBranch.textContent = "+ Add Branch";
      btnAddBranch.style.marginTop = "6px";
      btnAddBranch.addEventListener("click", () => {
        const bRow = createBranchRow("", "", nodeIds, nid);
        branchList.appendChild(bRow);
      });
      branchBox.appendChild(btnAddBranch);

      card.appendChild(branchBox);
      el.nodesContainer.appendChild(card);
    });
  }

  function createBranchRow(key, target, nodeIds, currentNid) {
    const row = document.createElement("div");
    row.className = "branch-row";

    const keyInput = document.createElement("input");
    keyInput.type = "text";
    keyInput.className = "form-input branch-key-input";
    keyInput.placeholder = "Branch Name (e.g. refund)";
    keyInput.value = key;
    keyInput.style.flex = "1";

    const targetSelect = document.createElement("select");
    targetSelect.className = "form-select branch-target-select";
    targetSelect.style.flex = "1";

    nodeIds.forEach((tId) => {
      const opt = document.createElement("option");
      opt.value = tId;
      opt.textContent = tId;
      targetSelect.appendChild(opt);
    });
    targetSelect.value = target || nodeIds[0] || "";

    const btnRemove = document.createElement("button");
    btnRemove.className = "btn btn-ghost btn-sm";
    btnRemove.textContent = "✕";
    btnRemove.addEventListener("click", () => {
      row.remove();
      state.currentSpec = readSpecFromEditor();
      renderVisualGraph();
    });

    row.appendChild(keyInput);
    row.appendChild(targetSelect);
    row.appendChild(btnRemove);
    return row;
  }

  // Add Node Handler
  el.btnAddNode.addEventListener("click", () => {
    state.currentSpec = readSpecFromEditor();
    const count = Object.keys(state.currentSpec.nodes).length + 1;
    const newId = `node_${count}`;
    state.currentSpec.nodes[newId] = {
      id: newId,
      handler: state.modules[0]?.name || "synthetic_order_lookup",
      transitions: {},
      default_transition: null,
      requires_approval: false,
    };
    syncSpecToEditor();
  });

  // Format JSON Action
  el.btnFormatJson.addEventListener("click", () => {
    try {
      const parsed = JSON.parse(el.inputStateJson.value);
      el.inputStateJson.value = JSON.stringify(parsed, null, 2);
    } catch (err) {
      showAlert(`Malformed JSON in input state: ${err.message}`, true);
    }
  });

  // Validate Specification
  el.btnValidateWf.addEventListener("click", async () => {
    state.currentSpec = readSpecFromEditor();
    try {
      const res = await api("/api/v1/workflows/validate", {
        method: "POST",
        body: { spec: state.currentSpec },
      });
      showAlert(`✓ Specification is valid! (SHA-256: ${res.spec_hash.slice(0, 16)}...)`, false);
    } catch (err) {
      showAlert(`Validation Error: ${err.message}`, true);
    }
  });

  // Save Workflow
  el.btnSaveWf.addEventListener("click", async () => {
    state.currentSpec = readSpecFromEditor();
    el.btnSaveWf.disabled = true;
    try {
      if (state.currentWorkflowId) {
        const res = await api(`/api/v1/workflows/${state.currentWorkflowId}`, {
          method: "PUT",
          body: {
            expected_revision: state.currentRevision,
            spec: state.currentSpec,
          },
        });
        state.currentRevision = res.revision;
        el.wfRevisionLabel.textContent = String(res.revision);
        el.wfHashLabel.textContent = res.spec_hash.slice(0, 16) + "...";
        showAlert(`Workflow '${res.name}' saved successfully (Revision ${res.revision}).`, false);
      } else {
        const res = await api("/api/v1/workflows", {
          method: "POST",
          body: { spec: state.currentSpec },
        });
        state.currentWorkflowId = res.workflow_id;
        state.currentRevision = res.revision;
        el.wfRevisionLabel.textContent = String(res.revision);
        el.wfHashLabel.textContent = res.spec_hash.slice(0, 16) + "...";
        showAlert(`Created and saved workflow '${res.name}' (${res.workflow_id}).`, false);
      }
      // Refresh list
      const wfData = await api("/api/v1/workflows");
      state.workflows = wfData.workflows || [];
      renderWorkflowSelect();
    } catch (err) {
      showAlert(`Save Error: ${err.message}`, true);
    } finally {
      el.btnSaveWf.disabled = false;
    }
  });

  // JSON Import & Export Modals
  el.btnExportJson.addEventListener("click", () => {
    state.currentSpec = readSpecFromEditor();
    el.modalTitle.textContent = "Export WorkflowSpec (JSON)";
    el.modalJsonTextarea.value = JSON.stringify(state.currentSpec, null, 2);
    el.modalActionBtn.textContent = "Copy to Clipboard";
    el.jsonModal.classList.remove("hidden");
  });

  el.btnImportJson.addEventListener("click", () => {
    el.modalTitle.textContent = "Import WorkflowSpec (JSON)";
    el.modalJsonTextarea.value = "";
    el.modalActionBtn.textContent = "Import into Editor";
    el.jsonModal.classList.remove("hidden");
  });

  el.btnCloseModal.addEventListener("click", () => {
    el.jsonModal.classList.add("hidden");
  });

  el.modalActionBtn.addEventListener("click", () => {
    if (el.modalActionBtn.textContent === "Copy to Clipboard") {
      navigator.clipboard.writeText(el.modalJsonTextarea.value);
      el.modalActionBtn.textContent = "Copied!";
      setTimeout(() => (el.modalActionBtn.textContent = "Copy to Clipboard"), 1500);
    } else {
      try {
        const parsed = JSON.parse(el.modalJsonTextarea.value);
        state.currentSpec = parsed;
        syncSpecToEditor();
        el.jsonModal.classList.add("hidden");
        showAlert("WorkflowSpec imported successfully into editor.", false);
      } catch (err) {
        alert(`Invalid JSON: ${err.message}`);
      }
    }
  });

  // Start Execution Run
  el.btnStartRun.addEventListener("click", async () => {
    let inputState = {};
    try {
      inputState = JSON.parse(el.inputStateJson.value || "{}");
    } catch (err) {
      showAlert(`Invalid initial state JSON: ${err.message}`, true);
      return;
    }

    state.currentSpec = readSpecFromEditor();
    el.btnStartRun.disabled = true;
    el.runLaunchStatus.classList.remove("hidden");
    el.runLaunchStatus.textContent = "Starting execution run...";

    try {
      const payload = state.currentWorkflowId
        ? { workflow_id: state.currentWorkflowId, initial_state: inputState }
        : { spec: state.currentSpec, initial_state: inputState };

      const run = await api("/api/v1/runs", {
        method: "POST",
        body: payload,
      });

      el.runLaunchStatus.textContent = `Run ${run.run_id} started (Status: ${run.status}).`;

      // Switch to Runs tab and inspect run
      switchTab("runs");
      await selectRun(run.run_id);
    } catch (err) {
      el.runLaunchStatus.textContent = `Run start failed: ${err.message}`;
      showAlert(`Run Execution Failed: ${err.message}`, true);
    } finally {
      el.btnStartRun.disabled = false;
    }
  });

  // --------------------------------------------------------------------------
  // Runs View Logic
  // --------------------------------------------------------------------------

  async function loadRuns() {
    try {
      const data = await api("/api/v1/runs?limit=50");
      state.runs = data.runs || [];
      renderRunsTable();
      if (state.selectedRunId) {
        await selectRun(state.selectedRunId);
      }
    } catch (err) {
      console.error("Failed to load runs:", err);
    }
  }

  el.btnRefreshRuns.addEventListener("click", loadRuns);

  function renderRunsTable() {
    while (el.runsTbody.firstChild) {
      el.runsTbody.removeChild(el.runsTbody.firstChild);
    }

    if (state.runs.length === 0) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 5;
      td.className = "empty-cell";
      td.textContent = "No execution runs recorded yet.";
      tr.appendChild(td);
      el.runsTbody.appendChild(tr);
      return;
    }

    state.runs.forEach((r) => {
      const tr = document.createElement("tr");
      if (r.run_id === state.selectedRunId) {
        tr.classList.add("active");
      }

      // Status
      const tdStatus = document.createElement("td");
      const badge = document.createElement("span");
      badge.className = `status-badge status-${r.status.toLowerCase()}`;
      badge.textContent = r.status.toUpperCase();
      tdStatus.appendChild(badge);

      // Run ID
      const tdId = document.createElement("td");
      const codeId = document.createElement("code");
      codeId.textContent = r.run_id.slice(0, 16);
      tdId.appendChild(codeId);

      // Node
      const tdNode = document.createElement("td");
      tdNode.textContent = r.current_node_id || "-";

      // Steps
      const tdSteps = document.createElement("td");
      tdSteps.textContent = String(r.step_count);

      // Time
      const tdTime = document.createElement("td");
      const d = new Date(r.created_at * 1000);
      tdTime.textContent = d.toLocaleTimeString();

      tr.appendChild(tdStatus);
      tr.appendChild(tdId);
      tr.appendChild(tdNode);
      tr.appendChild(tdSteps);
      tr.appendChild(tdTime);

      tr.addEventListener("click", () => selectRun(r.run_id));
      el.runsTbody.appendChild(tr);
    });
  }

  async function selectRun(runId) {
    state.selectedRunId = runId;
    renderRunsTable(); // update active row highlight

    if (state.runPollingInterval) {
      clearInterval(state.runPollingInterval);
      state.runPollingInterval = null;
    }

    try {
      const run = await api(`/api/v1/runs/${runId}`);
      renderRunDetail(run);

      // If still executing, poll honestly until settled
      if (run.status === "running") {
        state.runPollingInterval = setInterval(async () => {
          try {
            const updated = await api(`/api/v1/runs/${runId}`);
            renderRunDetail(updated);
            if (updated.status !== "running") {
              clearInterval(state.runPollingInterval);
              state.runPollingInterval = null;
              loadRuns();
            }
          } catch {
            clearInterval(state.runPollingInterval);
            state.runPollingInterval = null;
          }
        }, 1000);
      }
    } catch (err) {
      alert(`Failed to load run details: ${err.message}`);
    }
  }

  function renderRunDetail(run) {
    el.runEmptyPlaceholder.classList.add("hidden");
    el.runDetailContent.classList.remove("hidden");

    el.detailRunId.textContent = run.run_id;
    el.detailStatusBadge.className = `status-badge status-${run.status.toLowerCase()}`;
    el.detailStatusBadge.textContent = run.status.toUpperCase();
    el.detailCurrentNode.textContent = run.current_node_id || "None";
    el.detailStepCount.textContent = String(run.step_count);

    el.detailCreatedAt.textContent = new Date(run.created_at * 1000).toLocaleString();
    el.detailUpdatedAt.textContent = new Date(run.updated_at * 1000).toLocaleString();

    // Current State Box
    el.detailCurrentState.textContent = JSON.stringify(run.current_state, null, 2);

    // Error Box
    if (run.error) {
      el.detailErrorCard.classList.remove("hidden");
      el.detailErrorMessage.textContent = run.error;
    } else {
      el.detailErrorCard.classList.add("hidden");
    }

    // Pending Approval Panel
    if (run.status === "paused" && run.pending_approval) {
      el.pendingApprovalBox.classList.remove("hidden");
      el.approvalNodeId.textContent = run.pending_approval.node_id;
      el.approvalRequestId.textContent = run.pending_approval.approval_id;
      el.approvalInputState.textContent = JSON.stringify(
        run.pending_approval.input_state,
        null,
        2
      );
      el.btnGrantApproval.disabled = false;
      el.btnRejectApproval.disabled = false;
    } else {
      el.pendingApprovalBox.classList.add("hidden");
    }

    // Render Events Timeline
    while (el.eventsTimeline.firstChild) {
      el.eventsTimeline.removeChild(el.eventsTimeline.firstChild);
    }

    const events = run.events || [];
    events.forEach((ev) => {
      const item = document.createElement("div");
      item.className = "timeline-item";

      const seq = document.createElement("span");
      seq.className = "timeline-seq";
      seq.textContent = `#${ev.seq}`;

      const type = document.createElement("span");
      type.className = "timeline-type";
      type.textContent = ev.type;

      const meta = document.createElement("span");
      meta.className = "timeline-meta";
      meta.textContent = ev.node_id ? `node: ${ev.node_id}` : "";

      item.appendChild(seq);
      item.appendChild(type);
      item.appendChild(meta);
      el.eventsTimeline.appendChild(item);
    });
  }

  // Copy State Action
  el.btnCopyState.addEventListener("click", () => {
    navigator.clipboard.writeText(el.detailCurrentState.textContent);
    el.btnCopyState.textContent = "Copied!";
    setTimeout(() => (el.btnCopyState.textContent = "Copy JSON"), 1500);
  });

  // Approval Actions
  async function submitDecision(decision) {
    if (!state.selectedRunId) return;

    el.btnGrantApproval.disabled = true;
    el.btnRejectApproval.disabled = true;

    let stateUpdate = null;
    const noteRaw = el.approvalNotes.value.trim();
    if (noteRaw) {
      try {
        stateUpdate = JSON.parse(noteRaw);
      } catch {
        stateUpdate = { supervisor_note: noteRaw };
      }
    }

    try {
      const approvalId = el.approvalRequestId.textContent;
      const res = await api(`/api/v1/runs/${state.selectedRunId}/decide`, {
        method: "POST",
        body: {
          approval_id: approvalId,
          decision: decision,
          state_update: stateUpdate,
        },
      });

      renderRunDetail(res);
      await loadRuns();
    } catch (err) {
      alert(`Approval decision failed: ${err.message}`);
      el.btnGrantApproval.disabled = false;
      el.btnRejectApproval.disabled = false;
    }
  }

  el.btnGrantApproval.addEventListener("click", () => submitDecision("approve"));
  el.btnRejectApproval.addEventListener("click", () => submitDecision("reject"));

  // Launch Initializer
  window.addEventListener("DOMContentLoaded", init);
})();
