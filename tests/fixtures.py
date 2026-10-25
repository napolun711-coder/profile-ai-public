"""测试夹具（Phase 8）：在临时目录里现场生成各种格式的样本文件。

说明：
1. 全部离线生成，不依赖任何外部服务；
2. PDF 与 PPTX 用手写最小文件格式（标准库），避免为测试再引依赖；
   PDF 使用标准 Helvetica 字体，因此只含 ASCII 文本（中文样本用 txt/docx/xlsx 覆盖）。
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

from app.core.config import Settings


# ============================================================
# 样本文件
# ============================================================

def make_text(path: Path, text: str, encoding: str = "utf-8") -> Path:
    path.write_bytes(text.encode(encoding))
    return path


def make_pdf(path: Path, pages: List[str]) -> Path:
    """生成合法的最小 PDF；pages 里的空字符串代表"没有文字层"的页面（模拟扫描件）。"""
    objects: List[bytes] = []
    font_number = 3 + 2 * len(pages)
    kids = " ".join(f"{3 + 2 * index} 0 R" for index in range(len(pages)))

    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("ascii"))

    for index, text in enumerate(pages):
        content_number = 4 + 2 * index
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                f"/Resources << /Font << /F1 {font_number} 0 R >> >> "
                f"/Contents {content_number} 0 R >>"
            ).encode("ascii")
        )
        if text:
            escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
        else:
            stream = b"q Q"
        objects.append(
            b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )

    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets: List[int] = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

    xref_position = len(out)
    total = len(objects) + 1
    out += f"xref\n0 {total}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {total} /Root 1 0 R >>\nstartxref\n{xref_position}\n%%EOF\n"
    ).encode("ascii")

    path.write_bytes(bytes(out))
    return path


def make_docx(path: Path, paragraphs: List[str], table: Optional[List[List[str]]] = None) -> Path:
    import docx

    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    if table:
        shape = document.add_table(rows=len(table), cols=len(table[0]))
        for row_index, row in enumerate(table):
            for column_index, value in enumerate(row):
                shape.rows[row_index].cells[column_index].text = value
    document.save(str(path))
    return path


def make_xlsx(path: Path, sheet_name: str, rows: List[List[Any]]) -> Path:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_name
    for row in rows:
        sheet.append(row)
    workbook.save(str(path))
    return path


def make_pptx(path: Path, slides: List[str], images: int = 0) -> Path:
    """手写最小 pptx（幻灯片 XML + 可选的内嵌图片与 rels）。"""
    drawing = "http://schemas.openxmlformats.org/drawingml/2006/main"
    presentation = "http://schemas.openxmlformats.org/presentationml/2006/main"
    relationships = "http://schemas.openxmlformats.org/package/2006/relationships"

    with zipfile.ZipFile(str(path), "w") as archive:
        for index, text in enumerate(slides, start=1):
            xml = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<p:sld xmlns:p="{presentation}" xmlns:a="{drawing}">'
                "<p:cSld><p:spTree><p:sp><p:txBody>"
                f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p>"
                "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
            )
            archive.writestr(f"ppt/slides/slide{index}.xml", xml)

        if images:
            rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', f'<Relationships xmlns="{relationships}">']
            for number in range(1, images + 1):
                archive.writestr(f"ppt/media/image{number}.png", png_bytes())
                rels.append(
                    f'<Relationship Id="rId{number + 1}" '
                    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                    f'Target="../media/image{number}.png"/>'
                )
            rels.append("</Relationships>")
            archive.writestr("ppt/slides/_rels/slide1.xml.rels", "".join(rels))
    return path


def png_bytes(variant: int = 0) -> bytes:
    """内存版 1x1 合法 PNG；variant 不同 → 字节不同（避免 Office 工具去重）。"""
    import struct
    import zlib

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    header = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes([0, variant % 256, 0, 0]))
    return header + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def make_png(path: Path) -> Path:
    """生成一个 1x1 的合法 PNG（用于验证图片一律交给 OCR 处理）。"""
    path.write_bytes(png_bytes())
    return path


def make_docx_with_images(
    path: Path, paragraphs: List[str], images: int = 1
) -> Path:
    """带内嵌图片的 docx（正文若干段 + N 张互不相同的图片）。"""
    import io

    import docx

    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    for index in range(images):
        document.add_picture(io.BytesIO(png_bytes(index + 1)))
    document.save(str(path))
    return path


# ============================================================
# 配置与上游替身
# ============================================================

def settings_with(**overrides: Any) -> Settings:
    """基于默认值生成一份测试用配置（默认：OCR 关闭；质检恒定开启、没有总开关）。

    Phase 20：测试默认**关掉本机朗读、链路只留 cloud** —— 否则测试结果会取决于
    开发机 `models\\tts` 在不在（同一份单测在不同机器上答案不同）。本机朗读的用例
    """
    base = {}
    base.update(overrides)
    return replace(Settings(), **base)


def patch_settings(**overrides: Any):
    """同时替换 services 各模块看到的配置，保证测试不受本机 .env 影响。"""
    settings = settings_with(**overrides)
    return [
        mock.patch("app.services.ingest.get_settings", return_value=settings),
        mock.patch("app.services.quality.get_settings", return_value=settings),
        mock.patch("app.services.ocr.get_settings", return_value=settings),
    ]


class FakeResponse:
    """LightRAG 上传接口的替身响应。"""

    def __init__(self, status_code: int = 200, payload: Optional[Dict[str, Any]] = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {"track_id": "track-test", "status": "ok"}
        self.text = json.dumps(self._payload, ensure_ascii=False)

    def json(self) -> Dict[str, Any]:
        return self._payload
