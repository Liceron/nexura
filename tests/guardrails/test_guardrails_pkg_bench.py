# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Tests for P39 G7: offline guardrails benchmark harness."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from guardrails_pkg_helpers import verdict_json
from nexura_agents import FakeModel
from nexura_agents.run_context import RunContext
from nexura_guardrails.bench import (
    DEFAULT_DATA_DIR,
    evaluate_check,
    load_dataset,
    main,
    percentile,
    resolve_check,
    run_benchmark,
)
from nexura_guardrails.verdict import CheckInput, Verdict


class DummyCheck:
    """A simple deterministic check for testing benchmark metrics."""

    def __init__(self, name: str, blocked_texts: set[str]) -> None:
        self.name = name
        self.blocked_texts = blocked_texts
        self.stages = frozenset({"input"})
        self.deterministic = True

    async def __call__(self, ctx: RunContext[Any], item: CheckInput) -> Verdict:
        if item.text in self.blocked_texts:
            return Verdict(
                decision="block",
                categories=("prompt_injection",),
                score=0.9,
                reason="blocked by dummy",
                check=self.name,
            )
        return Verdict(decision="allow", check=self.name)


class ExplodingCheck:
    """A check that always raises an unhandled exception."""

    def __init__(self, name: str = "exploding") -> None:
        self.name = name
        self.stages = frozenset({"input"})
        self.deterministic = True

    async def __call__(self, ctx: RunContext[Any], item: CheckInput) -> Verdict:
        raise RuntimeError("boom")


# --------------------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_metric_maths_exact() -> None:
    """Metric maths on a tiny hand-made set with exact expected recall/FPR/precision/F1."""
    records = [
        {"text": "pos_1", "label": "positive", "source": "test", "license": "MIT"},
        {"text": "pos_2", "label": "positive", "source": "test", "license": "MIT"},
        {"text": "pos_3", "label": "positive", "source": "test", "license": "MIT"},
        {"text": "pos_4", "label": "positive", "source": "test", "license": "MIT"},
        {"text": "neg_1", "label": "negative", "source": "test", "license": "MIT"},
        {"text": "neg_2", "label": "negative", "source": "test", "license": "MIT"},
    ]

    # Check 1: blocks pos_1, pos_2, pos_3. Misses pos_4. Correctly allows neg_1, neg_2.
    # Positives = 4, Negatives = 2
    # TP = 3, FN = 1 -> Recall = 3/4 = 0.75
    # FP = 0, TN = 2 -> FPR = 0/2 = 0.0
    # Precision = 3 / (3 + 0) = 1.0
    # F1 = 2 * (1.0 * 0.75) / (1.0 + 0.75) = 1.5 / 1.75 = 6/7 = 0.857142857...
    check1 = DummyCheck("test_check_1", {"pos_1", "pos_2", "pos_3"})
    metrics1 = await evaluate_check(check1, records)

    assert metrics1["positives"] == 4
    assert metrics1["negatives"] == 2
    assert metrics1["true_positives"] == 3
    assert metrics1["false_negatives"] == 1
    assert metrics1["false_positives"] == 0
    assert metrics1["true_negatives"] == 2
    assert metrics1["recall"] == 0.75
    assert metrics1["detection_rate"] == 0.75
    assert metrics1["fpr"] == 0.0
    assert metrics1["false_positive_rate"] == 0.0
    assert metrics1["precision"] == 1.0
    assert metrics1["f1"] == round(6 / 7, 4)

    # Check 2: blocks pos_1 (TP=1, FN=3). Blocks neg_1 (FP=1, TN=1).
    # Recall = 1/4 = 0.25
    # FPR = 1/2 = 0.5
    # Precision = 1 / (1 + 1) = 0.5
    # F1 = 2 * (0.5 * 0.25) / (0.5 + 0.25) = 0.25 / 0.75 = 1/3 = 0.333333...
    check2 = DummyCheck("test_check_2", {"pos_1", "neg_1"})
    metrics2 = await evaluate_check(check2, records)

    assert metrics2["true_positives"] == 1
    assert metrics2["false_negatives"] == 3
    assert metrics2["false_positives"] == 1
    assert metrics2["true_negatives"] == 1
    assert metrics2["recall"] == 0.25
    assert metrics2["fpr"] == 0.5
    assert metrics2["precision"] == 0.5
    assert metrics2["f1"] == round(1 / 3, 4)


