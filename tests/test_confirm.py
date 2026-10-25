"""入库确认门单测（Phase 15）：通过质检的文件不会自己进知识库，必须有人点头。

三层各测一遍（每一层都是「用户能看到什么」的一部分）：
1. `services/advice.py`（纯函数）：结论必须可解释，且只有 ok 才允许自动放行；
2. `ingest._stage_confirm()`：always / auto / off / 会话级「全部允许」四种走法，
   以及「等确认时绝不调用 lightrag.upload」这条不变量；
3. 用户动作与时间：点「入库」/「跳过」/ 等待超时归档 / 非法动作一律 409。

页面契约（static/*.html 有没有真的接上后端）单独一组用例，避免「后端能跑、
页面上没按钮」这种半成品状态漏到用户手上。
"""

from __future__ import annotations

import json
import queue
import re
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from app.services import advice as advice_service
from app.services import ingest
from tests import fixtures

BASE_DIR = Path(__file__).resolve().parents[1]

#: 一份「内容量足够、结构正常」的正文：质检 pass，建议 ok
GOOD_DOC = (
    "# 年度工作总结\n\n"
    "第一部分：本年度共整理资料 128 份，覆盖财务、合同与个人档案三类。\n"
    "第二部分：完成知识库问答验证 60 次，命中率从 62% 提升到 88%。\n"
    "第三部分：把重复上传的资料做了合并，索引体积下降了约三成。\n"
    "第四部分：下一阶段计划补做历史归档的扫描件识别，并优化检索策略。\n"
    "第五部分：把常用问题整理成清单，方便随时提问与复核。\n"
    "第六部分：所有资料都只放在本机知识库里，不上传到任何外部服务。\n"
    "第七部分：重要结论在归档前都做过一次人工抽查。\n"
    "第八部分：遇到解析质量不好的文件，优先重新导出而不是硬入知识库。\n"
    "第九部分：整理目录时按年份与主题两级分类，便于后续检索。\n"
)

#: 同样能过质检，但含手机号与邮箱：建议降为 attention（值得先看一眼）
ATTENTION_DOC = GOOD_DOC + "联系人：张三，手机 13800001111，邮箱 zhangsan@example.com\n"

#: 重复行过多 → 质量门直接判 blocked（到不了确认门）
BLOCKED_DOC = "\n".join(["同样的一行内容"] * 60)


class AdviceTests(unittest.TestCase):
    """智能建议：可解释、离线、只有 ok 才算「可以直接入库」。"""

    def test_good_doc_is_ok(self) -> None:
        result = advice_service.advise(GOOD_DOC, file_name="工作总结.md")

        self.assertEqual(result.level, advice_service.LEVEL_OK)
        self.assertTrue(result.auto_ok)
        self.assertEqual(result.headline, advice_service.HEADLINES[advice_service.LEVEL_OK])
        self.assertEqual(result.reasons, [])
        self.assertTrue(all(item["ok"] for item in result.checks))
        self.assertEqual(result.stats["file_name"], "工作总结.md")
        self.assertTrue(result.preview)

    def test_every_check_is_explainable(self) -> None:
        """每条建议都必须带「名字 + 结论 + 人话说明」，前端要原样展示。"""
        result = advice_service.advise(GOOD_DOC)

        self.assertTrue(result.checks)
        for item in result.checks:
            self.assertTrue(item.get("name"), item)
            self.assertIn(item.get("level"), advice_service.LEVEL_ORDER)
            self.assertIsInstance(item.get("ok"), bool)
            self.assertTrue(item.get("detail"), item)

    def test_personal_info_downgrades_to_attention(self) -> None:
        result = advice_service.advise(ATTENTION_DOC)

        self.assertEqual(result.level, advice_service.LEVEL_ATTENTION)
        self.assertFalse(result.auto_ok)

        check = next(item for item in result.checks if item["name"] == "个人信息")
        self.assertFalse(check["ok"])
        self.assertIn("手机号", check["detail"])
        # 「为什么建议你先看一眼」必须能在页面上原样说出来
        self.assertIn(check["detail"], result.reasons)

    def test_pending_images_are_flagged(self) -> None:
        result = advice_service.advise(GOOD_DOC, images_total=3, images_pending=3)

        self.assertEqual(result.level, advice_service.LEVEL_RISK)
        self.assertIn("内嵌图片", [item["name"] for item in result.checks if not item["ok"]])

    def test_preview_is_trimmed(self) -> None:
        result = advice_service.advise("测" * 500)

        self.assertEqual(len(result.preview), advice_service.PREVIEW_CHARS + 1)
        self.assertTrue(result.preview.endswith("…"))

    def test_advice_never_says_gate_is_off(self) -> None:
        """Phase 18：质检关不掉 —— 建议文案里不该再出现「质量门已关闭」这类误导说法。"""
        result = advice_service.advise("太短的一段话")

        content_check = next(item for item in result.checks if item["name"] == "内容量")
        self.assertFalse(any("已关闭" in item["detail"] for item in result.checks), result.checks)
        self.assertFalse(content_check["ok"])   # 内容太少 → 如实提示，不再"放行式安慰"


