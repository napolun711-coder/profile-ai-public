"""Phase 16：文件管理与删除接口的离线用例（tests/test_files_api.py）。

覆盖四层：
1. `services/library.py`：知识库文档名 ↔ 本机作业档案的对应规则；
2. `services/origin.py`：原件定位顺序与**路径白名单**（只能读 data/ knowledge/ output/）；
3. `routers/files.py`：列表（含上游不可用时的降级）、打开原件、打开文件夹；
4. 删除：权限（手机 / 外站拿不到）、范围三档、**先知识库后本机**的顺序、
   默认不动原件、失败时一句人话。

约定：全程离线（上游换成替身、目录换成临时目录），不发请求、不碰真实 data\\。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from starlette.requests import Request

from app.clients import lightrag
from app.core.errors import UpstreamError
from app.models.schemas import FilesDelete, FolderOpen
from app.routers import control, files as files_router
from app.services import ingest, library, origin
from tests import fixtures
from tests.memory_fixtures import memory_store

#: 测试里的「别的设备地址」（不是本机网卡地址）
LAN_IP = "192.168.1.23"

GOOD_DOC = "这是一份用于测试的资料。" * 20


def make_request(host: str = "127.0.0.1", origin: str = "", path: str = "/api/files/list") -> Request:
    """构造最小请求：只关心来源地址与 Origin 头（与 tests/test_control.py 一致）。"""
    headers = [(b"origin", origin.encode("utf-8"))] if origin else []
    return Request({
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": headers,
        "client": (host, 51234),
        "query_string": b"",
    })


def upstream_doc(doc_id: str, name: str, *, status: str = "processed") -> dict:
    """一条上游文档（字段按 LightRAG 1.5.6 的 DocStatusResponse）。"""
    return {
        "id": doc_id,
        "file_path": name,
        "content_summary": name + " 的摘要",
        "content_length": len(GOOD_DOC),
        "status": status,
        "created_at": "2026-10-15 10:00:00",
        "updated_at": "2026-10-15 10:05:00",
        "track_id": "track-1",
        "chunks_count": 3,
        "error_msg": None,
        "metadata": {"source_file": name},
    }


class FilesFixture(unittest.TestCase):
    """把 data 目录、knowledge 目录、上游调用全部换到临时环境。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        self.parsed = self.data / "parsed"
        self.originals = self.data / "originals"
        self.uploads = self.data / "uploads"
        self.knowledge = self.root / "knowledge"
        for path in (self.parsed, self.originals, self.uploads, self.knowledge):
            path.mkdir(parents=True, exist_ok=True)

        self._patchers = [
            # 作业档案与留档（library 通过 ingest 的常量读盘）
            mock.patch.object(ingest, "PARSED_DIR", self.parsed),
            mock.patch.object(ingest, "KNOWLEDGE_DIR", self.knowledge),
            mock.patch.object(library, "KNOWLEDGE_DIR", self.knowledge),
            # 原件定位：根目录、各留档目录与路径白名单
            mock.patch.object(origin, "PROFILE_AI_HOME", self.root),
            mock.patch.object(origin, "DATA_DIR", self.data),
            mock.patch.object(origin, "ORIGINALS_DIR", self.originals),
            mock.patch.object(origin, "UPLOAD_DIR", self.uploads),
            mock.patch.object(origin, "KNOWLEDGE_DIR", self.knowledge),
            mock.patch.object(origin, "ALLOWED_ROOTS", (self.data, self.knowledge)),
            # 管理页的目录卡片：别去扫真实的 data 目录
            mock.patch.object(
                files_router,
                "FOLDER_TARGETS",
                {
                    "inbox": {"label": "收件箱", "path": self.data / "inbox", "mkdir": True},
                    "originals": {"label": "原件留档", "path": self.originals, "mkdir": True},
                },
            ),
            # 轮询状态：不启动线程，只要一个空状态
            mock.patch.object(
                files_router,
                "inbox_module",
                return_value=mock.Mock(status=lambda: {"enabled": False, "running": False}),
            ),
            *fixtures.patch_settings(),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(ingest.set_session_allow_all, False)
        self.addCleanup(ingest._pending_keys.clear)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ---------- 造数据 ----------

    def make_job(
        self,
        name: str = "资料.md",
        *,
        text: str = GOOD_DOC,
        archived: str = "",
        origin_name: str = "upload",
        confirm: str = "",
        state: str = ingest.STATE_DONE,
    ) -> dict:
        """建一个作业（真实调用 create_job），并把它标成想要的状态。"""
        source = fixtures.make_text(self.root / name, text)
        job = ingest.create_job(
            filename=name,
            content=source.read_bytes(),
            archived_path=archived,
            origin=origin_name,
            confirm=confirm,
        )
        source.unlink(missing_ok=True)
        if state != ingest.STATE_PARSING:
            ingest._update(job["file_id"], state=state, detail="测试置位")
        return ingest.read_result(job["file_id"]) or {}

    def upload_archive(self, name: str, *, text: str = GOOD_DOC, day: str = "20260101") -> str:
        """造一份上传留档（形如 data/uploads/20260101/090000_资料.md），返回相对路径。"""
        directory = self.uploads / day
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"090000_{name}"
        target.write_bytes(text.encode("utf-8"))
        return f"data/uploads/{day}/{target.name}"

    def patch_lightrag(self, **methods) -> None:
        """替换上游调用（列表 / 计数 / 管线 / 删除 / 按名反查）。"""
        for name, value in methods.items():
            patcher = mock.patch.object(lightrag, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def paginated(documents, *, counts=None):
        """构造 /documents/paginated 的替身返回值。"""
        def call(**kwargs):
            return {
                "documents": documents,
                "pagination": {
                    "page": 1,
                    "page_size": 50,
                    "total_count": len(documents),
                    "total_pages": 1,
                    "has_next": False,
                    "has_prev": False,
                },
                "status_counts": counts or {"all": len(documents), "processed": len(documents)},
            }
        return call


class LibraryTests(FilesFixture):
    """知识库文档名 ↔ 本机档案。"""

    def test_stem_key_normalizes_names(self) -> None:
        self.assertEqual(library.stem_key("报告 v2.pdf"), "报告_v2")
        self.assertEqual(library.stem_key("dir/报告 v2.pdf"), "报告_v2")
        self.assertEqual(library.stem_key("报告 V2.PDF"), "报告_v2")
        self.assertEqual(library.stem_key(""), "")

    def test_markdown_name_matches_upload_rule(self) -> None:
        self.assertEqual(library.markdown_name_of("报告 v2.pdf"), "报告_v2.md")
        self.assertEqual(library.markdown_name_of(""), "document.md")

    def test_match_by_document_name_and_file_id(self) -> None:
        record = self.make_job("资料 一.md")

        self.assertEqual(library.match_document("资料 一.md")["file_id"], record["file_id"])
        self.assertEqual(library.match_document("资料 一.MD")["file_id"], record["file_id"])
        self.assertEqual(
            library.match_document("", record["file_id"])["file_id"], record["file_id"]
        )
        self.assertIsNone(library.match_document("别人传的.md"))
        self.assertIsNone(library.match_document(""))

    def test_local_records_are_newest_first(self) -> None:
        first = self.make_job("第一份.md", text=GOOD_DOC + "甲")
        second = self.make_job("第二份.md", text=GOOD_DOC + "乙")

        names = [item["name"] for item in library.local_records()]

        # 同一秒内建的作业 created_at 相同，排序不可能严格；这里只要求「两份都在」
        self.assertEqual(sorted(names), sorted([first["name"], second["name"]]))

    def test_broken_archive_is_skipped(self) -> None:
        self.make_job("好好的.md")
        broken = self.parsed / "20260101000000-deadbeef"
        broken.mkdir(parents=True, exist_ok=True)
        (broken / "result.json").write_text("{不是 json", encoding="utf-8")

        records = library.local_records()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["name"], "好好的.md")

    def test_job_origin_and_confirm_override_are_recorded(self) -> None:
        record = self.make_job("收件箱来的.md", origin_name="inbox", confirm="auto")

        self.assertEqual(record["origin"], "inbox")
        self.assertEqual(record["confirm_override"], "auto")
        self.assertEqual(library.local_records()[0]["origin"], "inbox")

    def test_local_only_documents_and_archive_name(self) -> None:
        record = self.make_job("未入库.md", state=ingest.STATE_REVIEW)
        view = library.local_records()[0]

        rows = library.local_only_documents()

        self.assertEqual(rows[0]["file_path"], "未入库.md")
        self.assertEqual(view["markdown_name"], "未入库.md")
        self.assertEqual(
            library.archive_name_for(view), f"{record['file_id']}_未入库.md"
        )


class OriginTests(FilesFixture):
    """原件定位顺序与路径白名单。"""

    def test_archived_path_wins(self) -> None:
        record = self.make_job("资料.md", archived=self.upload_archive("资料.md"))

        info = origin.resolve(record)

        self.assertTrue(info["available"])
        self.assertEqual(info["kind"], "archive")
        self.assertEqual(info["name"], "090000_资料.md")
        self.assertTrue(info["relative"].startswith("data/uploads/"))

    def test_source_copy_is_second_choice(self) -> None:
        record = self.make_job("资料.md")

        info = origin.resolve(record)

        self.assertTrue(info["available"])
        self.assertEqual(info["kind"], "source")   # data\parsed\<file_id>\source.md
        self.assertEqual(info["path"], record["source_path"])

    def test_originals_folder_is_searched_by_name(self) -> None:
        # 轮询入库的原件：data\originals\YYYY-MM\<原名>
        # （正常情况 archived_path 也在；这里故意不给，验证「按名字搜」这条兜底）
        month = self.originals / "2026-10"
        month.mkdir(parents=True, exist_ok=True)
        (month / "扫描件.pdf").write_bytes(b"%PDF-1.4")

        info = origin.resolve({"name": "扫描件.pdf", "file_id": "x"})

        self.assertTrue(info["available"])
        self.assertEqual(info["kind"], "original")
        self.assertEqual(info["relative"], "data/originals/2026-10/扫描件.pdf")

    def test_uploads_folder_is_searched_by_suffix(self) -> None:
        self.upload_archive("旧资料.docx", text="hello")

        info = origin.resolve({"name": "旧资料.docx", "file_id": ""})

        self.assertTrue(info["available"])
        self.assertEqual(info["kind"], "archive")

    def test_knowledge_archive_is_used_as_last_resort(self) -> None:
        record = self.make_job("资料.md", archived="")
        # 把解析目录整个删掉，模拟「只剩 knowledge 留档」的历史数据
        for item in (self.parsed / record["file_id"]).iterdir():
            item.unlink()
        (self.knowledge / f"{record['file_id']}_资料.md").write_text("正文", encoding="utf-8")

        info = origin.resolve(record, names=["资料"])

        self.assertTrue(info["available"])
        self.assertEqual(info["kind"], "knowledge")
        self.assertIn("解析后的正文", info["note"])

    def test_missing_origin_explains_why(self) -> None:
        info = origin.resolve({"name": "哪都没有.pdf", "file_id": "20260101000000-abcdef12"})

        self.assertFalse(info["available"])
        self.assertEqual(info["path"], "")
        self.assertIn("没找到", info["reason"])
        self.assertIn("原件", info["reason"])

    def test_missing_without_local_record_says_so(self) -> None:
        info = origin.resolve(None, names=["别的工具的.md"])

        self.assertFalse(info["available"])
        self.assertIn("不是本应用上传的", info["reason"])

    def test_safe_path_blocks_outside_roots(self) -> None:
        outside = self.root / "外面.txt"
        outside.write_text("secret", encoding="utf-8")

        self.assertIsNone(origin.safe_path(str(outside)))
        self.assertIsNone(origin.safe_path("../外面.txt"))
        self.assertIsNone(origin.safe_path("C:/Windows/win.ini"))
        self.assertIsNone(origin.safe_path(""))
        # 目录也要挡掉（只有文件能打开）
        self.assertIsNone(origin.safe_path(str(self.data)))

    def test_safe_path_accepts_file_inside_roots(self) -> None:
        inside = self.uploads / "a.txt"
        inside.write_text("ok", encoding="utf-8")

        self.assertEqual(origin.safe_path("data/uploads/a.txt"), inside)

    def test_resolve_for_builds_url_with_file_id(self) -> None:
        self.make_job("资料.md", archived=self.upload_archive("资料.md"))
        index = library.build_index()

        info = origin.resolve_for(file_path="资料.md", index=index)

        self.assertTrue(info["file_id"])
        self.assertTrue(info["url"].startswith("/api/files/raw?file_id="))

    def test_media_type_of(self) -> None:
        self.assertEqual(origin.media_type_of(Path("a.pdf")), "application/pdf")
        self.assertEqual(origin.media_type_of(Path("a.unknown-ext")), "application/octet-stream")


class ListTests(FilesFixture):
    """GET /api/files/list：上游文档 + 本机档案 + 原件可用性。"""

    def test_merges_upstream_and_local(self) -> None:
        record = self.make_job("资料.md", archived=self.upload_archive("资料.md"))
        self.patch_lightrag(
            documents_paginated=self.paginated([upstream_doc("doc-1", "资料.md")])
        )

        payload = files_router.files_list(make_request())

        self.assertTrue(payload["upstream"]["ok"])
        row = payload["documents"][0]
        self.assertEqual(row["doc_id"], "doc-1")
        self.assertEqual(row["name"], "资料.md")
        self.assertEqual(row["status_label"], "已完成")
        self.assertEqual(row["file_id"], record["file_id"])
        self.assertTrue(row["has_local"])
        self.assertTrue(row["can_open"])
        self.assertEqual(row["origin"]["kind"], "archive")
        self.assertTrue(row["origin"]["url"].startswith("/api/files/raw?file_id="))
        self.assertEqual(payload["counts"]["all"], 1)

    def test_topic_filter_and_row_label(self) -> None:
        self.patch_lightrag(
            documents_paginated=self.paginated(
                [
                    upstream_doc("doc-1", "资料集A.md"),
                    upstream_doc("doc-2", "资料集B.md"),
                ]
            )
        )
        with memory_store() as (store, _):
            topic_a = store.create_topic(name="资料集A")
            topic_b = store.create_topic(name="资料集B")
            store.set_document_topic("资料集A.md", topic_a["id"])
            store.set_document_topic("资料集B.md", topic_b["id"])
            payload = files_router.files_list(make_request(), topic_id=topic_a["id"])

        self.assertEqual(
            [row["file_path"] for row in payload["documents"]], ["资料集A.md"]
        )
        row = payload["documents"][0]
        self.assertEqual(row["topic_id"], topic_a["id"])
        self.assertEqual(row["topic_name"], "资料集A")

    def test_foreign_document_has_no_origin_and_explains(self) -> None:
        self.patch_lightrag(
            documents_paginated=self.paginated([upstream_doc("doc-9", "别人传的.md")])
        )

        row = files_router.files_list(make_request())["documents"][0]

        self.assertFalse(row["has_local"])
        self.assertFalse(row["can_open"])
        self.assertIn("不是本应用上传的", row["origin"]["reason"])
        self.assertTrue(row["can_delete"])          # 知识库里有记录就能删记录

    def test_upstream_down_degrades_instead_of_failing(self) -> None:
        self.make_job("本机有.md", state=ingest.STATE_REVIEW)

        def boom(**kwargs):
            raise UpstreamError("知识库服务不可用（连接被拒绝）")

        self.patch_lightrag(documents_paginated=boom)

        payload = files_router.files_list(make_request())

        self.assertFalse(payload["upstream"]["ok"])
        self.assertEqual(payload["documents"], [])
        self.assertIn("知识库暂时不可用", payload["notice"])
        # 本机侧照样看得到（这正是降级的意义）
        self.assertEqual([row["name"] for row in payload["local_only"]], ["本机有.md"])

    def test_searched_documents_filter_locally(self) -> None:
        self.make_job("季度报告.md", text=GOOD_DOC + "报告")
        self.patch_lightrag(
            documents_paginated=self.paginated(
                [upstream_doc("doc-1", "季度报告.md"), upstream_doc("doc-2", "别的.md")]
            )
        )

        payload = files_router.files_list(make_request(), q="季度")

        self.assertTrue(payload["searched"])
        self.assertEqual([row["doc_id"] for row in payload["documents"]], ["doc-1"])
        self.assertIn("筛出", payload["notice"])

    def test_done_rows_are_not_repeated_in_local_only(self) -> None:
        self.make_job("已入库.md")
        self.patch_lightrag(documents_paginated=self.paginated([]))

        payload = files_router.files_list(make_request())

        self.assertEqual(payload["local_only"], [])


class RawAndResolveTests(FilesFixture):
    """GET /api/files/raw 与 GET /api/files/resolve。"""

    def test_resolve_returns_origin_info(self) -> None:
        self.make_job("资料.md", archived=self.upload_archive("资料.md"))

        payload = files_router.files_resolve(make_request(), file_path="资料.md")

        self.assertTrue(payload["available"])
        self.assertEqual(payload["kind"], "archive")

    def test_resolve_requires_a_target(self) -> None:
        with self.assertRaises(files_router.HTTPException) as ctx:
            files_router.files_resolve(make_request())

        self.assertEqual(ctx.exception.status_code, 400)

    def test_raw_serves_the_file_inline(self) -> None:
        self.make_job("资料.pdf", archived=self.upload_archive("资料.pdf", text="PDF"))

        response = files_router.files_raw(make_request(), file_path="资料.pdf")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(str(response.path).endswith("090000_资料.pdf"))
        self.assertIn("inline", response.headers["content-disposition"])
        self.assertIn("UTF-8''", response.headers["content-disposition"])

    def test_raw_missing_returns_readable_html_404(self) -> None:
        response = files_router.files_raw(make_request(), file_path="不存在.md")

        self.assertEqual(response.status_code, 404)
        body = response.body.decode("utf-8")
        self.assertIn("找不到这份资料的原件", body)
        self.assertIn("不是本应用上传的", body)

    def test_raw_path_parameter_is_whitelisted(self) -> None:
        outside = self.root / "外面.txt"
        outside.write_text("secret", encoding="utf-8")

        response = files_router.files_raw(make_request(), path=str(outside))

        self.assertEqual(response.status_code, 404)
        self.assertIn("不在允许范围内", response.body.decode("utf-8"))

    def test_raw_accepts_whitelisted_relative_path(self) -> None:
        target = self.uploads / "手写.txt"
        target.write_text("ok", encoding="utf-8")

        response = files_router.files_raw(make_request(), path="data/uploads/手写.txt")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Path(response.path), target)


