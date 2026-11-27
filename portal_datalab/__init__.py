"""Hermes tool bridge for the portal DataLab worker.

The tenant never runs analytical Python. This tool forwards a read-only
SELECT/WITH query to the portal; the portal validates the conversation
workspace and calls the isolated DataLab container.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger("hermes.datalab")

TOOLSET = "datalab"


def _enabled() -> bool:
    return bool(
        os.environ.get("HERMES_PORTAL_URL", "").strip()
        and os.environ.get("HERMES_PORTAL_TOKEN", "").strip()
    )


def _error(message: str) -> str:
    return json.dumps({"success": False, "error": message}, ensure_ascii=False)


def _call_portal(payload: dict[str, Any]) -> str:
    base = os.environ.get("HERMES_PORTAL_URL", "").rstrip("/")
    token = os.environ.get("HERMES_PORTAL_TOKEN", "").strip()
    if not base or not token:
        return _error("DataLab is not configured for this tenant")
    request = urllib.request.Request(
        base + "/api/datalab/query",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "hermes-datalab/0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read().decode("utf-8", "replace")
            data = json.loads(raw or "{}")
            return json.dumps({"success": True, **data}, ensure_ascii=False)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return _error(f"DataLab HTTP {exc.code}: {body[:800]}")
    except Exception as exc:
        logger.warning("datalab request failed: %s", exc)
        return _error(f"DataLab request failed: {exc}")


def _datalab_query(
    args: dict[str, Any],
    conversation_id: str | None = None,
    session_id: str | None = None,
    **_: Any,
) -> str:
    cid = (conversation_id or session_id or "").strip()
    if not cid:
        return _error("No conversation id in this tool context")
    tables = args.get("tables")
    sql = str(args.get("sql") or "").strip()
    limit = args.get("limit", 200)
    if not isinstance(tables, list) or not tables:
        return _error("tables must be a non-empty array")
    if not sql:
        return _error("sql is required")
    normalized: list[dict[str, Any]] = []
    for item in tables[:10]:
        if not isinstance(item, dict) or not str(item.get("path") or "").strip():
            return _error("each table needs a path")
        normalized.append(
            {
                "path": str(item.get("path")).strip(),
                "table": str(item.get("table") or "").strip(),
                "sheet": item.get("sheet"),
            }
        )
    try:
        limit_value = int(limit)
    except (TypeError, ValueError):
        limit_value = 200
    payload: dict[str, Any] = {
        "conversation_id": cid,
        "tables": normalized,
        "sql": sql,
        "limit": max(1, min(2000, limit_value)),
    }
    chart = args.get("chart")
    if isinstance(chart, dict):
        payload["chart"] = chart
    return _call_portal(payload)


def _call_portal_get(path: str, params: dict[str, Any]) -> str:
    base = os.environ.get("HERMES_PORTAL_URL", "").rstrip("/")
    token = os.environ.get("HERMES_PORTAL_TOKEN", "").strip()
    if not base or not token:
        return _error("DataLab is not configured for this tenant")
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        base + path + ("?" + query if query else ""),
        method="GET",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "hermes-datalab/0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8", "replace") or "{}")
            return json.dumps({"success": True, **data}, ensure_ascii=False)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return _error(f"DataLab HTTP {exc.code}: {body[:800]}")
    except Exception as exc:
        logger.warning("datalab DB list failed: %s", exc)
        return _error(f"DataLab request failed: {exc}")


def _datalab_databases(
    args: dict[str, Any],
    conversation_id: str | None = None,
    session_id: str | None = None,
    **_: Any,
) -> str:
    cid = (conversation_id or session_id or "").strip()
    if not cid:
        return _error("No conversation id in this tool context")
    return _call_portal_get("/api/datalab/databases", {"conversation_id": cid})


def _datalab_db_query(
    args: dict[str, Any],
    conversation_id: str | None = None,
    session_id: str | None = None,
    **_: Any,
) -> str:
    cid = (conversation_id or session_id or "").strip()
    if not cid:
        return _error("No conversation id in this tool context")
    connection = str(args.get("connection") or "").strip()
    sql = str(args.get("sql") or "").strip()
    if not connection:
        return _error("connection is required; call datalab_databases first")
    if not sql:
        return _error("sql is required")
    try:
        limit = max(1, min(2000, int(args.get("limit", 200))))
    except (TypeError, ValueError):
        limit = 200
    base = os.environ.get("HERMES_PORTAL_URL", "").rstrip("/")
    token = os.environ.get("HERMES_PORTAL_TOKEN", "").strip()
    if not base or not token:
        return _error("DataLab is not configured for this tenant")
    request = urllib.request.Request(
        base + "/api/datalab/db-query",
        data=json.dumps(
            {"conversation_id": cid, "connection": connection, "sql": sql, "limit": limit},
            ensure_ascii=False,
        ).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "hermes-datalab/0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8", "replace") or "{}")
            return json.dumps({"success": True, **data}, ensure_ascii=False)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return _error(f"DataLab HTTP {exc.code}: {body[:800]}")
    except Exception as exc:
        logger.warning("datalab DB query failed: %s", exc)
        return _error(f"DataLab request failed: {exc}")


def register(ctx) -> None:
    ctx.register_tool(
        name="datalab_query",
        toolset=TOOLSET,
        schema={
            "name": "datalab_query",
            "description": (
                "Run a read-only SELECT/WITH query over CSV/TSV/Excel/JSON/Parquet files in the "
                "current conversation workspace using the server-side DataLab worker (pandas + DuckDB). "
                "Register each file in `tables` with its conversation-workspace path and a safe table name, "
                "then query those table names. The worker never executes Python and cannot write files. "
                "Use this instead of trying to install pandas or connect to a database from the tenant."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tables": {
                        "type": "array",
                        "description": "Files to load; path may be /uploads/<tenant>/<conversation>/... or relative to the uploads root.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "table": {"type": "string", "description": "SQL table name, e.g. orders"},
                                "sheet": {
                                    "type": ["string", "integer"],
                                    "description": "Optional Excel sheet name or index",
                                },
                            },
                            "required": ["path"],
                        },
                    },
                    "sql": {
                        "type": "string",
                        "description": "One read-only SELECT or WITH query over the registered table names.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum result rows to return (default 200, max 2000).",
                        "minimum": 1,
                        "maximum": 2000,
                    },
                    "chart": {
                        "type": "object",
                        "description": "Optional chart generated from the query result. The result includes chart_file.url.",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["bar", "line", "pie", "scatter"],
                            },
                            "x": {"type": "string", "description": "X column name"},
                            "y": {
                                "type": ["string", "array"],
                                "items": {"type": "string"},
                                "description": "One y column or a list of y columns",
                            },
                            "title": {"type": "string"},
                        },
                        "required": ["x"],
                    },
                },
                "required": ["tables", "sql"],
            },
        },
        handler=lambda args, **kwargs: _datalab_query(args, **kwargs),
        check_fn=_enabled,
        emoji="📊",
    )
    ctx.register_tool(
        name="datalab_databases",
        toolset=TOOLSET,
        schema={
            "name": "datalab_databases",
            "description": (
                "List server-side database connections allowed for the current portal user. "
                "Returns only id, type and description — never credentials. Call this before "
                "datalab_db_query; if a database is absent, the user has no permission for it."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        handler=lambda args, **kwargs: _datalab_databases(args, **kwargs),
        check_fn=_enabled,
        emoji="🗄️",
    )
    ctx.register_tool(
        name="datalab_db_query",
        toolset=TOOLSET,
        schema={
            "name": "datalab_db_query",
            "description": (
                "Run one read-only SELECT/WITH query against an allowed server-side database "
                "connection. Credentials never enter the tenant. Roles are enforced by the portal "
                "and worker; a denied connection returns an error with no data."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "connection": {"type": "string", "description": "Connection id from datalab_databases"},
                    "sql": {"type": "string", "description": "One SELECT or WITH query"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 2000,
                        "description": "Maximum result rows (default 200)",
                    },
                },
                "required": ["connection", "sql"],
            },
        },
        handler=lambda args, **kwargs: _datalab_db_query(args, **kwargs),
        check_fn=_enabled,
        emoji="🗄️",
    )
