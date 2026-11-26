"""HTTP client and payload hardening for the Portal audit plugin (stdlib only)."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .wire import GENESIS_HASH, canonical_json, event_hash

logger = logging.getLogger("hermes.portal_audit")

# Exact keys whose values are credentials. "max_tokens"/"input_tokens" are usage
# metrics and must survive redaction.
_SECRET_KEY = re.compile(
    r"(?i)^(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"secret|client[_-]?secret|password|passwd|private[_-]?key|cookie|set-cookie)$"
)
_USAGE_KEYS = {
    "max_tokens", "input_tokens", "output_tokens", "total_tokens",
    "reasoning_tokens", "cache_read_tokens", "cache_write_tokens",
    "approx_input_tokens", "prompt_tokens", "completion_tokens",
}
_REDACTED = "***REDACTED***"


def _jsonable(value: Any, *, _depth: int = 0, _seen: set[int] | None = None) -> Any:
    """Make a hook payload safe for JSON and for hashing. Cycles become markers."""
    if _depth > 14:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else str(value)
    if isinstance(value, bytes):
        return f"<bytes {len(value)}>"
    if _seen is None:
        _seen = set()
    if isinstance(value, (dict, list, tuple, set)):
        ident = id(value)
        if ident in _seen:
            return "<cycle>"
        _seen.add(ident)
        try:
            if isinstance(value, Mapping):
                return {str(key): _jsonable(item, _depth=_depth + 1, _seen=_seen) for key, item in value.items()}
            return [_jsonable(item, _depth=_depth + 1, _seen=_seen) for item in value]
        finally:
            _seen.discard(ident)
    return str(value)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name not in _USAGE_KEYS and _SECRET_KEY.match(name):
                result[name] = _REDACTED
            else:
                result[name] = redact(item)
        return result
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def _truncate_strings(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        if len(value) <= limit:
            return value
        return value[:limit] + f"…[truncated {len(value) - limit} chars]"
    if isinstance(value, list):
        return [_truncate_strings(item, limit) for item in value]
    if isinstance(value, dict):
        return {key: _truncate_strings(item, limit) for key, item in value.items()}
    return value


def sanitize_payload(payload: Mapping[str, Any], *, max_bytes: int, max_string_chars: int = 200_000) -> dict[str, Any]:
    """JSON-safe + secret-redacted; bounded with an explicit truncation marker."""
    clean = redact(_jsonable(dict(payload)))
    text = canonical_json(clean)
    original_size = len(text.encode("utf-8"))
    if original_size <= max_bytes:
        return clean
    clipped = _truncate_strings(clean, max_string_chars)
    text = canonical_json(clipped)
    size = len(text.encode("utf-8"))
    if size <= max_bytes:
        clipped["_truncated"] = True
        clipped["_original_bytes"] = original_size
        return clipped
    preview = text[: max(1000, max_bytes - 512)]
    return {
        "_truncated": True,
        "_original_bytes": original_size,
        "_preview": preview,
    }


@dataclass(frozen=True)
class AuditConfig:
    base_url: str
    tenant: str
    token: str
    timeout_seconds: float = 3.0
    fail_closed: bool = True
    max_payload_bytes: int = 4 * 1024 * 1024
    max_string_chars: int = 200_000

    @property
    def ingest_url(self) -> str:
        return self.base_url.rstrip("/") + "/api/audit/events"


class ResyncNeeded(Exception):
    def __init__(self, expected_seq: int, expected_prev_hash: str) -> None:
        super().__init__(f"portal expects seq {expected_seq}")
        self.expected_seq = expected_seq
        self.expected_prev_hash = expected_prev_hash


class PortalRejected(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def http_post(url: str, token: str, body: bytes, timeout: float) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "hermes-portal-audit/0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read() or b"{}")
        except (ValueError, OSError):
            data = {}
        return exc.code, data


Transport = Callable[[str, str, bytes, float], tuple[int, dict[str, Any]]]


class PortalAuditClient:
    """Per-session hash chain, synchronous delivery, and fail-closed signalling."""

    def __init__(self, config: AuditConfig, *, transport: Transport | None = None) -> None:
        self.config = config
        self._transport = transport or http_post
        self._heads: dict[str, tuple[int, str]] = {}
        self._lock = threading.Lock()
        # The read-head -> send -> commit-head window must be atomic. Hooks can
        # fire from concurrent threads; without this, two events read the same
        # seq, one wins, and the other creates a false audit "gap".
        self._emit_lock = threading.Lock()
        self._down_until = 0.0

    @property
    def fail_closed(self) -> bool:
        return bool(self.config.fail_closed)

    def heartbeat(self, *, version: str = "0.1.0", pid: int | None = None, hostname: str | None = None) -> bool:
        """Tell the portal this tenant's audit plugin is alive."""
        url = self.config.base_url.rstrip("/") + "/api/tenants/heartbeat"
        body = json.dumps(
            {"version": version, "pid": pid, "hostname": hostname}, ensure_ascii=False
        ).encode("utf-8")
        try:
            status, _data = self._transport(url, self.config.token, body, self.config.timeout_seconds)
        except Exception as exc:
            logger.warning("portal audit heartbeat failed: %s", exc)
            return False
        return 200 <= int(status) < 300

    def emit(self, event_type: str, payload: Mapping[str, Any], *, session_id: str, ts: float | None = None) -> bool:
        sid = str(session_id or "unknown")[:200]
        safe_payload = sanitize_payload(
            payload, max_bytes=self.config.max_payload_bytes, max_string_chars=self.config.max_string_chars
        )
        if time.time() < self._down_until:
            return False
        with self._emit_lock:
            for _attempt in range(3):
                with self._lock:
                    seq, prev_hash = self._heads.get(sid, (0, GENESIS_HASH))
                stamp = float(ts if ts is not None else time.time())
                event = {
                    "tenant": self.config.tenant,
                    "session_id": sid,
                    "seq": seq + 1,
                    "ts": stamp,
                    "type": event_type,
                    "payload": safe_payload,
                    "prev_hash": prev_hash,
                }
                event["hash"] = event_hash(
                    tenant=event["tenant"],
                    session_id=sid,
                    seq=event["seq"],
                    ts=stamp,
                    event_type=event_type,
                    payload=safe_payload,
                    prev_hash=prev_hash,
                )
                body = canonical_json({"events": [event]}).encode("utf-8")
                try:
                    status, data = self._transport(
                        self.config.ingest_url, self.config.token, body, self.config.timeout_seconds
                    )
                except Exception as exc:  # network/DNS/TLS/timeout
                    logger.warning("portal audit send failed: %s", exc)
                    self._down_until = time.time() + 5.0
                    return False
                if status in (200, 201, 202):
                    with self._lock:
                        self._heads[sid] = (event["seq"], event["hash"])
                    self._down_until = 0.0
                    return True
                if status == 409 and data.get("expected_seq") is not None:
                    expected_seq = int(data["expected_seq"])
                    expected_prev = str(data.get("expected_prev_hash") or GENESIS_HASH)
                    with self._lock:
                        self._heads[sid] = (max(0, expected_seq - 1), expected_prev)
                    continue
                logger.warning("portal audit rejected event (%s): %s", status, str(data)[:300])
                self._down_until = time.time() + 5.0
                return False
            logger.warning("portal audit could not resynchronise session %s", sid)
            return False
