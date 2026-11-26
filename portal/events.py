"""Audit event canonicalisation and summaries.

The hash format is a wire contract shared with ``portal_audit`` (the Hermes
plugin). Change both sides together:

    sha256(canonical_json({
        "tenant": ..., "session_id": ..., "seq": int, "ts": float,
        "type": ..., "payload": {...}, "prev_hash": ...,
    }))

``canonical_json`` uses sorted keys and compact separators so the portal and
the plugin produce byte-identical input.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

GENESIS_HASH = ""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def event_hash(
    *,
    tenant: str,
    session_id: str,
    seq: int,
    ts: float,
    event_type: str,
    payload: Mapping[str, Any],
    prev_hash: str,
) -> str:
    body = {
        "tenant": tenant,
        "session_id": session_id,
        "seq": int(seq),
        "ts": float(ts),
        "type": event_type,
        "payload": dict(payload),
        "prev_hash": prev_hash,
    }
    return sha256_hex(canonical_json(body))


def token_hash(token: str) -> str:
    return sha256_hex(token)


def _clip(text: Any, limit: int = 400) -> str:
    value = "" if text is None else str(text)
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def summarize(event_type: str, payload: Mapping[str, Any]) -> str:
    """A short, human-readable line for the live feed and session list."""
    p = payload or {}
    if event_type == "llm.pre":
        return "用户：" + _clip(p.get("user_message"), 360)
    if event_type == "llm.post":
        return "助手：" + _clip(p.get("assistant_response"), 360)
    if event_type == "api.pre":
        model = p.get("model") or "?"
        count = p.get("message_count")
        tokens = p.get("approx_input_tokens")
        return f"模型请求 · {model} · {count or '?'} 条消息 · ~{tokens or '?'} tokens"
    if event_type == "api.post":
        model = p.get("response_model") or p.get("model") or "?"
        finish = p.get("finish_reason") or "?"
        usage = p.get("usage") or {}
        total = usage.get("total_tokens") if isinstance(usage, dict) else None
        return f"模型返回 · {model} · {finish}" + (f" · {total} tokens" if total else "")
    if event_type == "tool.pre":
        return f"工具调用 · {p.get('tool') or '?'} · " + _clip(p.get("args"), 260)
    if event_type == "tool.post":
        tool = p.get("tool") or "?"
        duration = p.get("duration_ms")
        error = " · 失败" if p.get("is_error") or p.get("error") else ""
        return f"工具结果 · {tool}" + (f" · {duration}ms" if duration else "") + error + " · " + _clip(p.get("result"), 220)
    if event_type == "session.start":
        return f"会话开始 · {p.get('model') or '?'} · {p.get('platform') or 'cli'}"
    if event_type == "session.end":
        status = "中断" if p.get("interrupted") else ("完成" if p.get("completed", True) else "失败")
        return f"会话结束 · {status} · {p.get('model') or '?'}"
    try:
        return _clip(canonical_json(p), 400)
    except (TypeError, ValueError):
        return _clip(p, 400)


def payload_bytes(payload: Mapping[str, Any]) -> int:
    return len(canonical_json(payload).encode("utf-8"))
