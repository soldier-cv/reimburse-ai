"""采集草稿：文件落盘、缩略图、AI 识别、批量确认。"""

import asyncio
import hashlib
import io
import json
import os
import re
import zipfile
from datetime import datetime

from sqlalchemy.orm import Session

from ..database import BASE_DIR, SessionLocal
from ..models import Attachment, CaptureDraft, Category, Company, Receivable
from .ai_config import get_ai_config
from .ai_service import DOC_TYPES, extract_document_with_ai, normalize_extraction
from .extractor import extract_pdf_text, filename_hints, has_usable_text, parse_pdf_text
from .money import normalize_date, parse_date, period_from_date, today_str, yuan_to_cents
from .payroll_match import propose_allocation

try:
    from PIL import Image, ImageOps
except Exception:  # pragma: no cover
    Image = None
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


UPLOAD_ROOT = os.environ.get("EXPENSE_UPLOAD_DIR") or os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_ROOT, exist_ok=True)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".heic"}
SUPPORTED_EXTS = IMAGE_EXTS | {".pdf", ".ofd", ".zip"}

MIME_BY_EXT = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".heic": "image/heic",
    ".ofd": "application/ofd",
    ".zip": "application/zip",
}

DOC_TYPE_LABELS = {
    "invoice": "发票",
    "ticket": "客票行程单",
    "receipt": "小票收据",
    "payment": "支付截图",
    "order": "订单截图",
    "bank": "银行回单",
    "salary": "工资条",
    "chat": "聊天记录",
    "other": "凭证",
}

# 草稿入账时附件归类，导出财务包时 invoice 进原件目录，其余进截图目录
ATTACHMENT_KIND_BY_DOC_TYPE = {
    "invoice": "invoice",
    "ticket": "invoice",
    "receipt": "receipt",
    "payment": "screenshot",
    "order": "screenshot",
    "salary": "screenshot",
    "bank": "payment_proof",
    "chat": "chat",
    "other": "other",
}

CATEGORY_KEYWORDS = {
    "差旅住宿": ("酒店", "住宿", "宾馆", "民宿", "差旅", "水单"),
    "城市交通": ("滴滴", "打车", "地铁", "公交", "出租", "网约车", "高铁", "火车", "铁路", "机票", "航空", "通行费", "停车", "加油", "汽油", "柴油"),
    "餐饮招待": ("餐饮", "餐费", "饭店", "餐厅", "外卖", "咖啡", "招待", "美团", "饿了么"),
    "办公采购": ("办公", "文具", "打印", "耗材", "电脑", "数码", "京东", "淘宝"),
    "通讯补贴": ("话费", "通讯", "流量", "宽带"),
    "工资": ("工资", "薪资", "薪酬", "月薪", "代发工资"),
    "奖金": ("奖金", "绩效", "年终奖", "提成"),
}

ZIP_MAX_MEMBERS = 300
ZIP_MAX_MEMBER_BYTES = 25 * 1024 * 1024
ZIP_MAX_TOTAL_BYTES = 300 * 1024 * 1024
LOW_CONFIDENCE = 0.6


def user_dir(user_id: int, sub: str = "drafts") -> str:
    path = os.path.join(UPLOAD_ROOT, f"user_{user_id}", sub)
    os.makedirs(path, exist_ok=True)
    return path


def safe_filename(filename: str) -> str:
    name = os.path.basename(filename or "file")
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip(" .")
    return name[:180] or "file"


