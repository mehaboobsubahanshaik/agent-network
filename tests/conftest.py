"""Test setup: a throwaway database, offline mode (no API keys), fresh seed data per test.

Run on SQLite (default):          pytest
Run on PostgreSQL + pgvector:     TEST_DATABASE_URL=postgresql://... pytest
"""
import os
import tempfile

# Must happen before the backend is imported, because the engine is created at import time.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tempfile.mkdtemp()}/test.db"
for name in ("GEMINI_API_KEY", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
             "EMBEDDING_API_KEY", "EMBEDDING_BASE_URL", "EMBEDDING_MODEL", "VERCEL"):
    os.environ.pop(name, None)
os.environ["ADMIN_TOKEN"] = "test-admin"
os.environ["EXPLORE_RATE"] = "0"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend import db as database  # noqa: E402
from backend.main import app  # noqa: E402


@pytest.fixture()
def client():
    database.reset_db()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def agents(client):
    """The built-in demo agents, keyed by handle."""
    return {a["handle"]: a for a in client.get("/api/v1/agents").json()["agents"]}


class Helpers:
    def __init__(self, client, agents):
        self.client, self.agents = client, agents

    def demo_post(self, handle: str, content: str, title: str = "", expect: int = 201) -> dict:
        response = self.client.post("/api/v1/demo/posts", json={
            "agent_id": self.agents[handle]["id"], "content": content, "title": title,
        })
        assert response.status_code == expect, response.text
        return response.json().get("post", response.json())

    def register(self, company: str, manifesto: str, industry: str = "Fintech", verify: bool = True,
                 **extra) -> tuple[dict, dict]:
        """Register an external agent. Returns (agent, auth_headers)."""
        response = self.client.post("/api/v1/agents/register", json={
            "company_name": company, "industry_category": industry, "interest_manifesto": manifesto, **extra,
        })
        assert response.status_code == 201, response.text
        body = response.json()
        headers = {"Authorization": f"Bearer {body['api_key']}"}
        if verify:
            verified = self.client.post(f"/api/v1/agents/{body['agent']['id']}/verify",
                                        json={"method": "demo"}, headers=headers)
            assert verified.status_code == 200, verified.text
        return {**body["agent"], "webhook_secret": body["webhook_secret"]}, headers

    @staticmethod
    def decisions(post: dict) -> dict:
        """{handle: decision} for one post."""
        return {log["agent"]["handle"]: log["decision"] for log in post["routing_logs"]}

    @staticmethod
    def log_for(post: dict, handle: str) -> dict:
        return next(log for log in post["routing_logs"] if log["agent"]["handle"] == handle)


@pytest.fixture()
def h(client, agents):
    return Helpers(client, agents)
