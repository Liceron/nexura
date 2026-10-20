"""Regressions from the P4 review: large datasets and dataset metadata redaction."""

import pytest
from httpx import ASGITransport, AsyncClient
from nexura_agents import Agent, FakeModel

from nexura_runtime.service.api.app import create_api_app

BASE = "/api/v1/evaluations/studio"
SECRET = "sk-abcdefghijklmnopqrstuvwxyz0123456789ABCD"


@pytest.fixture
def app(tmp_path):
    a = create_api_app(db_path=str(tmp_path / "e.sqlite3"), profile="local-owner")
    a.state.eval_agents = {
        "leaky": lambda v: Agent(
            name="l", instructions="x", model=FakeModel(handler=lambda m, t: f"key {SECRET}")
        )
    }
    return a


def _c(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.mark.asyncio
async def test_dataset_150_cases_accepted_and_preserved(app):
    cases = [{"name": f"c{i}", "input": "x"} for i in range(150)]
    async with _c(app) as c:
        r = await c.put(f"{BASE}/datasets/d", json={"cases": cases})
        assert r.status_code == 200, r.text  # limit is 200
        got = (await c.get(f"{BASE}/datasets/d")).json()
        assert len(got["cases"]) == 150


@pytest.mark.asyncio
async def test_dataset_name_description_secret_not_stored(app):
    async with _c(app) as c:
        r = await c.put(
            f"{BASE}/datasets/d",
            json={
                "name": f"n {SECRET}",
                "description": f"d {SECRET}",
                "cases": [{"name": "a", "input": "x"}],
            },
        )
        assert r.status_code == 200, r.text
        assert SECRET not in (await c.get(f"{BASE}/datasets/d")).text
        assert SECRET not in (await c.get(f"{BASE}/datasets")).text


@pytest.mark.asyncio
async def test_run_output_secret_redacted_everywhere(app):
    async with _c(app) as c:
        await c.put(
            f"{BASE}/datasets/d",
            json={"cases": [{"name": "a", "input": "x", "expected_output": "z"}]},
        )
        r = await c.post(
            f"{BASE}/runs", json={"dataset_id": "d", "target": {"kind": "agent", "id": "leaky"}}
        )
        assert r.status_code == 200, r.text
        assert SECRET not in r.text
        rid = r.json()["run_id"]
        assert SECRET not in (await c.get(f"{BASE}/runs/{rid}")).text
        assert SECRET not in (await c.get(f"{BASE}/runs")).text
        cmp = await c.post(
            f"{BASE}/compare", json={"baseline_run_id": rid, "candidate_run_id": rid}
        )
        assert SECRET not in cmp.text


@pytest.mark.asyncio
async def test_secret_in_case_input_and_metadata_redacted(app):
    async with _c(app) as c:
        r = await c.put(
            f"{BASE}/datasets/d",
            json={
                "cases": [
                    {
                        "name": "a",
                        "input": f"use {SECRET}",
                        "expected_output": f"{SECRET}",
                        "metadata": {"note": SECRET},
                    }
                ]
            },
        )
        assert r.status_code == 200, r.text
        assert SECRET not in (await c.get(f"{BASE}/datasets/d/export")).text
