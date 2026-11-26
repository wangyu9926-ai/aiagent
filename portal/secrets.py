"""Encryption for per-tenant runtime API keys.

The key lives beside the SQLite database (``runtime.key``). Losing it makes the
stored runtime keys unrecoverable; back it up with the database.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    def __init__(self, key_path: str | Path) -> None:
        self.key_path = Path(key_path)
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        if self.key_path.exists():
            self._fernet = Fernet(self.key_path.read_bytes().strip())
        else:
            key = Fernet.generate_key()
            self.key_path.write_bytes(key + b"\n")
            try:
                os.chmod(self.key_path, 0o600)
            except OSError:
                pass
            self._fernet = Fernet(key)

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise ValueError("运行环境密钥无法解密，请重新保存该租户的运行环境配置") from exc
