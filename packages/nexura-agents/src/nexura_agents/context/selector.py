# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Conservative context selector enforcing capacity bounds and protected invariants."""

from __future__ import annotations

import copy
import re
import uuid
from collections.abc import Sequence
from typing import Any

from nexura_agents.context.errors import ContextOverflowError
from nexura_agents.context.models import (
    ContextBundle,
    ContextDecisionRecord,
    ContextItem,
    ContextItemRecord,
    ContextPolicy,
)
from nexura_agents.context.projector import bound_logs, compact_table, project_json
from nexura_agents.context.protection import inspect_item_invariants
from nexura_agents.providers.types import (
    ProviderMessage,
    ToolCallPart,
    ToolResultPart,
)
from nexura_agents.tokenizers import (
    TokenEstimator,
    default_token_estimator,
)
from nexura_agents.types import Message

_SECRET_SCRUB_REGEX = re.compile(
    r"(?i)(api[_-]?key|password|secret|access_token|cookie|auth[_-]?token)"
    r"(\s*[=:]\s*)[^\s,;]+",
)
_BEARER_SCRUB_REGEX = re.compile(r"(?i)Bearer\s+[^\s,;]+")


def _scrub_preview(text: str) -> str:
    """Scrub sensitive credentials from audit decision previews."""
    s = _BEARER_SCRUB_REGEX.sub("Bearer [REDACTED]", text)
    s = _SECRET_SCRUB_REGEX.sub(r"\1\2[REDACTED]", s)
    return s[:200] + ("..." if len(s) > 200 else "")


