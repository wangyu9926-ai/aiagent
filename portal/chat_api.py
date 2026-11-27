"""Member chat proxy, tenant runtime configuration, and user/tenant grants.

The router is mounted by ``portal.app``. It deliberately keeps its own small
dependency helpers so the chat surface can evolve without touching the audit
spine's route table.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator, Literal, Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from portal import auth, db

router = APIRouter()

MAX_FILES = 5
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TOTAL_UPLOAD_BYTES = 50 * 1024 * 1024
BLOCKED_EXTENSIONS = {
    "exe", "dll", "msi", "bat", "cmd", "com", "scr", "ps1", "vbs", "jar",
}
HEARTBEAT_MAX_AGE_SECONDS = 180
LOCAL_FOLDER_MAX_FILES = 500
LOCAL_FILE_MAX_BYTES = 50 * 1024 * 1024
LOCAL_FOLDER_MAX_BYTES = 200 * 1024 * 1024
LOCAL_BATCH_FILES = 20
LOCAL_SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", ".venv", "venv", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".next", ".cache",
}
LOCAL_SKIP_FILES = {
    ".env", ".env.local", ".env.production", ".env.development",
    "id_rsa", "id_ed25519", "credentials.json", "auth.json",
}
LOCAL_SKIP_SUFFIXES = (".pem", ".key", ".pfx", ".p12")
SKILLS_DIR = Path(__file__).resolve().parent / "skills"


def _skill_catalog() -> list[dict[str, Any]]:
    try:
        raw = json.loads((SKILLS_DIR / "catalog.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("skill catalog unavailable: %s", exc)
        return []
    return [item for item in raw if isinstance(item, dict) and item.get("id") and item.get("entry")]


def _visible_skills(conn, user) -> list[dict[str, Any]]:
    visible: list[dict[str, Any]] = []
    for skill in _skill_catalog():
        roles = skill.get("roles") or []
        if roles and user["role"] not in roles:
            continue
        tenant_ids = skill.get("tenants") or []
        if tenant_ids and user["role"] != "admin":
            if not any(db.user_can_access(conn, user["user_id"], tenant_id) for tenant_id in tenant_ids):
                continue
        visible.append(
            {
                "id": skill["id"],
                "name": skill.get("name") or skill["id"],
                "description": skill.get("description") or "",
                "category": skill.get("category") or "",
            }
        )
    return visible


def _skill_content(skill: dict[str, Any]) -> str:
    entry = PurePosixPath(str(skill.get("entry") or ""))
    if entry.is_absolute() or ".." in entry.parts:
        raise HTTPException(status_code=500, detail="技能文件路径不合法")
    path = (SKILLS_DIR / Path(*entry.parts)).resolve()
    if not path.is_relative_to(SKILLS_DIR.resolve()) or not path.is_file():
        raise HTTPException(status_code=404, detail="技能内容不存在")
    return path.read_text(encoding="utf-8")[:50_000]


def _skill_content_for_id(skill_id: str) -> str:
    for item in _skill_catalog():
        if item.get("id") == skill_id:
            return _skill_content(item)
    raise HTTPException(status_code=404, detail="技能不存在")

logger = logging.getLogger("portal.chat")
_background_tasks: set[asyncio.Task] = set()


async def _stop_disconnected_run(
    base_url: str, api_key: str, run_id: str, transport: Any = None
) -> None:
    """Best-effort stop when the browser drops the SSE connection mid-run.

    Without this the tenant keeps executing (and may park on an approval whose
    browser is gone) after the portal has stopped forwarding the stream.
    """
    logger.warning("stopping run %s after the browser disconnected", run_id)
    try:
        async with httpx.AsyncClient(timeout=10.0, transport=transport) as client:
            response = await client.post(
                base_url.rstrip("/") + f"/v1/runs/{run_id}/stop",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={},
            )
        if response.status_code >= 400:
            logger.warning("stop disconnected run %s failed: HTTP %s", run_id, response.status_code)
    except Exception as exc:  # the client is already gone; never surface this as chat text
        logger.warning("stop disconnected run %s failed: %s", run_id, exc)


@dataclass
class ChatRun:
    """A tenant run that outlives the browser connection that started it."""

    conversation_id: str
    started_at: float = field(default_factory=time.time)
    status: str = "running"  # running / done / error / interrupted
    error: str = ""
    task: Any = None
    subscribers: set[asyncio.Queue] = field(default_factory=set)

    def publish(self, payload: str) -> None:
        for queue in tuple(self.subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # A slow browser only loses its own live view; the run and the DB persist.
                self.subscribers.discard(queue)

    def finish(self, status: str, error: str = "") -> None:
        self.status = status
        self.error = error
        for queue in tuple(self.subscribers):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        self.subscribers.clear()


async def _run_agent_stream(
    *,
    run: ChatRun,
    upstream_url: str,
    base_url: str,
    api_key: str,
    model: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    cfg: Any,
    transport: Any,
    persist: Any,
    persist_approval: Any,
) -> None:
    """Consume the tenant SSE independently of any browser request."""
    parts: list[str] = []
    usage: dict[str, Any] | None = None
    persisted = False
    current_event = ""
    active_run_id = ""
    approval_run_ids: set[str] = set()
    status = "done"
    error = ""

    def emit(payload: str) -> None:
        run.publish(payload)

    try:
        timeout = httpx.Timeout(connect=10.0, read=1800.0, write=30.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            async with client.stream("POST", upstream_url, headers=headers, json=payload) as response:
                if response.status_code >= 400:
                    raw = (await response.aread()).decode("utf-8", "replace")
                    message = f"运行环境返回 {response.status_code}: {raw[:800]}"
                    emit(_sse_error(message))
                    persist("error", message, {"model": model})
                    persisted = True
                    status, error = "error", message
                    return
                async for line in response.aiter_lines():
                    if line == "":
                        current_event = ""
                        emit(chr(10))
                        continue
                    if line.startswith("event:"):
                        current_event = line[6:].strip()
                    emit(line + chr(10))
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(chunk, dict):
                        if not active_run_id:
                            candidate = chunk.get("run_id") or chunk.get("id")
                            if isinstance(candidate, str) and candidate.startswith(("chatcmpl-", "run_")):
                                active_run_id = candidate
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                        if current_event == "approval.request" or chunk.get("type") == "approval.request":
                            approval_run_ids.add(str(chunk.get("run_id") or active_run_id or ""))
                            persist_approval(chunk)
                            continue
                    text = _chunk_text(chunk)
                    if text:
                        parts.append(text)
    except asyncio.CancelledError:
        status = "interrupted"
        if active_run_id:
            task = asyncio.create_task(
                _stop_disconnected_run(base_url, api_key, active_run_id, transport)
            )
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
    except Exception as exc:
        message = f"连接运行环境失败：{type(exc).__name__}: {exc}"
        emit(_sse_error(message))
        persist("error", message, {"model": model})
        persisted = True
        status, error = "error", message
    finally:
        if not persisted:
            text = "".join(parts)
            if text:
                persist("assistant", text, {"model": model, "usage": usage})
            elif status == "done":
                message = "运行环境没有返回内容"
                persist("error", message, {"model": model})
                status, error = "error", message
            elif status == "interrupted":
                persist("error", "任务已中断（门户或运行环境中断）", {"model": model})
        if approval_run_ids:
            c = db.connect(cfg.db_path)
            try:
                for run_id in approval_run_ids:
                    db.expire_pending_for_run(c, run_id)
            finally:
                c.close()
        run.finish(status, error)


def _get_conn(request: Request):
    conn = db.connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def current_user(request: Request, conn=Depends(_get_conn)) -> dict[str, Any]:
    user = db.get_session(conn, request.cookies.get(auth.COOKIE_NAME, ""))
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return user


def require_admin(user=Depends(current_user)) -> dict[str, Any]:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def check_csrf(request: Request, user: dict[str, Any]) -> None:
    supplied = request.headers.get(auth.CSRF_HEADER, "")
    if not auth.constant_time_equal(supplied, user["csrf"]):
        raise HTTPException(status_code=403, detail="CSRF 校验失败，请刷新页面后重试")


class RuntimeIn(BaseModel):
    base_url: str = Field(min_length=4, max_length=500)
    api_key: str = Field(default="", max_length=500)
    model: str = Field(default="hermes-agent", min_length=1, max_length=120)


class GrantIn(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    access_role: Literal["member", "viewer"] = "member"


class ConversationIn(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    title: Optional[str] = Field(default=None, max_length=80)


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    tenant_id: str = Field(min_length=1, max_length=64)
    goal: str = Field(min_length=1, max_length=4000)
    repo_url: str = Field(default="", max_length=500)
    known: str = Field(default="", max_length=4000)
    unknowns: str = Field(default="", max_length=4000)


class LocalFolderIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class DatalabTableIn(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    table: str = Field(default="", max_length=80)
    sheet: str | int | None = None


class DatalabQueryIn(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=64)
    tables: list[DatalabTableIn] = Field(min_length=1, max_length=10)
    sql: str = Field(min_length=1, max_length=50_000)
    limit: int = Field(default=200, ge=1, le=2000)


class ChatIn(BaseModel):
    content: str = Field(min_length=1, max_length=32000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=5)
    skill_ids: list[str] = Field(default_factory=list, max_length=5)


class ApprovalIn(BaseModel):
    choice: Literal["once", "session", "always", "deny"]


def _clean_base_url(value: str) -> str:
    cleaned = (value or "").strip().rstrip("/")
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("运行环境地址必须是 http:// 或 https:// 开头的完整 URL")
    return cleaned


def _sse_error(message: str) -> str:
    return "data: " + json.dumps({"error": {"message": message}}, ensure_ascii=False) + "\n\n"


def _chunk_text(chunk: Any) -> str:
    if not isinstance(chunk, dict):
        return ""
    choices = chunk.get("choices") or []
    if not choices:
        return ""
    choice = choices[0] or {}
    if not isinstance(choice, dict):
        return ""
    delta = choice.get("delta") or {}
    text = delta.get("content")
    if text is None:
        message = choice.get("message") or {}
        text = message.get("content")
    if isinstance(text, str):
        return text
    if isinstance(text, list):
        return "".join(
            part.get("text", "")
            for part in text
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


# ---------------------------------------------------------------------------
# Tenant runtime configuration (admin)
# ---------------------------------------------------------------------------


@router.get("/api/tenants/{tenant_id}/runtime")
def get_runtime(tenant_id: str, user=Depends(require_admin), conn=Depends(_get_conn)):
    if conn.execute("SELECT 1 FROM tenants WHERE id = ?", (tenant_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="租户不存在")
    runtime = db.get_runtime(conn, tenant_id)
    if runtime is None:
        return {"configured": False, "tenant_id": tenant_id}
    return {
        "configured": True,
        "tenant_id": tenant_id,
        "base_url": runtime["base_url"],
        "model": runtime["model"],
        "updated_at": runtime["updated_at"],
        "last_ok_at": runtime["last_ok_at"],
        "last_error": runtime["last_error"],
        "has_key": bool(runtime["api_key_enc"]),
    }


@router.put("/api/tenants/{tenant_id}/runtime")
def put_runtime(
    tenant_id: str,
    body: RuntimeIn,
    request: Request,
    user=Depends(require_admin),
    conn=Depends(_get_conn),
):
    check_csrf(request, user)
    if conn.execute("SELECT 1 FROM tenants WHERE id = ?", (tenant_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="租户不存在")
    try:
        base_url = _clean_base_url(body.base_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    existing = db.get_runtime(conn, tenant_id)
    if body.api_key.strip():
        encrypted = request.app.state.secret_box.encrypt(body.api_key.strip())
    elif existing is not None:
        encrypted = existing["api_key_enc"]
    else:
        raise HTTPException(status_code=400, detail="首次配置需要填写 API Key")
    db.set_runtime(conn, tenant_id, base_url=base_url, api_key_enc=encrypted, model=body.model)
    return {"ok": True, "tenant_id": tenant_id, "base_url": base_url, "model": body.model}


@router.post("/api/tenants/{tenant_id}/runtime/test")
def test_runtime(
    tenant_id: str,
    request: Request,
    user=Depends(require_admin),
    conn=Depends(_get_conn),
):
    check_csrf(request, user)
    runtime = db.get_runtime(conn, tenant_id)
    if runtime is None:
        raise HTTPException(status_code=404, detail="该租户还没有配置运行环境")
    try:
        api_key = request.app.state.secret_box.decrypt(runtime["api_key_enc"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    error = None
    try:
        with httpx.Client(timeout=6.0, transport=request.app.state.http_transport) as client:
            response = client.get(
                runtime["base_url"].rstrip("/") + "/v1/models",
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            )
        ok = response.status_code < 400
        if not ok:
            error = f"HTTP {response.status_code}: {response.text[:300]}"
    except Exception as exc:
        ok = False
        error = f"{type(exc).__name__}: {exc}"
    db.mark_runtime_test(conn, tenant_id, ok=ok, error=error)
    return {"ok": ok, "error": error}


# ---------------------------------------------------------------------------
# User -> tenant grants (admin)
# ---------------------------------------------------------------------------


@router.get("/api/users/{user_id}/tenants")
def list_user_grants(user_id: int, user=Depends(require_admin), conn=Depends(_get_conn)):
    if db.get_user(conn, user_id) is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"grants": db.user_grants(conn, user_id)}


@router.post("/api/users/{user_id}/tenants")
def grant_user_tenant(
    user_id: int,
    body: GrantIn,
    request: Request,
    user=Depends(require_admin),
    conn=Depends(_get_conn),
):
    check_csrf(request, user)
    try:
        db.grant_tenant(conn, user_id, body.tenant_id, body.access_role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@router.delete("/api/users/{user_id}/tenants/{tenant_id}")
def revoke_user_tenant(
    user_id: int,
    tenant_id: str,
    request: Request,
    user=Depends(require_admin),
    conn=Depends(_get_conn),
):
    check_csrf(request, user)
    if not db.revoke_tenant(conn, user_id, tenant_id):
        raise HTTPException(status_code=404, detail="授权不存在")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Admin approvals
# ---------------------------------------------------------------------------


@router.get("/api/approvals")
def list_approvals(
    status: str = "pending",
    tenant_id: Optional[str] = None,
    limit: int = 100,
    user=Depends(require_admin),
    conn=Depends(_get_conn),
):
    return {
        "approvals": db.list_approvals(
            conn,
            status=None if status == "all" else status,
            tenant_id=tenant_id or None,
            limit=limit,
        )
    }


@router.post("/api/approvals/{approval_id}")
def decide_approval(
    approval_id: str,
    body: ApprovalIn,
    request: Request,
    user=Depends(require_admin),
    conn=Depends(_get_conn),
):
    check_csrf(request, user)
    approval = db.get_approval(conn, approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="审批不存在")
    if approval["status"] != "pending":
        raise HTTPException(status_code=409, detail="该审批已经处理或已过期")
    runtime = db.get_runtime(conn, approval["tenant_id"])
    if runtime is None:
        raise HTTPException(status_code=409, detail="该租户没有运行环境")
    try:
        api_key = request.app.state.secret_box.decrypt(runtime["api_key_enc"])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    base_url = runtime["base_url"].rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    request_id = str(approval.get("request_id") or "")
    try:
        with httpx.Client(timeout=30.0, transport=request.app.state.http_transport) as client:
            # Current Hermes resolves approvals through a generation-fenced session control.
            # Its exact payload is {request_id, execution_generation, choice}; the generation
            # is reported by the run projection (the SSE approval event does not carry it).
            run_response = client.get(f"{base_url}/v1/runs/{approval['run_id']}", headers=headers)
            run_status = run_response.json() if run_response.status_code == 200 else {}
            generation = run_status.get("execution_generation") if isinstance(run_status, dict) else None
            pending = run_status.get("pending_controls") if isinstance(run_status, dict) else None
            if isinstance(pending, list) and request_id:
                still_pending = any(
                    isinstance(prompt, dict)
                    and (prompt.get("prompt_id") == request_id or prompt.get("request_id") == request_id)
                    for prompt in pending
                )
                if not still_pending:
                    db.expire_pending_for_run(conn, approval["run_id"])
                    raise HTTPException(status_code=409, detail="运行环境里这个审批已经过期，请刷新")
            payload: dict[str, Any] = {"choice": body.choice}
            if request_id:
                payload["request_id"] = request_id
            if isinstance(generation, int):
                payload["execution_generation"] = generation
            response = client.post(
                f"{base_url}/v1/runs/{approval['run_id']}/approval",
                headers=headers,
                json=payload,
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"连接运行环境失败：{exc}") from exc
    if response.status_code >= 400:
        if response.status_code in {404, 409}:
            db.expire_pending_for_run(conn, approval["run_id"])
        raise HTTPException(
            status_code=502,
            detail=f"运行环境拒绝审批：HTTP {response.status_code} {response.text[:300]}",
        )
    updated = db.resolve_approval(conn, approval_id, choice=body.choice, decided_by=user["user_id"])
    if updated is None:
        raise HTTPException(status_code=409, detail="审批状态已变化，请刷新")
    return {"ok": True, "approval": updated}


# ---------------------------------------------------------------------------
# Server-side skill library
# ---------------------------------------------------------------------------


@router.get("/api/skills")
def skills_list(user=Depends(current_user), conn=Depends(_get_conn)):
    return {"skills": _visible_skills(conn, user)}


@router.get("/api/assets")
def assets_overview(user=Depends(require_admin), conn=Depends(_get_conn)):
    assets = {item["tenant_id"]: item for item in db.list_tenant_assets(conn)}
    tenants: list[dict[str, Any]] = []
    for tenant in db.list_tenants(conn):
        asset = assets.get(tenant["id"], {})
        heartbeat = db.get_heartbeat(conn, tenant["id"])
        tenants.append(
            {
                "tenant_id": tenant["id"],
                "tenant_name": tenant["name"],
                "disabled": bool(tenant["disabled"]),
                "last_seen": heartbeat["last_seen"] if heartbeat else None,
                "skill_count": int(asset.get("skill_count") or 0),
                "memory_count": int(asset.get("memory_count") or 0),
                "recent_skills": asset.get("recent_skills") or [],
                "updated_at": asset.get("updated_at"),
            }
        )
    return {"server_skills": _visible_skills(conn, user), "tenants": tenants}


@router.get("/api/skills/{skill_id}")
def skill_detail(skill_id: str, user=Depends(current_user), conn=Depends(_get_conn)):
    visible = {item["id"]: item for item in _visible_skills(conn, user)}
    skill = visible.get(skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="技能不存在或没有权限")
    for item in _skill_catalog():
        if item.get("id") == skill_id:
            return {**skill, "content": _skill_content(item)}
    raise HTTPException(status_code=404, detail="技能不存在")


# ---------------------------------------------------------------------------
# DataLab proxy (tenant-token authenticated)
# ---------------------------------------------------------------------------


@router.post("/api/datalab/query")
async def datalab_query(
    body: DatalabQueryIn,
    request: Request,
    conn=Depends(_get_conn),
):
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer 租户令牌")
    tenant = db.get_tenant_by_token(conn, header[7:].strip())
    if tenant is None:
        raise HTTPException(status_code=401, detail="租户令牌无效或已停用")
    conversation = db.get_conversation(conn, body.conversation_id)
    if conversation is None or conversation["tenant_id"] != tenant["id"]:
        raise HTTPException(status_code=404, detail="会话不存在或不属于该租户")
    settings = request.app.state.settings
    if not settings.datalab_url or not settings.datalab_token:
        raise HTTPException(status_code=503, detail="DataLab 尚未配置")
    upload_root = settings.upload_dir or (settings.data_dir / "uploads")
    workspace = (upload_root / tenant["id"] / conversation["id"]).resolve()
    tables: list[dict[str, Any]] = []
    for index, item in enumerate(body.tables):
        raw = item.path.replace("\\", "/").strip()
        if raw.startswith("/uploads/"):
            raw = raw[len("/uploads/"):]
        elif raw.startswith("/"):
            raise HTTPException(status_code=400, detail="DataLab 只接受会话工作区内的文件")
        pure = PurePosixPath(raw)
        if pure.is_absolute() or ".." in pure.parts or not pure.parts:
            raise HTTPException(status_code=400, detail="DataLab 文件路径不合法")
        full = (upload_root / Path(*pure.parts)).resolve()
        if not full.is_relative_to(workspace):
            raise HTTPException(status_code=403, detail="DataLab 只能读取该会话工作区")
        if not full.is_file():
            raise HTTPException(status_code=404, detail=f"DataLab 文件不存在：{pure.as_posix()}")
        tables.append(
            {
                "path": pure.as_posix(),
                "table": item.table or f"t{index + 1}",
                "sheet": item.sheet,
            }
        )
    payload = {"tables": tables, "sql": body.sql, "limit": body.limit}
    try:
        async with httpx.AsyncClient(
            timeout=90.0, transport=request.app.state.http_transport
        ) as client:
            response = await client.post(
                settings.datalab_url.rstrip("/") + "/query",
                headers={
                    "X-Datalab-Token": settings.datalab_token,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"DataLab 连接失败：{exc}") from exc
    if response.status_code >= 400:
        raise HTTPException(
            status_code=400 if response.status_code < 500 else 502,
            detail=f"DataLab 拒绝查询：{response.text[:600]}",
        )
    return response.json()


# ---------------------------------------------------------------------------
# Projects / onboarding
# ---------------------------------------------------------------------------


def _project_report_path(settings: Any, project: dict[str, Any]) -> Path:
    upload_root = settings.upload_dir or (settings.data_dir / "uploads")
    return upload_root / project["tenant_id"] / "projects" / project["id"] / "ONBOARDING.md"


def _project_prompt(project: dict[str, Any]) -> str:
    repo = project.get("repo_url") or "（未提供）"
    return f"""你现在接手项目「{project['name']}」。目标不是马上改代码，而是用最少时间摸清项目并产出可执行的接手报告。