@pytest.mark.asyncio
async def test_latency_fields_present_and_percentile() -> None:
    """Latency fields (p50/p95) are present, non-negative, and order-consistent."""
    # Test percentile math
    assert percentile([], 50.0) == 0.0
    assert percentile([10.0], 50.0) == 10.0
    assert percentile([10.0, 20.0, 30.0, 40.0, 50.0], 50.0) == 30.0
    assert percentile([10.0, 20.0, 30.0, 40.0, 50.0], 95.0) == 48.0

    records = [
        {"text": "pos", "label": "positive", "source": "test", "license": "MIT"},
        {"text": "neg", "label": "negative", "source": "test", "license": "MIT"},
    ]
    check = DummyCheck("latency_test", {"pos"})
    metrics = await evaluate_check(check, records)

    assert "p50_latency_ms" in metrics
    assert "p95_latency_ms" in metrics
    assert isinstance(metrics["p50_latency_ms"], float)
    assert isinstance(metrics["p95_latency_ms"], float)
    assert metrics["p50_latency_ms"] >= 0.0
    assert metrics["p95_latency_ms"] >= metrics["p50_latency_ms"]


def test_hashes_and_licenses_in_report(tmp_path: Path) -> None:
    """Dataset file SHA-256 hashes and license strings appear in the report."""
    fixture_file = tmp_path / "custom.jsonl"
    content = (
        '{"text": "Sample attack", "label": "positive", "source": "t", "license": "Apache-2.0"}\n'
        '{"text": "Sample benign", "label": "negative", "source": "t", "license": "MIT"}\n'
    )
    fixture_file.write_text(content, encoding="utf-8")
    expected_hash = hashlib.sha256(fixture_file.read_bytes()).hexdigest()

    records, hashes, licenses, details = load_dataset(fixture_file)
    assert len(records) == 2
    assert hashes[fixture_file.name] == expected_hash
    assert sorted(licenses) == ["Apache-2.0", "MIT"]
    assert details[0]["sha256"] == expected_hash
    assert details[0]["records"] == 2


@pytest.mark.asyncio
async def test_judge_run_with_fake_model() -> None:
    """Benchmark runs with a JudgeCheck and FakeModel."""
    records = [
        {"text": "malicious injection", "label": "positive", "source": "test", "license": "MIT"},
        {"text": "benign query", "label": "negative", "source": "test", "license": "MIT"},
    ]
    model = FakeModel(
        responses=[
            verdict_json("block", ["prompt_injection"], 0.95, "injection detected"),
            verdict_json("allow", [], 0.05, "benign query"),
        ]
    )
    judge_check = resolve_check("judge", judge_model=model, suite="injection")
    metrics = await evaluate_check(judge_check, records)

    assert metrics["check"] == "judge"
    assert metrics["recall"] == 1.0
    assert metrics["fpr"] == 0.0
    assert metrics["precision"] == 1.0
    assert metrics["f1"] == 1.0
    assert metrics["error_count"] == 0
    assert len(model.calls) == 2


