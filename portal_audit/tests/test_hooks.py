from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import portal_audit


class FakeClient:
    fail_closed = True
    config = SimpleNamespace(tenant="tenant-a", base_url="http://127.0.0.1:9180")

    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.calls: list[tuple[tuple, dict]] = []

    def emit(self, *args, **kwargs) -> bool:
        self.calls.append((args, kwargs))
        return self.ok


class FakeCtx:
    def __init__(self) -> None:
        self.hooks: dict[str, object] = {}

    def register_hook(self, name, callback) -> None:
        self.hooks[name] = callback


class HookTests(unittest.TestCase):
    def test_asset_snapshot_counts_skills_and_memories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            skill = home / "skills" / "productivity" / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("# demo", encoding="utf-8")
            memories = home / "memories"
            memories.mkdir()
            (memories / "note.md").write_text("note", encoding="utf-8")
            with mock.patch.dict(os.environ, {"HERMES_HOME": str(home)}):
                portal_audit._ASSET_CACHE = (0.0, {})
                snapshot = portal_audit._asset_snapshot()
        self.assertEqual(snapshot["skill_count"], 1)
        self.assertEqual(snapshot["memory_count"], 1)
        self.assertEqual(snapshot["recent_skills"], ["productivity/demo"])

    def test_registers_all_hooks(self) -> None:
        ctx = FakeCtx()
        portal_audit.register(ctx, client=FakeClient(ok=True))
        self.assertEqual(set(ctx.hooks), set(portal_audit.PROVIDED_HOOKS))

    def test_pre_hooks_block_when_audit_is_down(self) -> None:
        ctx = FakeCtx()
        portal_audit.register(ctx, client=FakeClient(ok=False))
        blocked_turn = ctx.hooks["pre_llm_call"](user_message="hi", session_id="s")
        self.assertEqual(blocked_turn["action"], "block")
        blocked_tool = ctx.hooks["pre_tool_call"](tool_name="terminal", args={"cmd": "rm"}, session_id="s")
        self.assertEqual(blocked_tool["action"], "block")
        self.assertIsNone(ctx.hooks["post_llm_call"](session_id="s", assistant_response="ok"))

    def test_pre_hooks_pass_when_audit_is_up(self) -> None:
        ctx = FakeCtx()
        portal_audit.register(ctx, client=FakeClient(ok=True))
        self.assertIsNone(ctx.hooks["pre_llm_call"](user_message="hi", session_id="s"))
        self.assertIsNone(ctx.hooks["pre_tool_call"](tool_name="terminal", args={}, session_id="s"))

    def test_payload_contains_full_tool_result(self) -> None:
        ctx = FakeCtx()
        client = FakeClient(ok=True)
        portal_audit.register(ctx, client=client)
        ctx.hooks["post_tool_call"](
            tool_name="terminal", args={"cmd": "ls"}, result="a\nb", duration_ms=12, session_id="s"
        )
        event_type, payload = client.calls[-1][0]
        self.assertEqual(event_type, "tool.post")
        self.assertEqual(payload["result"], "a\nb")
        self.assertEqual(payload["duration_ms"], 12)


if __name__ == "__main__":
    unittest.main()
