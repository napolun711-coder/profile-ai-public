"""闸门测试（Phase 9 / 15）：把「未通过质检的文件绝不会进知识库」变成可执行的代码约束。

三类用例：
1. **不变量用例**：直接调用 `ingest._stage_ingest()`（绕过上层流程），
   验证质量门结论不是 pass 时会被拦下、且不会调用 `lightrag.upload`；
2. **结构用例**：扫描全仓库源码，确认只有 `app/services/ingest.py` 会调用
   `lightrag` 的 `upload(...)`，防止将来有人从别处悄悄把内容送进知识库；
3. **结构用例（Phase 15）**：确认门的入库建议 `app/services/advice.py` 保持**纯函数**
   （不联网、不读磁盘、不起进程），并且确实还被确认门用着 —— 否则"先看清再入库"就成了空话。
"""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services import ingest
from tests import fixtures

BASE_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BASE_DIR / "app"
INGEST_MODULE = APP_DIR / "services" / "ingest.py"


class GateInvariantTests(unittest.TestCase):
    """不变量：未经质检（或质检非 pass）的内容一律不得入库。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.parsed_dir = self.tmp / "parsed"
        self.knowledge_dir = self.tmp / "knowledge"
        self.upload_mock = mock.Mock(return_value=fixtures.FakeResponse())

        self._patchers = [
            mock.patch.object(ingest, "PARSED_DIR", self.parsed_dir),
            mock.patch.object(ingest, "KNOWLEDGE_DIR", self.knowledge_dir),
            mock.patch.object(ingest.lightrag, "upload", self.upload_mock),
            *fixtures.patch_settings(),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _blocked_job(self, name: str = "重复.md") -> str:
        """造一个质检结论为 blocked 的作业（内容重复行超阈值）。"""
        path = fixtures.make_text(self.tmp / name, "\n".join(["同样的一行内容"] * 60))
        data = ingest.create_job(filename=name, content=path.read_bytes())
        file_id = data["file_id"]
        ingest._write_content(file_id, path.read_text(encoding="utf-8"))
        ingest._patch_section(file_id, "quality", level="blocked", score=10, reasons=["重复行过多"])
        return file_id

    def test_direct_ingest_is_blocked_when_quality_not_pass(self) -> None:
        file_id = self._blocked_job()

        outcome = ingest._stage_ingest(file_id, fixtures.settings_with(), forced=False)

        self.assertEqual(outcome["state"], ingest.STATE_FAILED)
        self.assertIn("未经质检", outcome["detail"])
        self.upload_mock.assert_not_called()

        result = json.loads(ingest.result_path(file_id).read_text(encoding="utf-8"))
        self.assertEqual(result["lightrag"]["state"], "skipped")

    def test_direct_ingest_is_blocked_when_quality_missing(self) -> None:
        """作业还没质检（level 为空）时直接调入库 → 同样被拦下。"""
        path = fixtures.make_text(self.tmp / "未质检.md", "内容充分的资料正文。" * 20)
        data = ingest.create_job(filename="未质检.md", content=path.read_bytes())
        file_id = data["file_id"]
        ingest._write_content(file_id, path.read_text(encoding="utf-8"))

        outcome = ingest._stage_ingest(file_id, fixtures.settings_with(), forced=False)

        self.assertEqual(outcome["state"], ingest.STATE_FAILED)
        self.upload_mock.assert_not_called()

    def test_forced_ingest_still_allowed(self) -> None:
        """强制入库是设计内的受控绕过：只允许人工放行，并且必须留痕。"""
        file_id = self._blocked_job()

        outcome = ingest._stage_ingest(file_id, fixtures.settings_with(), forced=True)

        self.assertEqual(outcome["state"], ingest.STATE_DONE)
        self.assertEqual(self.upload_mock.call_count, 1)
        result = json.loads(ingest.result_path(file_id).read_text(encoding="utf-8"))
        self.assertTrue(result["quality"]["forced"])
        self.assertTrue(result["quality"]["forced_at"])
        self.assertEqual(result["quality"]["forced_reasons"], ["重复行过多"])

    def test_pass_document_is_ingested(self) -> None:
        path = fixtures.make_text(self.tmp / "正常.md", "内容充分的资料正文。" * 20)
        data = ingest.create_job(filename="正常.md", content=path.read_bytes())
        file_id = data["file_id"]
        ingest._write_content(file_id, path.read_text(encoding="utf-8"))
        ingest._patch_section(file_id, "quality", level="pass", score=90, reasons=[])

        outcome = ingest._stage_ingest(file_id, fixtures.settings_with(), forced=False)

        self.assertEqual(outcome["state"], ingest.STATE_DONE)
        self.assertEqual(self.upload_mock.call_count, 1)


class GateStructureTests(unittest.TestCase):
    """结构约束：全仓库只有 ingest.py 可以调用 lightrag.upload(...)。"""

    def _python_files(self):
        return [
            path
            for path in APP_DIR.rglob("*.py")
            if "__pycache__" not in path.parts
        ]

    def test_only_ingest_module_uploads_to_lightrag(self) -> None:
        offenders = []
        for path in self._python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr != "upload":
                    continue
                target = ast.unparse(node.func.value)
                if "lightrag" in target and path != INGEST_MODULE:
                    offenders.append(f"{path.relative_to(BASE_DIR)}:{node.lineno} {target}.upload")

        self.assertEqual(offenders, [], f"只有 app/services/ingest.py 允许调用 lightrag.upload：{offenders}")

    def test_ingest_module_has_upload_call(self) -> None:
        """反向保险：确保 ingest.py 里确实还有那一处唯一出口（避免重构时误删）。"""
        tree = ast.parse(INGEST_MODULE.read_text(encoding="utf-8"))
        calls = [
            ast.unparse(node.func)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "upload"
        ]
        self.assertEqual(calls, ["lightrag.upload"])


class AdviceStructureTests(unittest.TestCase):
    """结构约束（Phase 15）：入库建议是**纯函数** —— 不联网、不读磁盘、不起进程。

    为什么值得守：建议是在「还没确认入库」时就要展示给用户看的（卡片 + `/ingest/pending`）。
    如果哪天有人为了"更准的建议"在 `advice.py` 里读文件或调接口，就等于在用户点头之前
    动了文件、甚至把内容送出了本机；这条断言让这种事立刻现形。
    """

    MODULE = APP_DIR / "services" / "advice.py"
    FORBIDDEN_IMPORTS = {
        "requests", "httpx", "aiohttp", "urllib", "urllib3", "http",
        "socket", "subprocess", "ftplib", "smtplib", "shutil", "tempfile",
    }
    FORBIDDEN_CALLS = {
        "open", "read_text", "read_bytes", "write_text", "write_bytes",
        "urlopen", "connect", "Popen", "run",
    }

    def _tree(self) -> ast.Module:
        self.assertTrue(self.MODULE.is_file(), f"缺少 {self.MODULE.name}（确认门的建议模块）")
        return ast.parse(self.MODULE.read_text(encoding="utf-8"))

    def test_advice_only_imports_pure_modules(self) -> None:
        offenders = []
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for name in names:
                if name in self.FORBIDDEN_IMPORTS:
                    offenders.append(f"{node.lineno} import {name}")

        self.assertEqual(offenders, [], f"advice.py 只能是纯函数（不联网 / 不读盘）：{offenders}")

    def test_advice_never_touches_disk_or_network(self) -> None:
        offenders = []
        for node in ast.walk(self._tree()):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            else:
                continue
            if name in self.FORBIDDEN_CALLS:
                offenders.append(f"{node.lineno} {ast.unparse(node.func)}()")

        self.assertEqual(offenders, [], f"advice.py 不得读文件 / 发请求 / 起进程：{offenders}")

    def test_advice_is_still_used_by_the_gate(self) -> None:
        """反向保险：确认门确实还在用这份建议（避免重构后卡片上再也看不到建议）。"""
        tree = ast.parse(INGEST_MODULE.read_text(encoding="utf-8"))
        called = {
            ast.unparse(node.func)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }

        self.assertIn("advice_service.advise", called)


if __name__ == "__main__":
    unittest.main()