def test_cli_end_to_end_json_and_markdown(tmp_path: Path) -> None:
    """CLI end-to-end creates both report.json and report.md."""
    out_json = tmp_path / "report.json"
    out_md = tmp_path / "report.md"

    exit_code = main(
        [
            "--suite",
            "injection",
            "--checks",
            "injection",
            "--data",
            str(DEFAULT_DATA_DIR),
            "--out",
            str(out_json),
            "--markdown",
            str(out_md),
        ]
    )
    assert exit_code == 0
    assert out_json.exists()
    assert out_md.exists()

    report_data = json.loads(out_json.read_text(encoding="utf-8"))
    assert report_data["suite"] == "injection"
    assert "injection.jsonl" in report_data["dataset"]["hashes"]
    assert "MIT" in report_data["dataset"]["licenses"]
    assert "injection" in report_data["checks"]
    injection_metrics = report_data["checks"]["injection"]
    assert injection_metrics["total"] == 60
    assert injection_metrics["positives"] == 30
    assert injection_metrics["negatives"] == 30
    assert "p50_latency_ms" in injection_metrics
    assert "p95_latency_ms" in injection_metrics
    assert "error_count" in injection_metrics

    md_content = out_md.read_text(encoding="utf-8")
    assert "# Guardrails Benchmark Report: injection" in md_content
    assert "| Check | Detection Rate (Recall) |" in md_content
    assert "| injection |" in md_content
    assert "injection.jsonl" in md_content


@pytest.mark.asyncio
async def test_error_counting_when_check_raises() -> None:
    """Errors during check evaluation are caught and counted without crashing."""
    records = [
        {"text": "pos_1", "label": "positive", "source": "test", "license": "MIT"},
        {"text": "neg_1", "label": "negative", "source": "test", "license": "MIT"},
    ]
    exploding = ExplodingCheck("exploding")
    metrics = await evaluate_check(exploding, records)

    assert metrics["error_count"] == 2
    assert metrics["errors"] == 2
    assert metrics["true_positives"] == 0
    assert metrics["false_positives"] == 0
    assert metrics["recall"] == 0.0


@pytest.mark.asyncio
async def test_committed_bench_fixtures_baseline() -> None:
    """Benchmark runs against the committed fixture datasets for injection and pii."""
    injection_report = await run_benchmark(
        suite="injection",
        checks=["injection"],
        data_dir=DEFAULT_DATA_DIR,
    )
    assert injection_report["suite"] == "injection"
    assert injection_report["dataset"]["total_records"] == 60
    assert "injection" in injection_report["checks"]
    inj_m = injection_report["checks"]["injection"]
    assert inj_m["recall"] == 0.7
    assert inj_m["fpr"] == 0.0333
    assert inj_m["precision"] == 0.9545
    assert inj_m["f1"] == 0.8077
    assert inj_m["true_positives"] == 21
    assert inj_m["false_positives"] == 1
    assert inj_m["true_negatives"] == 29
    assert inj_m["false_negatives"] == 9
    assert inj_m["error_count"] == 0

    pii_report = await run_benchmark(
        suite="pii",
        checks=["pii"],
        data_dir=DEFAULT_DATA_DIR,
    )
    assert pii_report["suite"] == "pii"
    assert pii_report["dataset"]["total_records"] == 40
    assert "pii" in pii_report["checks"]
    pii_m = pii_report["checks"]["pii"]
    assert pii_m["recall"] == 1.0
    assert pii_m["fpr"] == 0.05
    assert pii_m["precision"] == 0.9524
    assert pii_m["f1"] == 0.9756
    assert pii_m["true_positives"] == 20
    assert pii_m["false_positives"] == 1
    assert pii_m["true_negatives"] == 19
    assert pii_m["false_negatives"] == 0
    assert pii_m["error_count"] == 0


