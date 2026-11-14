"""API mode: real embeddings + LLM judge + LLM agents, tested against a fake OpenAI-compatible provider."""
import json
import math

import httpx
import pytest

from backend import llm, local_nlp
from backend.config import get_settings


class FakeProvider:
    """Stands in for Gemini/OpenAI. Records every request so tests can inspect the prompts."""

    def __init__(self):
        self.requests = []
        self.rate_limit_next_chat = False

    def post(self, url, headers=None, timeout=None, **kwargs):
        body = kwargs["json"]
        self.requests.append({"url": url, "body": body, "headers": headers})
        request = httpx.Request("POST", url)
        model = body["model"]
        if model.startswith("missing"):
            return httpx.Response(404, json={"error": {"message": f"model {model} not found"}}, request=request)
        if model.startswith("busy"):
            return httpx.Response(503, json={"error": {"message": "This model is currently experiencing high demand."}},
                                  request=request)
        if model.startswith("hanging"):
            raise httpx.ReadTimeout("The read operation timed out")
        if url.endswith("/embeddings"):
            # 1000 numbers, longer than our 768-dim column, to exercise truncation
            vector = local_nlp.local_embed(body["input"], 768) + [0.5] * 232
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": vector}]}, request=request)
        if self.rate_limit_next_chat:
            self.rate_limit_next_chat = False
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": {"message": "slow down"}},
                                  request=request)
        system, user = body["messages"][0]["content"], body["messages"][1]["content"]
        if "relevance judge" in system:
            candidates = json.loads(user)["candidates"]
            results = [{"id": c["id"], "score": 0.92 if c["company"] in ("PhonePe", "HDFC Bank") else 0.2,
                        "reason": f"test verdict for {c['company']}"} for c in candidates]
            content = "```json\n" + json.dumps({"results": results}) + "\n```"   # fenced, like real models
        elif "PhonePe" in system:
            content = '{"engage": true, "reason": "UPI cashback fits", "comment": "Extra ₹50 cashback with PhonePe UPI."}'
        else:
            content = 'Sure! {"engage": false, "reason": "No matching offer", "comment": ""}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]}, request=request)


