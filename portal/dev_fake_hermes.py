"""Dev-only fake Hermes API server for exercising the portal chat proxy.

It implements just enough of the OpenAI-compatible surface:
    GET  /v1/models
    POST /v1/chat/completions   (SSE streaming)

Run it beside the portal when you do not want to spend model tokens:

    .venv/Scripts/python.exe -m uvicorn portal.dev_fake_hermes:app --port 8643
    # then configure the tenant runtime to http://127.0.0.1:8643 with key sk-fake

This is NOT the real agent: it does not run tools, call a model, or emit portal
audit events. Use a real Hermes API server for production and audit tests.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="Fake Hermes API", docs_url=None, redoc_url=None)
API_KEY = os.environ.get("FAKE_HERMES_API_KEY", "sk-fake")


def _authorized(request: Request) -> bool:
    return request.headers.get("authorization", "") == f"Bearer {API_KEY}"


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/v1/models")
def models(request: Request):
    if not _authorized(request):
        return JSONResponse({"error": {"message": "bad key"}}, status_code=401)
    return {
        "object": "list",
        "data": [{"id": "demo-model", "object": "model", "created": int(time.time()), "owned_by": "fake"}],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    if not _authorized(request):
        return JSONResponse({"error": {"message": "bad key"}}, status_code=401)
    body = await request.json()
    messages = body.get("messages") or []
    user_text = messages[-1].get("content", "") if messages else ""
    session_id = request.headers.get("X-Hermes-Session-Id", "")
    reply = (
        f"这是演示运行环境的回复。你刚才说：「{user_text}」。\n\n"
        f"会话 ID：{session_id or '（未提供）'}\n"
        "真实部署时，这条消息会由租户容器里的 Hermes Agent 生成，"
        "并通过 portal-audit 插件把模型请求、工具调用和结果送回监管台。"
    )
    if not body.get("stream"):
        return {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion",
            "model": body.get("model", "demo-model"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 30, "total_tokens": 40},
        }

    async def generate():
        for index in range(0, len(reply), 8):
            await asyncio.sleep(0.04)
            chunk = {
                "id": "chatcmpl-demo",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": body.get("model", "demo-model"),
                "choices": [{"index": 0, "delta": {"content": reply[index:index + 8]}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        yield 'data: {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 30, "total_tokens": 40}}\n\n'
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
