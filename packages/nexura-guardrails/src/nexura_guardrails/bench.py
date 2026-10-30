# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Offline guardrails benchmark harness in the nexura-guardrails package.

Evaluates pre-filters and JudgeCheck against local JSONL benchmark datasets
without network calls.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from nexura_guardrails.checks import Check, evaluate
from nexura_guardrails.judge import JudgeCheck
from nexura_guardrails.prefilters import (
    injection,
    json_only,
    length,
    pii,
    topics,
)
from nexura_guardrails.verdict import DEFAULT_JUDGE_CATEGORIES

__all__ = [
    "DEFAULT_DATA_DIR",
    "SUPPORTED_CHECKS",
    "SUPPORTED_SUITES",
    "evaluate_check",
    "format_markdown_report",
    "load_dataset",
    "main",
    "percentile",
    "resolve_check",
    "run_benchmark",
    "run_benchmark_sync",
]

DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "bench_data"
SUPPORTED_SUITES: tuple[str, ...] = ("injection", "pii", "safety", "over_refusal")
SUPPORTED_CHECKS: tuple[str, ...] = (
    "injection",
    "pii",
    "length",
    "topics",
    "json_only",
    "judge",
)


def percentile(values: Sequence[float], p: float) -> float:
    """Compute the p-th percentile (0..100) using linear interpolation."""
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    sorted_v = sorted(values)
    k = (len(sorted_v) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(sorted_v[int(k)])
    d0 = sorted_v[int(f)] * (c - k)
    d1 = sorted_v[int(c)] * (k - f)
    return float(d0 + d1)


def load_dataset(
    data_path: Path | str,
    suite: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str], list[str], list[dict[str, Any]]]:
    """Load JSONL benchmark records from a file or directory.

    Returns:
        (records, file_hashes, licenses, file_details)
    """
    path = Path(data_path)
    if not path.exists():
        raise FileNotFoundError(f"Benchmark data path does not exist: {path}")

    files: list[Path] = []
    if path.is_file():
        files = [path]
    elif path.is_dir():
        if suite is not None:
            suite_file = path / f"{suite}.jsonl"
            if not suite_file.exists():
                raise FileNotFoundError(
                    f"Benchmark suite dataset file '{suite}.jsonl' not found in {path}"
                )
            files = [suite_file]
        else:
            files = sorted(path.glob("*.jsonl"))
    else:
        raise FileNotFoundError(f"Benchmark data path is neither a file nor a directory: {path}")

    if not files:
        raise FileNotFoundError(f"No JSONL dataset files found in {path}")

    records: list[dict[str, Any]] = []
    file_hashes: dict[str, str] = {}
    file_details: list[dict[str, Any]] = []
    all_licenses: set[str] = set()

    for f in files:
        raw_bytes = f.read_bytes()
        sha256 = hashlib.sha256(raw_bytes).hexdigest()
        file_hashes[f.name] = sha256
        file_records_count = 0
        file_licenses: set[str] = set()

        text_content = raw_bytes.decode("utf-8")
        for line_no, line in enumerate(text_content.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at {f.name}:{line_no}: {exc}") from exc

            if not isinstance(row, dict):
                raise ValueError(f"Row {line_no} in {f.name} is not a JSON object")
            if "text" not in row or "label" not in row:
                raise ValueError(f"Missing required fields ('text', 'label') in {f.name}:{line_no}")
            if row["label"] not in ("positive", "negative"):
                raise ValueError(
                    f"Invalid label {row['label']!r} in {f.name}:{line_no}; "
                    "must be 'positive' or 'negative'"
                )

            lic = row.get("license", "unknown")
            all_licenses.add(lic)
            file_licenses.add(lic)
            file_records_count += 1
            records.append(row)

        file_details.append(
            {
                "path": str(f),
                "file": f.name,
                "sha256": sha256,
                "records": file_records_count,
                "licenses": sorted(file_licenses),
            }
        )

    return records, file_hashes, sorted(all_licenses), file_details


def resolve_check(
    name: str,
    *,
    judge_model: Any = None,
    suite: str | None = None,
) -> Check:
    """Resolve a check name to a Check instance."""
    name_clean = name.strip()
    if name_clean == "injection":
        return injection()
    if name_clean == "pii":
        return pii()
    if name_clean == "length":
        return length()
    if name_clean == "topics":
        return topics()
    if name_clean == "json_only":
        return json_only()
    if name_clean == "judge":
        if judge_model is None:
            raise ValueError(
                "Judge check requested but no judge model provided "
                "(pass a model instance in code or use '--judge-model test')."
            )
        resolved_model: Any
        if isinstance(judge_model, str):
            if judge_model == "test":
                from nexura_agents.models.fake import FakeModel

                resolved_model = FakeModel(
                    responses=[
                        json.dumps(
                            {
                                "decision": "allow",
                                "categories": [],
                                "score": 0.0,
                                "reason": "offline test judge",
                            }
                        )
                    ],
                    repeat_last=True,
                )
            else:
                raise ValueError(
                    "The benchmark harness is strictly offline: networked model string "
                    f"{judge_model!r} is not permitted. Pass a local model instance in code "
                    "(e.g. FakeModel) or use 'test'."
                )
        else:
            resolved_model = judge_model

        categories: list[str] | tuple[str, ...] | None = None
        if suite == "injection":
            categories = ("prompt_injection", "jailbreak", "other")
        elif suite == "pii":
            categories = ("privacy", "other")
        elif suite in ("safety", "over_refusal"):
            categories = DEFAULT_JUDGE_CATEGORIES
        return JudgeCheck(model=resolved_model, categories=categories)
    raise ValueError(
        f"Unknown check name {name_clean!r}. Available: {', '.join(SUPPORTED_CHECKS)}."
    )


async def evaluate_check(
    check: Check,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Evaluate one check against a dataset and return metrics."""
    tp = 0
    fn = 0
    fp = 0
    tn = 0
    error_count = 0
    latencies: list[float] = []

    for item in records:
        text = str(item["text"])
        label = item["label"]
        t0 = time.perf_counter()
        is_error = False
        is_detected = False

        try:
            verdict = await evaluate(check, text)
            lat_ms = (time.perf_counter() - t0) * 1000
            latencies.append(lat_ms)
            if verdict.decision == "error":
                is_error = True
            elif verdict.decision in ("block", "rewrite", "review"):
                is_detected = True
        except Exception:
            lat_ms = (time.perf_counter() - t0) * 1000
            latencies.append(lat_ms)
            is_error = True

        if is_error:
            error_count += 1

        if label == "positive":
            if is_detected:
                tp += 1
            else:
                fn += 1
        elif label == "negative":
            if is_detected:
                fp += 1
            else:
                tn += 1

    positives = tp + fn
    negatives = fp + tn
    total = len(records)

    recall = (tp / positives) if positives > 0 else 0.0
    fpr = (fp / negatives) if negatives > 0 else 0.0
    precision = (tp / (tp + fp)) if (tp + fp) > 0 else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0

    p50 = percentile(latencies, 50.0)
    p95 = percentile(latencies, 95.0)

    check_name = getattr(check, "name", str(check))

    return {
        "check": check_name,
        "detection_rate": round(recall, 4),
        "recall": round(recall, 4),
        "false_positive_rate": round(fpr, 4),
        "fpr": round(fpr, 4),
        "precision": round(precision, 4),
        "f1": round(f1, 4),
        "p50_latency_ms": round(p50, 3),
        "p95_latency_ms": round(p95, 3),
        "error_count": error_count,
        "errors": error_count,
        "total": total,
        "positives": positives,
        "negatives": negatives,
        "true_positives": tp,
        "false_positives": fp,
        "true_negatives": tn,
        "false_negatives": fn,
    }


def format_markdown_report(report: dict[str, Any]) -> str:
    """Format benchmark results into a clean markdown document."""
    suite = report.get("suite", "unknown")
    dataset = report.get("dataset", {})
    total = dataset.get("total_records", 0)
    positives = dataset.get("positive_records", 0)
    negatives = dataset.get("negative_records", 0)
    licenses = ", ".join(dataset.get("licenses", [])) or "none"
    hashes = dataset.get("hashes", {})

    lines: list[str] = [
        f"# Guardrails Benchmark Report: {suite}",
        "",
        "## Dataset",
        f"- **Suite**: {suite}",
        f"- **Total records**: {total} ({positives} positives, {negatives} negatives)",
        f"- **Licenses**: {licenses}",
        "- **Dataset file SHA-256 hashes**:",
    ]
    for fn, h in hashes.items():
        lines.append(f"  - `{fn}`: `{h}`")

    lines.extend(
        [
            "",
            "## Results",
            "",
            (
                "| Check | Detection Rate (Recall) | False Positive Rate | Precision | "
                "F1 Score | p50 Latency (ms) | p95 Latency (ms) | Errors |"
            ),
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )

    checks = report.get("checks", {})
    for name, metrics in checks.items():
        rec = f"{metrics.get('recall', 0.0):.2%}"
        fpr = f"{metrics.get('false_positive_rate', 0.0):.2%}"
        prec = f"{metrics.get('precision', 0.0):.2%}"
        f1 = f"{metrics.get('f1', 0.0):.2%}"
        p50 = f"{metrics.get('p50_latency_ms', 0.0):.2f}"
        p95 = f"{metrics.get('p95_latency_ms', 0.0):.2f}"
        err = str(metrics.get("error_count", 0))
        lines.append(f"| {name} | {rec} | {fpr} | {prec} | {f1} | {p50} | {p95} | {err} |")

    lines.append("")
    return "\n".join(lines)


async def run_benchmark(
    suite: str,
    checks: Sequence[str | Check],
    data_dir: Path | str,
    *,
    judge_model: Any = None,
    out_json: Path | str | None = None,
    out_markdown: Path | str | None = None,
) -> dict[str, Any]:
    """Run guardrails benchmark suite across named checks."""
    records, file_hashes, licenses, file_details = load_dataset(data_dir, suite=suite)

    positives_count = sum(1 for r in records if r.get("label") == "positive")
    negatives_count = sum(1 for r in records if r.get("label") == "negative")

    resolved_checks: list[tuple[str, Check]] = []
    for c in checks:
        if isinstance(c, str):
            resolved_checks.append((c, resolve_check(c, judge_model=judge_model, suite=suite)))
        else:
            resolved_checks.append((getattr(c, "name", "check"), c))

    check_results: dict[str, Any] = {}
    for name, check_obj in resolved_checks:
        check_results[name] = await evaluate_check(check_obj, records)

    report = {
        "suite": suite,
        "dataset": {
            "files": file_details,
            "hashes": file_hashes,
            "licenses": licenses,
            "total_records": len(records),
            "positive_records": positives_count,
            "negative_records": negatives_count,
        },
        "dataset_hashes": file_hashes,
        "dataset_licenses": licenses,
        "checks": check_results,
    }

    if out_json:
        out_p = Path(out_json)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if out_markdown:
        md_text = format_markdown_report(report)
        md_p = Path(out_markdown)
        md_p.parent.mkdir(parents=True, exist_ok=True)
        md_p.write_text(md_text, encoding="utf-8")

    return report


def run_benchmark_sync(
    suite: str,
    checks: Sequence[str | Check],
    data_dir: Path | str,
    *,
    judge_model: Any = None,
    out_json: Path | str | None = None,
    out_markdown: Path | str | None = None,
) -> dict[str, Any]:
    """Synchronous entry point for running a benchmark."""
    return asyncio.run(
        run_benchmark(
            suite=suite,
            checks=checks,
            data_dir=data_dir,
            judge_model=judge_model,
            out_json=out_json,
            out_markdown=out_markdown,
        )
    )


def main(argv: list[str] | None = None, *, judge_model_override: Any = None) -> int:
    """CLI entry point for python -m nexura_guardrails.bench."""
    parser = argparse.ArgumentParser(
        prog="python -m nexura_guardrails.bench",
        description="Offline guardrails benchmark harness (makes no network calls).",
    )
    parser.add_argument(
        "--suite",
        required=True,
        help=f"Benchmark suite to run. Supported suites: {', '.join(SUPPORTED_SUITES)}.",
    )
    parser.add_argument(
        "--checks",
        required=True,
        help=(
            "Comma-separated check names (e.g. injection,pii,judge). "
            f"Available: {', '.join(SUPPORTED_CHECKS)}."
        ),
    )
    parser.add_argument(
        "--data",
        required=False,
        default=str(DEFAULT_DATA_DIR),
        help=(
            "Path to directory containing JSONL datasets or a single JSONL file "
            f"(default: {DEFAULT_DATA_DIR})."
        ),
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Path to output JSON report file.",
    )
    parser.add_argument(
        "--markdown",
        required=False,
        default=None,
        help="Optional path to output Markdown report file.",
    )
    parser.add_argument(
        "--judge-model",
        required=False,
        default=None,
        help=(
            "Offline model spec for JudgeCheck. Only explicitly local options "
            "(e.g. 'test') are permitted. Network model strings are refused."
        ),
    )

    args = parser.parse_args(argv)

    if args.suite not in SUPPORTED_SUITES:
        sys.stderr.write(
            f"Benchmark error: Unknown or unsupported suite {args.suite!r}. "
            f"Supported suites: {', '.join(SUPPORTED_SUITES)}.\n"
        )
        return 1

    check_names = [c.strip() for c in args.checks.split(",") if c.strip()]
    if not check_names:
        sys.stderr.write("Benchmark error: At least one check must be specified in --checks.\n")
        return 1

    unknown_checks = [c for c in check_names if c not in SUPPORTED_CHECKS]
    if unknown_checks:
        sys.stderr.write(
            f"Benchmark error: Unknown check(s): {', '.join(unknown_checks)}. "
            f"Available checks: {', '.join(SUPPORTED_CHECKS)}.\n"
        )
        return 1

    model = judge_model_override or args.judge_model

    # Enforce strictly offline execution: reject networked model strings
    if args.judge_model is not None and args.judge_model != "test":
        sys.stderr.write(
            "Benchmark error: The benchmark harness is strictly offline: "
            f"networked model string {args.judge_model!r} is not permitted. "
            "Pass a local model instance in code (e.g. FakeModel) or use '--judge-model test'.\n"
        )
        return 1

    if "judge" in check_names:
        if model is None:
            sys.stderr.write(
                "Benchmark error: Judge check requested but no judge model provided. "
                "Pass a local model instance in code (e.g. FakeModel) or use "
                "'--judge-model test'.\n"
            )
            return 1
        if isinstance(model, str) and model != "test":
            sys.stderr.write(
                "Benchmark error: The benchmark harness is strictly offline: "
                f"networked model string {model!r} is not permitted. "
                "Pass a local model instance in code (e.g. FakeModel) or use "
                "'--judge-model test'.\n"
            )
            return 1

    try:
        report = run_benchmark_sync(
            suite=args.suite,
            checks=check_names,
            data_dir=args.data,
            judge_model=model,
            out_json=args.out,
            out_markdown=args.markdown,
        )
    except Exception as exc:
        sys.stderr.write(f"Benchmark error: {exc}\n")
        return 1

    checks_results = report.get("checks", {})
    total_failures: list[str] = []
    partial_failures: list[str] = []
    for c_name, c_metrics in checks_results.items():
        err = c_metrics.get("error_count", 0)
        tot = c_metrics.get("total", 0)
        if tot > 0 and err == tot:
            total_failures.append(f"{c_name} ({err}/{tot} errors)")
        elif err > 0:
            partial_failures.append(f"{c_name} ({err}/{tot} errors)")

    if total_failures:
        sys.stderr.write(
            f"Benchmark failed: total evaluation failure in check(s): {', '.join(total_failures)}\n"
        )
        return 2

    if partial_failures:
        sys.stderr.write(
            f"Benchmark completed with partial errors in check(s): {', '.join(partial_failures)}\n"
        )
        return 3

    return 0


if __name__ == "__main__":
    sys.exit(main())
