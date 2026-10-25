"""P1：Tool Manager 运行时（校验、超时、重试、熔断、权限、审计）。

边界与取舍：
- **重试只给只读、幂等的调用**（`side_effect="none"`），写操作永远只尝试一次；
- **硬超时只给“外部 + 只读”工具**（当前只有 `knowledge.search`）。本机读写工具
  另开线程强等会造成“已经报超时、后台还在写”的假中止，所以只记录耗时并在审计里
  标 `over_timeout`；
- **外部工具默认关闭**：`ToolPolicy.allowed_external=False`，调用方必须显式放开；
- 参数/结果校验失败不会把可用结果判死：前置校验失败直接拒绝调用，后置校验失败
  原样返回成功结果，只在 `usage` 与审计里留痕；
- 审计只写元数据（工具名、耗时、参数键名与哈希），不写参数值与正文。
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from app.core.paths import DATA_DIR, KNOWLEDGE_DIR, OUTPUT_DIR
from app.plugins.audit import AuditLogger
from app.plugins.contracts import (
    ERROR_CIRCUIT_OPEN,
    ERROR_INTERNAL,
    ERROR_INVALID_INPUT,
    ERROR_NOT_FOUND,
    ERROR_PERMISSION,
    ERROR_TIMEOUT,
    SIDE_EFFECTS,
    ToolContext,
    ToolError,
    ToolResult,
    ToolSpec,
)
from app.plugins.registry import ToolNotFoundError, ToolRegistry
from app.plugins.schema import check as check_schema

logger = logging.getLogger(__name__)

#: 参数里视为“路径”的键名；只对 read_local 工具做白名单校验
PATH_KEYS: Tuple[str, ...] = ("path", "file_path", "dir", "directory")

#: 路径白名单的默认根目录（外加系统临时目录，测试与影子脚本都在临时目录里跑）
DEFAULT_PATH_ROOTS: Tuple[Path, ...] = (DATA_DIR, KNOWLEDGE_DIR, OUTPUT_DIR)


@dataclass(frozen=True)
class ToolPolicy:
    """一次运行时的工具使用策略；纯数据，可测试、可替换。"""

    allowed_external: bool = False
    enabled_tools: Optional[Tuple[str, ...]] = None
    allowed_side_effects: Tuple[str, ...] = ("none", "read_local")
    enforce_path_whitelist: bool = True
    failure_threshold: int = 5
    cooldown_s: float = 60.0

    def __post_init__(self) -> None:
        if not isinstance(self.allowed_external, bool):
            raise ValueError("ToolPolicy.allowed_external 必须是布尔值")
        if self.enabled_tools is not None:
            if not isinstance(self.enabled_tools, tuple) or not all(
                isinstance(item, str) and item for item in self.enabled_tools
            ):
                raise ValueError("ToolPolicy.enabled_tools 必须是非空字符串元组或 None")
        if not isinstance(self.allowed_side_effects, tuple) or not all(
            item in SIDE_EFFECTS for item in self.allowed_side_effects
        ):
            raise ValueError(
                "ToolPolicy.allowed_side_effects 必须是 %s 的子集" % ", ".join(SIDE_EFFECTS)
            )
        if not isinstance(self.enforce_path_whitelist, bool):
            raise ValueError("ToolPolicy.enforce_path_whitelist 必须是布尔值")
        if isinstance(self.failure_threshold, bool) or not isinstance(self.failure_threshold, int):
            raise ValueError("ToolPolicy.failure_threshold 必须是整数")
        if self.failure_threshold < 1:
            raise ValueError("ToolPolicy.failure_threshold 至少为 1")
        if isinstance(self.cooldown_s, bool) or not isinstance(self.cooldown_s, (int, float)):
            raise ValueError("ToolPolicy.cooldown_s 必须是数字")
        if self.cooldown_s < 0:
            raise ValueError("ToolPolicy.cooldown_s 不能为负数")

    def evaluate(
        self,
        spec: ToolSpec,
        *,
        caller: str = "script",
        declared: Sequence[str] = (),
    ) -> Tuple[bool, str]:
        """判断“当前策略 + 调用方”能否调用该工具，返回 (是否允许, 原因码)。

        原因码是稳定的英文标识（`ok` / `tool_disabled` / `side_effect_disabled` /
        `external_disabled` / `skill_not_allowed` / `undeclared`），
        UI 与测试按它分支，不解析中文文案。
        """
        if self.enabled_tools is not None and spec.name not in self.enabled_tools:
            return False, "tool_disabled"
        if spec.side_effect not in self.allowed_side_effects:
            return False, "side_effect_disabled"
        if spec.external and not self.allowed_external:
            return False, "external_disabled"

        if str(caller) == "skill":
            if not spec.allowed_for_skills:
                return False, "skill_not_allowed"
            if spec.external and not _declared(spec, declared):
                return False, "undeclared"

        return True, "ok"


class ToolManager:
    """对注册表的一次包装：所有跨边界的调用都从这里过。"""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        policy: Optional[ToolPolicy] = None,
        audit: Optional[AuditLogger] = None,
        logger_: Optional[logging.Logger] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        path_roots: Optional[Sequence[Path]] = None,
    ) -> None:
        self.registry = registry
        self.policy = policy or ToolPolicy()
        self.audit = audit or AuditLogger()
        self._logger = logger_ or logger
        self._clock = clock
        self._sleep = sleep
        self._path_roots = (
            tuple(_resolve_roots(path_roots))
            if path_roots is not None
            else _resolve_roots(DEFAULT_PATH_ROOTS + (Path(tempfile.gettempdir()),))
        )

        self._lock = threading.Lock()
        self._failures: Dict[str, int] = {}
        self._open_until: Dict[str, float] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="tool-timeout"
        )

    # ============================================================
    # 对外入口
    # ============================================================

    def call(
        self,
        name: str,
        *,
        args: Optional[Dict[str, Any]] = None,
        ctx: Optional[ToolContext] = None,
        caller: str = "script",
        declared: Sequence[str] = (),
    ) -> ToolResult:
        """按策略调用工具；任何失败都变成 `ToolResult.failure`，不向调用方抛异常。"""
        started = time.perf_counter()
        caller = str(caller or "script")
        raw_args: Any = {} if args is None else args
        audit_args = raw_args if isinstance(raw_args, dict) else {}

        try:
            tool = self.registry.get(name)
        except ToolNotFoundError:
            return self._finish(
                name=str(name or ""),
                caller=caller,
                ctx=ctx,
                started=started,
                args=audit_args,
                attempts=0,
                result=ToolResult.failure(
                    ToolError(code=ERROR_NOT_FOUND, message=f"未注册的工具：{name}")
                ),
            )

        spec: ToolSpec = tool.spec

        allowed, reason = self.policy.evaluate(spec, caller=caller, declared=declared)
        if not allowed:
            return self._finish(
                name=spec.name,
                caller=caller,
                ctx=ctx,
                started=started,
                args=audit_args,
                attempts=0,
                result=ToolResult.failure(_permission_error(reason)),
            )

        if not isinstance(raw_args, dict):
            return self._finish(
                name=spec.name,
                caller=caller,
                ctx=ctx,
                started=started,
                args={},
                attempts=0,
                result=ToolResult.failure(
                    ToolError(code=ERROR_INVALID_INPUT, message="args 必须是对象（键值对）")
                ),
            )

        if self.policy.enforce_path_whitelist and spec.side_effect == "read_local":
            path_problem = _check_paths(raw_args, self._path_roots)
            if path_problem:
                return self._finish(
                    name=spec.name,
                    caller=caller,
                    ctx=ctx,
                    started=started,
                    args=raw_args,
                    attempts=0,
                    result=ToolResult.failure(
                        ToolError(code=ERROR_INVALID_INPUT, message=path_problem)
                    ),
                )

        problems = check_schema(spec.input_schema, raw_args)
        if problems:
            return self._finish(
                name=spec.name,
                caller=caller,
                ctx=ctx,
                started=started,
                args=raw_args,
                attempts=0,
                result=ToolResult.failure(
                    ToolError(
                        code=ERROR_INVALID_INPUT,
                        message="参数校验未通过：" + "；".join(problems[:3]),
                    )
                ),
            )

        if self.circuit_state(spec.name) == "open":
            return self._finish(
                name=spec.name,
                caller=caller,
                ctx=ctx,
                started=started,
                args=raw_args,
                attempts=0,
                result=ToolResult.failure(
                    ToolError(
                        code=ERROR_CIRCUIT_OPEN,
                        message="该工具连续失败，已暂时熔断，请稍后再试",
                    )
                ),
            )

        result, attempts, over_timeout = self._invoke_with_retry(
            tool, spec, raw_args, ctx
        )

        output_problems: List[str] = []
        if result.ok and spec.output_schema and isinstance(result.data, dict):
            output_problems = check_schema(spec.output_schema, result.data)
            if output_problems:
                # 软失败：上游结构变了也要把可用结果交出去，只留痕。
                self._logger.warning(
                    "工具 %s 的结果不符合 output_schema（已原样返回）：%s",
                    spec.name,
                    output_problems[:3],
                )

        return self._finish(
            name=spec.name,
            caller=caller,
            ctx=ctx,
            started=started,
            args=raw_args,
            attempts=attempts,
            result=result,
            over_timeout=over_timeout,
            output_problems=output_problems,
        )

    def circuit_state(self, name: str) -> str:
        """返回 `closed` / `open` / `half_open`。冷却结束后的首次调用算半开。"""
        with self._lock:
            until = self._open_until.get(name)
            if until is not None:
                return "open" if self._clock() < until else "half_open"
            if self._failures.get(name, 0) >= self.policy.failure_threshold:
                return "half_open"
            return "closed"

    def reset(self) -> None:
        """清空熔断计数（测试/手动恢复用）。"""
        with self._lock:
            self._failures.clear()
            self._open_until.clear()

    def close(self) -> None:
        """释放超时线程池（测试用；正常进程退出时无需显式调用）。"""
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ============================================================
    # 调用与重试
    # ============================================================

    def _invoke_with_retry(
        self,
        tool: Any,
        spec: ToolSpec,
        args: Dict[str, Any],
        ctx: Optional[ToolContext],
    ) -> Tuple[ToolResult, int, bool]:
        attempts = 0
        over_timeout = False
        retry = spec.retry

        while True:
            attempts += 1
            result, over_timeout = self._invoke_once(tool, spec, args, ctx)

            if result.ok:
                self._record_success(spec.name)
                return result, attempts, over_timeout

            if not self._should_retry(spec, result, attempts):
                # 一次 call 只记一次失败（重试过程中的失败不重复计数）
                self._record_failure(spec, result)
                return result, attempts, over_timeout

            self._sleep(float(retry.backoff_s) * attempts)

    def _invoke_once(
        self,
        tool: Any,
        spec: ToolSpec,
        args: Dict[str, Any],
        ctx: Optional[ToolContext],
    ) -> Tuple[ToolResult, bool]:
        """真正调用一次；返回 (结果, 是否超时)。"""
        started = time.perf_counter()

        if _needs_hard_timeout(spec):
            return self._invoke_with_hard_timeout(tool, spec, args, ctx)

        try:
            result = tool.invoke(ctx, **args)
        except Exception as exc:  # noqa: BLE001 - 工具边界必须把异常变成可展示原因
            return _exception_result(exc), False

        if not isinstance(result, ToolResult):
            return (
                ToolResult.failure(
                    ToolError(
                        code=ERROR_INTERNAL,
                        message=f"工具返回了非 ToolResult 对象：{type(result).__name__}",
                    )
                ),
                False,
            )

        elapsed_ms = (time.perf_counter() - started) * 1000
        return result, elapsed_ms > float(spec.timeout_s) * 1000

    def _invoke_with_hard_timeout(
        self,
        tool: Any,
        spec: ToolSpec,
        args: Dict[str, Any],
        ctx: Optional[ToolContext],
    ) -> Tuple[ToolResult, bool]:
        timeout_s = float(spec.timeout_s)
        try:
            future = self._executor.submit(tool.invoke, ctx, **args)
        except Exception as exc:  # noqa: BLE001 - 参数无法传入也属于输入问题
            return (
                ToolResult.failure(
                    ToolError(
                        code=ERROR_INVALID_INPUT,
                        message=f"参数无法传给工具：{type(exc).__name__}：{exc}",
                    )
                ),
                False,
            )

        try:
            result = future.result(timeout=timeout_s)
        except FutureTimeoutError:
            future.cancel()
            self._logger.warning(
                "工具 %s 超过 %.1fs 未返回（底层调用可能仍在收尾）", spec.name, timeout_s
            )
            return (
                ToolResult.failure(
                    ToolError(
                        code=ERROR_TIMEOUT,
                        message=f"工具调用超过 {timeout_s:.0f} 秒，已放弃等待",
                        retryable=True,
                    ),
                    usage={"timeout_s": timeout_s},
                ),
                True,
            )
        except Exception as exc:  # noqa: BLE001 - 线程里的异常在这里统一收敛
            return _exception_result(exc), False

        if not isinstance(result, ToolResult):
            return (
                ToolResult.failure(
                    ToolError(
                        code=ERROR_INTERNAL,
                        message=f"工具返回了非 ToolResult 对象：{type(result).__name__}",
                    )
                ),
                False,
            )
        return result, False

    def _should_retry(
        self, spec: ToolSpec, result: ToolResult, attempts: int
    ) -> bool:
        """只有“只读幂等 + 明确可重试 + 次数未用完”才重试。"""
        if spec.side_effect != "none":
            return False
        if result.error is None or not result.error.retryable:
            return False
        if attempts >= spec.retry.max_attempts:
            return False
        if spec.retry.retry_on and result.error.code not in spec.retry.retry_on:
            return False
        return True

    # ============================================================
    # 熔断
    # ============================================================

    def _record_success(self, name: str) -> None:
        with self._lock:
            self._failures.pop(name, None)
            self._open_until.pop(name, None)

    def _record_failure(self, spec: ToolSpec, result: ToolResult) -> None:
        """只统计“可重试类失败”；参数错误/文件损坏不会把工具熔断。"""
        if spec.side_effect != "none":
            return
        if result.error is None or not result.error.retryable:
            return
        with self._lock:
            count = self._failures.get(spec.name, 0) + 1
            self._failures[spec.name] = count
            if count >= self.policy.failure_threshold:
                self._open_until[spec.name] = self._clock() + float(self.policy.cooldown_s)

    # ============================================================
    # 审计
    # ============================================================

    def _finish(
        self,
        *,
        name: str,
        caller: str,
        ctx: Optional[ToolContext],
        started: float,
        args: Dict[str, Any],
        attempts: int,
        result: ToolResult,
        over_timeout: bool = False,
        output_problems: Optional[List[str]] = None,
    ) -> ToolResult:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        problems = list(output_problems or [])

        result.usage["attempts"] = attempts
        result.usage["manager_elapsed_ms"] = elapsed_ms
        if over_timeout:
            result.usage["over_timeout"] = True
        if problems:
            result.usage["output_valid"] = False
            result.usage["output_problems"] = problems[:5]

        record = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "tool": name,
            "caller": caller,
            "status": "ok" if result.ok else "error",
            "error_code": result.error.code if result.error else "",
            "elapsed_ms": elapsed_ms,
            "attempts": attempts,
            "args_keys": sorted(args) if isinstance(args, dict) else [],
            "args_sha256": _digest(args) if isinstance(args, dict) else "",
            "trace_id": str(getattr(ctx, "trace_id", "") or ""),
            "over_timeout": bool(over_timeout),
            "output_invalid": bool(problems),
        }
        self.audit.write(record)
        return result


# ============================================================
# 纯函数辅助
# ============================================================

def _needs_hard_timeout(spec: ToolSpec) -> bool:
    """只有“外部 + 只读”的工具才套线程硬超时（当前 = knowledge.search）。"""
    return bool(spec.external and spec.side_effect == "none")


def _declared(spec: ToolSpec, declared: Sequence[str]) -> bool:
    """Skill 声明：按工具名或能力名任一匹配即可。"""
    if isinstance(declared, str):
        declared = (declared,)
    names = {str(item) for item in (declared or ())}
    if spec.name in names:
        return True
    return any(capability in names for capability in spec.capabilities)


def _permission_error(reason: str) -> ToolError:
    messages = {
        "tool_disabled": "该工具未在启用名单内",
        "side_effect_disabled": "该工具会改变状态，当前策略不允许调用",
        "external_disabled": "外部工具默认关闭：需要显式放开 allowed_external",
        "skill_not_allowed": "该工具不允许 Skill 直接调用",
        "undeclared": "Skill 未声明使用该工具或能力",
    }
    return ToolError(
        code=ERROR_PERMISSION,
        message=messages.get(reason, f"调用被策略拒绝（{reason}）"),
        detail=reason,
    )


def _exception_result(exc: Exception) -> ToolResult:
    return ToolResult.failure(
        ToolError(
            code=ERROR_INTERNAL,
            message=f"工具异常（{type(exc).__name__}）：{exc}",
        )
    )


def _resolve_roots(roots: Sequence[Path]) -> List[Path]:
    resolved: List[Path] = []
    for root in roots:
        try:
            resolved.append(Path(root).resolve())
        except (OSError, RuntimeError, ValueError):
            continue
    return resolved


def _check_paths(args: Dict[str, Any], roots: Sequence[Path]) -> Optional[str]:
    """检查参数里的路径是否都落在白名单内；返回第一条问题或 None。"""
    for key in PATH_KEYS:
        if key not in args or args[key] is None:
            continue
        problem = check_path(args[key], roots=roots)
        if problem:
            return f"{key}：{problem}"
    return None


def check_path(value: Any, *, roots: Optional[Sequence[Path]] = None) -> Optional[str]:
    """路径白名单校验（纯函数）；返回问题描述或 None。"""
    if not isinstance(value, (str, Path)):
        return "必须是字符串路径"
    raw = str(value).strip()
    if not raw:
        return "路径不能为空"

    try:
        target = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return f"无法解析路径：{raw}"

    allowed = _resolve_roots(roots) if roots is not None else _resolve_roots(
        DEFAULT_PATH_ROOTS + (Path(tempfile.gettempdir()),)
    )
    for root in allowed:
        try:
            target.relative_to(root)
            return None
        except ValueError:
            continue
    return "路径不在允许范围内（仅 data/、knowledge/、output/ 与临时目录）"


def _digest(args: Dict[str, Any]) -> str:
    """参数整体哈希；不记录参数值本身。"""
    try:
        text = json.dumps(args, sort_keys=True, ensure_ascii=False, default=_fallback)
    except (TypeError, ValueError):
        text = repr(sorted((str(key) for key in args)))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fallback(value: Any) -> str:
    return f"<{type(value).__name__}>"


__all__ = [
    "DEFAULT_PATH_ROOTS",
    "PATH_KEYS",
    "ToolManager",
    "ToolPolicy",
    "check_path",
]
