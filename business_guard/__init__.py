"""Business-workspace guard.

The business tenant intentionally has no terminal or code execution. File and
browser tools still exist, so this plugin keeps them away from the tenant's own
Hermes home secrets and from internal service URLs.

It is installed only in the business tenant home and enabled through
``plugins.enabled``. A blocked call returns the standard pre_tool_call directive.
"""

from __future__ import annotations

import logging
import os
from pathlib import PurePosixPath

logger = logging.getLogger("hermes.business_guard")

BLOCK_MESSAGE = "业务工作区禁止访问 Hermes 配置文件、密钥、审计 Token 或内部服务。"

# These live under HERMES_HOME (/opt/data). User work belongs in /uploads or
# other non-sensitive paths.
SENSITIVE_HOME_PARTS = (
    "/.env",
    "/config.yaml",
    "/auth.json",
    "/state.db",
    "/sessions",
    "/plugins",
    "/memories",
    "/logs",
    "/cache",
    "/bootstrap-cache",
)

BLOCKED_PREFIXES = ("/opt/hermes", "/proc", "/etc", "/root")

INTERNAL_HOSTS = (
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "portal",
    "egress",
    "egress-web",
    "hermes-tenant-visitor",
    "hermes-tenant-business",
)

FILE_TOOLS = {"read_file", "write_file", "patch", "search_files", "vision_analyze"}

# Recursive search must never start above the tenant's own workspace: a search
# rooted at / or /opt/data would return lines from .env/config.yaml/sessions.
BLOCKED_SEARCH_ROOTS = {
    "/", "/opt", "/home", "/root", "/etc", "/proc", "/sys", "/dev", "/var",
    "/usr", "/bin", "/sbin", "/lib", "/lib64",
}


def _normalize(value: object) -> str:
    return str(value or "").replace("\\", "/")


def _allowed_upload_root() -> str:
    return _normalize(os.environ.get("HERMES_BUSINESS_UPLOAD_ROOT", "")).rstrip("/")


def _resolve_path(path: str) -> str:
    """Resolve tool paths the way the file tools do: relative to HERMES_HOME."""
    normalized = _normalize(path).strip()
    if not normalized:
        return ""
    home = _normalize(os.environ.get("HERMES_HOME", "/opt/data")).rstrip("/") or "/opt/data"
    if normalized == "~":
        normalized = home
    elif normalized.startswith("~/"):
        normalized = home + normalized[1:]
    elif not normalized.startswith("/"):
        normalized = home + "/" + normalized
    return PurePosixPath(normalized).as_posix()


def _blocks_path(path: str) -> bool:
    normalized = _resolve_path(path)
    if not normalized:
        return False
    if ".." in PurePosixPath(normalized).parts:
        return True
    if any(normalized.startswith(prefix) for prefix in BLOCKED_PREFIXES):
        return True
    upload_root = _allowed_upload_root()
    if upload_root and (normalized == "/uploads" or normalized.startswith("/uploads/")):
        if normalized != upload_root and not normalized.startswith(upload_root + "/"):
            return True
    if normalized.startswith("/opt/data"):
        return any(marker in normalized for marker in SENSITIVE_HOME_PARTS)
    return False


def _blocks_search(path: str) -> bool:
    normalized = _resolve_path(path).rstrip("/") or "/"
    if normalized in BLOCKED_SEARCH_ROOTS:
        return True
    if normalized == "/opt/data" or normalized.startswith("/opt/data/"):
        return True
    return _blocks_path(normalized)


def _blocks_url(url: str) -> bool:
    normalized = _normalize(url).strip().lower()
    if normalized.startswith("file:"):
        return True
    for host in INTERNAL_HOSTS:
        if f"://{host}" in normalized or f"://{host}." in normalized:
            return True
    return False


def register(ctx) -> None:
    def pre_tool_call(**kwargs):
        tool = str(kwargs.get("tool_name") or "")
        args = kwargs.get("args") or {}
        if not isinstance(args, dict):
            return None
        if tool in FILE_TOOLS:
            for key in ("path", "file_path", "image_path", "directory"):
                if _blocks_path(args.get(key, "")):
                    logger.info("business-guard blocked %s path=%s", tool, args.get(key))
                    return {"action": "block", "message": BLOCK_MESSAGE}
            if tool == "search_files" and _blocks_search(args.get("path", "/opt/data")):
                logger.info("business-guard blocked search path=%s", args.get("path", "/opt/data"))
                return {"action": "block", "message": BLOCK_MESSAGE}
        if tool.startswith("browser_") and _blocks_url(args.get("url", "")):
            logger.info("business-guard blocked %s url=%s", tool, args.get("url"))
            return {"action": "block", "message": BLOCK_MESSAGE}
        return None

    ctx.register_hook("pre_tool_call", pre_tool_call)