@pytest.fixture()
def provider(monkeypatch):
    fake = FakeProvider()
    for name, value in {
        "LLM_BASE_URL": "https://fake-llm.test/v1", "LLM_API_KEY": "test-key",
        "LLM_MODEL": "missing-chat-model,good-chat-model", "LLM_REASONING_EFFORT": "low",
        "EMBEDDING_BASE_URL": "https://fake-llm.test/v1", "EMBEDDING_API_KEY": "test-key",
        "EMBEDDING_MODEL": "missing-embed-model,good-embed-model",
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    llm._working_model.clear()
    monkeypatch.setattr(llm.httpx, "post", fake.post)
    monkeypatch.setattr(llm.time, "sleep", lambda _seconds: None)
    yield fake
    monkeypatch.undo()
    get_settings.cache_clear()
    llm._working_model.clear()


def test_full_flow_with_embeddings_and_llm_judge(h, client, provider):
    provider.rate_limit_next_chat = True                     # first judge call gets a 429 and is retried
    post = h.demo_post("swiggy", "5% off all deliveries on Diwali.")
    summary = post["routing_summary"]

    assert summary["mode"]["embeddings"] == "good-embed-model"          # fell back past the missing model
    assert summary["mode"]["judge"] == "llm:good-chat-model"
    decisions = h.decisions(post)
    assert decisions["phonepe"] == decisions["hdfc-bank"] == "notified"
    assert decisions["bajaj-auto"] != "notified"
    assert h.log_for(post, "phonepe")["reason"] == "test verdict for PhonePe"

    # one batched judge call for the whole post, not one call per candidate
    judge_calls = [r for r in provider.requests if r["url"].endswith("/chat/completions")
                   and "relevance judge" in r["body"]["messages"][0]["content"]
                   and r["body"]["model"] == "good-chat-model"]
    assert len(judge_calls) == 2                             # the 429 and its retry
    assert len(json.loads(judge_calls[0]["body"]["messages"][1]["content"])["candidates"]) == 6

    results = client.post(f"/api/v1/demo/posts/{post['id']}/process-pending").json()
    by_agent = {r["agent"]["handle"]: r for r in results["results"]}
    assert by_agent["phonepe"]["action"] == "commented" and by_agent["phonepe"]["responder"] == "llm"
    assert by_agent["hdfc-bank"]["action"] == "ignored"      # the agent itself decided not to engage
    assert h.log_for(results["post"], "hdfc-bank")["status"] == "IGNORED"


def test_post_text_reaches_the_agent_llm_only_as_data(h, client, provider):
    """Prompt-injection defence: instructions stay in the system turn, the post travels as JSON data."""
    post = h.demo_post("swiggy", "5% off all deliveries on Diwali.")
    client.post(f"/api/v1/demo/posts/{post['id']}/process-pending")
    agent_calls = [r for r in provider.requests if r["url"].endswith("/chat/completions")
                   and "partnerships agent" in r["body"]["messages"][0]["content"]]
    assert agent_calls
    for call in agent_calls:
        system, user = (m["content"] for m in call["body"]["messages"][:2])
        assert "5% off all deliveries" not in system         # post text never enters the instructions
        payload = json.loads(user)["notification"]           # the user turn is a pure JSON document
        assert payload["post"]["content"] == "5% off all deliveries on Diwali."
        assert "untrusted" in payload["notice"]
        assert "never invent" in system.lower()               # replies are grounded in the agent's own facts


def test_provider_outage_saves_the_post_and_allows_retry(h, client, provider, monkeypatch):
    def down(url, **_):
        raise httpx.ConnectError("provider unreachable")

    monkeypatch.setattr(llm.httpx, "post", down)
    response = client.post("/api/v1/demo/posts", json={"agent_id": h.agents["swiggy"]["id"],
                                                        "content": "5% off all deliveries on Diwali."})
    assert response.status_code == 503
    body = response.json()
    assert body["kind"] == "network" and body["retry"].endswith("/route")
    saved = client.get(f"/api/v1/posts/{body['post_id']}").json()["post"]
    assert saved["routing_status"] == "failed" and saved["routing_logs"] == []   # nobody half-notified

    monkeypatch.setattr(llm.httpx, "post", provider.post)    # provider is back
    retried = client.post(body["retry"]).json()["post"]
    assert retried["routing_status"] == "routed" and h.decisions(retried)["phonepe"] == "notified"


def test_status_live_check(client, provider):
    data = client.get("/api/v1/system/status?check=true").json()
    assert data["llm"]["mode"] == data["embeddings"]["mode"] == "api"
    assert data["live_check"]["embeddings"] == {"ok": True, "model": "good-embed-model"}


def test_parse_json_tolerates_fences_and_chatter():
    assert llm.parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.parse_json('Here you go: {"a": {"b": 2}} hope that helps') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        llm.parse_json("no json here")


def test_fit_dimension_truncates_and_normalises():
    vector = llm.fit_dimension([3.0, 4.0, 12.0], 2)
    assert vector == [0.6, 0.8]
    assert math.isclose(sum(x * x for x in llm.fit_dimension([1.0] * 10, 4)), 1.0)
    assert llm.fit_dimension([1.0], 3) == [1.0, 0.0, 0.0]


def test_busy_or_hanging_chat_model_falls_through_to_the_next_one(h, provider, monkeypatch):
    """Free-tier models are sometimes overloaded (HTTP 503) or simply hang: use the next model in the list."""
    monkeypatch.setenv("LLM_MODEL", "busy-chat-model,hanging-chat-model,good-chat-model")
    get_settings.cache_clear()
    post = h.demo_post("swiggy", "5% off all deliveries on Diwali.")
    assert post["routing_summary"]["mode"]["judge"] == "llm:good-chat-model"
    assert h.decisions(post)["phonepe"] == "notified"
    # the model that worked is remembered, so the next call does not try the busy ones again
    before = len(provider.requests)
    llm.chat_json("You are the relevance judge.", '{"candidates": []}')
    assert [r["body"]["model"] for r in provider.requests[before:]] == ["good-chat-model"]


def test_busy_embedding_model_is_not_swapped_for_another(h, client, provider, monkeypatch):
    """Vectors from two embedding models must never be mixed, so embeddings fail loudly instead."""
    monkeypatch.setenv("EMBEDDING_MODEL", "busy-embed-model,good-embed-model")
    get_settings.cache_clear()
    response = client.post("/api/v1/demo/posts", json={"agent_id": h.agents["swiggy"]["id"],
                                                        "content": "5% off all deliveries on Diwali."})
    assert response.status_code == 503 and response.json()["kind"] == "upstream"
