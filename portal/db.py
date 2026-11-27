"""SQLite storage for accounts, tenant tokens, and the append-only audit chain."""

from __future__ import annotations

import json
import math
import re
import secrets
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from portal import auth, events

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'member', 'viewer')),
    created_at REAL NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    ip TEXT,
    user_agent TEXT
);

CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant TEXT NOT NULL,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    summary TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL,
    received_at REAL NOT NULL,
    UNIQUE (tenant, session_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_audit_events_tenant_ts ON audit_events (tenant, ts);
CREATE INDEX IF NOT EXISTS idx_audit_events_session ON audit_events (tenant, session_id, seq);
CREATE INDEX IF NOT EXISTS idx_audit_events_type_ts ON audit_events (type, ts);

CREATE TABLE IF NOT EXISTS audit_heads (
    tenant TEXT NOT NULL,
    session_id TEXT NOT NULL,
    last_seq INTEGER NOT NULL,
    last_hash TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (tenant, session_id)
);

CREATE TABLE IF NOT EXISTS audit_sessions (
    tenant TEXT NOT NULL,
    session_id TEXT NOT NULL,
    first_ts REAL NOT NULL,
    last_ts REAL NOT NULL,
    event_count INTEGER NOT NULL,
    last_type TEXT NOT NULL,
    last_summary TEXT NOT NULL,
    PRIMARY KEY (tenant, session_id)
);

CREATE TABLE IF NOT EXISTS audit_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant TEXT NOT NULL,
    session_id TEXT NOT NULL,
    expected_seq INTEGER NOT NULL,
    got_seq INTEGER NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_gaps_created ON audit_gaps (created_at);

CREATE TABLE IF NOT EXISTS tenant_runtimes (
    tenant_id TEXT PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    base_url TEXT NOT NULL,
    api_key_enc TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT 'hermes-agent',
    updated_at REAL NOT NULL,
    last_ok_at REAL,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS user_tenants (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    access_role TEXT NOT NULL CHECK (access_role IN ('member', 'viewer')),
    created_at REAL NOT NULL,
    PRIMARY KEY (user_id, tenant_id)
);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversations_tenant_user ON conversations (tenant_id, user_id, updated_at);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'error')),
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    meta TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages (conversation_id, id);

