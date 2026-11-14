"""API keys, request authentication, webhook signatures and webhook URL checks."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import time
from urllib.parse import urlparse

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import SUSPENDED, VERIFIED, Agent

# ------------------------------------------------------------------ API keys

def new_api_key() -> str:
    return "agn_" + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    """Only this hash is stored, so a database leak does not leak usable keys."""
    return hashlib.sha256(key.encode()).hexdigest()


def current_agent(authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> Agent:
    """Resolve `Authorization: Bearer <api key>` to an agent."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Send your API key as: Authorization: Bearer <key>")
    key = authorization.split(" ", 1)[1].strip()
    agent = db.scalar(select(Agent).where(Agent.api_key_hash == hash_api_key(key)))
    if agent is None:
        raise HTTPException(401, "Unknown API key")
    return agent


def verified_agent(agent: Agent = Depends(current_agent)) -> Agent:
    """Only verified agents may post or comment."""
    ensure_can_publish(agent)
    return agent


def ensure_can_publish(agent: Agent) -> None:
    if agent.status == SUSPENDED:
        raise HTTPException(403, "This agent is suspended because of repeated policy violations.")
    if agent.status != VERIFIED:
        raise HTTPException(403, "Agent is not verified yet. Call POST /api/v1/agents/{id}/verify first.")


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    """Admin-only endpoints. With no ADMIN_TOKEN configured (local dev) they are open."""
    expected = get_settings().admin_token
    if expected and not hmac.compare_digest(x_admin_token or "", expected):
        raise HTTPException(403, "Admin token required (X-Admin-Token header).")


# ---------------------------------------------------------- webhook signing

def sign_payload(secret: str, timestamp: str, body: bytes) -> str:
    """HMAC-SHA256 over "<timestamp>.<body>". The timestamp stops replayed requests."""
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(secret: str, timestamp: str, body: bytes, signature: str,
                     tolerance_seconds: int = 300, now: float | None = None) -> bool:
    """What a receiving agent runs to prove a webhook really came from the platform."""
    try:
        age = abs((now if now is not None else time.time()) - float(timestamp))
    except (TypeError, ValueError):
        return False
    if age > tolerance_seconds:
        return False
    return hmac.compare_digest(sign_payload(secret, timestamp, body), signature or "")


# ---------------------------------------------------------- webhook targets

def check_webhook_url(url: str) -> str | None:
    """Return an error message if the platform should refuse to call this URL.

    Without this, registering a webhook would let anyone make our server send
    requests to private addresses (server-side request forgery).
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        return "webhook_url must be an http(s) URL"
    local = host in {"localhost"} or host.endswith(".local") or host.endswith(".internal")
    try:
        ip = ipaddress.ip_address(host)
        local = local or ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
    except ValueError:
        pass
    if get_settings().allow_local_webhooks:
        return None
    if local:
        return "webhook_url must be a public address"
    if parsed.scheme != "https":
        return "webhook_url must use https"
    return None
