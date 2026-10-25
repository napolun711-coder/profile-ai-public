"""局域网设备授权存储。

主流程由远程设备发起连接请求，电脑端一键允许后换取独立设备令牌；同时保留一次性
六位数字授权码作为兼容入口。落盘时只保存令牌/请求凭证的 SHA-256 哈希，不保存原始
秘密；设备名、创建时间和最近使用时间用于电脑端管理列表。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.core.paths import DATA_DIR

DEVICE_COOKIE = "pai_device_token"
PAIRING_TTL_SECONDS = 300
PAIRING_REQUEST_TTL_SECONDS = 300
PAIRING_CODE_DIGITS = 6
PAIRING_MAX_ATTEMPTS = 5
PAIRING_ATTEMPT_WINDOW_SECONDS = 60
PAIRING_REQUEST_MAX_PENDING = 12
DEVICE_MAX_AGE_SECONDS = 365 * 24 * 3600
TOKEN_FILE: Path = DATA_DIR / "access_devices.json"

_LOCK = threading.RLock()
_ATTEMPTS: Dict[str, deque[float]] = {}


class AccessStoreError(RuntimeError):
    """设备授权存储不可读写。"""


class PairingRateLimitError(ValueError):
    """同一来源尝试六位码过于频繁。"""

    def __init__(self, retry_after: int) -> None:
        super().__init__("尝试次数过多，请稍后再试")
        self.retry_after = max(1, int(retry_after))


def _allow_attempt(client_ip: str, now_ts: float) -> int:
    key = str(client_ip or "unknown")
    attempts = _ATTEMPTS.setdefault(key, deque())
    cutoff = now_ts - PAIRING_ATTEMPT_WINDOW_SECONDS
    while attempts and attempts[0] <= cutoff:
        attempts.popleft()
    if len(attempts) >= PAIRING_MAX_ATTEMPTS:
        return max(1, int(PAIRING_ATTEMPT_WINDOW_SECONDS - (now_ts - attempts[0])) + 1)
    attempts.append(now_ts)
    return 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _empty_state() -> Dict[str, Any]:
    return {"version": 2, "devices": {}, "pairings": {}, "requests": {}}


def _load_state() -> Dict[str, Any]:
    try:
        raw = TOKEN_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty_state()
    except OSError as exc:
        raise AccessStoreError("无法读取设备授权文件") from exc
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise AccessStoreError("设备授权文件格式损坏") from exc
    if not isinstance(data, dict):
        raise AccessStoreError("设备授权文件格式损坏")
    devices = data.get("devices")
    pairings = data.get("pairings")
    if not isinstance(devices, dict) or not isinstance(pairings, dict):
        raise AccessStoreError("设备授权文件缺少 devices/pairings")
    requests = data.setdefault("requests", {})
    if not isinstance(requests, dict):
        raise AccessStoreError("设备授权文件中的 requests 格式损坏")
    data["version"] = max(2, int(data.get("version") or 1))
    return data


def _save_state(data: Dict[str, Any]) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(TOKEN_FILE.parent),
            prefix=".access-devices-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            tmp_name = handle.name
        os.replace(tmp_name, TOKEN_FILE)
        tmp_name = ""
        try:
            os.chmod(TOKEN_FILE, 0o600)
        except OSError:
            pass
    except OSError as exc:
        raise AccessStoreError("无法写入设备授权文件") from exc
    finally:
        if tmp_name:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass


def _prune_pairings(data: Dict[str, Any], now_ts: float) -> bool:
    pairings = data.get("pairings") or {}
    expired = [
        key
        for key, item in pairings.items()
        if not isinstance(item, dict) or float(item.get("expires_ts") or 0) <= now_ts
    ]
    for key in expired:
        pairings.pop(key, None)
    return bool(expired)


def _prune_requests(data: Dict[str, Any], now_ts: float) -> bool:
    requests = data.setdefault("requests", {})
    expired = [
        key
        for key, item in requests.items()
        if not isinstance(item, dict) or float(item.get("expires_ts") or 0) <= now_ts
    ]
    for key in expired:
        requests.pop(key, None)
    return bool(expired)


def _clean_name(value: str) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    text = " ".join(text.split()).strip()
    return (text or "未命名设备")[:40]


def _public_device(device_id: str, item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": device_id,
        "name": str(item.get("name") or "未命名设备"),
        "created_at": str(item.get("created_at") or ""),
        "last_seen_at": str(item.get("last_seen_at") or ""),
        "last_ip": str(item.get("last_ip") or ""),
    }


def _public_request(request_id: str, item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": request_id,
        "name": str(item.get("name") or "未命名设备"),
        "created_at": str(item.get("created_at") or ""),
        "expires_at": datetime.fromtimestamp(
            float(item.get("expires_ts") or 0), timezone.utc
        ).isoformat(timespec="seconds"),
        "client_ip": str(item.get("client_ip") or ""),
    }


def _default_device_name(user_agent: str) -> str:
    text = str(user_agent or "")
    if "iPad" in text:
        platform = "iPad"
    elif "iPhone" in text:
        platform = "iPhone"
    elif "Android" in text:
        platform = "Android 平板"
    elif "Windows" in text:
        platform = "Windows 设备"
    elif "Macintosh" in text or "Mac OS" in text:
        platform = "Mac 设备"
    else:
        platform = "局域网设备"
    suffix = secrets.token_hex(2).upper()[:3]
    return "{0} · {1}".format(platform, suffix)


def create_pairing_request(
    device_name: str = "",
    *,
    client_ip: str = "",
    user_agent: str = "",
    ttl_seconds: int = PAIRING_REQUEST_TTL_SECONDS,
) -> Dict[str, Any]:
    """创建待电脑确认的连接请求；请求凭证只以哈希形式落盘。"""
    now_ts = time.time()
    expires_ts = now_ts + max(60, min(int(ttl_seconds or PAIRING_REQUEST_TTL_SECONDS), 900))
    request_id = secrets.token_urlsafe(12)
    request_secret = secrets.token_urlsafe(32)
    name = _clean_name(device_name or _default_device_name(user_agent))
    with _LOCK:
        data = _load_state()
        _prune_requests(data, now_ts)
        requests = data.setdefault("requests", {})
        # 防止未授权设备无限堆请求；只保留最新的一批待确认请求。
        if len(requests) >= PAIRING_REQUEST_MAX_PENDING:
            oldest = sorted(
                requests.items(),
                key=lambda pair: float((pair[1] or {}).get("created_ts") or 0),
            )[: len(requests) - PAIRING_REQUEST_MAX_PENDING + 1]
            for key, _ in oldest:
                requests.pop(key, None)
        requests[request_id] = {
            "secret_hash": _hash(request_secret),
            "name": name,
            "created_at": _now_iso(),
            "created_ts": now_ts,
            "expires_ts": expires_ts,
            "client_ip": str(client_ip or ""),
            "user_agent": str(user_agent or "")[:240],
            "approved": False,
        }
        _save_state(data)
    return {
        "request_id": request_id,
        "request_secret": request_secret,
        "name": name,
        "expires_at": datetime.fromtimestamp(expires_ts, timezone.utc).isoformat(timespec="seconds"),
        "expires_in": int(expires_ts - now_ts),
    }


def list_pairing_requests() -> list[Dict[str, Any]]:
    """电脑端列出待确认请求。"""
    now_ts = time.time()
    with _LOCK:
        data = _load_state()
        changed = _prune_requests(data, now_ts)
        if changed:
            _save_state(data)
        items = [
            _public_request(str(request_id), item)
            for request_id, item in (data.get("requests") or {}).items()
            if isinstance(item, dict) and not item.get("approved")
        ]
    return sorted(items, key=lambda item: item.get("created_at") or "", reverse=True)


def approve_pairing_request(request_id: str) -> Dict[str, Any]:
    """电脑端允许一条请求；设备令牌要等原设备领取时才生成。"""
    wanted = str(request_id or "").strip()
    now_ts = time.time()
    with _LOCK:
        data = _load_state()
        _prune_requests(data, now_ts)
        item = (data.get("requests") or {}).get(wanted)
        if not isinstance(item, dict):
            raise ValueError("连接请求不存在或已过期")
        item["approved"] = True
        item["approved_at"] = _now_iso()
        _save_state(data)
    return _public_request(wanted, item)


def cancel_pairing_request(request_id: str) -> bool:
    """电脑端忽略一条请求。"""
    wanted = str(request_id or "").strip()
    if not wanted:
        return False
    with _LOCK:
        data = _load_state()
        _prune_requests(data, time.time())
        existing = (data.get("requests") or {}).pop(wanted, None)
        if existing is None:
            _save_state(data)
            return False
        _save_state(data)
    return True


def claim_pairing_request(
    request_id: str,
    request_secret: str,
    *,
    client_ip: str = "",
) -> Tuple[Optional[str], Optional[Dict[str, Any]], str]:
    """原设备轮询请求状态；电脑确认后一次性兑换设备令牌。

    返回 ``(token, device, status)``。未确认时 token/device 为 ``None``，
    status 为 ``pending``；确认并兑换成功时为 ``approved``。
    """
    wanted = str(request_id or "").strip()
    raw_secret = str(request_secret or "").strip()
    if not wanted or not raw_secret:
        raise ValueError("连接请求无效或已过期")
    now_ts = time.time()
    with _LOCK:
        data = _load_state()
        changed = _prune_requests(data, now_ts)
        item = (data.get("requests") or {}).get(wanted)
        if not isinstance(item, dict):
            if changed:
                _save_state(data)
            raise ValueError("连接请求已过期，请重新发起")
        if not secrets.compare_digest(str(item.get("secret_hash") or ""), _hash(raw_secret)):
            raise ValueError("连接请求凭证无效")
        request_ip = str(item.get("client_ip") or "")
        if request_ip and client_ip and request_ip != client_ip:
            raise ValueError("连接请求来源已变化，请重新发起")
        if not item.get("approved"):
            if changed:
                _save_state(data)
            return None, None, "pending"

        device_id = secrets.token_hex(8)
        token = secrets.token_urlsafe(32)
        now_iso = _now_iso()
        device = {
            "id": device_id,
            "name": _clean_name(str(item.get("name") or "")),
            "token_hash": _hash(token),
            "created_at": now_iso,
            "last_seen_at": now_iso,
            "last_seen_ts": now_ts,
            "last_ip": str(client_ip or ""),
            "user_agent": str(item.get("user_agent") or "")[:240],
        }
        data["devices"][device_id] = device
        data["requests"].pop(wanted, None)
        _save_state(data)
    return token, _public_device(device_id, device), "approved"


def create_pairing(*, created_by_ip: str = "", ttl_seconds: int = PAIRING_TTL_SECONDS) -> Dict[str, Any]:
    """生成一次性六位码；默认 5 分钟有效，重新生成会让上一枚立即失效。"""
    code = "{0:06d}".format(secrets.randbelow(10 ** PAIRING_CODE_DIGITS))
    now_ts = time.time()
    expires_ts = now_ts + max(60, min(int(ttl_seconds or PAIRING_TTL_SECONDS), 900))
    with _LOCK:
        data = _load_state()
        # 只保留当前这一枚 6 位码：重新生成会让上一枚立即失效。
        data["pairings"] = {_hash(code): {
            "created_at": _now_iso(),
            "expires_ts": expires_ts,
            "created_by_ip": str(created_by_ip or ""),
        }}
        _save_state(data)
    return {
        "code": code,
        "expires_at": datetime.fromtimestamp(expires_ts, timezone.utc).isoformat(timespec="seconds"),
        "expires_in": int(expires_ts - now_ts),
    }


def pair_device(
    code: str,
    device_name: str,
    *,
    client_ip: str = "",
    user_agent: str = "",
) -> Tuple[str, Dict[str, Any]]:
    """消费配对码并创建设备，返回 (原始令牌, 可公开设备信息)。"""
    cleaned_code = str(code or "").strip()
    if len(cleaned_code) != PAIRING_CODE_DIGITS or not cleaned_code.isdigit():
        raise ValueError("授权码无效、已过期或已被替换")
    now_ts = time.time()
    with _LOCK:
        retry_after = _allow_attempt(client_ip, now_ts)
        if retry_after:
            raise PairingRateLimitError(retry_after)
        code_hash = _hash(cleaned_code)
        data = _load_state()
        changed = _prune_pairings(data, now_ts)
        item = (data.get("pairings") or {}).get(code_hash)
        if not isinstance(item, dict):
            if changed:
                _save_state(data)
            raise ValueError("授权码无效、已过期或已被替换")
        data["pairings"].pop(code_hash, None)
        _ATTEMPTS.pop(str(client_ip or "unknown"), None)

        device_id = secrets.token_hex(8)
        token = secrets.token_urlsafe(32)
        now_iso = _now_iso()
        device = {
            "id": device_id,
            "name": _clean_name(device_name),
            "token_hash": _hash(token),
            "created_at": now_iso,
            "last_seen_at": now_iso,
            "last_seen_ts": now_ts,
            "last_ip": str(client_ip or ""),
            "user_agent": str(user_agent or "")[:240],
        }
        data["devices"][device_id] = device
        _save_state(data)
    return token, _public_device(device_id, device)


def authenticate_device(token: str, *, client_ip: str = "") -> Optional[Dict[str, Any]]:
    """校验设备令牌；有效时按分钟节流更新 last_seen。"""
    raw = str(token or "").strip()
    if not raw:
        return None
    wanted = _hash(raw)
    now_ts = time.time()
    with _LOCK:
        data = _load_state()
        for device_id, item in (data.get("devices") or {}).items():
            if not isinstance(item, dict):
                continue
            if secrets.compare_digest(str(item.get("token_hash") or ""), wanted):
                last_ts = float(item.get("last_seen_ts") or 0)
                if now_ts - last_ts >= 60:
                    item["last_seen_ts"] = now_ts
                    item["last_seen_at"] = _now_iso()
                    item["last_ip"] = str(client_ip or "")
                    _save_state(data)
                return _public_device(str(device_id), item)
    return None


def list_devices() -> list[Dict[str, Any]]:
    with _LOCK:
        data = _load_state()
        devices = [
            _public_device(str(device_id), item)
            for device_id, item in (data.get("devices") or {}).items()
            if isinstance(item, dict)
        ]
    return sorted(devices, key=lambda item: item.get("created_at") or "", reverse=True)


def delete_device(device_id: str) -> bool:
    wanted = str(device_id or "").strip()
    if not wanted:
        return False
    with _LOCK:
        data = _load_state()
        existing = (data.get("devices") or {}).pop(wanted, None)
        if existing is None:
            return False
        _save_state(data)
    return True


def reset_for_tests() -> None:
    """测试辅助：只清空当前测试指向的授权文件。"""
    with _LOCK:
        _ATTEMPTS.clear()
        try:
            TOKEN_FILE.unlink(missing_ok=True)
        except OSError:
            pass


__all__ = [
    "AccessStoreError",
    "DEVICE_COOKIE",
    "DEVICE_MAX_AGE_SECONDS",
    "PAIRING_TTL_SECONDS",
    "PAIRING_REQUEST_TTL_SECONDS",
    "PAIRING_CODE_DIGITS",
    "PairingRateLimitError",
    "TOKEN_FILE",
    "approve_pairing_request",
    "authenticate_device",
    "cancel_pairing_request",
    "claim_pairing_request",
    "create_pairing",
    "create_pairing_request",
    "delete_device",
    "list_devices",
    "list_pairing_requests",
    "pair_device",
    "reset_for_tests",
]
