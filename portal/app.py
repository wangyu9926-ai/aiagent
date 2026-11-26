"""FastAPI application: login, RBAC, tenant tokens, audit ingest/query/SSE, admin UI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from urllib.parse import urlparse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Literal, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from portal import auth, db, events
from portal.chat_api import router as chat_router
from portal.config import Settings, generated_password, load_settings
from portal.secrets import SecretBox

logger = logging.getLogger("portal")
logging.basicConfig(level=os.environ.get("PORTAL_LOG_LEVEL", "INFO"))

_DUMMY_HASH = auth.hash_password("portal-dummy-password")


def _request_is_https(request: Request) -> bool:
    """Trust the TLS edge's forwarded scheme in addition to the direct scheme."""
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return request.url.scheme == "https" or forwarded == "https"


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=256)
    role: Literal["admin", "member", "viewer"] = "member"
    tenant_id: Optional[str] = Field(default=None, max_length=64)


class RuntimeIn(BaseModel):
    base_url: str = Field(min_length=4, max_length=500)
    api_key: str = Field(min_length=1, max_length=500)
    model: str = Field(default="hermes-agent", min_length=1, max_length=120)


class GrantIn(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    access_role: Literal["member", "viewer"] = "member"


class ConversationIn(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    title: Optional[str] = Field(default=None, max_length=80)


class ChatIn(BaseModel):
    content: str = Field(min_length=1, max_length=32000)


class TenantIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    id: Optional[str] = Field(default=None, max_length=41)


class DisableIn(BaseModel):
    disabled: bool = True


class HeartbeatIn(BaseModel):
    version: Optional[str] = Field(default=None, max_length=80)
    pid: Optional[int] = None
    hostname: Optional[str] = Field(default=None, max_length=200)


def _get_conn(request: Request):
    conn = db.connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def _cookie(request: Request) -> str:
    return request.cookies.get(auth.COOKIE_NAME, "")


def current_user(request: Request, conn=Depends(_get_conn)) -> dict[str, Any]:
    user = db.get_session(conn, _cookie(request))
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


def _client_ip(request: Request) -> str:
    client = request.client
    return getattr(client, "host", None) or "unknown"


def _bootstrap(cfg: Settings) -> None:
    db.init_db(cfg.db_path)
    conn = db.connect(cfg.db_path)
    try:
        db.purge_expired_sessions(conn)
        if cfg.admin_password:
            created = db.bootstrap_admin(conn, cfg.admin_user, cfg.admin_password)
            if created:
                logger.info("Created bootstrap admin %r", cfg.admin_user)
            return
        row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
        if row and int(row[0]) > 0:
            return
        password = generated_password()
        db.bootstrap_admin(conn, cfg.admin_user, password)
        target = cfg.data_dir / "initial-admin.txt"
        try:
            target.write_text(
                f"username: {cfg.admin_user}\npassword: {password}\n"
                "Log in, then change this password from the Users page.\n",
                encoding="utf-8",
            )
            try:
                target.chmod(0o600)
            except OSError:
                pass
        except OSError:
            pass
        logger.warning(
            "No PORTAL_ADMIN_PASSWORD set. Generated bootstrap admin %r with password %r. "
            "It is also written to %s. Set PORTAL_ADMIN_PASSWORD on first start to choose your own.",
            cfg.admin_user,
            password,
            target,
        )
    finally:
        conn.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or load_settings()
    cfg.static_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        _bootstrap(app.state.settings)
        yield

    app = FastAPI(
        title="Hermes Portal",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = cfg
    app.state.limiter = auth.LoginLimiter(
        max_attempts=cfg.login_max_attempts, window_seconds=cfg.login_window_seconds
    )
    app.state.secret_box = SecretBox(cfg.data_dir / "runtime.key")
    app.state.chat_transport = None
    app.state.http_transport = None
    app.mount("/static", StaticFiles(directory=str(cfg.static_dir)), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'",
        )
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse("/login", status_code=302)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/login", include_in_schema=False)
    def login_page(request: Request, conn=Depends(_get_conn)):
        user = db.get_session(conn, _cookie(request))
        if user is not None:
            target = "/dashboard" if user["role"] == "admin" else "/chat"
            return RedirectResponse(target, status_code=302)
        return FileResponse(cfg.static_dir / "login.html")

    @app.get("/dashboard", include_in_schema=False)
    def dashboard_page(request: Request, conn=Depends(_get_conn)):
        user = db.get_session(conn, _cookie(request))
        if user is None:
            return RedirectResponse("/login", status_code=302)
        if user["role"] != "admin":
            return HTMLResponse(
                "<!doctype html><meta charset='utf-8'><title>无权访问</title>"
                "<body style='font-family:system-ui;padding:40px'>"
                "<h1>403</h1><p>当前账号不是管理员，无法打开监管台。</p>"
                "<p><a href='/chat'>进入聊天</a> · <a href='/login'>返回登录</a></p></body>",
                status_code=403,
            )
        return FileResponse(cfg.static_dir / "dashboard.html")

    @app.get("/chat", include_in_schema=False)
    def chat_page(request: Request, conn=Depends(_get_conn)):
        if db.get_session(conn, _cookie(request)) is None:
            return RedirectResponse("/login", status_code=302)
        return FileResponse(cfg.static_dir / "chat.html")

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    @app.post("/api/auth/login")
    def login(body: LoginIn, request: Request, conn=Depends(_get_conn)):
        limiter: auth.LoginLimiter = request.app.state.limiter
        key = f"{_client_ip(request)}:{body.username.strip().lower()}"
        if not limiter.allow(key):
            raise HTTPException(status_code=429, detail="尝试次数过多，请稍后再试")
        row = db.get_user_by_username(conn, body.username.strip())
        stored_hash = row["password_hash"] if row is not None else _DUMMY_HASH
        password_ok = auth.verify_password(body.password, stored_hash)
        if row is None or not password_ok or row["disabled"]:
            limiter.fail(key)
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        limiter.reset(key)
        token, csrf, expires = db.create_session(
            conn,
            row["id"],
            ttl_seconds=request.app.state.settings.session_ttl_seconds,
            ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        response = JSONResponse(
            {
                "ok": True,
                "user": {"id": row["id"], "username": row["username"], "role": row["role"]},
                "csrf": csrf,
                "expires_at": expires,
            }
        )
        response.set_cookie(
            auth.COOKIE_NAME,
            token,
            max_age=request.app.state.settings.session_ttl_seconds,
            httponly=True,
            samesite="lax",
            secure=request.app.state.settings.secure_cookies or _request_is_https(request),
            path="/",
        )
        return response

    @app.post("/api/auth/logout")
    def logout(
        request: Request,
        user=Depends(current_user),
        conn=Depends(_get_conn),
    ):
        check_csrf(request, user)
        db.delete_session(conn, _cookie(request))
        response = JSONResponse({"ok": True})
        response.delete_cookie(auth.COOKIE_NAME, path="/")
        return response

    @app.get("/api/me")
    def me(user=Depends(current_user)):
        return {
            "user": {
                "id": user["user_id"],
                "username": user["username"],
                "role": user["role"],
            },
            "csrf": user["csrf"],
            "expires_at": user["expires_at"],
        }

    # ------------------------------------------------------------------
    # Admin: users and tenants
    # ------------------------------------------------------------------

    @app.get("/api/users")
    def get_users(user=Depends(require_admin), conn=Depends(_get_conn)):
        return {"users": db.list_users(conn)}

    @app.post("/api/users")
    def post_user(
        body: UserIn,
        request: Request,
        user=Depends(require_admin),
        conn=Depends(_get_conn),
    ):
        check_csrf(request, user)
        if body.tenant_id and conn.execute(
            "SELECT 1 FROM tenants WHERE id = ?", (body.tenant_id,)
        ).fetchone() is None:
            raise HTTPException(status_code=400, detail="租户不存在")
        try:
            created = db.create_user(conn, body.username, body.password, body.role)
            if body.tenant_id:
                db.grant_tenant(conn, created["id"], body.tenant_id, "member")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"user": created}

    @app.post("/api/users/{user_id}/disable")
    def disable_user(
        user_id: int,
        body: DisableIn,
        request: Request,
        user=Depends(require_admin),
        conn=Depends(_get_conn),
    ):
        check_csrf(request, user)
        if user_id == user["user_id"] and body.disabled:
            raise HTTPException(status_code=400, detail="不能停用当前登录账号")
        if not db.set_user_disabled(conn, user_id, body.disabled):
            raise HTTPException(status_code=404, detail="用户不存在")
        return {"ok": True}

    @app.get("/api/tenants")
    def get_tenants(user=Depends(require_admin), conn=Depends(_get_conn)):
        return {"tenants": db.list_tenants(conn)}

    @app.post("/api/tenants")
    def post_tenant(
        body: TenantIn,
        request: Request,
        user=Depends(require_admin),
        conn=Depends(_get_conn),
    ):
        check_csrf(request, user)
        try:
            tenant_id, token = db.create_tenant(conn, body.name, body.id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"tenant": {"id": tenant_id, "name": body.name}, "token": token}

    @app.post("/api/tenants/{tenant_id}/disable")
    def disable_tenant(
        tenant_id: str,
        body: DisableIn,
        request: Request,
        user=Depends(require_admin),
        conn=Depends(_get_conn),
    ):
        check_csrf(request, user)
        if not db.set_tenant_disabled(conn, tenant_id, body.disabled):
            raise HTTPException(status_code=404, detail="租户不存在")
        return {"ok": True}

    # ------------------------------------------------------------------
    # Audit: tenant ingest
    # ------------------------------------------------------------------

    @app.post("/api/audit/events")
    async def ingest(request: Request, conn=Depends(_get_conn)):
        raw = await request.body()
        if len(raw) > request.app.state.settings.max_body_bytes:
            raise HTTPException(status_code=413, detail="请求体过大")
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="缺少 Bearer 租户令牌")
        token = header[7:].strip()
        tenant = db.get_tenant_by_token(conn, token)
        if tenant is None:
            raise HTTPException(status_code=401, detail="租户令牌无效或已停用")
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from exc
        if isinstance(body, dict) and "events" in body:
            batch = body["events"]
        else:
            batch = [body]
        if not isinstance(batch, list):
            raise HTTPException(status_code=400, detail="events 必须是数组")
        if not batch:
            return {"accepted": 0, "duplicates": 0, "head_seq": 0}
        if len(batch) > 500:
            raise HTTPException(status_code=413, detail="单批最多 500 条事件")
        try:
            result = db.ingest_events(conn, tenant["id"], batch)
        except db.IngestError as exc:
            return JSONResponse(
                status_code=exc.status,
                content={
                    "error": exc.code,
                    "message": exc.message,
                    "expected_seq": exc.expected_seq,
                    "expected_prev_hash": exc.expected_prev_hash,
                },
            )
        return result

    @app.post("/api/tenants/heartbeat")
    async def tenant_heartbeat(request: Request, conn=Depends(_get_conn)):
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="缺少 Bearer 租户令牌")
        tenant = db.get_tenant_by_token(conn, header[7:].strip())
        if tenant is None:
            raise HTTPException(status_code=401, detail="租户令牌无效或已停用")
        try:
            body = json.loads(await request.body() or b"{}")
        except json.JSONDecodeError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        pid = body.get("pid")
        db.record_heartbeat(
            conn,
            tenant["id"],
            version=str(body.get("version") or "")[:80] or None,
            pid=int(pid) if isinstance(pid, int) else None,
            hostname=str(body.get("hostname") or "")[:200] or None,
        )
        return {"ok": True}

    # ------------------------------------------------------------------
    # Audit: admin query and live stream
    # ------------------------------------------------------------------

    @app.get("/api/stats")
    def get_stats(user=Depends(require_admin), conn=Depends(_get_conn)):
        return db.stats(conn)

    @app.get("/api/sessions")
    def get_sessions(
        tenant: Optional[str] = None,
        limit: int = 100,
        user=Depends(require_admin),
        conn=Depends(_get_conn),
    ):
        return {"sessions": db.query_sessions(conn, tenant=tenant or None, limit=limit)}

    @app.get("/api/audit/events")
    def get_events(
        tenant: Optional[str] = None,
        session_id: Optional[str] = None,
        type: Optional[str] = None,
        q: Optional[str] = None,
        limit: int = 100,
        before_id: Optional[int] = None,
        user=Depends(require_admin),
        conn=Depends(_get_conn),
    ):
        items = db.query_events(
            conn,
            tenant=tenant or None,
            session_id=session_id or None,
            event_type=type or None,
            q=q or None,
            limit=limit,
            before_id=before_id,
        )
        next_before = min((item["id"] for item in items), default=None) if len(items) >= limit else None
        return {"events": items, "next_before_id": next_before}

    @app.get("/api/audit/events/{event_id}")
    def get_event(event_id: int, user=Depends(require_admin), conn=Depends(_get_conn)):
        item = db.get_event(conn, event_id)
        if item is None:
            raise HTTPException(status_code=404, detail="事件不存在")
        return item

    @app.get("/api/audit/verify")
    def verify(
        tenant: str,
        session_id: str,
        user=Depends(require_admin),
        conn=Depends(_get_conn),
    ):
        return db.verify_chain(conn, tenant, session_id)

    @app.get("/api/audit/stream")
    async def stream(
        request: Request,
        after_id: int = 0,
        tenant: Optional[str] = None,
        user=Depends(require_admin),
    ):
        cfg = request.app.state.settings

        def fetch(after: int) -> list[dict[str, Any]]:
            conn = db.connect(cfg.db_path)
            try:
                return db.fetch_events_after(conn, after, tenant=tenant or None)
            finally:
                conn.close()

        async def generate() -> AsyncIterator[str]:
            last = max(0, int(after_id))
            idle = 0.0
            while True:
                if await request.is_disconnected():
                    return
                rows = await asyncio.to_thread(fetch, last)
                if rows:
                    for row in rows:
                        last = int(row["id"])
                        yield f"id: {last}\ndata: {json.dumps(row, ensure_ascii=False)}\n\n"
                    idle = 0.0
                else:
                    idle += 0.5
                    if idle >= 10:
                        yield ": keepalive\n\n"
                        idle = 0.0
                await asyncio.sleep(0.5)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    app.include_router(chat_router)
    return app


app = create_app()