CREATE TABLE IF NOT EXISTS chat_attachments (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mime TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_attachments_conversation ON chat_attachments (conversation_id, id);

CREATE TABLE IF NOT EXISTS local_folders (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    server_path TEXT NOT NULL,
    file_count INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    connected_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_heartbeats (
    tenant_id TEXT PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    last_seen REAL NOT NULL,
    version TEXT,
    pid INTEGER,
    hostname TEXT
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    user_id INTEGER,
    run_id TEXT NOT NULL,
    request_id TEXT NOT NULL DEFAULT '',
    tool TEXT,
    command TEXT,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    choice TEXT,
    created_at REAL NOT NULL,
    decided_at REAL,
    decided_by INTEGER
);

CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals (status, created_at);
CREATE INDEX IF NOT EXISTS idx_approvals_run ON approvals (run_id, request_id);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    repo_url TEXT,
    goal TEXT NOT NULL DEFAULT '',
    known TEXT,
    unknowns TEXT,
    workspace TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_projects_tenant ON projects (tenant_id, updated_at);
"""

HEX64 = re.compile(r"^[0-9a-f]{64}$")


class IngestError(Exception):
    """A rejected audit batch with a machine-readable reason and resync hint."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        expected_seq: int | None = None,
        expected_prev_hash: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.expected_seq = expected_seq
        self.expected_prev_hash = expected_prev_hash


def now() -> float:
    return time.time()


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db(db_path: str | Path) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Accounts and browser sessions
# ---------------------------------------------------------------------------


def create_user(conn: sqlite3.Connection, username: str, password: str, role: str) -> dict[str, Any]:
    username = username.strip()
    if not username or len(username) > 64:
        raise ValueError("用户名长度必须在 1–64 个字符之间")
    if role not in {"admin", "member", "viewer"}:
        raise ValueError("角色必须是 admin、member 或 viewer")
    if len(password) < 8:
        raise ValueError("密码至少 8 位")
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (username, auth.hash_password(password), role, now()),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ValueError("用户名已存在") from exc
    return {"id": cur.lastrowid, "username": username, "role": role, "disabled": False}


def get_user_by_username(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def get_user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def list_users(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, username, role, created_at, disabled FROM users ORDER BY id"
    ).fetchall()
    return [dict(row) for row in rows]


def set_user_disabled(conn: sqlite3.Connection, user_id: int, disabled: bool) -> bool:
    cur = conn.execute("UPDATE users SET disabled = ? WHERE id = ?", (1 if disabled else 0, user_id))
    conn.commit()
    return cur.rowcount > 0


def create_session(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    ttl_seconds: int,
    ip: str | None,
    user_agent: str | None,
) -> tuple[str, str, float]:
    token = auth.new_token(32)
    csrf = auth.new_csrf_token()
    created = now()
    expires = created + max(60, int(ttl_seconds))
    conn.execute(
        "INSERT INTO sessions (id, user_id, csrf, created_at, expires_at, ip, user_agent) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (auth.token_hash(token), user_id, csrf, created, expires, ip, (user_agent or "")[:500]),
    )
    conn.commit()
    return token, csrf, expires


def get_session(conn: sqlite3.Connection, token: str) -> dict[str, Any] | None:
    if not token:
        return None
    row = conn.execute(
        "SELECT s.id, s.csrf, s.created_at, s.expires_at, u.id AS user_id, u.username, u.role, u.disabled "
        "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id = ?",
        (auth.token_hash(token),),
    ).fetchone()
    if row is None:
        return None
    if row["disabled"] or row["expires_at"] <= now():
        conn.execute("DELETE FROM sessions WHERE id = ?", (row["id"],))
        conn.commit()
        return None
    return {
        "session_hash": row["id"],
        "csrf": row["csrf"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "user_id": row["user_id"],
        "username": row["username"],
        "role": row["role"],
    }


def delete_session(conn: sqlite3.Connection, token: str) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE id = ?", (auth.token_hash(token),))
        conn.commit()


def bootstrap_admin(conn: sqlite3.Connection, username: str, password: str) -> bool:
    """Create the first admin only when the database has no users."""
    row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
    if row and int(row[0]) > 0:
        return False
    create_user(conn, username, password, "admin")
    return True


def purge_expired_sessions(conn: sqlite3.Connection) -> int:
    cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now(),))
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# Tenants and tokens
# ---------------------------------------------------------------------------


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return (slug[:32] or "tenant").strip("-") or "tenant"


def create_tenant(conn: sqlite3.Connection, name: str, tenant_id: str | None = None) -> tuple[str, str]:
    name = name.strip()
    if not name or len(name) > 80:
        raise ValueError("租户名称长度必须在 1–80 个字符之间")
    base = (tenant_id or _slugify(name)).strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", base):
        raise ValueError("租户 ID 只能包含小写字母、数字和连字符，长度 2–41")
    candidate = base
    while conn.execute("SELECT 1 FROM tenants WHERE id = ?", (candidate,)).fetchone():
        candidate = f"{base}-{secrets.token_hex(2)}"
    token = auth.new_token(32)
    conn.execute(
        "INSERT INTO tenants (id, name, token_hash, created_at) VALUES (?, ?, ?, ?)",
        (candidate, name, auth.token_hash(token), now()),
    )
    conn.commit()
    return candidate, token


def get_tenant_by_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    if not token:
        return None
    return conn.execute(
        "SELECT * FROM tenants WHERE token_hash = ? AND disabled = 0",
        (auth.token_hash(token),),
    ).fetchone()


def list_tenants(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT t.id, t.name, t.created_at, t.disabled,
               (SELECT COUNT(*) FROM audit_sessions s WHERE s.tenant = t.id) AS sessions,
               (SELECT COUNT(*) FROM audit_events e WHERE e.tenant = t.id) AS event_count,
               (SELECT MAX(e.ts) FROM audit_events e WHERE e.tenant = t.id) AS last_ts,
               tr.base_url AS runtime_base_url, tr.model AS runtime_model,
               tr.last_ok_at AS runtime_ok_at, tr.last_error AS runtime_error
        FROM tenants t
        LEFT JOIN tenant_runtimes tr ON tr.tenant_id = t.id
        ORDER BY t.created_at DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def set_tenant_disabled(conn: sqlite3.Connection, tenant_id: str, disabled: bool) -> bool:
    cur = conn.execute(
        "UPDATE tenants SET disabled = ? WHERE id = ?", (1 if disabled else 0, tenant_id)
    )
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Audit ingest
# ---------------------------------------------------------------------------


def _validate_event(raw: Mapping[str, Any], tenant: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise IngestError(400, "invalid_event", "事件必须是对象")
    session_id = str(raw.get("session_id") or "").strip()
    if not session_id or len(session_id) > 200:
        raise IngestError(400, "invalid_session_id", "session_id 缺失或过长")
    try:
        seq = int(raw.get("seq"))
    except (TypeError, ValueError) as exc:
        raise IngestError(400, "invalid_seq", "seq 必须是整数") from exc
    if isinstance(raw.get("seq"), bool) or seq < 1:
        raise IngestError(400, "invalid_seq", "seq 必须是正整数")
    try:
        ts = float(raw.get("ts"))
    except (TypeError, ValueError) as exc:
        raise IngestError(400, "invalid_ts", "ts 必须是数字") from exc
    if not math.isfinite(ts):
        raise IngestError(400, "invalid_ts", "ts 必须是有限数字")
    event_type = str(raw.get("type") or "").strip()
    if not event_type or len(event_type) > 64:
        raise IngestError(400, "invalid_type", "type 缺失或过长")
    payload = raw.get("payload")
    if not isinstance(payload, Mapping):
        raise IngestError(400, "invalid_payload", "payload 必须是对象")
    prev_hash = str(raw.get("prev_hash") or "")
    digest = str(raw.get("hash") or "")
    if prev_hash and not HEX64.fullmatch(prev_hash):
        raise IngestError(400, "invalid_prev_hash", "prev_hash 必须是 64 位十六进制")
    if not HEX64.fullmatch(digest):
        raise IngestError(400, "invalid_hash", "hash 必须是 64 位十六进制")
    payload_dict = dict(payload)
    if events.payload_bytes(payload_dict) > 4 * 1024 * 1024:
        raise IngestError(413, "payload_too_large", "单条事件 payload 超过 4 MB")
    event_tenant = raw.get("tenant")
    if event_tenant is not None and str(event_tenant) != tenant:
        raise IngestError(403, "tenant_mismatch", "事件租户与令牌不匹配")
    return {
        "tenant": tenant,
        "session_id": session_id,
        "seq": seq,
        "ts": ts,
        "type": event_type,
        "payload": payload_dict,
        "prev_hash": prev_hash,
        "hash": digest,
    }


def ingest_events(
    conn: sqlite3.Connection, tenant: str, raw_events: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    if not raw_events:
        return {"accepted": 0, "duplicates": 0, "head_seq": 0}
    normalized = [_validate_event(item, tenant) for item in raw_events]
    accepted = 0
    duplicates = 0
    last_seq = 0
    last_hash = events.GENESIS_HASH
    conn.execute("BEGIN IMMEDIATE")
    try:
        for item in normalized:
            sid = item["session_id"]
            seq = item["seq"]
            existing = conn.execute(
                "SELECT hash FROM audit_events WHERE tenant = ? AND session_id = ? AND seq = ?",
                (tenant, sid, seq),
            ).fetchone()
            head = conn.execute(
                "SELECT last_seq, last_hash FROM audit_heads WHERE tenant = ? AND session_id = ?",
                (tenant, sid),
            ).fetchone()
            head_seq = int(head["last_seq"]) if head else 0
            head_hash = str(head["last_hash"]) if head else events.GENESIS_HASH
            if existing is not None:
                if existing["hash"] == item["hash"]:
                    duplicates += 1
                    last_seq, last_hash = head_seq, head_hash
                    continue
                raise IngestError(
                    409,
                    "conflict",
                    "同一 seq 已存在不同内容；请从服务端 head 重新同步",
                    expected_seq=head_seq + 1,
                    expected_prev_hash=head_hash,
                )
            if seq != head_seq + 1:
                raise IngestError(
                    409,
                    "sequence_gap",
                    f"期望 seq {head_seq + 1}，收到 {seq}",
                    expected_seq=head_seq + 1,
                    expected_prev_hash=head_hash,
                )
            if item["prev_hash"] != head_hash:
                raise IngestError(
                    409,
                    "prev_hash_mismatch",
                    "prev_hash 与服务端 head 不一致",
                    expected_seq=head_seq + 1,
                    expected_prev_hash=head_hash,
                )
            computed = events.event_hash(
                tenant=tenant,
                session_id=sid,
                seq=seq,
                ts=item["ts"],
                event_type=item["type"],
                payload=item["payload"],
                prev_hash=item["prev_hash"],
            )
            if not auth.constant_time_equal(computed, item["hash"]):
                raise IngestError(
                    409,
                    "hash_mismatch",
                    "事件哈希校验失败",
                    expected_seq=head_seq + 1,
                    expected_prev_hash=head_hash,
                )
            summary = events.summarize(item["type"], item["payload"])
            received = now()
            conn.execute(
                "INSERT INTO audit_events "
                "(tenant, session_id, seq, ts, type, payload, summary, prev_hash, hash, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tenant,
                    sid,
                    seq,
                    item["ts"],
                    item["type"],
                    events.canonical_json(item["payload"]),
                    summary,
                    item["prev_hash"],
                    item["hash"],
                    received,
                ),
            )
            conn.execute(
                "INSERT INTO audit_heads (tenant, session_id, last_seq, last_hash, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (tenant, session_id) DO UPDATE SET "
                "last_seq = excluded.last_seq, last_hash = excluded.last_hash, updated_at = excluded.updated_at",
                (tenant, sid, seq, item["hash"], received),
            )
            conn.execute(
                "INSERT INTO audit_sessions "
                "(tenant, session_id, first_ts, last_ts, event_count, last_type, last_summary) "
                "VALUES (?, ?, ?, ?, 1, ?, ?) "
                "ON CONFLICT (tenant, session_id) DO UPDATE SET "
                "last_ts = excluded.last_ts, event_count = audit_sessions.event_count + 1, "
                "last_type = excluded.last_type, last_summary = excluded.last_summary",
                (tenant, sid, item["ts"], item["ts"], item["type"], summary),
            )
            accepted += 1
            last_seq, last_hash = seq, item["hash"]
        conn.commit()
    except IngestError as exc:
        conn.rollback()
        if exc.expected_seq is not None and normalized:
            try:
                conn.execute(
                    "INSERT INTO audit_gaps (tenant, session_id, expected_seq, got_seq, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (tenant, normalized[0]["session_id"], exc.expected_seq, normalized[0]["seq"], now()),
                )
                conn.commit()
            except sqlite3.Error:
                conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    return {"accepted": accepted, "duplicates": duplicates, "head_seq": last_seq, "head_hash": last_hash}


# ---------------------------------------------------------------------------
# Audit queries
# ---------------------------------------------------------------------------


def query_events(
    conn: sqlite3.Connection,
    *,
    tenant: str | None = None,
    session_id: str | None = None,
    event_type: str | None = None,
    q: str | None = None,
    limit: int = 100,
    before_id: int | None = None,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if tenant:
        where.append("tenant = ?")
        params.append(tenant)
    if session_id:
        where.append("session_id = ?")
        params.append(session_id)
    if event_type:
        where.append("type = ?")
        params.append(event_type)
    if q:
        where.append("(summary LIKE ? OR session_id LIKE ?)")
        needle = f"%{q}%"
        params.extend([needle, needle])
    if before_id:
        where.append("id < ?")
        params.append(int(before_id))
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(max(1, min(500, int(limit))))
    rows = conn.execute(
        "SELECT id, tenant, session_id, seq, ts, type, summary, received_at "
        f"FROM audit_events{clause} ORDER BY id DESC LIMIT ?",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def get_event(conn: sqlite3.Connection, event_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM audit_events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    try:
        result["payload"] = json.loads(result["payload"])
    except (TypeError, ValueError):
        pass
    return result


def fetch_events_after(
    conn: sqlite3.Connection,
    after_id: int,
    *,
    tenant: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    if tenant:
        rows = conn.execute(
            "SELECT id, tenant, session_id, seq, ts, type, summary FROM audit_events "
            "WHERE id > ? AND tenant = ? ORDER BY id ASC LIMIT ?",
            (int(after_id), tenant, int(limit)),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, tenant, session_id, seq, ts, type, summary FROM audit_events "
            "WHERE id > ? ORDER BY id ASC LIMIT ?",
            (int(after_id), int(limit)),
        ).fetchall()
    return [dict(row) for row in rows]


def query_sessions(
    conn: sqlite3.Connection, *, tenant: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    params: list[Any] = []
    where = ""
    if tenant:
        where = " WHERE tenant = ?"
        params.append(tenant)
    params.append(max(1, min(500, int(limit))))
    rows = conn.execute(
        "SELECT tenant, session_id, first_ts, last_ts, event_count, last_type, last_summary "
        f"FROM audit_sessions{where} ORDER BY last_ts DESC LIMIT ?",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    current = now()
    def scalar(sql: str, params: Iterable[Any] = ()) -> int:
        row = conn.execute(sql, tuple(params)).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    return {
        "users": scalar("SELECT COUNT(*) FROM users"),
        "tenants": scalar("SELECT COUNT(*) FROM tenants WHERE disabled = 0"),
        "sessions": scalar("SELECT COUNT(*) FROM audit_sessions"),
        "active_sessions": scalar(
            "SELECT COUNT(*) FROM audit_sessions WHERE last_ts > ?", (current - 15 * 60,)
        ),
        "events_today": scalar("SELECT COUNT(*) FROM audit_events WHERE ts > ?", (current - 86400,)),
        "gaps_today": scalar("SELECT COUNT(*) FROM audit_gaps WHERE created_at > ?", (current - 86400,)),
    }


def verify_chain(conn: sqlite3.Connection, tenant: str, session_id: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT seq, ts, type, payload, prev_hash, hash FROM audit_events "
        "WHERE tenant = ? AND session_id = ? ORDER BY seq ASC",
        (tenant, session_id),
    ).fetchall()
    prev = events.GENESIS_HASH
    for row in rows:
        if row["prev_hash"] != prev:
            return {
                "ok": False,
                "events": len(rows),
                "first_bad_seq": row["seq"],
                "reason": "prev_hash 不连续",
            }
        computed = events.event_hash(
            tenant=tenant,
            session_id=session_id,
            seq=row["seq"],
            ts=row["ts"],
            event_type=row["type"],
            payload=json.loads(row["payload"]),
            prev_hash=row["prev_hash"],
        )
        if not auth.constant_time_equal(computed, row["hash"]):
            return {
                "ok": False,
                "events": len(rows),
                "first_bad_seq": row["seq"],
                "reason": "payload 哈希不匹配",
            }
        prev = row["hash"]
    return {"ok": True, "events": len(rows), "first_bad_seq": None, "reason": None}


# ---------------------------------------------------------------------------
# Tenant runtimes, access grants, and member conversations
# ---------------------------------------------------------------------------


def set_runtime(
    conn: sqlite3.Connection,
    tenant_id: str,
    *,
    base_url: str,
    api_key_enc: str,
    model: str,
) -> None:
    conn.execute(
        "INSERT INTO tenant_runtimes (tenant_id, base_url, api_key_enc, model, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (tenant_id) DO UPDATE SET "
        "base_url = excluded.base_url, api_key_enc = excluded.api_key_enc, "
        "model = excluded.model, updated_at = excluded.updated_at, "
        "last_ok_at = NULL, last_error = NULL",
        (tenant_id, base_url.rstrip("/"), api_key_enc, model or "hermes-agent", now()),
    )
    conn.commit()


def get_runtime(conn: sqlite3.Connection, tenant_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM tenant_runtimes WHERE tenant_id = ?", (tenant_id,)).fetchone()
    return dict(row) if row is not None else None


def mark_runtime_test(conn: sqlite3.Connection, tenant_id: str, *, ok: bool, error: str | None = None) -> None:
    conn.execute(
        "UPDATE tenant_runtimes SET last_ok_at = ?, last_error = ? WHERE tenant_id = ?",
        (now() if ok else None, None if ok else error, tenant_id),
    )
    conn.commit()


def grant_tenant(
    conn: sqlite3.Connection, user_id: int, tenant_id: str, access_role: str = "member"
) -> None:
    if access_role not in {"member", "viewer"}:
        raise ValueError("授权角色必须是 member 或 viewer")
    try:
        conn.execute(
            "INSERT INTO user_tenants (user_id, tenant_id, access_role, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (user_id, tenant_id) DO UPDATE SET access_role = excluded.access_role",
            (user_id, tenant_id, access_role, now()),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ValueError("用户或租户不存在") from exc


def revoke_tenant(conn: sqlite3.Connection, user_id: int, tenant_id: str) -> bool:
    cur = conn.execute(
        "DELETE FROM user_tenants WHERE user_id = ? AND tenant_id = ?", (user_id, tenant_id)
    )
    conn.commit()
    return cur.rowcount > 0


def user_can_access(conn: sqlite3.Connection, user_id: int, tenant_id: str) -> str | None:
    user = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        return None
    if user["role"] == "admin":
        return "admin"
    row = conn.execute(
        "SELECT access_role FROM user_tenants WHERE user_id = ? AND tenant_id = ?",
        (user_id, tenant_id),
    ).fetchone()
    return str(row["access_role"]) if row is not None else None


def user_tenants(conn: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT t.id, t.name, ut.access_role,
               tr.base_url AS runtime_base_url, tr.model AS runtime_model,
               tr.last_ok_at AS runtime_ok_at, tr.last_error AS runtime_error
        FROM user_tenants ut
        JOIN tenants t ON t.id = ut.tenant_id
        LEFT JOIN tenant_runtimes tr ON tr.tenant_id = t.id
        WHERE ut.user_id = ? AND t.disabled = 0
        ORDER BY t.name
        """,
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def user_grants(conn: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT ut.tenant_id, t.name, ut.access_role, ut.created_at
        FROM user_tenants ut JOIN tenants t ON t.id = ut.tenant_id
        WHERE ut.user_id = ? ORDER BY t.name
        """,
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def create_conversation(
    conn: sqlite3.Connection, tenant_id: str, user_id: int, title: str = "新会话"
) -> dict[str, Any]:
    conversation_id = uuid.uuid4().hex
    clean_title = (title or "新会话").strip()[:80] or "新会话"
    created = now()
    conn.execute(
        "INSERT INTO conversations (id, tenant_id, user_id, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (conversation_id, tenant_id, user_id, clean_title, created, created),
    )
    conn.commit()
    return {
        "id": conversation_id,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "title": clean_title,
        "created_at": created,
        "updated_at": created,
    }


def list_conversations(
    conn: sqlite3.Connection, tenant_id: str, user_id: int, limit: int = 100
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, tenant_id, user_id, title, created_at, updated_at FROM conversations "
        "WHERE tenant_id = ? AND user_id = ? ORDER BY updated_at DESC LIMIT ?",
        (tenant_id, user_id, max(1, min(200, int(limit)))),
    ).fetchall()
    return [dict(row) for row in rows]


def get_conversation(conn: sqlite3.Connection, conversation_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT c.*, t.name AS tenant_name FROM conversations c "
        "JOIN tenants t ON t.id = c.tenant_id WHERE c.id = ?",
        (conversation_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def create_project(
    conn: sqlite3.Connection,
    *,
    name: str,
    tenant_id: str,
    workspace: str,
    created_by: int | None,
    repo_url: str = "",
    goal: str = "",
    known: str = "",
    unknowns: str = "",
    project_id: str | None = None,
) -> dict[str, Any]:
    clean_name = (name or "").strip()
    if not clean_name or len(clean_name) > 80:
        raise ValueError("项目名称长度必须在 1–80 个字符之间")
    project_id = project_id or ("prj_" + secrets.token_hex(8))
    created = now()
    conn.execute(
        "INSERT INTO projects "
        "(id, name, tenant_id, repo_url, goal, known, unknowns, workspace, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            project_id,
            clean_name,
            tenant_id,
            (repo_url or "").strip(),
            (goal or "").strip(),
            (known or "").strip(),
            (unknowns or "").strip(),
            workspace,
            created_by,
            created,
            created,
        ),
    )
    conn.commit()
    return get_project(conn, project_id) or {}


def get_project(conn: sqlite3.Connection, project_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT p.*, t.name AS tenant_name FROM projects p "
        "JOIN tenants t ON t.id = p.tenant_id WHERE p.id = ?",
        (project_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def list_projects(
    conn: sqlite3.Connection, tenant_ids: Iterable[str] | None = None
) -> list[dict[str, Any]]:
    if tenant_ids is None:
        rows = conn.execute(
            "SELECT p.*, t.name AS tenant_name FROM projects p "
            "JOIN tenants t ON t.id = p.tenant_id ORDER BY p.updated_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]
    ids = [str(tenant_id) for tenant_id in tenant_ids]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT p.*, t.name AS tenant_name FROM projects p "
        f"JOIN tenants t ON t.id = p.tenant_id WHERE p.tenant_id IN ({placeholders}) "
        f"ORDER BY p.updated_at DESC",
        ids,
    ).fetchall()
    return [dict(row) for row in rows]


def touch_project(conn: sqlite3.Connection, project_id: str) -> None:
    conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now(), project_id))
    conn.commit()


def add_message(
    conn: sqlite3.Connection,
    conversation_id: str,
    role: str,
    content: str,
    *,
    meta: Mapping[str, Any] | None = None,
) -> int:
    if role not in {"user", "assistant", "system", "error"}:
        raise ValueError("role 必须是 user、assistant、system 或 error")
    conversation = conn.execute(
        "SELECT title FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()
    if conversation is None:
        raise ValueError("会话不存在")
    created = now()
    cur = conn.execute(
        "INSERT INTO messages (conversation_id, role, content, created_at, meta) VALUES (?, ?, ?, ?, ?)",
        (
            conversation_id,
            role,
            content,
            created,
            json.dumps(meta, ensure_ascii=False) if meta else None,
        ),
    )
    title = conversation["title"]
    if role == "user" and title in {"", "新会话"}:
        first_line = content.strip().splitlines()[0] if content.strip() else ""
        title = (first_line[:60] or "新会话")
    conn.execute(
        "UPDATE conversations SET updated_at = ?, title = ? WHERE id = ?",
        (created, title, conversation_id),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_messages(
    conn: sqlite3.Connection, conversation_id: str, limit: int = 500
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM (SELECT id, conversation_id, role, content, created_at, meta "
        "FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
        (conversation_id, max(1, min(1000, int(limit)))),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        if item.get("meta"):
            try:
                item["meta"] = json.loads(item["meta"])
            except (TypeError, ValueError):
                pass
        result.append(item)
    return result


def touch_conversation(conn: sqlite3.Connection, conversation_id: str, title: str | None = None) -> None:
    if title is None:
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now(), conversation_id))
    else:
        conn.execute(
            "UPDATE conversations SET updated_at = ?, title = ? WHERE id = ?",
            (now(), title[:80], conversation_id),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Chat attachments
# ---------------------------------------------------------------------------


def add_attachment(
    conn: sqlite3.Connection,
    conversation_id: str,
    user_id: int,
    *,
    name: str,
    stored_path: str,
    size: int,
    mime: str | None = None,
) -> dict[str, Any]:
    attachment_id = uuid.uuid4().hex
    created = now()
    conn.execute(
        "INSERT INTO chat_attachments "
        "(id, conversation_id, user_id, name, stored_path, size, mime, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (attachment_id, conversation_id, user_id, name, stored_path, int(size), mime, created),
    )
    conn.commit()
    return {
        "id": attachment_id,
        "conversation_id": conversation_id,
        "name": name,
        "stored_path": stored_path,
        "size": int(size),
        "mime": mime,
        "created_at": created,
    }


def get_attachment(
    conn: sqlite3.Connection, attachment_id: str, conversation_id: str, user_id: int
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM chat_attachments WHERE id = ? AND conversation_id = ? AND user_id = ?",
        (attachment_id, conversation_id, user_id),
    ).fetchone()
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Local folder bridge
# ---------------------------------------------------------------------------


def set_local_folder(
    conn: sqlite3.Connection,
    conversation_id: str,
    *,
    name: str,
    server_path: str,
    file_count: int = 0,
    total_bytes: int = 0,
) -> dict[str, Any]:
    created = now()
    conn.execute(
        "INSERT INTO local_folders "
        "(conversation_id, name, server_path, file_count, total_bytes, connected_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (conversation_id) DO UPDATE SET "
        "name = excluded.name, server_path = excluded.server_path, "
        "file_count = excluded.file_count, total_bytes = excluded.total_bytes, "
        "updated_at = excluded.updated_at",
        (conversation_id, name, server_path, int(file_count), int(total_bytes), created, created),
    )
    conn.commit()
    return get_local_folder(conn, conversation_id) or {}


def get_local_folder(conn: sqlite3.Connection, conversation_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM local_folders WHERE conversation_id = ?", (conversation_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def update_local_folder_stats(
    conn: sqlite3.Connection, conversation_id: str, *, file_count: int, total_bytes: int
) -> None:
    conn.execute(
        "UPDATE local_folders SET file_count = ?, total_bytes = ?, updated_at = ? "
        "WHERE conversation_id = ?",
        (int(file_count), int(total_bytes), now(), conversation_id),
    )
    conn.commit()


def delete_local_folder(conn: sqlite3.Connection, conversation_id: str) -> None:
    conn.execute("DELETE FROM local_folders WHERE conversation_id = ?", (conversation_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Tenant audit heartbeat
# ---------------------------------------------------------------------------


def record_heartbeat(
    conn: sqlite3.Connection,
    tenant_id: str,
    *,
    version: str | None = None,
    pid: int | None = None,
    hostname: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO tenant_heartbeats (tenant_id, last_seen, version, pid, hostname) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (tenant_id) DO UPDATE SET "
        "last_seen = excluded.last_seen, version = excluded.version, "
        "pid = excluded.pid, hostname = excluded.hostname",
        (tenant_id, now(), version, pid, hostname),
    )
    conn.commit()


def get_heartbeat(conn: sqlite3.Connection, tenant_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM tenant_heartbeats WHERE tenant_id = ?", (tenant_id,)).fetchone()
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Pending tool/command approvals
# ---------------------------------------------------------------------------


def create_approval(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    conversation_id: str,
    user_id: int | None,
    run_id: str,
    request_id: str = "",
    tool: str | None = None,
    command: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    request_id = str(request_id or "")
    existing = conn.execute(
        "SELECT * FROM approvals WHERE run_id = ? AND request_id = ? AND status = 'pending' "
        "ORDER BY created_at DESC LIMIT 1",
        (run_id, request_id),
    ).fetchone()
    if existing is not None:
        return dict(existing)
    approval_id = uuid.uuid4().hex
    created = now()
    conn.execute(
        "INSERT INTO approvals "
        "(id, tenant_id, conversation_id, user_id, run_id, request_id, tool, command, description, "
        "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
        (
            approval_id,
            tenant_id,
            conversation_id,
            user_id,
            run_id,
            request_id,
            tool,
            command,
            description,
            created,
        ),
    )
    conn.commit()
    return {
        "id": approval_id,
        "tenant_id": tenant_id,
        "conversation_id": conversation_id,
        "user_id": user_id,
        "run_id": run_id,
        "request_id": request_id,
        "tool": tool,
        "command": command,
        "description": description,
        "status": "pending",
        "created_at": created,
    }


def get_approval(conn: sqlite3.Connection, approval_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    return dict(row) if row is not None else None


def list_approvals(
    conn: sqlite3.Connection,
    *,
    status: str | None = "pending",
    tenant_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if status:
        where.append("a.status = ?")
        params.append(status)
    if tenant_id:
        where.append("a.tenant_id = ?")
        params.append(tenant_id)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(max(1, min(200, int(limit))))
    rows = conn.execute(
        "SELECT a.*, u.username FROM approvals a LEFT JOIN users u ON u.id = a.user_id "
        f"{clause} ORDER BY a.created_at DESC LIMIT ?",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def resolve_approval(
    conn: sqlite3.Connection, approval_id: str, *, choice: str, decided_by: int | None
) -> dict[str, Any] | None:
    status = "denied" if choice == "deny" else "approved"
    cur = conn.execute(
        "UPDATE approvals SET status = ?, choice = ?, decided_at = ?, decided_by = ? "
        "WHERE id = ? AND status = 'pending'",
        (status, choice, now(), decided_by, approval_id),
    )
    conn.commit()
    if cur.rowcount <= 0:
        return None
    return get_approval(conn, approval_id)


def expire_pending_for_run(conn: sqlite3.Connection, run_id: str) -> int:
    cur = conn.execute(
        "UPDATE approvals SET status = 'expired', decided_at = ? WHERE run_id = ? AND status = 'pending'",
        (now(), run_id),
    )
    conn.commit()
    return cur.rowcount
