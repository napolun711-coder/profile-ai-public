"""Runtime endpoint selection persisted outside the repository.

Windows may reserve a TCP range (for example 9607-9706) which includes LightRAG's
default port 9621. The launcher must not assume a fixed port. This module chooses a
healthy/bindable local endpoint, persists it under the user's ProfileAI config and
keeps every launcher component looking at the same value.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple
from urllib.parse import urlsplit

from app.core.paths import RUNTIME_FILE

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "0.0.0.0", "::1", "::", ""}
DEFAULT_LIGHTRAG_PORT = 9961
FALLBACK_PORT_START = 18021
FALLBACK_PORT_END = 18120


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_runtime(path: Path | None = None) -> Dict[str, Any]:
    target = path or RUNTIME_FILE
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_runtime(values: Dict[str, Any], path: Path | None = None) -> Dict[str, Any]:
    target = path or RUNTIME_FILE
    current = load_runtime(target)
    current.update(values)
    current["updated_at"] = _now()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, target)
    return current


def split_url(url: str, default_port: int = DEFAULT_LIGHTRAG_PORT) -> Tuple[str, int]:
    text = str(url or "").strip()
    if not text:
        return "127.0.0.1", default_port
    if "://" not in text:
        text = "http://" + text
    parsed = urlsplit(text)
    try:
        port = int(parsed.port or default_port)
    except ValueError:
        port = default_port
    return parsed.hostname or "127.0.0.1", port


def port_open(host: str, port: int, timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def port_bindable(host: str, port: int) -> bool:
    probe_host = "127.0.0.1" if host in LOOPBACK_HOSTS else host
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((probe_host, int(port)))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def parse_excluded_ranges(text: str) -> Tuple[Tuple[int, int], ...]:
    ranges: List[Tuple[int, int]] = []
    for raw in str(text or "").splitlines():
        parts = raw.split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            start, end = int(parts[0]), int(parts[1])
            if 0 < start <= end <= 65535:
                ranges.append((start, end))
    return tuple(ranges)


def port_is_excluded(port: int, ranges: Sequence[Tuple[int, int]]) -> bool:
    return any(start <= int(port) <= end for start, end in ranges)


def windows_excluded_ranges() -> Tuple[Tuple[int, int], ...]:
    if os.name != "nt":
        return ()
    try:
        done = subprocess.run(
            ["netsh", "interface", "ipv4", "show", "excludedportrange", "protocol=tcp"],
            capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    for encoding in ("utf-8", "gbk", "mbcs"):
        try:
            return parse_excluded_ranges(done.stdout.decode(encoding, errors="strict"))
        except (LookupError, UnicodeDecodeError):
            continue
    return parse_excluded_ranges(done.stdout.decode("utf-8", errors="replace"))


def lightrag_health(url: str, timeout: float = 1.5) -> bool:
    import urllib.request

    health = str(url or "").rstrip("/") + "/health"
    try:
        request = urllib.request.Request(health, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= int(response.status) < 300
    except Exception:  # noqa: BLE001 - health probes are best effort
        return False


def candidate_ports(preferred: int, app_port: int, persisted: int = 0) -> List[int]:
    ports: List[int] = []
    for item in (persisted, preferred, DEFAULT_LIGHTRAG_PORT):
        port = int(item or 0)
        if 0 < port <= 65535 and port != int(app_port) and port not in ports:
            ports.append(port)
    for port in range(FALLBACK_PORT_START, FALLBACK_PORT_END + 1):
        if port != int(app_port) and port not in ports:
            ports.append(port)
    return ports


def resolve_lightrag_endpoint(configured_url: str, app_port: int, *, prefer_persisted: bool = False) -> Tuple[str, bool]:
    """Return (effective_url, changed).

    Existing healthy local LightRAG instances are reused. Reserved, occupied by an
    unhealthy service, or otherwise unbindable ports are skipped. The selected local
    endpoint is persisted for subsequent starts.
    """
    host, preferred = split_url(configured_url)
    if host not in LOOPBACK_HOSTS:
        return str(configured_url or "").rstrip("/"), False

    runtime = load_runtime()
    persisted = int(runtime.get("lightrag_port") or 0) if prefer_persisted else 0
    ranges = windows_excluded_ranges()
    for port in candidate_ports(preferred, app_port, persisted):
        url = "http://127.0.0.1:%d" % port
        if port_open("127.0.0.1", port):
            if lightrag_health(url):
                save_runtime({"lightrag_url": url, "lightrag_port": port, "app_port": int(app_port)})
                return url, url != str(configured_url or "").rstrip("/")
            continue
        if port_is_excluded(port, ranges):
            continue
        if port_bindable("127.0.0.1", port):
            save_runtime({"lightrag_url": url, "lightrag_port": port, "app_port": int(app_port)})
            return url, url != str(configured_url or "").rstrip("/")
    raise RuntimeError("没有找到可用的本地 LightRAG 端口；请关闭占用端口的程序后重试")


def persisted_lightrag_url() -> str:
    value = str(load_runtime().get("lightrag_url") or "").strip().rstrip("/")
    return value
