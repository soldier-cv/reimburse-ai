"""导出：Excel 明细、合并票据 PDF、财务报销包 ZIP。"""

import hashlib
import io
import os
import re
import shutil
import tempfile
import zipfile
from datetime import datetime

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from sqlalchemy.orm import Session

from ..models import Attachment, Receivable
from .money import cents_to_yuan, days_overdue, period_from_date, today_str

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except Exception:  # pragma: no cover
    Image = None
    ImageDraw = None
    ImageFont = None
    ImageOps = None

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception:  # pragma: no cover
    pass

try:
    import pymupdf as fitz  # PyMuPDF
except Exception:  # pragma: no cover
    try:
        import fitz
    except Exception:
        fitz = None


A4_WIDTH, A4_HEIGHT = 1240, 1754  # 150 DPI
MARGIN_X, MARGIN_TOP, MARGIN_BOTTOM = 64, 74, 96

FONT_CANDIDATES = [
    os.environ.get("CJK_FONT_PATH", ""),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
]


def _font_path() -> str | None:
    for path in FONT_CANDIDATES:
        if path and os.path.exists(path):
            return path
    return None


_FONT_CACHE = {}


def font(size: int):
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    path = _font_path()
    loaded = None
    if path:
        try:
            loaded = ImageFont.truetype(path, size, index=0)
        except Exception:
            try:
                loaded = ImageFont.truetype(path, size)
            except Exception:
                loaded = None
    if loaded is None:
        loaded = ImageFont.load_default()
    _FONT_CACHE[size] = loaded
    return loaded


def sanitize_filename(name: str, fallback: str = "file") -> str:
    text = os.path.basename(name or fallback)
    text = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", text).strip(" .")
    return text[:150] or fallback


def _receivable_sort_key(receivable: Receivable):
    return (receivable.occurred_on or "", receivable.id)


def _attachments(receivable: Receivable) -> list[Attachment]:
    return sorted(
        [item for item in receivable.attachments if item.deleted_at is None],
        key=lambda item: (item.sort_order or 0, item.id),
    )


def _render_attachment_pages(attachment: Attachment) -> list:
    """把一个附件渲染成若干 PIL 图片页。"""
    if Image is None:
        return []
    path = attachment.stored_path
    if not path or not os.path.exists(path):
        return []
    ext = os.path.splitext(path)[1].lower()
    pages = []
    try:
        if ext == ".pdf" and fitz is not None:
            document = fitz.open(path)
            for index in range(len(document)):
                page = document.load_page(index)
                pixmap = page.get_pixmap(dpi=150, alpha=False)
                pages.append(Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples))
            document.close()
        elif ext in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"):
            image = Image.open(path)
            image = ImageOps.exif_transpose(image).convert("RGB")
            pages.append(image)
        elif ext == ".heic":
            try:
                from pillow_heif import register_heif_opener

                register_heif_opener()
                image = Image.open(path)
                pages.append(image.convert("RGB"))
            except Exception:
                return []
        else:
            return []
    except Exception:
        return []
    return pages


def _fit_page(image, label: str, page_no: int) -> list:
    """把图片等比放到 A4，超长图自动切页。"""
    available_width = A4_WIDTH - MARGIN_X * 2
    available_height = A4_HEIGHT - MARGIN_TOP - MARGIN_BOTTOM
    width, height = image.size
    if width <= 0 or height <= 0:
        return []
    scale = available_width / width
    scaled_height = int(height * scale)
    chunks = []
    offset = 0
    while offset < scaled_height:
        slice_height = min(available_height, scaled_height - offset)
        chunks.append((offset, slice_height))
        offset += slice_height

    result = []
    for chunk_index, (offset, slice_height) in enumerate(chunks, start=1):
        canvas = Image.new("RGB", (A4_WIDTH, A4_HEIGHT), "white")
        draw = ImageDraw.Draw(canvas)
        source_top = int(offset / scale)
        source_bottom = min(height, int((offset + slice_height) / scale))
        crop = image.crop((0, source_top, width, source_bottom))
        target_width = available_width
        target_height = max(1, int(crop.height * scale))
        if target_height > available_height:
            target_height = available_height
            crop = crop.resize((target_width, target_height), Image.LANCZOS)
        else:
            crop = crop.resize((target_width, target_height), Image.LANCZOS)
        canvas.paste(crop, (MARGIN_X, MARGIN_TOP))
        footer = f"{label}  ·  第 {page_no} 页"
        draw.text((MARGIN_X, A4_HEIGHT - 62), footer, font=font(24), fill="#64748b")
        result.append(canvas)
        page_no += 1
    return result


