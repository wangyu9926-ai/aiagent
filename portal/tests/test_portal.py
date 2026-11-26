from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from portal.app import create_app
from portal.config import Settings
from portal.events import event_hash


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


class PortalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.settings = _settings(self.data_dir)
        self.app = create_app(self.settings)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.csrf = self.login(self.client, "admin", ADMIN_PASSWORD)

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------

    def login(self, client: TestClient, username: str, password: str) -> str:
        response = client.post("/api/auth/login", json={"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["csrf"]

    def create_tenant(self, tenant_id: str = "team-a", name: str = "团队 A") -> str:
        response = self.client.post(
            "/api/tenants",
            json={"name": name, "id": tenant_id},
            headers={"X-Portal-CSRF": self.csrf},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"]

    def create_user(self, username: str, password: str, role: str) -> None:
        response = self.client.post(
            "/api/users",
            json={"username": username, "password": password, "role": role},
            headers={"X-Portal-CSRF": self.csrf},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def make_event(
        self,
        tenant: str,
        session_id: str,
        seq: int,
        prev_hash: str,
        *,
        event_type: str = "llm.pre",
        payload: dict | None = None,
    ) -> dict:
        payload = payload if payload is not None else {"user_message": f"message {seq}"}
        ts = time.time() + seq
        return {
            "tenant": tenant,
            "session_id": session_id,
            "seq": seq,
            "ts": ts,
            "type": event_type,
            "payload": payload,
            "prev_hash": prev_hash,
            "hash": event_hash(
                tenant=tenant,
                session_id=session_id,
                seq=seq,
                ts=ts,
                event_type=event_type,
                payload=payload,
                prev_hash=prev_hash,
            ),
        }

    def ingest(self, token: str, events: list[dict]):
        return self.client.post(
            "/api/audit/events",
            json={"events": events},
            headers={"Authorization": f"Bearer {token}"},
        )

    def chain(self, tenant: str, session_id: str, count: int = 2) -> list[dict]:
        events = []
        prev = ""
        for seq in range(1, count + 1):
            event = self.make_event(tenant, session_id, seq, prev)
            events.append(event)
            prev = event["hash"]
        return events

    # -- tests -----------------------------------------------------------

    def test_login_required_and_health(self) -> None:
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        with TestClient(self.app) as anon:
            self.assertEqual(anon.get("/api/stats").status_code, 401)
        self.assertEqual(
            self.client.post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code,
            401,
        )
        me = self.client.get("/api/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["user"]["role"], "admin")

    def test_https_forwarded_proto_marks_cookie_secure(self) -> None:
        with TestClient(self.app) as edge:
            response = edge.post(
                "/api/auth/login",
                json={"username": "admin", "password": ADMIN_PASSWORD},
                headers={"X-Forwarded-Proto": "https"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("secure", response.headers["set-cookie"].lower())

    def test_create_tenant_ingest_and_query(self) -> None:
        token = self.create_tenant()
        events = self.chain("team-a", "session-1", 2)
        response = self.ingest(token, events)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["accepted"], 2)

        listing = self.client.get("/api/audit/events", params={"tenant": "team-a"})
        self.assertEqual(listing.status_code, 200)
        items = listing.json()["events"]
        self.assertEqual([item["seq"] for item in items], [2, 1])

        detail = self.client.get(f"/api/audit/events/{items[0]['id']}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["payload"]["user_message"], "message 2")

        sessions = self.client.get("/api/sessions").json()["sessions"]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["event_count"], 2)

        verify = self.client.get(
            "/api/audit/verify",
            params={"tenant": "team-a", "session_id": "session-1"},
        )
        self.assertEqual(verify.status_code, 200)
        self.assertTrue(verify.json()["ok"])
        self.assertEqual(verify.json()["events"], 2)

    def test_duplicate_event_is_idempotent(self) -> None:
        token = self.create_tenant()
        event = self.make_event("team-a", "s", 1, "")
        self.assertEqual(self.ingest(token, [event]).status_code, 200)
        duplicate = self.ingest(token, [event])
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(duplicate.json()["duplicates"], 1)
        self.assertEqual(len(self.client.get("/api/audit/events").json()["events"]), 1)

    def test_gap_is_rejected_then_recovered(self) -> None:
        token = self.create_tenant()
        first = self.make_event("team-a", "s", 1, "")
        stale_third = self.make_event("team-a", "s", 3, first["hash"])
        self.assertEqual(self.ingest(token, [first]).status_code, 200)
        rejected = self.ingest(token, [stale_third])
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertEqual(rejected.json()["error"], "sequence_gap")
        self.assertEqual(rejected.json()["expected_seq"], 2)
        self.assertEqual(self.client.get("/api/stats").json()["gaps_today"], 1)

        second = self.make_event("team-a", "s", 2, first["hash"])
        third = self.make_event("team-a", "s", 3, second["hash"])
        recovered = self.ingest(token, [second, third])
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertTrue(
            self.client.get(
                "/api/audit/verify", params={"tenant": "team-a", "session_id": "s"}
            ).json()["ok"]
        )

    def test_tampered_hash_and_cross_tenant_rejected(self) -> None:
        token = self.create_tenant("team-a", "团队 A")
        other_token = self.create_tenant("team-b", "团队 B")
        event = self.make_event("team-a", "s", 1, "")
        event["hash"] = "0" * 64
        self.assertEqual(self.ingest(token, [event]).status_code, 409)

        wrong_tenant = self.make_event("team-b", "s", 1, "")
        response = self.ingest(token, [wrong_tenant])
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["message"], "事件租户与令牌不匹配")

        self.assertTrue(self.ingest(other_token, [wrong_tenant]).status_code == 200)

    def test_csrf_is_required_for_admin_writes(self) -> None:
        response = self.client.post("/api/tenants", json={"name": "no csrf"})
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            "/api/users",
            json={"username": "bob", "password": "password-123", "role": "member"},
            headers={"X-Portal-CSRF": "wrong"},
        )
        self.assertEqual(response.status_code, 403)

    def test_member_cannot_open_admin_surfaces(self) -> None:
        self.create_user("member", "member-pass-123", "member")
        with TestClient(self.app) as member_client:
            self.login(member_client, "member", "member-pass-123")
            self.assertEqual(member_client.get("/api/me").json()["user"]["role"], "member")
            self.assertEqual(member_client.get("/api/audit/events").status_code, 403)
            self.assertEqual(member_client.get("/api/stats").status_code, 403)
            self.assertEqual(member_client.get("/dashboard").status_code, 403)

    def test_tampering_in_database_breaks_verification(self) -> None:
        token = self.create_tenant()
        self.assertEqual(self.ingest(token, self.chain("team-a", "s", 2)).status_code, 200)
        import sqlite3

        conn = sqlite3.connect(self.settings.db_path)
        try:
            conn.execute("UPDATE audit_events SET payload = '{}' WHERE seq = 1")
            conn.commit()
        finally:
            conn.close()
        verify = self.client.get(
            "/api/audit/verify", params={"tenant": "team-a", "session_id": "s"}
        ).json()
        self.assertFalse(verify["ok"])
        self.assertEqual(verify["first_bad_seq"], 1)


if __name__ == "__main__":
    unittest.main()
