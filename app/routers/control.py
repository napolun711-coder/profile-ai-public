"""服务控制接口（Phase 10）：网页里的「停止服务」与「关闭窗口即停服」。

为什么判断放在后端：
    浏览器区分不了「关窗口」和「刷新 / 跳到设置页」，所以前端只做两件小事——
    页面在的时候每 15 秒报一次「我在」（keepalive）；页面离开时用 sendBeacon 预约一个延迟停止。
    真正的判断在这里：预约之后只要还有任何一个页面报「我在」，预约就会被取消；
    有入库作业在跑（ingest.is_busy()）时自动把 25 秒延长到 5 分钟，避免打断索引。

手机 / 局域网（Phase 10.1 适配）：
    手机用电脑的局域网 IP 打开页面，天然不在「本机」白名单里，所以它的权限被拆成两半：
      · 能「续命」——POST /api/app/keepalive 把已预约的停止往后推（默认 180 秒宽限，
        见 CLOSE_STOP_LAN_DELAY）：你在手机上用的时候，电脑上的服务不会被关掉；
      · 不能「停止」——POST /api/app/stop 对非本机一律 403，leave 也被忽略
        （返回 lan-ignored），所以局域网里的其它设备关不掉你的服务。
    为什么手机的宽限比本机长：手机切到别的 App / 锁屏时浏览器会暂停页面的定时器，
    180 秒是给这种情况留的余量；回到前台页面会自动补一次续命，可以接着用。
    手机页面靠 GET /api/app/session 读状态（这个接口不返回令牌，局域网可读）。

安全边界（APP_HOST 可能是 0.0.0.0，局域网里的其他人不能关你的服务）：
    1. 停止接口只接受来自本机（127.0.0.1 / ::1）的请求，外加一个每次启动随机生成的令牌
       （令牌只能从本机的 GET /api/app/control 读到）；
    2. 续命 / 状态接口允许局域网调用（它们停不掉任何东西，只能把停止往后推），
       但仍校验 Origin / Referer 必须是「本机的某个地址」（含各网卡 IP，防跨站伪造）；
    3. 既不是本机、也不是本机地址的来源一律 403。

真正「动刀」的地方只有一处：scripts\\lifecycle.py（先停同级服务 —— LightRAG，以及本次由
启动器拉起的本地 Qdrant；共享的 Qdrant 容器与 Ollama 不动），写 logs\\stop.log，最后退自己。
"""

from __future__ import annotations

import logging
import os
import secrets
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request

from app.core.config import Settings, get_settings
from app.models.schemas import ServiceControl
from app.services import ingest

logger = logging.getLogger(__name__)

router = APIRouter(tags=["control"])

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"

# 本机白名单（IPv4 / IPv6 回环）
LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# 每次启动随机生成；页面从 GET /api/app/control 读走，重启后旧令牌自动失效
TOKEN = secrets.token_urlsafe(24)

MIN_DELAY = 3.0          # 即使「立即停止」也留 3 秒，让响应先回到浏览器
MAX_DELAY = 3600.0

_LOCK = threading.Lock()
_TIMER: Optional[threading.Timer] = None
_DUE_AT: Optional[float] = None
_REASON = ""
_LOCAL_CACHE: Optional[frozenset] = None   # local_addresses() 的缓存
_LOCAL_NAMES_CACHE: Optional[frozenset] = None   # local_hostnames() 的缓存


# ============================================================
# 与 scripts/lifecycle.py 对接（scripts 不是包，按需注入 sys.path）
# ============================================================

def lifecycle_module():
    """导入 scripts\\lifecycle.py；失败时返回 None（接口会显示为不可用）。"""
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import lifecycle
    except Exception:  # noqa: BLE001 - 缺文件时不应让整个应用起不来
        logger.exception("无法导入 scripts\\lifecycle.py，网页停止功能不可用")
        return None
    return lifecycle


# ============================================================
# 三重校验：本机 + 令牌 + 来源
# ============================================================

def client_host(request: Request) -> str:
    """请求来自哪个地址（uvicorn 会填好 request.client）。"""
    return (request.client.host if request.client else "") or ""


def client_is_local(request: Request) -> bool:
    """是不是从本机（127.0.0.1 / ::1）打开页面 —— 只有本机能停服。"""
    return client_host(request) in LOCAL_HOSTS


