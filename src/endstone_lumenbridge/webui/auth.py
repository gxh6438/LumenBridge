"""WebUI 鉴权：HMAC-SHA256 签名 token（payload.signature），仅用标准库。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from typing import Any

TOKEN_TTL = 24 * 3600

# 管理员密码的哈希存储格式：pbkdf2_sha256$<迭代数>$<盐hex>$<摘要hex>
_HASH_SCHEME = "pbkdf2_sha256"
_PBKDF2_ITERATIONS = 240_000
# 迭代数上限：防配置被人为塞入天文数字导致每次登录耗时暴涨（DoS）
_PBKDF2_MAX_ITERATIONS = 2_000_000


def hash_password(plain: str) -> str:
    """生成带随机盐的 PBKDF2-SHA256 密码哈希串（配置存储用）。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"{_HASH_SCHEME}${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def is_hashed_password(stored: str) -> bool:
    """判断存储值是否已是哈希格式（决定登录校验与自动迁移分支）。"""
    return isinstance(stored, str) and stored.startswith(_HASH_SCHEME + "$")


def verify_password(plain: str, stored: str) -> bool:
    """校验密码：存储值为哈希串则走 PBKDF2，否则按明文恒时比较（旧配置兼容）。"""
    if is_hashed_password(stored):
        try:
            _, iters_s, salt_hex, digest_hex = stored.split("$", 3)
            iters = int(iters_s)
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(digest_hex)
        except (ValueError, AttributeError):
            return False
        if iters <= 0 or iters > _PBKDF2_MAX_ITERATIONS:
            return False
        got = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, iters)
        return hmac.compare_digest(got, expected)
    return hmac.compare_digest(str(plain).encode("utf-8"), str(stored).encode("utf-8"))


def generate_secret() -> str:
    """生成随机签名密钥"""
    return secrets.token_hex(32)


def generate_password(length: int = 8) -> str:
    """生成随机管理员密码"""
    chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(chars) for _ in range(length))


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(payload_b64: str, secret: str) -> str:
    sig = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
    return _b64encode(sig)


def issue_token(secret: str, ttl: int = TOKEN_TTL) -> str:
    """签发 token（模块级工具函数；带版本号的签发请使用 AuthProvider）"""
    now = int(time.time())
    payload = {"iat": now, "exp": now + ttl}
    payload_b64 = _b64encode(json.dumps(payload).encode())
    return f"{payload_b64}.{_sign(payload_b64, secret)}"


def verify_token(token: str, secret: str) -> bool:
    """校验 token 签名与有效期（模块级工具函数；带版本号的校验请使用 AuthProvider）"""
    if not token or "." not in token:
        return False
    payload_b64, sig = token.rsplit(".", 1)
    # compare_digest 对 str 要求 ASCII-only，统一编码为 bytes 防 TypeError 逃逸成 500
    if not hmac.compare_digest(sig.encode("utf-8"), _sign(payload_b64, secret).encode("utf-8")):
        return False
    try:
        payload: dict[str, Any] = json.loads(_b64decode(payload_b64))
        if not isinstance(payload, dict):
            return False
        exp = int(payload.get("exp", 0))
    except (ValueError, json.JSONDecodeError, TypeError, AttributeError):
        return False
    return exp > time.time()


class AuthProvider:
    """WebUI 鉴权提供者：在 HMAC token 基础上引入 token 版本号。

    payload 中携带签发时的版本号 "ver"，校验时必须与当前版本一致；
    修改密码/密钥后调用 invalidate_tokens() 使版本 +1，
    所有已签发的旧 token 立即失效（内存实现，重启后回到版本 0）。
    """

    def __init__(self, secret: str) -> None:
        self._secret = secret
        self._version = 0
        self._lock = threading.Lock()

    @property
    def secret(self) -> str:
        with self._lock:
            return self._secret

    def set_secret(self, secret: str) -> None:
        """更新签名密钥并使全部旧 token 失效"""
        with self._lock:
            self._secret = secret
            self._version += 1

    def invalidate_tokens(self) -> None:
        """版本 +1：所有已签发 token 立即失效"""
        with self._lock:
            self._version += 1

    def issue_token(self, ttl: int = TOKEN_TTL) -> str:
        """签发携带当前版本号的 token"""
        with self._lock:
            secret, version = self._secret, self._version
        now = int(time.time())
        payload = {"iat": now, "exp": now + ttl, "ver": version}
        payload_b64 = _b64encode(json.dumps(payload).encode())
        return f"{payload_b64}.{_sign(payload_b64, secret)}"

    def verify_token(self, token: str) -> bool:
        """校验签名、有效期与版本号（ver 必须等于当前版本）"""
        with self._lock:
            secret, version = self._secret, self._version
        if not token or "." not in token:
            return False
        payload_b64, sig = token.rsplit(".", 1)
        # compare_digest 对 str 要求 ASCII-only，统一编码为 bytes 比较
        if not hmac.compare_digest(sig.encode("utf-8"), _sign(payload_b64, secret).encode("utf-8")):
            return False
        try:
            payload: dict[str, Any] = json.loads(_b64decode(payload_b64))
            if not isinstance(payload, dict):
                return False
            exp = int(payload.get("exp", 0))
            ver = payload.get("ver", -1)
        except (ValueError, json.JSONDecodeError, TypeError, AttributeError):
            return False
        # 缺版本号或版本不匹配（旧 token）一律拒绝
        if not isinstance(ver, int) or isinstance(ver, bool):
            return False
        return exp > time.time() and ver == version
