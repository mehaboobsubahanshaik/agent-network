"""How notifications reach agents: signed webhooks (push) with the inbox (pull) as the fallback."""
import json
import time

import httpx

from backend import webhooks
from backend.security import sign_payload, verify_signature

MANIFESTO = ("Interested in food delivery festive discounts, e-commerce promotions and merchant cashback "
             "opportunities where a wallet payment offer can be attached.")


def test_signature_round_trip_and_tampering():
    secret, timestamp, body = "s3cret", str(int(time.time())), b'{"event":"post.relevant"}'
    signature = sign_payload(secret, timestamp, body)
    assert verify_signature(secret, timestamp, body, signature)
    assert not verify_signature(secret, timestamp, body + b" ", signature)          # body changed
    assert not verify_signature("other-secret", timestamp, body, signature)         # wrong secret
    old = str(int(time.time()) - 3600)
    assert not verify_signature(secret, old, body, sign_payload(secret, old, body))  # replayed an hour later


def test_webhook_is_delivered_signed_and_verifiable(h, client, monkeypatch):
    calls = []

    def fake_post(url, content=None, headers=None, timeout=None, **_):
        calls.append({"url": url, "body": content, "headers": headers})
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(webhooks.httpx, "post", fake_post)
    agent, _ = h.register("QuickWallet", MANIFESTO, industry="Payments",
                          webhook_url="https://agent.quickwallet.example/hooks/agentnet")
    post = h.demo_post("swiggy", "5% off all deliveries on Diwali.")

    assert len(calls) == 1 and calls[0]["url"].endswith("/hooks/agentnet")
    headers, body = calls[0]["headers"], calls[0]["body"]
    # the receiving agent can prove the request came from the platform
    assert verify_signature(agent["webhook_secret"], headers["X-AgentNet-Timestamp"], body,
                            headers["X-AgentNet-Signature"])
    payload = json.loads(body)
    assert payload["event"] == "post.relevant"
    assert payload["post"]["content"] == "5% off all deliveries on Diwali."
    assert payload["post"]["author"]["company_name"] == "Swiggy"
    assert payload["reply_to"] == f"/api/v1/posts/{post['id']}/comments"
    log = h.log_for(post, "quickwallet")
    assert log["delivery"] == {"channel": "webhook", "attempts": 1, "error": None}


def test_failed_webhook_is_retried_then_falls_back_to_the_inbox(h, client, monkeypatch):
    attempts = []

    def failing_post(url, **_):
        attempts.append(url)
        raise httpx.ConnectTimeout("agent server is down")

    monkeypatch.setattr(webhooks.httpx, "post", failing_post)
    monkeypatch.setattr(webhooks.time, "sleep", lambda _seconds: None)
    _, auth = h.register("QuickWallet", MANIFESTO, industry="Payments",
                         webhook_url="https://agent.quickwallet.example/hooks/agentnet")
    post = h.demo_post("swiggy", "5% off all deliveries on Diwali.")     # posting still succeeds

    log = h.log_for(post, "quickwallet")
    assert len(attempts) == webhooks.MAX_ATTEMPTS
    assert log["status"] == "FAILED" and log["delivery"]["error"] == "ConnectTimeout"
    # nothing is lost: the agent can still pull it
    inbox = client.get("/api/v1/notifications", headers=auth).json()["notifications"]
    assert len(inbox) == 1 and inbox[0]["status"] == "NOTIFIED"
    # and a failed delivery is not counted as the agent ignoring the post
    assert client.get("/api/v1/agents/me", headers=auth).json()["agent"]["stats"]["ignored"] == 0


def test_agents_without_a_webhook_use_the_inbox(h, client):
    _, auth = h.register("QuickWallet", MANIFESTO, industry="Payments")
    post = h.demo_post("swiggy", "5% off all deliveries on Diwali.")
    assert h.log_for(post, "quickwallet")["delivery"]["channel"] == "inbox"
    assert len(client.get("/api/v1/notifications", headers=auth).json()["notifications"]) == 1


def test_inbox_is_private_to_each_agent(h, client):
    _, auth_a = h.register("QuickWallet", MANIFESTO, industry="Payments")
    _, auth_b = h.register("SlowWallet", "Interested in stock market updates and IPO announcements.")
    h.demo_post("swiggy", "5% off all deliveries on Diwali.")
    note = client.get("/api/v1/notifications", headers=auth_a).json()["notifications"][0]
    assert client.get("/api/v1/notifications", headers=auth_b).json()["notifications"] == []
    stolen = client.post(f"/api/v1/notifications/{note['notification_id']}/dismiss", headers=auth_b, json={})
    assert stolen.status_code == 404
