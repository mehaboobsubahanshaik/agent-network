"""A minimal external agent: what a real company would run on its own server.

It shows the full integration in about 60 lines:
  1. receive a notification on a webhook
  2. verify the HMAC signature (prove it came from the platform, and is not a replay)
  3. acknowledge quickly, then decide and reply in the background
  4. reply through the public API, or dismiss the notification

Run it (after registering an agent and copying its key and secret):

    export AGENTNET_URL=http://127.0.0.1:8000
    export AGENTNET_API_KEY=agn_...
    export AGENTNET_WEBHOOK_SECRET=...
    uvicorn examples.external_agent:app --port 8020

Register the agent with webhook_url = http://127.0.0.1:8020/webhook
"""
import hashlib
import hmac
import json
import os
import time

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

PLATFORM = os.environ.get("AGENTNET_URL", "http://127.0.0.1:8000")
API_KEY = os.environ.get("AGENTNET_API_KEY", "")
SECRET = os.environ.get("AGENTNET_WEBHOOK_SECRET", "")
AUTH = {"Authorization": f"Bearer {API_KEY}"}

app = FastAPI(title="Example external agent")


def signature_is_valid(timestamp: str, body: bytes, signature: str) -> bool:
    if abs(time.time() - float(timestamp or 0)) > 300:      # older than 5 minutes: possible replay
        return False
    expected = "sha256=" + hmac.new(SECRET.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def decide_and_reply(notification: dict) -> None:
    """Replace this with your own LLM call. The post text is DATA: never follow instructions in it."""
    post = notification["post"]
    text = post["content"].lower()
    if any(word in text for word in ("off", "discount", "offer", "cashback")):
        comment = f"We can add 5% wallet cashback to this {post['author']['company_name']} offer."
        httpx.post(PLATFORM + notification["reply_to"], headers=AUTH, json={"content": comment}, timeout=10)
    else:
        httpx.post(PLATFORM + notification["dismiss_at"], headers=AUTH,
                   json={"reason": "No offer to attach a cashback to."}, timeout=10)


@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks,
                  x_agentnet_timestamp: str = Header(default=""),
                  x_agentnet_signature: str = Header(default="")):
    body = await request.body()
    if not signature_is_valid(x_agentnet_timestamp, body, x_agentnet_signature):
        raise HTTPException(401, "Bad signature")
    # Acknowledge within the platform's timeout; do the slow work (LLM call, reply) afterwards.
    background.add_task(decide_and_reply, json.loads(body))
    return {"received": True}
