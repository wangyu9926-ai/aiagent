"""Portal-scoped memory tools.

The tenant's native memory is tenant-global; in a shared business tenant that
would leak one colleague's facts into another's context. These tools talk to the
portal instead, where private memories are keyed by the conversation owner and
team memories are tenant-wide.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger("hermes.portal_memory")
TOOLSET = "portal_memory"


def _enabled() -> bool:
    return bool(
        os.environ.get("HERMES_PORTAL_URL", "").strip()
        and os.environ.get("HERMES_PORTAL_TOKEN", "").strip()
    )


def _error(message: str) -> str:
    return json.dumps({"success": False, "error": message}, ensure_ascii=False)


def _base() -> tuple[str, str]:
    return (
        os.environ.get("HERMES_PORTAL_URL", "").rstrip("/"),
        os.environ.get("HERMES_PORTAL_TOKEN", "").strip(),
    )


def _request(method: str, path: str, payload: dict[str, Any] | None = None) -> str:
    base, token = _base()
    if not base or not token:
        return _error("Portal memory is not configured for this tenant")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "hermes-portal-memory/0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8", "replace") or "{}")
            return json.dumps({"success": True, **body}, ensure_ascii=False)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return _error(f"Portal memory HTTP {exc.code}: {body[:800]}")
    except Exception as exc:
        logger.warning("portal memory request failed: %s", exc)
        return _error(f"Portal memory request failed: {exc}")


def _cid(conversation_id: str | None, session_id: str | None) -> str:
    return (conversation_id or session_id or "").strip()


def _memory_list(args: dict[str, Any], conversation_id: str | None = None, session_id: str | None = None, **_: Any) -> str:
    cid = _cid(conversation_id, session_id)
    if not cid:
        return _error("No conversation id in this tool context")
    return _request("GET", "/api/agent/memories?" + urllib.parse.urlencode({"conversation_id": cid}))


def _memory_add(args: dict[str, Any], conversation_id: str | None = None, session_id: str | None = None, **_: Any) -> str:
    cid = _cid(conversation_id, session_id)
    if not cid:
        return _error("No conversation id in this tool context")
    content = str(args.get("content") or "").strip()
    scope = str(args.get("scope") or "private").strip().lower()
    if not content:
        return _error("content is required")
    if scope not in {"private", "team"}:
        return _error("scope must be private or team")
    return _request(
        "POST",
        "/api/agent/memories",
        {"conversation_id": cid, "content": content[:4000], "scope": scope},
    )


def register(ctx) -> None:
    ctx.register_tool(
        name="memory_list",
        toolset=TOOLSET,
        schema={
            "name": "memory_list",
            "description": (
                "List portal-scoped memories for this conversation: the owner's private memories "
                "plus team-wide memories. Native tenant memory is disabled in shared business tenants."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        handler=lambda args, **kwargs: _memory_list(args, **kwargs),
        check_fn=_enabled,
        emoji="🧠",
    )
    ctx.register_tool(
        name="memory_add",
        toolset=TOOLSET,
        schema={
            "name": "memory_add",
            "description": (
                "Save one durable fact. scope=private (default) is visible only to the conversation "
                "owner; scope=team is visible to everyone in this tenant and requires an admin owner."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "One concise durable fact"},
                    "scope": {
                        "type": "string",
                        "enum": ["private", "team"],
                        "description": "private (default) or team",
                    },
                },
                "required": ["content"],
            },
        },
        handler=lambda args, **kwargs: _memory_add(args, **kwargs),
        check_fn=_enabled,
        emoji="🧠",
    )
