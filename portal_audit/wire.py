"""Wire format shared with the portal.

This is intentionally a copy of ``portal.events`` so the plugin can be installed
on its own into ``$HERMES_HOME/plugins`` without importing the portal package.
The portal test suite asserts both implementations produce the same hash.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

GENESIS_HASH = ""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def event_hash(
    *,
    tenant: str,
    session_id: str,
    seq: int,
    ts: float,
    event_type: str,
    payload: Mapping[str, Any],
    prev_hash: str,
) -> str:
    body = {
        "tenant": tenant,
        "session_id": session_id,
        "seq": int(seq),
        "ts": float(ts),
        "type": event_type,
        "payload": dict(payload),
        "prev_hash": prev_hash,
    }
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
