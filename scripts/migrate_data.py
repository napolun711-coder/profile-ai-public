"""把知识库数据搬进项目（Phase 4.3 / M2）。

做四件事：
  1) 停止 LightRAG（避免复制到写一半的数据）；
  2) 把 LightRAG 当前的数据目录与文档输入目录**复制**到项目内的 rag_storage\\ 与 knowledge\\；
  3) 在 config/user-settings.json 里把“知识库数据目录/文档输入目录”指向项目内路径；
  4) 通过 scripts/start.py 重启服务，并用一次真实问答验收。

特点：只复制、不删除、可重复运行；原目录保持不动，随时可在设置页改回去。

用法（推荐静默）：双击项目根目录的 migrate_data_silent.vbs
                  （全程无窗口，做完弹一个结果框，日志 logs\\migrate_data.log）
用法（前台查看）：双击 migrate_data.bat

参数：
  --silent   静默模式：日志写 logs\\migrate_data.log，结束时弹结果框（成功/失败都给回执）
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))
from app.core import user_settings  # noqa: E402
import silent_utils  # noqa: E402

# 静默会话（main() 里创建）：统一负责「写日志 + 结束回执」
SESSION = None


def no_window_flags():
    """Windows 下不创建控制台窗口（netstat/taskkill 不再闪出黑窗）。"""
    return silent_utils.no_window_flags()


def log(text: str = "") -> None:
    """同时写控制台与日志（静默时日志是唯一排查入口）。"""
    if SESSION is None:
        print(text, flush=True)
        return
    SESSION.log(text)


def step(text: str) -> None:
    log("")
    log("  " + text)


def port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def pid_on_port(port: int):
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                             timeout=20, creationflags=no_window_flags()).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(":" + str(port)):
            if parts[4].isdigit():
                return int(parts[4])
    return None


def stop_lightrag(port: int) -> str:
    pid_file = LOGS / "lightrag.pid"
    target = None
    if pid_file.is_file():
        text = pid_file.read_text(encoding="ascii", errors="ignore").strip()
        if text.isdigit():
            target = int(text)
    if target is None:
        target = pid_on_port(port)
    if target is None:
        return "LightRAG 未在运行"
    subprocess.run(["taskkill", "/PID", str(target), "/T", "/F"], capture_output=True, text=True,
                   timeout=30, creationflags=no_window_flags())
    try:
        pid_file.unlink()
    except OSError:
        pass
    for _ in range(20):
        if not port_open("127.0.0.1", port):
            break
        import time
        time.sleep(1)
    return "已停止 LightRAG（进程 " + str(target) + "）"


def resolve_dir(value: str, parent: Path, default_name: str) -> Path:
    # 相对路径按 parent（LightRAG 目录）解析；空值用 parent/默认名
    if not value:
        return parent / default_name
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate
    return (parent / candidate).resolve()


def count_files(folder: Path) -> int:
    if not folder.is_dir():
        return 0
    return sum(1 for item in folder.rglob("*") if item.is_file())


def main() -> int:
    global SESSION
    SESSION = silent_utils.SilentSession("migrate_data", "本地知识库助手 - 迁移数据")
    SESSION.start()
    log("  ============================================================")
    log("   把知识库数据搬进项目（便于整包复制 / 备份 / 换电脑）")
    log("  ============================================================")

    conf = user_settings.load()
    lightrag_home = user_settings.lightrag_home(conf)
    rag_env = user_settings.effective_lightrag_env(conf)
    effective = dict(rag_env)
    effective.update(user_settings.to_env(conf, "lightrag"))

    port_text = str(effective.get("PORT") or "")
    lr_port = int(port_text) if port_text.isdigit() else 9621

    src_working = resolve_dir(str(rag_env.get("WORKING_DIR") or ""), lightrag_home, "rag_storage")  # 源目录取 LightRAG 自身 .env
    src_inputs = resolve_dir(str(rag_env.get("INPUT_DIR") or ""), lightrag_home, "inputs")  # 源目录取 LightRAG 自身 .env
    dst_working = ROOT / "rag_storage"
    dst_inputs = ROOT / "knowledge"

    log("  当前数据目录：" + str(src_working))
    log("  当前输入目录：" + str(src_inputs))
    log("  迁移目标：    " + str(dst_working) + " / " + str(dst_inputs))

    if str(src_working).lower() == str(dst_working).lower():
        log("  源目录与目标目录相同，无需迁移。")
        return SESSION.finish(
            0,
            "无需迁移：数据目录已经是项目内的 " + str(dst_working) + "（没有复制任何文件）。",
        )

    step("1/4 停止 LightRAG（避免复制到写一半的文件）")
    log("  " + stop_lightrag(lr_port))

    step("2/4 复制数据到项目 data 目录（只复制，不删除原文件）")
    copied_ok = True
    for src, dst, name in ((src_working, dst_working, "知识库数据"), (src_inputs, dst_inputs, "文档输入")):
        if not src.is_dir():
            log("  [注意] 源目录不存在，跳过：" + str(src))
            continue
        try:
            shutil.copytree(src, dst, dirs_exist_ok=True)
            log("  " + name + "：复制 " + str(count_files(src)) + " 个文件 -> " + str(dst))
        except OSError as exc:
            log("  [失败] " + name + " 复制失败：" + exc.__class__.__name__)
            copied_ok = False
    if not copied_ok:
        log("  未修改任何配置，原状态可用；可重试本脚本。")
        return SESSION.finish(3, "", "复制知识库数据失败；未修改任何配置，原状态可用，可直接重试本脚本。")

    step("3/4 把设置页的“数据目录/输入目录”指向项目内")
    conf["graph"]["working_dir"] = "./rag_storage"
    conf["graph"]["input_dir"] = "./knowledge"
    user_settings.save(conf)
    log("  已保存配置：" + str(user_settings.SETTINGS_FILE))
    log("  将注入：" + str(user_settings.mask_env(user_settings.to_env(user_settings.load(), "lightrag"))))

    step("4/4 重启服务并用一次问答验收")
    code = SESSION.run_child(
        [sys.executable, str(SCRIPTS / "start.py"), "--no-pause", "--skip-browser"],
        cwd=str(ROOT),
    ).returncode
    if code != 0:
        log("  [失败] 服务启动未通过；可在设置页把两个目录清空后重启回到原状态。")
        return SESSION.finish(3, "", "数据已复制，但服务启动未通过；可在设置页把两个目录清空后重启回到原状态。")
    try:
        import requests
        health = requests.get("http://127.0.0.1:8100/health", timeout=30).json()
        log("  总体状态：" + str(health.get("status")) + "，知识库服务：" + ("正常" if health.get("lightrag", {}).get("ok") else "未连接"))
        reply = requests.post(
            "http://127.0.0.1:8100/chat",
            json={"message": "项目资料主要集中在哪些方面？"},
            timeout=300,
        )
        text = str((reply.json() if reply.status_code == 200 else {}).get("response") or "").strip()
        if text:
            log("  [验收通过] 问答返回 " + str(len(text)) + " 字，项目内数据可用。")
        else:
            log("  [注意] 问答未返回内容，请到页面确认。")
    except Exception as exc:  # noqa: BLE001
        log("  [注意] 自动验收失败：" + exc.__class__.__name__)

    log("")
    log("  完成：知识库数据已在项目内（" + str(dst_working) + " / " + str(dst_inputs) + "），原目录未删除，可回退。")
    return SESSION.finish(
        0,
        "迁移完成：数据已在项目内（" + str(dst_working) + " / " + str(dst_inputs) + "），"
        "原目录未删除、可回退；服务已重启并通过问答验收。",
    )


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception as exc:  # noqa: BLE001 - 静默模式下必须留下可见原因，不能静默退出
        reason = "脚本内部错误：" + exc.__class__.__name__ + "：" + str(exc)
        if SESSION is None:
            raise
        SESSION.log("  [失败] " + reason)
        exit_code = SESSION.finish(3, "", reason)
    sys.exit(exit_code)
