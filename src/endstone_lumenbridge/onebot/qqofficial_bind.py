"""QQ 官方机器人扫码绑定（q.qq.com lite 绑定接口 + 零依赖 AES-256-GCM 解密）。

流程（与 QQ 开放平台网页端 / AstrBot 扫码登录一致）：本地生成 32 字节
bind_key 创建绑定任务 → 用户 QQ 扫 ``connect.html?task_id=...`` 二维码授权 →
轮询 ``/lite/poll_bind_result``，status=2 时返回 ``bot_appid`` 与
``bot_encrypt_secret``（AES-256-GCM 密文，密钥即 bind_key）→ 解密得
AppSecret 写入适配器配置。AES-256-GCM 纯 Python 实现，避免 pycryptodome 依赖。
"""

from __future__ import annotations

import base64
import json
import secrets
import urllib.error
import urllib.request
from typing import Any

from .adapter import USER_AGENT

BIND_HOST = "q.qq.com"
_API_TIMEOUT = 10.0
_POLL_INTERVAL = 2

STATUS_NONE = 0
STATUS_PENDING = 1
STATUS_COMPLETED = 2
STATUS_EXPIRED = 3


# ---------------------------------------------------------------- AES-256（仅加密方向）
def _build_sbox() -> list[int]:
    """生成 AES S 盒（GF(2^8) 逆元 + 仿射变换，标准生成算法）。"""
    sbox = [0] * 256
    p = q = 1
    while True:
        # p *= 3; q *= 0xf3（互逆生成元遍历非零元素）
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= (q << 1) & 0xFF
        q ^= (q << 2) & 0xFF
        q ^= (q << 4) & 0xFF
        if q & 0x80:
            q ^= 0x09
        # 仿射变换
        x = q ^ (((q << 1) | (q >> 7)) & 0xFF) ^ (((q << 2) | (q >> 6)) & 0xFF)
        x ^= (((q << 3) | (q >> 5)) & 0xFF) ^ (((q << 4) | (q >> 4)) & 0xFF)
        sbox[p] = (x ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    return sbox


_SBOX = _build_sbox()
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40)


def _xtime(a: int) -> int:
    return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else (a << 1)


def _sub_word(word: int) -> int:
    return (
        (_SBOX[(word >> 24) & 0xFF] << 24)
        | (_SBOX[(word >> 16) & 0xFF] << 16)
        | (_SBOX[(word >> 8) & 0xFF] << 8)
        | _SBOX[word & 0xFF]
    )


