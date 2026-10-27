"""JWT（HS256）签发与校验，只用标准库。

为什么不用 PyJWT：这里只需要一种算法、三个字段，自己实现少一个依赖，
也方便把「签名到底签了什么」讲清楚。

签名的内容就是 `base64(header).base64(payload)` 这个字符串，
用 HMAC-SHA256 + 密钥算出来附在后面。校验时重算一遍并做**恒定时间比较**，
避免通过响应时间猜测签名。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Optional

DEFAULT_SECRET = "dev-insecure-secret-change-me"
DEFAULT_TTL = 86400


class TokenError(Exception):
    """token 不可用（格式错、签名错、过期）。"""


def _secret() -> bytes:
    return os.getenv("JWT_SECRET", DEFAULT_SECRET).encode("utf-8")


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(message: bytes) -> str:
    return _b64encode(hmac.new(_secret(), message, hashlib.sha256).digest())


def create_token(user_id: int, ttl_seconds: Optional[int] = None) -> str:
    now = int(time.time())
    ttl = DEFAULT_TTL if ttl_seconds is None else int(ttl_seconds)
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"sub": str(user_id), "iat": now, "exp": now + ttl}
    segments = [
        _b64encode(json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8")),
        _b64encode(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")),
    ]
    signing_input = ".".join(segments).encode("ascii")
    segments.append(_sign(signing_input))
    return ".".join(segments)


def decode_token(token: str) -> dict[str, Any]:
    parts = (token or "").split(".")
    if len(parts) != 3:
        raise TokenError("malformed_token")
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    if not hmac.compare_digest(_sign(signing_input), parts[2]):
        raise TokenError("invalid_signature")
    try:
        payload = json.loads(_b64decode(parts[1]))
    except (ValueError, TypeError) as exc:
        raise TokenError("malformed_payload") from exc
    if not isinstance(payload, dict):
        raise TokenError("malformed_payload")
    if int(payload.get("exp", 0)) < int(time.time()):
        raise TokenError("token_expired")
    return payload


def user_id_from_token(token: Optional[str]) -> Optional[int]:
    """校验失败一律返回 None，调用方只需要判空。"""
    if not token:
        return None
    try:
        return int(decode_token(token)["sub"])
    except (TokenError, KeyError, TypeError, ValueError):
        return None