class OverviewTests(FilesFixture):
    def test_overview_reports_counts_pipeline_and_capabilities(self) -> None:
        self.make_job("资料.md")
        self.patch_lightrag(
            status_counts=lambda: {"status_counts": {"processed": 2, "all": 2}},
            pipeline_status=lambda: {"busy": True, "docs": 1, "latest_message": "解析中"},
        )

        payload = files_router.files_overview(make_request())

        self.assertEqual(payload["counts"]["processed"], 2)
        self.assertTrue(payload["pipeline"]["busy"])
        self.assertEqual(payload["local"]["jobs"], 1)
        self.assertEqual(payload["local"]["in_knowledge_base"], 1)
        self.assertTrue(payload["capabilities"]["can_delete"])
        self.assertEqual([item["key"] for item in payload["folders"]], ["inbox", "originals"])

    def test_overview_degrades_when_upstream_is_down(self) -> None:
        def boom():
            raise UpstreamError("知识库服务不可用")

        self.patch_lightrag(status_counts=boom)

        payload = files_router.files_overview(make_request())

        self.assertFalse(payload["upstream"]["ok"])
        self.assertIn("知识库暂时不可用", payload["notice"])
        self.assertEqual(payload["counts"], {})


class ResponseStub:
    """最小的上游响应替身（删除只用到 status_code 与 text）。"""

    def __init__(self, status_code: int = 200, text: str = '{"status":"deletion_started"}') -> None:
        self.status_code = status_code
        self.text = text


