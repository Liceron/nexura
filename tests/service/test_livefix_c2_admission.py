"""Live pass C2: a run under a monetary cap, operator model prices, recovery and cancel.

Local fakes only: ``PricedFake`` is a labelled ``FakeModel`` that reports a provider model id
the way a real adapter does; no provider is ever called. Runs on both service backends
(``--service-backend=sqlite|postgres``).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from nexura_agents import FakeModel
from nexura_agents.types import ModelResponse, Usage
from nexura_agents.usage.errors import InvalidPricingError

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.model_pricing import (
    MODEL_PRICING_ENV,
    ModelPricingError,
    build_pricing_table,
    parse_model_pricing,
)

API = "/api/v1"
PROVIDER_ID = "gemini-3.5-flash-lite"
ALIAS = "gemini-flash-lite"
PRICE = {
    "input_per_million": "0.10",
    "output_per_million": "0.40",
    "source": "test price list",
    "effective_date": "2026-10-01",
}


class PricedFake(FakeModel):
    """Labelled local fake that reports a provider model id, like a real adapter would."""

    def __init__(self, model_id: str, responses: list[Any] | None = None) -> None:
        super().__init__(
            responses=responses or ["fake answer"],
            repeat_last=True,
            default_usage=Usage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
        )
        self.model_id = model_id


class LostOnceFake(PricedFake):
    """Raises after it was admitted the first time (a call sent whose answer was lost)."""

    def __init__(self, model_id: str) -> None:
        super().__init__(model_id)
        self.attempts = 0

    async def generate(self, *args: Any, **kwargs: Any) -> ModelResponse:
        self.attempts += 1
        if self.attempts == 1:
            raise ConnectionError("connection reset after the request was sent")
        return await super().generate(*args, **kwargs)


def _app(tmp_path: Path, model: Any = None, **kw: Any) -> TestClient:
    model = model or PricedFake(PROVIDER_ID)
    app = create_api_app(
        db_path=tmp_path / "s.db",
        profile="local-owner",
        chat_models={ALIAS: model},
        **kw,
    )
    client = TestClient(app)
    client.model = model  # type: ignore[attr-defined]
    return client


def _agent_workflow(c: TestClient) -> str:
    agent = c.post(f"{API}/agents", json={"name": "helper", "model": ALIAS, "instructions": "x"})
    assert agent.status_code in (200, 201), agent.text
    spec = {
        "spec_version": 1,
        "name": "chat",
        "entry_node": "ask",
        "nodes": {
            "ask": {
                "id": "ask",
                "handler": "nexura.agents.agent",
                "module_id": "nexura.agents.agent",
                "config": {"agent_id": agent.json()["agent_id"]},
            }
        },
        "terminal_nodes": ["ask"],
    }
    wf = c.post(f"{API}/workflows", json={"spec": spec})
    assert wf.status_code == 201, wf.text
    return str(wf.json()["workflow_id"])


def _run(c: TestClient, workflow_id: str, cap: str | None = "1") -> dict[str, Any]:
    body: dict[str, Any] = {"workflow_id": workflow_id, "initial_state": {"input": "hello"}}
    if cap is not None:
        body["budget_config"] = {"max_cost_usd": cap}
    r = c.post(f"{API}/runs", json=body)
    assert r.status_code in (200, 201), r.text
    data: dict[str, Any] = c.get(f"{API}/runs/{r.json()['run_id']}").json()
    return data


def _event_types(run: dict[str, Any]) -> list[str]:
    return [str(e.get("event_type") or e.get("type")) for e in run.get("events", [])]


# ------------------------------------------------------------------ the price list format


def test_price_list_is_parsed_strictly() -> None:
    parsed = parse_model_pricing(json.dumps({PROVIDER_ID: PRICE}))
    entry = parsed[PROVIDER_ID]
    assert entry.input_cost_per_million == Decimal("0.10")
    assert entry.output_cost_per_million == Decimal("0.40")
    assert (entry.source, entry.effective_date) == ("test price list", "2026-10-01")
    # JSON numbers keep their decimal value (parsed as Decimal, never float).
    numbers = parse_model_pricing(
        '{"m": {"input_per_million": 0.1, "output_per_million": 3, "effective_date": "2026-01-02"}}'
    )
    assert numbers["m"].input_cost_per_million == Decimal("0.1")
    assert numbers["m"].source == MODEL_PRICING_ENV
    assert parse_model_pricing(None) == {} and parse_model_pricing("  ") == {}


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("{not json", "not valid JSON"),
        ("[]", "JSON object keyed by model id"),
        (json.dumps({"m": "0.1"}), "must be an object"),
        (json.dumps({"m": {**PRICE, "colour": "red"}}), "unknown keys: colour"),
        (json.dumps({"m": {"input_per_million": "1"}}), "needs output_per_million"),
        (json.dumps({"m": {**PRICE, "input_per_million": "-0.01"}}), "at least 0"),
        (json.dumps({"m": {**PRICE, "output_per_million": "NaN"}}), "at least 0"),
        (json.dumps({"m": {**PRICE, "output_per_million": "cheap"}}), "decimal number"),
        (json.dumps({"m": {**PRICE, "output_per_million": True}}), "decimal number"),
        (json.dumps({"m": {**PRICE, "effective_date": "01/10/2026"}}), "YYYY-MM-DD"),
        (json.dumps({"m": {**PRICE, "effective_date": "2026-02-30"}}), "not a valid date"),
        (json.dumps({"m": {**PRICE, "source": ""}}), "'source' must be text"),
        (json.dumps({"": PRICE}), "model ids of 1 to"),
    ],
)
def test_bad_price_lists_are_refused(value: str, message: str) -> None:
    with pytest.raises(ModelPricingError, match=message):
        parse_model_pricing(value)


def test_table_merges_over_the_standard_table_and_resolves_aliases() -> None:
    table = build_pricing_table({ALIAS: PRICE}, aliases={ALIAS: PROVIDER_ID})
    assert table.get_pricing("gpt-4o") is not None  # built-in prices kept
    for model_id in (ALIAS, PROVIDER_ID):
        price = table.get_pricing(model_id)
        assert price is not None and price.model_id == model_id
        assert price.output_cost_per_million == Decimal("0.40")
    assert "+operator-" in table.version
    # Priced by provider id: the chat model name resolves too.
    by_id = build_pricing_table({PROVIDER_ID: PRICE}, aliases={ALIAS: PROVIDER_ID})
    assert by_id.get_pricing(ALIAS) is not None
    # Two names of one id with different prices are ambiguous.
    other = {**PRICE, "output_per_million": "9"}
    with pytest.raises(ModelPricingError, match="different prices"):
        build_pricing_table({"a": PRICE, "b": other}, aliases={"a": "m", "b": "m"})
    # An operator entry replaces a built-in one of the same id.
    replaced = build_pricing_table({"gpt-4o": PRICE})
    assert replaced.get_pricing("gpt-4o").input_cost_per_million == Decimal("0.10")  # type: ignore[union-attr]
    assert build_pricing_table(None).version == "2026-10-01"


def test_a_bad_price_list_stops_the_service(tmp_path: Path) -> None:
    with pytest.raises(ModelPricingError):
        create_api_app(db_path=tmp_path / "s.db", model_pricing='{"m": {}}')


# ------------------------------------------------------------------ admission before dispatch


def test_unpriced_model_under_a_cap_fails_without_an_effect_claim(tmp_path: Path) -> None:
    with _app(tmp_path) as c:
        run = _run(c, _agent_workflow(c))
        assert run["status"] == "failed", run
        assert run.get("pause_reason") is None and run.get("pending_effect") is None
        assert f"Unknown pricing for model '{PROVIDER_ID}'" in str(run.get("error")), run
        assert "nothing was sent" in str(run.get("error"))
        assert "RUN_PAUSED_EFFECT_UNKNOWN" not in _event_types(run)
        assert c.model.calls == []  # type: ignore[attr-defined]
        effects = c.get(f"{API}/runs/{run['run_id']}/effects").json()["effects"]
        assert [e["status"] for e in effects] in ([], ["resolved"])
        assert all(e.get("resolution") == "not_applied" for e in effects)
        budget = c.get(f"{API}/runs/{run['run_id']}").json()["budget_status"]
        assert Decimal(budget["total_reserved_usd"]) == 0


def test_budget_exceeded_at_admission_fails_without_an_effect_claim(tmp_path: Path) -> None:
    with _app(tmp_path, model_pricing={PROVIDER_ID: PRICE}) as c:
        # Priced, but the first call's reservation does not fit a tiny cap.
        run = _run(c, _agent_workflow(c), cap="0.00000001")
        assert run["status"] == "failed", run
        assert run.get("pending_effect") is None
        assert "nothing was sent" in str(run.get("error")), run
        assert "RUN_PAUSED_EFFECT_UNKNOWN" not in _event_types(run)
        assert c.model.calls == []  # type: ignore[attr-defined]


def test_operator_price_lets_a_capped_run_answer_and_costs_it(tmp_path: Path) -> None:
    with _app(tmp_path, model_pricing=json.dumps({PROVIDER_ID: PRICE})) as c:
        table = c.app.state.pricing_table  # type: ignore[attr-defined]
        assert table.get_pricing(ALIAS) is not None and table.get_pricing(PROVIDER_ID) is not None
        run = _run(c, _agent_workflow(c))
        assert run["status"] == "completed", run
        assert run["current_state"]["output"] == "fake answer"
        budget = run["budget_status"]
        # 100 input tokens at 0.10/M + 20 output tokens at 0.40/M.
        assert Decimal(budget["committed_cost_usd"]) == Decimal("0.000018"), budget
        # Before: the trace cost was empty ("—") for this model.
        trace = c.get(f"{API}/runs/{run['run_id']}/trace").json()
        node = next(s for s in trace["spans"] if s["kind"] == "node")
        assert node["model_id"] == PROVIDER_ID
        assert Decimal(str(node["cost_usd"])) == Decimal("0.000018"), node
        root = next(s for s in trace["spans"] if s["kind"] == "run")
        assert Decimal(str(root["cost_usd"])) == Decimal("0.000018"), root


def test_price_by_chat_model_name_also_admits(tmp_path: Path) -> None:
    with _app(tmp_path, model_pricing={ALIAS: PRICE}) as c:
        run = _run(c, _agent_workflow(c))
        assert run["status"] == "completed", run


def test_a_run_without_a_cap_is_unchanged(tmp_path: Path) -> None:
    with _app(tmp_path) as c:
        run = _run(c, _agent_workflow(c), cap=None)
        assert run["status"] == "completed", run


def test_a_call_sent_and_lost_still_pauses_and_recovers_after_did_not_happen(
    tmp_path: Path,
) -> None:
    """Real ambiguity stays effect_unknown; "did not happen" + Recover runs the step again."""
    with _app(tmp_path, LostOnceFake(PROVIDER_ID), model_pricing={PROVIDER_ID: PRICE}) as c:
        run = _run(c, _agent_workflow(c))
        assert run["status"] == "paused" and run["pause_reason"] == "effect_unknown", run
        effect_id = run["pending_effect"]["effect_id"]
        resolved = c.post(
            f"{API}/runs/{run['run_id']}/effects/{effect_id}/resolve",
            json={"resolution": "not_applied", "reason": "did not happen"},
        )
        assert resolved.status_code == 200, resolved.text
        recovered = c.post(f"{API}/runs/{run['run_id']}/recover", json={})
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["status"] == "completed", recovered.json()
        assert recovered.json()["current_state"]["output"] == "fake answer"


def test_recover_after_did_not_happen_ends_failed_when_admission_refuses(
    tmp_path: Path,
) -> None:
    """The live loop: after "did not happen", Recover re-ran the step, the price was still
    unknown and the run paused again. A refusal before dispatch now fails the run."""
    with _app(tmp_path, LostOnceFake(PROVIDER_ID), model_pricing={PROVIDER_ID: PRICE}) as c:
        run = _run(c, _agent_workflow(c))
        assert run["status"] == "paused", run
        effect_id = run["pending_effect"]["effect_id"]
        c.post(
            f"{API}/runs/{run['run_id']}/effects/{effect_id}/resolve",
            json={"resolution": "not_applied", "reason": "did not happen"},
        ).raise_for_status()
        table = c.app.state.pricing_table  # type: ignore[attr-defined]
        for model_id in (PROVIDER_ID, ALIAS):  # the operator removed the price meanwhile
            table._models.pop(model_id, None)
        assert table.get_pricing(PROVIDER_ID) is None
        recovered = c.post(f"{API}/runs/{run['run_id']}/recover", json={})
        assert recovered.status_code == 200, recovered.text
        body = recovered.json()
        assert body["status"] == "failed", body
        assert body.get("pending_effect") is None
        assert "nothing was sent" in str(body.get("error"))
        again = c.post(f"{API}/runs/{run['run_id']}/recover", json={})
        assert again.status_code == 409 and again.json()["detail"]["code"] == "RUN_ALREADY_TERMINAL"


def test_invalid_pricing_error_is_still_raised_by_the_budget(tmp_path: Path) -> None:
    """The deny-unknown-pricing rule itself is unchanged."""
    from nexura_agents.usage.manager import BudgetManager
    from nexura_agents.usage.models import BudgetConfig

    manager = BudgetManager(pricing_table=build_pricing_table({PROVIDER_ID: PRICE}))
    budget = manager.create_run_budget(
        workspace_id="w", run_id="r", config=BudgetConfig(max_cost_usd=Decimal("1"))
    )
    with pytest.raises(InvalidPricingError):
        budget.admit_provider_call("unpriced-model")
    assert budget.admit_provider_call(PROVIDER_ID)


# ------------------------------------------------------------------ cancel a paused run


def test_a_paused_effect_unknown_run_can_be_cancelled_and_is_audited(tmp_path: Path) -> None:
    with _app(tmp_path, LostOnceFake(PROVIDER_ID), model_pricing={PROVIDER_ID: PRICE}) as c:
        run = _run(c, _agent_workflow(c))
        assert run["status"] == "paused" and run["pause_reason"] == "effect_unknown", run
        effect_id = run["pending_effect"]["effect_id"]
        r = c.post(f"{API}/runs/{run['run_id']}/cancel", json={"reason": "operator gave up"})
        assert r.status_code == 200, r.text
        body = c.get(f"{API}/runs/{run['run_id']}").json()
        assert body["status"] == "cancelled" and body["error"] == "Cancelled: operator gave up"
        assert body.get("pending_effect") is None and body.get("pause_reason") is None
        cancelled = [e for e in body["events"] if e["type"] == "RUN_CANCELLED"]
        assert len(cancelled) == 1
        assert cancelled[0]["data"] == {
            "reason": "operator gave up",
            "previous_status": "paused",
            "actor": "local-owner",
            "unresolved_effect_id": effect_id,
        }
        assert Decimal(body["budget_status"]["total_reserved_usd"]) == 0
        # Cancelling did not decide what the step did; the effect can still be recorded.
        effects = c.get(f"{API}/runs/{run['run_id']}/effects").json()["effects"]
        assert [e["status"] for e in effects] == ["indeterminate"]
        # Finished: no recover, no second cancel.
        again = c.post(f"{API}/runs/{run['run_id']}/cancel", json={})
        assert again.status_code == 409 and again.json()["detail"]["code"] == "RUN_ALREADY_TERMINAL"
        recover = c.post(f"{API}/runs/{run['run_id']}/recover", json={})
        assert recover.status_code == 409


def test_a_run_waiting_for_approval_can_be_cancelled(tmp_path: Path) -> None:
    with _app(tmp_path) as c:
        spec = {
            "spec_version": 1,
            "name": "gate",
            "entry_node": "ask",
            "nodes": {
                "ask": {
                    "id": "ask",
                    "handler": "nexura.agents.agent",
                    "module_id": "nexura.agents.agent",
                    "config": {"agent_id": "missing"},
                    "requires_approval": True,
                }
            },
            "terminal_nodes": ["ask"],
        }
        wf = c.post(f"{API}/workflows", json={"spec": spec})
        assert wf.status_code == 201, wf.text
        r = c.post(f"{API}/runs", json={"workflow_id": wf.json()["workflow_id"]})
        run = c.get(f"{API}/runs/{r.json()['run_id']}").json()
        assert run["status"] == "pending_approval", run
        res = c.post(f"{API}/runs/{run['run_id']}/cancel", json={})
        assert res.status_code == 200, res.text
        body = c.get(f"{API}/runs/{run['run_id']}").json()
        assert body["status"] == "cancelled" and body.get("pending_approval") is None
        event = [e for e in body["events"] if e["type"] == "RUN_CANCELLED"][0]
        assert event["data"]["previous_status"] == "pending_approval"
        decide = c.post(
            f"{API}/runs/{run['run_id']}/decide",
            json={"approval_id": run["pending_approval"]["approval_id"], "decision": "approve"},
        )
        assert decide.status_code == 409


# ------------------------------------------------------------------ review round (repros moved
# from .autopilot-livefix/repros/test_review_pricing.py)

UNPRICED_PROVIDER_ID = "gemini-3.5-flash-lite"
#: A chat model name that is also a built-in price-table id (an old name kept for an
#: upgraded model).
BUILTIN_NAMED_ALIAS = "gemini-1.5-flash"
REVIEW_PRICE = {"input_per_million": "1", "output_per_million": "1", "effective_date": "2026-01-01"}


def test_alias_named_like_a_builtin_model_does_not_price_an_unpriced_provider_model() -> None:
    """Review M1: the deny-unknown-pricing rule must hold: nobody priced the provider id."""
    table = build_pricing_table(None, aliases={BUILTIN_NAMED_ALIAS: UNPRICED_PROVIDER_ID})
    assert table.get_pricing(UNPRICED_PROVIDER_ID) is None, (
        "the unpriced provider model silently inherited the built-in price of the alias name: "
        f"{table.get_pricing(UNPRICED_PROVIDER_ID)}"
    )
    # A built-in provider id does not price a chat model name either.
    other = build_pricing_table(None, aliases={"fast": "gpt-4o"})
    assert other.get_pricing("fast") is None
    # Operator prices still cross the alias, both ways, overriding a built-in entry.
    by_name = build_pricing_table(
        {BUILTIN_NAMED_ALIAS: PRICE}, aliases={BUILTIN_NAMED_ALIAS: PROVIDER_ID}
    )
    assert by_name.get_pricing(PROVIDER_ID).input_cost_per_million == Decimal("0.10")  # type: ignore[union-attr]
    by_id = build_pricing_table({PROVIDER_ID: PRICE}, aliases={BUILTIN_NAMED_ALIAS: PROVIDER_ID})
    assert by_id.get_pricing(BUILTIN_NAMED_ALIAS).input_cost_per_million == Decimal("0.10")  # type: ignore[union-attr]
    # Built-in entries keep their own source.
    assert by_id.get_pricing("gpt-4o").source == "operator_reference_schedule_v1"  # type: ignore[union-attr]


def test_a_name_that_is_also_another_models_provider_id_is_ambiguous() -> None:
    with pytest.raises(ModelPricingError, match="ambiguous"):
        build_pricing_table({"x": PRICE}, aliases={"a": "x", "x": "y"})


def test_capped_run_of_an_unpriced_model_behind_a_builtin_named_alias_is_refused(
    tmp_path: Path,
) -> None:
    model = PricedFake(UNPRICED_PROVIDER_ID)
    app = create_api_app(
        db_path=tmp_path / "s.db",
        profile="local-owner",
        chat_models={BUILTIN_NAMED_ALIAS: model},
    )
    with TestClient(app) as c:
        agent = c.post(
            f"{API}/agents",
            json={"name": "helper", "model": BUILTIN_NAMED_ALIAS, "instructions": "x"},
        )
        assert agent.status_code in (200, 201), agent.text
        spec = {
            "spec_version": 1,
            "name": "chat",
            "entry_node": "ask",
            "nodes": {
                "ask": {
                    "id": "ask",
                    "handler": "nexura.agents.agent",
                    "module_id": "nexura.agents.agent",
                    "config": {"agent_id": agent.json()["agent_id"]},
                }
            },
            "terminal_nodes": ["ask"],
        }
        wf = c.post(f"{API}/workflows", json={"spec": spec})
        assert wf.status_code == 201, wf.text
        run = _run(c, str(wf.json()["workflow_id"]))
        assert run["status"] == "failed", (
            "a run under a monetary cap answered with a model nobody priced (charged at the "
            f"built-in price of '{BUILTIN_NAMED_ALIAS}'): status={run['status']}, "
            f"budget={run.get('budget_status')}"
        )
        assert f"Unknown pricing for model '{UNPRICED_PROVIDER_ID}'" in str(run["error"])
        assert model.calls == []


def test_a_model_listed_twice_is_refused() -> None:
    """Review m2: JSON duplicate keys are last-wins unless refused."""
    text = (
        '{"m": {"input_per_million": "1", "output_per_million": "1", '
        '"effective_date": "2026-01-01"}, '
        '"m": {"input_per_million": "0", "output_per_million": "0", '
        '"effective_date": "2026-01-01"}}'
    )
    with pytest.raises(ModelPricingError, match="twice"):
        parse_model_pricing(text)
    nested = (
        '{"m": {"input_per_million": "1", "input_per_million": "2", '
        '"output_per_million": "1", "effective_date": "2026-01-01"}}'
    )
    with pytest.raises(ModelPricingError, match="twice"):
        parse_model_pricing(nested)


def test_an_absurd_price_is_refused_at_start_not_at_every_capped_run() -> None:
    """Review m3: 1e999999 USD/M passed validation, then every capped admission (and trace
    cost) raised decimal.Overflow instead of a clean refusal."""
    value = json.dumps({"m": {**REVIEW_PRICE, "input_per_million": "1e999999"}})
    try:
        parsed = parse_model_pricing(value)
    except ModelPricingError:
        pass
    else:  # pragma: no cover - the original repro's failing path
        from nexura_agents.usage.errors import UsageError
        from nexura_agents.usage.manager import BudgetManager
        from nexura_agents.usage.models import BudgetConfig

        budget = BudgetManager(pricing_table=build_pricing_table(value)).create_run_budget(
            workspace_id="w", run_id="r", config=BudgetConfig(max_cost_usd=Decimal("1"))
        )
        assert parsed["m"].input_cost_per_million == Decimal("1e999999")
        with pytest.raises(UsageError):
            budget.admit_provider_call("m", estimated_tokens=1000)
    with pytest.raises(ModelPricingError, match="above 100000"):
        parse_model_pricing(json.dumps({"m": {**REVIEW_PRICE, "output_per_million": "100000.01"}}))
    assert parse_model_pricing(json.dumps({"m": {**REVIEW_PRICE, "input_per_million": "100000"}}))


def test_an_alias_named_like_another_models_provider_id_cannot_take_an_operator_price() -> None:
    """Review round 2 (r2-1): 'gpt-4o' -> cheap-model (operator-priced) and 'fast' -> gpt-4o
    (built-in price). Copying cheap-model's price onto the name 'gpt-4o' would charge 'fast'
    runs 0.01/M; the configuration is refused, directly or through a derived price."""
    from nexura_runtime.service.api.model_pricing import ModelPricingError, build_pricing_table

    cheap = {
        "input_per_million": "0.01",
        "output_per_million": "0.01",
        "effective_date": "2026-01-01",
    }
    with pytest.raises(ModelPricingError, match="ambiguous"):
        build_pricing_table(
            {"cheap-model": cheap}, aliases={"gpt-4o": "cheap-model", "fast": "gpt-4o"}
        )
    with pytest.raises(ModelPricingError, match="ambiguous"):
        build_pricing_table(
            {"other": cheap}, aliases={"gpt-4o": "p", "other": "p", "fast": "gpt-4o"}
        )
    # Unpriced, the same names are fine: nothing crosses an alias.
    table = build_pricing_table(None, aliases={"gpt-4o": "cheap-model", "fast": "gpt-4o"})
    builtin = table.get_pricing("gpt-4o")
    assert builtin is not None and str(builtin.input_cost_per_million) == "2.50"