def _cover_image(title: str, lines: list[tuple[str, str]], note: str = "") -> "Image.Image":
    canvas = Image.new("RGB", (A4_WIDTH, A4_HEIGHT), "white")
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, A4_WIDTH, 18), fill="#0f172a")
    draw.text((MARGIN_X, 150), "应 收 报 销 材 料 包", font=font(54), fill="#0f172a")
    draw.text((MARGIN_X, 230), title, font=font(30), fill="#334155")
    draw.line((MARGIN_X, 300, A4_WIDTH - MARGIN_X, 300), fill="#cbd5e1", width=2)

    y = 360
    for label, value in lines:
        draw.text((MARGIN_X, y), label, font=font(26), fill="#94a3b8")
        draw.text((MARGIN_X + 260, y), value, font=font(28), fill="#0f172a")
        y += 58

    if note:
        draw.line((MARGIN_X, y + 20, A4_WIDTH - MARGIN_X, y + 20), fill="#e2e8f0", width=2)
        draw.multiline_text((MARGIN_X, y + 50), note, font=font(22), fill="#64748b", spacing=12)

    draw.text(
        (MARGIN_X, A4_HEIGHT - 120),
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        font=font(20),
        fill="#94a3b8",
    )
    draw.text(
        (MARGIN_X, A4_HEIGHT - 86),
        "本材料包由个人应收台账系统生成，金额与明细已做完整性校验。",
        font=font(20),
        fill="#94a3b8",
    )
    return canvas


def _cover_parts(receivables: list[Receivable]) -> tuple[list[tuple[str, str]], str]:
    total = sum(item.amount_cents or 0 for item in receivables)
    companies = sorted({item.company.name for item in receivables if item.company})
    periods = sorted({item.period for item in receivables if item.period})
    lines = [
        ("公司", "、".join(companies) or "-"),
        ("期间", "、".join(periods) or "-"),
        ("笔数", f"{len(receivables)} 笔"),
        ("合计金额", f"¥ {total / 100:,.2f}"),
    ]
    note_lines = []
    for index, item in enumerate(sorted(receivables, key=_receivable_sort_key)[:18], start=1):
        note_lines.append(
            f"{index:02d}. {(item.occurred_on or item.period or '-')}  {item.title}  ¥{item.amount_cents / 100:,.2f}"
        )
    if len(receivables) > 18:
        note_lines.append(f"... 其余 {len(receivables) - 18} 笔见明细表")
    return lines, "\n".join(note_lines)


def build_cover_pdf(receivables: list[Receivable], title: str = "财务报销材料") -> bytes:
    if Image is None:
        return b""
    lines, note = _cover_parts(receivables)
    cover = _cover_image(title, lines, note)
    output = io.BytesIO()
    cover.save(output, format="PDF", resolution=150)
    return output.getvalue()


def build_merged_pdf(receivables: list[Receivable], db: Session, include_cover: bool = True) -> bytes:
    """把所有附件按清单顺序合并成一个 PDF，每页带序号和页码。"""
    if Image is None:
        return b""
    ordered = sorted(receivables, key=_receivable_sort_key)
    pages = []
    if include_cover:
        lines, note = _cover_parts(ordered)
        pages.append(_cover_image("报销票据合并件", lines, note))

    page_no = len(pages) + 1
    for index, receivable in enumerate(ordered, start=1):
        attachments = _attachments(receivable)
        item_pages = 0
        if not attachments:
            canvas = Image.new("RGB", (A4_WIDTH, A4_HEIGHT), "white")
            draw = ImageDraw.Draw(canvas)
            draw.text((MARGIN_X, 200), f"{index:02d}  {receivable.title}", font=font(34), fill="#0f172a")
            draw.text(
                (MARGIN_X, 280),
                f"金额：¥{receivable.amount_cents / 100:,.2f}",
                font=font(28),
                fill="#334155",
            )
            draw.text(
                (MARGIN_X, 340),
                "该笔应收没有上传附件，请补充发票或截图后再导出。",
                font=font(24),
                fill="#b45309",
            )
            draw.text((MARGIN_X, A4_HEIGHT - 62), f"第 {page_no} 页", font=font(24), fill="#64748b")
            pages.append(canvas)
            page_no += 1
            continue

        for attach_index, attachment in enumerate(attachments, start=1):
            label = f"{index:02d}-{attach_index}  {sanitize_filename(attachment.filename, '附件')}"
            for image in _render_attachment_pages(attachment):
                fitted = _fit_page(image, label, page_no)
                pages.extend(fitted)
                page_no += len(fitted)
                item_pages += len(fitted)

    if not pages:
        return b""
    output = io.BytesIO()
    pages[0].save(output, format="PDF", resolution=150, save_all=True, append_images=pages[1:])
    return output.getvalue()


