from __future__ import annotations

import json
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from portal.events import event_hash as portal_event_hash
from portal_audit.client import AuditConfig, PortalAuditClient, sanitize_payload


class FakeTransport:
    def __init__(self, responses=None, error: Exception | None = None, delay: float = 0.0) -> None:
        self.responses = list(responses or [])
        self.error = error
        self.delay = delay
        self.calls: list[tuple[str, str, bytes, float]] = []
        self.events: list[dict] = []
        self.bodies: list[dict] = []

    def __call__(self, url, token, body, timeout):
        if self.delay:
            time.sleep(self.delay)
        self.calls.append((url, token, body, timeout))
        payload = json.loads(body)
        self.bodies.append(payload)
        if "events" in payload:
            self.events.extend(payload["events"])
        if self.error is not None:
            raise self.error
        if self.responses:
            return self.responses.pop(0)
        return 200, {"accepted": 1}


def config(**overrides) -> AuditConfig:
    values = dict(
        base_url="http://127.0.0.1:9180",
        tenant="tenant-a",
        token="t0ken",
        timeout_seconds=0.2,
        fail_closed=True,
        max_payload_bytes=4 * 1024 * 1024,
    )
    values.update(overrides)
    return AuditConfig(**values)


class ClientTests(unittest.TestCase):
    def test_chain_matches_portal_hash(self) -> None:
        transport = FakeTransport()
        client = PortalAuditClient(config(), transport=transport)
        self.assertTrue(client.emit("llm.pre", {"user_message": "hi"}, session_id="s"))
        self.assertTrue(client.emit("tool.pre", {"tool": "terminal", "args": {"cmd": "ls"}}, session_id="s"))
        self.assertEqual([event["seq"] for event in transport.events], [1, 2])
        self.assertEqual(transport.events[0]["prev_hash"], "")
        self.assertEqual(transport.events[1]["prev_hash"], transport.events[0]["hash"])
        for event in transport.events:
            self.assertEqual(
                event["hash"],
                portal_event_hash(
                    tenant=event["tenant"],
                    session_id=event["session_id"],
                    seq=event["seq"],
                    ts=event["ts"],
                    event_type=event["type"],
                    payload=event["payload"],
                    prev_hash=event["prev_hash"],
                ),
            )

    def test_resync_from_portal_head(self) -> None:
        expected_prev = "a" * 64
        transport = FakeTransport(
            responses=[(409, {"expected_seq": 5, "expected_prev_hash": expected_prev}), (200, {})]
        )
        client = PortalAuditClient(config(), transport=transport)
        self.assertTrue(client.emit("llm.pre", {"user_message": "after restart"}, session_id="s"))
        self.assertEqual(transport.events[-1]["seq"], 5)
        self.assertEqual(transport.events[-1]["prev_hash"], expected_prev)
        accepted = transport.events[-1]
        self.assertTrue(client.emit("llm.pre", {"user_message": "next"}, session_id="s"))
        self.assertEqual(transport.events[-1]["seq"], 6)
        self.assertEqual(transport.events[-1]["prev_hash"], accepted["hash"])

    def test_concurrent_emits_use_unique_ordered_sequences(self) -> None:
        transport = FakeTransport(delay=0.002)
        client = PortalAuditClient(config(), transport=transport)

        def emit(index: int) -> bool:
            return client.emit("tool.pre", {"tool": "terminal", "index": index}, session_id="s")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(emit, range(20)))
        self.assertTrue(all(results))
        seqs = [event["seq"] for event in transport.events]
        self.assertEqual(seqs, list(range(1, 21)))

    def test_heartbeat_posts_to_heartbeat_endpoint(self) -> None:
        transport = FakeTransport()
        client = PortalAuditClient(config(), transport=transport)
        self.assertTrue(client.heartbeat(version="0.1.0", pid=123, hostname="test-host"))
        self.assertTrue(transport.calls[-1][0].endswith("/api/tenants/heartbeat"))
        self.assertEqual(transport.bodies[-1]["hostname"], "test-host")

    def test_network_failure_returns_false_and_opens_circuit(self) -> None:
        transport = FakeTransport(error=RuntimeError("down"))
        client = PortalAuditClient(config(), transport=transport)
        self.assertFalse(client.emit("llm.pre", {"user_message": "hi"}, session_id="s"))
        self.assertFalse(client.emit("llm.pre", {"user_message": "again"}, session_id="s"))
        self.assertEqual(len(transport.calls), 1)

    def test_redaction_keeps_usage_metrics(self) -> None:
        payload = {
            "api_key": "sk-secret",
            "max_tokens": 4096,
            "nested": {"password": "hunter2", "total_tokens": 123},
            "messages": [{"role": "user", "content": "hello"}],
        }
        clean = sanitize_payload(payload, max_bytes=4096, max_string_chars=1024)
        self.assertEqual(clean["api_key"], "***REDACTED***")
        self.assertEqual(clean["max_tokens"], 4096)
        self.assertEqual(clean["nested"]["password"], "***REDACTED***")
        self.assertEqual(clean["nested"]["total_tokens"], 123)
        self.assertEqual(clean["messages"][0]["content"], "hello")

    def test_large_payload_is_bounded_with_marker(self) -> None:
        payload = {"big": "a" * 5000, "api_key": "sk-secret"}
        clean = sanitize_payload(payload, max_bytes=2000, max_string_chars=200)
        self.assertTrue(clean.get("_truncated"))
        self.assertEqual(clean["api_key"], "***REDACTED***")
        self.assertLess(len(json.dumps(clean)), 4000)


if __name__ == "__main__":
    unittest.main()
