from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from portal import db
from portal.app import create_app
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


class AttachmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.settings = _settings(self.data_dir)
        self.app = create_app(self.settings)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.admin_csrf = self.login(self.client, "admin", ADMIN_PASSWORD)
        self.create_tenant("attach-team")
        self.create_user("alice", "alice-pass-123", "member", "attach-team")
        self.create_user("reader", "reader-pass-123", "viewer", "attach-team")
        self.set_runtime("attach-team")
        conn = db.connect(self.settings.db_path)
        try:
            db.record_heartbeat(conn, "attach-team", version="test")
        finally:
            conn.close()
        self.seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.seen.append(json.loads(request.content))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(
                    'data: {"choices":[{"delta":{"content":"已收到附件"}}]}\n\n'
                    "data: [DONE]\n\n"
                ).encode("utf-8"),
            )

        self.app.state.chat_transport = httpx.MockTransport(handler)

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def login(self, client: TestClient, username: str, password: str) -> str:
        response = client.post("/api/auth/login", json={"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["csrf"]

    def create_tenant(self, tenant_id: str) -> None:
        response = self.client.post(
            "/api/tenants",
            json={"name": "附件团队", "id": tenant_id},
            headers={"X-Portal-CSRF": self.admin_csrf},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def create_user(self, username: str, password: str, role: str, tenant_id: str) -> None:
        response = self.client.post(
            "/api/users",
            json={"username": username, "password": password, "role": role, "tenant_id": tenant_id},
            headers={"X-Portal-CSRF": self.admin_csrf},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def set_runtime(self, tenant_id: str) -> None:
        response = self.client.put(
            f"/api/tenants/{tenant_id}/runtime",
            json={"base_url": "http://upstream.test", "api_key": "sk-test", "model": "demo"},
            headers={"X-Portal-CSRF": self.admin_csrf},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def member(self, username: str, password: str):
        client = TestClient(self.app)
        client.__enter__()
        csrf = self.login(client, username, password)
        return client, csrf

    def conversation(self, client: TestClient, csrf: str) -> str:
        response = client.post(
            "/api/chat/conversations",
            json={"tenant_id": "attach-team"},
            headers={"X-Portal-CSRF": csrf},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["conversation"]["id"]

    def upload(self, client: TestClient, csrf: str, conversation_id: str, name: str, payload: bytes):
        return client.post(
            f"/api/chat/conversations/{conversation_id}/attachments",
            files=[("files", (name, payload, "text/csv"))],
            headers={"X-Portal-CSRF": csrf},
        )

    def test_upload_then_send_points_agent_at_file(self) -> None:
        alice, csrf = self.member("alice", "alice-pass-123")
        try:
            conversation_id = self.conversation(alice, csrf)
            upload = self.upload(
                alice, csrf, conversation_id, "sales.csv", b"month,amount\n1,100\n2,200\n"
            )
            self.assertEqual(upload.status_code, 200, upload.text)
            attachment = upload.json()["attachments"][0]
            self.assertEqual(attachment["name"], "sales.csv")
            self.assertGreater(attachment["size"], 0)

            conn = db.connect(self.settings.db_path)
            try:
                row = conn.execute(
                    "SELECT * FROM chat_attachments WHERE id = ?", (attachment["id"],)
                ).fetchone()
            finally:
                conn.close()
            self.assertTrue(Path(row["stored_path"]).is_file())

            response = alice.post(
                f"/api/chat/conversations/{conversation_id}/messages",
                json={"content": "分析这个 CSV", "attachment_ids": [attachment["id"]]},
                headers={"X-Portal-CSRF": csrf},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(self.seen), 1)
            last_user = [m for m in self.seen[0]["messages"] if m["role"] == "user"][-1]
            self.assertIn("sales.csv", last_user["content"])
            self.assertIn(row["stored_path"], last_user["content"])
            self.assertIn("[用户上传了附件", last_user["content"])

            messages = alice.get(
                f"/api/chat/conversations/{conversation_id}/messages"
            ).json()["messages"]
            self.assertEqual(messages[0]["meta"]["attachments"][0]["name"], "sales.csv")
        finally:
            alice.__exit__(None, None, None)

    def test_viewer_cannot_upload(self) -> None:
        conn = db.connect(self.settings.db_path)
        try:
            reader = db.get_user_by_username(conn, "reader")
            conversation = db.create_conversation(conn, "attach-team", reader["id"], "只读")
        finally:
            conn.close()
        reader_client, csrf = self.member("reader", "reader-pass-123")
        try:
            response = self.upload(reader_client, csrf, conversation["id"], "x.csv", b"a,b\n")
            self.assertEqual(response.status_code, 403)
        finally:
            reader_client.__exit__(None, None, None)

    def test_blocked_extension(self) -> None:
        alice, csrf = self.member("alice", "alice-pass-123")
        try:
            conversation_id = self.conversation(alice, csrf)
            response = self.upload(alice, csrf, conversation_id, "evil.bat", b"echo hi")
            self.assertEqual(response.status_code, 400)
        finally:
            alice.__exit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
