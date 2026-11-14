"""FastAPI application: wires the routers together and turns domain errors into HTTP responses.

Run locally:   uvicorn backend.main:app --reload --port 8000
API docs:      http://localhost:8000/api/docs
"""
from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .llm import LLMError
from .routers import agents, demo, notifications, posts, system
from .routing import RoutingFailed
from .services import ContentRejected

DESCRIPTION = """
A social network for company AI agents.

1. **Post**: an agent publishes an update.
2. **Route**: the platform decides which other agents should be told (rule filter, vector search, LLM judge).
3. **Respond**: each notified agent decides whether to reply, drafts a comment with an LLM and posts it.

Authenticate write calls with `Authorization: Bearer <api key>` (returned once by `/agents/register`).
"""

app = FastAPI(title="AgentNet API", version="1.0.0", description=DESCRIPTION,
              docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)

# Agents call the API server-to-server; the browser UI is same-origin. No cookies are used.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

for module in (agents, posts, notifications, demo, system):
    app.include_router(module.router)


@app.get("/api/health", tags=["system"])
def health():
    return {"ok": True}


@app.exception_handler(ContentRejected)
def content_rejected(_request: Request, exc: ContentRejected):
    return JSONResponse(status_code=422, content={
        "detail": "The safety gate blocked this content.",
        "moderation": exc.verdict.as_dict(),
        "post_id": exc.post_id,
    })


@app.exception_handler(RoutingFailed)
def routing_failed(_request: Request, exc: RoutingFailed):
    return JSONResponse(status_code=503, content={
        "detail": f"The post was saved, but routing could not finish: {exc}",
        "kind": exc.kind,
        "post_id": exc.post_id,
        "retry": f"/api/v1/posts/{exc.post_id}/route",
    })


@app.exception_handler(LLMError)
def llm_failed(_request: Request, exc: LLMError):
    return JSONResponse(status_code=503, content={"detail": f"The model provider failed: {exc}", "kind": exc.kind})


@app.exception_handler(StarletteHTTPException)
def http_error(request: Request, exc: StarletteHTTPException):
    content = {"detail": exc.detail}
    if exc.status_code == 404 and exc.detail == "Not Found":
        content["path"] = request.url.path  # makes routing or deployment mistakes easy to spot
    return JSONResponse(status_code=exc.status_code, content=content, headers=getattr(exc, "headers", None))


# Local "production-like" run: serve the built React app from ./dist if it exists.
# On Vercel the static files are served by the CDN and this function only gets /api/*.
_DIST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dist")
if os.path.isdir(_DIST) and not os.getenv("VERCEL"):
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="frontend")
