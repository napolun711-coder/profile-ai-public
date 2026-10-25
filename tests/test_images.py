"""Phase 17：无文字图片的入库路径（写描述 + 起名字 + 打开原图）。

为什么单独一个文件：这条路径上三件事最容易「看着能跑、其实错」，都用可执行的断言钉住：

1. **命名**：文档名 = `<用户输入的名字>_<短ID>.md`（短 ID 来自内容指纹，用户改不了）——
   清洗 / 限长 / 幂等（重复提交同一个名字不会长出 `书桌照片_3fcb99_3fcb99.md`）；
2. **写描述**：描述**追加**进正文，而不是另存一份；重复保存是「替换上一段描述」而不是
   越堆越多；复检按**纯文本**判质 —— 否则短描述会永远卡在 needs_ocr（真机死循环：
   按图片规则「每页 < 100 字」判，用户写多少描述都出不来）；
3. **索引**：`data/gallery/index.json` 必须把「用户起的名字」对得回本机作业
   （引用里双击打开原图、按行删除都靠它），索引丢了能重建，跳过 / 删除要释放名字。

约定：全程离线（上游换成替身、目录换成临时目录），不发请求、不碰真实 data\\。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.clients import lightrag
from app.models.schemas import IngestContent
from app.routers import ingest as ingest_router
from app.services import gallery, ingest, library
from tests import fixtures

PNG_NAME = "书桌照片.jpg"

#: 「像人写的」长描述：行行不同、信息量够，能过质量门
LONG_CAPTION = "\n".join(
    f"第 {index} 条：照片里是书桌、台灯与一摞资料，拍于 2026 年 10 月，已按项目归档。"
    for index in range(1, 21)
)
#: 真实的短描述（几十个字）：按图片规则判质会一直 needs_ocr，按纯文本判才走得下去
SHORT_CAPTION = "书桌照片：2026 年 10 月拍的，桌上有台灯和两摞资料。"


class ImageFixture(unittest.TestCase):
    """把作业目录、留档目录、上游调用全部换到临时环境。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.parsed = self.tmp / "parsed"
        self.knowledge = self.tmp / "knowledge"
        self.parsed.mkdir(parents=True, exist_ok=True)

        self.upload_mock = mock.Mock(return_value=fixtures.FakeResponse())
        self.by_name_mock = mock.Mock(return_value=[])
        self._patchers = [
            # 注意：gallery 的目录是**动态**跟着 ingest.PARSED_DIR 走的（见 gallery.parsed_dir），
            # 所以换掉这里就等于把索引也搬进临时目录，测试碰不到真实 data\\gallery\\。
            mock.patch.object(ingest, "PARSED_DIR", self.parsed),
            mock.patch.object(ingest, "KNOWLEDGE_DIR", self.knowledge),
            mock.patch.object(ingest.lightrag, "upload", self.upload_mock),
            mock.patch.object(lightrag, "documents_by_name", self.by_name_mock),
            *fixtures.patch_settings(),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ---------- 造数据 ----------

    def make_image_job(self, name: str = PNG_NAME, variant: int = 0) -> dict:
        """建一个真机那样的图片作业：route=image、解析不出正文、停在「需要 OCR」。

        `variant` 不同则图片内容（因此 sha256）不同 —— 造「两份不同的图片」时用它，
        默认的同一张图重复上传则用来验证「内容相同不算重名」。
        """
        image = self.tmp / name
        image.write_bytes(fixtures.png_bytes(variant))
        data = ingest.create_job(filename=name, content=image.read_bytes())
        self.assertEqual(data["route"], "image")             # 前提：图片确实走 image 路由
        ingest._update(
            data["file_id"],
            state=ingest.STATE_NEEDS_OCR,
            detail="本地提取不到文字：可用「图片 / 扫描件」入口或点「OCR 解析」",
        )
        return ingest.read_result(data["file_id"]) or {}

    def write_caption(self, file_id: str, text: str, *, doc_name: str = "") -> dict:
        """走一遍用户点「保存描述」的服务端路径。"""
        return ingest.edit_content(file_id, text, caption=True, doc_name=doc_name)

    def ingest_with_name(self, doc_name: str = "书桌照片") -> dict:
        """走完整链路：写描述 → 确认门 → 入库，返回入库后的档案。"""
        data = self.make_image_job()
        file_id = data["file_id"]
        outcome = self.write_caption(file_id, LONG_CAPTION, doc_name=doc_name)
        self.assertEqual(outcome["state"], ingest.STATE_REVIEW, outcome.get("message"))

        final = ingest._stage_ingest(
            file_id, fixtures.settings_with(), forced=False, confirmed=True
        )
        self.assertEqual(final["state"], ingest.STATE_DONE, final.get("detail"))
        return ingest.read_result(file_id) or {}


class DocNameTests(ImageFixture):
    """文档名规则：`<用户输入的名字>_<短ID>.md`。"""

    def test_doc_id_is_short_stable_and_from_content(self) -> None:
        data = self.make_image_job()

        doc_id = ingest.doc_id_of(data)

        self.assertEqual(len(doc_id), ingest.DOC_ID_CHARS)
        self.assertTrue(all(char in "0123456789abcdef" for char in doc_id))
        self.assertEqual(doc_id, ingest.doc_id_of(ingest.read_result(data["file_id"]) or {}))

    def test_user_name_is_cleaned_bounded_and_idempotent(self) -> None:
        data = self.make_image_job()
        doc_id = ingest.doc_id_of(data)

        self.assertEqual(ingest.make_doc_name("书桌照片", data), f"书桌照片_{doc_id}.md")
        self.assertEqual(ingest.make_doc_name("  书桌/照片:v2  ", data), f"书桌_照片_v2_{doc_id}.md")

        bounded = ingest.make_doc_name("长" * 80, data)
        self.assertTrue(bounded.startswith("长" * ingest.DOC_NAME_MAX_CHARS))
        self.assertTrue(bounded.endswith(f"_{doc_id}.md"))

        suggested = ingest.suggested_doc_name(data)
        self.assertEqual(ingest.make_doc_name(suggested, data), suggested)   # 回填完整名不会重复追加
        self.assertEqual(ingest.make_doc_name("   ", data), suggested)       # 空输入退回默认名
        self.assertEqual(suggested, f"{Path(PNG_NAME).stem}_{doc_id}.md")

    def test_only_images_get_the_short_id_suffix(self) -> None:
        """其余资料的文档名**一点没变**（历史文档的映射因此照旧对得上）。"""
        source = fixtures.make_text(self.tmp / "报告 v2.md", "内容充分的一段正文。" * 30)
        data = ingest.create_job(filename="报告 v2.md", content=source.read_bytes())

        self.assertEqual(ingest.doc_name_for(data), "报告_v2.md")
        self.assertFalse(ingest.is_image_job(data))

    def test_doc_name_stem_is_what_the_page_prefills(self) -> None:
        data = self.make_image_job()

        self.assertEqual(ingest.doc_name_stem_of(data), Path(PNG_NAME).stem)

        ingest._update(data["file_id"], doc_name="台灯特写_abcdef.md")
        fresh = ingest.read_result(data["file_id"]) or {}

        self.assertEqual(ingest.doc_name_stem_of(fresh), "台灯特写")
        self.assertEqual(ingest.doc_name_for(fresh), "台灯特写_abcdef.md")


class CaptionTests(ImageFixture):
    """写描述：追加进正文、可反复改、复检按纯文本判质。"""

    def test_image_without_text_is_editable(self) -> None:
        """图片没有正文是正常的 —— 页面要能打开描述框，而不是「请先点重试」。"""
        data = self.make_image_job()

        payload = ingest.content_for_edit(data["file_id"])

        self.assertTrue(payload["editable"])
        self.assertEqual(payload["reason"], "")
        self.assertTrue(payload["is_image"])
        self.assertEqual(payload["doc_name_stem"], Path(PNG_NAME).stem)
        self.assertEqual(payload["content"], "")

    def test_text_document_without_body_is_still_blocked(self) -> None:
        """反向保险：非图片资料没有正文是真失败，不能靠写描述绕过。"""
        source = fixtures.make_text(self.tmp / "空的.md", "内容充分的一段正文。" * 30)
        data = ingest.create_job(filename="空的.md", content=source.read_bytes())
        ingest._update(data["file_id"], state=ingest.STATE_FAILED, detail="解析失败")

        payload = ingest.content_for_edit(data["file_id"])

        self.assertFalse(payload["editable"])
        self.assertIn("重试", payload["reason"])

    def test_caption_is_appended_into_content(self) -> None:
        data = self.make_image_job()

        outcome = self.write_caption(data["file_id"], LONG_CAPTION, doc_name="书桌照片")

        self.assertTrue(outcome["ok"], outcome.get("message"))
        self.assertIn("已保存描述", outcome["message"])
        text = ingest._read_content(data["file_id"])
        self.assertIn(ingest.CAPTION_MARK, text)
        self.assertIn("书桌、台灯", text)

        fresh = ingest.read_result(data["file_id"]) or {}
        self.assertTrue(fresh["manual_text"])
        self.assertEqual(fresh["doc_name"], f"书桌照片_{ingest.doc_id_of(fresh)}.md")
        self.assertEqual(int(fresh["caption"]["chars"]), len(LONG_CAPTION))

    def test_rewriting_caption_replaces_the_old_one(self) -> None:
        data = self.make_image_job()

        self.write_caption(data["file_id"], "第一版描述：桌上有台灯。")
        self.write_caption(data["file_id"], "第二版描述：桌上有台灯和两摞资料。")

        text = ingest._read_content(data["file_id"])
        self.assertEqual(text.count(ingest.CAPTION_MARK), 1)      # 只有一段描述
        self.assertNotIn("第一版描述", text)
        self.assertIn("第二版描述", text)

    def test_empty_caption_is_rejected(self) -> None:
        data = self.make_image_job()

        outcome = ingest.edit_content(data["file_id"], "   ", caption=True)

        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["http_status"], 400)
        self.assertIn("描述不能为空", outcome["message"])

    def test_short_caption_does_not_fall_back_to_needs_ocr(self) -> None:
        """短描述若按图片规则判质，会永远停在 needs_ocr —— 这里必须不是那个状态。"""
        data = self.make_image_job()

        outcome = self.write_caption(data["file_id"], SHORT_CAPTION)

        self.assertTrue(outcome["ok"], outcome.get("message"))
        self.assertNotEqual(outcome["state"], ingest.STATE_NEEDS_OCR)
        self.assertIn(outcome["state"], (ingest.STATE_BLOCKED, ingest.STATE_REVIEW))

    def test_long_caption_reaches_the_confirm_gate(self) -> None:
        data = self.make_image_job()

        outcome = self.write_caption(data["file_id"], LONG_CAPTION, doc_name="书桌照片")

        self.assertEqual(outcome["state"], ingest.STATE_REVIEW)   # 质检通过 → 停在确认门
        self.assertFalse(outcome["ingested"])


