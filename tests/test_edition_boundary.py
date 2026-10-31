# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""The release gate's edition-boundary step: report since P28 step 3, required since step 9."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType

from nexura_runtime.release.evidence import Invariant, Receipt, summarize, validate_receipt

ROOT = Path(__file__).resolve().parents[1]


def _load_gate() -> ModuleType:
    path = ROOT / "experiments" / "release-gate" / "run_gate.py"
    spec = importlib.util.spec_from_file_location("run_gate_boundary", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve types via sys.modules
    spec.loader.exec_module(module)
    return module


gate = _load_gate()

LEAKY_WHEEL = [
    "nexura_runtime/__init__.py",
    "nexura_runtime/publishing/gateway.py",
    "nexura_enterprise/__init__.py",
    "nexura_runtime-0.4.0.dist-info/METADATA",
]


def _fake_wheel(path: Path, members: list[str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name in members:
            zf.writestr(name, "x = 1\n")
    return path


def _fake_sdist(path: Path, members: list[str]) -> Path:
    with tarfile.open(path, "w:gz") as tf:
        for name in members:
            data = b"x = 1\n"
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


def test_fake_wheel_listing_with_nexura_enterprise_is_flagged() -> None:
    assert gate.enterprise_members(LEAKY_WHEEL) == ["nexura_enterprise/__init__.py"]
    report = gate.build_boundary_report({"leaky.whl": LEAKY_WHEEL}, [], [], "test")
    assert report["summary"]["enterprise_members"] == 1
    assert report["artifacts"][0]["enterprise_members"] == ["nexura_enterprise/__init__.py"]
    assert "src/nexura_runtime/publishing" in report["artifacts"][0]["closed_modules_shipped"]
    assert "edition-boundary (enforced): 1 artifacts, 1 enterprise members" in (
        gate.boundary_summary_line(report)
    )


def test_enterprise_path_segments_are_flagged_but_lookalikes_are_not() -> None:
    members = [
        "nexura_runtime-0.4.0/enterprise/LICENSE",
        "nexura-enterprise-0.4.0/PKG-INFO",
        "nexura_runtime/service/extensions.py",
        "docs/enterprise-edition.md",
        "nexura_runtime/notenterprise/x.py",
    ]
    assert gate.enterprise_members(members) == [
        "nexura-enterprise-0.4.0/PKG-INFO",
        "nexura_runtime-0.4.0/enterprise/LICENSE",
    ]
    assert gate.enterprise_members(["nexura_runtime/__init__.py"]) == []


def test_real_archives_are_listed_and_flagged(tmp_path: Path) -> None:
    wheel = _fake_wheel(tmp_path / "nexura_runtime-0.4.0-py3-none-any.whl", LEAKY_WHEEL)
    sdist = _fake_sdist(
        tmp_path / "nexura_runtime-0.4.0.tar.gz",
        [
            # Step 5 split `secrets/`: the local encrypted store stays OSS, the routes moved.
            "nexura_runtime-0.4.0/src/nexura_runtime/secrets/store.py",
            "nexura_runtime-0.4.0/src/nexura_runtime/secrets/api.py",
            "nexura_runtime-0.4.0/enterprise/src/nexura_enterprise/__init__.py",
            "nexura_runtime-0.4.0/README.md",
        ],
    )
    assert gate.archive_members(wheel) == LEAKY_WHEEL
    artifacts = {p.name: gate.archive_members(p) for p in (wheel, sdist)}
    report = gate.build_boundary_report(artifacts, [], [], "test")
    rows = {r["name"]: r for r in report["artifacts"]}
    assert rows[sdist.name]["enterprise_members"] == [
        "nexura_runtime-0.4.0/enterprise/src/nexura_enterprise/__init__.py"
    ]
    assert rows[sdist.name]["closed_modules_shipped"] == ["src/nexura_runtime/secrets/api.py"]
    assert report["summary"]["enterprise_members"] == 2


def test_closed_modules_in_members_matches_wheel_and_sdist_spellings() -> None:
    wheel = ["nexura_runtime/service/auth/tokens.py", "nexura_runtime/publishing_notes.py"]
    assert gate.closed_modules_in_members(wheel) == ["src/nexura_runtime/service/auth/tokens.py"]
    sdist = ["pkg-0.4.0/tests/test_publish.py", "pkg-0.4.0/src/nexura_runtime/workflow.py"]
    assert gate.closed_modules_in_members(sdist, sdist=True) == ["tests/test_publish.py"]
    # Split modules are checked by symbol in the tree, never by artifact path.
    assert gate.closed_modules_in_members(["nexura_runtime/service/api/middleware.py"]) == []


def test_python_enterprise_imports_are_detected() -> None:
    text = "\n".join(
        [
            "from nexura_enterprise.auth import SharedAuthProvider",
            "import os, nexura_enterprise",
            "mod = importlib.import_module('nexura_enterprise.app')",
            "# nexura_enterprise is the closed edition",
            'MESSAGE = "requires the Nexura enterprise edition"',
            "import nexura_enterprise_like_name_but_not",
        ]
    )
    assert [n for n, _ in gate.enterprise_imports_in_text(text, ".py")] == [1, 2, 3]


def test_typescript_enterprise_imports_are_detected() -> None:
    text = "\n".join(
        [
            "import { x } from '../../enterprise/studio-web/main';",
            "const m = await import('nexura-enterprise/login');",
            "const r = require('../enterprise');",
            "import { Badge } from './enterpriseBadge';",
            "const label = 'enterprise';",
        ]
    )
    assert [n for n, _ in gate.enterprise_imports_in_text(text, ".tsx")] == [1, 2, 3]


def test_static_scan_finds_a_planted_import(tmp_path: Path) -> None:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "leak.py").write_text("from nexura_enterprise import app\n")
    (tmp_path / "src" / "pkg" / "clean.py").write_text("import json\n")
    (tmp_path / "studio-web" / "src").mkdir(parents=True)
    (tmp_path / "studio-web" / "src" / "a.ts").write_text("import '../../enterprise/x';\n")
    (tmp_path / "studio-web" / "src" / "node_modules").mkdir()
    (tmp_path / "studio-web" / "src" / "node_modules" / "b.ts").write_text(
        "import '../enterprise/y';\n"
    )
    hits = gate.scan_source_imports(tmp_path)
    assert [(h["file"], h["line"]) for h in hits] == [
        ("src/pkg/leak.py", 1),
        ("studio-web/src/a.ts", 1),
    ]


def test_oss_sources_do_not_import_enterprise() -> None:
    assert gate.scan_source_imports(ROOT) == []


def test_closed_modules_status_reports_dirs_split_symbols_and_absence(tmp_path: Path) -> None:
    (tmp_path / "pub").mkdir()
    (tmp_path / "pub" / "selfhost.py").write_text("")
    (tmp_path / "mw.py").write_text("def authenticate_shared(): ...\n")
    closed = (
        gate.ClosedModule("pub", 4),
        gate.ClosedModule("mw.py", 7, "split", ("authenticate_shared", "extract_credentials")),
        gate.ClosedModule("gone.py", 7),
        gate.ClosedModule("moved.py", 7, "split", ("ApiKey",)),
    )
    rows = {r["path"]: r for r in gate.closed_modules_status(tmp_path, closed)}
    assert rows["pub"]["present"] is True and rows["pub"]["files"] == ["selfhost.py"]
    assert rows["mw.py"]["present"] is True
    assert rows["mw.py"]["symbols_remaining"] == ["authenticate_shared"]
    assert rows["gone.py"]["present"] is False
    assert rows["moved.py"]["present"] is False


def test_maintained_list_covers_the_plan_examples_steps_4_to_7_moved_later_steps_in_oss() -> None:
    paths = {m.path for m in gate.CLOSED_MODULES}
    for expected in (
        "src/nexura_runtime/publishing",
        "src/nexura_runtime/service/api/publishing.py",
        "src/nexura_runtime/integrations/langflow/server.py",
        "src/nexura_runtime/integrations/langflow/events.py",
        "src/nexura_runtime/integrations/nexura_v2/auth.py",
        "src/nexura_runtime/integrations/nexura_v2/policy.py",
        "src/nexura_runtime/integrations/nexura_v2/records.py",
        "src/nexura_runtime/integrations/nexura_v2/shadow.py",
        "tests/integrations/nexura_v2",
        "tests/integrations/langflow/test_v2_api.py",
        "tests/integrations/langflow/test_v7_per_caller.py",
        "docs/migrating-from-langflow.md",
        "src/nexura_runtime/service/auth/tokens.py",
        "src/nexura_runtime/secrets/api.py",
        "src/nexura_runtime/secrets/store.py",
        "src/nexura_runtime/service/api/organisations.py",
        "src/nexura_runtime/service/workspaces/organisations.py",
        "studio-web/src/features/organisation",
        "studio-web/tests/e2e/organisation.spec.ts",
        "docs/organisations-and-secrets.md",
        "tests/test_orgs_and_secrets.py",
        # Step 7 (plan 1.1, 1.2, 1.4, 1.5 and 3.2).
        "src/nexura_runtime/service/auth/api_keys.py",
        "src/nexura_runtime/service/auth/resources.py",
        "src/nexura_runtime/service/auth/models.py",
        "src/nexura_runtime/service/auth/profile.py",
        "src/nexura_runtime/service/module.py",
        "src/nexura_runtime/service/workspaces/repository.py",
        "src/nexura_runtime/service/api/middleware.py",
        "src/nexura_runtime/service/api/auth_providers.py",
        "src/nexura_runtime/service/api/workspaces.py",
        "clients/typescript/src/client.ts",
        "examples/backend/b24.py",
        "tests/backend/b24/test_identity_workspaces.py",
        "tests/security/b28/test_resource_scoped_keys.py",
        "tests/backend/test_security_b7_auth_app.py",
        "tests/backend/test_security_b8_auth_app.py",
        "tests/security/test_secure_defaults.py",
    ):
        assert expected in paths
    # A file may be split in more than one step (builtin_extensions.py, client.py, the Studio
    # dictionaries), but never listed twice for the same step.
    keys = [(m.path, m.step) for m in gate.CLOSED_MODULES]
    assert len(set(keys)) == len(keys)
    assert gate.MOVED_THROUGH_STEP == 8
    # Founder decision (question 7): the single-user local Docker setup stays OSS.
    assert "deployment" not in paths and (ROOT / "deployment" / "Dockerfile").is_file()
    # Founder decision (question 3): the local encrypted secrets file stays OSS, so `secrets/`
    # is split, not moved whole.
    assert "src/nexura_runtime/secrets" not in paths
    for kept in ("__init__.py", "store.py", "keys.py", "errors.py"):
        assert (ROOT / "src" / "nexura_runtime" / "secrets" / kept).is_file(), kept
    # Founder decision (question 5): the Langflow importer, the export back to Langflow JSON
    # and the trace exporter stay OSS, so `nexura_v2/` is split and p01.py stays.
    assert "src/nexura_runtime/integrations/nexura_v2" not in paths
    assert "examples/sdk_integration/p01.py" not in paths
    for kept in (
        "src/nexura_runtime/integrations/nexura_v2/exporter.py",
        "src/nexura_runtime/integrations/langflow/build.py",
        "src/nexura_runtime/integrations/langflow/export.py",
        "src/nexura_runtime/integrations/langflow/flow.py",
        "examples/sdk_integration/p01.py",
        "tests/integrations/langflow/test_langflow_load.py",
        "tests/integrations/langflow/test_v5_export.py",
        "tests/sdk_integration/p01/test_exporter.py",
    ):
        assert (ROOT / kept).is_file(), kept
    # Plan section 3: safety is not a paid feature. The safety controls stay OSS, and of
    # tests/security only the resource-scoped key file moved whole.
    for kept in (
        "src/nexura_runtime/service/api/bind_guard.py",
        "src/nexura_runtime/service/api/middleware.py",
        "src/nexura_runtime/service/api/auth_providers.py",
        "src/nexura_runtime/service/auth/permissions.py",
        "src/nexura_runtime/service/workspaces/scoping.py",
        "tests/security/test_secure_defaults.py",
        "tests/security/b28/test_cancel_paths.py",
        "tests/security/p27_review/test_p27_review_sandbox_fixes.py",
        "tests/test_p28s7_auth_moved.py",
    ):
        assert (ROOT / kept).is_file(), kept
    whole_security_moves = {
        m.path
        for m in gate.CLOSED_MODULES
        if m.path.startswith("tests/security") and m.kind == "move"
    }
    assert whole_security_moves == {
        "tests/security/b28/test_resource_scoped_keys.py",
    }

    rows = gate.closed_modules_status(ROOT)
    moved_steps = (4, 5, 6, 7, 8)
    done = {step: [r for r in rows if r["step"] == step] for step in moved_steps}
    assert len(done[4]) >= 20
    assert len(done[5]) >= 15
    assert len(done[6]) >= 23
    assert len(done[7]) >= 60
    assert len(done[8]) >= 1
    for step in moved_steps:
        for row in done[step]:
            # P28 steps 4 to 8 moved every listed module (and split symbol) out of the OSS tree.
            assert row["present"] is False, row
            assert row["status"] == "moved" and row["expected"] == "moved", row
            if row["kind"] == "move":
                assert row.get("dest", "").startswith("enterprise/"), row
                if (ROOT / "enterprise").is_dir():
                    assert row["dest_present"] is True, row
    for row in rows:
        if row["step"] > 8:
            assert row["present"] is True, row  # later steps have not moved anything yet
            assert row["status"] == "in_oss" and row["expected"] == "in_oss", row

    report = gate.build_boundary_report({}, [], rows, "test")
    summary = report["summary"]
    assert summary["closed_modules_off_plan"] == []
    assert summary["closed_modules_moved_by_step"] == {
        "4": len(done[4]),
        "5": len(done[5]),
        "6": len(done[6]),
        "7": len(done[7]),
        "8": len(done[8]),
    }
    assert "4" not in summary["closed_modules_still_in_oss_by_step"]
    assert "5" not in summary["closed_modules_still_in_oss_by_step"]
    assert "6" not in summary["closed_modules_still_in_oss_by_step"]
    assert "7" not in summary["closed_modules_still_in_oss_by_step"]
    # Every listed closed module has left the OSS tree (P28 step 8 removed the last one).
    assert summary["closed_modules_still_in_oss"] == 0
    moved_total = sum(len(done[s]) for s in moved_steps)
    assert f"{moved_total} moved" in gate.boundary_summary_line(report)


def test_off_plan_modules_are_reported(tmp_path: Path) -> None:
    (tmp_path / "late.py").write_text("")
    closed = (
        gate.ClosedModule("late.py", 3),  # its step is done but it is still in OSS
        gate.ClosedModule("early.py", 9),  # moved before its step
        gate.ClosedModule("ok.py", 9),
    )
    (tmp_path / "ok.py").write_text("")
    rows = gate.closed_modules_status(tmp_path, closed, moved_through_step=4)
    report = gate.build_boundary_report({}, [], rows, "test")
    assert report["summary"]["closed_modules_off_plan"] == ["late.py", "early.py"]
    assert "2 off plan" in gate.boundary_summary_line(report)


def test_leaky_build_fails_the_step_and_reuses_the_gate_build(tmp_path: Path) -> None:
    # P28 step 9: a wheel with closed-edition code fails the step (it was report-only).
    dist = tmp_path / "dist"
    dist.mkdir()
    _fake_wheel(dist / "nexura_runtime-0.4.0-py3-none-any.whl", LEAKY_WHEEL)
    out = tmp_path / "evidence" / "edition_boundary.json"
    receipt = gate.check_edition_boundary(tmp_path, out_file=out)
    assert receipt.status == "failed"
    assert validate_receipt(receipt) == []
    assert receipt.measurements["mode"] == "enforced"
    assert receipt.measurements["enterprise_members"] == 1
    failed = {i.name for i in receipt.invariants if not i.held}
    assert failed == {
        "no enterprise member in OSS artifacts",
        "no closed module shipped in OSS artifacts",
    }
    assert "no enterprise member in OSS artifacts" in receipt.reason
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["artifact_source"].startswith("reused the gate build")
    assert report["artifacts"][0]["enterprise_members"] == ["nexura_enterprise/__init__.py"]
    # The tree itself holds no listed closed module.
    assert report["summary"]["closed_modules_still_in_oss"] == 0
    assert not (tmp_path / "boundary-dist").exists()  # nothing was rebuilt


def test_clean_build_passes_the_step(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    _fake_wheel(dist / "nexura_runtime-0.4.0-py3-none-any.whl", ["nexura_runtime/__init__.py"])
    receipt = gate.check_edition_boundary(tmp_path, out_file=tmp_path / "b.json")
    assert receipt.status == "passed", receipt.reason
    assert all(i.held for i in receipt.invariants)


def test_boundary_failure_blocks_readiness() -> None:
    assert "edition-boundary" in gate.REQUIRED
    ok = Receipt("only", "passed", "true", 0, 0.0, (Invariant("ran", True),))
    boundary = Receipt(
        "edition-boundary", "failed", "list artifacts", 1, 0.0, (Invariant("x", False),), {}, "b"
    )
    assert summarize([ok, boundary], ["only", "edition-boundary"], {}).ready is False


def test_step7_boundary_accepts_local_equivalents_and_rejects_closed_fixtures(
    tmp_path: Path,
) -> None:
    rows = [
        m
        for m in gate.CLOSED_MODULES
        if m.step == 7 and m.path.startswith("tests/") and m.kind == "split"
    ]
    assert len(rows) >= 40
    for module in rows:
        path = tmp_path / module.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("from _local_env import local_headers\n")
        assert gate.closed_modules_status(tmp_path, [module])[0]["present"] is False
        for marker in module.symbols:
            path.write_text(marker)
            assert gate.closed_modules_status(tmp_path, [module])[0]["present"] is True


# --- PKG-03: closed-edition client route strings in OSS artifacts -------------------------------

CLOSED_SEGMENTS = ["/members", "/my-membership", "/organisations", "/published", "/secrets"]


def _write_tgz(path: Path, files: dict[str, str]) -> Path:
    with tarfile.open(path, "w:gz") as tf:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


def test_closed_route_strings_are_found_but_module_paths_and_words_are_not() -> None:
    assert sorted(f"/{s}" for s in gate.CLOSED_CLIENT_ROUTE_SEGMENTS) == CLOSED_SEGMENTS
    leaky = {
        'request("GET", "/organisations")': ["/organisations"],
        "request('POST', `/published/${encodeURIComponent(id)}/promote`)": ["/published"],
        'f"/workspaces/{_safe_quote(ws)}/members"': ["/members"],
        "`${t(r)}/my-membership`": ["/my-membership"],
        "the hosted ``/api/v1/secrets`` routes": ["/secrets"],
        '("GET", "/api/v1/organisations/{org_id}/members")': ["/members", "/organisations"],
    }
    for text, expected in leaky.items():
        assert gate.closed_route_strings_in_text(text) == expected, text
    for clean in (
        "nexura_runtime/secrets/store.py",
        "from nexura_runtime.secrets import EncryptedSecretStore",
        "docs/local-secrets.md",
        "import { getMyMembership } from '../../api/membership';",
        "team/members and the published workflow",
        "/publishedAt /members-only",
    ):
        assert gate.closed_route_strings_in_text(clean) == [], clean


def test_leaky_wheel_text_fails_the_step(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / "nexura_runtime-0.4.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as zf:
        zf.writestr("nexura_runtime/__init__.py", "x = 1\n")
        zf.writestr(
            "nexura_runtime/service/api/client.py",
            'return await self._root._request("GET", "/organisations")\n',
        )
        zf.writestr("nexura_runtime/secrets/store.py", '"""nexura_runtime/secrets/store.py"""\n')
        zf.writestr("nexura_runtime/static/font.woff2", b"\x00/organisations")
    receipt = gate.check_edition_boundary(tmp_path, out_file=tmp_path / "b.json")
    assert receipt.status == "failed"
    failed = {i.name for i in receipt.invariants if not i.held}
    assert failed == {"no closed client route string in OSS artifacts"}
    assert "nexura_runtime/service/api/client.py ['/organisations']" in receipt.reason
    report = json.loads((tmp_path / "b.json").read_text(encoding="utf-8"))
    assert report["closed_client_route_strings"] == {
        wheel.name: {"nexura_runtime/service/api/client.py": ["/organisations"]}
    }
    assert report["summary"]["closed_client_route_strings_in_artifacts"] == 1


def test_leaky_npm_tarball_and_studio_bundle_fail_the_step(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    _fake_wheel(dist / "nexura_runtime-0.4.0-py3-none-any.whl", ["nexura_runtime/__init__.py"])
    tarball = _write_tgz(
        tmp_path / "liceron-nexura-sdk-0.4.0.tgz",
        {
            "package/dist/client.js": 'return this.client.request("GET", "/published");',
            "package/README.md": "# client",
        },
    )
    studio = tmp_path / "studio-dist"
    (studio / "assets").mkdir(parents=True)
    (studio / "assets" / "index-abc.js").write_text("fetch(`${b}/workspaces/${w}/members`)")
    (studio / "assets" / "index-abc.js.map").write_text('{"sourcesContent": ["\\"/secrets\\""]}')
    receipt = gate.check_edition_boundary(
        tmp_path,
        out_file=tmp_path / "b.json",
        npm_tarball=tarball,
        studio_dist=studio,
        require_js_artifacts=True,
    )
    assert receipt.status == "failed"
    failed = {i.name for i in receipt.invariants if not i.held}
    assert failed == {"no closed client route string in OSS artifacts"}
    report = json.loads((tmp_path / "b.json").read_text(encoding="utf-8"))
    assert report["closed_client_route_strings"] == {
        "nexura_runtime-0.4.0-py3-none-any.whl": {},
        tarball.name: {"package/dist/client.js": ["/published"]},
        "studio-web/dist": {
            "assets/index-abc.js": ["/members"],
            "assets/index-abc.js.map": ["/secrets"],
        },
    }
    assert report["summary"]["route_string_artifacts_scanned"] == 3


def test_clean_npm_tarball_and_studio_bundle_pass_and_missing_ones_fail(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    _fake_wheel(dist / "nexura_runtime-0.4.0-py3-none-any.whl", ["nexura_runtime/__init__.py"])
    tarball = _write_tgz(
        tmp_path / "liceron-nexura-sdk-0.4.0.tgz",
        {"package/dist/client.js": 'request("GET", "/workflows"); // ../api/membership'},
    )
    studio = tmp_path / "studio-dist"
    (studio / "assets").mkdir(parents=True)
    (studio / "assets" / "index.js").write_text("import('./api/membership');")
    ok = gate.check_edition_boundary(
        tmp_path,
        out_file=tmp_path / "ok.json",
        npm_tarball=tarball,
        studio_dist=studio,
        require_js_artifacts=True,
    )
    assert ok.status == "passed", ok.reason
    assert ok.measurements["route_string_artifacts_scanned"] == 3
    # The gate run requires both JS artifacts: a missing one fails the step, it is not skipped.
    missing = gate.check_edition_boundary(
        tmp_path,
        out_file=tmp_path / "missing.json",
        npm_tarball=tmp_path / "absent.tgz",
        studio_dist=tmp_path / "no-studio",
        require_js_artifacts=True,
    )
    assert missing.status == "failed"
    failed = {i.name for i in missing.invariants if not i.held}
    assert failed == {"npm tarball and Studio bundle scanned"}
    assert "npm tarball" in missing.reason and "Studio bundle" in missing.reason


def test_gate_run_scans_the_npm_tarball_and_studio_bundle() -> None:
    import inspect

    main = inspect.getsource(gate.main)
    with_js = inspect.getsource(gate.check_edition_boundary_with_js)
    assert "check_edition_boundary_with_js(Path(tmp))" in main
    # It runs after the TypeScript client and Studio steps that build what it scans.
    assert main.index("check_studio_unit") < main.index("check_edition_boundary_with_js")
    assert "require_js_artifacts=True" in with_js and "pack_ts_client" in with_js
    assert 'ROOT / "studio-web" / "dist"' in with_js


def test_real_oss_artifacts_have_no_closed_client_route_strings(tmp_path: Path) -> None:
    """The real wheels, sdists and npm tarball (built here) and the Studio bundle (when built)."""
    import shutil
    import subprocess

    out = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--all-packages", "--out-dir", str(out)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    archives = sorted([*out.glob("*.whl"), *out.glob("*.tar.gz")])
    # A wheel and an sdist for each of the eleven Python packages.
    assert len(archives) == 22
    assert any(p.name.startswith("nexura_runtime-") and p.suffix == ".whl" for p in archives)
    assert any(p.name.startswith("nexura_guardrails-") and p.suffix == ".whl" for p in archives)
    for archive in archives:
        assert gate.closed_route_strings_in_archive(archive) == {}, archive.name
    tsc = ROOT / "clients" / "typescript" / "node_modules" / ".bin" / "tsc"
    if shutil.which("npm") and tsc.exists():
        tarball, how = gate.pack_ts_client(tmp_path)
        assert tarball is not None, how
        with tarfile.open(tarball) as tf:
            assert "package/dist/client.js" in tf.getnames()
        assert gate.closed_route_strings_in_archive(tarball) == {}
    studio = ROOT / "studio-web" / "dist"
    if (studio / "assets").is_dir():
        assert any(studio.rglob("*.js"))
        assert gate.closed_route_strings_in_dir(studio) == {}
