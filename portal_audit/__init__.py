"""Hermes Portal audit plugin.

Every turn boundary, model request/response, auxiliary model call, tool call, and
tool result is shipped to the portal on a per-session hash chain. When the
portal cannot be reached and ``HERMES_PORTAL_FAIL_CLOSED`` is on (default), the
next turn or tool call is blocked instead of running unaudited.

Configuration (environment):
    HERMES_PORTAL_URL          e.g. http://127.0.0.1:9180
    HERMES_PORTAL_TENANT       tenant id created in the portal
    HERMES_PORTAL_TOKEN        tenant audit token (shown once)
    HERMES_PORTAL_FAIL_CLOSED  1 (default) / 0
    HERMES_PORTAL_TIMEOUT      seconds, default 3
    HERMES_PORTAL_MAX_PAYLOAD_BYTES  default 4 MiB
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from typing import Any, Mapping

from .client import AuditConfig, PortalAuditClient

logger = logging.getLogger("hermes.portal_audit")

_CLIENT: PortalAuditClient | None = None
_HEARTBEAT_STOP = threading.Event()
_HEARTBEAT_STARTED = False

PROVIDED_HOOKS = (
    "pre_llm_call",
    "post_llm_call",
    "pre_api_request",
    "post_api_request",
    "pre_auxiliary_call",
    "post_auxiliary_call",
    "pre_tool_call",
    "post_tool_call",
    "on_session_start",
    "on_session_end",
)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _config_from_env() -> AuditConfig | None:
    base_url = os.environ.get("HERMES_PORTAL_URL", "").strip()
    tenant = os.environ.get("HERMES_PORTAL_TENANT", "").strip()
    token = os.environ.get("HERMES_PORTAL_TOKEN", "").strip()
    if not (base_url and tenant and token):
        return None
    return AuditConfig(
        base_url=base_url,
        tenant=tenant,
        token=token,
        timeout_seconds=_env_float("HERMES_PORTAL_TIMEOUT", 3.0),
        fail_closed=_env_bool("HERMES_PORTAL_FAIL_CLOSED", True),
        max_payload_bytes=_env_int("HERMES_PORTAL_MAX_PAYLOAD_BYTES", 4 * 1024 * 1024),
    )


def _session_id(kwargs: Mapping[str, Any]) -> str:
    return str(kwargs.get("session_id") or kwargs.get("task_id") or "unknown")


def _emit(client: PortalAuditClient, event_type: str, payload: Mapping[str, Any], kwargs: Mapping[str, Any]) -> bool:
    try:
        return client.emit(event_type, payload, session_id=_session_id(kwargs))
    except Exception:
        logger.warning("portal audit emit failed for %s", event_type, exc_info=True)
        return False


def register(ctx, *, client: PortalAuditClient | None = None) -> None:
    """Plugin entry point. ``client`` is injectable for tests."""
    global _CLIENT
    if client is None:
        config = _config_from_env()
        if config is None:
            logger.info(
                "portal audit disabled: set HERMES_PORTAL_URL, HERMES_PORTAL_TENANT and HERMES_PORTAL_TOKEN"
            )
            return
        client = PortalAuditClient(config)
    _CLIENT = client

    global _HEARTBEAT_STARTED
    if not _HEARTBEAT_STARTED and hasattr(client, "heartbeat"):

        def _heartbeat_loop() -> None:
            while not _HEARTBEAT_STOP.is_set():
                try:
                    client.heartbeat(version="0.1.0", pid=os.getpid(), hostname=socket.gethostname())
                except Exception:
                    logger.debug("portal audit heartbeat error", exc_info=True)
                _HEARTBEAT_STOP.wait(30)

        _HEARTBEAT_STARTED = True
        threading.Thread(target=_heartbeat_loop, daemon=True, name="portal-audit-heartbeat").start()

    def pre_llm_call(**kwargs):
        payload = {
            "user_message": kwargs.get("user_message"),
            "model": kwargs.get("model"),
            "platform": kwargs.get("platform"),
            "turn_id": kwargs.get("turn_id"),
            "task_id": kwargs.get("task_id"),
            "is_first_turn": kwargs.get("is_first_turn"),
            "parent_session_id": kwargs.get("parent_session_id"),
            "sender_id": kwargs.get("sender_id"),
            "history_messages": len(kwargs.get("conversation_history") or []),
        }
        ok = _emit(client, "llm.pre", payload, kwargs)
        if not ok and client.fail_closed:
            return {
                "action": "block",
                "message": "审计通道不可用，已阻止本轮对话（Hermes Portal）。",
            }
        return None

    def post_llm_call(**kwargs):
        payload = {
            "user_message": kwargs.get("user_message"),
            "assistant_response": kwargs.get("assistant_response"),
            "model": kwargs.get("model"),
            "platform": kwargs.get("platform"),
            "turn_id": kwargs.get("turn_id"),
            "task_id": kwargs.get("task_id"),
            "review_signals": kwargs.get("review_signals"),
        }
        _emit(client, "llm.post", payload, kwargs)
        return None

    def pre_api_request(**kwargs):
        payload = {
            "user_message": kwargs.get("user_message"),
            "request": kwargs.get("request"),
            "request_messages": kwargs.get("request_messages"),
            "system_prompt": kwargs.get("system_prompt"),
            "model": kwargs.get("model"),
            "provider": kwargs.get("provider"),
            "base_url": kwargs.get("base_url"),
            "api_mode": kwargs.get("api_mode"),
            "api_request_id": kwargs.get("api_request_id"),
            "api_call_count": kwargs.get("api_call_count"),
            "retry_count": kwargs.get("retry_count"),
            "message_count": kwargs.get("message_count"),
            "tool_count": kwargs.get("tool_count"),
            "approx_input_tokens": kwargs.get("approx_input_tokens"),
            "max_tokens": kwargs.get("max_tokens"),
        }
        _emit(client, "api.pre", payload, kwargs)
        return None

    def post_api_request(**kwargs):
        payload = {
            "model": kwargs.get("model"),
            "provider": kwargs.get("provider"),
            "api_mode": kwargs.get("api_mode"),
            "api_request_id": kwargs.get("api_request_id"),
            "api_call_count": kwargs.get("api_call_count"),
            "retry_count": kwargs.get("retry_count"),
            "response": kwargs.get("response"),
            "usage": kwargs.get("usage"),
            "api_duration": kwargs.get("api_duration"),
            "finish_reason": kwargs.get("finish_reason"),
            "response_model": kwargs.get("response_model"),
            "assistant_content_chars": kwargs.get("assistant_content_chars"),
            "assistant_tool_call_count": kwargs.get("assistant_tool_call_count"),
        }
        _emit(client, "api.post", payload, kwargs)
        return None

    def pre_auxiliary_call(**kwargs):
        payload = {
            "aux_task": kwargs.get("aux_task"),
            "request": kwargs.get("request"),
            "model": kwargs.get("model"),
            "provider": kwargs.get("provider"),
            "api_request_id": kwargs.get("api_request_id"),
            "retry_count": kwargs.get("retry_count"),
            "streaming": kwargs.get("streaming"),
        }
        _emit(client, "aux.pre", payload, kwargs)
        return None

    def post_auxiliary_call(**kwargs):
        payload = {
            "aux_task": kwargs.get("aux_task"),
            "response": kwargs.get("response"),
            "usage": kwargs.get("usage"),
            "api_duration": kwargs.get("api_duration"),
            "finish_reason": kwargs.get("finish_reason"),
            "response_model": kwargs.get("response_model"),
            "error": kwargs.get("error"),
            "error_type": kwargs.get("error_type"),
        }
        _emit(client, "aux.post", payload, kwargs)
        return None

    def pre_tool_call(**kwargs):
        payload = {
            "tool": kwargs.get("tool_name"),
            "args": kwargs.get("args"),
            "task_id": kwargs.get("task_id"),
        }
        ok = _emit(client, "tool.pre", payload, kwargs)
        if not ok and client.fail_closed:
            return {
                "action": "block",
                "message": "审计通道不可用，已阻止工具执行（Hermes Portal）。",
            }
        return None

    def post_tool_call(**kwargs):
        payload = {
            "tool": kwargs.get("tool_name"),
            "args": kwargs.get("args"),
            "result": kwargs.get("result"),
            "duration_ms": kwargs.get("duration_ms"),
            "task_id": kwargs.get("task_id"),
            "error": kwargs.get("error"),
            "is_error": kwargs.get("is_error"),
        }
        _emit(client, "tool.post", payload, kwargs)
        return None

    def on_session_start(**kwargs):
        _emit(client, "session.start", {"model": kwargs.get("model"), "platform": kwargs.get("platform")}, kwargs)
        return None

    def on_session_end(**kwargs):
        _emit(
            client,
            "session.end",
            {
                "completed": kwargs.get("completed"),
                "interrupted": kwargs.get("interrupted"),
                "model": kwargs.get("model"),
                "platform": kwargs.get("platform"),
            },
            kwargs,
        )
        return None

    for name, callback in (
        ("pre_llm_call", pre_llm_call),
        ("post_llm_call", post_llm_call),
        ("pre_api_request", pre_api_request),
        ("post_api_request", post_api_request),
        ("pre_auxiliary_call", pre_auxiliary_call),
        ("post_auxiliary_call", post_auxiliary_call),
        ("pre_tool_call", pre_tool_call),
        ("post_tool_call", post_tool_call),
        ("on_session_start", on_session_start),
        ("on_session_end", on_session_end),
    ):
        ctx.register_hook(name, callback)
    logger.info("portal audit enabled for tenant %s -> %s", client.config.tenant, client.config.base_url)
