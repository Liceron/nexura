# nexura-guardrails-ai

Bidirectional bridge for Nexura 0.7.1 and Guardrails AI `>=0.11,<0.12`.
Apache-2.0. Source-only until a release is published.

Install from this workspace with `uv sync`, or build its wheel with
`uv build --package nexura-guardrails-ai`. The package depends on `nexura-agents`,
`nexura-guardrails` and `guardrails-ai`; it does not require Nexura runtime or Studio.

```python
from guardrails import Guard, OnFailAction
from nexura_guardrails import as_guardrail, pii
from nexura_guardrails_ai import GuardrailsAICheck, as_validator

# Nexura -> Guardrails AI: ordinary local regex checks, no model.
guard = Guard(use_server=False).use(as_validator(pii(action="redact"), on_fail=OnFailAction.FIX))
outcome = guard.validate("Email bob@example.com", num_reasks=0)
assert "bob@example.com" not in outcome.validated_output

# Guardrails AI -> Nexura: an existing Guard, or installed Validator instances.
check = GuardrailsAICheck(guard, on_fail="fix", deterministic=True)
output_guardrail = as_guardrail(check, stage="output")
```

`GuardrailsAICheck` accepts one validator, an iterable, `Guard`, or `AsyncGuard`.
It supports `as_guardrail`, `as_tool_guardrail`, composition and `GuardrailPolicy`.
`guardrails_ai(*validators, on_fail="block")` returns an input/output `Preset`.
`as_validator(check)` registers under `nexura/<check-name>` once, while each
instance keeps its own check configuration. Both sync and async Guards work.

`nexura_validators()` supplies injection, PII block, length (100,000 chars)
and JSON-only filters, for JSON output. Select
`nexura_validators(pii_action="redact")` for the PII FIX variant. Configure topics
with `keywords=`/`deny=` to include the topics filter. For plain text choose individual
`as_validator(...)` checks.

See [Guardrails AI documentation](../../docs/guardrails-package.md#guardrails-ai)
for both directions, the built-in category map, Hub installation, telemetry,
network behavior and timeout limits. No validators are installed at runtime.