class GalleryTests(ImageFixture):
    """名字 ↔ 作业的索引：入库后能对上、丢了能重建、跳过 / 删除会释放。"""

    def test_doc_name_is_registered_and_matches_origin(self) -> None:
        fresh = self.ingest_with_name()
        doc_name = fresh["doc_name"]
        self.assertTrue(doc_name.startswith("书桌照片_"))
        self.assertTrue(doc_name.endswith(".md"))

        entry = gallery.lookup(doc_name)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["file_id"], fresh["file_id"])
        self.assertEqual(entry["sha256"], fresh["sha256"])
        # P1B：入库时同时登记证据 v1（描述、资源路径、指针），索引升到 v2
        self.assertEqual(entry["evidence"]["version"], 1)
        self.assertEqual(entry["evidence"]["kind"], "image")
        self.assertEqual(entry["evidence"]["image_id"], f"img:{fresh['file_id']}:0")
        self.assertEqual(entry["evidence"]["caption"], LONG_CAPTION)
        self.assertEqual(entry["evidence"]["asset_paths"], ["source.jpg"])
        self.assertEqual(gallery.read_index()["version"], 2)

        # 上游文档名 → 本机作业（双击打开原图、按行删除都走这里）
        record = library.match_document(doc_name)
        self.assertEqual(record["file_id"], fresh["file_id"])
        self.assertEqual(library.match_document(doc_name.upper())["file_id"], fresh["file_id"])
        # 留档名也跟着文档名走，否则原件定位会去找一份不存在的 `<file_id>_书桌照片.jpg`
        self.assertEqual(library.archive_name_for(record), f"{fresh['file_id']}_{doc_name}")

    def test_index_rebuilds_from_archives(self) -> None:
        """索引是派生数据：删掉也能从各作业档案扫回来（不许成为单点）。"""
        fresh = self.ingest_with_name()
        gallery.index_path().unlink()
        self.assertEqual(gallery.load(), {})

        self.assertEqual(gallery.owner_of(fresh["doc_name"]), fresh["file_id"])

    def test_skip_releases_the_name(self) -> None:
        data = self.make_image_job()
        file_id = data["file_id"]
        self.write_caption(file_id, LONG_CAPTION, doc_name="书桌照片")
        doc_name = str((ingest.read_result(file_id) or {}).get("doc_name") or "")
        gallery.register(doc_name, file_id)                       # 假设它已经进过知识库
        self.assertEqual(gallery.owner_of(doc_name), file_id)

        ingest._stage_skip(file_id)

        self.assertIsNone(gallery.lookup(doc_name))

    def test_removed_document_releases_the_name(self) -> None:
        fresh = self.ingest_with_name()

        ingest.mark_removed_from_kb(fresh["file_id"])

        self.assertIsNone(gallery.lookup(fresh["doc_name"]))