class IngestJobMixin:
    """作业级用例的公共夹具：落盘目录指向临时目录、LightRAG 上传换成替身，全程离线。

    `ConfirmGateTests`（Phase 15 确认门）与 `EditContentTests`（Phase 15.2 改正文）
    共用这一套：两边的「用例之间必须互不串状态」要求完全一样，
    夹具（临时目录 / 替身 / 模块级全局变量复位）只能有一份。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)

        self.parsed_dir = self.tmp / "parsed"
        self.knowledge_dir = self.tmp / "knowledge"
        self.parsed_dir.mkdir(parents=True, exist_ok=True)
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)

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

        # 会话级开关是模块级全局状态，用例之间必须复位
        self.addCleanup(ingest.set_session_allow_all, False)
        # Phase 15.1：队列与「重复提交登记表」同样是模块级状态，用例之间必须清干净
        self.addCleanup(ingest._pending_keys.clear)
        self.addCleanup(self._drain_jobs)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ---------- 辅助 ----------

    def confirm_with(self, **overrides) -> None:
        """临时改确认门设置（例如 ingest_confirm="off"）。"""
        for patcher in fixtures.patch_settings(**overrides):
            patcher.start()
            self.addCleanup(patcher.stop)

    def submit(self, text: str, name: str = "资料.md") -> str:
        path = fixtures.make_text(self.tmp / name, text)
        data = ingest.create_job(filename=name, content=path.read_bytes())
        return data["file_id"]

    def run_to_review(self, text: str = GOOD_DOC, name: str = "资料.md") -> str:
        """跑完解析 + 质检，停在确认门上的作业。"""
        file_id = self.submit(text, name)
        state = ingest.run_job(file_id)
        self.assertEqual(state["state"], ingest.STATE_REVIEW, state)
        return file_id

    def result_json(self, file_id: str) -> dict:
        return json.loads(ingest.result_path(file_id).read_text(encoding="utf-8"))

    def wait_for_state(self, file_id: str, expected: str, timeout: float = 20.0) -> dict:
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = ingest.read_result(file_id) or {}
            if data.get("state") == expected:
                return data
            time.sleep(0.05)
        self.fail(f"作业在 {timeout} 秒内没有进入 {expected}：{ingest.read_result(file_id)}")
        return {}

    def _drain_jobs(self) -> None:
        """清掉队列里没被工作线程取走的作业（按住工作线程的用例会留下残留）。"""
        while True:
            try:
                ingest._queue.get_nowait()
            except Exception:   # queue.Empty：队列已经空了
                return


class ConfirmGateTests(IngestJobMixin, unittest.TestCase):
    """确认门：质检通过后必须有人点头才入库（夹具见 IngestJobMixin）。"""

    # ---------- always（默认）----------

    def test_always_stops_before_knowledge_base(self) -> None:
        file_id = self.run_to_review()

        self.upload_mock.assert_not_called()
        data = self.result_json(file_id)
        self.assertTrue(data["review_since"])
        self.assertEqual(data["advice"]["level"], advice_service.LEVEL_OK)
        self.assertEqual(data["advice"]["mode"], ingest.CONFIRM_ALWAYS)
        self.assertFalse(data["advice"]["auto"])
        self.assertEqual(data["advice"]["allowed_by"], "")

        payload = ingest.status(file_id)
        self.assertTrue(payload["can"]["ingest"])
        self.assertTrue(payload["can"]["skip"])
        self.assertFalse(payload["can"]["force"])
        self.assertTrue(payload["advice"]["checks"])

    def test_pending_reviews_lists_waiting_jobs(self) -> None:
        file_id = self.run_to_review(name="待确认.md")

        items = ingest.pending_reviews()

        self.assertEqual([item["file_id"] for item in items], [file_id])
        item = items[0]
        self.assertEqual(item["name"], "待确认.md")
        self.assertTrue(item["review_since"])
        self.assertTrue(item["advice"]["headline"])

    def test_active_jobs_lists_busy_and_disappears_after_stop(self) -> None:
        file_id = self.submit(GOOD_DOC, "正在解析.md")

        items = ingest.active_jobs()

        self.assertEqual([item["file_id"] for item in items], [file_id])
        self.assertEqual(items[0]["state"], ingest.STATE_PARSING)
        self.assertEqual(items[0]["name"], "正在解析.md")

        ingest.run_job(file_id)
        self.assertEqual(ingest.active_jobs(), [])
    # ---------- off / auto ----------

    def test_off_ingests_without_asking(self) -> None:
        self.confirm_with(ingest_confirm="off")
        file_id = self.submit(GOOD_DOC)

        state = ingest.run_job(file_id)

        self.assertEqual(state["state"], ingest.STATE_DONE)
        self.assertEqual(self.upload_mock.call_count, 1)
        data = self.result_json(file_id)
        self.assertEqual(data["advice"]["allowed_by"], ingest.CONFIRM_OFF)
        self.assertEqual(data["review_since"], "")
        self.assertEqual(ingest.pending_reviews(), [])

    def test_auto_ingests_ok_but_still_asks_about_attention(self) -> None:
        self.confirm_with(ingest_confirm="auto")

        good = self.submit(GOOD_DOC, "正常.md")
        self.assertEqual(ingest.run_job(good)["state"], ingest.STATE_DONE)
        self.assertEqual(self.result_json(good)["advice"]["allowed_by"], "auto")

        risky = self.submit(ATTENTION_DOC, "含个人信息.md")
        self.assertEqual(ingest.run_job(risky)["state"], ingest.STATE_REVIEW)
        self.assertEqual(self.upload_mock.call_count, 1)  # 只有第一份入库

    # ---------- 会话级「全部允许」 ----------

    def test_session_allow_all_releases_waiting_jobs(self) -> None:
        file_id = self.run_to_review()

        outcome = ingest.set_session_allow_all(True)

        self.assertTrue(outcome["allow_all"])
        self.assertEqual(outcome["released"], 1)
        self.assertIn("已打开", outcome["message"])
        self.wait_for_state(file_id, ingest.STATE_DONE)
        self.assertEqual(self.upload_mock.call_count, 1)
        self.assertEqual(ingest.pending_reviews(), [])

    def test_session_allow_all_skips_gate_for_new_files(self) -> None:
        ingest.set_session_allow_all(True)
        file_id = self.submit(GOOD_DOC)

        state = ingest.run_job(file_id)

        self.assertEqual(state["state"], ingest.STATE_DONE)
        data = self.result_json(file_id)
        self.assertEqual(data["advice"]["mode"], ingest.CONFIRM_ALWAYS)  # 设置没变
        self.assertEqual(data["advice"]["allowed_by"], "session")        # 放行理由写进档案

    def test_session_allow_all_can_be_turned_off(self) -> None:
        ingest.set_session_allow_all(True)
        outcome = ingest.set_session_allow_all(False)

        self.assertFalse(outcome["allow_all"])
        self.assertEqual(outcome["released"], 0)
        self.assertIn("已关闭", outcome["message"])
        # 关掉之后新文件照旧停在确认门上
        self.assertEqual(ingest.run_job(self.submit(GOOD_DOC))["state"], ingest.STATE_REVIEW)

    # ---------- 用户动作 ----------

    def test_ingest_action_submits_to_knowledge_base(self) -> None:
        file_id = self.run_to_review()

        outcome = ingest.activate(file_id, ingest.ACTION_INGEST)
        self.assertTrue(outcome["ok"], outcome)

        self.wait_for_state(file_id, ingest.STATE_DONE)
        self.assertEqual(self.upload_mock.call_count, 1)
        data = self.result_json(file_id)
        self.assertFalse(data["quality"]["forced"])          # 确认入库不是「强制入库」
        self.assertEqual(data["review_since"], "")
        self.assertIn(ingest.GATE_CONFIRMED_NOTE, data["detail"])
        self.assertEqual(ingest.pending_reviews(), [])

    def test_skip_action_never_uploads(self) -> None:
        file_id = self.run_to_review()

        outcome = ingest.activate(file_id, ingest.ACTION_SKIP)
        self.assertTrue(outcome["ok"], outcome)

        self.wait_for_state(file_id, ingest.STATE_SKIPPED)
        self.upload_mock.assert_not_called()
        data = self.result_json(file_id)
        self.assertEqual(data["lightrag"]["state"], "skipped")
        self.assertEqual(data["detail"], ingest.DETAIL_SKIPPED)
        self.assertTrue(data["skipped_at"])
        self.assertEqual(ingest.pending_reviews(), [])

    def test_actions_rejected_outside_review(self) -> None:
        """不在确认门上的作业：点「入库 / 跳过」都必须被挡住（409）。"""
        self.confirm_with(ingest_confirm="off")
        done = self.submit(GOOD_DOC, "已入库.md")
        ingest.run_job(done)

        blocked = self.submit(BLOCKED_DOC, "重复.md")
        ingest.run_job(blocked)

        for file_id, action in (
            (done, ingest.ACTION_INGEST),
            (blocked, ingest.ACTION_INGEST),
            (blocked, ingest.ACTION_SKIP),
        ):
            outcome = ingest.activate(file_id, action)
            self.assertFalse(outcome["ok"], (action, outcome))
            self.assertEqual(outcome["http_status"], 409, (action, outcome))

    def test_force_action_rejected_on_review(self) -> None:
        """已通过质检的作业走「入库」，不能用「强制入库」（否则档案里留误导痕迹）。"""
        file_id = self.run_to_review()

        outcome = ingest.activate(file_id, ingest.ACTION_FORCE)

        self.assertFalse(outcome["ok"], outcome)
        self.assertEqual(outcome["http_status"], 409)
        self.assertIn("入库", outcome["message"])
        self.upload_mock.assert_not_called()
        self.assertEqual(ingest.read_result(file_id)["state"], ingest.STATE_REVIEW)

    # ---------- Phase 15.1：连点幂等 / 重试语义 / 留档时序 ----------

    def test_repeated_ingest_clicks_submit_only_once(self) -> None:
        """连点「入库」只入库一次；队列里残留的重复作业也不会再打知识库。

        改造前的真实故障：刷新后连点，2 秒内 11 次动作、5 次被知识库 409 拒绝
        （logs/app.log 2026-10-16 00:51）。现在：入队去重 + 执行前复查状态，双保险。
        """
        file_id = self.run_to_review()

        # 按住后台工作线程：把模块级队列换成临时队列 + 不启新线程，
        # 否则前一个用例留下的工作线程会抢着把作业取走，断言队列长度就不可靠了。
        sandbox = queue.Queue()
        with mock.patch.object(ingest, "_queue", sandbox), \
                mock.patch.object(ingest, "_ensure_worker", lambda: None):
            outcomes = [ingest.activate(file_id, ingest.ACTION_INGEST) for _ in range(5)]

        self.assertTrue(all(item["ok"] for item in outcomes), outcomes)
        ignored = [item for item in outcomes if item.get("duplicate_submit")]
        self.assertEqual(len(ignored), 4, outcomes)
        self.assertIn("已忽略", ignored[0]["message"])
        self.assertEqual(sandbox.qsize(), 1)                 # 5 次点击只排了 1 个作业
        self.upload_mock.assert_not_called()

        ingest.run_job(file_id, ingest.ACTION_INGEST)       # 工作线程真正执行那一次
        self.assertEqual(ingest.read_result(file_id)["state"], ingest.STATE_DONE)
        self.assertEqual(self.upload_mock.call_count, 1)

        # 队列里若还留着重复作业（旧行为会再入库一次），现在必须整段忽略
        ingest.run_job(file_id, ingest.ACTION_INGEST)
        self.assertEqual(self.upload_mock.call_count, 1)
        self.assertEqual(ingest.read_result(file_id)["state"], ingest.STATE_DONE)

    def test_repeated_skip_clicks_are_ignored(self) -> None:
        """连点「跳过」同样只登记一次。"""
        file_id = self.run_to_review()

        sandbox = queue.Queue()
        with mock.patch.object(ingest, "_queue", sandbox), \
                mock.patch.object(ingest, "_ensure_worker", lambda: None):
            outcomes = [ingest.activate(file_id, ingest.ACTION_SKIP) for _ in range(3)]

        self.assertEqual(
            [bool(item.get("duplicate_submit")) for item in outcomes], [False, True, True]
        )
        self.assertEqual(sandbox.qsize(), 1)

        ingest.run_job(file_id, ingest.ACTION_SKIP)
        ingest.run_job(file_id, ingest.ACTION_SKIP)         # 重复作业：整段忽略
        self.assertEqual(ingest.read_result(file_id)["state"], ingest.STATE_SKIPPED)
        self.upload_mock.assert_not_called()

    def test_rejected_upload_explains_reason_and_leaves_no_archive(self) -> None:
        """知识库拒绝接收时：说人话 + 不留档 + 原文仍可查（Phase 15.1）。"""
        self.upload_mock.return_value = fixtures.FakeResponse(
            status_code=409,
            payload={
                "detail": "Document storage already contains '资料.md' (Status: processed). "
                "Delete the existing record before re-uploading."
            },
        )
        file_id = self.run_to_review()
        # 顺手验证自愈：旧版本「先留档再上传」可能已经留下过一份残留，被拒时应当一起回收
        stale = self.knowledge_dir / f"{file_id}_资料.md"
        stale.write_text("旧版本「先留档再上传」留下的残留", encoding="utf-8")

        state = ingest.run_job(file_id, ingest.ACTION_INGEST)

        self.assertEqual(state["state"], ingest.STATE_FAILED)
        self.assertIn("已经有同名文档", state["detail"])       # 不再是「HTTP 409，请查日志」
        self.assertIn("重试", state["detail"])
        self.assertEqual(list(self.knowledge_dir.glob("*.md")), [])   # ★ 没入库就不留档
        self.assertFalse(stale.exists())                      # ★ 历史残留也被回收
        data = self.result_json(file_id)
        self.assertIn("already contains", data["lightrag"]["error"] or "")
        self.assertEqual(data["lightrag"]["http_status"], 409)
        self.assertFalse(data.get("knowledge_path"))
        self.assertEqual(ingest.pending_reviews(), [])

    def test_skip_cleans_up_leftover_archive(self) -> None:
        """「跳过 = 没入库」要在 knowledge/ 里看得见：历史残留文件一并回收。"""
        file_id = self.run_to_review()
        stale = self.knowledge_dir / f"{file_id}_资料.md"
        stale.write_text("旧版本遗留的留档", encoding="utf-8")

        self.assertTrue(ingest.activate(file_id, ingest.ACTION_SKIP)["ok"])
        self.wait_for_state(file_id, ingest.STATE_SKIPPED)

        self.assertFalse(stale.exists())
        self.assertFalse(self.result_json(file_id).get("knowledge_path"))

    def test_retry_after_ingest_failure_resumes_the_gate(self) -> None:
        """入库被拒后点「重试」：从确认门继续，**不重跑解析**（否则确认界面又凭空出现）。"""
        self.upload_mock.return_value = fixtures.FakeResponse(
            status_code=409,
            payload={
                "detail": "Document storage already contains '资料.md' (Status: processed)."
            },
        )
        file_id = self.run_to_review()
        self.assertEqual(
            ingest.run_job(file_id, ingest.ACTION_INGEST)["state"], ingest.STATE_FAILED
        )

        with mock.patch.object(
            ingest.parse_service, "parse_file", side_effect=AssertionError("重试不该重新解析")
        ):
            state = ingest.run_job(file_id, ingest.ACTION_RETRY)

        self.assertEqual(state["state"], ingest.STATE_REVIEW)
        self.assertTrue(state["advice"]["headline"])       # 建议重算过，页面照旧能解释
        self.assertTrue(state["review_since"])
        self.assertEqual(self.upload_mock.call_count, 1)   # 重试本身没有再打知识库

    # ---------- 等待超时归档 ----------

    def test_expired_review_is_archived_as_skipped(self) -> None:
        file_id = self.run_to_review()
        ingest._update(
            file_id,
            review_since=(datetime.now() - timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S"),
        )

        archived = ingest.archive_expired_reviews(ttl_hours=24)

        self.assertEqual(archived, [file_id])
        self.upload_mock.assert_not_called()
        data = self.result_json(file_id)
        self.assertEqual(data["state"], ingest.STATE_SKIPPED)
        self.assertIn("24 小时", data["detail"])

    def test_recent_review_is_kept(self) -> None:
        file_id = self.run_to_review()

        self.assertEqual(ingest.archive_expired_reviews(ttl_hours=24), [])
        self.assertEqual(ingest.read_result(file_id)["state"], ingest.STATE_REVIEW)

    def test_archive_disabled_when_ttl_is_zero(self) -> None:
        file_id = self.run_to_review()
        ingest._update(file_id, review_since="2000-01-01 00:00:00")

        self.assertEqual(ingest.archive_expired_reviews(ttl_hours=0), [])
        self.assertEqual(ingest.read_result(file_id)["state"], ingest.STATE_REVIEW)


class EditContentTests(IngestJobMixin, unittest.TestCase):
    """Phase 15.2：过程可读化 + 先改正文再入库（夹具见 IngestJobMixin）。

    两条主线各测一遍：
    · 时间线只记录、只展示，**不参与判定**（写不进去也不影响入库）；
    · 编辑正文**不是绕过质量门的后门**（改完必重新质检，且永远要用户点头）。
    """

    # ---------- 一、处理过程时间线 ----------

    def test_timeline_walks_every_step_up_to_review(self) -> None:
        file_id = self.run_to_review()

        payload = ingest.status(file_id)
        rows = {row["key"]: row for row in payload["timeline"]}

        self.assertEqual(
            list(rows),
            [ingest.STEP_ACCEPTED, ingest.STEP_PARSING, ingest.STEP_CHECKING, ingest.STEP_REVIEW],
        )
        self.assertEqual(rows[ingest.STEP_ACCEPTED]["state"], "done")
        self.assertEqual(rows[ingest.STEP_PARSING]["state"], "done")
        self.assertEqual(rows[ingest.STEP_CHECKING]["state"], "done")
        self.assertEqual(rows[ingest.STEP_REVIEW]["state"], "current")

        for key, row in rows.items():
            self.assertEqual(row["label"], ingest.STEP_LABELS[key])
            self.assertTrue(row["at"], row)          # 每一步都要有时间戳
            self.assertTrue(row["hint"], row)        # 每一步都要能说人话
        self.assertIn("通过", rows[ingest.STEP_CHECKING]["hint"])
        self.assertIn("入库", rows[ingest.STEP_REVIEW]["hint"])   # 告诉用户现在该做什么
        self.assertGreaterEqual(payload["timeline_elapsed_s"], 0.0)
        self.assertTrue(payload["can"]["edit"])                   # 停在确认门 → 能改正文

    def test_timeline_points_at_the_failed_step(self) -> None:
        """被知识库拒绝时：失败的是「提交」那一步，原因直接写在提示语里。"""
        self.upload_mock.return_value = fixtures.FakeResponse(404, {"detail": "not found"})
        file_id = self.run_to_review()

        ingest.activate(file_id, ingest.ACTION_INGEST)
        self.wait_for_state(file_id, ingest.STATE_FAILED)

        rows = {row["key"]: row for row in ingest.status(file_id)["timeline"]}
        self.assertEqual(rows[ingest.STEP_SUBMIT]["state"], "failed")
        self.assertEqual(rows[ingest.STEP_REVIEW]["state"], "done")
        self.assertIn("拒绝", rows[ingest.STEP_SUBMIT]["hint"])
        self.assertNotIn(ingest.STEP_INDEX, rows)       # 没走到建索引，不空着一行

    def test_timeline_marks_skipped_job(self) -> None:
        file_id = self.run_to_review()
        ingest.activate(file_id, ingest.ACTION_SKIP)
        self.wait_for_state(file_id, ingest.STATE_SKIPPED)

        rows = {row["key"]: row for row in ingest.status(file_id)["timeline"]}
        self.assertEqual(rows[ingest.STEP_REVIEW]["state"], "skipped")
        self.assertIn("跳过", rows[ingest.STEP_REVIEW]["hint"])
        self.assertNotIn(ingest.STEP_SUBMIT, rows)

    def test_timeline_reaches_index_after_ingest(self) -> None:
        self.confirm_with(ingest_confirm="off")
        file_id = self.submit(GOOD_DOC)
        ingest.run_job(file_id)

        rows = {row["key"]: row for row in ingest.status(file_id)["timeline"]}
        self.assertEqual(rows[ingest.STEP_SUBMIT]["state"], "done")
        self.assertEqual(rows[ingest.STEP_INDEX]["state"], "current")
        self.assertIn("提交号", rows[ingest.STEP_SUBMIT]["hint"])

    def test_timeline_records_the_latest_run(self) -> None:
        """重跑（重新质检）覆盖旧时间：时间线讲的是「这一轮」，不是第一次。"""
        file_id = self.run_to_review()
        ingest._patch_section(
            file_id, "timeline", **{ingest.STEP_CHECKING: "2000-01-01 00:00:00"}
        )

        outcome = ingest.edit_content(file_id, GOOD_DOC + "\n补充一句：用于复核这一轮时间线。\n")

        self.assertTrue(outcome["ok"], outcome)
        stamps = ingest.read_result(file_id)["timeline"]
        self.assertNotEqual(stamps[ingest.STEP_CHECKING], "2000-01-01 00:00:00")
        self.assertTrue(stamps[ingest.STEP_ACCEPTED])    # 受理时间还在（不是重新上传）

    def test_timeline_write_failure_cannot_break_ingest(self) -> None:
        """「只展示不判定」不是口号：连写失败都不许抛出去。"""
        file_id = self.run_to_review()

        with mock.patch.object(ingest, "_write_result", side_effect=OSError("磁盘满了")):
            ingest._touch_step(file_id, ingest.STEP_INDEX)     # 不抛异常即通过

        self.assertEqual(ingest.read_result(file_id)["state"], ingest.STATE_REVIEW)

    # ---------- 二、先改正文，再入库 ----------

    def test_content_for_edit_returns_current_text(self) -> None:
        file_id = self.run_to_review()

        payload = ingest.content_for_edit(file_id)

        self.assertEqual(payload["file_id"], file_id)
        self.assertEqual(payload["content"], ingest._read_content(file_id))
        self.assertEqual(payload["chars"], len(payload["content"]))
        self.assertTrue(payload["editable"])
        self.assertEqual(payload["reason"], "")
        self.assertEqual(payload["edit"]["count"], 0)
        self.assertEqual(payload["max_chars"], ingest.CONTENT_MAX_CHARS)
        self.assertIsNone(ingest.content_for_edit("不存在的作业"))

    def test_edit_saves_backup_and_reruns_quality(self) -> None:
        file_id = self.run_to_review()
        original = ingest._read_content(file_id)
        edited = original + "\n补充：以上数据以归档原件为准。\n"

        outcome = ingest.edit_content(file_id, edited)

        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(outcome["state"], ingest.STATE_REVIEW)   # 仍然停在确认门
        self.assertEqual(self.upload_mock.call_count, 0)          # 编辑本身不入库
        self.assertEqual(ingest._read_content(file_id), edited)
        # 改之前的上一版留着对照；原件与上传留档不动
        backup = ingest.job_dir(file_id) / ingest.BACKUP_NAME
        self.assertEqual(backup.read_text(encoding="utf-8"), original)

        data = ingest.read_result(file_id)
        self.assertEqual(data["edit"]["count"], 1)
        self.assertTrue(data["edit"]["edited_at"])
        self.assertEqual(data["edit"]["before_chars"], len(original))
        self.assertEqual(data["edit"]["after_chars"], len(edited))
        self.assertIn("仍在等你点", outcome["message"])
        self.assertEqual(ingest.status(file_id)["edit"]["count"], 1)
        self.assertIn(ingest.BACKUP_NAME, ingest.status(file_id)["edit"]["backup"])

    def test_edit_is_not_a_backdoor_around_quality_gate(self) -> None:
        """把正文改坏（整篇重复行）→ 重新质检必须拦下，而且绝不入库。"""
        file_id = self.run_to_review()

        outcome = ingest.edit_content(file_id, BLOCKED_DOC)

        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(outcome["state"], ingest.STATE_BLOCKED)
        self.assertIn("没过", outcome["message"])
        self.assertEqual(self.upload_mock.call_count, 0)

        payload = ingest.status(file_id)
        rows = {row["key"]: row for row in payload["timeline"]}
        self.assertEqual(rows[ingest.STEP_CHECKING]["state"], "failed")
        self.assertIn("未通过", rows[ingest.STEP_CHECKING]["hint"])
        self.assertTrue(payload["can"]["force"])          # 兜底动作还在：可以「仍要入库」

    def test_edit_can_rescue_a_blocked_document(self) -> None:
        """质检没过也能改；改好就重新过门（但仍然要用户点头，不自动入库）。"""
        file_id = self.submit(BLOCKED_DOC)
        self.assertEqual(ingest.run_job(file_id)["state"], ingest.STATE_BLOCKED)

        outcome = ingest.edit_content(file_id, GOOD_DOC)

        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(outcome["state"], ingest.STATE_REVIEW)
        self.assertIn("重新质检通过", outcome["message"])
        self.assertEqual(self.upload_mock.call_count, 0)

    def test_save_and_ingest_uploads_the_edited_text(self) -> None:
        """「保存并入库」＝保存后再点一次「入库」：入库的是**改后**的正文。"""
        file_id = self.run_to_review()
        marker = "（本行是人工补充的修订说明）"
        edited = ingest._read_content(file_id) + "\n" + marker + "\n"

        outcome = ingest.edit_content(file_id, edited, ingest_now=True)

        self.assertTrue(outcome["ok"], outcome)
        self.assertTrue(outcome["ingested"])
        self.assertEqual(outcome["state"], ingest.STATE_DONE)
        self.assertEqual(self.upload_mock.call_count, 1)
        uploaded = self.upload_mock.call_args.args[1]
        self.assertIn(marker.encode("utf-8"), uploaded)

        data = ingest.read_result(file_id)
        self.assertTrue(data["edit"]["ingest_after_edit"])
        self.assertEqual(data["review_since"], "")            # 确认门已走完
        self.assertIn(ingest.GATE_CONFIRMED_NOTE, data["detail"])
        self.assertTrue(list(self.knowledge_dir.glob(f"{file_id}_*.md")))   # 确实留档了

    def test_save_and_ingest_still_respects_the_gate(self) -> None:
        """「保存并入库」不能绕过质量门：改坏的正文一律不入库。"""
        file_id = self.run_to_review()

        outcome = ingest.edit_content(file_id, BLOCKED_DOC, ingest_now=True)

        self.assertEqual(outcome["state"], ingest.STATE_BLOCKED)
        self.assertFalse(outcome["ingested"])
        self.assertEqual(self.upload_mock.call_count, 0)

    def test_edit_rejects_empty_oversized_and_unfinished(self) -> None:
        file_id = self.run_to_review()
        original = ingest._read_content(file_id)

        empty = ingest.edit_content(file_id, "   \n  ")
        self.assertFalse(empty["ok"])
        self.assertEqual(empty["http_status"], 400)
        self.assertIn("不能为空", empty["message"])

        with mock.patch.object(ingest, "CONTENT_MAX_CHARS", 20):
            huge = ingest.edit_content(file_id, "字" * 30)
        self.assertFalse(huge["ok"])
        self.assertEqual(huge["http_status"], 413)
        self.assertIn("太长", huge["message"])

        missing = ingest.edit_content("不存在的作业", "随便改点")
        self.assertEqual((missing["ok"], missing["http_status"]), (False, 404))

        # 正在处理中：不给改（等这一步做完）
        ingest._update(file_id, state=ingest.STATE_INGESTING)
        busy = ingest.edit_content(file_id, GOOD_DOC)
        self.assertEqual((busy["ok"], busy["http_status"]), (False, 409))
        self.assertIn("正在提交知识库", busy["message"])

        # 已经入库：改动不会同步到知识库，直说，不假装能改
        ingest._update(file_id, state=ingest.STATE_DONE)
        done = ingest.edit_content(file_id, GOOD_DOC)
        self.assertEqual((done["ok"], done["http_status"]), (False, 409))
        self.assertIn("已经入库", done["message"])

        self.assertEqual(ingest._read_content(file_id), original)   # 被拒绝的都没写盘

    def test_edit_without_changes_is_a_noop(self) -> None:
        file_id = self.run_to_review()

        outcome = ingest.edit_content(file_id, ingest._read_content(file_id))

        self.assertTrue(outcome["ok"])
        self.assertFalse(outcome["edited"])
        self.assertEqual(ingest.read_result(file_id)["edit"]["count"], 0)
        self.assertFalse((ingest.job_dir(file_id) / ingest.BACKUP_NAME).exists())

    def test_edit_on_skipped_job_returns_to_review(self) -> None:
        """「跳过」是可逆的：还能改正文再决定（改完照样重新过质检）。"""
        file_id = self.run_to_review()
        ingest.activate(file_id, ingest.ACTION_SKIP)
        self.wait_for_state(file_id, ingest.STATE_SKIPPED)

        outcome = ingest.edit_content(file_id, GOOD_DOC + "\n跳过之后补的一句说明。\n")

        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(outcome["state"], ingest.STATE_REVIEW)
        self.assertEqual(self.upload_mock.call_count, 0)     # 只回到确认门，没有自己入库

    def test_edit_never_touches_the_source_file(self) -> None:
        """原件（source.*）与上传留档一律不改：改的只有 content.md。"""
        path = fixtures.make_text(self.tmp / "原件.md", GOOD_DOC)
        file_id = ingest.create_job(filename="原件.md", content=path.read_bytes())["file_id"]
        ingest.run_job(file_id)
        source = ingest.source_path(file_id, ".md")
        before = source.read_bytes()

        outcome = ingest.edit_content(file_id, GOOD_DOC + "\n只改正文的一句话。\n")

        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(source.read_bytes(), before)
        self.assertNotEqual(ingest._read_content(file_id), before.decode("utf-8"))


class ConfirmUiContractTests(unittest.TestCase):
    """页面契约：设置页与主页都必须真的接上确认门（防止「后端能跑、页面没入口」）。"""

    def _read(self, name: str) -> str:
        return (BASE_DIR / "static" / name).read_text(encoding="utf-8")

    def test_settings_page_has_ingest_section(self) -> None:
        html = self._read("settings.html")
        for anchor in ('id="sec-ingest"', 'id="inboxState"', 'id="sum-ingest"', 'id="ingest_confirm"'):
            self.assertIn(anchor, html)
        self.assertIn('["ingest","confirm"]', html)
        self.assertIn('["ingest","ttl_hours"]', html)
        self.assertIn('{id:"sec-ingest"', html)

    def test_index_page_wires_review_card(self) -> None:
        html = self._read("index.html")

        for anchor in ("advice", "review", "ingest"):
            self.assertIn(anchor, html)
        for endpoint in ("/ingest/pending", "/ingest/session", "/ingest/action/"):
            self.assertIn(endpoint, html)
        self.assertIn("can.ingest", html)
        self.assertIn("can.skip", html)
        self.assertIn(ingest.STATE_REVIEW, html)
        self.assertIn(ingest.STATE_SKIPPED, html)

    def test_index_page_guards_double_click(self) -> None:
        """Phase 15.1：页面必须防连点、当场给「提交中」反馈，并且知识库原文可展开。"""
        html = self._read("index.html")

        for anchor in (
            "__busy",
            "duplicate_submit",
            "renderUpstreamDetail",
            "查看知识库原始返回",
            "正在提交",
        ):
            self.assertIn(anchor, html)

    def test_get_element_by_id_targets_exist(self) -> None:
        """页面里 getElementById("x") 引用的 id 必须真的存在（纯手写页面最容易犯的错）。

        允许三种动态创建方式：`id="x"`、`.id = "x"`、`setAttribute("id", "x")`；
        侧栏徽章用 `badge.id = "nav-" + item.badge`，所以 `nav-<badge>` 也算已定义
        （badge 名字取自 SECTIONS 里的 `badge: "..."`）。
        """
        for name in ("index.html", "settings.html"):
            html = self._read(name)
            defined = set(re.findall(r'id="([^"]+)"', html))
            defined |= set(re.findall(r"\.id\s*=\s*[\"']([^\"']+)[\"']", html))
            defined |= set(re.findall(r'setAttribute\(\s*"id"\s*,\s*"([^"]+)"', html))
            defined |= {f"nav-{badge}" for badge in re.findall(r'badge:\s*"([^"]+)"', html)}

            missing = sorted(
                {
                    ref
                    for ref in re.findall(r'getElementById\("([^"]+)"\)', html)
                    if ref not in defined
                }
            )
            self.assertEqual(missing, [], f"{name} 里这些 id 找不到对应元素：{missing}")


if __name__ == "__main__":
    unittest.main()