工作区（租户内路径）：{project['workspace']}
先读取工作区根目录的 PROJECT.md。

已知条件：
{project.get('known') or '（未提供）'}

需要重点查清的未知点：
{project.get('unknowns') or '（未提供）'}

代码/入口：{repo}

按顺序做：
1. 如果代码/入口是 Git 地址，且工作区 repo/ 为空，把仓库 clone 到 {project['workspace']}/repo；如果已有文件，不要覆盖，先判断现状。如果是普通网址，用 browser 工具打开了解。
2. 盘点目录结构、README、依赖清单、配置样例、启动/测试脚本、CI、数据库/外部服务线索。
3. 找出 3–7 个关键入口文件和主要调用链，说明它们如何串起来。
4. 在安全、只读、非生产的前提下实际运行可用的检查或测试；没有条件运行就说明缺什么，不要假装运行过。
5. 输出《项目接手报告》：目标复述、架构与数据流、本地运行方式、关键文件、外部依赖、风险、未知项、建议的前 3 个任务（按优先级）。
6. 把完整报告写到 {project['workspace']}/ONBOARDING.md，然后在回复里给出摘要。

硬性规则：
- 不读取、不输出 .env、auth.json、config.yaml、state.db、sessions、plugins 等敏感文件内容；需要配置时只列变量名。
- 不执行破坏性命令，不碰生产环境；安装依赖前先说明用途。
- 命令或工具失败要如实报告，不要编造结果。
- 信息不足时先做合理假设并标注，只在确实阻塞时提出最多 3 个问题。
"""


@router.get("/api/projects")
def projects_list(
    request: Request,
    user=Depends(current_user),
    conn=Depends(_get_conn),
):
    projects = db.list_projects(conn)
    if user["role"] != "admin":
        projects = [
            project
            for project in projects
            if db.user_can_access(conn, user["user_id"], project["tenant_id"]) is not None
        ]
    settings = request.app.state.settings
    for project in projects:
        project["access_role"] = db.user_can_access(conn, user["user_id"], project["tenant_id"])
        report = _project_report_path(settings, project)
        project["report_exists"] = report.is_file()
        project["report_size"] = report.stat().st_size if report.is_file() else 0
        project["report_updated_at"] = report.stat().st_mtime if report.is_file() else None
    return {"projects": projects}


@router.post("/api/projects")
def projects_create(
    body: ProjectIn,
    request: Request,
    user=Depends(require_admin),
    conn=Depends(_get_conn),
):
    check_csrf(request, user)
    tenants = {tenant["id"]: tenant for tenant in db.list_tenants(conn)}
    tenant = tenants.get(body.tenant_id)
    if tenant is None or tenant["disabled"]:
        raise HTTPException(status_code=404, detail="租户不存在或已停用")
    settings = request.app.state.settings
    upload_root = settings.upload_dir or (settings.data_dir / "uploads")
    project_id = "prj_" + secrets.token_hex(8)
    workspace_host = upload_root / body.tenant_id / "projects" / project_id
    try:
        workspace_host.mkdir(parents=True, exist_ok=True)
        for name in ("repo", "input"):
            (workspace_host / name).mkdir(exist_ok=True)
        for path in (workspace_host, workspace_host / "repo", workspace_host / "input"):
            path.chmod(0o777)
        workspace = f"/uploads/{body.tenant_id}/projects/{project_id}"
        project_md = workspace_host / "PROJECT.md"
        project_md.write_text(
            "\n".join(
                [
                    f"# {body.name.strip()}",
                    "",
                    f"- 租户：{body.tenant_id}",
                    f"- 工作区：`{workspace}`",
                    f"- 代码/入口：{body.repo_url.strip() or '（未提供）'}",
                    "",
                    "## 目标",
                    body.goal.strip(),
                    "",
                    "## 已知条件",
                    body.known.strip() or "（未提供）",
                    "",
                    "## 未知点",
                    body.unknowns.strip() or "（未提供）",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        project_md.chmod(0o666)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"创建工作区失败：{exc}") from exc
    project = db.create_project(
        conn,
        name=body.name,
        tenant_id=body.tenant_id,
        workspace=workspace,
        created_by=user["user_id"],
        repo_url=body.repo_url,
        goal=body.goal,
        known=body.known,
        unknowns=body.unknowns,
        project_id=project_id,
    )
    project["access_role"] = "admin"
    return {"project": project}


@router.get("/api/projects/{project_id}/report")
def projects_report(
    project_id: str,
    request: Request,
    user=Depends(current_user),
    conn=Depends(_get_conn),
):
    project = db.get_project(conn, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    if db.user_can_access(conn, user["user_id"], project["tenant_id"]) is None:
        raise HTTPException(status_code=403, detail="没有该项目的访问权限")
    report = _project_report_path(request.app.state.settings, project)
    if not report.is_file():
        raise HTTPException(status_code=404, detail="接手报告还没有生成")
    try:
        content = report.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"读取报告失败：{exc}") from exc
    return {
        "project": {"id": project["id"], "name": project["name"]},
        "size": report.stat().st_size,
        "updated_at": report.stat().st_mtime,
        "content": content,
    }


@router.post("/api/projects/{project_id}/onboard")
def projects_onboard(
    project_id: str,
    request: Request,
    user=Depends(current_user),
    conn=Depends(_get_conn),
):
    check_csrf(request, user)
    project = db.get_project(conn, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    access = db.user_can_access(conn, user["user_id"], project["tenant_id"])
    if access is None:
        raise HTTPException(status_code=403, detail="没有该项目的访问权限")
    if user["role"] != "admin" and (user["role"] == "viewer" or access == "viewer"):
        raise HTTPException(status_code=403, detail="只读账号不能发起项目接手")
    if db.get_runtime(conn, project["tenant_id"]) is None:
        raise HTTPException(status_code=409, detail="该项目所在租户还没有配置运行环境")
    settings = request.app.state.settings
    upload_root = settings.upload_dir or (settings.data_dir / "uploads")
    workspace_host = upload_root / project["tenant_id"] / "projects" / project_id
    try:
        for name in ("", "repo", "input"):
            path = workspace_host / name if name else workspace_host
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(0o777)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"准备工作区失败：{exc}") from exc
    conversation = db.create_conversation(
        conn, project["tenant_id"], user["user_id"], f"项目接入 · {project['name']}"[:80]
    )
    db.touch_project(conn, project_id)
    return {"conversation": conversation, "prompt": _project_prompt(project)}


# ---------------------------------------------------------------------------
# Member chat
# ---------------------------------------------------------------------------


@router.get("/api/me/tenants")
def my_tenants(user=Depends(current_user), conn=Depends(_get_conn)):
    if user["role"] == "admin":
        tenants = [
            {
                "id": tenant["id"],
                "name": tenant["name"],
                "access_role": "admin",
                "runtime_base_url": tenant["runtime_base_url"],
                "runtime_model": tenant["runtime_model"],
                "runtime_ok_at": tenant["runtime_ok_at"],
                "runtime_error": tenant["runtime_error"],
            }
            for tenant in db.list_tenants(conn)
            if not tenant["disabled"]
        ]
    else:
        tenants = db.user_tenants(conn, user["user_id"])
    for tenant in tenants:
        tenant["runtime_ready"] = bool(tenant.get("runtime_base_url"))
    return {"tenants": tenants}


def _conversation_for(conn, user, conversation_id: str) -> dict[str, Any]:
    conversation = db.get_conversation(conn, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if conversation["user_id"] != user["user_id"] and user["role"] != "admin":
        raise HTTPException(status_code=404, detail="会话不存在")
    return conversation


@router.get("/api/chat/conversations")
def chat_conversations(
    tenant_id: str,
    user=Depends(current_user),
    conn=Depends(_get_conn),
):
    if db.user_can_access(conn, user["user_id"], tenant_id) is None:
        raise HTTPException(status_code=403, detail="没有该租户的访问权限")
    return {"conversations": db.list_conversations(conn, tenant_id, user["user_id"])}


@router.post("/api/chat/conversations")
def create_chat_conversation(
    body: ConversationIn,
    request: Request,
    user=Depends(current_user),
    conn=Depends(_get_conn),
):
    check_csrf(request, user)
    access = db.user_can_access(conn, user["user_id"], body.tenant_id)
    if access is None:
        raise HTTPException(status_code=403, detail="没有该租户的访问权限")
    if user["role"] != "admin" and (user["role"] == "viewer" or access == "viewer"):
        raise HTTPException(status_code=403, detail="只读账号不能创建会话")
    conversation = db.create_conversation(conn, body.tenant_id, user["user_id"], body.title or "新会话")
    return {"conversation": conversation}


@router.get("/api/chat/conversations/{conversation_id}/messages")
def chat_messages(
    conversation_id: str,
    user=Depends(current_user),
    conn=Depends(_get_conn),
):
    conversation = _conversation_for(conn, user, conversation_id)
    return {
        "conversation": conversation,
        "messages": db.list_messages(conn, conversation_id),
    }


def _safe_filename(value: str) -> str:
    name = Path(value or "file").name
    name = re.sub(r"[^\w.\- ]", "_", name).strip() or "file"
    if len(name) > 120:
        stem, dot, suffix = name.rpartition(".")
        keep = f"{dot}{suffix}" if stem and len(suffix) <= 12 else ""
        name = name[: max(1, 120 - len(keep))] + keep
    return name


def _local_root(settings: Any, conversation: dict[str, Any]) -> Path:
    upload_root = settings.upload_dir or (settings.data_dir / "uploads")
    return upload_root / conversation["tenant_id"] / conversation["id"] / "local"


def _safe_local_relpath(raw: str) -> str:
    text = str(raw or "").replace("\\", "/").strip()
    if not text:
        raise HTTPException(status_code=400, detail="本地文件路径为空")
    pure = PurePosixPath(text)
    if pure.is_absolute() or ".." in pure.parts:
        raise HTTPException(status_code=400, detail="本地文件路径不合法")
    parts = [part for part in pure.parts if part not in ("", ".")]
    if not parts or len(parts) > 32:
        raise HTTPException(status_code=400, detail="本地文件路径层级不合法")
    for part in parts:
        if len(part) > 160 or ":" in part or "\x00" in part:
            raise HTTPException(status_code=400, detail="本地文件名不合法")
        if part in LOCAL_SKIP_DIRS:
            raise HTTPException(status_code=400, detail=f"跳过目录：{part}")
    name = parts[-1].lower()
    if name in LOCAL_SKIP_FILES or name.endswith(LOCAL_SKIP_SUFFIXES):
        raise HTTPException(status_code=400, detail="为避免泄露密钥，已跳过敏感文件")
    return "/".join(parts)


def _local_file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _local_stats(root: Path) -> tuple[int, int]:
    count = 0
    total = 0
    if not root.is_dir():
        return count, total
    root_resolved = root.resolve()
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        if not path.resolve().is_relative_to(root_resolved):
            continue
        count += 1
        total += path.stat().st_size
    return count, total


def _local_manifest(root: Path) -> tuple[list[dict[str, Any]], int]:
    files: list[dict[str, Any]] = []
    total = 0
    if not root.is_dir():
        return files, total
    root_resolved = root.resolve()
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if not path.resolve().is_relative_to(root_resolved):
            continue
        relative = path.relative_to(root).as_posix()
        if any(part in LOCAL_SKIP_DIRS for part in PurePosixPath(relative).parts):
            continue
        file_name = PurePosixPath(relative).name.lower()
        if file_name in LOCAL_SKIP_FILES or file_name.endswith(LOCAL_SKIP_SUFFIXES):
            continue
        stat = path.stat()
        total += stat.st_size
        files.append(
            {
                "path": relative,
                "size": stat.st_size,
                "sha256": _local_file_hash(path),
                "updated_at": stat.st_mtime,
            }
        )
        if len(files) >= 2000:
            break
    return files, total


@router.post("/api/chat/conversations/{conversation_id}/attachments")
def upload_attachments(
    conversation_id: str,
    request: Request,
    files: list[UploadFile] = File(...),
    user=Depends(current_user),
    conn=Depends(_get_conn),
):
    check_csrf(request, user)
    conversation = _conversation_for(conn, user, conversation_id)
    access = db.user_can_access(conn, user["user_id"], conversation["tenant_id"])
    if access is None:
        raise HTTPException(status_code=403, detail="没有该租户的访问权限")
    if user["role"] != "admin" and (user["role"] == "viewer" or access == "viewer"):
        raise HTTPException(status_code=403, detail="只读账号不能上传附件")
    if not files:
        raise HTTPException(status_code=400, detail="没有选择文件")
    if len(files) > MAX_FILES:
        raise HTTPException(status_code=413, detail=f"一次最多上传 {MAX_FILES} 个文件")
    settings = request.app.state.settings
    upload_root = settings.upload_dir or (settings.data_dir / "uploads")
    target_dir = upload_root / conversation["tenant_id"] / conversation_id
    target_dir.mkdir(parents=True, exist_ok=True)
    saved: list[dict[str, Any]] = []
    saved_paths: list[Path] = []
    total = 0
    try:
        for upload in files:
            safe_name = _safe_filename(upload.filename or "file")
            extension = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else ""
            if extension in BLOCKED_EXTENSIONS:
                raise HTTPException(status_code=400, detail=f"不允许上传 .{extension} 文件")
            stored = target_dir / f"{__import__('uuid').uuid4().hex}_{safe_name}"
            size = 0
            with stored.open("wb") as output:
                while True:
                    chunk = upload.file.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    total += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise HTTPException(status_code=413, detail=f"{safe_name} 超过单文件 25 MB 限制")
                    if total > MAX_TOTAL_UPLOAD_BYTES:
                        raise HTTPException(status_code=413, detail="本次上传总大小超过 50 MB")
                    output.write(chunk)
            row = db.add_attachment(
                conn,
                conversation_id,
                user["user_id"],
                name=safe_name,
                stored_path=str(stored),
                size=size,
                mime=upload.content_type,
            )
            saved.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "size": row["size"],
                    "mime": row["mime"],
                }
            )
            saved_paths.append(stored)
    except Exception:
        for path in saved_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return {"attachments": saved}


# ---------------------------------------------------------------------------
# Local folder bridge
# ---------------------------------------------------------------------------


def _local_folder_payload(settings: Any, conversation: dict[str, Any]) -> dict[str, Any]:
    server_path = f"/uploads/{conversation['tenant_id']}/{conversation['id']}/local"
    return {"server_path": server_path}


@router.get("/api/chat/conversations/{conversation_id}/local-folder")
def local_folder_get(
    conversation_id: str,
    request: Request,
    user=Depends(current_user),
    conn=Depends(_get_conn),
):
    conversation = _conversation_for(conn, user, conversation_id)
    folder = db.get_local_folder(conn, conversation_id)
    if folder is None:
        return {"connected": False, "folder": None}
    root = _local_root(request.app.state.settings, conversation)
    if not root.is_dir():
        db.delete_local_folder(conn, conversation_id)
        return {"connected": False, "folder": None}
    return {
        "connected": True,
        "folder": {
            **folder,
            **_local_folder_payload(request.app.state.settings, conversation),
        },
    }


@router.post("/api/chat/conversations/{conversation_id}/local-folder")
def local_folder_connect(
    conversation_id: str,
    body: LocalFolderIn,
    request: Request,
    user=Depends(current_user),
    conn=Depends(_get_conn),
):
    check_csrf(request, user)
    conversation = _conversation_for(conn, user, conversation_id)
    if conversation["user_id"] != user["user_id"]:
        raise HTTPException(status_code=403, detail="不能给其他人的会话授权本地文件夹")
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="只读账号不能授权本地文件夹")
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="文件夹名不能为空")
    root = _local_root(request.app.state.settings, conversation)
    existing = db.get_local_folder(conn, conversation_id)
    file_count = 0
    total_bytes = 0
    if existing is not None and existing["name"] == name and root.is_dir():
        file_count = int(existing["file_count"])
        total_bytes = int(existing["total_bytes"])
    else:
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o777)
    except OSError:
        pass
    folder = db.set_local_folder(
        conn,
        conversation_id,
        name=name,
        server_path=_local_folder_payload(request.app.state.settings, conversation)["server_path"],
        file_count=file_count,
        total_bytes=total_bytes,
    )
    return {"connected": True, "folder": {**folder, **_local_folder_payload(request.app.state.settings, conversation)}}


@router.post("/api/chat/conversations/{conversation_id}/local-folder/upload")
async def local_folder_upload(
    conversation_id: str,
    request: Request,
    paths: list[str] = Form(...),
    files: list[UploadFile] = File(...),
    user=Depends(current_user),
    conn=Depends(_get_conn),
):
    check_csrf(request, user)
    conversation = _conversation_for(conn, user, conversation_id)
    if conversation["user_id"] != user["user_id"]:
        raise HTTPException(status_code=403, detail="不能给其他人的会话上传本地文件夹")
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="只读账号不能上传本地文件夹")
    folder = db.get_local_folder(conn, conversation_id)
    if folder is None:
        raise HTTPException(status_code=409, detail="请先在浏览器授权本地文件夹")
    if not files or len(files) != len(paths):
        raise HTTPException(status_code=400, detail="文件与路径数量不一致")
    if len(files) > LOCAL_BATCH_FILES:
        raise HTTPException(status_code=413, detail=f"单批最多 {LOCAL_BATCH_FILES} 个文件")
    root = _local_root(request.app.state.settings, conversation)
    root.mkdir(parents=True, exist_ok=True)
    root_resolved = root.resolve()
    current_count, current_bytes = _local_stats(root)
    if current_count >= LOCAL_FOLDER_MAX_FILES:
        raise HTTPException(status_code=413, detail=f"文件夹最多 {LOCAL_FOLDER_MAX_FILES} 个文件")
    uploaded: list[dict[str, Any]] = []
    total_added = 0
    for raw_path, upload in zip(paths, files):
        relative = _safe_local_relpath(raw_path)
        unresolved = root / relative
        if unresolved.is_symlink():
            raise HTTPException(status_code=400, detail="不允许写入符号链接")
        target = unresolved.resolve()
        if not target.is_relative_to(root_resolved):
            raise HTTPException(status_code=400, detail="本地文件路径越界")
        data = await upload.read()
        if len(data) > LOCAL_FILE_MAX_BYTES:
            raise HTTPException(status_code=413, detail=f"{relative} 超过 50 MB")
        total_added += len(data)
        if current_bytes + total_added > LOCAL_FOLDER_MAX_BYTES:
            raise HTTPException(status_code=413, detail="本地文件夹总量超过 200 MB")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.parent.chmod(0o777)
        except OSError:
            pass
        target.write_bytes(data)
        try:
            target.chmod(0o666)
        except OSError:
            pass
        uploaded.append(
            {"path": relative, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        )
    count, total = _local_stats(root)
    db.update_local_folder_stats(conn, conversation_id, file_count=count, total_bytes=total)
    return {"accepted": len(uploaded), "uploaded": uploaded, "file_count": count, "total_bytes": total}


@router.get("/api/chat/conversations/{conversation_id}/local-folder/files")
def local_folder_files(
    conversation_id: str,
    request: Request,
    user=Depends(current_user),
    conn=Depends(_get_conn),
):
    conversation = _conversation_for(conn, user, conversation_id)
    folder = db.get_local_folder(conn, conversation_id)
    if folder is None:
        raise HTTPException(status_code=404, detail="这个会话还没有授权本地文件夹")
    root = _local_root(request.app.state.settings, conversation)
    files, total = _local_manifest(root)
    db.update_local_folder_stats(conn, conversation_id, file_count=len(files), total_bytes=total)
    folder = db.get_local_folder(conn, conversation_id) or folder
    return {
        "folder": {**folder, **_local_folder_payload(request.app.state.settings, conversation)},
        "files": files,
        "total_bytes": total,
    }


@router.get("/api/chat/conversations/{conversation_id}/local-folder/download")
def local_folder_download(
    conversation_id: str,
    path: str,
    request: Request,
    user=Depends(current_user),
    conn=Depends(_get_conn),
):
    conversation = _conversation_for(conn, user, conversation_id)
    if db.get_local_folder(conn, conversation_id) is None:
        raise HTTPException(status_code=404, detail="这个会话还没有授权本地文件夹")
    relative = _safe_local_relpath(path)
    root = _local_root(request.app.state.settings, conversation)
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()) or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(target, media_type="application/octet-stream", filename=Path(relative).name)


@router.delete("/api/chat/conversations/{conversation_id}/local-folder")
def local_folder_disconnect(
    conversation_id: str,
    request: Request,
    user=Depends(current_user),
    conn=Depends(_get_conn),
):
    check_csrf(request, user)
    conversation = _conversation_for(conn, user, conversation_id)
    if conversation["user_id"] != user["user_id"]:
        raise HTTPException(status_code=403, detail="不能清理其他人的会话")
    root = _local_root(request.app.state.settings, conversation)
    shutil.rmtree(root, ignore_errors=True)
    db.delete_local_folder(conn, conversation_id)
    return {"ok": True}


@router.post("/api/chat/conversations/{conversation_id}/messages")
async def send_chat_message(
    conversation_id: str,
    body: ChatIn,
    request: Request,
    user=Depends(current_user),
    conn=Depends(_get_conn),
):
    check_csrf(request, user)
    conversation = _conversation_for(conn, user, conversation_id)
    if conversation["user_id"] != user["user_id"]:
        raise HTTPException(status_code=403, detail="不能替其他用户发送消息")
    access = db.user_can_access(conn, user["user_id"], conversation["tenant_id"])
    if access is None:
        raise HTTPException(status_code=403, detail="没有该租户的访问权限")
    if user["role"] != "admin" and (user["role"] == "viewer" or access == "viewer"):
        raise HTTPException(status_code=403, detail="只读账号不能发送消息")
    runtime = db.get_runtime(conn, conversation["tenant_id"])
    if runtime is None or not runtime.get("base_url"):
        raise HTTPException(status_code=409, detail="该租户还没有配置运行环境，请联系管理员")
    heartbeat = db.get_heartbeat(conn, conversation["tenant_id"])
    if heartbeat is None or time.time() - float(heartbeat["last_seen"]) > HEARTBEAT_MAX_AGE_SECONDS:
        raise HTTPException(
            status_code=409,
            detail="审计心跳超时：该运行环境没有连着门户审计，已阻止对话",
        )
    try:
        api_key = request.app.state.secret_box.decrypt(runtime["api_key_enc"])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    runs: dict[str, ChatRun] = getattr(request.app.state, "chat_runs", None) or {}
    request.app.state.chat_runs = runs
    existing = runs.get(conversation_id)
    if existing is not None and existing.status == "running":
        raise HTTPException(status_code=409, detail="Agent 还在回复上一条消息，请等它完成或刷新查看进度")
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="消息不能为空")
    attachments: list[dict[str, Any]] = []
    attachment_note = ""
    if body.attachment_ids:
        lines: list[str] = []
        for attachment_id in dict.fromkeys(body.attachment_ids):
            row = db.get_attachment(conn, attachment_id, conversation_id, user["user_id"])
            if row is None:
                raise HTTPException(status_code=404, detail="附件不存在或不属于这个会话")
            attachments.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "size": row["size"],
                    "mime": row["mime"],
                }
            )
            lines.append(f"- {row['name']}: {row['stored_path']} ({row['size']} bytes)")
        attachment_note = (chr(10) + chr(10) + "[用户上传了附件，可直接用文件/终端工具读取]" + chr(10) + chr(10).join(lines))
    local_folder = db.get_local_folder(conn, conversation_id)
    local_note = ""
    if local_folder is not None:
        local_note = (
            chr(10) * 2
            + f"[用户已授权本地文件夹「{local_folder['name']}」，服务器镜像路径：{local_folder['server_path']}，"
            + f"当前 {local_folder['file_count']} 个文件 / {local_folder['total_bytes']} 字节。"
            + "可以直接读取和修改这个目录里的文件；用户会在浏览器点“同步回本地”把改动写回自己的电脑。"
            + "优先只改这个目录，不要访问其他本地路径。]"
        )
    skill_note = ""
    selected_skill_ids: list[str] = []
    if body.skill_ids:
        visible = {item["id"]: item for item in _visible_skills(conn, user)}
        blocks: list[str] = []
        for skill_id in dict.fromkeys(body.skill_ids):
            skill = visible.get(skill_id)
            if skill is None:
                raise HTTPException(status_code=404, detail=f"技能不存在或没有权限：{skill_id}")
            selected_skill_ids.append(skill_id)
            blocks.append(f"### 技能：{skill['name']}\n{_skill_content_for_id(skill_id)}")
        skill_note = (
            chr(10) * 2
            + "[本轮使用服务端技能，按下列说明执行；技能说明只对本轮生效]"
            + chr(10)
            + chr(10).join(blocks)
        )
    meta: dict[str, Any] = {}
    if attachments:
        meta["attachments"] = attachments
    if selected_skill_ids:
        meta["skills"] = selected_skill_ids
    db.add_message(
        conn,
        conversation_id,
        "user",
        content,
        meta=meta or None,
    )
    history_rows = db.list_messages(conn, conversation_id, limit=80)
    payload_messages: list[dict[str, str]] = []
    used_chars = 0
    for row in reversed(history_rows):
        if row["role"] not in {"user", "assistant"}:
            continue
        text = str(row["content"])
        if payload_messages and used_chars + len(text) > 120_000:
            break
        payload_messages.append({"role": row["role"], "content": text})
        used_chars += len(text)
    payload_messages.reverse()
    if not payload_messages:
        payload_messages = [{"role": "user", "content": content}]
    extra_note = attachment_note + local_note + skill_note
    if extra_note and payload_messages and payload_messages[-1]["role"] == "user":
        payload_messages[-1]["content"] = content + extra_note
    model = runtime.get("model") or "hermes-agent"
    upstream_url = runtime["base_url"].rstrip("/") + "/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "X-Hermes-Session-Id": conversation_id,
        "X-Hermes-Session-Key": conversation_id,
    }
    payload = {
        "model": model,
        "messages": payload_messages,
        "stream": True,
    }
    cfg = request.app.state.settings
    transport = request.app.state.chat_transport

    def persist(role: str, text: str, meta: dict[str, Any] | None = None) -> None:
        if not text:
            return
        c = db.connect(cfg.db_path)
        try:
            db.add_message(c, conversation_id, role, text, meta=meta)
        finally:
            c.close()

    def persist_approval(payload: dict[str, Any]) -> None:
        run_id = str(payload.get("run_id") or "").strip()
        if not run_id:
            return
        c = db.connect(cfg.db_path)
        try:
            db.create_approval(
                c,
                tenant_id=conversation["tenant_id"],
                conversation_id=conversation_id,
                user_id=user["user_id"],
                run_id=run_id,
                request_id=str(payload.get("request_id") or ""),
                tool=str(payload.get("tool") or payload.get("tool_name") or "") or None,
                command=str(payload.get("command") or "") or None,
                description=str(payload.get("description") or "") or None,
            )
        finally:
            c.close()

    run = ChatRun(conversation_id)
    runs[conversation_id] = run
    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    run.subscribers.add(queue)
    run.task = asyncio.create_task(
        _run_agent_stream(
            run=run,
            upstream_url=upstream_url,
            base_url=runtime["base_url"],
            api_key=api_key,
            model=model,
            payload=payload,
            headers=headers,
            cfg=cfg,
            transport=transport,
            persist=persist,
            persist_approval=persist_approval,
        )
    )

    async def stream() -> AsyncIterator[str]:
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            # The browser may go away; the tenant run keeps going in run.task.
            run.subscribers.discard(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Hermes-Session-Id": conversation_id,
        },
    )


@router.get("/api/chat/conversations/{conversation_id}/active")
def chat_active(
    conversation_id: str,
    request: Request,
    user=Depends(current_user),
    conn=Depends(_get_conn),
):
    _conversation_for(conn, user, conversation_id)
    runs: dict[str, ChatRun] = getattr(request.app.state, "chat_runs", None) or {}
    run = runs.get(conversation_id)
    if run is None:
        return {"active": False, "status": "idle", "started_at": None, "error": ""}
    return {
        "active": run.status == "running",
        "status": run.status,
        "started_at": run.started_at,
        "error": run.error,
    }
