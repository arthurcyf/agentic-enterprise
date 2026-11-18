"""Egress proxy: the sandbox's only route to the internet.

The sandbox sets ANTHROPIC_BASE_URL=http://egress:8080 and holds only a dummy
API key. This proxy forwards allowlisted Anthropic API paths to
https://api.anthropic.com, replacing the dummy key with the real one, which
lives only in this container. Every other path is refused and logged.
"""

# How this file fits in
# ---------------------
# Runs as:  compose service `egress` (on sandbox_net and egress_net, the only one with internet).
# Called by: the Claude Code CLI inside the sandbox, because orchestrator.engagement sets
#           ANTHROPIC_BASE_URL=http://egress:8080 and ANTHROPIC_API_KEY=sk-dummy in the container.
# Secret:   the real ANTHROPIC_API_KEY comes from the host shell via docker-compose.yml, only here.
from __future__ import annotations

import logging
import os
import re

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

UPSTREAM = os.environ.get("UPSTREAM", "https://api.anthropic.com")
ALLOWED_PATHS = [re.compile(p) for p in (
    r"^/v1/messages$",
    r"^/v1/messages/count_tokens$",
    r"^/v1/models(/.*)?$",
)]
DROP_REQUEST_HEADERS = {"host", "x-api-key", "authorization", "content-length", "connection"}
DROP_RESPONSE_HEADERS = {"content-length", "transfer-encoding", "connection", "content-encoding"}

log = logging.getLogger("egress")
logging.basicConfig(level=logging.INFO, format="%(asctime)s egress %(message)s")
app = FastAPI(title="egress")
client = httpx.AsyncClient(base_url=UPSTREAM, timeout=httpx.Timeout(600, connect=15))


@app.get("/health")
async def health():
    return {"ok": True}


@app.api_route("/{path:path}", methods=["GET", "POST"])
async def forward(path: str, request: Request):
    full = "/" + path
    if not any(p.match(full) for p in ALLOWED_PATHS):
        log.info("DENY %s %s", request.method, full)
        return JSONResponse({"error": f"egress: {full} is not an allowed upstream path"}, status_code=403)
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return JSONResponse({"error": "egress: ANTHROPIC_API_KEY is not configured on the proxy"}, status_code=503)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in DROP_REQUEST_HEADERS}
    headers["x-api-key"] = key
    upstream = client.build_request(request.method, full, params=request.query_params,
                                    headers=headers, content=await request.body())
    resp = await client.send(upstream, stream=True)
    log.info("ALLOW %s %s -> %s", request.method, full, resp.status_code)
    return StreamingResponse(resp.aiter_bytes(), status_code=resp.status_code,
                             headers={k: v for k, v in resp.headers.items() if k.lower() not in DROP_RESPONSE_HEADERS},
                             background=_closer(resp))


def _closer(resp):
    from starlette.background import BackgroundTask

    return BackgroundTask(resp.aclose)