class NameConflictTests(ImageFixture):
    """重名守卫：不静默传上去第二份同名文档，也不能把正常入库堵死。"""

    def test_conflict_with_another_job_is_reported(self) -> None:
        fresh = self.ingest_with_name()
        other = self.make_image_job("另一张.jpg", variant=1)     # 内容不同 → sha256 不同

        conflict = ingest.doc_name_conflict(fresh["doc_name"], other)

        self.assertIn("已经给了另一份资料", conflict)
        self.assertIn(fresh["file_id"], conflict)

    def test_same_content_is_allowed(self) -> None:
        """同一张图重复上传：内容相同，多一份同名文档不会让人看错 —— 放行。"""
        fresh = self.ingest_with_name()
        clone = dict(fresh)
        clone["file_id"] = "20261016220000-ffffffff"              # 另一个作业，内容一样

        self.assertEqual(ingest.doc_name_conflict(fresh["doc_name"], clone), "")

    def test_own_name_is_allowed(self) -> None:
        """删除后重新入库（「仍要入库」）：名字本来就是它自己的，不能拦自己。"""
        fresh = self.ingest_with_name()

        self.assertEqual(ingest.doc_name_conflict(fresh["doc_name"], fresh), "")

    def test_upstream_match_is_reported(self) -> None:
        """知识库里已经有同名文档（别的工具传的）→ 也要拦，并告诉用户怎么处理。"""
        self.by_name_mock.return_value = [{"id": "doc-abc", "file_path": "别人的资料.md"}]
        data = self.make_image_job()

        conflict = ingest.doc_name_conflict("别人的资料.md", data)

        self.assertIn("知识库里已经有一份", conflict)
        self.assertIn("doc-abc", conflict)

    def test_upstream_failure_does_not_block(self) -> None:
        """查不到就当没有：一次网络故障不能卡死正常入库，也不能谎报「名字被占用」。"""
        self.by_name_mock.side_effect = RuntimeError("知识库连不上")
        data = self.make_image_job()

        self.assertEqual(ingest.doc_name_conflict("随便.md", data), "")

    def test_ingest_stops_with_human_message_and_leaves_no_archive(self) -> None:
        fresh = self.ingest_with_name()
        other = self.make_image_job("第二张.jpg", variant=2)
        file_id = other["file_id"]
        self.write_caption(file_id, LONG_CAPTION, doc_name="书桌照片")
        ingest._update(file_id, doc_name=fresh["doc_name"])       # 撞上第一份的名字

        outcome = ingest._stage_ingest(
            file_id, fixtures.settings_with(), forced=False, confirmed=True
        )

        self.assertEqual(outcome["state"], ingest.STATE_REVIEW)   # 停在原地，等用户改名
        self.assertIn("已经给了另一份资料", outcome["detail"])
        self.assertEqual(self.upload_mock.call_count, 1)          # 只有第一份真的进了知识库
        archived = [path.name for path in self.knowledge.glob("*")]
        self.assertEqual(archived, [f"{fresh['file_id']}_{fresh['doc_name']}"])


