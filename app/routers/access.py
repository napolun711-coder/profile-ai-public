"""局域网一键确认授权与设备管理。"""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app.core import access_devices, chat_access
from app.core.config import get_settings
from app.core.paths import STATIC_DIR

router = APIRouter(tags=["access"])


class PairRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    device_name: str = Field(default="", max_length=80)


class PairRequestCreate(BaseModel):
    device_name: str = Field(default="", max_length=40)


class PairRequestClaim(BaseModel):
    request_secret: str = Field(min_length=20, max_length=160)


def _require_local(request: Request) -> None:
    if not chat_access.lan_enabled():
        raise HTTPException(status_code=409, detail="当前不是局域网模式，不需要设备授权")
    if not chat_access.client_is_local(request):
        raise HTTPException(status_code=403, detail="授权码生成和设备管理只允许在电脑本机操作")


def _default_lan_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        value = str(probe.getsockname()[0] or "")
        if value and ipaddress.ip_address(value).is_private:
            return value
    except (OSError, ValueError):
        pass
    finally:
        probe.close()
    try:
        candidates = []
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            value = str(info[4][0] if len(info) > 4 else "")
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                continue
            if not address.is_private or address.is_loopback or address.is_link_local:
                continue
            # Windows 上会同时列出 VMware / Hyper-V 虚拟网卡；优先常见家庭网段，
            # 并排除通常被网关占用的 x.x.x.1。
            score = 0
            text = str(address)
            if text.startswith("192.168."):
                score += 30
            elif text.startswith("10."):
                score += 20
            else:
                score += 10
            if not text.endswith(".1"):
                score += 10
            candidates.append((score, text))
        if candidates:
            return sorted(candidates, reverse=True)[0][1]
    except (OSError, ValueError):
        pass
    return "127.0.0.1"


def _pair_base_url(request: Request) -> str:
    explicit = str(os.environ.get("PROFILE_AI_PAIR_BASE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    host = _default_lan_ip()
    # 用户可见的聊天与配对地址统一走应用主端口。即使本机同时开着 8443
    # 的 HTTPS 语音兼容入口，也不能让授权地址自动漂移到另一个端口。
    app_port = int(get_settings().app_port or 8100)
    return "http://{0}:{1}".format(host, app_port)


def _set_device_cookie(response: JSONResponse, token: str, request: Request) -> JSONResponse:
    forwarded = str(request.headers.get("x-forwarded-proto") or "").lower()
    secure = request.url.scheme == "https" or forwarded == "https"
    response.set_cookie(
        access_devices.DEVICE_COOKIE,
        token,
        max_age=access_devices.DEVICE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )
    return response


@router.get("/pair")
def pair_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "pair.html", headers={"Cache-Control": "no-cache"})


@router.get("/access-required")
def access_required_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "access_required.html", headers={"Cache-Control": "no-cache"})


@router.get("/api/app/access/status")
def access_status(request: Request) -> Dict[str, Any]:
    return chat_access.access_state(request)


@router.post("/api/app/access/pairing")
def create_pairing(request: Request) -> Dict[str, Any]:
    _require_local(request)
    try:
        item = access_devices.create_pairing(
            created_by_ip=chat_access.client_host(request)
        )
    except access_devices.AccessStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    url = _pair_base_url(request) + "/pair"
    return {
        "code": item["code"],
        "url": url,
        "expires_at": item["expires_at"],
        "expires_in": item["expires_in"],
    }


@router.get("/api/app/access/connect-info")
def connect_info(request: Request) -> Dict[str, Any]:
    """电脑设置页显示平板可直接打开的聊天地址。"""
    _require_local(request)
    return {"url": _pair_base_url(request) + "/web"}


@router.post("/api/app/access/requests")
def create_access_request(payload: PairRequestCreate, request: Request) -> Dict[str, Any]:
    """平板/手机打开聊天页后自动发起，无需输入授权码。"""
    if not chat_access.lan_enabled():
        raise HTTPException(status_code=409, detail="当前不是局域网模式，不需要设备授权")
    try:
        item = access_devices.create_pairing_request(
            payload.device_name,
            client_ip=chat_access.client_host(request),
            user_agent=str(request.headers.get("user-agent") or ""),
        )
    except access_devices.AccessStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return item


@router.post("/api/app/access/requests/{request_id}/claim")
def claim_access_request(
    request_id: str,
    payload: PairRequestClaim,
    request: Request,
) -> JSONResponse:
    """原设备轮询状态；电脑端确认后，这里一次性兑换设备 Cookie。"""
    if not chat_access.lan_enabled():
        raise HTTPException(status_code=409, detail="当前不是局域网模式，不需要设备授权")
    try:
        token, device, status = access_devices.claim_pairing_request(
            request_id,
            payload.request_secret,
            client_ip=chat_access.client_host(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    except access_devices.AccessStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if status == "pending":
        return JSONResponse({"success": True, "status": "pending"})
    response = JSONResponse({
        "success": True,
        "status": "approved",
        "device": device,
        "redirect": "/web",
    })
    return _set_device_cookie(response, str(token or ""), request)


@router.get("/api/app/access/requests")
def pending_access_requests(request: Request) -> Dict[str, Any]:
    _require_local(request)
    try:
        items = access_devices.list_pairing_requests()
    except access_devices.AccessStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"requests": items}


@router.post("/api/app/access/requests/{request_id}/approve")
def approve_access_request(request_id: str, request: Request) -> Dict[str, Any]:
    _require_local(request)
    try:
        item = access_devices.approve_pairing_request(request_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except access_devices.AccessStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"success": True, "request": item}


@router.delete("/api/app/access/requests/{request_id}")
def cancel_access_request(request_id: str, request: Request) -> Dict[str, Any]:
    _require_local(request)
    try:
        deleted = access_devices.cancel_pairing_request(request_id)
    except access_devices.AccessStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="连接请求不存在或已过期")
    return {"success": True, "request_id": request_id}


@router.post("/api/app/access/pair")
def pair_device(payload: PairRequest, request: Request) -> JSONResponse:
    try:
        token, device = access_devices.pair_device(
            payload.code,
            payload.device_name,
            client_ip=chat_access.client_host(request),
            user_agent=str(request.headers.get("user-agent") or ""),
        )
    except access_devices.PairingRateLimitError as exc:
        raise HTTPException(
            status_code=429,
            detail={"message": str(exc), "retry_after": exc.retry_after},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    except access_devices.AccessStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    response = JSONResponse({"success": True, "device": device, "redirect": "/web"})
    return _set_device_cookie(response, token, request)


@router.get("/api/app/access/devices")
def devices(request: Request) -> Dict[str, Any]:
    _require_local(request)
    try:
        items = access_devices.list_devices()
    except access_devices.AccessStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"devices": items}


@router.delete("/api/app/access/devices/{device_id}")
def remove_device(device_id: str, request: Request) -> Dict[str, Any]:
    _require_local(request)
    try:
        deleted = access_devices.delete_device(device_id)
    except access_devices.AccessStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="设备不存在或已删除")
    return {"success": True, "device_id": device_id}
