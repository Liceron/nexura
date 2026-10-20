"""O09: reference release scenario, restore compatibility and evidence integrity."""

from __future__ import annotations

import importlib.util
import io
import json
import socket
import sys
import tarfile
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest

from nexura_runtime.release import BackupEngine, UnsupportedBackupVersionError
from nexura_runtime.release.evidence import (
    EvidenceError,
    Invariant,
    Receipt,
    cost_per_success,
    read_receipts,
    summarize,
    write_receipts,
)

ROOT = Path(__file__).resolve().parents[3]


def _load_example() -> ModuleType:
    spec = importlib.util.spec_from_file_location("o09_example", ROOT / "examples/oss/o09.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve types via sys.modules
    spec.loader.exec_module(module)
    return module


o09 = _load_example()


# ---------------------------------------------------------------------------
# Reference order-support scenario
# ---------------------------------------------------------------------------


async def test_reference_scenario_invariants(tmp_path: Path) -> None:
    with o09.offline():
        result = await o09.reference_scenario(tmp_path)

    assert result["paused_status"] == "paused" and result["paused_at"] == "refund"
    assert result["final_status"] == "completed"
    # Required evidence kept; deleted and foreign sources omitted with reasons.
    assert {"instructions", "order", "policy-current"} <= set(result["evidence_included"])
    assert result["evidence_omitted"] == {
        "policy-retired": "source_deleted",
        "policy-other-team": "unauthorized_workspace",
    }
    # Exactly one model dispatch and one refund despite restart and duplicate resume.
    assert result["provider_dispatches"] == 1
    assert result["refunds_executed"] == 1
    assert result["duplicate_resume_rejected"] is True
    # Committed spend survives restart and restore; nothing charged twice.
    assert Decimal(result["spent_before_restart"]) > 0
    assert result["spent_after_restart"] == result["spent_before_restart"]
    assert result["total_spent_usd"] == result["spent_before_restart"]
    assert result["restored_spent_usd"] == result["spent_before_restart"]
    assert result["unknown_usage_count"] == 0
    # The restored backup still holds the pending decision and can be approved.
    assert result["restored_resume_status"] == "completed"
    assert result["restored_refunds_executed"] == 1
    assert result["export_spec_hash_matches"] is True


async def test_budget_exhaustion_blocks_dispatch_and_effects(tmp_path: Path) -> None:
    with o09.offline():
        result = await o09.budget_exhaustion_scenario(tmp_path)
    assert result["status"] == "failed"
    assert "cost limit exceeded" in result["error"]
    assert result["provider_dispatches"] == 0
    assert result["refunds_executed"] == 0
    assert result["pending_approval"] is False


def test_offline_guard_blocks_external_connections() -> None:
    with o09.offline(), socket.socket() as sock, pytest.raises(RuntimeError, match="external"):
        sock.connect(("203.0.113.10", 443))


# ---------------------------------------------------------------------------
# Backup compatibility boundary
# ---------------------------------------------------------------------------


def _rewrite_manifest_version(archive: Path, version: int, out: Path) -> None:
    with tarfile.open(archive, "r:gz") as src, tarfile.open(out, "w:gz") as dst:
        for member in src.getmembers():
            data = src.extractfile(member)
            if member.name == BackupEngine.MANIFEST_FILENAME and data is not None:
                manifest = json.loads(data.read())
                manifest["version"] = version
                payload = json.dumps(manifest).encode()
                member.size = len(payload)
                dst.addfile(member, io.BytesIO(payload))
            else:
                dst.addfile(member, data)


@pytest.mark.parametrize("version", [1, 3, None])
def test_restore_rejects_unsupported_manifest_versions(tmp_path: Path, version: int | None) -> None:
    db = tmp_path / "state.sqlite3"
    db.write_bytes(b"")
    archive = tmp_path / "backup.tar.gz"
    BackupEngine().create_backup(db, target_archive_path=archive)
    altered = tmp_path / "altered.tar.gz"
    _rewrite_manifest_version(archive, version, altered)  # type: ignore[arg-type]

    target = tmp_path / "restored.sqlite3"
    with pytest.raises(UnsupportedBackupVersionError, match="not supported"):
        BackupEngine().restore_backup(altered, target_db_path=target)
    assert not target.exists(), "rejected restore must not publish any state"


# ---------------------------------------------------------------------------
# Evidence integrity: the summary cannot be made green without real receipts
# ---------------------------------------------------------------------------


def _passed(check_id: str) -> Receipt:
    return Receipt(
        check_id=check_id,
        status="passed",
        command=f"run {check_id}",
        exit_code=0,
        duration_s=1.0,
        invariants=(Invariant("exit code zero", True),),
    )


def test_all_required_passing_receipts_are_ready() -> None:
    summary = summarize([_passed("a"), _passed("b")], ["a", "b"], environment={})
    summary.assert_consistent()
    assert summary.ready


def test_seeded_bad_expectation_is_detected() -> None:
    bad = Receipt(
        check_id="a",
        status="passed",
        command="run a",
        exit_code=0,
        duration_s=1.0,
        invariants=(Invariant("refund executed once", False, "executed 2 times"),),
    )
    summary = summarize([bad], ["a"], environment={})
    assert not summary.ready
    with pytest.raises(EvidenceError, match="invariants failed"):
        summary.assert_consistent()


def test_missing_receipt_is_not_ready() -> None:
    summary = summarize([_passed("a")], ["a", "b"], environment={})
    assert summary.missing == ["b"]
    assert not summary.ready
    with pytest.raises(EvidenceError, match="b: required receipt missing"):
        summary.assert_consistent()
    assert "| b | MISSING |" in summary.to_markdown()


@pytest.mark.parametrize(
    ("receipt", "message"),
    [
        (
            Receipt("a", "passed", "", 0, 0.0, (Invariant("x", True),)),
            "without an executed command",
        ),
        (Receipt("a", "passed", "run a", 0, 0.0, ()), "without any asserted invariant"),
        (Receipt("a", "passed", "run a", 1, 0.0, (Invariant("x", True),)), "exit code 1"),
        (Receipt("a", "skipped", "run a", None, 0.0), "without a stated reason"),
    ],
)
def test_simulated_or_unexplained_results_are_rejected(receipt: Receipt, message: str) -> None:
    summary = summarize([receipt], ["a"], environment={})
    with pytest.raises(EvidenceError, match=message):
        summary.assert_consistent()


def test_skipped_required_profile_stays_unverified() -> None:
    skipped = Receipt("vector", "skipped", "pytest b22", None, 0.0, reason="no Postgres")
    summary = summarize([skipped], ["vector"], environment={})
    summary.assert_consistent()  # an honest skip is consistent evidence...
    assert summary.unverified == ["vector"]  # ...but the scope is not verified
    assert not summary.ready


def test_cost_per_success_is_undefined_without_successes() -> None:
    assert cost_per_success(Decimal("0.30"), 0) is None
    assert cost_per_success(None, 3) is None
    assert cost_per_success(Decimal("0.30"), 3) == Decimal("0.10")


def test_receipts_round_trip_and_reject_unknown_schema(tmp_path: Path) -> None:
    path = tmp_path / "receipts.json"
    write_receipts(path, [_passed("a")])
    assert read_receipts(path)[0].invariants[0].name == "exit code zero"
    data = json.loads(path.read_text())
    data["schema_version"] = 99
    path.write_text(json.dumps(data))
    with pytest.raises(EvidenceError, match="schema version"):
        read_receipts(path)