BASE_DIR = Path(__file__).resolve().parents[1]


class RouterPassthroughTests(ImageFixture):
    """接口层：`caption` / `doc_name` 真的透到了服务层（页面传了、路由忘了接 = 白传）。"""

    def test_caption_and_doc_name_go_through(self) -> None:
        data = self.make_image_job()
        file_id = data["file_id"]

        payload = ingest_router.ingest_content_save(
            file_id,
            IngestContent(content=LONG_CAPTION, caption=True, doc_name="书桌照片"),
        )

        self.assertTrue(payload["success"], payload.get("message"))
        fresh = ingest.read_result(file_id) or {}
        self.assertEqual(fresh["doc_name"], ingest.make_doc_name("书桌照片", fresh))
        self.assertTrue(fresh["manual_text"])                     # 记下了「这段是我写的」
        self.assertEqual(payload["status"]["doc_name"], fresh["doc_name"])
        self.assertIn(ingest.CAPTION_MARK, ingest._read_content(file_id))
        self.assertEqual(payload["status"]["state"], ingest.STATE_REVIEW)   # 仍在确认门

    def test_empty_caption_is_rejected_in_plain_words(self) -> None:
        from fastapi import HTTPException

        data = self.make_image_job()

        with self.assertRaises(HTTPException) as caught:
            ingest_router.ingest_content_save(
                data["file_id"], IngestContent(content="   ", caption=True, doc_name="书桌照片")
            )

        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("描述不能为空", str(caught.exception.detail))