def _key_expansion_256(key: bytes) -> list[int]:
    """AES-256 密钥扩展：返回 60 个 32 位轮密钥字。"""
    words = [int.from_bytes(key[4 * i : 4 * i + 4], "big") for i in range(8)]
    for i in range(8, 60):
        temp = words[i - 1]
        if i % 8 == 0:
            temp = _sub_word(((temp << 8) | (temp >> 24)) & 0xFFFFFFFF) ^ (_RCON[i // 8 - 1] << 24)
        elif i % 8 == 4:
            temp = _sub_word(temp)
        words.append(words[i - 8] ^ temp)
    return words


def _encrypt_block(block: bytes, round_keys: list[int]) -> bytes:
    """AES-256 加密单个 16 字节块（CTR / GHASH 只需正向加密）。"""
    s = bytearray(block)

    def add_round_key(rnd: int) -> None:
        for c in range(4):
            word = round_keys[rnd * 4 + c]
            base = 4 * c
            s[base] ^= (word >> 24) & 0xFF
            s[base + 1] ^= (word >> 16) & 0xFF
            s[base + 2] ^= (word >> 8) & 0xFF
            s[base + 3] ^= word & 0xFF

    def sub_shift() -> None:
        for i in range(16):
            s[i] = _SBOX[s[i]]
        # 行循环左移（state 按列主序存放：s[4c+r]）
        s[1], s[5], s[9], s[13] = s[5], s[9], s[13], s[1]
        s[2], s[6], s[10], s[14] = s[10], s[14], s[2], s[6]
        s[3], s[7], s[11], s[15] = s[15], s[3], s[7], s[11]

    def mix_columns() -> None:
        for c in range(4):
            i = 4 * c
            a0, a1, a2, a3 = s[i], s[i + 1], s[i + 2], s[i + 3]
            s[i] = _xtime(a0) ^ (_xtime(a1) ^ a1) ^ a2 ^ a3
            s[i + 1] = a0 ^ _xtime(a1) ^ (_xtime(a2) ^ a2) ^ a3
            s[i + 2] = a0 ^ a1 ^ _xtime(a2) ^ (_xtime(a3) ^ a3)
            s[i + 3] = (_xtime(a0) ^ a0) ^ a1 ^ a2 ^ _xtime(a3)

    add_round_key(0)
    for rnd in range(1, 14):
        sub_shift()
        mix_columns()
        add_round_key(rnd)
    sub_shift()
    add_round_key(14)
    return bytes(s)


def _gcm_gmul(x: int, y: int) -> int:
    """GF(2^128) 乘法（GCM 反射位序约定）。"""
    reduction = 0xE1000000000000000000000000000000
    z = 0
    v = x
    for i in range(127, -1, -1):
        if (y >> i) & 1:
            z ^= v
        v = (v >> 1) ^ reduction if v & 1 else v >> 1
    return z


def aes256_gcm_decrypt(key: bytes, nonce: bytes, ciphertext: bytes, tag: bytes) -> bytes:
    """AES-256-GCM 解密并校验标签；失败抛 ValueError。"""
    if len(key) != 32:
        raise ValueError("AES-256 key must be 32 bytes")
    if len(nonce) != 12:
        raise ValueError("GCM nonce must be 12 bytes")
    if len(tag) != 16:
        raise ValueError("GCM tag must be 16 bytes")
    round_keys = _key_expansion_256(key)
    j0 = nonce + b"\x00\x00\x00\x01"

    # CTR 解密（首个数据计数器块为 inc32(J0)，即计数器 2）
    plain = bytearray(len(ciphertext))
    counter_hi = j0[:12]
    counter_lo = 2
    for offset in range(0, len(ciphertext), 16):
        keystream = _encrypt_block(
            counter_hi + counter_lo.to_bytes(4, "big"), round_keys
        )
        chunk = ciphertext[offset : offset + 16]
        for i, byte in enumerate(chunk):
            plain[offset + i] = byte ^ keystream[i]
        counter_lo = (counter_lo + 1) & 0xFFFFFFFF

    # GHASH 校验（AAD 为空：长度块 = 0^64 || len(C)*8）
    h = int.from_bytes(_encrypt_block(b"\x00" * 16, round_keys), "big")
    acc = 0
    for offset in range(0, len(ciphertext), 16):
        block = ciphertext[offset : offset + 16].ljust(16, b"\x00")
        acc = _gcm_gmul(acc ^ int.from_bytes(block, "big"), h)
    length_block = b"\x00" * 8 + ((len(ciphertext) * 8) & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "big")
    acc = _gcm_gmul(acc ^ int.from_bytes(length_block, "big"), h)
    expected = bytes(
        a ^ b
        for a, b in zip(
            _encrypt_block(j0, round_keys), acc.to_bytes(16, "big")
        )
    )
    if not secrets.compare_digest(expected, tag):
        raise ValueError("GCM tag mismatch")
    return bytes(plain)


# ---------------------------------------------------------------- 绑定接口
def generate_bind_key() -> str:
    """生成 base64 编码的 AES-256 绑定密钥。"""
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def connect_url(task_id: str) -> str:
    """扫码绑定页地址（渲染为二维码供 QQ 扫描）。"""
    return f"https://{BIND_HOST}/qqbot/openclaw/connect.html?task_id={urllib.request.quote(task_id, safe='')}&_wv=2"


def _post_json(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"https://{BIND_HOST}{path}",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("QQ bind api response is not an object")
    retcode = data.get("retcode")
    if retcode is not None:
        try:
            ok = int(retcode) == 0
        except (TypeError, ValueError):
            ok = False
        if not ok:
            raise RuntimeError(
                str(data.get("msg") or data.get("message") or "QQ bind api returned error")
            )
    return data


def create_bind_task_sync() -> dict[str, str]:
    """创建扫码绑定任务；返回 task_id / bind_key / 二维码地址 / 轮询间隔。"""
    bind_key = generate_bind_key()
    data = _post_json("/lite/create_bind_task", {"key": bind_key})
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    task_id = str(payload.get("task_id") or "").strip()
    if not task_id:
        raise RuntimeError("QQ bind task response missing task_id")
    return {
        "task_id": task_id,
        "bind_key": bind_key,
        "qrcode": connect_url(task_id),
        "interval": str(_POLL_INTERVAL),
    }


def decrypt_secret(encrypted_secret: str, bind_key: str) -> str:
    """解密 poll 返回的 bot_encrypt_secret（nonce(12) + 密文 + tag(16) 的 base64）。"""
    try:
        key = base64.b64decode(bind_key)
        raw = base64.b64decode(encrypted_secret)
    except Exception as exc:
        raise ValueError("QQ bot credential decode failed") from exc
    if len(key) != 32 or len(raw) < 28:
        raise ValueError("QQ bot credential malformed")
    plain = aes256_gcm_decrypt(key, raw[:12], raw[12:-16], raw[-16:])
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("QQ bot credential decrypt failed") from exc


def map_bind_result(data: dict[str, Any], bind_key: str) -> dict[str, Any]:
    """把 poll 响应映射为 {status: pending|created|expired|error, ...}。"""
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    try:
        raw_status = int(payload.get("status", STATUS_NONE))
    except (TypeError, ValueError):
        raw_status = STATUS_NONE

    if raw_status == STATUS_COMPLETED:
        appid = str(payload.get("bot_appid") or "").strip()
        encrypted = str(payload.get("bot_encrypt_secret") or "").strip()
        if not appid or not encrypted:
            return {"status": "error", "message": "QR scan ok but credential incomplete"}
        try:
            secret = decrypt_secret(encrypted, bind_key)
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        return {"status": "created", "appid": appid, "secret": secret}
    if raw_status == STATUS_EXPIRED:
        return {"status": "expired"}
    return {"status": "pending"}


def poll_bind_task_sync(task_id: str, bind_key: str) -> dict[str, Any]:
    """轮询一次绑定结果并完成解密。"""
    data = _post_json("/lite/poll_bind_result", {"task_id": task_id})
    return map_bind_result(data, bind_key)
