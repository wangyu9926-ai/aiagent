"""Portal configuration, read from the environment once at startup."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = PACKAGE_DIR / "data"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    db_path: Path
    admin_user: str
    admin_password: str | None
    session_ttl_seconds: int
    secure_cookies: bool
    max_body_bytes: int
    login_max_attempts: int
    login_window_seconds: int
    bind_host: str
    bind_port: int
    upload_dir: Path | None = None

    @property
    def static_dir(self) -> Path:
        return PACKAGE_DIR / "static"


def load_settings() -> Settings:
    """Load settings. The data directory is created if missing."""
    data_dir = Path(os.environ.get("PORTAL_DATA_DIR", str(DEFAULT_DATA_DIR))).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = Path(os.environ.get("PORTAL_DB", str(data_dir / "portal.db"))).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    upload_dir = Path(os.environ.get("PORTAL_UPLOAD_DIR", str(data_dir / "uploads"))).expanduser()
    return Settings(
        data_dir=data_dir,
        db_path=db_path,
        admin_user=os.environ.get("PORTAL_ADMIN_USER", "admin").strip() or "admin",
        admin_password=os.environ.get("PORTAL_ADMIN_PASSWORD") or None,
        session_ttl_seconds=_env_int("PORTAL_SESSION_TTL_SECONDS", 12 * 60 * 60),
        secure_cookies=_env_bool("PORTAL_SECURE_COOKIES", False),
        max_body_bytes=_env_int("PORTAL_MAX_BODY_BYTES", 8 * 1024 * 1024),
        login_max_attempts=_env_int("PORTAL_LOGIN_MAX_ATTEMPTS", 8),
        login_window_seconds=_env_int("PORTAL_LOGIN_WINDOW_SECONDS", 300),
        bind_host=os.environ.get("PORTAL_HOST", "127.0.0.1"),
        bind_port=_env_int("PORTAL_PORT", 9180),
        upload_dir=upload_dir,
    )


def generated_password() -> str:
    """A readable, strong bootstrap password (avoids ambiguous punctuation)."""
    return secrets.token_urlsafe(15)