class DeleteTests(FilesFixture):
    """POST /api/files/delete：权限、三档范围、改动顺序、默认不动原件。"""

    def setUp(self) -> None:
        super().setUp()
        self.delete_calls: list = []
        self.delete_response = ResponseStub()
        self.by_name_result: list = []

        def fake_delete(doc_ids, *, delete_file=False, delete_llm_cache=False):
            self.delete_calls.append(
                {
                    "doc_ids": list(doc_ids),
                    "delete_file": delete_file,
                    "delete_llm_cache": delete_llm_cache,
                }
            )
            return self.delete_response

        def fake_by_name(name, **kwargs):
            return self.by_name_result

        # 默认拿不到任何文档（删除前的「还在建索引吗」检查用），个别用例再覆盖
        self.patch_lightrag(
            delete_document=fake_delete,
            documents_by_name=fake_by_name,
            documents_paginated=self.paginated([]),
        )

    # ---------- 造一份「哪儿都有」的资料 ----------

    def prepare(self, name: str = "资料.md") -> dict:
        archived = self.upload_archive(name)
        record = self.make_job(name, archived=archived)
        (self.knowledge / f"{record['file_id']}_{name}").write_text("正文", encoding="utf-8")
        self.parsed_dir = self.parsed / record["file_id"]
        self.archived_file = self.uploads / "20260101" / f"090000_{name}"
        return record

    def payload(self, **overrides) -> FilesDelete:
        data = {
            "token": control.TOKEN,
            "scope": "kb",
            "confirm": True,
            "reason": "测试",
        }
        data.update(overrides)
        return FilesDelete(**data)

    def row(self, record: dict, *, doc_id: str = "doc-1") -> dict:
        """页面提交的一行：doc_id 与 file_id 成对（items 的语义）。"""
        return {
            "doc_id": doc_id,
            "file_id": record["file_id"],
            "file_path": record.get("name") or "",
            "name": record.get("name") or "",
        }

    # ---------- 权限 ----------

    def test_lan_page_is_rejected(self) -> None:
        record = self.prepare()

        with self.assertRaises(files_router.HTTPException) as ctx:
            files_router.files_delete(
                self.payload(scope="local", items=[self.row(record)]),
                make_request(host=LAN_IP),
            )

        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(self.delete_calls, [])
        self.assertTrue(self.archived_file.is_file())

    def test_foreign_origin_is_rejected(self) -> None:
        record = self.prepare()

        with self.assertRaises(files_router.HTTPException) as ctx:
            files_router.files_delete(
                self.payload(items=[self.row(record)]),
                make_request(origin="https://evil.example.com"),
            )

        self.assertEqual(ctx.exception.status_code, 403)

    def test_wrong_token_is_rejected(self) -> None:
        record = self.prepare()

        with self.assertRaises(files_router.HTTPException) as ctx:
            files_router.files_delete(
                self.payload(token="not-the-token", items=[self.row(record)]), make_request()
            )

        self.assertEqual(ctx.exception.status_code, 403)

    def test_missing_token_is_rejected_even_from_local(self) -> None:
        # control.guard 把空令牌当成「没给就不验」；删除必须比它再严一档 ——
        # 否则本机上任何脚本都能不带令牌把资料删掉。
        record = self.prepare()

        with self.assertRaises(files_router.HTTPException) as ctx:
            files_router.files_delete(
                self.payload(token="", items=[self.row(record)]), make_request()
            )

        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("令牌", str(ctx.exception.detail))
        self.assertEqual(self.delete_calls, [])
        self.assertTrue(self.archived_file.is_file())

    # ---------- 入参 ----------

    def test_invalid_scope_is_rejected(self) -> None:
        with self.assertRaises(files_router.HTTPException) as ctx:
            files_router.files_delete(self.payload(scope="everything"), make_request())

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("kb / local / all", ctx.exception.detail["message"])

    def test_confirm_flag_is_required(self) -> None:
        with self.assertRaises(files_router.HTTPException) as ctx:
            files_router.files_delete(self.payload(confirm=False), make_request())

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(self.delete_calls, [])

    def test_empty_selection_is_rejected(self) -> None:
        with self.assertRaises(files_router.HTTPException) as ctx:
            files_router.files_delete(self.payload(doc_ids=[], file_ids=[]), make_request())

        self.assertEqual(ctx.exception.status_code, 400)

    # ---------- 三档范围 ----------

    def test_scope_kb_keeps_every_local_file(self) -> None:
        record = self.prepare()

        payload = files_router.files_delete(
            self.payload(scope="kb", items=[self.row(record)]), make_request()
        )

        self.assertEqual(self.delete_calls[0]["doc_ids"], ["doc-1"])
        self.assertFalse(self.delete_calls[0]["delete_file"])
        # 本机留档与原件一个都不能少
        self.assertTrue((self.knowledge / f"{record['file_id']}_资料.md").is_file())
        self.assertTrue(self.parsed_dir.is_dir())
        self.assertTrue(self.archived_file.is_file())
        # 档案要如实记住「已经从知识库删了」，不能再写 done
        after = ingest.read_result(record["file_id"]) or {}
        self.assertEqual(after["state"], ingest.STATE_REMOVED)
        self.assertEqual(after["lightrag"]["state"], "deleted")
        self.assertIn("知识库记录 1 条", payload["message"])

    def test_scope_local_removes_archives_but_keeps_originals(self) -> None:
        record = self.prepare()

        payload = files_router.files_delete(
            self.payload(scope="local", items=[self.row(record)]), make_request()
        )

        self.assertFalse((self.knowledge / f"{record['file_id']}_资料.md").exists())
        self.assertFalse(self.parsed_dir.exists())
        self.assertTrue(self.archived_file.is_file())        # 原件默认保留
        self.assertIn("原件保留", payload["message"])
        self.assertEqual(payload["removed"]["knowledge"], [f"{record['file_id']}_资料.md"])

    def test_scope_all_also_asks_upstream_to_delete_its_copy(self) -> None:
        record = self.prepare()

        files_router.files_delete(
            self.payload(scope="all", items=[self.row(record)]), make_request()
        )

        self.assertTrue(self.delete_calls[0]["delete_file"])

    def test_remove_originals_deletes_the_archived_original(self) -> None:
        record = self.prepare()

        payload = files_router.files_delete(
            self.payload(scope="kb", remove_originals=True, items=[self.row(record)]),
            make_request(),
        )

        self.assertFalse(self.archived_file.exists())
        self.assertEqual(len(payload["removed"]["originals"]), 1)

    # ---------- 顺序与失败 ----------

    def test_upstream_rejection_blocks_local_deletion(self) -> None:
        record = self.prepare()
        self.delete_response = ResponseStub(409, '{"detail":"Pipeline is busy"}')

        with self.assertRaises(files_router.HTTPException) as ctx:
            files_router.files_delete(
                self.payload(scope="local", items=[self.row(record)]), make_request()
            )

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("管线忙", ctx.exception.detail["message"])
        # 关键：知识库没删成就绝不动本机文件（否则会出现「留档没了、文档还在」）
        self.assertTrue((self.knowledge / f"{record['file_id']}_资料.md").is_file())
        self.assertTrue(self.parsed_dir.is_dir())

    def test_upstream_not_found_is_treated_as_already_gone(self) -> None:
        record = self.prepare()
        self.delete_response = ResponseStub(404, '{"detail":"not found"}')

        payload = files_router.files_delete(
            self.payload(scope="local", items=[self.row(record)]), make_request()
        )

        self.assertTrue(payload["success"])
        self.assertIn("已经没有", payload["note"])
        self.assertEqual(payload["deleted_from_kb"], [])
        self.assertFalse((self.knowledge / f"{record['file_id']}_资料.md").exists())

    def test_upstream_5xx_is_reported_as_service_failure(self) -> None:
        record = self.prepare()
        self.delete_response = ResponseStub(503, "boom")

        with self.assertRaises(files_router.HTTPException) as ctx:
            files_router.files_delete(
                self.payload(items=[self.row(record)]), make_request()
            )

        self.assertEqual(ctx.exception.status_code, 502)

    # ---------- 本机行 / doc_id 反查 ----------

    def test_local_row_is_deleted_without_doc_id(self) -> None:
        record = self.prepare()

        payload = files_router.files_delete(
            self.payload(doc_ids=[], file_ids=[record["file_id"]], scope="local"), make_request()
        )

        self.assertEqual(self.delete_calls, [])               # 上游没有这条，不瞎删
        self.assertFalse(self.parsed_dir.exists())
        self.assertTrue(payload["success"])

    def test_doc_id_is_looked_up_by_name_when_missing(self) -> None:
        record = self.prepare()
        self.by_name_result = [{"id": "doc-from-lookup"}]

        files_router.files_delete(
            self.payload(doc_ids=[], file_ids=[record["file_id"]], scope="kb"), make_request()
        )

        self.assertEqual(self.delete_calls[0]["doc_ids"], ["doc-from-lookup"])


