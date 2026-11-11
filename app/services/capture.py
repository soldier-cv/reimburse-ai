"""采集草稿：文件落盘、缩略图、AI 识别、批量确认。"""

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
from .ai_service import extract_document_with_ai, normalize_extraction
from .extractor import extract_standard_invoice, extract_toll_summary
from .money import normalize_date, period_from_date, today_str, yuan_to_cents

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
    "payment": "支付截图",
    "salary": "工资条",
    "chat": "聊天记录",
    "other": "凭证",
}

CATEGORY_KEYWORDS = {
    "差旅住宿": ("酒店", "住宿", "宾馆", "民宿", "差旅"),
    "城市交通": ("滴滴", "打车", "地铁", "公交", "出租", "网约车", "高铁", "火车", "机票", "航空"),
    "餐饮招待": ("餐饮", "餐费", "饭店", "餐厅", "外卖", "咖啡", "招待"),
    "办公采购": ("办公", "文具", "打印", "耗材", "电脑", "数码"),
    "通讯补贴": ("话费", "通讯", "流量", "宽带"),
    "工资": ("工资", "薪资", "薪酬", "月薪", "代发工资"),
    "奖金": ("奖金", "绩效", "年终奖", "提成"),
}


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
    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            for member in [m for m in archive.infolist() if not m.is_dir()][:300]:
                raw_name = member.filename
                try:
                    raw_name = raw_name.encode("cp437").decode("gbk")
                except Exception:
                    pass
                ext = file_ext(raw_name)
                if ext not in SUPPORTED_EXTS:
                    continue
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
        for key in ("title", "item", "merchant", "seller", "evidence")
    )
    if doc_type == "salary":
        for category in categories:
            if category.kind == "payroll" or category.name in ("工资", "奖金"):
                return category.id
    for category in categories:
        for keyword in CATEGORY_KEYWORDS.get(category.name, ()):
            if keyword in text:
                return category.id
    for category in categories:
        if category.kind == "reimbursement":
            return category.id
    return categories[0].id


def _guess_company_id(db: Session, user_id: int, data: dict) -> int | None:
    text = " ".join(str(data.get(key) or "") for key in ("buyer", "merchant", "seller"))
    if not text.strip():
        return None
    companies = db.query(Company).filter(Company.user_id == user_id, Company.is_active == True).all()
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
    if (amount is None or amount <= 0) and data.get("amount_candidates"):
        amount = data["amount_candidates"][0]

    if amount is not None and amount > 0:
        try:
            draft.amount_cents = yuan_to_cents(amount)
        except Exception:
            draft.amount_cents = None

    occurred = normalize_date(data.get("date") or "", fallback="")
    if occurred:
        draft.occurred_on = occurred

    draft.doc_type = data.get("doc_type") or "other"
    draft.merchant = str(data.get("merchant") or data.get("seller") or "")[:150]
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


def _local_extract(file_path: str, filename: str, ext: str) -> dict:
    if ext != ".pdf":
        return {}
    standard = {}
    toll = {}
    try:
        standard = extract_standard_invoice(file_path, filename)
    except Exception:
        standard = {}
    try:
        toll = extract_toll_summary(file_path, filename)
    except Exception:
        toll = {}

    candidates = []
    if standard.get("amount", 0) > 0:
        candidates.append(standard)
    if toll.get("amount", 0) > 0:
        candidates.append(toll)
    if not candidates:
        return {}
    chosen = max(candidates, key=lambda item: float(item.get("amount") or 0))
    return {
        "doc_type": "invoice",
        "amount": float(chosen.get("amount") or 0),
        "amount_candidates": [float(item.get("amount") or 0) for item in candidates],
        "date": chosen.get("invoice_date") or "",
        "merchant": chosen.get("seller") or "",
        "item": chosen.get("item_name") or "",
        "title": chosen.get("item_name") or "发票",
        "invoice_code": chosen.get("invoice_code") or "",
        "invoice_no": chosen.get("invoice_no") or "",
        "buyer": chosen.get("buyer") or "",
        "seller": chosen.get("seller") or "",
        "net_amount": chosen.get("net_amount") or 0,
        "confidence": {"amount": 0.99},
        "evidence": "PDF 文本层本地提取",
        "method": "native_pdf",
    }