class PageContractTests(unittest.TestCase):
    """页面契约：图片的「写描述」入口真的接上了（后端能跑、页面没入口 = 功能等于不存在）。"""

    def setUp(self) -> None:
        self.html = (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")

    def test_caption_panel_is_wired(self) -> None:
        for anchor in (
            "✎ 写描述",             # 纯图片 / 追加描述时的入口
            "写描述",               # 入口与面板标题都要能一眼找到
            "保存描述",             # 编辑面板的按钮
            "CAPTION_HINT",         # 面板里说清「描述就是入库内容」
            "captionNoteText",
            "doc_name_stem",        # 名字输入框的回填值（只让用户改名字部分）
            "body.caption = true",  # 提交时告诉后端「这段是描述，不是要覆盖正文」
            "body.doc_name",        # 名字与描述一起提交
            "图片可以直接点",        # 停在 needs_ocr 时告诉用户不用先配 OCR
            # OCR 之后图片作业必须还能改完整正文（否则识别错了只能重新上传）
            "✎ 修改正文",
            "edit-full",
            "edit-caption",
            "OCR_FULL_HINT",
            "captionMode ? (payload.caption_text || \"\") : (payload.content || \"\")",
        ):
            self.assertIn(anchor, self.html)

    def test_user_name_is_visible_on_the_card(self) -> None:
        """用户起的名字要看得见（入库后叫什么）：改了名却看不到结果，等于没改。"""
        self.assertIn("入库为「", self.html)          # 卡片副标题 / 确认面板
        self.assertIn("入库后的文档名", self.html)     # 面板说明里再给一次


if __name__ == "__main__":
    unittest.main()