class ContextSelector:
    """Selects and projects context at the assembly-to-provider boundary.

    Guarantees:
    - Authoritative execution history remains immutable.
    - Protected invariants (instructions, negations, IDs, amounts, approvals)
      are preserved exactly.
    - Capacity overflow on mandatory items fails explicitly with ContextOverflowError.
    - Short contexts incur bounded overhead and can bypass selection.
    - Access authorization and source deletion are strictly enforced.
    """

    def __init__(
        self,
        policy: ContextPolicy | None = None,
        token_estimator: TokenEstimator | None = None,
    ) -> None:
        self.policy = policy or ContextPolicy()
        self.token_estimator = token_estimator or default_token_estimator

    def _estimate_item_tokens(self, item: ContextItem) -> int:
        content = item.content
        if isinstance(content, str):
            text = content
        elif isinstance(content, dict):
            text = str(content)
        elif hasattr(content, "text_content") and content.text_content:
            text = str(content.text_content)
        elif hasattr(content, "content") and content.content:
            text = str(content.content)
        else:
            text = str(content)
        return max(1, self.token_estimator.estimate_tokens(text))

    def select(
        self,
        items: Sequence[ContextItem],
        *,
        current_workspace_id: str | None = None,
        allowed_document_ids: set[str] | None = None,
        run_id: str | None = None,
        node_id: str | None = None,
    ) -> ContextBundle:
        """Select, project, and bound candidate items into a ContextBundle."""
        record_id = f"ctx_dec_{uuid.uuid4().hex[:12]}"

        # 1. Check if reduction is enabled (default is opt-in)
        if not self.policy.enabled:
            item_records: list[ContextItemRecord] = []
            tot_tokens = 0
            for it in items:
                toks = self._estimate_item_tokens(it)
                tot_tokens += toks
                item_records.append(
                    ContextItemRecord(
                        item_id=it.item_id,
                        item_type=it.item_type,
                        role=it.role,
                        tool_call_id=it.tool_call_id,
                        source_provenance=it.source_provenance,
                        decision="included",
                        reason="policy_disabled",
                        is_protected=False,
                        original_tokens=toks,
                        projected_tokens=toks,
                        content_preview=_scrub_preview(str(it.content)),
                    )
                )
            dec_rec = ContextDecisionRecord(
                record_id=record_id,
                run_id=run_id,
                node_id=node_id,
                policy_version=self.policy.version,
                status="bypassed_disabled",
                initial_tokens=tot_tokens,
                projected_tokens=tot_tokens,
                reduction_ratio=0.0,
                items=item_records,
            )
            return ContextBundle(
                messages=[it.content for it in items if it.item_type == "message"],
                tools=None,
                decision_record=dec_rec,
                total_tokens=tot_tokens,
                is_reduced=False,
                capacity_limit=self.policy.max_context_tokens,
                remaining_tokens=max(0, self.policy.max_context_tokens - tot_tokens),
            )

        # 2. Check initial tokens for short-context bypass
        initial_tokens = sum(self._estimate_item_tokens(it) for it in items)
        if initial_tokens <= self.policy.short_context_threshold_tokens:
            item_records = []
            for it in items:
                toks = self._estimate_item_tokens(it)
                item_records.append(
                    ContextItemRecord(
                        item_id=it.item_id,
                        item_type=it.item_type,
                        role=it.role,
                        tool_call_id=it.tool_call_id,
                        source_provenance=it.source_provenance,
                        decision="included",
                        reason="bypassed_short_context",
                        is_protected=False,
                        original_tokens=toks,
                        projected_tokens=toks,
                        content_preview=_scrub_preview(str(it.content)),
                    )
                )
            dec_rec = ContextDecisionRecord(
                record_id=record_id,
                run_id=run_id,
                node_id=node_id,
                policy_version=self.policy.version,
                status="bypassed_short_context",
                initial_tokens=initial_tokens,
                projected_tokens=initial_tokens,
                reduction_ratio=0.0,
                items=item_records,
            )
            return ContextBundle(
                messages=[it.content for it in items if it.item_type == "message"],
                tools=None,
                decision_record=dec_rec,
                total_tokens=initial_tokens,
                is_reduced=False,
                capacity_limit=self.policy.max_context_tokens,
                remaining_tokens=max(0, self.policy.max_context_tokens - initial_tokens),
            )

        # 3. Source deletion, revocation, and workspace authorization filtering
        eligible_items: list[tuple[ContextItem, int]] = []
        item_records = []
        omitted_item_ids: set[str] = set()

        for it in items:
            orig_tokens = self._estimate_item_tokens(it)

            # Workspace scoping check
            if (
                current_workspace_id is not None
                and it.workspace_id is not None
                and it.workspace_id != current_workspace_id
            ):
                item_records.append(
                    ContextItemRecord(
                        item_id=it.item_id,
                        item_type=it.item_type,
                        role=it.role,
                        tool_call_id=it.tool_call_id,
                        source_provenance=it.source_provenance,
                        decision="omitted",
                        reason="unauthorized_workspace",
                        is_protected=False,
                        original_tokens=orig_tokens,
                        projected_tokens=0,
                        content_preview=_scrub_preview(str(it.content)),
                    )
                )
                omitted_item_ids.add(it.item_id)
                continue

            # Deletion check
            if it.is_deleted:
                item_records.append(
                    ContextItemRecord(
                        item_id=it.item_id,
                        item_type=it.item_type,
                        role=it.role,
                        tool_call_id=it.tool_call_id,
                        source_provenance=it.source_provenance,
                        decision="omitted",
                        reason="source_deleted",
                        is_protected=False,
                        original_tokens=orig_tokens,
                        projected_tokens=0,
                        content_preview=_scrub_preview(str(it.content)),
                    )
                )
                omitted_item_ids.add(it.item_id)
                continue

            # Revocation check
            if it.is_revoked:
                item_records.append(
                    ContextItemRecord(
                        item_id=it.item_id,
                        item_type=it.item_type,
                        role=it.role,
                        tool_call_id=it.tool_call_id,
                        source_provenance=it.source_provenance,
                        decision="omitted",
                        reason="source_revoked",
                        is_protected=False,
                        original_tokens=orig_tokens,
                        projected_tokens=0,
                        content_preview=_scrub_preview(str(it.content)),
                    )
                )
                omitted_item_ids.add(it.item_id)
                continue

            # Allowed document IDs check
            if (
                allowed_document_ids is not None
                and it.source_provenance is not None
                and it.source_provenance not in allowed_document_ids
            ):
                item_records.append(
                    ContextItemRecord(
                        item_id=it.item_id,
                        item_type=it.item_type,
                        role=it.role,
                        tool_call_id=it.tool_call_id,
                        source_provenance=it.source_provenance,
                        decision="omitted",
                        reason="unauthorized_source",
                        is_protected=False,
                        original_tokens=orig_tokens,
                        projected_tokens=0,
                        content_preview=_scrub_preview(str(it.content)),
                    )
                )
                omitted_item_ids.add(it.item_id)
                continue

            eligible_items.append((it, orig_tokens))

        # 4. Invariant inspection
        protected_items: list[tuple[ContextItem, int, list[str]]] = []
        unprotected_items: list[tuple[ContextItem, int]] = []

        for it, toks in eligible_items:
            is_prot, prot_reasons = inspect_item_invariants(it, self.policy)
            if is_prot:
                protected_items.append((it, toks, prot_reasons))
            else:
                unprotected_items.append((it, toks))

        # 5. Tool association pairing: ensure tool calls & results remain linked
        # If a tool result is protected or included, its tool call must be kept.
        protected_tool_ids = {it.tool_call_id for it, _, _ in protected_items if it.tool_call_id}
        promoted_indices: list[int] = []
        for idx, (it, _toks) in enumerate(unprotected_items):
            if it.tool_call_id and it.tool_call_id in protected_tool_ids:
                promoted_indices.append(idx)

        for idx in reversed(promoted_indices):
            it, toks = unprotected_items.pop(idx)
            protected_items.append((it, toks, ["protected_tool_association"]))

        # 6. Check Capacity Overflow on Protected Invariants
        protected_tokens = sum(toks for _, toks, _ in protected_items)
        if protected_tokens > self.policy.max_context_tokens:
            # Check overflow strategy
            if self.policy.overflow_strategy == "compact_tables":
                # Attempt to compact any table/log within protected items
                new_protected_items = []
                for it, toks, reasons in protected_items:
                    if it.item_type == "table":
                        compacted, _, _ = compact_table(
                            it.content, max_rows=self.policy.max_table_rows
                        )
                        c_toks = self.token_estimator.estimate_tokens(compacted)
                        it_copy = copy.copy(it)
                        it_copy.content = compacted
                        new_protected_items.append((it_copy, c_toks, reasons))
                    elif it.item_type == "log":
                        bounded, _, _ = bound_logs(
                            str(it.content), max_lines=self.policy.max_log_lines
                        )
                        b_toks = self.token_estimator.estimate_tokens(bounded)
                        it_copy = copy.copy(it)
                        it_copy.content = bounded
                        new_protected_items.append((it_copy, b_toks, reasons))
                    else:
                        new_protected_items.append((it, toks, reasons))
                protected_items = new_protected_items
                protected_tokens = sum(toks for _, toks, _ in protected_items)

            if protected_tokens > self.policy.max_context_tokens:
                overflow_ids = [it.item_id for it, _, _ in protected_items]
                msg = (
                    f"Protected context ({protected_tokens} tokens) exceeds maximum capacity "
                    f"({self.policy.max_context_tokens} tokens). "
                    f"Required protected items: {overflow_ids}. "
                    f"Mandatory instructions and evidence cannot be silently discarded."
                )
                dec_rec = ContextDecisionRecord(
                    record_id=record_id,
                    run_id=run_id,
                    node_id=node_id,
                    policy_version=self.policy.version,
                    status="overflow_failed",
                    initial_tokens=initial_tokens,
                    projected_tokens=protected_tokens,
                    reduction_ratio=0.0,
                    items=item_records,
                    overflow_error=msg,
                )
                raise ContextOverflowError(
                    msg,
                    required_tokens=protected_tokens,
                    max_context_tokens=self.policy.max_context_tokens,
                    protected_tokens=protected_tokens,
                    overflow_items=overflow_ids,
                )

        # 7. Pack items: all protected items are included
        final_included_items: list[Any] = []
        final_records: list[ContextItemRecord] = list(item_records)
        current_projected_tokens = 0

        for it, toks, reasons in protected_items:
            final_included_items.append(it.content)
            current_projected_tokens += toks
            final_records.append(
                ContextItemRecord(
                    item_id=it.item_id,
                    item_type=it.item_type,
                    role=it.role,
                    tool_call_id=it.tool_call_id,
                    source_provenance=it.source_provenance,
                    decision="included",
                    reason=reasons[0] if reasons else "protected",
                    is_protected=True,
                    protected_reasons=reasons,
                    original_tokens=toks,
                    projected_tokens=toks,
                    content_preview=_scrub_preview(str(it.content)),
                )
            )

        # 8. Pack non-protected items into remaining capacity
        for it, toks in unprotected_items:
            # Check JSON projections
            projected_content = it.content
            decision_type: Any = "included"
            decision_reason = "within_budget"

            if self.policy.json_projections and (
                it.item_type == "structured_json" or isinstance(it.content, (dict, str))
            ):
                spec = self.policy.json_projections.get(it.item_type, None)
                if spec:
                    projected_content = project_json(it.content, projection_spec=spec)
                    decision_type = "projected"
                    decision_reason = "json_projection_applied"

            # Check table compaction
            if it.item_type == "table":
                compacted, _, omitted = compact_table(
                    projected_content, max_rows=self.policy.max_table_rows
                )
                if omitted > 0:
                    projected_content = compacted
                    decision_type = "compacted"
                    decision_reason = "table_rows_bounded"

            # Check log bounding
            if it.item_type == "log":
                bounded, _, omitted = bound_logs(
                    str(projected_content), max_lines=self.policy.max_log_lines
                )
                if omitted > 0:
                    projected_content = bounded
                    decision_type = "compacted"
                    decision_reason = "log_lines_bounded"

            item_toks = self.token_estimator.estimate_tokens(str(projected_content))

            if current_projected_tokens + item_toks <= self.policy.max_context_tokens:
                current_projected_tokens += item_toks
                final_included_items.append(projected_content)
                final_records.append(
                    ContextItemRecord(
                        item_id=it.item_id,
                        item_type=it.item_type,
                        role=it.role,
                        tool_call_id=it.tool_call_id,
                        source_provenance=it.source_provenance,
                        decision=decision_type,
                        reason=decision_reason,
                        is_protected=False,
                        original_tokens=toks,
                        projected_tokens=item_toks,
                        content_preview=_scrub_preview(str(projected_content)),
                    )
                )
            else:
                final_records.append(
                    ContextItemRecord(
                        item_id=it.item_id,
                        item_type=it.item_type,
                        role=it.role,
                        tool_call_id=it.tool_call_id,
                        source_provenance=it.source_provenance,
                        decision="omitted",
                        reason="exceeded_capacity",
                        is_protected=False,
                        original_tokens=toks,
                        projected_tokens=0,
                        content_preview=_scrub_preview(str(it.content)),
                    )
                )

        ratio = 0.0
        if initial_tokens > 0:
            ratio = round(max(0.0, 1.0 - (current_projected_tokens / initial_tokens)), 4)

        dec_rec = ContextDecisionRecord(
            record_id=record_id,
            run_id=run_id,
            node_id=node_id,
            policy_version=self.policy.version,
            status="applied",
            initial_tokens=initial_tokens,
            projected_tokens=current_projected_tokens,
            reduction_ratio=ratio,
            items=final_records,
        )

        return ContextBundle(
            messages=[
                m
                for m in final_included_items
                if hasattr(m, "role") or isinstance(m, (Message, ProviderMessage))
            ],
            tools=None,
            decision_record=dec_rec,
            total_tokens=current_projected_tokens,
            is_reduced=current_projected_tokens < initial_tokens,
            capacity_limit=self.policy.max_context_tokens,
            remaining_tokens=max(0, self.policy.max_context_tokens - current_projected_tokens),
        )

    def select_messages(
        self,
        messages: Sequence[Message | ProviderMessage],
        tools: list[dict[str, Any]] | None = None,
        *,
        current_workspace_id: str | None = None,
        allowed_document_ids: set[str] | None = None,
        run_id: str | None = None,
        node_id: str | None = None,
    ) -> ContextBundle:
        """Helper to run context selection directly on a sequence of conversation messages.

        Preserves authoritative message history and maps into ContextItem instances.
        """
        items: list[ContextItem] = []
        for idx, m in enumerate(messages):
            role_obj = getattr(m, "role", "user")
            role_val = str(getattr(role_obj, "value", role_obj))
            item_type: Any = "system_prompt" if role_val == "system" else "message"

            tool_call_id = None
            if hasattr(m, "tool_call_id") and m.tool_call_id:
                tool_call_id = m.tool_call_id
            elif hasattr(m, "parts"):
                for p in getattr(m, "parts", []):
                    if isinstance(p, ToolCallPart):
                        tool_call_id = p.id
                        break
                    elif isinstance(p, ToolResultPart):
                        tool_call_id = p.tool_call_id
                        break

            items.append(
                ContextItem(
                    item_id=f"msg_{idx}_{role_val}",
                    item_type=item_type,
                    content=m,
                    role=role_val,
                    tool_call_id=tool_call_id,
                )
            )

        bundle = self.select(
            items,
            current_workspace_id=current_workspace_id,
            allowed_document_ids=allowed_document_ids,
            run_id=run_id,
            node_id=node_id,
        )
        bundle.tools = tools
        return bundle
