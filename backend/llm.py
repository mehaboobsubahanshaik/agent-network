"""Thin OpenAI-compatible client for chat (JSON answers) and embeddings.

Works with any provider that speaks the OpenAI REST shape (Gemini, OpenAI,
Groq, Ollama...). Plain httpx, no SDK, so every request is easy to read.
"""
from __future__ import annotations

import json
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from .config import get_settings

log = logging.getLogger("agentnet.llm")


class LLMError(Exception):
    """Raised when the provider cannot give us a usable answer."""

    def __init__(self, message: str, kind: str = "error", status: int | None = None):
        super().__init__(message)
        self.kind = kind      # rate_limited | timeout | network | upstream | bad_json | config | error
        self.status = status


# Remember which model from the configured list actually works for this key.
_working_model: dict[str, str] = {}
_drop_reasoning_effort = False


def _post(base_url: str, api_key: str, path: str, body: dict, timeout: float = 30.0) -> httpx.Response:
    """POST with one retry on rate limits, server errors and network blips."""
    url = f"{base_url}/{path}"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    for attempt in (1, 2):
        try:
            response = httpx.post(url, json=body, headers=headers, timeout=timeout)
        except httpx.TimeoutException as exc:
            # A model that hangs is usually overloaded. Do not wait a second time: report it,
            # so the caller can move on to the next model.
            raise LLMError(f"The model did not answer within {timeout:.0f} seconds.", kind="timeout") from exc
        except httpx.HTTPError as exc:
            if attempt == 2:
                raise LLMError(f"Could not reach the model provider: {exc}", kind="network") from exc
            time.sleep(0.8)
            continue
        if response.status_code == 429 or response.status_code >= 500:
            if attempt == 2:
                kind = "rate_limited" if response.status_code == 429 else "upstream"
                raise LLMError(_error_text(response), kind=kind, status=response.status_code)
            try:
                wait = float(response.headers.get("retry-after", "2"))
            except ValueError:
                wait = 2.0
            time.sleep(min(wait, 8.0))
            continue
        return response
    raise LLMError("unreachable")  # pragma: no cover


def _error_text(response: httpx.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, list) and data:
            data = data[0]
        message = data.get("error", {}).get("message") if isinstance(data.get("error"), dict) else data.get("error")
        return f"HTTP {response.status_code}: {message or response.text[:300]}"
    except Exception:
        return f"HTTP {response.status_code}: {response.text[:300]}"


def _call_with_model_fallback(kind: str, base_url: str, api_key: str, models: tuple[str, ...],
                              path: str, make_body) -> tuple[dict, str]:
    """Try the configured models in order and stick with the first that the key can use."""
    ordered = list(models)
    if _working_model.get(kind) in ordered:
        ordered.remove(_working_model[kind])
        ordered.insert(0, _working_model[kind])
    last_error = "no model configured"
    for position, model in enumerate(ordered):
        try:
            response = _post(base_url, api_key, path, make_body(model))
        except LLMError as exc:
            # Chat only: if this model is overloaded, rate limited or hanging, use the next one.
            # Embedding models are never swapped this way, because vectors from different
            # models cannot be compared with each other.
            busy = exc.kind in ("upstream", "rate_limited", "timeout")
            if kind == "chat" and busy and position < len(ordered) - 1:
                last_error = str(exc)
                log.warning("model %s is busy (%s); trying the next one", model, last_error)
                continue
            raise
        if response.status_code < 300:
            _working_model[kind] = model
            return response.json(), model
        last_error = _error_text(response)
        model_problem = response.status_code == 404 or (
            response.status_code in (400, 403) and "model" in response.text.lower()
        )
        if not model_problem:
            raise LLMError(last_error, kind="config" if response.status_code in (401, 403) else "error",
                           status=response.status_code)
        log.warning("model %s not usable (%s); trying the next one", model, last_error)
    raise LLMError(f"No configured {kind} model worked. Last error: {last_error}", kind="config")


# ---------------------------------------------------------------- chat (JSON)

def parse_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply (tolerates ``` fences and chatter)."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start:end + 1])


def chat_json(system: str, user: str, temperature: float = 0.2) -> dict:
    """Ask the chat model a question and get a JSON object back."""
    global _drop_reasoning_effort
    s = get_settings()
    if not s.llm_enabled:
        raise LLMError("No chat model is configured.", kind="config")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def make_body(model: str) -> dict:
        body = {"model": model, "messages": messages, "temperature": temperature}
        if s.llm_reasoning_effort and not _drop_reasoning_effort:
            body["reasoning_effort"] = s.llm_reasoning_effort
        return body

    for attempt in (1, 2):
        try:
            data, _model = _call_with_model_fallback("chat", s.llm_base_url, s.llm_api_key, s.llm_models,
                                                     "chat/completions", make_body)
        except LLMError as exc:
            # Some providers reject the optional reasoning_effort field: drop it once and retry.
            if s.llm_reasoning_effort and not _drop_reasoning_effort and "reasoning" in str(exc).lower():
                _drop_reasoning_effort = True
                continue
            raise
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        try:
            return parse_json(content)
        except (ValueError, json.JSONDecodeError):
            if attempt == 2:
                raise LLMError(f"Model did not return valid JSON: {content[:200]!r}", kind="bad_json")
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": "Reply again with only one valid JSON object."})
    raise LLMError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------- embeddings

def fit_dimension(vector: list[float], dim: int) -> list[float]:
    """Truncate (or pad) to `dim` and L2-normalise.

    Modern embedding models (Gemini, OpenAI v3) are trained so that a prefix of
    the vector is still a good embedding, which keeps the pgvector column small.
    """
    vector = list(vector[:dim]) + [0.0] * max(0, dim - len(vector))
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm else vector


def embed_texts(texts: list[str]) -> tuple[list[list[float]], str]:
    """Embed each text with the configured API. Returns (vectors, model_name)."""
    s = get_settings()
    if not s.embedding_api_enabled:
        raise LLMError("No embedding model is configured.", kind="config")

    def embed_one(text: str) -> tuple[list[float], str]:
        data, model = _call_with_model_fallback(
            "embedding", s.embedding_base_url, s.embedding_api_key, s.embedding_models,
            "embeddings", lambda m: {"model": m, "input": text},
        )
        return fit_dimension(data["data"][0]["embedding"], s.embedding_dim), model

    if not texts:
        return [], _working_model.get("embedding") or s.embedding_models[0]
    first_vector, model = embed_one(texts[0])  # settles which model works before fanning out
    with ThreadPoolExecutor(max_workers=4) as pool:
        rest = list(pool.map(embed_one, texts[1:]))
    return [first_vector] + [vector for vector, _ in rest], model


def diagnose() -> dict:
    """Live check of both endpoints, for the /system/status page."""
    s = get_settings()
    report: dict = {}
    if s.llm_enabled:
        try:
            answer = chat_json("You are a health check.", 'Reply with exactly {"ok": true}')
            report["chat"] = {"ok": bool(answer.get("ok")), "model": _working_model.get("chat")}
        except LLMError as exc:
            report["chat"] = {"ok": False, "error": str(exc), "kind": exc.kind}
    else:
        report["chat"] = {"ok": False, "error": "not configured (offline judge and responder in use)"}
    if s.embedding_api_enabled:
        try:
            vectors, model = embed_texts(["health check"])
            report["embeddings"] = {"ok": len(vectors[0]) == s.embedding_dim, "model": model}
        except LLMError as exc:
            report["embeddings"] = {"ok": False, "error": str(exc), "kind": exc.kind}
    else:
        report["embeddings"] = {"ok": False, "error": "not configured (offline embedder in use)"}
    return report
