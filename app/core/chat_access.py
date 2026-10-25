"""局域网会话/记忆访问授权。

背景：会话库默认可以跨设备查看；一旦 `APP_HOST=0.0.0.0`，同一局域网的设备都能
请求 `/api/sessions/*`、`/api/memory/*`。个人或工作敏感资料属于敏感数据，不能只靠
"局域网可信"这个假设。

边界：
- 只保护**会话 / 记忆 / 导出**接口；聊天、静态页、健康检查保持原行为；
- 本机（127.0.0.1 / ::1）永远放行；
- 局域网设备通过电脑端一键确认获得独立设备令牌（六位码接口保留兼容）；
- Cookie `pai_device_token` 为 HttpOnly；服务端只保存令牌哈希；
- 经本机 Caddy 反代时，用 X-Forwarded-For 的原始客户端地址做局域网判断。
"""

from __future__ import annotations

import ipaddress
import os
import secrets
import socket
from pathlib import Path
from typing import Any, Dict

from fastapi import HTTPException, Request

from app.core import access_devices
from app.core.config import get_settings
from app.core.paths import DATA_DIR

#: 仅本机回环地址；与 control.LOCAL_HOSTS 的语义一致，但放在 core 层避免
#: 路由包 __init__ 的循环导入（sessions/memory 路由都要依赖本模块）。
LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

#: 口令文件放在 data/（已 gitignore）；可用测试 patch。
TOKEN_FILE: Path = DATA_DIR / "chat_access_token.txt"


def _env_token() -> str:
    try:
        return str(get_settings().chat_access_token or "").strip()
    except Exception:  # noqa: BLE001 - 配置坏了不应拖垮鉴权；按无 env 口令处理
        return ""


def _read_file_token() -> str:
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_file_token(token: str) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        # Windows 上 chmod 基本无效；文件在 data/ 下，已由目录权限兜底。
        pass


def current_token() -> str:
    """当前生效的口令；env 优先，其次文件，最后自动生成。失败返回空串。"""
    env = _env_token()
    if env:
        return env
    token = _read_file_token()
    if token:
        return token
    token = secrets.token_urlsafe(24)
    try:
        _write_file_token(token)
    except OSError:
        return ""
    return token


def lan_enabled() -> bool:
    """服务是否绑定在局域网地址上（只有这时才需要口令）。"""
    host = str(getattr(get_settings(), "app_host", "") or "")
    return host in ("0.0.0.0", "::", "")


def client_is_local(request: Request) -> bool:
    return client_host(request) in local_hosts()


def local_hosts() -> frozenset[str]:
    """回环地址加上本机网卡地址（兼容通过本机 HTTPS 域名/IP 打开设置页）。"""
    values = set(LOCAL_HOSTS)
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            value = str(item[4][0] if len(item) > 4 else "")
            if value:
                values.add(value)
    except OSError:
        pass
    return frozenset(values)


def client_host(request: Request) -> str:
    """返回真实请求来源地址。

    只在本机回环反代（当前为 Caddy 127.0.0.1 -> 8100）时才信任
    X-Forwarded-For；局域网设备直连 8100 时伪造该头不会获得本机身份。
    """
    direct = (request.client.host if request.client else "") or ""
    if direct not in LOCAL_HOSTS or direct == "localhost":
        return direct
    forwarded = str(request.headers.get("x-forwarded-for") or "").split(",", 1)[0].strip()
    if not forwarded:
        return direct
    try:
        return str(ipaddress.ip_address(forwarded))
    except ValueError:
        return direct


def describe() -> Dict[str, Any]:
    """本机设置页用：返回口令与来源；非局域网模式明确说不需要。"""
    if not lan_enabled():
        return {
            "enabled": False,
            "token": "",
            "source": "",
            "message": "当前仅本机访问（APP_HOST 不是 0.0.0.0），不需要局域网口令",
        }
    token = current_token()
    source = "env" if _env_token() else ("file" if token else "")
    return {
        "enabled": bool(token),
        "token": token,
        "source": source,
        "message": "" if token else "无法生成口令：请检查 data 目录是否可写",
    }


def rotate() -> Dict[str, Any]:
    """重新生成文件口令；env 口令不能在这里改。"""
    if not lan_enabled():
        raise HTTPException(status_code=409, detail="当前仅本机访问，不需要局域网口令")
    if _env_token():
        raise HTTPException(
            status_code=409,
            detail="口令写在 .env 的 CHAT_ACCESS_TOKEN 里：请修改 .env 后重启服务",
        )
    token = secrets.token_urlsafe(24)
    try:
        _write_file_token(token)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail="无法写入口令文件（请检查 data 目录权限）",
        ) from exc
    return {
        "success": True,
        "token": token,
        "message": "已重新生成；平板/手机需要重新输入一次",
    }


def require_local_access(request: Request) -> None:
    """FastAPI 依赖：写操作仅限本机；局域网即使有聊天口令也拒绝。"""
    if not lan_enabled():
        return
    if client_is_local(request):
        return
    raise HTTPException(
        status_code=403,
        detail={
            "error": "local_only",
            "message": "记忆写操作仅限本机；局域网设备只能查看，不能确认/编辑/删除",
        },
    )


def require_chat_access(request: Request) -> None:
    """FastAPI 依赖：非本机访问会话/记忆接口时必须已通过六位码授权。"""
    if not lan_enabled():
        return
    if client_is_local(request):
        return
    carried = device_token(request)
    try:
        device = access_devices.authenticate_device(
            carried,
            client_ip=client_host(request),
        )
    except access_devices.AccessStoreError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "device_access_unavailable",
                "message": "设备授权存储不可用：{0}".format(exc),
            },
        ) from exc
    if device:
        return
    raise HTTPException(
        status_code=401,
        detail={
            "error": "device_auth_required",
            "message": "这台局域网设备尚未授权。请在电脑端设置页点击“允许”完成连接。",
            "pair_url": "/pair",
        },
    )


def device_token(request: Request) -> str:
    """设备令牌只从专用 Header 或 HttpOnly Cookie 读取，不接受 URL 参数。"""
    header = str(request.headers.get("x-device-token") or "").strip()
    if header:
        return header
    return str(request.cookies.get(access_devices.DEVICE_COOKIE) or "").strip()


def access_state(request: Request) -> Dict[str, Any]:
    """页面启动时判断本机 / 已授权设备 / 待输入六位码。"""
    local = client_is_local(request)
    if not lan_enabled():
        return {"enabled": False, "local": local, "authorized": True, "device": None}
    if local:
        return {"enabled": True, "local": True, "authorized": True, "device": None}
    try:
        device = access_devices.authenticate_device(
            device_token(request),
            client_ip=client_host(request),
        )
    except access_devices.AccessStoreError:
        device = None
    return {
        "enabled": True,
        "local": False,
        "authorized": bool(device),
        "device": device,
    }


__all__ = [
    "LOCAL_HOSTS",
    "TOKEN_FILE",
    "client_host",
    "client_is_local",
    "access_state",
    "current_token",
    "device_token",
    "require_local_access",
    "describe",
    "lan_enabled",
    "local_hosts",
    "require_chat_access",
    "rotate",
]