def file_ext(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except Exception:
        return ""


def save_upload_bytes(user_id: int, filename: str, data: bytes) -> dict:
    ext = file_ext(filename)
    clean_name = safe_filename(filename)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    stored_name = f"{stamp}_{clean_name}"
    path = os.path.join(user_dir(user_id, "drafts"), stored_name)
    with open(path, "wb") as handle:
        handle.write(data)
    return {
        "original_filename": clean_name,
        "stored_path": path,
        "content_type": MIME_BY_EXT.get(ext, "application/octet-stream"),
        "file_size": len(data),
        "sha256": sha256_bytes(data),
        "ext": ext,
    }


def create_thumbnail(file_path: str, user_id: int, ext: str) -> str:
    """生成缩略图；失败时返回空字符串。"""
    out_dir = user_dir(user_id, "thumbs")
    base = os.path.splitext(os.path.basename(file_path))[0]
    out_path = os.path.join(out_dir, f"{base}.jpg")
    try:
        if ext == ".pdf" and fitz is not None:
            document = fitz.open(file_path)
            page = document.load_page(0)
            pixmap = page.get_pixmap(dpi=80, alpha=False)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            document.close()
        elif ext in IMAGE_EXTS and Image is not None:
            image = Image.open(file_path)
            image = ImageOps.exif_transpose(image)
        else:
            return ""
        image = image.convert("RGB")
        image.thumbnail((360, 360))
        image.save(out_path, format="JPEG", quality=80, optimize=True)
        return out_path
    except Exception:
        return ""


def create_draft(db: Session, user_id: int, saved: dict, company_id=None, category_id=None) -> CaptureDraft:
    draft = CaptureDraft(
        user_id=user_id,
        company_id=company_id,
        category_id=category_id,
        original_filename=saved["original_filename"],
        stored_path=saved["stored_path"],
        thumb_path=create_thumbnail(saved["stored_path"], user_id, saved["ext"]),
        content_type=saved["content_type"],
        file_size=saved["file_size"],
        sha256=saved["sha256"],
        status="pending",
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return draft


def _is_toll_main(filename: str) -> bool:
    name = filename or ""
    return "(票据)" in name or "（票据）" in name or "按票据索引" in name


def _is_toll_aux(filename: str) -> bool:
    name = filename or ""
    return "(行程)" in name or "（行程）" in name or "按行程索引" in name


def _set_extra_files(draft: CaptureDraft, extras: list[dict]) -> None:
    try:
        data = json.loads(draft.extracted_json or "{}")
    except Exception:
        data = {}
    data["extra_files"] = extras
    draft.extracted_json = json.dumps(data, ensure_ascii=False)
    draft.note = "通行费主汇总单，行程明细与原始发票包已自动挂载"


def get_extra_files(draft: CaptureDraft) -> list[dict]:
    try:
        data = json.loads(draft.extracted_json or "{}")
        extras = data.get("extra_files") or []
        return [item for item in extras if isinstance(item, dict) and item.get("stored_path")]
    except Exception:
        return []


def create_drafts_from_zip(db: Session, user_id: int, zip_path: str, company_id=None, category_id=None) -> list[CaptureDraft]:
    """安全解压 ZIP；通行费包自动把行程单与原始发票包挂到主汇总单。"""
    members_data = []
    total_bytes = 0
    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            for member in [m for m in archive.infolist() if not m.is_dir()][:ZIP_MAX_MEMBERS]:
                raw_name = member.filename
                try:
                    raw_name = raw_name.encode("cp437").decode("gbk")
                except Exception:
                    pass
                ext = file_ext(raw_name)
                if ext not in SUPPORTED_EXTS:
                    continue
                # 按声明的解压大小拦截压缩炸弹
                if member.file_size > ZIP_MAX_MEMBER_BYTES:
                    raise ValueError(f"压缩包内文件超过 25MB: {os.path.basename(raw_name)}")
                total_bytes += member.file_size
                if total_bytes > ZIP_MAX_TOTAL_BYTES:
                    raise ValueError("压缩包解压后超过 300MB，请分批上传")
                try:
                    data = archive.read(member)
                except Exception:
                    continue
                members_data.append((raw_name, ext, data))
    except zipfile.BadZipFile as exc:
        raise ValueError("压缩包格式不正确") from exc

    main_index = next((index for index, item in enumerate(members_data) if _is_toll_main(item[0])), None)
    toll_package = main_index is not None
    drafts: list[CaptureDraft] = []
    main_draft: CaptureDraft | None = None
    extras: list[dict] = []

    # 主汇总单优先入库，保证附件能挂到正确的主记录
    if toll_package:
        ordered_indexes = [main_index] + [index for index in range(len(members_data)) if index != main_index]
    else:
        ordered_indexes = list(range(len(members_data)))

    for index in ordered_indexes:
        filename, ext, data = members_data[index]
        if toll_package and index == main_index:
            saved = save_upload_bytes(user_id, filename, data)
            main_draft = create_draft(db, user_id, saved, company_id, category_id)
            drafts.append(main_draft)
            continue
        if toll_package and (_is_toll_aux(filename) or ext == ".zip"):
            saved = save_upload_bytes(user_id, filename, data)
            extras.append(
                {
                    "original_filename": saved["original_filename"],
                    "stored_path": saved["stored_path"],
                    "content_type": saved["content_type"],
                    "file_size": saved["file_size"],
                    "sha256": saved["sha256"],
                    "kind": "other",
                    "note": "通行费行程明细" if _is_toll_aux(filename) else "通行费原始发票包",
                }
            )
            continue
        if ext == ".zip":
            continue
        saved = save_upload_bytes(user_id, filename, data)
        draft = create_draft(db, user_id, saved, company_id, category_id)
        if toll_package is False and _is_toll_aux(filename):
            draft.title = "通行费行程明细（未找到主汇总单）"
            draft.note = "这个文件是行程明细，未检测到主汇总单，请确认是否重复上传"
        drafts.append(draft)

    if main_draft and extras:
        _set_extra_files(main_draft, extras)
        db.commit()
    return drafts


def _guess_category_id(db: Session, user_id: int, data: dict) -> int | None:
    categories = (
        db.query(Category)
        .filter(Category.user_id == user_id, Category.is_active == True)
        .order_by(Category.sort_order.asc(), Category.id.asc())
        .all()
    )
    if not categories:
        return None
    doc_type = (data.get("doc_type") or "").lower()
    text = " ".join(
        str(data.get(key) or "")
        for key in ("title", "item", "merchant", "seller", "doc_subtype", "evidence")
    )
    if doc_type == "salary":
        payroll = [category for category in categories if category.kind == "payroll"]
        for category in payroll:
            for keyword in CATEGORY_KEYWORDS.get(category.name, ()):
                if keyword in text and category.name != "工资":
                    return category.id
        if payroll:
            return payroll[0].id
    for category in categories:
        if category.kind == "payroll":
            continue
        for keyword in CATEGORY_KEYWORDS.get(category.name, ()):
            if keyword in text:
                return category.id
    for category in categories:
        if category.kind == "reimbursement":
            return category.id
    return categories[0].id


def _normalize_tax_id(value) -> str:
    return re.sub(r"[^0-9A-Z]", "", str(value or "").upper())


def _guess_company_id(db: Session, user_id: int, data: dict) -> int | None:
    """先按购买方税号精确匹配，再按抬头/名称包含匹配。商户与销售方是收款人，不参与匹配。"""
    companies = db.query(Company).filter(Company.user_id == user_id, Company.is_active == True).all()
    if not companies:
        return None
    buyer_tax_id = _normalize_tax_id(data.get("buyer_tax_id"))
    if buyer_tax_id:
        for company in companies:
            if _normalize_tax_id(company.tax_id) == buyer_tax_id:
                return company.id

    keys = ["buyer", "payer"]
    if (data.get("doc_type") or "") == "salary":
        keys.append("merchant")
    text = " ".join(str(data.get(key) or "") for key in keys)
    if not text.strip():
        return None
    for company in companies:
        for candidate in (company.invoice_title, company.name, company.short_name):
            if candidate and len(candidate) >= 2 and candidate in text:
                return company.id
    return None


def _count_missing_company(db: Session, user_id: int) -> bool:
    return db.query(Company).filter(Company.user_id == user_id, Company.is_active == True).count() == 0


def _apply_extraction(db: Session, draft: CaptureDraft, data: dict) -> None:
    amount = data.get("amount")
    if (amount is None or amount <= 0) and data.get("entries"):
        total = sum(float(entry.get("amount") or 0) for entry in data["entries"])
        amount = total if total > 0 else None
        data["amount_from"] = "entries"
    if (amount is None or amount <= 0) and data.get("amount_candidates"):
        amount = data["amount_candidates"][0]
        data["amount_from"] = "candidate"

    if amount is not None and amount > 0:
        try:
            draft.amount_cents = yuan_to_cents(amount)
        except Exception:
            draft.amount_cents = None

    occurred = parse_date(data.get("date") or "")
    if occurred:
        draft.occurred_on = occurred.isoformat()
    else:
        data["date_missing"] = True

    doc_type = data.get("doc_type") or "other"
    draft.doc_type = doc_type if doc_type in DOC_TYPES else "other"
    draft.merchant = str(data.get("merchant") or data.get("seller") or data.get("payer") or "")[:150]
    draft.title = str(
        data.get("title")
        or data.get("item")
        or data.get("merchant")
        or DOC_TYPE_LABELS.get(draft.doc_type, "凭证")
    )[:200]
    draft.invoice_code = str(data.get("invoice_code") or "")[:50]
    draft.invoice_no = str(data.get("invoice_no") or "")[:100]
    try:
        existing = json.loads(draft.extracted_json or "{}")
    except Exception:
        existing = {}
    extras = existing.get("extra_files") or []
    if extras:
        data["extra_files"] = extras
    draft.extracted_json = json.dumps(data, ensure_ascii=False)

    if not draft.category_id:
        draft.category_id = _guess_category_id(db, draft.user_id, data)
    if not draft.company_id:
        draft.company_id = _guess_company_id(db, draft.user_id, data)


def _read_extracted(draft: CaptureDraft) -> dict:
    try:
        data = json.loads(draft.extracted_json or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _reusable_result(db: Session, draft: CaptureDraft) -> dict:
    """同一用户上传过同一文件且已有识别结果时直接复用，不再调用 AI。"""
    if not draft.sha256:
        return {}
    previous_drafts = (
        db.query(CaptureDraft)
        .filter(
            CaptureDraft.user_id == draft.user_id,
            CaptureDraft.sha256 == draft.sha256,
            CaptureDraft.id != draft.id,
            CaptureDraft.status.in_(("ready", "confirmed")),
            CaptureDraft.extracted_json.isnot(None),
        )
        .order_by(CaptureDraft.id.desc())
        .limit(5)
        .all()
    )
    for previous in previous_drafts:
        data = _read_extracted(previous)
        if data.get("method") and data.get("amount"):
            data.pop("extra_files", None)
            data["reused_from_draft"] = previous.id
            return data
    return {}


def _prepare(db: Session, draft_id: int, reuse: bool) -> dict | None:
    """同步阶段：占用草稿、读取 PDF 文本层、套模板、查复用。"""
    draft = db.query(CaptureDraft).filter(CaptureDraft.id == draft_id).first()
    if not draft or draft.status in ("confirmed", "discarded", "processing"):
        return None
    draft.status = "processing"
    draft.error = None
    db.commit()

    ext = file_ext(draft.stored_path)
    plan = {"ext": ext, "data": {}, "text": "", "hints": filename_hints(draft.original_filename), "ai": None}

    if reuse:
        reused = _reusable_result(db, draft)
        if reused:
            plan["data"] = reused
            return plan

    if ext == ".pdf":
        text = extract_pdf_text(draft.stored_path)
        parsed = parse_pdf_text(text, draft.original_filename)
        if parsed:
            if not parsed.get("invoice_no") and plan["hints"].get("invoice_no"):
                parsed["invoice_no"] = plan["hints"]["invoice_no"]
            plan["data"] = parsed
            return plan
        if has_usable_text(text):
            plan["text"] = text

    if ext in IMAGE_EXTS or ext == ".pdf":
        ai_config = get_ai_config(db, draft.user_id)
        if ai_config["can_call"]:
            plan["ai"] = {
                "file_path": draft.stored_path,
                "base_url": ai_config["base_url"],
                "api_key": ai_config["api_key"],
                "model_name": ai_config["model_name"],
                "system_prompt": ai_config["system_prompt"],
                "mime_hint": draft.content_type,
            }
    return plan


def _filename_fallback(hints: dict) -> dict:
    if not hints.get("amount"):
        return {}
    return {
        "doc_type": "other",
        "amount": hints["amount"],
        "amount_candidates": [hints["amount"]],
        "invoice_no": hints.get("invoice_no", ""),
        "confidence": {"amount": 0.3},
        "evidence": "文件名",
        "method": "filename",
    }


def _finish(db: Session, draft_id: int, plan: dict, ai_result: dict | None) -> None:
    """同步阶段：落库识别结果。"""
    draft = db.query(CaptureDraft).filter(CaptureDraft.id == draft_id).first()
    if not draft:
        return
    ext = plan["ext"]
    data = plan["data"]
    error = None

    if not data and ai_result is not None:
        if ai_result.get("_error"):
            error = ai_result["_error"]
        elif ai_result:
            data = ai_result
    elif not data and ext in IMAGE_EXTS | {".pdf"}:
        error = "未配置 AI 接口，已保留原图，请手动填写金额"

    if not data or not data.get("amount"):
        fallback = _filename_fallback(plan["hints"])
        if fallback and not data:
            data = fallback
        elif fallback:
            data.setdefault("amount_candidates", [])
            if fallback["amount"] not in data["amount_candidates"]:
                data["amount_candidates"].append(fallback["amount"])

    if not data and ext == ".ofd":
        error = "OFD 文件已保存为原件，请手动填写金额"
    if not data and ext not in IMAGE_EXTS | {".pdf", ".ofd"}:
        error = "该文件类型无法自动识别，请手动填写金额"

    if data:
        _apply_extraction(db, draft, data)
        if error and draft.amount_cents:
            error = None

    if not draft.occurred_on:
        draft.occurred_on = today_str()
    draft.error = error
    draft.status = "ready"
    draft.updated_at = datetime.now()
    db.commit()


def _mark_failed(db: Session, draft_id: int, exc: Exception) -> None:
    db.rollback()
    draft = db.query(CaptureDraft).filter(CaptureDraft.id == draft_id).first()
    if draft:
        draft.status = "failed"
        draft.error = f"识别失败: {exc}"
        db.commit()


async def _process_draft(db: Session, draft_id: int, reuse: bool = True) -> None:
    """识别一个草稿。PDF 解析与数据库读写放到线程里，避免阻塞事件循环。"""
    try:
        plan = await asyncio.to_thread(_prepare, db, draft_id, reuse)
        if plan is None:
            return
        ai_result = None
        if not plan["data"] and plan["ai"]:
            ai_result = await extract_document_with_ai(text=plan["text"], **plan["ai"])
        await asyncio.to_thread(_finish, db, draft_id, plan, ai_result)
    except Exception as exc:
        await asyncio.to_thread(_mark_failed, db, draft_id, exc)


CAPTURE_CONCURRENCY = max(1, int(os.environ.get("CAPTURE_CONCURRENCY", "4") or 4))
_semaphores: dict = {}


def _semaphore() -> asyncio.Semaphore:
    loop_id = id(asyncio.get_running_loop())
    semaphore = _semaphores.get(loop_id)
    if semaphore is None:
        semaphore = asyncio.Semaphore(CAPTURE_CONCURRENCY)
        _semaphores[loop_id] = semaphore
    return semaphore


async def process_draft_async(draft_id: int, reuse: bool = True) -> None:
    """识别一个草稿。独立会话，供后台任务调用。"""
    async with _semaphore():
        db = SessionLocal()
        try:
            await _process_draft(db, draft_id, reuse)
        finally:
            db.close()


async def process_drafts_async(draft_ids: list[int], reuse: bool = True) -> None:
    """并发识别一批草稿，并发数由 CAPTURE_CONCURRENCY 控制（默认 4）。"""
    await asyncio.gather(*(process_draft_async(draft_id, reuse) for draft_id in draft_ids))


def rebuild_draft_from_file(db: Session, draft: CaptureDraft) -> None:
    """用户重试时把状态复位。"""
    draft.status = "pending"
    draft.error = None
    db.commit()


def find_duplicates(db: Session, user_id: int, sha256: str, invoice_no: str, invoice_code: str) -> list[dict]:
    warnings = []
    if invoice_no:
        query = db.query(Attachment).filter(
            Attachment.user_id == user_id,
            Attachment.invoice_no == invoice_no,
            Attachment.deleted_at.is_(None),
        )
        if invoice_code:
            query = query.filter(Attachment.invoice_code == invoice_code)
        existing = query.first()
        if existing:
            warnings.append(
                {
                    "type": "invoice_no",
                    "message": f"发票号 {invoice_no} 已经存在于应收 #{existing.receivable_id}",
                    "receivable_id": existing.receivable_id,
                }
            )
    if sha256:
        existing_attachment = (
            db.query(Attachment)
            .filter(Attachment.user_id == user_id, Attachment.sha256 == sha256, Attachment.deleted_at.is_(None))
            .first()
        )
        existing_draft = (
            db.query(CaptureDraft)
            .filter(
                CaptureDraft.user_id == user_id,
                CaptureDraft.sha256 == sha256,
                CaptureDraft.status == "confirmed",
            )
            .first()
        )
        target_id = existing_attachment.receivable_id if existing_attachment else (
            existing_draft.receivable_id if existing_draft else None
        )
        if target_id:
            warnings.append(
                {
                    "type": "file_hash",
                    "message": f"这个文件已经上传过，关联应收 #{target_id}",
                    "receivable_id": target_id,
                }
            )
    return warnings


def draft_payload(draft: CaptureDraft, db: Session | None = None) -> dict:
    extracted = _read_extracted(draft)
    confidence = extracted.get("confidence") if isinstance(extracted.get("confidence"), dict) else {}
    amount_confidence = confidence.get("amount")
    candidates = []
    for value in extracted.get("amount_candidates") or []:
        try:
            cents = yuan_to_cents(value)
        except Exception:
            continue
        if cents > 0 and cents != draft.amount_cents and cents not in candidates:
            candidates.append(cents)
    is_inflow = extracted.get("direction") == "in"
    payroll_match = None
    if is_inflow and db is not None and draft.company_id and draft.amount_cents:
        payroll_match = propose_allocation(
            db,
            draft.user_id,
            draft.company_id,
            draft.amount_cents,
            draft.occurred_on or "",
            str(extracted.get("remark") or ""),
        )
    hints = []
    if draft.amount_cents and not extracted.get("amount_confirmed") and (
        extracted.get("method") == "filename"
        or extracted.get("amount_from") == "candidate"
        or (isinstance(amount_confidence, (int, float)) and amount_confidence < LOW_CONFIDENCE)
    ):
        hints.append("金额把握不高，请对照原件核对")
    if draft.status == "ready" and extracted and extracted.get("date_missing"):
        hints.append("未识别到日期，已暂填今天")
    return {
        "id": draft.id,
        "company_id": draft.company_id,
        "category_id": draft.category_id,
        "original_filename": draft.original_filename,
        "content_type": draft.content_type,
        "file_size": draft.file_size,
        "sha256": draft.sha256,
        "doc_type": draft.doc_type,
        "doc_subtype": str(extracted.get("doc_subtype") or "")[:40],
        "method": str(extracted.get("method") or ""),
        "evidence": str(extracted.get("evidence") or "")[:120],
        "remark": str(extracted.get("remark") or "")[:120],
        "is_inflow": is_inflow,
        "payroll_match": payroll_match,
        "amount_candidates_cents": candidates[:4],
        "hints": hints,
        "amount": round((draft.amount_cents or 0) / 100, 2) if draft.amount_cents else None,
        "amount_cents": draft.amount_cents,
        "occurred_on": draft.occurred_on or "",
        "title": draft.title or "",
        "merchant": draft.merchant or "",
        "invoice_code": draft.invoice_code or "",
        "invoice_no": draft.invoice_no or "",
        "note": draft.note or "",
        "status": draft.status,
        "error": draft.error or "",
        "receivable_id": draft.receivable_id,
        "has_thumb": bool(draft.thumb_path),
        "extra_file_count": len(get_extra_files(draft)),
        "created_at": draft.created_at.strftime("%Y-%m-%d %H:%M:%S") if draft.created_at else "",
    }
