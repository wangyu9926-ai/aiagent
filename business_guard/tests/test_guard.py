from __future__ import annotations

import os
import unittest
from unittest import mock

import business_guard


class FakeCtx:
    def __init__(self) -> None:
        self.hooks: dict[str, object] = {}

    def register_hook(self, name, callback) -> None:
        self.hooks[name] = callback


class GuardTests(unittest.TestCase):
    def test_sensitive_home_paths_blocked(self) -> None:
        with mock.patch.dict(os.environ, {"HERMES_HOME": "/opt/data"}):
            for path in (
                "/opt/data/.env",
                "/opt/data/config.yaml",
                "/opt/data/auth.json",
                "/opt/data/state.db",
                "/opt/data/sessions/abc",
                "/opt/data/plugins/x",
                "/opt/data/memories/x",
                "/opt/data/../etc/passwd",
                "/opt/hermes/cli.py",
                "/proc/1/environ",
                ".env",
                "config.yaml",
                "./sessions/abc",
                "~/.env",
            ):
                self.assertTrue(business_guard._blocks_path(path), path)

    def test_workspace_paths_allowed(self) -> None:
        for path in (
            "/uploads/report.csv",
            "/uploads/config.yaml",
            "/opt/data/SOUL.md",
            "/opt/data/report.md",
            "",
        ):
            self.assertFalse(business_guard._blocks_path(path), path)

    def test_browser_internal_and_file_urls_blocked(self) -> None:
        for url in (
            "file:///opt/data/.env",
            "http://portal:9180/api/me",
            "http://egress:8080/v1/models",
            "http://127.0.0.1:8642/v1/models",
            "http://localhost:9181/",
        ):
            self.assertTrue(business_guard._blocks_url(url), url)
        for url in ("https://example.com", "https://pypi.org/simple/", ""):
            self.assertFalse(business_guard._blocks_url(url), url)

    def test_upload_root_isolates_tenants(self) -> None:
        with mock.patch.dict(os.environ, {"HERMES_BUSINESS_UPLOAD_ROOT": "/uploads/business"}):
            self.assertFalse(business_guard._blocks_path("/uploads/business/report.csv"))
            for path in ("/uploads", "/uploads/visitor-hermes/a.csv", "/uploads/business2/a.csv"):
                self.assertTrue(business_guard._blocks_path(path), path)

    def test_search_cannot_start_at_home_or_filesystem_root(self) -> None:
        with mock.patch.dict(os.environ, {"HERMES_HOME": "/opt/data"}):
            for path in (
                "", ".", "./logs", "logs", "~", "/", "/opt", "/opt/data",
                "/opt/data/SOUL.md", "/home", "/tmp/..",
            ):
                self.assertTrue(business_guard._blocks_search(path), path)
            self.assertFalse(business_guard._blocks_search("/uploads/report.csv"))

    def test_pre_tool_call_returns_block(self) -> None:
        ctx = FakeCtx()
        business_guard.register(ctx)
        hook = ctx.hooks["pre_tool_call"]
        blocked = hook(tool_name="read_file", args={"path": "/opt/data/.env"})
        self.assertEqual(blocked["action"], "block")
        allowed = hook(tool_name="read_file", args={"path": "/uploads/report.csv"})
        self.assertIsNone(allowed)
        browser_blocked = hook(tool_name="browser_navigate", args={"url": "file:///opt/data/.env"})
        self.assertEqual(browser_blocked["action"], "block")


if __name__ == "__main__":
    unittest.main()
