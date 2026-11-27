from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from portal import db
from portal.app import create_app
from portal.chat_api import ChatRun
from portal.config import Settings


ADMIN_PASSWORD = "admin-pass-123"


def _settings(data_dir: Path) -> Settings:
    return Settings(
        data_dir=data_dir,
        db_path=data_dir / "portal.db",
        admin_user="admin",
        admin_password=ADMIN_PASSWORD,
        session_ttl_seconds=3600,
        secure_cookies=False,
        max_body_bytes=8 * 1024 * 1024,
        login_max_attempts=20,
        login_window_seconds=300,
        bind_host="127.0.0.1",
        bind_port=9180,
    )


def _sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks)
    return (body + "data: [DONE]\n\n").encode("utf-8")


class ChatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.settings = _settings(self.data_dir)
        self.app = create_app(self.settings)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.admin_csrf = self.login(self.client, "admin", ADMIN_PASSWORD)
        self.tenant_token = self.create_tenant("team-chat")
        self.create_user("alice", "alice-pass-123", "member", "team-chat")
        self.create_user("reader", "reader-pass-123", "viewer", "team-chat")
        self.set_runtime("team-chat", "http://upstream.test", "sk-upstream", "demo-model")
        conn = db.connect(self.settings.db_path)
        try:
            db.record_heartbeat(conn, "team-chat", version="test")
        finally:
            conn.close()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------

    def login(self, client: TestClient, username: str, password: str) -> str:
        response = client.post("/api/auth/login", json={"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["csrf"]

    def create_tenant(self, tenant_id: str, name: str = "聊天团队") -> str:
        response = self.client.post(
            "/api/tenants",
            json={"name": name, "id": tenant_id},
            headers={"X-Portal-CSRF": self.admin_csrf},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"]

    def create_user(self, username: str, password: str, role: str, tenant_id: str | None = None) -> None:
        response = self.client.post(
            "/api/users",
            json={"username": username, "password": password, "role": role, "tenant_id": tenant_id},
            headers={"X-Portal-CSRF": self.admin_csrf},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def set_runtime(self, tenant_id: str, base_url: str, api_key: str, model: str) -> None:
        response = self.client.put(
            f"/api/tenants/{tenant_id}/runtime",
            json={"base_url": base_url, "api_key": api_key, "model": model},
            headers={"X-Portal-CSRF": self.admin_csrf},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def user_client(self, username: str, password: str):
        client = TestClient(self.app)
        client.__enter__()
        csrf = self.login(client, username, password)
        return client, csrf

    def make_conversation(self, client: TestClient, csrf: str, tenant_id: str = "team-chat") -> dict:
        response = client.post(
            "/api/chat/conversations",
            json={"tenant_id": tenant_id},
            headers={"X-Portal-CSRF": csrf},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["conversation"]

    # -- tests -----------------------------------------------------------

    def test_member_chat_streams_and_persists(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["auth"] = request.headers.get("Authorization")
            seen["session"] = request.headers.get("X-Hermes-Session-Id")
            seen["model"] = json.loads(request.content)["model"]
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse(
                    {"choices": [{"delta": {"content": "你好"}}]},
                    {"choices": [{"delta": {"content": "，世界"}}]},
                    {"choices": [], "usage": {"total_tokens": 42}},
                ),
            )

        self.app.state.chat_transport = httpx.MockTransport(handler)
        alice, csrf = self.user_client("alice", "alice-pass-123")
        try:
            conversation = self.make_conversation(alice, csrf)
            impersonation = self.client.post(
                f"/api/chat/conversations/{conversation['id']}/messages",
                json={"content": "admin should not speak for alice"},
                headers={"X-Portal-CSRF": self.admin_csrf},
            )
            self.assertEqual(impersonation.status_code, 403)
            response = alice.post(
                f"/api/chat/conversations/{conversation['id']}/messages",
                json={"content": "帮我看下项目"},
                headers={"X-Portal-CSRF": csrf},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("你好", response.text)
            self.assertIn("，世界", response.text)
            self.assertEqual(seen["path"], "/v1/chat/completions")
            self.assertEqual(seen["auth"], "Bearer sk-upstream")
            self.assertEqual(seen["session"], conversation["id"])
            self.assertEqual(seen["model"], "demo-model")

            messages = alice.get(f"/api/chat/conversations/{conversation['id']}/messages").json()["messages"]
            self.assertEqual([item["role"] for item in messages], ["user", "assistant"])
            self.assertEqual(messages[0]["content"], "帮我看下项目")
            self.assertEqual(messages[1]["content"], "你好，世界")
            self.assertEqual(messages[1]["meta"]["usage"]["total_tokens"], 42)
            detail = alice.get(f"/api/chat/conversations/{conversation['id']}/messages").json()["conversation"]
            self.assertEqual(detail["title"], "帮我看下项目")
        finally:
            alice.__exit__(None, None, None)

    def test_runtime_key_is_encrypted_at_rest_and_models_probe_works(self) -> None:
        conn = db.connect(self.settings.db_path)
        try:
            row = conn.execute("SELECT api_key_enc FROM tenant_runtimes WHERE tenant_id = 'team-chat'").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertNotIn("sk-upstream", row["api_key_enc"])
        self.assertEqual(self.app.state.secret_box.decrypt(row["api_key_enc"]), "sk-upstream")

        def models_handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v1/models")
            self.assertEqual(request.headers.get("Authorization"), "Bearer sk-upstream")
            return httpx.Response(200, json={"data": [{"id": "demo-model"}]})

        self.app.state.http_transport = httpx.MockTransport(models_handler)
        response = self.client.post(
            "/api/tenants/team-chat/runtime/test",
            headers={"X-Portal-CSRF": self.admin_csrf},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ok"])

    def test_viewer_can_read_but_not_send(self) -> None:
        conn = db.connect(self.settings.db_path)
        try:
            reader = db.get_user_by_username(conn, "reader")
            conversation = db.create_conversation(conn, "team-chat", reader["id"], "只读会话")
        finally:
            conn.close()
        reader_client, reader_csrf = self.user_client("reader", "reader-pass-123")
        try:
            listing = reader_client.get(f"/api/chat/conversations/{conversation['id']}/messages")
            self.assertEqual(listing.status_code, 200, listing.text)
            send = reader_client.post(
                f"/api/chat/conversations/{conversation['id']}/messages",
                json={"content": "不该发送"},
                headers={"X-Portal-CSRF": reader_csrf},
            )
            self.assertEqual(send.status_code, 403)
            create = reader_client.post(
                "/api/chat/conversations",
                json={"tenant_id": "team-chat"},
                headers={"X-Portal-CSRF": reader_csrf},
            )
            self.assertEqual(create.status_code, 403)
        finally:
            reader_client.__exit__(None, None, None)

    def test_member_cannot_access_another_tenant(self) -> None:
        self.create_tenant("other-team", "别的团队")
        alice, csrf = self.user_client("alice", "alice-pass-123")
        try:
            listing = alice.get("/api/chat/conversations", params={"tenant_id": "other-team"})
            self.assertEqual(listing.status_code, 403)
        finally:
            alice.__exit__(None, None, None)

    def test_admin_can_approve_pending_tool_call(self) -> None:
        alice, csrf = self.user_client("alice", "alice-pass-123")
        try:
            conversation = self.make_conversation(alice, csrf)
            conn = db.connect(self.settings.db_path)
            try:
                alice_id = db.get_user_by_username(conn, "alice")["id"]
                approval = db.create_approval(
                    conn,
                    tenant_id="team-chat",
                    conversation_id=conversation["id"],
                    user_id=alice_id,
                    run_id="run-approval-1",
                    request_id="req-1",
                    tool="terminal",
                    command="echo approved",
                    description="test approval",
                )
            finally:
                conn.close()
            seen = {}

            def handler(request: httpx.Request) -> httpx.Response:
                if request.method == "GET":
                    seen["status_path"] = request.url.path
                    return httpx.Response(
                        200,
                        json={
                            "execution_generation": 7,
                            "pending_controls": [{"prompt_id": "req-1", "kind": "approval"}],
                        },
                    )
                seen["path"] = request.url.path
                seen["body"] = json.loads(request.content)
                return httpx.Response(200, json={"ok": True})

            self.app.state.http_transport = httpx.MockTransport(handler)
            member_attempt = alice.post(
                f"/api/approvals/{approval['id']}",
                json={"choice": "once"},
                headers={"X-Portal-CSRF": csrf},
            )
            self.assertEqual(member_attempt.status_code, 403)
            response = self.client.post(
                f"/api/approvals/{approval['id']}",
                json={"choice": "once"},
                headers={"X-Portal-CSRF": self.admin_csrf},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(seen["status_path"], "/v1/runs/run-approval-1")
            self.assertEqual(seen["path"], "/v1/runs/run-approval-1/approval")
            self.assertEqual(seen["body"], {"choice": "once", "request_id": "req-1", "execution_generation": 7})
            conn = db.connect(self.settings.db_path)
            try:
                row = db.get_approval(conn, approval["id"])
            finally:
                conn.close()
            self.assertEqual(row["status"], "approved")
        finally:
            alice.__exit__(None, None, None)

    def test_active_background_run_blocks_second_send_and_reports_state(self) -> None:
        alice, csrf = self.user_client("alice", "alice-pass-123")
        try:
            conversation = self.make_conversation(alice, csrf)
            idle = alice.get(f"/api/chat/conversations/{conversation['id']}/active")
            self.assertEqual(idle.status_code, 200)
            self.assertFalse(idle.json()["active"])
            self.app.state.chat_runs = {
                conversation["id"]: ChatRun(conversation["id"])
            }
            blocked = alice.post(
                f"/api/chat/conversations/{conversation['id']}/messages",
                json={"content": "第二条"},
                headers={"X-Portal-CSRF": csrf},
            )
            self.assertEqual(blocked.status_code, 409)
            active = alice.get(f"/api/chat/conversations/{conversation['id']}/active").json()
            self.assertTrue(active["active"])
            self.assertEqual(active["status"], "running")
            self.app.state.chat_runs[conversation["id"]].finish("done")
            done = alice.get(f"/api/chat/conversations/{conversation['id']}/active").json()
            self.assertFalse(done["active"])
            self.assertEqual(done["status"], "done")
        finally:
            alice.__exit__(None, None, None)

    def test_project_creation_workspace_and_onboarding(self) -> None:
        response = self.client.post(
            "/api/projects",
            json={
                "name": "订单中台",
                "tenant_id": "team-chat",
                "goal": "先摸清项目，找出最值得做的三个改进",
                "repo_url": "https://example.com/acme/orders.git",
                "known": "Python + Postgres",
                "unknowns": "部署方式和测试入口未知",
            },
            headers={"X-Portal-CSRF": self.admin_csrf},
        )
        self.assertEqual(response.status_code, 200, response.text)
        project = response.json()["project"]
        self.assertTrue(project["id"].startswith("prj_"))
        workspace = Path(self.settings.upload_dir or self.settings.data_dir / "uploads") / (
            "team-chat/projects/" + project["id"]
        )
        self.assertTrue((workspace / "PROJECT.md").is_file())
        self.assertTrue((workspace / "repo").is_dir())

        alice, csrf = self.user_client("alice", "alice-pass-123")
        try:
            listing = alice.get("/api/projects").json()["projects"]
            self.assertEqual([item["id"] for item in listing], [project["id"]])
            onboarding = alice.post(
                f"/api/projects/{project['id']}/onboard",
                headers={"X-Portal-CSRF": csrf},
            )
            self.assertEqual(onboarding.status_code, 200, onboarding.text)
            payload = onboarding.json()
            self.assertEqual(payload["conversation"]["tenant_id"], "team-chat")
            self.assertIn("订单中台", payload["prompt"])
            self.assertIn("ONBOARDING.md", payload["prompt"])
            missing = alice.get(f"/api/projects/{project['id']}/report")
            self.assertEqual(missing.status_code, 404)
            (workspace / "ONBOARDING.md").write_text("# 接手报告\n\n内容", encoding="utf-8")
            report = alice.get(f"/api/projects/{project['id']}/report")
            self.assertEqual(report.status_code, 200, report.text)
            self.assertIn("接手报告", report.json()["content"])
            listed = alice.get("/api/projects").json()["projects"][0]
            self.assertTrue(listed["report_exists"])
            self.assertGreater(listed["report_size"], 0)
        finally:
            alice.__exit__(None, None, None)

    def test_project_not_visible_to_other_tenant_member(self) -> None:
        self.create_tenant("other-team", "另一个团队")
        self.create_user("bob", "bob-pass-123", "member", "other-team")
        project = self.client.post(
            "/api/projects",
            json={"name": "内部项目", "tenant_id": "team-chat", "goal": "只给 team-chat 看"},
            headers={"X-Portal-CSRF": self.admin_csrf},
        ).json()["project"]
        bob, csrf = self.user_client("bob", "bob-pass-123")
        try:
            self.assertEqual(bob.get("/api/projects").json()["projects"], [])
            response = bob.post(
                f"/api/projects/{project['id']}/onboard",
                headers={"X-Portal-CSRF": csrf},
            )
            self.assertEqual(response.status_code, 403)
        finally:
            bob.__exit__(None, None, None)

    def test_skill_catalog_and_turn_injection(self) -> None:
        alice, csrf = self.user_client("alice", "alice-pass-123")
        try:
            listing = alice.get("/api/skills")
            self.assertEqual(listing.status_code, 200, listing.text)
            ids = [item["id"] for item in listing.json()["skills"]]
            self.assertIn("data-analysis", ids)
            detail = alice.get("/api/skills/data-analysis")
            self.assertEqual(detail.status_code, 200, detail.text)
            self.assertIn("数据分析技能", detail.json()["content"])

            seen = {}

            def handler(request: httpx.Request) -> httpx.Response:
                seen["body"] = json.loads(request.content)
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=_sse({"choices": [{"delta": {"content": "已按技能执行"}}]}),
                )

            self.app.state.chat_transport = httpx.MockTransport(handler)
            conversation = self.make_conversation(alice, csrf)
            response = alice.post(
                f"/api/chat/conversations/{conversation['id']}/messages",
                json={"content": "分析这份数据", "skill_ids": ["data-analysis"]},
                headers={"X-Portal-CSRF": csrf},
            )
            self.assertEqual(response.status_code, 200, response.text)
            sent = seen["body"]["messages"][-1]["content"]
            self.assertIn("本轮使用服务端技能", sent)
            self.assertIn("数据分析技能", sent)
            messages = alice.get(
                f"/api/chat/conversations/{conversation['id']}/messages"
            ).json()["messages"]
            self.assertEqual(messages[0]["meta"]["skills"], ["data-analysis"])
        finally:
            alice.__exit__(None, None, None)

    def test_private_and_team_memory_scopes(self) -> None:
        alice, alice_csrf = self.user_client("alice", "alice-pass-123")
        self.create_user("bob", "bob-pass-123", "member", "team-chat")
        bob, bob_csrf = self.user_client("bob", "bob-pass-123")
        try:
            alice_conversation = self.make_conversation(alice, alice_csrf)
            bob_conversation = self.make_conversation(bob, bob_csrf)
            private = alice.post(
                f"/api/chat/conversations/{alice_conversation['id']}/memories",
                json={"content": "Alice 的私有口径", "scope": "private"},
                headers={"X-Portal-CSRF": alice_csrf},
            )
            self.assertEqual(private.status_code, 200, private.text)
            alice_memories = alice.get(
                f"/api/chat/conversations/{alice_conversation['id']}/memories"
            ).json()["memories"]
            self.assertEqual([item["content"] for item in alice_memories], ["Alice 的私有口径"])
            bob_memories = bob.get(
                f"/api/chat/conversations/{bob_conversation['id']}/memories"
            ).json()["memories"]
            self.assertEqual(bob_memories, [])
            denied_team = bob.post(
                f"/api/chat/conversations/{bob_conversation['id']}/memories",
                json={"content": "Bob 想写团队记忆", "scope": "team"},
                headers={"X-Portal-CSRF": bob_csrf},
            )
            self.assertEqual(denied_team.status_code, 403)
            admin_conversation = self.make_conversation(self.client, self.admin_csrf)
            team = self.client.post(
                f"/api/chat/conversations/{admin_conversation['id']}/memories",
                json={"content": "团队统一按财务口径", "scope": "team"},
                headers={"X-Portal-CSRF": self.admin_csrf},
            )
            self.assertEqual(team.status_code, 200, team.text)
            visible = bob.get(
                f"/api/chat/conversations/{bob_conversation['id']}/memories"
            ).json()["memories"]
            self.assertEqual([item["content"] for item in visible], ["团队统一按财务口径"])
        finally:
            alice.__exit__(None, None, None)
            bob.__exit__(None, None, None)

    def test_chat_injects_private_and_team_memories(self) -> None:
        alice, csrf = self.user_client("alice", "alice-pass-123")
        try:
            conversation = self.make_conversation(alice, csrf)
            alice.post(
                f"/api/chat/conversations/{conversation['id']}/memories",
                json={"content": "Alice 私有备注", "scope": "private"},
                headers={"X-Portal-CSRF": csrf},
            )
            admin_conversation = self.make_conversation(self.client, self.admin_csrf)
            self.client.post(
                f"/api/chat/conversations/{admin_conversation['id']}/memories",
                json={"content": "团队共享口径", "scope": "team"},
                headers={"X-Portal-CSRF": self.admin_csrf},
            )
            seen = {}

            def handler(request: httpx.Request) -> httpx.Response:
                seen["content"] = json.loads(request.content)["messages"][-1]["content"]
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=_sse({"choices": [{"delta": {"content": "ok"}}]}),
                )

            self.app.state.chat_transport = httpx.MockTransport(handler)
            response = alice.post(
                f"/api/chat/conversations/{conversation['id']}/messages",
                json={"content": "你好"},
                headers={"X-Portal-CSRF": csrf},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("Alice 私有备注", seen["content"])
            self.assertIn("团队共享口径", seen["content"])
        finally:
            alice.__exit__(None, None, None)

    def test_heartbeat_reports_skill_and_memory_assets(self) -> None:
        response = self.client.post(
            "/api/tenants/heartbeat",
            json={
                "version": "test",
                "skill_count": 7,
                "memory_count": 3,
                "recent_skills": ["productivity/new-skill", "software-development/audit"],
            },
            headers={"Authorization": f"Bearer {self.tenant_token}"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        assets = self.client.get("/api/assets")
        self.assertEqual(assets.status_code, 200, assets.text)
        tenant = next(
            item for item in assets.json()["tenants"] if item["tenant_id"] == "team-chat"
        )
        self.assertEqual(tenant["skill_count"], 7)
        self.assertEqual(tenant["memory_count"], 3)
        self.assertEqual(
            tenant["recent_skills"],
            ["productivity/new-skill", "software-development/audit"],
        )
        self.assertIn("data-analysis", [item["id"] for item in assets.json()["server_skills"]])

    def test_datalab_proxy_validates_workspace_and_forwards_tenant_token(self) -> None:
        self.app.state.settings = dataclasses.replace(
            self.app.state.settings,
            datalab_url="http://datalab.test:8090",
            datalab_token="worker-secret",
        )
        alice, csrf = self.user_client("alice", "alice-pass-123")
        try:
            conversation = self.make_conversation(alice, csrf)
            upload_root = self.settings.upload_dir or self.settings.data_dir / "uploads"
            workspace = upload_root / "team-chat" / conversation["id"] / "local"
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
            seen = {}

            def handler(request: httpx.Request) -> httpx.Response:
                seen["path"] = request.url.path
                seen["token"] = request.headers.get("x-datalab-token")
                seen["body"] = json.loads(request.content)
                return httpx.Response(
                    200,
                    json={"columns": ["n"], "rows": [[1]], "row_count": 1, "truncated": False},
                )

            self.app.state.http_transport = httpx.MockTransport(handler)
            response = alice.post(
                "/api/datalab/query",
                json={
                    "conversation_id": conversation["id"],
                    "tables": [
                        {
                            "path": f"/uploads/team-chat/{conversation['id']}/local/data.csv",
                            "table": "data",
                        }
                    ],
                    "sql": "select count(*) as n from data",
                    "limit": 10,
                },
                headers={"Authorization": f"Bearer {self.tenant_token}"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["rows"], [[1]])
            self.assertEqual(seen["path"], "/query")
            self.assertEqual(seen["token"], "worker-secret")
            self.assertEqual(seen["body"]["tables"][0]["path"], f"team-chat/{conversation['id']}/local/data.csv")
            blocked = alice.post(
                "/api/datalab/query",
                json={
                    "conversation_id": conversation["id"],
                    "tables": [{"path": "/uploads/team-chat/other/data.csv", "table": "data"}],
                    "sql": "select 1",
                },
                headers={"Authorization": f"Bearer {self.tenant_token}"},
            )
            self.assertEqual(blocked.status_code, 403)
        finally:
            alice.__exit__(None, None, None)

    def test_datalab_database_connections_use_conversation_owner_role(self) -> None:
        self.app.state.settings = dataclasses.replace(
            self.app.state.settings,
            datalab_url="http://datalab.test:8090",
            datalab_token="worker-secret",
        )
        alice, csrf = self.user_client("alice", "alice-pass-123")
        try:
            conversation = self.make_conversation(alice, csrf)
            seen = {}

            def handler(request: httpx.Request) -> httpx.Response:
                if request.url.path == "/db/connections":
                    seen["role"] = request.url.params.get("role")
                    return httpx.Response(
                        200,
                        json={
                            "connections": [
                                {"id": "sales_demo", "type": "sqlite", "description": "sales"}
                            ],
                            "role": seen["role"],
                        },
                    )
                seen["db_body"] = json.loads(request.content)
                return httpx.Response(403, json={"error": "connection is not allowed for this role"})

            self.app.state.http_transport = httpx.MockTransport(handler)
            listing = alice.get(
                "/api/datalab/databases",
                params={"conversation_id": conversation["id"]},
                headers={"Authorization": f"Bearer {self.tenant_token}"},
            )
            self.assertEqual(listing.status_code, 200, listing.text)
            self.assertEqual(seen["role"], "member")
            denied = alice.post(
                "/api/datalab/db-query",
                json={
                    "conversation_id": conversation["id"],
                    "connection": "finance_demo",
                    "sql": "select 1",
                },
                headers={"Authorization": f"Bearer {self.tenant_token}"},
            )
            self.assertEqual(denied.status_code, 400)
            self.assertEqual(seen["db_body"]["role"], "member")
        finally:
            alice.__exit__(None, None, None)

    def test_datalab_chart_is_saved_and_downloadable(self) -> None:
        self.app.state.settings = dataclasses.replace(
            self.app.state.settings,
            datalab_url="http://datalab.test:8090",
            datalab_token="worker-secret",
        )
        alice, csrf = self.user_client("alice", "alice-pass-123")
        try:
            conversation = self.make_conversation(alice, csrf)
            upload_root = self.settings.upload_dir or self.settings.data_dir / "uploads"
            workspace = upload_root / "team-chat" / conversation["id"] / "local"
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
            png = (
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
                "/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="
            )

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200,
                    json={
                        "columns": ["a"],
                        "rows": [[1]],
                        "row_count": 1,
                        "truncated": False,
                        "chart_png_base64": png,
                        "chart_mime": "image/png",
                    },
                )

            self.app.state.http_transport = httpx.MockTransport(handler)
            response = alice.post(
                "/api/datalab/query",
                json={
                    "conversation_id": conversation["id"],
                    "tables": [
                        {
                            "path": f"/uploads/team-chat/{conversation['id']}/local/data.csv",
                            "table": "data",
                        }
                    ],
                    "sql": "select a from data",
                    "chart": {"type": "bar", "x": "a"},
                },
                headers={"Authorization": f"Bearer {self.tenant_token}"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertNotIn("chart_png_base64", payload)
            self.assertEqual(payload["chart_file"]["size"], 68)
            download = alice.get(payload["chart_file"]["url"])
            self.assertEqual(download.status_code, 200)
            self.assertTrue(download.content.startswith(b"\x89PNG"))
            messages = alice.get(
                f"/api/chat/conversations/{conversation['id']}/messages"
            ).json()["messages"]
            self.assertTrue(
                any(item["meta"] and item["meta"].get("files") for item in messages)
            )
        finally:
            alice.__exit__(None, None, None)

    def test_local_folder_bridge_round_trip(self) -> None:
        alice, csrf = self.user_client("alice", "alice-pass-123")
        try:
            conversation = self.make_conversation(alice, csrf)
            connected = alice.post(
                f"/api/chat/conversations/{conversation['id']}/local-folder",
                json={"name": "我的资料"},
                headers={"X-Portal-CSRF": csrf},
            )
            self.assertEqual(connected.status_code, 200, connected.text)
            self.assertEqual(connected.json()["folder"]["server_path"].split("/")[-1], "local")
            uploaded = alice.post(
                f"/api/chat/conversations/{conversation['id']}/local-folder/upload",
                data={"paths": ["notes/a.txt", "notes/b.txt"]},
                files=[
                    ("files", ("a.txt", b"hello", "text/plain")),
                    ("files", ("b.txt", b"world", "text/plain")),
                ],
                headers={"X-Portal-CSRF": csrf},
            )
            self.assertEqual(uploaded.status_code, 200, uploaded.text)
            self.assertEqual(uploaded.json()["accepted"], 2)
            manifest = alice.get(
                f"/api/chat/conversations/{conversation['id']}/local-folder/files"
            )
            self.assertEqual(manifest.status_code, 200, manifest.text)
            paths = [item["path"] for item in manifest.json()["files"]]
            self.assertEqual(paths, ["notes/a.txt", "notes/b.txt"])
            download = alice.get(
                f"/api/chat/conversations/{conversation['id']}/local-folder/download",
                params={"path": "notes/a.txt"},
            )
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.content, b"hello")
            blocked = alice.post(
                f"/api/chat/conversations/{conversation['id']}/local-folder/upload",
                data={"paths": ["../escape.txt"]},
                files=[("files", ("escape.txt", b"x", "text/plain"))],
                headers={"X-Portal-CSRF": csrf},
            )
            self.assertEqual(blocked.status_code, 400)
            removed = alice.delete(
                f"/api/chat/conversations/{conversation['id']}/local-folder",
                headers={"X-Portal-CSRF": csrf},
            )
            self.assertEqual(removed.status_code, 200, removed.text)
            self.assertFalse(
                alice.get(f"/api/chat/conversations/{conversation['id']}/local-folder").json()["connected"]
            )
        finally:
            alice.__exit__(None, None, None)

    def test_stale_heartbeat_blocks_chat(self) -> None:
        alice, csrf = self.user_client("alice", "alice-pass-123")
        try:
            conversation = self.make_conversation(alice, csrf)
            conn = db.connect(self.settings.db_path)
            try:
                conn.execute("DELETE FROM tenant_heartbeats WHERE tenant_id = 'team-chat'")
                conn.commit()
            finally:
                conn.close()
            response = alice.post(
                f"/api/chat/conversations/{conversation['id']}/messages",
                json={"content": "hi"},
                headers={"X-Portal-CSRF": csrf},
            )
            self.assertEqual(response.status_code, 409, response.text)
            self.assertIn("心跳", response.json()["detail"])
        finally:
            alice.__exit__(None, None, None)

    def test_missing_runtime_returns_409(self) -> None:
        self.create_tenant("no-runtime")
        conn = db.connect(self.settings.db_path)
        try:
            alice_id = db.get_user_by_username(conn, "alice")["id"]
        finally:
            conn.close()
        grant = self.client.post(
            f"/api/users/{alice_id}/tenants",
            json={"tenant_id": "no-runtime", "access_role": "member"},
            headers={"X-Portal-CSRF": self.admin_csrf},
        )
        self.assertEqual(grant.status_code, 200, grant.text)
        alice, csrf = self.user_client("alice", "alice-pass-123")
        try:
            conversation = self.make_conversation(alice, csrf, "no-runtime")
            response = alice.post(
                f"/api/chat/conversations/{conversation['id']}/messages",
                json={"content": "hi"},
                headers={"X-Portal-CSRF": csrf},
            )
            self.assertEqual(response.status_code, 409)
        finally:
            alice.__exit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
