"""Backend tests for F03: Workflow library search, pagination,
lifecycle deletion, and cross-workspace scoping.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.workflow import WorkflowNode, WorkflowSpec


def _make_sample_spec(name: str) -> WorkflowSpec:
    return WorkflowSpec(
        name=name,
        entry_node="step_1",
        nodes={
            "step_1": WorkflowNode(
                id="step_1",
                handler="synthetic_order_lookup",
                transitions={},
                requires_approval=False,
            )
        },
    )


@pytest.mark.asyncio
async def test_workflows_search_pagination_and_deletion(tmp_path):
    db_path = tmp_path / "test_f03.sqlite3"
    app = create_api_app(db_path=db_path, profile="local-owner")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers_alpha = {"X-Workspace-ID": "workspace-alpha"}
        headers_beta = {"X-Workspace-ID": "workspace-beta"}

        # 1. Create workflows in workspace-alpha
        wf_alpha_1 = await client.post(
            "/api/v1/workflows",
            headers=headers_alpha,
            json={"spec": _make_sample_spec("Order Processing Alpha").to_dict()},
        )
        assert wf_alpha_1.status_code == 201
        id_1 = wf_alpha_1.json()["workflow_id"]

        wf_alpha_2 = await client.post(
            "/api/v1/workflows",
            headers=headers_alpha,
            json={"spec": _make_sample_spec("Inventory Check").to_dict()},
        )
        assert wf_alpha_2.status_code == 201
        id_2 = wf_alpha_2.json()["workflow_id"]

        # Create workflow in workspace-beta
        wf_beta_1 = await client.post(
            "/api/v1/workflows",
            headers=headers_beta,
            json={"spec": _make_sample_spec("Order Processing Beta").to_dict()},
        )
        assert wf_beta_1.status_code == 201
        id_beta = wf_beta_1.json()["workflow_id"]

        # 2. Test scoping & search in workspace-alpha
        list_res = await client.get("/api/v1/workflows", headers=headers_alpha)
        assert list_res.status_code == 200
        items = list_res.json()["items"]
        assert len(items) == 2
        item_ids = [it["workflow_id"] for it in items]
        assert id_1 in item_ids
        assert id_2 in item_ids
        assert id_beta not in item_ids  # No leak from workspace-beta

        # Search by query "order"
        search_res = await client.get("/api/v1/workflows?query=order", headers=headers_alpha)
        assert search_res.status_code == 200
        search_items = search_res.json()["items"]
        assert len(search_items) == 1
        assert search_items[0]["workflow_id"] == id_1

        # Search by query "inventory"
        search_inv = await client.get("/api/v1/workflows?query=inventory", headers=headers_alpha)
        assert search_inv.status_code == 200
        assert len(search_inv.json()["items"]) == 1
        assert search_inv.json()["items"][0]["workflow_id"] == id_2

        # Pagination: limit=1, offset=0
        p1 = await client.get("/api/v1/workflows?limit=1&offset=0", headers=headers_alpha)
        assert len(p1.json()["items"]) == 1
        assert p1.json()["has_more"] is True
        assert p1.json()["total"] == 2

        # 3. Test Cross-Workspace Access Prevention
        cross_get = await client.get(f"/api/v1/workflows/{id_beta}", headers=headers_alpha)
        # Local-owner mode names the other workspace (shared mode answers 404).
        assert cross_get.status_code == 403
        assert cross_get.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"

        cross_del = await client.delete(f"/api/v1/workflows/{id_beta}", headers=headers_alpha)
        assert cross_del.status_code == 403
        assert cross_del.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"

        # 4. Test Deletion Contract in workspace-alpha
        del_res = await client.delete(f"/api/v1/workflows/{id_1}", headers=headers_alpha)
        assert del_res.status_code == 200
        assert del_res.json()["deleted"] is True
        assert del_res.json()["workflow_id"] == id_1

        # Getting deleted workflow returns 404
        get_after = await client.get(f"/api/v1/workflows/{id_1}", headers=headers_alpha)
        assert get_after.status_code == 404
        assert get_after.json()["detail"]["code"] == "WORKFLOW_NOT_FOUND"

        # List now has only 1 workflow
        list_after = await client.get("/api/v1/workflows", headers=headers_alpha)
        assert list_after.json()["total"] == 1
        assert list_after.json()["items"][0]["workflow_id"] == id_2

        # Deleting already deleted workflow returns 404
        del_again = await client.delete(f"/api/v1/workflows/{id_1}", headers=headers_alpha)
        assert del_again.status_code == 404
