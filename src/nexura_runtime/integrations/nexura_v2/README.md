# V2 trace compatibility

Public import: `nexura_runtime.integrations.nexura_v2`.
Exports complete, finished `NexuraTracer.get_spans()` to V2's `/api/v1/traces`.
It does not run V2 flows, migrate databases, enforce V2 action approvals or move
workflow execution into the platform.

```python
from nexura_runtime.integrations.nexura_v2 import NexuraV2TraceExporter, TraceMetadata

# Use an explicitly supplied scoped platform key and HTTPS origin.
# This example is configuration guidance; do not invoke a live endpoint in tests.
with NexuraV2TraceExporter(platform_origin, platform_key) as exporter:
    receipt = exporter.export(tracer.get_spans(), TraceMetadata("Support run"))
```

Run `uv run python examples/sdk_integration/p01.py` for real SDK FakeModel/Runner
execution through an explicitly local httpx.MockTransport ingestion fixture.
That example never connects to a platform or paid provider.

## Guarantees and boundaries

- Deterministic trace-scoped UUID mapping of SDK span IDs, preserved parent links,
  stable ordering and identical retry bytes. Caller source trace IDs must be
  globally unique across runs; a repeated ID intentionally refers to the same trace.
- Finished timestamps and hierarchy are validated before dispatch; max 1000
  spans, depth 64 and serialized body 4 MiB follow the inspected V2 contract.
- Content excluded by default; safe known telemetry attributes allowlisted.
  Opt-in `capture_content=True` requires a `redactor` callback. Reads
  `nexura.input`/`nexura.output` attributes; known credential fields and exporter
  key are additionally redacted. This is not automatic PII detection.
- Unknown provider usage stays absent in source attributes and has explicit
  completeness metadata. Legacy aggregate totals are known-token subtotals;
  V2's current UI/aggregation does not fully expose that uncertainty or cache
  read/write economics. Leaf aggregation follows legacy semantics and is not a
  provider-billing ledger or proof of complete instrumentation coverage.
- Client ownership respected. Close/context-manager releases only owned clients.
  Redirects disabled even for caller clients configured to follow redirects.
  Remote HTTP rejected; loopback HTTP allowed for local fixtures.
- Synchronous export, bounded attempts/timeout/backoff; use outside async hot
  paths or invoke in an owned worker thread. No background exporter or durable
  retry queue is implied. HTTP success must acknowledge the matching UUID.
- No Langflow/lfx imports and no parent package dependency. No provider pricing,
  cache hits, token counts or original platform integration is fabricated.

Verification uses only the local mock ingestion fixture described above; it does not
prove acceptance by a live V2 service. The contract limits are listed above. V2 itself
remains unmodified/uninvoked in this phase; no V2 source was copied.

## Closed edition

The V2-shaped run server (`V2FlowServer`) and the platform-service adapters that plug into it
(Firebase ID-token verification, RBAC and policy bundles, V2-shaped run records, the shadow-run
harness) are part of the closed edition since EDITIONS_PLAN step 6. Only the trace exporter
above is open source.
