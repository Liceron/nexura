"""P28 step 2: the ``ServiceExtension`` seam and the opt-in ``nexura.extensions`` loader."""

from __future__ import annotations

import importlib.metadata
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from nexura_runtime.service.api import app as app_module
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.builtin_extensions import (
    BUILTIN_EXTENSION_NAMES,
    builtin_extensions,
)
from nexura_runtime.service.api.descriptors import BUILTIN_CAPABILITIES
from nexura_runtime.service.extensions import (
    EXTENSION_ENTRY_POINT_GROUP,
    ExtensionContext,
    ExtensionError,
    ExtensionLoadError,
    ServiceExtension,
    load_entry_point_extensions,
)

GROUP = EXTENSION_ENTRY_POINT_GROUP


class RecordingExtension:
    """Mounts ``GET /api/v1/<name>/ping`` and records the order it was configured in."""

    def __init__(
        self,
        name: str,
        log: list[str] | None = None,
        *,
        capabilities: dict[str, bool] | None = None,
        health: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.log = log if log is not None else []
        self.capabilities = capabilities or {}
        self.health = health or {}

    def configure(self, context: ExtensionContext) -> None:
        self.log.append(self.name)
        router = APIRouter(prefix=f"/{self.name}")

        @router.get("/ping")
        async def ping() -> dict[str, str]:
            return {"extension": self.name}

        context.api_v1.include_router(router)
        if self.capabilities:
            context.add_capabilities(self.capabilities)
        if self.health:
            context.add_health_fields(self.health)


class BrokenExtension:
    name = "broken"

    def configure(self, context: ExtensionContext) -> None:
        raise RuntimeError("boom")


def _db(tmp_path: Path, name: str = "svc.sqlite3") -> str:
    return str(tmp_path / name)


def _ep(name: str, value: str) -> importlib.metadata.EntryPoint:
    return importlib.metadata.EntryPoint(name=name, value=value, group=GROUP)


@pytest.fixture
def fake_entry_points(monkeypatch: pytest.MonkeyPatch) -> list[importlib.metadata.EntryPoint]:
    """Replace installed entry points with a controlled list."""
    eps: list[importlib.metadata.EntryPoint] = []

    def entry_points(*, group: str) -> list[importlib.metadata.EntryPoint]:
        return [ep for ep in eps if ep.group == group]

    monkeypatch.setattr(importlib.metadata, "entry_points", entry_points)
    return eps


@pytest.fixture
def plugin_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A throwaway importable module holding extensions for entry points to name."""
    name = f"p28_plugin_{tmp_path.name.replace('-', '_')}"
    (tmp_path / f"{name}.py").write_text(
        textwrap.dedent(
            """
            LOG = []

            class Ext:
                def __init__(self, name="ep-class"):
                    self.name = name

                def configure(self, context):
                    LOG.append(self.name)

            INSTANCE = Ext("ep-instance")

            def factory():
                return Ext("ep-factory")

            def not_an_extension():
                return 42
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / f"{name}_broken.py").write_text("raise ImportError('missing dep')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield name
    sys.modules.pop(name, None)
    sys.modules.pop(f"{name}_broken", None)


def test_extensions_implement_the_protocol() -> None:
    for ext in [*builtin_extensions(), RecordingExtension("x"), BrokenExtension()]:
        assert isinstance(ext, ServiceExtension)
    # Publishing (EDITIONS_PLAN step 4) and organisations (step 5) moved to the closed edition:
    # no in-tree extension is left. The positive checks are in enterprise/tests.
    assert frozenset() == BUILTIN_EXTENSION_NAMES
    assert builtin_extensions() == []


def test_no_in_tree_extension_and_closed_routes_are_absent(
    tmp_path: Path,
) -> None:
    app = create_api_app(db_path=_db(tmp_path))
    assert app.state.extensions.configured == []
    assert not hasattr(app.state, "publication_store")
    assert not hasattr(app.state, "org_repo")
    with TestClient(app) as client:
        assert client.get("/api/v1/published").status_code == 404
        assert client.get("/api/v1/organisations").status_code == 404
        assert client.get("/api/v1/secrets").status_code == 404
        # The open-source credentials API is still served.
        assert client.get("/api/v1/credentials").status_code == 200


def test_extensions_run_in_order_after_the_built_ins(tmp_path: Path) -> None:
    log: list[str] = []
    app = create_api_app(
        db_path=_db(tmp_path),
        extensions=[RecordingExtension("beta", log), RecordingExtension("alpha", log)],
    )
    assert log == ["beta", "alpha"]
    assert app.state.extensions.configured == [
        "beta",
        "alpha",
    ]
    with TestClient(app) as client:
        assert client.get("/api/v1/beta/ping").json() == {"extension": "beta"}
        assert client.get("/api/v1/alpha/ping").json() == {"extension": "alpha"}


def test_capabilities_from_extensions(tmp_path: Path) -> None:
    app = create_api_app(
        db_path=_db(tmp_path),
        extensions=[RecordingExtension("caps", capabilities={"caps_feature": True})],
    )
    with TestClient(app) as client:
        flags = client.get("/api/v1/capabilities").json()["capabilities"]
    assert flags == {**BUILTIN_CAPABILITIES, "caps_feature": True}


def test_capabilities_unchanged_without_extensions(tmp_path: Path) -> None:
    with TestClient(create_api_app(db_path=_db(tmp_path))) as client:
        assert client.get("/api/v1/capabilities").json()["capabilities"] == BUILTIN_CAPABILITIES


@pytest.mark.parametrize(
    "flags",
    [{"modules": False}, {"bad": "yes"}, {"": True}],
    ids=["redefines-built-in", "not-bool", "empty-name"],
)
def test_invalid_capabilities_fail_startup(tmp_path: Path, flags: dict[str, Any]) -> None:
    with pytest.raises(ExtensionError):
        create_api_app(
            db_path=_db(tmp_path), extensions=[RecordingExtension("caps", capabilities=flags)]
        )


def test_two_extensions_cannot_claim_one_capability(tmp_path: Path) -> None:
    with pytest.raises(ExtensionError, match="shared"):
        create_api_app(
            db_path=_db(tmp_path),
            extensions=[
                RecordingExtension("one", capabilities={"shared": True}),
                RecordingExtension("two", capabilities={"shared": True}),
            ],
        )


def test_readyz_unchanged_without_health_fields(tmp_path: Path) -> None:
    with TestClient(create_api_app(db_path=_db(tmp_path))) as client:
        assert set(client.get("/readyz").json()) == {"status", "timestamp", "probes"}


def test_health_fields_must_be_json(tmp_path: Path) -> None:
    with pytest.raises(ExtensionError, match="JSON"):
        create_api_app(
            db_path=_db(tmp_path), extensions=[RecordingExtension("h", health={"x": object()})]
        )


@pytest.mark.parametrize(
    ("extensions", "message"),
    [
        ([RecordingExtension("dup"), RecordingExtension("dup")], "dup"),
        ([RecordingExtension("nexura.builtin")], "nexura.builtin"),
        ([RecordingExtension("")], "non-empty"),
        ([object()], "not a ServiceExtension"),
    ],
    ids=["duplicate", "built-in-name", "empty-name", "wrong-type"],
)
def test_invalid_extensions_fail_before_anything_is_created(
    tmp_path: Path, extensions: list[Any], message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No extension ships in-tree since EDITIONS_PLAN step 5, so reserve a name the way an
    # in-tree extension would, to keep checking that built-in names cannot be reused.
    monkeypatch.setattr(app_module, "BUILTIN_EXTENSION_NAMES", frozenset({"nexura.builtin"}))
    db = tmp_path / "never.sqlite3"
    with pytest.raises(ExtensionError, match=message):
        create_api_app(db_path=str(db), extensions=extensions)
    assert not db.exists()


def test_broken_extension_fails_startup_loudly(tmp_path: Path) -> None:
    log: list[str] = []
    with pytest.raises(RuntimeError, match="boom") as info:
        create_api_app(
            db_path=_db(tmp_path),
            extensions=[BrokenExtension(), RecordingExtension("after", log)],
        )
    assert "while configuring Nexura service extension 'broken'" in info.value.__notes__
    assert log == []  # nothing after the broken extension was configured


def test_entry_points_are_not_read_unless_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(**_: Any) -> Any:
        raise AssertionError("entry points must not be read")

    monkeypatch.setattr(importlib.metadata, "entry_points", explode)
    assert load_entry_point_extensions([]) == []
    app = create_api_app(db_path=_db(tmp_path))
    assert app.state.extensions.configured == []


def test_installed_but_not_enabled_extension_is_ignored(
    tmp_path: Path, fake_entry_points: list[Any], plugin_module: str
) -> None:
    fake_entry_points.append(_ep("ep-class", f"{plugin_module}:Ext"))
    app = create_api_app(db_path=_db(tmp_path))
    assert "ep-class" not in app.state.extensions.configured
    assert plugin_module not in sys.modules


def test_enabled_entry_points_load_in_the_enabled_order(
    tmp_path: Path, fake_entry_points: list[Any], plugin_module: str
) -> None:
    fake_entry_points.extend(
        [
            _ep("ep-class", f"{plugin_module}:Ext"),
            _ep("ep-instance", f"{plugin_module}:INSTANCE"),
            _ep("ep-factory", f"{plugin_module}:factory"),
        ]
    )
    app = create_api_app(
        db_path=_db(tmp_path),
        extensions=[RecordingExtension("explicit")],
        enabled_extensions=["ep-factory", "ep-instance", "ep-class"],
    )
    assert app.state.extensions.configured == [
        "explicit",
        "ep-factory",
        "ep-instance",
        "ep-class",
    ]
    assert sys.modules[plugin_module].LOG == ["ep-factory", "ep-instance", "ep-class"]


@pytest.mark.parametrize(
    ("entry", "enabled", "message"),
    [
        (None, ["missing"], "no installed package"),
        (("bad", "{mod}_broken:Ext"), ["bad"], "failed to import"),
        (("bad", "{mod}:not_an_extension"), ["bad"], "not a ServiceExtension"),
        (("bad", "{mod}:Ext"), ["bad", "bad"], "more than once"),
        (None, [""], "non-empty"),
    ],
    ids=["unknown", "import-error", "wrong-type", "enabled-twice", "empty-name"],
)
def test_broken_entry_point_fails_startup_before_anything_is_created(
    tmp_path: Path,
    fake_entry_points: list[Any],
    plugin_module: str,
    entry: tuple[str, str] | None,
    enabled: list[str],
    message: str,
) -> None:
    if entry is not None:
        fake_entry_points.append(_ep(entry[0], entry[1].format(mod=plugin_module)))
    db = tmp_path / "never.sqlite3"
    with pytest.raises(ExtensionLoadError, match=message):
        create_api_app(db_path=str(db), enabled_extensions=enabled)
    assert not db.exists()


def test_ambiguous_entry_point_name_is_refused(
    fake_entry_points: list[Any], plugin_module: str
) -> None:
    fake_entry_points.extend(
        [_ep("twice", f"{plugin_module}:Ext"), _ep("twice", f"{plugin_module}:factory")]
    )
    with pytest.raises(ExtensionLoadError, match="ambiguous"):
        load_entry_point_extensions(["twice"])


def test_entry_point_and_explicit_extension_cannot_share_a_name(
    tmp_path: Path, fake_entry_points: list[Any], plugin_module: str
) -> None:
    fake_entry_points.append(_ep("ep-instance", f"{plugin_module}:INSTANCE"))
    with pytest.raises(ExtensionError, match="ep-instance"):
        create_api_app(
            db_path=_db(tmp_path),
            extensions=[RecordingExtension("ep-instance")],
            enabled_extensions=["ep-instance"],
        )


def test_extension_routes_are_behind_a_test_authenticating_provider(tmp_path: Path) -> None:
    from _local_env import TestAuthProvider

    app = create_api_app(
        db_path=_db(tmp_path),
        profile="authenticated-shared-service",
        auth_provider=TestAuthProvider(),
        extensions=[RecordingExtension("guarded")],
    )
    with TestClient(app) as client:
        assert client.get("/api/v1/guarded/ping").status_code == 401
        ok = client.get("/api/v1/guarded/ping", headers={"x-test-user": "local-owner"})
        assert ok.status_code == 200 and ok.json() == {"extension": "guarded"}


def test_extension_health_fields_follow_test_provider_operator_decision(tmp_path: Path) -> None:
    from _local_env import TestAuthProvider

    app = create_api_app(
        db_path=_db(tmp_path),
        profile="authenticated-shared-service",
        auth_provider=TestAuthProvider(),
        extensions=[RecordingExtension("probe", health={"queue_depth": 0})],
    )
    with TestClient(app) as client:
        assert "extensions" not in client.get("/readyz").json()
        assert (
            "extensions" not in client.get("/readyz", headers={"x-test-user": "local-owner"}).json()
        )
        assert client.get(
            "/readyz", headers={"x-test-user": "local-owner", "x-test-operator": "yes"}
        ).json()["extensions"] == {"probe": {"queue_depth": 0}}
        assert "extensions" not in client.get("/healthz").json()