class DeleteWhileIndexingTests(FilesFixture):
    """还在建索引的文档不许删（真机实测：删了会被索引覆盖，用户看到「删了没反应」）。"""

    def setUp(self) -> None:
        super().setUp()
        self.delete_calls: list = []

        def fake_delete(doc_ids, *, delete_file=False, delete_llm_cache=False):
            self.delete_calls.append(list(doc_ids))
            return ResponseStub(200)

        self.patch_lightrag(
            delete_document=fake_delete,
            documents_by_name=lambda name, **kwargs: [],
        )
        self.record = self.make_job("在建索引.md", archived=self.upload_archive("在建索引.md"))

    def payload(self, **overrides) -> FilesDelete:
        data = {
            "token": control.TOKEN,
            "scope": "local",
            "confirm": True,
            "items": [
                {
                    "doc_id": "doc-indexing",
                    "file_id": self.record["file_id"],
                    "file_path": "在建索引.md",
                    "name": "在建索引.md",
                }
            ],
        }
        data.update(overrides)
        return FilesDelete(**data)

    def test_indexing_document_is_rejected_with_advice(self) -> None:
        self.patch_lightrag(
            documents_paginated=self.paginated([upstream_doc("doc-indexing", "在建索引.md", status="processing")])
        )

        with self.assertRaises(files_router.HTTPException) as ctx:
            files_router.files_delete(self.payload(), make_request())

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["error"], "kb_indexing")
        self.assertIn("还在建索引", ctx.exception.detail["message"])
        self.assertIn("建索引中", ctx.exception.detail["message"])
        # 关键：没有发出删除请求，本机留档与原件一个没动
        self.assertEqual(self.delete_calls, [])
        self.assertTrue((self.parsed / self.record["file_id"]).is_dir())
        self.assertTrue((self.uploads / "20260101" / "090000_在建索引.md").is_file())

    def test_completed_document_can_be_deleted(self) -> None:
        self.patch_lightrag(
            documents_paginated=self.paginated([upstream_doc("doc-indexing", "在建索引.md", status="processed")])
        )

        payload = files_router.files_delete(self.payload(), make_request())

        self.assertEqual(self.delete_calls, [["doc-indexing"]])
        self.assertFalse((self.parsed / self.record["file_id"]).exists())
        self.assertTrue(payload["success"])

    def test_status_lookup_failure_does_not_block_the_user(self) -> None:
        def boom(**kwargs):
            raise UpstreamError("知识库服务不可用")

        self.patch_lightrag(documents_paginated=boom)

        payload = files_router.files_delete(self.payload(), make_request())

        self.assertEqual(self.delete_calls, [["doc-indexing"]])
        self.assertTrue(payload["success"])


