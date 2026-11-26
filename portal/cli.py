"""Small operator CLI for the portal.

    python -m portal.cli create-tenant "团队 A"
    python -m portal.cli create-user alice 'strong-password' member
    python -m portal.cli reset-password alice 'new-password'
    python -m portal.cli list
    python -m portal.cli verify <tenant> <session_id>
    python -m portal.cli seed-demo
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from portal import auth, db, events
from portal.config import load_settings


def _connect():
    cfg = load_settings()
    db.init_db(cfg.db_path)
    return cfg, db.connect(cfg.db_path)


def cmd_create_user(args) -> int:
    _, conn = _connect()
    try:
        user = db.create_user(conn, args.username, args.password, args.role)
        print(json.dumps(user, ensure_ascii=False, indent=2))
        return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_create_tenant(args) -> int:
    _, conn = _connect()
    try:
        tenant_id, token = db.create_tenant(conn, args.name, args.id)
        print(f"tenant: {tenant_id}")
        print(f"token:  {token}")
        print("Store the token now: it is not shown again.")
        return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_reset_password(args) -> int:
    _, conn = _connect()
    try:
        row = db.get_user_by_username(conn, args.username)
        if row is None:
            print("error: user not found", file=sys.stderr)
            return 1
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (auth.hash_password(args.password), row["id"]),
        )
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
        conn.commit()
        print(f"password updated for {row['username']}; existing browser sessions were revoked")
        return 0
    finally:
        conn.close()


def cmd_list(args) -> int:
    _, conn = _connect()
    try:
        print("users:")
        for user in db.list_users(conn):
            print(f"  {user['id']:>3}  {user['username']:<24} {user['role']:<7} disabled={bool(user['disabled'])}")
        print("tenants:")
        for tenant in db.list_tenants(conn):
            print(
                f"  {tenant['id']:<24} events={tenant['event_count']:<6} "
                f"sessions={tenant['sessions']:<4} {tenant['name']}"
            )
        return 0
    finally:
        conn.close()


def cmd_verify(args) -> int:
    _, conn = _connect()
    try:
        print(json.dumps(db.verify_chain(conn, args.tenant, args.session_id), ensure_ascii=False, indent=2))
        return 0
    finally:
        conn.close()


def cmd_seed_demo(args) -> int:
    """Create a demo tenant and a chained session so the console has something to show."""
    _, conn = _connect()
    try:
        tenant_id = args.tenant
        row = conn.execute("SELECT id FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
        token = None
        if row is None:
            tenant_id, token = db.create_tenant(conn, "演示租户", tenant_id)
        session_id = f"demo-{int(time.time())}"
        base = time.time() - 30
        rows = [
            ("session.start", {"model": "demo-model", "platform": "web"}),
            ("llm.pre", {"user_message": "帮我快速接手这个项目：先看技术栈和启动方式。", "model": "demo-model", "platform": "web"}),
            ("api.pre", {"model": "demo-model", "message_count": 3, "approx_input_tokens": 1840, "provider": "demo"}),
            ("tool.pre", {"tool": "search_files", "args": {"pattern": "package.json|pyproject.toml", "path": "."}}),
            ("tool.post", {"tool": "search_files", "duration_ms": 84, "result": "found pyproject.toml, package.json", "is_error": False}),
            ("tool.pre", {"tool": "read_file", "args": {"path": "README.md"}}),
            ("tool.post", {"tool": "read_file", "duration_ms": 12, "result": "# Demo project\\nFastAPI + React", "is_error": False}),
            ("api.post", {"model": "demo-model", "response_model": "demo-model", "finish_reason": "stop", "usage": {"total_tokens": 2210}}),
            ("llm.post", {"assistant_response": "这是 FastAPI + React 项目，先跑 `pytest`，再跑 `pnpm dev`。", "model": "demo-model", "platform": "web"}),
            ("session.end", {"completed": True, "interrupted": False, "model": "demo-model", "platform": "web"}),
        ]
        prev = events.GENESIS_HASH
        batch = []
        for index, (event_type, payload) in enumerate(rows, start=1):
            ts = base + index
            digest = events.event_hash(
                tenant=tenant_id,
                session_id=session_id,
                seq=index,
                ts=ts,
                event_type=event_type,
                payload=payload,
                prev_hash=prev,
            )
            batch.append(
                {
                    "tenant": tenant_id,
                    "session_id": session_id,
                    "seq": index,
                    "ts": ts,
                    "type": event_type,
                    "payload": payload,
                    "prev_hash": prev,
                    "hash": digest,
                }
            )
            prev = digest
        result = db.ingest_events(conn, tenant_id, batch)
        print(json.dumps({"tenant": tenant_id, "session_id": session_id, **result}, ensure_ascii=False, indent=2))
        if token:
            print(f"tenant token (shown once): {token}")
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m portal.cli", description="Hermes Portal operator CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-user", help="create a portal user")
    p.add_argument("username")
    p.add_argument("password")
    p.add_argument("role", nargs="?", default="member", choices=["admin", "member", "viewer"])
    p.set_defaults(func=cmd_create_user)

    p = sub.add_parser("create-tenant", help="create a tenant and print its audit token")
    p.add_argument("name")
    p.add_argument("--id", dest="id", default=None)
    p.set_defaults(func=cmd_create_tenant)

    p = sub.add_parser("reset-password", help="reset a user's password and revoke sessions")
    p.add_argument("username")
    p.add_argument("password")
    p.set_defaults(func=cmd_reset_password)

    p = sub.add_parser("list", help="list users and tenants")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("verify", help="verify one session's audit hash chain")
    p.add_argument("tenant")
    p.add_argument("session_id")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("seed-demo", help="create a demo tenant and a synthetic audited session")
    p.add_argument("--tenant", default="demo")
    p.set_defaults(func=cmd_seed_demo)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
