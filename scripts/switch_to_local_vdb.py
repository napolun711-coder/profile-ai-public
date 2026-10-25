"""切换到本地文件向量库（Phase 4.3a）。

做三件事：
  1) 在 config/user-settings.json 中把向量库设为“本地文件”（NanoVectorDBStorage）；
  2) 调用 LightRAG 自带的离线重建工具 lightrag-rebuild-vdb，从已有的图谱与文本块
     重算向量（只跑本地嵌入模型，不调用云端大模型，因此不消耗 API 额度）；
  3) 通过 scripts/start.py 重启服务并等待就绪，最后用一次真实问答做验收。

前提：Ollama 在运行且已拉取嵌入模型。原文与旧的 Qdrant 数据都不会被删除，
随时可在设置页把“向量库”改回 Qdrant 再重启即可切回。

用法（推荐静默）：双击项目根目录的 switch_vdb_silent.vbs
                  （全程无窗口，做完弹一个结果框，日志 logs\\switch_vdb.log）
用法（前台查看）：双击 switch_vdb.bat

参数：
  --silent   静默模式：日志写 logs\\switch_vdb.log，结束时弹结果框（成功/失败都给回执）
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
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
    """Windows 下不创建控制台窗口（netstat/taskkill/重建工具不再闪出黑窗）。"""
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
    # 通过 netstat 找监听端口的进程号（不依赖第三方库）
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
            timeout=20, creationflags=no_window_flags()
        ).stdout
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
    try:
        subprocess.run(["taskkill", "/PID", str(target), "/T", "/F"], capture_output=True, text=True,
                       timeout=30, creationflags=no_window_flags())
    except (OSError, subprocess.SubprocessError) as exc:
        return "停止 LightRAG 失败：" + exc.__class__.__name__
    try:
        pid_file.unlink()
    except OSError:
        pass
    for _ in range(20):
        if not port_open("127.0.0.1", port):
            break
        time.sleep(1)
    return "已停止 LightRAG（进程 " + str(target) + "）"

def run_rebuild(lightrag_home: Path, working_dir_value: str) -> int:
    # 用环境变量驱动 LightRAG 离线重建工具（交互式，这里用 stdin 自动应答）
    tool = lightrag_home / ".venv" / "Scripts" / "lightrag-rebuild-vdb.exe"
    if not tool.is_file():
        log("  [失败] 未找到离线重建工具：" + str(tool))
        return 3
    env = dict(os.environ)
    env["LIGHTRAG_VECTOR_STORAGE"] = "NanoVectorDBStorage"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if working_dir_value:
        env["WORKING_DIR"] = working_dir_value
    LOGS.mkdir(parents=True, exist_ok=True)
    console_log = LOGS / "switch_local_vdb.txt"
    answers = "yes" + chr(10) + "4" + chr(10) + "yes" + chr(10) + "0" + chr(10)
    log("  正在重算向量（只使用本地嵌入模型，不消耗云端额度）...")
    with open(console_log, "w", encoding="utf-8", errors="replace") as handle:
        result = subprocess.run(
            [str(tool)],
            cwd=str(lightrag_home),
            env=env,
            input=answers,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=handle,
            stderr=subprocess.STDOUT,
            timeout=1800,
            creationflags=no_window_flags(),
        )
    log("  重建工具退出码：" + str(result.returncode))
    log("  完整日志：" + str(console_log))
    return result.returncode


def show_tail(path: Path, count: int = 12) -> None:
    if not path.is_file():
        return
    try:
        lines = [row for row in path.read_text(encoding="utf-8", errors="replace").splitlines() if row.strip()]
    except OSError:
        return
    if not lines:
        return
    log("  ---- " + path.name + " 末尾 ----")
    for row in lines[-count:]:
        log("    " + row)


def main() -> int:
    global SESSION
    SESSION = silent_utils.SilentSession("switch_vdb", "本地知识库助手 - 切换本地向量库")
    SESSION.start()
    log("  ============================================================")
    log("   切换到本地文件向量库（去掉对 Docker / Qdrant 的依赖）")
    log("  ============================================================")

    conf = user_settings.load()
    lightrag_home = user_settings.lightrag_home(conf)
    rag_env = user_settings.effective_lightrag_env(conf)
    working_dir_value = str(rag_env.get("WORKING_DIR") or "")
    if working_dir_value in ("./rag_storage", "rag_storage"):
        working_dir_value = ""
    lr_port = 9621
    port_text = str(rag_env.get("PORT") or "")
    if port_text.isdigit():
        lr_port = int(port_text)
    log("  LightRAG 目录：" + str(lightrag_home))
    log("  数据目录：     " + (working_dir_value or "LightRAG 自身设置（rag_storage）"))

    step("1/5 检查本地嵌入模型（Ollama）")
    ollama_url = str(rag_env.get("EMBEDDING_BINDING_HOST") or "http://localhost:11434")
    host_part = ollama_url.split("//")[-1].split("/")[0]
    ollama_host, _, ollama_port_text = host_part.partition(":")
    ollama_port = int(ollama_port_text) if ollama_port_text.isdigit() else 11434
    if not port_open(ollama_host or "127.0.0.1", ollama_port):
        log("  [失败] 无法连接 Ollama：" + ollama_url + "，请先启动 Ollama 再重试")
        return SESSION.finish(3, "", "无法连接 Ollama（" + ollama_url + "）。请先启动 Ollama 再重试；本脚本未做任何修改。")
    log("  Ollama 已就绪：" + ollama_url)

    step("2/5 停止 LightRAG（重建工具要求先停止服务）")
    log("  " + stop_lightrag(lr_port))

    step("3/5 重算向量（来源：已有图谱 + 文本块，不调用云端大模型）")
    if run_rebuild(lightrag_home, working_dir_value) != 0:
        log("  [失败] 重建未正常结束，日志末尾如下：")
        show_tail(LOGS / "switch_local_vdb.txt", 15)
        log("  未修改任何配置，仍是原来的 Qdrant 模式；修正后重新运行本脚本即可。")
        return SESSION.finish(
            3,
            "",
            "向量重建未正常结束；未修改任何配置，仍是原来的 Qdrant 模式。"
            "完整输出见 logs\\switch_local_vdb.txt。",
        )

    step("4/5 把设置页的“向量库”设为本地文件（重建成功后才写入）")
    conf["vector_store"]["type"] = "local"
    user_settings.save(conf)
    log("  已保存配置：" + str(user_settings.SETTINGS_FILE))
    log("  将注入：" + str(user_settings.mask_env(user_settings.to_env(user_settings.load(), "lightrag"))))

    step("5/5 重启服务并自动验收")
    start_code = SESSION.run_child(
        [sys.executable, str(SCRIPTS / "start.py"), "--no-pause", "--skip-browser"],
        cwd=str(ROOT),
    ).returncode
    if start_code != 0:
        log("  [失败] 服务启动未通过，请查看上面的提示。")
        return SESSION.finish(3, "", "配置已切到本地文件向量库，但服务启动未通过；修正后重启服务即可（设置页可改回 Qdrant）。")

    try:
        import requests
    except ImportError:
        log("  未安装 requests，跳过自动验收。")
        return SESSION.finish(0, "已切换到本地文件向量库（未安装 requests，跳过自动问答验收）。")
    try:
        health = requests.get("http://127.0.0.1:8100/health", timeout=30).json()
        log("  总体状态：" + str(health.get("status")))
        log("  知识库服务：" + ("正常" if health.get("lightrag", {}).get("ok") else "未连接"))
        reply = requests.post(
            "http://127.0.0.1:8100/chat",
            json={"message": "项目资料主要集中在哪些方面？"},
            timeout=300,
        )
        payload = reply.json() if reply.status_code == 200 else {}
        text = str(payload.get("response") or "").strip()
        if text:
            log("  [验收通过] 问答返回 " + str(len(text)) + " 字，本地向量库检索正常。")
        else:
            log("  [注意] 问答未返回内容，请到页面确认，或查看 logs/lightrag_console.txt。")
    except Exception as exc:  # noqa: BLE001
        log("  [注意] 自动验收失败：" + exc.__class__.__name__)

    log("")
    log("  完成：知识库已使用本地文件向量库，不再需要 Docker / Qdrant。")
    log("  如需切回：设置页把“向量库”改为 Qdrant，保存后重启服务。")
    return SESSION.finish(
        0,
        "已切换到本地文件向量库：不再需要 Docker / Qdrant。" + chr(10) + "如需切回：设置页把“向量库”改回 Qdrant，保存后重启服务。",
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