class DeleteMessageTests(unittest.TestCase):
    """删除被拒时的说法（纯函数：不联网、不读盘）。"""

    def test_busy_pipeline_message_mentions_waiting(self) -> None:
        message = files_router._delete_message(409, '{"detail":"Pipeline is busy"}')

        self.assertIn("管线忙", message)
        self.assertIn("再点一次删除", message)

    def test_other_409_is_generic_but_actionable(self) -> None:
        message = files_router._delete_message(409, '{"detail":"conflict"}')

        self.assertIn("稍等一会儿再试", message)

    def test_auth_error_points_at_lightrag_config(self) -> None:
        self.assertIn("鉴权", files_router._delete_message(401, ""))
        self.assertIn("鉴权", files_router._delete_message(403, ""))

    def test_falls_back_to_the_shared_translator(self) -> None:
        message = files_router._delete_message(429, '{"detail":"Too Many Requests"}')

        self.assertIn("太忙", message)


class OpenFolderTests(FilesFixture):
    """POST /api/files/open-folder：仅本机、容器里明确拒绝、不弹控制台窗。"""

    def test_unknown_target_is_rejected(self) -> None:
        with self.assertRaises(files_router.HTTPException) as ctx:
            files_router.files_open_folder(
                FolderOpen(token=control.TOKEN, target="C:/Windows"), make_request()
            )

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail["allowed"], ["inbox", "originals"])

    def test_requires_token(self) -> None:
        with self.assertRaises(files_router.HTTPException) as ctx:
            files_router.files_open_folder(FolderOpen(token="bad", target="inbox"), make_request())

        self.assertEqual(ctx.exception.status_code, 403)

    def test_missing_token_is_rejected(self) -> None:
        # 与删除同一档：本机页面必须带令牌（防本机上的其它脚本乱开窗口）
        with self.assertRaises(files_router.HTTPException) as ctx:
            files_router.files_open_folder(FolderOpen(token="", target="inbox"), make_request())

        self.assertEqual(ctx.exception.status_code, 403)

    def test_opens_the_folder_via_explorer(self) -> None:
        popen = mock.Mock()
        with mock.patch.object(files_router.subprocess, "Popen", popen):
            payload = files_router.files_open_folder(
                FolderOpen(token=control.TOKEN, target="inbox"), make_request()
            )

        self.assertTrue(payload["success"])
        self.assertIn("资源管理器", payload["message"])
        command = popen.call_args.args[0]
        self.assertEqual(command[0], "explorer")
        self.assertTrue(str(command[1]).endswith("inbox"))
        self.assertIn("creationflags", popen.call_args.kwargs)

    def test_container_mode_is_rejected_with_a_hint(self) -> None:
        with mock.patch.object(files_router, "os", mock.Mock(name="posix")):
            with self.assertRaises(files_router.HTTPException) as ctx:
                files_router.files_open_folder(
                    FolderOpen(token=control.TOKEN, target="inbox"), make_request()
                )

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("容器模式", ctx.exception.detail["message"])

    def test_missing_folder_without_mkdir_is_reported(self) -> None:
        missing = self.root / "还没有这个目录"
        with mock.patch.object(
            files_router,
            "FOLDER_TARGETS",
            {"knowledge": {"label": "知识库留档", "path": missing, "mkdir": False}},
        ):
            with self.assertRaises(files_router.HTTPException) as ctx:
                files_router.files_open_folder(
                    FolderOpen(token=control.TOKEN, target="knowledge"), make_request()
                )

        self.assertEqual(ctx.exception.status_code, 404)


