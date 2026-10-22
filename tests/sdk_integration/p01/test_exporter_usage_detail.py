"""P38 C2 review (B2): GenAI cache and reasoning token breakdowns reach the V2 envelope
without being counted twice or making ordinary usage look incomplete. Local only.
"""

from __future__ import annotations

from typing import Any

import pytest

from nexura_runtime.integrations.nexura_v2 import (
    TraceMetadata,
    TraceValidationError,
    build_trace_envelope,
)
from nexura_runtime.observability.models import SpanRecord

ALL_FIVE = {
    "gen_ai.request.model": "m",
    "gen_ai.provider.name": "openai",
    "gen_ai.usage.input_tokens": 100,  # includes the 40 cached and 10 cache-written tokens
    "gen_ai.usage.output_tokens": 20,  # includes the 5 reasoning tokens
    "gen_ai.usage.cache_read.input_tokens": 40,
    "gen_ai.usage.cache_write.input_tokens": 10,
    "gen_ai.usage.reasoning.output_tokens": 5,
}


def spans(attributes: dict[str, Any]) -> list[SpanRecord]:
    return [
        SpanRecord(
            trace_id="t1",
            span_id="chat",
            name="chat m",
            start_time=1,
            end_time=2,
            status="ok",
            attributes=attributes,
        )
    ]


def test_breakdowns_are_forwarded_but_not_added_to_the_total() -> None:
    payload = build_trace_envelope(spans(ALL_FIVE), TraceMetadata("t"))
    assert payload["total_tokens"] == 120
    (row,) = payload["spans"]
    attrs = row["attributes"]
    assert attrs["gen_ai.usage.cache_read.input_tokens"] == 40
    assert attrs["gen_ai.usage.cache_write.input_tokens"] == 10
    assert attrs["gen_ai.usage.reasoning.output_tokens"] == 5
    assert attrs["gen_ai.provider.name"] == "openai"
    assert attrs["nexura.usage.complete"] is True


def test_usage_is_complete_without_breakdowns() -> None:
    plain = {k: v for k, v in ALL_FIVE.items() if "cache" not in k and "reasoning" not in k}
    payload = build_trace_envelope(spans(plain), TraceMetadata("t"))
    (row,) = payload["spans"]
    assert row["attributes"]["nexura.usage.complete"] is True
    assert row["attributes"]["nexura.trace.usage_complete"] is True
    assert payload["total_tokens"] == 120


@pytest.mark.parametrize("bad", [-1, True, "40", 1.5])
def test_breakdowns_must_be_nonnegative_integers(bad: Any) -> None:
    with pytest.raises(TraceValidationError):
        build_trace_envelope(
            spans({**ALL_FIVE, "gen_ai.usage.cache_read.input_tokens": bad}), TraceMetadata("t")
        )