def test_offline_cli_must_not_dispatch_a_provider_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No provider is contacted: replace the factory with a recording local fake."""
    dispatched = []

    def factory(model_spec: Any) -> Any:
        dispatched.append(model_spec)
        return FakeModel(
            [json.dumps({"decision": "allow", "categories": [], "score": 0.0, "reason": "fixture"})]
        )

    monkeypatch.setattr("nexura_agents.models.infer.infer_model", factory)
    dataset = tmp_path / "one.jsonl"
    dataset.write_text('{"text":"hello","label":"negative","source":"review","license":"MIT"}\n')
    main(
        [
            "--suite",
            "injection",
            "--checks",
            "judge",
            "--judge-model",
            "openai:gpt-4.1-mini",
            "--data",
            str(dataset),
            "--out",
            str(tmp_path / "out.json"),
        ]
    )
    assert dispatched == [], "offline CLI entered the provider dispatcher"


def test_absent_suite_must_not_silently_combine_unrelated_fixtures() -> None:
    with pytest.raises((ValueError, FileNotFoundError)):
        load_dataset(DEFAULT_DATA_DIR, suite="safety")


def test_all_judge_errors_must_not_be_a_successful_cli_run(tmp_path: Path) -> None:
    class BrokenOfflineModel:
        async def generate(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("deliberate offline judge failure")

    out = tmp_path / "errors.json"
    status = main(
        [
            "--suite",
            "injection",
            "--checks",
            "judge",
            "--data",
            str(DEFAULT_DATA_DIR),
            "--out",
            str(out),
        ],
        judge_model_override=BrokenOfflineModel(),
    )
    report = json.loads(out.read_text())
    assert report["checks"]["judge"]["error_count"] == 60
    assert status != 0, "all 60 evaluations errored but the CLI returned success"


def test_cli_rejects_unknown_suite(tmp_path: Path) -> None:
    """CLI refuses unknown suite with non-zero exit code."""
    out = tmp_path / "out.json"
    status = main(
        [
            "--suite",
            "nonexistent_suite",
            "--checks",
            "injection",
            "--data",
            str(DEFAULT_DATA_DIR),
            "--out",
            str(out),
        ]
    )
    assert status != 0
    assert not out.exists()


def test_cli_local_test_judge_model(tmp_path: Path) -> None:
    """CLI works offline with the explicitly local 'test' judge model."""
    out = tmp_path / "out.json"
    status = main(
        [
            "--suite",
            "injection",
            "--checks",
            "judge",
            "--judge-model",
            "test",
            "--data",
            str(DEFAULT_DATA_DIR),
            "--out",
            str(out),
        ]
    )
    assert status == 0
    assert out.exists()
    data = json.loads(out.read_text())
    assert data["checks"]["judge"]["total"] == 60
    assert data["checks"]["judge"]["error_count"] == 0


def test_resolve_check_rejects_networked_model_string() -> None:
    """resolve_check strictly prohibits networked model strings."""
    with pytest.raises(ValueError, match="strictly offline"):
        resolve_check("judge", judge_model="ollama:qwen3:8b", suite="injection")
    with pytest.raises(ValueError, match="strictly offline"):
        resolve_check("judge", judge_model="openai:gpt-4.1-mini", suite="injection")


def test_cli_returns_code_3_on_partial_errors(tmp_path: Path) -> None:
    """CLI returns exit code 3 when a check encounters partial errors."""

    class SometimesFailingModel(FakeModel):
        def __init__(self) -> None:
            super().__init__(
                responses=[
                    json.dumps(
                        {
                            "decision": "allow",
                            "categories": [],
                            "score": 0.0,
                            "reason": "allowed",
                        }
                    )
                ],
                repeat_last=True,
            )
            self.call_count = 0

        async def generate(self, *args: Any, **kwargs: Any) -> Any:
            self.call_count += 1
            if self.call_count <= 5:
                raise RuntimeError("intermittent failure")
            return await super().generate(*args, **kwargs)

    out = tmp_path / "partial.json"
    status = main(
        [
            "--suite",
            "injection",
            "--checks",
            "judge",
            "--data",
            str(DEFAULT_DATA_DIR),
            "--out",
            str(out),
        ],
        judge_model_override=SometimesFailingModel(),
    )
    assert status == 3
    data = json.loads(out.read_text())
    assert data["checks"]["judge"]["error_count"] == 5