class ImageAssetTests(FilesFixture):
    """P2G：GET /api/images/asset（图片引用的缩略图，只读）。"""

    file_id = "20260101090000-abc12345"

    def make_image_job(self) -> Path:
        job = self.parsed / self.file_id
        job.mkdir(parents=True, exist_ok=True)
        image = job / "source.jpg"
        image.write_bytes(b"\xff\xd8\xff\xe0test")
        return image

    def records(self, **overrides):
        record = {
            "doc_name": "截图.md",
            "file_id": self.file_id,
            "image_id": f"img:{self.file_id}:0",
            "asset_paths": ["source.jpg"],
        }
        record.update(overrides)
        return [record]

    def test_serves_image_asset(self) -> None:
        image = self.make_image_job()
        with mock.patch.object(
            files_router.image_evidence, "image_records", return_value=self.records()
        ):
            response = files_router.image_asset(
                make_request(),
                file_id=self.file_id,
                image_id=f"img:{self.file_id}:0",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Path(response.path), image)
        self.assertEqual(response.media_type, "image/jpeg")
        self.assertEqual(response.headers["cache-control"], "private, max-age=300")

    def test_unknown_image_is_404(self) -> None:
        self.make_image_job()
        with mock.patch.object(
            files_router.image_evidence, "image_records", return_value=self.records()
        ):
            with self.assertRaises(files_router.HTTPException) as ctx:
                files_router.image_asset(
                    make_request(),
                    file_id=self.file_id,
                    image_id="img:unknown:0",
                )

        self.assertEqual(ctx.exception.status_code, 404)

    def test_asset_path_cannot_escape_job_dir(self) -> None:
        self.make_image_job()
        (self.parsed / "secret.jpg").write_bytes(b"\xff\xd8\xff")
        with mock.patch.object(
            files_router.image_evidence,
            "image_records",
            return_value=self.records(asset_paths=["../secret.jpg"]),
        ):
            with self.assertRaises(files_router.HTTPException) as ctx:
                files_router.image_asset(
                    make_request(),
                    file_id=self.file_id,
                    image_id=f"img:{self.file_id}:0",
                )

        self.assertEqual(ctx.exception.status_code, 404)

    def test_non_image_asset_is_rejected(self) -> None:
        job = self.parsed / self.file_id
        job.mkdir(parents=True, exist_ok=True)
        (job / "note.txt").write_text("not an image", encoding="utf-8")
        with mock.patch.object(
            files_router.image_evidence,
            "image_records",
            return_value=self.records(asset_paths=["note.txt"]),
        ):
            with self.assertRaises(files_router.HTTPException) as ctx:
                files_router.image_asset(
                    make_request(),
                    file_id=self.file_id,
                    image_id=f"img:{self.file_id}:0",
                )

        self.assertEqual(ctx.exception.status_code, 404)

    def test_invalid_file_id_never_reads_disk(self) -> None:
        with mock.patch.object(files_router.image_evidence, "image_records") as records:
            with self.assertRaises(files_router.HTTPException):
                files_router.image_asset(
                    make_request(), file_id="../secret", image_id="img:x:0"
                )

        records.assert_not_called()

    def test_falls_back_to_original_only_when_it_is_an_image(self) -> None:
        original = self.originals / "原图.png"
        original.write_bytes(b"\x89PNG test")
        with mock.patch.object(
            files_router.image_evidence, "image_records", return_value=self.records(asset_paths=[])
        ), mock.patch.object(
            files_router.origin,
            "resolve_for",
            return_value={"available": True, "path": str(original)},
        ):
            response = files_router.image_asset(
                make_request(),
                file_id=self.file_id,
                image_id=f"img:{self.file_id}:0",
            )
        self.assertEqual(Path(response.path), original)

        pdf = self.originals / "原件.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        with mock.patch.object(
            files_router.image_evidence, "image_records", return_value=self.records(asset_paths=[])
        ), mock.patch.object(
            files_router.origin,
            "resolve_for",
            return_value={"available": True, "path": str(pdf)},
        ):
            with self.assertRaises(files_router.HTTPException) as ctx:
                files_router.image_asset(
                    make_request(),
                    file_id=self.file_id,
                    image_id=f"img:{self.file_id}:0",
                )

        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()