def build_excel(receivables: list[Receivable], title: str = "应收明细") -> io.BytesIO:
    workbook = openpyxl.Workbook()
    font_title = Font(name="微软雅黑", size=15, bold=True, color="0F172A")
    font_sub = Font(name="微软雅黑", size=10, color="64748B")
    font_header = Font(name="微软雅黑", size=10, bold=True, color="FFFFFF")
    font_cell = Font(name="微软雅黑", size=10, color="0F172A")
    fill_header = PatternFill(start_color="0F172A", end_color="0F172A", fill_type="solid")
    fill_alt = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
    thin = Side(style="thin", color="E2E8F0")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    ordered = sorted(receivables, key=_receivable_sort_key)

    summary = workbook.active
    summary.title = "汇总"
    summary["A1"] = title
    summary["A1"].font = font_title
    summary["A2"] = f"共 {len(ordered)} 笔，合计 ¥{sum(item.amount_cents or 0 for item in ordered) / 100:,.2f}"
    summary["A2"].font = font_sub
    summary["A4"] = "公司"
    summary["B4"] = "笔数"
    summary["C4"] = "金额"
    for cell in ("A4", "B4", "C4"):
        summary[cell].font = font_header
        summary[cell].fill = fill_header
        summary[cell].alignment = Alignment(horizontal="center")
        summary[cell].border = border

    groups = {}
    for item in ordered:
        name = item.company.name if item.company else "-"
        groups.setdefault(name, []).append(item)
    row = 5
    for name, items in sorted(groups.items()):
        summary.cell(row=row, column=1, value=name).font = font_cell
        summary.cell(row=row, column=2, value=len(items)).font = font_cell
        summary.cell(row=row, column=3, value=float(sum(i.amount_cents or 0 for i in items) / 100)).font = font_cell
        summary.cell(row=row, column=3).number_format = '#,##0.00'
        for col in range(1, 4):
            summary.cell(row=row, column=col).border = border
        row += 1
    summary.column_dimensions["A"].width = 28
    summary.column_dimensions["B"].width = 12
    summary.column_dimensions["C"].width = 16

    detail = workbook.create_sheet("明细")
    headers = ["序号", "公司", "归属月", "发生日期", "类别", "事项", "金额", "已收", "待收", "状态", "发票号", "附件数", "备注"]
    detail.append(headers)
    for col in range(1, len(headers) + 1):
        cell = detail.cell(row=1, column=col)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border
    status_labels = {
        "pending": "待提交",
        "submitted": "已提交",
        "partial": "部分到账",
        "paid": "已到账",
        "rejected": "被驳回",
        "abandoned": "已放弃",
    }
    for index, item in enumerate(ordered, start=1):
        attachments = _attachments(item)
        invoice_no = next((a.invoice_no for a in attachments if a.invoice_no), "")
        received = item.received_cents or 0
        values = [
            index,
            item.company.name if item.company else "",
            item.period or "",
            item.occurred_on or "",
            item.category.name if item.category else "",
            item.title,
            float((item.amount_cents or 0) / 100),
            float(received / 100),
            float(max(0, (item.amount_cents or 0) - received) / 100),
            status_labels.get(item.status, item.status),
            invoice_no,
            len(attachments),
            item.note or "",
        ]
        detail.append(values)
        for col in range(1, len(headers) + 1):
            cell = detail.cell(row=index + 1, column=col)
            cell.font = font_cell
            cell.border = border
            if index % 2 == 0:
                cell.fill = fill_alt
        for col in (7, 8, 9):
            detail.cell(row=index + 1, column=col).number_format = '#,##0.00'
            detail.cell(row=index + 1, column=col).alignment = Alignment(horizontal="right")
    widths = [7, 24, 10, 13, 14, 32, 14, 14, 14, 12, 22, 8, 30]
    for index, width in enumerate(widths, start=1):
        detail.column_dimensions[get_column_letter(index)].width = width
    detail.freeze_panes = "A2"

    monthly = workbook.create_sheet("按月度")
    monthly.append(["归属月", "笔数", "金额"])
    for cell in monthly[1]:
        cell.font = font_header
        cell.fill = fill_header
        cell.border = border
    by_period = {}
    for item in ordered:
        by_period.setdefault(item.period or "-", []).append(item)
    for row_index, (period, items) in enumerate(sorted(by_period.items()), start=2):
        monthly.cell(row=row_index, column=1, value=period).font = font_cell
        monthly.cell(row=row_index, column=2, value=len(items)).font = font_cell
        monthly.cell(row=row_index, column=3, value=float(sum(i.amount_cents or 0 for i in items) / 100)).font = font_cell
        monthly.cell(row=row_index, column=3).number_format = '#,##0.00'
        for col in range(1, 4):
            monthly.cell(row=row_index, column=col).border = border
    monthly.column_dimensions["A"].width = 14
    monthly.column_dimensions["B"].width = 12
    monthly.column_dimensions["C"].width = 16

    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)
    return output


