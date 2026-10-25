"""Phase 16：文件夹轮询（services/inbox.py）的离线用例。

覆盖：
1. 「长稳」判断：刚出现 / 还在变大的文件不碰，稳定两轮之后才受理；
2. 受理结果：原件移动（不是复制）到 data\\originals\\YYYY-MM\\、作业交给 ingest、
   指纹写进 data\\watch\\index.json（**同一内容不会入库两次**）；
3. 隔离：类型不支持 / 太大 → 移进 _failed 并写下 .reason.txt（**不删**）；
4. 忽略：临时文件（.part / ~$…）、_taken 与 _failed 子目录一律不扫；
5. 危险边界：本模块**不直接**调用 lightrag（入库出口仍在 ingest.py），
   与收件箱确认门默认 auto；
6. 中断重受理（`_should_reaccept`）：服务被停导致作业还卡在 parsing / 作业档案已消失 /
   上次压根没建成作业 → 重投必须**重新受理**（宁可多受理一次，也不静默丢件）；
   只有「同一次运行内重复」与「上次已经跑完（done / review / skipped / failed …）」才判重。

全程离线：目录换到临时目录，`ingest.enqueue` 换成替身（不起工作线程），不碰真实 data\\。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from app.core.config import DEFAULT_INBOX_CONFIRM
from app.services import inbox, ingest, origin
from tests import fixtures

GOOD_DOC = "这是一份用于测试的资料。" * 20


class InboxFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        self.inbox_dir = self.data / "inbox"
        self.taken = self.inbox_dir / "_taken"
        self.failed = self.inbox_dir / "_failed"
        self.originals = self.data / "originals"
        self.watch = self.data / "watch"
        self.index = self.watch / "index.json"
        self.parsed = self.data / "parsed"
        self.knowledge = self.root / "knowledge"
        for path in (self.inbox_dir, self.parsed, self.knowledge):
            path.mkdir(parents=True, exist_ok=True)

        self.settings = fixtures.settings_with(
            inbox_enabled=True,
            inbox_poll_seconds=600,
            inbox_settle_seconds=0,
            inbox_confirm="always",
        )
        self.enqueued: list = []
        self._patchers = [
            mock.patch.object(inbox, "INBOX_DIR", self.inbox_dir),
            mock.patch.object(inbox, "INBOX_TAKEN_DIR", self.taken),
            mock.patch.object(inbox, "INBOX_FAILED_DIR", self.failed),
            mock.patch.object(inbox, "ORIGINALS_DIR", self.originals),
            mock.patch.object(inbox, "WATCH_DIR", self.watch),
            mock.patch.object(inbox, "WATCH_INDEX", self.index),
            mock.patch.object(inbox, "get_settings", return_value=self.settings),
            mock.patch.object(origin, "BASE_DIR", self.root),
            mock.patch.object(ingest, "PARSED_DIR", self.parsed),
            mock.patch.object(ingest, "KNOWLEDGE_DIR", self.knowledge),
            mock.patch.object(ingest, "enqueue", self.enqueued.append),
            *fixtures.patch_settings(),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(inbox.reset_state)
        inbox.reset_state()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ---------- 辅助 ----------

    def drop(self, name: str, text: str = GOOD_DOC) -> Path:
        """往收件箱丢一个文件（模拟用户复制进来）。"""
        target = self.inbox_dir / name
        target.write_bytes(text.encode("utf-8"))
        return target

    def accept(self, name: str) -> dict:
        """丢文件 → 扫两轮（第一轮登记、第二轮受理），返回受理结果。"""
        self.drop(name)
        inbox.scan_once(reason="test")
        return inbox.scan_once(reason="test")

    def read_index(self) -> dict:
        return json.loads(self.index.read_text(encoding="utf-8"))

    def jobs(self) -> list:
        return [item for item in self.parsed.iterdir() if item.is_dir()]


class SettleTests(InboxFixture):
    """「长稳」判断。"""

    def test_first_round_only_registers_the_candidate(self) -> None:
        self.drop("资料.md")

        first = inbox.scan_once(reason="test")

        self.assertEqual(first["accepted"], 0)
        self.assertEqual(first["inspected"], 1)
        self.assertEqual([item["name"] for item in first["settling"]], ["资料.md"])

    def test_second_round_accepts_the_file(self) -> None:
        result = self.accept("资料.md")

        self.assertEqual(result["accepted"], 1)
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(len(self.enqueued), 1)

    def test_growing_file_keeps_waiting(self) -> None:
        target = self.drop("大文件.md", "x" * 10)
        inbox.scan_once(reason="test")

        target.write_bytes(("x" * 20).encode("utf-8"))     # 还在变大
        second = inbox.scan_once(reason="test")

        self.assertEqual(second["accepted"], 0)

        third = inbox.scan_once(reason="test")             # 不再变了
        self.assertEqual(third["accepted"], 1)

    def test_temp_and_hidden_files_are_ignored(self) -> None:
        for name in ("~$报告.docx", "下载中.crdownload", "半截.part", ".隐藏.txt"):
            self.drop(name)

        result = inbox.scan_once(reason="test")

        self.assertEqual(result["handled"], [])
        self.assertEqual(result["accepted"], 0)
        # 原件一个都没动：不搬、不删（用户自己决定怎么处理）
        self.assertTrue((self.inbox_dir / "半截.part").is_file())
        self.assertTrue((self.inbox_dir / "~$报告.docx").is_file())

    def test_subdirectories_of_inbox_are_not_scanned(self) -> None:
        self.taken.mkdir(parents=True, exist_ok=True)
        (self.taken / "老文件.md").write_text("old", encoding="utf-8")

        result = inbox.scan_once(reason="test")

        self.assertEqual(result["inspected"], 0)
        self.assertTrue((self.taken / "老文件.md").is_file())


class AcceptTests(InboxFixture):
    """受理：原件归档、作业参数、指纹索引。"""

    def test_original_is_moved_and_recorded(self) -> None:
        self.accept("资料.md")

        month = self.originals / datetime.now().strftime("%Y-%m")
        self.assertTrue((month / "资料.md").is_file())
        self.assertFalse((self.inbox_dir / "资料.md").exists())   # 收件箱已清空

        data = ingest.read_result(self.enqueued[0]) or {}
        self.assertEqual(data["origin"], "inbox")
        self.assertEqual(data["confirm_override"], DEFAULT_INBOX_CONFIRM)
        self.assertTrue(str(data["archived_path"]).startswith("data/originals/"))
        self.assertEqual(data["name"], "资料.md")

    def test_same_name_with_new_content_is_accepted_again(self) -> None:
        # 判重看的是**内容指纹**，不是文件名：改过内容的同名文件应当照常入库
        self.accept("资料.md")
        self.drop("资料.md", GOOD_DOC + "（第二版）")
        inbox.scan_once(reason="test")
        second = inbox.scan_once(reason="test")

        self.assertEqual(second["accepted"], 1)
        self.assertEqual(len(self.enqueued), 2)

    def test_duplicate_content_is_archived_to_taken(self) -> None:
        self.accept("资料.md")
        self.drop("副本.md")                     # 内容完全相同
        inbox.scan_once(reason="test")
        result = inbox.scan_once(reason="test")

        self.assertEqual(result["accepted"], 0)
        self.assertEqual(result["duplicate"], 1)
        self.assertEqual(len(self.enqueued), 1)   # 只入库一次
        self.assertTrue((self.taken / "副本.md").is_file())
        self.assertIn("完全相同", result["handled"][0]["reason"])

    def test_index_records_digest_and_job(self) -> None:
        self.accept("资料.md")

        index = self.read_index()

        self.assertEqual(len(index), 1)
        digest, entry = list(index.items())[0]
        self.assertEqual(len(digest), 64)                  # sha256
        self.assertEqual(entry["name"], "资料.md")
        self.assertEqual(entry["file_id"], self.enqueued[0])
        self.assertTrue(entry["archived"].startswith("data/originals/"))

    def test_duplicate_check_survives_a_restart(self) -> None:
        self.accept("资料.md")
        # 上一次运行已经把这份资料送进知识库（state=done）→ 重启后当然还是要判重
        ingest._update(self.enqueued[0], state=ingest.STATE_DONE)
        inbox.reset_state()                                # 模拟服务重启（内存清空、索引还在）
        self.drop("又一份.md")
        inbox.scan_once(reason="test")
        result = inbox.scan_once(reason="test")

        self.assertEqual(result["accepted"], 0)
        self.assertEqual(result["duplicate"], 1)

    def test_index_with_bom_still_reads(self) -> None:
        """别人（脚本 / 记事本 / PowerShell）写出带 BOM 的索引也要能读。

        读不了就会被当成「索引损坏」静默丢空 —— 同一份资料会再入一次库（重复文档）。
        """
        self.accept("资料.md")
        # 上一次运行已经把它送进知识库（state=done）→ 重启后当然还是要判重
        ingest._update(self.enqueued[0], state=ingest.STATE_DONE)
        self.index.write_bytes(b"\xef\xbb\xbf" + self.index.read_bytes())
        inbox.reset_state()                                # 丢掉内存缓存，重新读盘

        self.drop("又一份.md")
        inbox.scan_once(reason="test")
        result = inbox.scan_once(reason="test")

        self.assertEqual(result["accepted"], 0)
        self.assertEqual(result["duplicate"], 1)

    def test_interrupted_job_is_reaccepted_after_restart(self) -> None:
        """上一次受理到一半就被停（作业还停在 parsing）→ 重投同一份必须重新受理。

        Phase 16 真机事故的尾巴：服务被误停之后作业停在 parsing（再也不会自己继续），
        用户把那份文件**再丢一次**，如果只按内容指纹判重，页面只会说「内容完全相同」，
        资料就永远进不了知识库 —— 静默丢件比多受理一次糟得多，所以这里必须重新受理。
        """
        self.accept("资料.md")
        self.assertEqual(ingest.read_result(self.enqueued[0])["state"], ingest.STATE_PARSING)

        inbox.reset_state()                                # 服务重启：上一次的作业已经跑不动了
        self.drop("再投一次.md")
        inbox.scan_once(reason="test")
        result = inbox.scan_once(reason="test")

        self.assertEqual(result["accepted"], 1)
        self.assertEqual(result["duplicate"], 0)
        self.assertEqual(len(self.enqueued), 2)             # 又建了一个作业
        self.assertTrue(result["handled"][0]["reaccepted"])
        self.assertIn("没跑完", result["handled"][0]["reason"])
        month = self.originals / datetime.now().strftime("%Y-%m")
        self.assertTrue((month / "再投一次.md").is_file())
        self.assertFalse((self.taken / "再投一次.md").exists())   # 没被当成重复件归档掉

    def test_job_missing_from_disk_is_reaccepted(self) -> None:
        """作业档案已经不存在（被清理 / 磁盘被删）→ 同样重新受理，不静默判重。"""
        self.accept("资料.md")
        shutil.rmtree(ingest.job_dir(self.enqueued[0]), ignore_errors=True)

        inbox.reset_state()
        self.drop("再投一次.md")
        inbox.scan_once(reason="test")
        result = inbox.scan_once(reason="test")

        self.assertEqual(result["accepted"], 1)
        self.assertEqual(result["duplicate"], 0)
        self.assertIn("作业记录已不存在", result["handled"][0]["reason"])


class ReacceptRuleTests(unittest.TestCase):
    """`_should_reaccept()` 的判据（纯函数，不碰磁盘）。

    取舍写死在 inbox.py 的 docstring 里：**宁可多受理一次，也不要把用户的资料静默丢掉**，
    所以只有「同一次运行内重复」与「上一次已经跑完」才判重。
    """

    def test_job_never_created_is_reaccepted(self) -> None:
        ok, why = inbox._should_reaccept({"file_id": "", "run": "old-run"})
        self.assertTrue(ok)
        self.assertIn("没有建成作业", why)

    def test_same_run_duplicate_is_not_reaccepted(self) -> None:
        ok, why = inbox._should_reaccept(
            {"file_id": "20260101000000-abcdef", "run": inbox.RUN_ID}
        )
        self.assertFalse(ok)
        self.assertEqual(why, "")

    def test_unfinished_states_are_reaccepted(self) -> None:
        """上一次受理之后服务就被停了 → 那些作业不会再自己继续，必须重新受理。"""
        for state in (ingest.STATE_PARSING, ingest.STATE_CHECKING, ""):
            with self.subTest(state=state or "空状态"), \
                 mock.patch.object(ingest, "read_result",
                                   lambda fid, _state=state: {"state": _state}):
                ok, why = inbox._should_reaccept(
                    {"file_id": "20260101000000-abcdef", "run": "old-run"}
                )
                self.assertTrue(ok)
                self.assertIn("没跑完", why)

    def test_finished_states_keep_deduplicating(self) -> None:
        for state in (ingest.STATE_DONE, ingest.STATE_REVIEW, ingest.STATE_SKIPPED,
                      ingest.STATE_INGESTING, ingest.STATE_FAILED, ingest.STATE_BLOCKED,
                      ingest.STATE_NEEDS_OCR):
            with self.subTest(state=state), \
                 mock.patch.object(ingest, "read_result",
                                   lambda fid, _state=state: {"state": _state}):
                ok, why = inbox._should_reaccept(
                    {"file_id": "20260101000000-abcdef", "run": "old-run"}
                )
                self.assertFalse(ok)
                self.assertEqual(why, "")

    def test_missing_job_record_is_reaccepted(self) -> None:
        with mock.patch.object(ingest, "read_result", lambda fid: None):
            ok, why = inbox._should_reaccept(
                {"file_id": "20260101000000-abcdef", "run": "old-run"}
            )
        self.assertTrue(ok)
        self.assertIn("作业记录已不存在", why)


class IsolateTests(InboxFixture):
    """受理不了的文件：隔离 + 写下原因，绝不清掉。"""

    def test_unsupported_extension_is_isolated_with_reason(self) -> None:
        self.drop("木马.exe")
        inbox.scan_once(reason="test")
        result = inbox.scan_once(reason="test")

        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.jobs(), [])
        self.assertTrue((self.failed / "木马.exe").is_file())
        reason = (self.failed / "木马.exe.reason.txt").read_text(encoding="utf-8")
        self.assertIn("不支持的文件类型", reason)

    def test_oversized_file_is_isolated(self) -> None:
        small_limit = fixtures.settings_with(
            inbox_enabled=True, inbox_settle_seconds=0, max_upload_size=10
        )
        with mock.patch.object(inbox, "get_settings", return_value=small_limit):
            self.drop("很大的.md", "x" * 100)
            inbox.scan_once(reason="test")
            result = inbox.scan_once(reason="test")

        self.assertEqual(result["failed"], 1)
        reason = (self.failed / "很大的.md.reason.txt").read_text(encoding="utf-8")
        self.assertIn("文件太大", reason)

    def test_job_creation_failure_keeps_the_original(self) -> None:
        def boom(**kwargs):
            raise RuntimeError("磁盘满了")

        with mock.patch.object(ingest, "create_job", boom):
            self.drop("坏运气.md")
            inbox.scan_once(reason="test")
            result = inbox.scan_once(reason="test")

        self.assertEqual(result["failed"], 1)
        # 原件不能丢：先在 data\originals\ 归档过，再被隔离进 _failed
        self.assertTrue(list(self.failed.glob("坏运气.md*")))
        reason = (self.failed / "坏运气.md.reason.txt").read_text(encoding="utf-8")
        self.assertIn("受理失败", reason)

    def test_no_direct_lightrag_calls(self) -> None:
        # 结构不变量：轮询模块**不许**自己接上游（入库出口只有 ingest._stage_ingest）
        source = Path(inbox.__file__).read_text(encoding="utf-8")

        self.assertNotIn("from app.clients", source)
        self.assertNotIn("import lightrag", source)


class StatusAndThreadTests(InboxFixture):
    def test_status_reports_dirs_and_counts(self) -> None:
        self.accept("资料.md")

        payload = inbox.status()

        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["settle_seconds"], 0)
        self.assertEqual(payload["confirm"], "always")
        self.assertEqual(payload["dirs"]["inbox"]["files"], 0)
        self.assertEqual(payload["dirs"]["originals"]["files"], 1)
        self.assertTrue(payload["dirs"]["index"]["exists"])
        self.assertEqual(payload["accepted"], 1)
        self.assertEqual(payload["known"], 1)
        self.assertIn("data/inbox", payload["hint"])
        self.assertEqual(payload["events"][0]["kind"], "accepted")

    def test_scan_once_creates_all_directories(self) -> None:
        inbox.scan_once(reason="test")

        for path in (self.inbox_dir, self.taken, self.failed, self.originals, self.watch):
            self.assertTrue(path.is_dir(), f"{path} 应该被自动创建")

    def test_start_is_skipped_when_disabled(self) -> None:
        disabled = fixtures.settings_with(inbox_enabled=False)
        with mock.patch.object(inbox, "get_settings", return_value=disabled):
            self.assertFalse(inbox.start())

        self.assertFalse(inbox.is_running())

    def test_start_and_stop_the_watcher(self) -> None:
        self.assertTrue(inbox.start())
        self.assertTrue(inbox.is_running())
        self.assertFalse(inbox.start())            # 幂等：不会开出第二个线程
        self.assertTrue(inbox.stop())
        self.assertFalse(inbox.is_running())

    def test_default_confirm_mode_is_always(self) -> None:
        # 公开默认值：收件箱来的文件也必须人工确认。
        self.assertEqual(DEFAULT_INBOX_CONFIRM, "always")


if __name__ == "__main__":
    unittest.main()