async def process_draft_async(draft_id: int) -> None:
    """识别一个草稿。独立会话，供后台任务调用。"""
    db = SessionLocal()
    try:
        await _process_draft(db, draft_id)
    finally:
        db.close()


async def _process_draft(db: Session, draft_id: int) -> None:
    draft = db.query(CaptureDraft).filter(CaptureDraft.id == draft_id).first()
    if not draft or draft.status in ("confirmed", "discarded"):
        return
    if draft.status == "processing":
        return

    draft.status = "processing"
    draft.error = None
    db.commit()

    try:
        ext = file_ext(draft.stored_path)
        data = _local_extract(draft.stored_path, draft.original_filename, ext)

        if (not data or not data.get("amount")) and ext in IMAGE_EXTS | {".pdf"}:
            ai_config = get_ai_config(db, draft.user_id)
            if ai_config["can_call"]:
                ai_result = await extract_document_with_ai(
                    file_path=draft.stored_path,
                    base_url=ai_config["base_url"],
                    api_key=ai_config["api_key"],
                    model_name=ai_config["model_name"],
                    system_prompt=ai_config["system_prompt"],
                    mime_hint=draft.content_type,
                )
                if ai_result.get("_error"):
                    draft.error = ai_result["_error"]
                elif ai_result:
                    data = ai_result
            else:
                draft.error = "未配置 AI 接口，已保留原图，请手动填写金额"

        if not data and ext == ".ofd":
            draft.error = "OFD 文件已保存为原件，请手动填写金额"
        if not data and ext not in IMAGE_EXTS | {".pdf", ".ofd"}:
            draft.error = "该文件类型无法自动识别，请手动填写金额"

        if data:
            _apply_extraction(db, draft, data)
            if draft.error and draft.amount_cents:
                draft.error = None

        if not draft.occurred_on:
            draft.occurred_on = today_str()
        draft.status = "ready"
        draft.updated_at = datetime.now()
        db.commit()
    except Exception as exc:
        db.rollback()
        draft = db.query(CaptureDraft).filter(CaptureDraft.id == draft_id).first()
        if draft:
            draft.status = "failed"
            draft.error = f"识别失败: {exc}"
            db.commit()


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



def extract_buyer(draft: CaptureDraft) -> str:
    try:
        data = json.loads(draft.extracted_json or "{}")
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("buyer") or "").strip()


def _normalize_title(value: str) -> str:
    return "".join((value or "").split())


def company_title_matches(company: Company, buyer: str) -> bool:
    buyer_norm = _normalize_title(buyer)
    if not buyer_norm:
        return True
    for candidate in (company.invoice_title, company.name, company.short_name):
        cand = _normalize_title(candidate or "")
        if len(cand) >= 2 and (cand in buyer_norm or buyer_norm in cand):
            return True
    return False


def find_buyer_mismatch(db: Session, user_id: int, draft: CaptureDraft, company_id: int | None = None) -> list[dict]:
    """发票购买方与公司抬头不一致时给出提示，不自动拦截以外的删除。"""
    if (draft.doc_type or "") != "invoice":
        return []
    buyer = extract_buyer(draft)
    if not buyer:
        return []
    target_id = company_id or draft.company_id
    if not target_id:
        return []
    company = (
        db.query(Company)
        .filter(Company.id == target_id, Company.user_id == user_id)
        .first()
    )
    if not company or company_title_matches(company, buyer):
        return []
    expected = (company.invoice_title or company.name or "").strip()
    return [
        {
            "type": "buyer_mismatch",
            "message": f"发票「{draft.original_filename}」的购买方「{buyer}」与公司抬头「{expected}」不一致",
            "buyer": buyer,
            "invoice_title": expected,
            "draft_id": draft.id,
        }
    ]


def draft_payload(draft: CaptureDraft) -> dict:
    return {
        "id": draft.id,
        "company_id": draft.company_id,
        "category_id": draft.category_id,
        "original_filename": draft.original_filename,
        "content_type": draft.content_type,
        "file_size": draft.file_size,
        "sha256": draft.sha256,
        "doc_type": draft.doc_type,
        "amount": round((draft.amount_cents or 0) / 100, 2) if draft.amount_cents else None,
        "amount_cents": draft.amount_cents,
        "occurred_on": draft.occurred_on or "",
        "title": draft.title or "",
        "merchant": draft.merchant or "",
        "buyer": extract_buyer(draft),
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