def _unique_name(existing: set[str], name: str) -> str:
    base, ext = os.path.splitext(sanitize_filename(name))
    candidate = f"{base}{ext}"
    index = 2
    while candidate.lower() in existing:
        candidate = f"{base}_{index}{ext}"
        index += 1
    existing.add(candidate.lower())
    return candidate


def build_finance_zip(receivables: list[Receivable], db: Session, output_dir: str, base_name: str = "财务报销包") -> dict:
    """生成完整财务包，返回文件信息。"""
    os.makedirs(output_dir, exist_ok=True)
    ordered = sorted(receivables, key=_receivable_sort_key)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_name = f"{sanitize_filename(base_name)}_{timestamp}.zip"
    zip_path = os.path.join(output_dir, zip_name)

    with tempfile.TemporaryDirectory() as temp_dir:
        originals_dir = os.path.join(temp_dir, "03_票据原件")
        screenshots_dir = os.path.join(temp_dir, "04_截图凭证")
        os.makedirs(originals_dir, exist_ok=True)
        os.makedirs(screenshots_dir, exist_ok=True)

        cover_bytes = build_cover_pdf(ordered, "财务报销材料")
        cover_path = os.path.join(temp_dir, "00_封面.pdf")
        with open(cover_path, "wb") as handle:
            handle.write(cover_bytes)

        excel_bytes = build_excel(ordered, "财务报销明细")
        excel_path = os.path.join(temp_dir, "01_报销明细.xlsx")
        with open(excel_path, "wb") as handle:
            handle.write(excel_bytes.getvalue())

        merged_bytes = build_merged_pdf(ordered, db, include_cover=False)
        merged_path = os.path.join(temp_dir, "02_合并票据.pdf")
        with open(merged_path, "wb") as handle:
            handle.write(merged_bytes)

        used_names = set()
        for index, item in enumerate(ordered, start=1):
            for attachment in _attachments(item):
                if not attachment.stored_path or not os.path.exists(attachment.stored_path):
                    continue
                ext = os.path.splitext(attachment.stored_path)[1].lower()
                invoice_no = attachment.invoice_no or ""
                prefix = f"{index:02d}_{item.occurred_on or item.period or '无日期'}_{(item.category.name if item.category else '费用')}_{item.amount_cents / 100:.2f}"
                if invoice_no:
                    prefix += f"_{invoice_no}"
                name = _unique_name(used_names, f"{prefix}_{attachment.filename}")
                target_dir = originals_dir if attachment.kind == "invoice" else screenshots_dir
                shutil.copy2(attachment.stored_path, os.path.join(target_dir, name))

        readme_path = os.path.join(temp_dir, "README.txt")
        with open(readme_path, "w", encoding="utf-8") as handle:
            handle.write(
                "个人应收报销材料包\n"
                f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"笔数：{len(ordered)}\n"
                f"合计：¥{sum(i.amount_cents or 0 for i in ordered) / 100:,.2f}\n\n"
                "目录说明：\n"
                "00_封面.pdf        材料包封面与合计\n"
                "01_报销明细.xlsx   报销明细表\n"
                "02_合并票据.pdf    按明细顺序合并的票据，可直接发送\n"
                "03_票据原件/       发票、PDF 原件\n"
                "04_截图凭证/       支付截图、聊天记录、无票说明\n"
            )

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for root, _dirs, files in os.walk(temp_dir):
                for filename in files:
                    full_path = os.path.join(root, filename)
                    relative = os.path.relpath(full_path, temp_dir)
                    archive.write(full_path, relative)

    file_size = os.path.getsize(zip_path)
    digest = hashlib.sha256()
    with open(zip_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "filename": zip_name,
        "path": zip_path,
        "size": file_size,
        "hash": digest.hexdigest(),
    }


def build_attachments_zip(receivables: list[Receivable], output_dir: str, base_name: str = "原始附件") -> dict:
    """只打包原始附件，按公司和月份分目录。"""
    os.makedirs(output_dir, exist_ok=True)
    ordered = sorted(receivables, key=_receivable_sort_key)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_name = f"{sanitize_filename(base_name)}_{timestamp}.zip"
    zip_path = os.path.join(output_dir, zip_name)
    used_names = set()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, item in enumerate(ordered, start=1):
            company = sanitize_filename(item.company.name if item.company else "未分公司")
            period = item.period or "无归属月"
            for attachment in _attachments(item):
                if not attachment.stored_path or not os.path.exists(attachment.stored_path):
                    continue
                name = _unique_name(used_names, f"{index:02d}_{attachment.filename}")
                archive.write(attachment.stored_path, f"{company}/{period}/{name}")
    digest = hashlib.sha256()
    with open(zip_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "filename": zip_name,
        "path": zip_path,
        "size": os.path.getsize(zip_path),
        "hash": digest.hexdigest(),
    }