def local_addresses() -> frozenset[str]:
    """本机所有地址：回环 + 主机名解析出的网卡 IP + 默认出口 IP。

    手机页面的 Origin 就是这些地址之一（例如 http://192.168.1.23:8100），
    所以「续命 / 状态」这类停不掉任何东西的接口可以按它放行。结果是缓存的：
    网卡地址在进程存活期内基本不变，没必要每次请求都解析一遍。
    """
    global _LOCAL_CACHE
    if _LOCAL_CACHE is not None:
        return _LOCAL_CACHE
    found = {"127.0.0.1", "::1", "localhost"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            found.add(str(info[4][0]))
    except OSError:
        pass                      # 解析不出主机名不影响下面的兜底
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))   # 只用来问出默认出口地址，不会真的发包
            found.add(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass
    _LOCAL_CACHE = frozenset(found)
    return _LOCAL_CACHE


def local_hostnames() -> frozenset[str]:
    """本机的**主机名**（小写）。

    平板 / 手机除了用 `http://192.168.x.x:8100`，也可能用电脑名打开
    （`http://DESKTOP-XXX:8100`）—— 浏览器的 Origin 里就是主机名，不是 IP。
    只认本机自己的名字，不接受别的域名（避免 DNS 重绑定借 Origin 打进来）。
    """
    global _LOCAL_NAMES_CACHE
    if _LOCAL_NAMES_CACHE is not None:
        return _LOCAL_NAMES_CACHE
    names = {"localhost"}
    for candidate in (socket.gethostname(), socket.getfqdn()):
        name = str(candidate or "").strip().lower()
        if name:
            names.add(name)
    _LOCAL_NAMES_CACHE = frozenset(names)
    return _LOCAL_NAMES_CACHE


def origin_host(request: Request) -> str:
    """请求的来源主机（浏览器的同源请求必带 Origin，sendBeacon 也带）。"""
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    if not origin:
        return ""                       # 无来源（例如 curl / 脚本）交给别的手段兜住
    return urlparse(origin).hostname or ""


def origin_allowed(request: Request, settings: Settings, allow_lan: bool = False) -> bool:
    """来源主机校验。

    allow_lan=False（停止类）：只认回环与 APP_HOST 里写死的主机；
    allow_lan=True（续命 / 状态类）：再认「本机所有网卡地址」—— 手机页面用的就是它。
    """
    host = origin_host(request).lower()
    if not host:
        return True
    if host in LOCAL_HOSTS:
        return True
    # 局域网入口：IP 与本机主机名都认（平板用电脑名打开时 Origin 是主机名）
    if allow_lan and (host in local_addresses() or host in local_hostnames()):
        return True
    if settings.app_host and settings.app_host not in ("0.0.0.0", "::", ""):
        return host == str(settings.app_host).lower()
    return False


def guard(request: Request, token: str = "") -> Settings:
    """本机 + 令牌 + 来源三重校验（停止类接口）；任一不过抛 403。"""
    settings = get_settings()
    if not client_is_local(request):
        raise HTTPException(status_code=403, detail="停止服务只允许在本机（127.0.0.1）操作")
    if not origin_allowed(request, settings):
        raise HTTPException(status_code=403, detail="请求来源不被信任，已拒绝")
    if token and not secrets.compare_digest(token, TOKEN):
        raise HTTPException(status_code=403, detail="控制令牌无效，请刷新页面后重试")
    return settings


def guard_remote(request: Request, token: str = "") -> Settings:
    """「续命 / 状态」接口的校验：手机（局域网）也能调。

    放宽的依据是这两个接口停不掉任何东西：keepalive 只会把「已经预约好的停止」
    往后推，session 只是读状态（不含令牌）。令牌给了就照验，没给（手机没有令牌）
    也能过；来源仍必须是本机的某个地址，防止别的网站借浏览器打过来。
    """
    settings = get_settings()
    if not origin_allowed(request, settings, allow_lan=True):
        raise HTTPException(status_code=403, detail="请求来源不被信任，已拒绝")
    if token and not secrets.compare_digest(token, TOKEN):
        raise HTTPException(status_code=403, detail="控制令牌无效，请刷新页面后重试")
    return settings


def guard_action(request: Request, token: str = "") -> Settings:
    """「会动数据」的动作比 guard 再严一档：**令牌不能为空**（Phase 16）。

    为什么不能直接用 guard：它把空令牌当成「没给就不验」（历史行为，stop / leave 沿用），
    对**不可恢复**的动作太松了 —— 本机上任何一个进程、一段脚本都能不带令牌调。

    受影响的接口（都是会改数据的）：
        POST /api/files/delete       删除知识库记录 / 本机留档
        POST /api/files/open-folder  在宿主机资源管理器里打开目录
        POST /api/inbox/scan         立刻扫描收件箱（会推动真实的入库流水线）

    令牌只有本机页面能从 GET /api/app/control 读到：手机 / 局域网页面拿不到，
    所以它们连这些请求都发不出来（页面上也不显示按钮）。
    """
    settings = guard(request, token)
    if not (token or "").strip():
        raise HTTPException(
            status_code=403,
            detail="缺少控制令牌：请在电脑上的页面（/files）操作",
        )
    return settings


# ============================================================
# 停止倒计时（可被后续的 keepalive 取消）
# ============================================================

def pending_seconds() -> Optional[int]:
    """还有多少秒停止；没有预约时返回 None。"""
    with _LOCK:
        if _DUE_AT is None:
            return None
        return max(0, int(_DUE_AT - time.time()))


def scheduled() -> bool:
    """是否有正在计时的停止预约。"""
    with _LOCK:
        return _TIMER is not None


def cancel_stop() -> None:
    """取消预约（页面报 keepalive 时调用）。"""
    global _TIMER, _DUE_AT, _REASON
    with _LOCK:
        if _TIMER is not None:
            _TIMER.cancel()
        _TIMER = None
        _DUE_AT = None
        _REASON = ""


def schedule_stop(delay: float, reason: str) -> float:
    """预约在 delay 秒后停止；重复调用以最后一次为准。"""
    global _TIMER, _DUE_AT, _REASON
    seconds = min(max(float(delay), MIN_DELAY), MAX_DELAY)
    with _LOCK:
        if _TIMER is not None:
            _TIMER.cancel()
        _TIMER = threading.Timer(seconds, _fire_stop)
        _TIMER.daemon = True
        _DUE_AT = time.time() + seconds
        _REASON = reason
        _TIMER.start()
    return seconds


def planned_delay(
    settings: Settings, requested: Optional[float] = None, respect_busy: bool = True
) -> float:
    """算倒计时（秒）。

    requested=None（页面离开）：用配置值（默认 25 秒）；有作业在跑就延长到 5 分钟。
    requested=3（用户点了「停止全部服务」）：按用户要的来，只保底 3 秒让响应先回到浏览器。
    """
    if requested is None:
        base = float(settings.close_stop_delay)
    else:
        try:
            base = float(requested)
        except (TypeError, ValueError):
            base = float(settings.close_stop_delay)
    if respect_busy and ingest.is_busy():
        base = max(base, float(settings.close_stop_busy_delay))
    return min(max(base, MIN_DELAY), MAX_DELAY)


def extend_stop(delay: float, reason: str = "") -> Optional[float]:
    """把「已经预约好的停止」往后推；没有预约时什么都不做（返回 None）。

    手机 / 局域网页面的 keepalive 走这里：它没有令牌、也必须制造不出停止，
    只能在别人（本机页面）已经预约了停止的情况下把它推后 —— 这样「电脑上关了窗口、
    手机还在用」时服务不会被关掉；等手机也不再续命，最后那次推后的时间就会生效。

    理由（reason）会**保留最初提出停止的那一个**，只在后面追加标记，例如
    `logs\\stop.log` 里的「原因：tab-close + lan-keepalive」——既看得出是谁先要停的，
    也看得出手机把它推后过。重复追加会被去重（手机每 15 秒续命一次，不能让理由越滚越长）。
    """
    with _LOCK:
        if _TIMER is None or _DUE_AT is None:
            return None
        keep = _REASON
    if reason and reason not in keep:
        keep = f"{keep} + {reason}" if keep else reason
    return schedule_stop(delay, keep)


def keepalive_delay(settings: Settings, remote: bool = False) -> float:
    """续命后新的倒计时（秒）。

    本机页面：25 秒（CLOSE_STOP_DELAY）；手机 / 局域网页面：180 秒
    （CLOSE_STOP_LAN_DELAY —— 手机切后台 / 锁屏时定时器会被浏览器暂停，要留余量）。
    有入库作业在跑时两者都延长到 5 分钟。
    """
    base = float(settings.close_stop_lan_delay if remote else settings.close_stop_delay)
    if ingest.is_busy():
        base = max(base, float(settings.close_stop_busy_delay))
    return min(max(base, MIN_DELAY), MAX_DELAY)


def _fire_stop() -> None:
    """到点了：停掉同级服务 → 写日志 → 结束本进程（在定时器线程里执行）。

    到点前**再查一次「忙不忙」**：预约时（25 秒前）可能还是空闲的，但用户在这段时间里
    往 `data\\inbox\\` 丢了个文件、索引刚跑起来 —— 直接停会把正在建的文档连同
    LightRAG / Qdrant 一起砍断（Phase 16 真机事故：文档停在 parsing，用户看到
    「受理了却没入库」）。忙就按 `CLOSE_STOP_BUSY_DELAY` 再排一次，理由里追加
    `busy-wait`；用户显式点「停止全部服务」（respect_busy=False）不受这里阻拦。
    """
    if ingest.is_busy():
        settings = get_settings()
        keep = _REASON or "web-leave"
        if "busy-wait" not in keep:
            keep = f"{keep} + busy-wait"
        seconds = schedule_stop(float(settings.close_stop_busy_delay), keep)
        logger.warning("到点时还有入库作业在跑：停止再延后 %s 秒（原因：%s）", int(seconds), keep)
        return
    reason = _REASON or "web-leave"
    module = lifecycle_module()
    if module is None:
        logger.error("找不到 scripts\\lifecycle.py，只能退出本进程，同级服务请用桌面快捷方式停止")
    else:
        try:
            messages, problems = module.stop_all(reason=reason, defer_app=True)
            for item in messages:
                logger.warning("停服：%s", item)
            for item in problems:
                logger.error("停服未完成：%s", item)
        except Exception:  # noqa: BLE001 - 退出流程不能被异常打断
            logger.exception("停服时出现异常，仍将退出本进程")
    logger.warning("页面已离开：本应用退出（原因：%s）", reason)
    exit_process()


def exit_process() -> None:
    """结束本进程（uvicorn 在 Windows 上没有可靠的优雅退出信号）。

    先说清楚为什么这么「暴力」：Windows 上给 uvicorn 发 CTRL_C_EVENT 会广播给同控制台的
    所有进程（包括启动器与其它 python），风险比直接退出更大；而 stop.py 的 taskkill /F
    本来就是这个强度。这里的做法等价，但只影响自己：先把日志刷盘，再 os._exit(0)。
    """
    try:
        logging.shutdown()
    except Exception:  # noqa: BLE001
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except Exception:  # noqa: BLE001
            pass
    os._exit(0)


# ============================================================
# 接口
# ============================================================

@router.get("/api/app/session")
def session_state(request: Request) -> Dict[str, Any]:
    """页面初始化读一次：现在这个页面是「本机」还是「手机 / 局域网」，各自能做什么。

    与 /api/app/control 的区别是**不返回令牌**，所以局域网（手机）也能读：
    手机拿不到令牌 → 停不了服务，但知道自己能不能续命、倒计时还剩多少。
    """
    settings = guard_remote(request)
    local = client_is_local(request)
    enabled = bool(settings.close_stops_services and lifecycle_module() is not None)
    return {
        "mode": "local" if local else "lan",
        "enabled": enabled,
        "can_stop": bool(local and enabled),
        "keepalive": bool(enabled and (local or settings.lan_keepalive)),
        "delay_seconds": settings.close_stop_delay,
        "lan_delay_seconds": settings.close_stop_lan_delay,
        "busy_delay_seconds": settings.close_stop_busy_delay,
        "busy": ingest.is_busy(),
        "pending_seconds": pending_seconds(),
        "note": "手机 / 局域网页面只能让服务继续运行（把已预约的停止往后推），不能停止服务；"
                "停止请在电脑上操作。",
    }


@router.get("/api/app/control")
def control_state(request: Request) -> Dict[str, Any]:
    """页面启动时读一次：开关、倒计时、忙不忙、令牌（只有本机能读）。"""
    settings = guard(request)
    return {
        "mode": "local",
        "enabled": bool(settings.close_stops_services and lifecycle_module() is not None),
        "token": TOKEN,
        "delay_seconds": settings.close_stop_delay,
        "busy_delay_seconds": settings.close_stop_busy_delay,
        "busy": ingest.is_busy(),
        "pending_seconds": pending_seconds(),
        "stops": ["本应用", "知识库（LightRAG）", "本次由启动器拉起的本地 Qdrant（如果有）"],
        "note": "共享的 Qdrant 容器与 Ollama 不属于本应用，停止时不会动它们；"
                "有文件正在处理时倒计时会自动延长。",
    }


@router.post("/api/app/keepalive")
def keepalive(payload: ServiceControl, request: Request) -> Dict[str, Any]:
    """页面还在。

    本机页面：取消已预约的停止（刷新 / 跳设置页 / 另一个标签都靠它续命）；
    手机 / 局域网页面：把已预约的停止往后推（CLOSE_STOP_LAN_DELAY，默认 180 秒）——
    手机没有令牌，也制造不出停止：没有预约时这里什么都不做。
    """
    settings = guard_remote(request, payload.token)
    busy = ingest.is_busy()
    local = client_is_local(request)
    mode = "local" if local else "lan"
    if not settings.close_stops_services:
        return {"status": "disabled", "mode": mode, "busy": busy, "pending_seconds": pending_seconds()}
    if local:
        cancel_stop()
        return {"status": "alive", "mode": mode, "busy": busy, "pending_seconds": None}
    if not settings.lan_keepalive:
        # 关掉了手机续命：手机页面不再影响倒计时（也不会被它一直拖住）
        return {"status": "ignored", "mode": mode, "busy": busy, "pending_seconds": pending_seconds()}
    seconds = keepalive_delay(settings, remote=True)
    extended = extend_stop(seconds, "lan-keepalive")
    if extended is not None:
        logger.info("手机 / 局域网页（%s）续命：停止已推后 %s 秒", client_host(request), int(extended))
    return {
        "status": "alive",
        "mode": mode,
        "busy": busy,
        "extended": extended is not None,
        "pending_seconds": pending_seconds(),
    }


@router.post("/api/app/leave")
def leave(payload: ServiceControl, request: Request) -> Dict[str, Any]:
    """页面离开。

    本机页面：预约停止（真正的判断在后端，见模块开头说明）；
    手机 / 局域网页面：不预约 —— 手机离开不改变电脑上的任何东西。电脑上的页面
    如果也关了，它自己的预约（含手机推后出来的那段时间）会自然到期。
    """
    busy = ingest.is_busy()
    if not client_is_local(request):
        guard_remote(request, payload.token)
        return {"status": "lan-ignored", "mode": "lan", "busy": busy, "pending_seconds": pending_seconds()}
    settings = guard(request, payload.token)
    if not settings.close_stops_services:
        return {"status": "disabled", "mode": "local", "pending_seconds": None, "busy": busy}
    seconds = schedule_stop(planned_delay(settings, None), payload.reason or "tab-close")
    logger.info("页面离开：已预约 %s 秒后停止服务（busy=%s）", seconds, busy)
    return {"status": "scheduled", "mode": "local", "pending_seconds": int(seconds), "busy": busy}


@router.post("/api/app/stop")
def stop_now(payload: ServiceControl, request: Request) -> Dict[str, Any]:
    """立即停止（顶栏「停止全部服务」按钮）：3 秒后停，够响应回到浏览器。

    只对本机（127.0.0.1 页面）开放：手机 / 局域网页面显示不出这个按钮，硬调也是 403。
    与 leave 的区别：这里 respect_busy=False —— 用户明确点了停止，就按他要的做；
    是否忙由前端在点击前提示（响应里也会回 busy，方便界面如实说明）。
    """
    settings = guard(request, payload.token)
    if not settings.close_stops_services:
        raise HTTPException(
            status_code=409,
            detail="当前配置不允许从网页停止服务（CLOSE_STOPS_SERVICES=false）",
        )
    busy = ingest.is_busy()
    seconds = schedule_stop(
        planned_delay(
            settings, payload.delay if payload.delay is not None else 3.0, respect_busy=False
        ),
        payload.reason or "web-stop",
    )
    logger.warning("网页点击停止：%s 秒后停止服务（busy=%s）", seconds, busy)
    return {"status": "stopping", "pending_seconds": int(seconds), "busy": busy}

