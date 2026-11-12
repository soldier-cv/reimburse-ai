"""本机生成票据预览：PDF 按页渲染、图片校正 EXIF，缓存到用户目录。"""

from __future__ import annotations

import json
import os
from typing import Optional

from .capture import IMAGE_EXTS, file_ext, sha256_file, user_dir

PREVIEW_LONG_EDGE = 1600
PREVIEW_QUALITY = 80

try:
    from PIL import Image, ImageOps
except Exception:  # pragma: no cover
    Image = None
    ImageOps = None

try:
    import pymupdf as fitz
except Exception:  # pragma: no cover
    try:
        import fitz
    except Exception:
        fitz = None


class PreviewUnsupported(Exception):
    def __init__(self, message: str, kind: str = "unsupported"):
        super().__init__(message)
        self.message = message
        self.kind = kind


def _meta_path(preview_dir: str, digest: str) -> str:
    return os.path.join(preview_dir, f"{digest}_meta.json")


def _page_path(preview_dir: str, digest: str, page: int) -> str:
    return os.path.join(preview_dir, f"{digest}_p{page}_{PREVIEW_LONG_EDGE}q{PREVIEW_QUALITY}.jpg")


def _read_meta(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_meta(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    os.replace(tmp, path)


def _resize_long_edge(image: "Image.Image", long_edge: int) -> "Image.Image":
    width, height = image.size
    longest = max(width, height)
    if longest <= long_edge or longest <= 0:
        return image
    scale = long_edge / longest
    return image.resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.Resampling.LANCZOS)


def _save_jpeg(image: "Image.Image", dest: str) -> None:
    image = image.convert("RGB")
    image = _resize_long_edge(image, PREVIEW_LONG_EDGE)
    tmp = dest + ".tmp"
    image.save(tmp, format="JPEG", quality=PREVIEW_QUALITY, optimize=True)
    os.replace(tmp, dest)


def _render_image(source: str, dest: str) -> int:
    if Image is None:
        raise PreviewUnsupported("当前环境无法处理图片预览")
    image = Image.open(source)
    image = ImageOps.exif_transpose(image)
    _save_jpeg(image, dest)
    return 1


def _pdf_page_count(source: str) -> int:
    if fitz is None:
        raise PreviewUnsupported("当前环境未安装 PDF 渲染组件，无法预览")
    document = fitz.open(source)
    try:
        return document.page_count
    finally:
        document.close()


def _render_pdf_page(source: str, dest: str, page: int) -> int:
    if fitz is None:
        raise PreviewUnsupported("当前环境未安装 PDF 渲染组件，无法预览")
    document = fitz.open(source)
    try:
        pages = document.page_count
        if page < 1 or page > pages:
            raise PreviewUnsupported(f"页码超出范围（共 {pages} 页）", kind="range")
        pdf_page = document.load_page(page - 1)
        rect = pdf_page.rect
        longest = max(rect.width, rect.height) or 1
        # 按长边约 1600 像素选择缩放；72 dpi 下 1 点 = 1 像素
        scale = PREVIEW_LONG_EDGE / longest
        matrix = fitz.Matrix(scale, scale)
        pixmap = pdf_page.get_pixmap(matrix=matrix, alpha=False)
        if Image is not None:
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            _save_jpeg(image, dest)
        else:
            pixmap.save(dest)
        return pages
    finally:
        document.close()


def inspect_source(source: str, filename: str = "") -> tuple[str, int]:
    """返回 (kind, page_count)。kind 为 image / pdf / ofd / other。"""
    ext = file_ext(filename or source)
    if ext == ".ofd":
        return "ofd", 0
    if ext == ".pdf":
        return "pdf", _pdf_page_count(source)
    if ext in IMAGE_EXTS:
        return "image", 1
    return "other", 0


def ensure_preview(user_id: int, source: str, page: int = 1, filename: str = "") -> dict:
    """生成或读取缓存预览，返回路径与页数信息。"""
    if not source or not os.path.exists(source):
        raise PreviewUnsupported("原件不存在", kind="missing")
    kind, _ = inspect_source(source, filename)
    if kind == "ofd":
        raise PreviewUnsupported("OFD 暂不支持在线预览，请下载原件查看", kind="ofd")
    if kind == "other":
        raise PreviewUnsupported("该文件类型暂不支持在线预览，请下载原件查看", kind="other")
    if kind == "pdf" and fitz is None:
        raise PreviewUnsupported("当前环境未安装 PDF 渲染组件，无法预览")
    if kind == "image" and Image is None:
        raise PreviewUnsupported("当前环境无法处理图片预览")

    page = int(page or 1)
    if page < 1:
        raise PreviewUnsupported("页码从 1 开始", kind="range")

    digest = sha256_file(source) or f"id{user_id}_{os.path.basename(source)}"
    preview_dir = user_dir(user_id, "previews")
    meta_file = _meta_path(preview_dir, digest)
    meta = _read_meta(meta_file)
    pages = int(meta.get("pages") or 0)
    dest = _page_path(preview_dir, digest, page)

    if kind == "image":
        pages = 1
        if page != 1:
            raise PreviewUnsupported("图片只有 1 页", kind="range")
        if not os.path.exists(dest):
            _render_image(source, dest)
        _write_meta(meta_file, {"pages": 1, "kind": "image"})
        return {"path": dest, "page": 1, "pages": 1, "kind": "image", "media_type": "image/jpeg"}

    if pages <= 0:
        pages = _pdf_page_count(source)
        _write_meta(meta_file, {"pages": pages, "kind": "pdf"})
    if page > pages:
        raise PreviewUnsupported(f"页码超出范围（共 {pages} 页）", kind="range")
    if not os.path.exists(dest):
        pages = _render_pdf_page(source, dest, page)
        _write_meta(meta_file, {"pages": pages, "kind": "pdf"})
    return {"path": dest, "page": page, "pages": pages, "kind": "pdf", "media_type": "image/jpeg"}
