"""Tests for Sessions API endpoints and chat playground."""

import pytest
from httpx import ASGITransport, AsyncClient

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.studio.store import StudioStore


@pytest.fixture
def tmp_store(tmp_path):
    db_file = tmp_path / "test_sessions.sqlite3"
    return StudioStore(db_file)


@pytest.mark.asyncio
async def test_sessions_lifecycle_and_chat(tmp_store):
    app = create_api_app(store=tmp_store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. List sessions initially empty
        resp = await client.get("/api/v1/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

        # 2. Create a new session
        resp = await client.post("/api/v1/sessions", json={"title": "Test Chat"})
        assert resp.status_code == 201
        sess_data = resp.json()
        sess_id = sess_data["session_id"]
        assert sess_id.startswith("sess-")
        assert sess_data["revision"] == 1

        # 3. List sessions contains new session
        resp = await client.get("/api/v1/sessions")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1
        assert resp.json()["items"][0]["session_id"] == sess_id

        # 4. Chat turn 1: English greeting
        resp = await client.post(
            f"/api/v1/sessions/{sess_id}/chat",
            json={"message": "Hello there!", "language": "en"},
        )
        assert resp.status_code == 200
        chat_resp = resp.json()
        assert chat_resp["session_id"] == sess_id
        assert chat_resp["model"] == "fake-deterministic (Local Offline Fixture)"
        assert "Hello! I am the Nexura Assistant" in chat_resp["reply"]
        assert len(chat_resp["messages"]) >= 2  # includes system, user, assistant
        assert chat_resp["usage"]["total_tokens"] > 0
        assert chat_resp["latency_ms"] >= 0

        # 5. Chat turn 2: Math calculation tool execution
        resp = await client.post(
            f"/api/v1/sessions/{sess_id}/chat",
            json={"message": "calculate 25 * 4", "language": "en"},
        )
        assert resp.status_code == 200
        calc_resp = resp.json()
        assert "100" in calc_resp["reply"]
        assert len(calc_resp["tool_executions"]) >= 1
        assert calc_resp["tool_executions"][0]["tool"] == "calculate"
        assert calc_resp["tool_executions"][0]["output"] == "100"

        # 6. Chat turn 3: Hindi greeting
        resp = await client.post(
            f"/api/v1/sessions/{sess_id}/chat",
            json={"message": "नमस्ते", "language": "hi"},
        )
        assert resp.status_code == 200
        hi_resp = resp.json()
        assert "नमस्ते! मैं नेक्सुरा असिस्टेंट हूँ" in hi_resp["reply"]

        # 7. Get full transcript
        resp = await client.get(f"/api/v1/sessions/{sess_id}")
        assert resp.status_code == 200
        transcript = resp.json()
        assert transcript["session_id"] == sess_id
        assert len(transcript["messages"]) >= 5

        # 8. Delete session
        resp = await client.delete(f"/api/v1/sessions/{sess_id}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

        # 9. Verify 404 after deletion
        resp = await client.get(f"/api/v1/sessions/{sess_id}")
        assert resp.status_code == 404
