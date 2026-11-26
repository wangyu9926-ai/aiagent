"""Allowlisted egress proxy for tenant containers.

Tenant containers sit on an internal Docker network with no route to the
internet. This proxy is the only service on that network that can reach the
model upstream, and it only forwards ``/v1/...`` model traffic. Every request is
logged with its model name, status and duration; request bodies and credentials
are never logged.

This is deliberately a reverse proxy for the configured model endpoint, not an
open forward proxy. Browser/web traffic from a tenant is blocked by design until
a dedicated allowlisted web egress is added.
"""

from __future__ import annotations

import json
import logging
import os
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

logging.basicConfig(level=os.environ.get("EGRESS_LOG_LEVEL", "INFO"))
logger = logging.getLogger("hermes.egress")

UPSTREAM = os.environ.get("EGRESS_UPSTREAM", "http://host.docker.internal:7864").rstrip("/")
UPSTREAM_API_KEY = os.environ.get("EGRESS_API_KEY", "").strip()

app = FastAPI(title="Hermes Egress", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/healthz")
def healthz():
    return {"ok": True, "upstream": UPSTREAM}


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "DELETE", "OPTIONS"])
async def proxy_model(path: str, request: Request):
    if ".." in path.split("/"):
        return JSONResponse({"error": {"message": "invalid path"}}, status_code=400)
    url = f"{UPSTREAM}/v1/{path}"
    body = await request.body()
    model = ""
    try:
        parsed = json.loads(body or b"{}")
        if isinstance(parsed, dict):
            model = str(parsed.get("model") or "")
    except (ValueError, TypeError):
        pass
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "content-length", "connection"}
    }
    # The tenant only holds a placeholder key. The real model credential lives
    # in this proxy so a user with shell access cannot read or reuse it.
    if UPSTREAM_API_KEY:
        headers["authorization"] = f"Bearer {UPSTREAM_API_KEY}"
    started = time.time()
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=None, write=60.0, pool=10.0))
    try:
        upstream_request = client.build_request(request.method, url, headers=headers, content=body)
        response = await client.send(upstream_request, stream=True)
    except Exception as exc:
        await client.aclose()
        logger.warning("egress upstream error path=%s model=%s error=%s", path, model, type(exc).__name__)
        return JSONResponse(
            {"error": {"message": f"egress upstream unavailable: {type(exc).__name__}"}},
            status_code=502,
        )
    logger.info(
        "egress method=%s path=%s model=%s status=%s elapsed=%.3fs",
        request.method,
        path,
        model or "-",
        response.status_code,
        time.time() - started,
    )
    response_headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in {"content-length", "transfer-encoding", "connection"}
    }

    async def cleanup():
        await response.aclose()
        await client.aclose()

    return StreamingResponse(
        response.aiter_raw(),
        status_code=response.status_code,
        headers=response_headers,
        background=BackgroundTask(cleanup),
    )
