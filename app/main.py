import asyncio
import calendar
import io
import json
import os
import shutil
from datetime import date, datetime, timedelta
from typing import List, Optional

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import exists, func, or_
from sqlalchemy.orm import Session

from .database import BASE_DIR, Base, engine, get_db
from .models import (
    AgentToken,
    Attachment,
    AuditLog,
    Batch,
    CaptureDraft,
    Category,
    Company,
    ExportRecord,
    Payment,
    PaymentItem,
    PayrollPlan,
    Receivable,
    SystemSetting,
    User,
    UserSetting,
    materials_status,
)
from .schemas import (
    AdminCreateUserRequest,
    AdminResetPasswordRequest,
    AgentTokenCreate,
    AgentTokenUpdate,
    AIModelFetchRequest,
    AISettingRequest,
    BatchCreate,
    BatchItemsRequest,
    BatchPaymentRequest,
    BatchSubmit,
    BatchUnsubmitRequest,
    BatchUpdate,
    CaptureConfirm,
    CapturePaymentApply,
    CategoryCreate,
    CategoryUpdate,
    CompanyCreate,
    CompanyUpdate,
    DraftUpdate,
    FinancePackRequest,
    InitAdminRequest,
    LoginRequest,
    PasswordChangeRequest,
    PaymentCreate,
    PaymentAllocation,
    PayrollPlanCreate,
    PayrollPlanUpdate,
    ReceivableCreate,
    ReceivableUpdate,
    RegisterRequest,
)
from .services.ai_config import get_ai_config, get_setting, is_default_prompt, set_user_setting
from .services.ai_service import (
    DEFAULT_SYSTEM_PROMPT,
    DOC_TYPES,
    close_http_clients,
    fetch_available_models,
    test_ai_connection,
)
from .services.audit import record_audit
from .services.auth import (
    create_access_token,
    get_current_user,
    hash_password,
    init_default_admin,
    rehash_user_password_if_needed,
    verify_password_and_check_rehash,
)
from .services.auth_guard import check_auth_banned, clear_auth_failures, record_auth_failure
from .services.capture import (
    ATTACHMENT_KIND_BY_DOC_TYPE,
    IMAGE_EXTS,
    UPLOAD_ROOT,
    _process_draft,
    create_draft,
    create_drafts_from_zip,
    draft_payload,
    file_ext,
    find_buyer_mismatch,
    find_duplicates,
    get_extra_files,
    process_drafts_async,
    rebuild_draft_from_file,
    remove_user_uploads,
    safe_filename,
    save_upload_bytes,
    user_dir,
    create_thumbnail,
)
from .services.download_tokens import make_download_token, verify_download_token
from .services.exporter import (
    build_attachments_zip,
    build_excel,
    build_finance_zip,
    build_merged_pdf,
)
from .services.agent_tokens import (
    MAX_TOKENS_PER_USER,
    generate_plaintext_token,
    display_prefix,
    hash_token,
    parse_expires_at,
    parse_scopes,
    reject_if_agent_bearer,
    serialize_token,
    store_scopes,
)
from .services.migrations import ensure_auth_schema, ensure_agent_token_schema, ensure_receivable_schema, ensure_v33_schema
from .services.payroll_match import propose_allocation
from .services.payroll_plans import (
    assert_no_overlap,
    clamp_payday,
    missing_summary,
    parse_plan_dates,
    parse_salary,
    serialize_plan,
    sync_user_plans,
    _payroll_category,
)
from .services.preview import PreviewUnsupported, ensure_preview, inspect_source
from .services.ledger import (
    COLLECTABLE_STATUSES,
    LOCKED_MESSAGE,
    assert_batch_adjustable,
    assert_can_delete,
    assert_same_period,
    assert_status_change,
    assert_unlocked_for_edit,
    batch_has_payments,
    collectable,
    collectable_reason,
    has_payments,
    is_locked,
    receivable_period,
    reset_receivable_to_pending,
    resolve_item_period,
)
from .services.money import (
    add_days,
    cents_to_text,
    cents_to_yuan,
    current_month,
    days_overdue,
    month_range,
    normalize_date,
    normalize_period,
    parse_date,
    period_from_date,
    today_local,
    today_str,
    yuan_to_cents,
)

Base.metadata.create_all(bind=engine)

_ENABLE_DOCS = os.environ.get("ENABLE_API_DOCS", "").strip().lower() in ("1", "true", "yes")
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(
    title="个人应收与报销管理台",
    version="3.2.0",
    docs_url="/docs" if _ENABLE_DOCS else None,
    redoc_url="/redoc" if _ENABLE_DOCS else None,
    openapi_url="/openapi.json" if _ENABLE_DOCS else None,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
_cors_origins = [item.strip() for item in os.environ.get("CORS_ORIGINS", "").split(",") if item.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

os.makedirs(UPLOAD_ROOT, exist_ok=True)

DEFAULT_CATEGORIES = [
    ("差旅住宿", "reimbursement", True, 10),
    ("城市交通", "reimbursement", True, 20),
    ("餐饮招待", "reimbursement", True, 30),
    ("办公采购", "reimbursement", True, 40),
    ("通讯补贴", "reimbursement", False, 50),
    ("工资", "payroll", False, 60),
    ("奖金", "payroll", False, 70),
    ("其他", "other", False, 90),
]

STATUS_LABELS = {
    "pending": "待提交",
    "submitted": "已提交",
    "partial": "部分到账",
    "paid": "已到账",
    "rejected": "被驳回",
    "abandoned": "已放弃",
}

KIND_LABELS = {"reimbursement": "报销", "payroll": "薪资", "other": "其他"}


def seed_user_defaults(db: Session, user_id: int) -> None:
    """为新用户初始化默认应收类别。"""
    if db.query(Category).filter(Category.user_id == user_id).count() > 0:
        return
    for name, kind, requires_invoice, sort_order in DEFAULT_CATEGORIES:
        db.add(
            Category(
                user_id=user_id,
                name=name,
                kind=kind,
                requires_invoice=requires_invoice,
                sort_order=sort_order,
            )
        )
    db.commit()


def _company_or_404(db: Session, user: User, company_id: int) -> Company:
    company = (
        db.query(Company)
        .filter(Company.id == company_id, Company.user_id == user.id)
        .first()
    )
    if not company:
        raise HTTPException(status_code=404, detail="公司不存在")
    return company


def _category_or_404(db: Session, user: User, category_id: int) -> Category:
    category = (
        db.query(Category)
        .filter(Category.id == category_id, Category.user_id == user.id)
        .first()
    )
    if not category:
        raise HTTPException(status_code=404, detail="类别不存在")
    return category


def _receivable_or_404(db: Session, user: User, receivable_id: int, include_deleted: bool = False) -> Receivable:
    query = db.query(Receivable).filter(Receivable.id == receivable_id, Receivable.user_id == user.id)
    if not include_deleted:
        query = query.filter(Receivable.deleted_at.is_(None))
    receivable = query.first()
    if not receivable:
        raise HTTPException(status_code=404, detail="应收事项不存在")
    return receivable


def _batch_or_404(db: Session, user: User, batch_id: int) -> Batch:
    batch = db.query(Batch).filter(Batch.id == batch_id, Batch.user_id == user.id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    return batch


def _next_pay_date(company: Company, submitted_on: str) -> str:
    submitted = parse_date(submitted_on) or today_local()
    payday = int(company.payday or 0)
    if 1 <= payday <= 31:
        def make_date(year: int, month: int) -> date:
            last_day = calendar.monthrange(year, month)[1]
            return date(year, month, min(payday, last_day))

        candidate = make_date(submitted.year, submitted.month)
        if candidate < submitted:
            if submitted.month == 12:
                candidate = make_date(submitted.year + 1, 1)
            else:
                candidate = make_date(submitted.year, submitted.month + 1)
        return candidate.isoformat()
    return add_days(submitted_on, int(company.payment_days or 30))


def _serialize_company(company: Company, stats: dict | None = None) -> dict:
    data = {
        "id": company.id,
        "name": company.name,
        "short_name": company.short_name or "",
        "invoice_title": company.invoice_title or "",
        "tax_id": company.tax_id or "",
        "payday": company.payday or 0,
        "payment_days": company.payment_days or 30,
        "color": company.color or "#2563eb",
        "note": company.note or "",
        "is_active": bool(company.is_active),
    }
    if stats:
        data.update(stats)
    return data


def _serialize_category(category: Category) -> dict:
    return {
        "id": category.id,
        "name": category.name,
        "kind": category.kind,
        "requires_invoice": bool(category.requires_invoice),
        "sort_order": category.sort_order or 0,
        "is_active": bool(category.is_active),
    }


def _serialize_attachment(attachment: Attachment) -> dict:
    return {
        "id": attachment.id,
        "receivable_id": attachment.receivable_id,
        "kind": attachment.kind,
        "filename": attachment.filename,
        "content_type": attachment.content_type or "",
        "file_size": attachment.file_size or 0,
        "invoice_code": attachment.invoice_code or "",
        "invoice_no": attachment.invoice_no or "",
        "amount": cents_to_yuan(attachment.amount_cents) if attachment.amount_cents else None,
        "note": attachment.note or "",
        "created_at": attachment.created_at.strftime("%Y-%m-%d %H:%M:%S") if attachment.created_at else "",
    }


def _serialize_receivable(receivable: Receivable, detail: bool = False) -> dict:
    attachments = [a for a in receivable.attachments if a.deleted_at is None]
    invoices = [a for a in attachments if a.kind == "invoice"]
    materials = materials_status(receivable)
    data = {
        "id": receivable.id,
        "company_id": receivable.company_id,
        "company_name": receivable.company.name if receivable.company else "",
        "company_color": receivable.company.color if receivable.company else "#2563eb",
        "category_id": receivable.category_id,
        "category_name": receivable.category.name if receivable.category else "",
        "batch_id": receivable.batch_id,
        "batch_title": receivable.batch.title if receivable.batch else "",
        "kind": receivable.kind,
        "kind_label": KIND_LABELS.get(receivable.kind, receivable.kind),
        "title": receivable.title,
        "period": receivable.period or "",
        "occurred_on": receivable.occurred_on or "",
        "amount": cents_to_yuan(receivable.amount_cents),
        "amount_cents": receivable.amount_cents or 0,
        "received_cents": receivable.received_cents or 0,
        "received": cents_to_yuan(receivable.received_cents or 0),
        "remaining_cents": max(0, (receivable.amount_cents or 0) - (receivable.received_cents or 0)),
        "remaining": cents_to_yuan(max(0, (receivable.amount_cents or 0) - (receivable.received_cents or 0))),
        "status": receivable.status,
        "status_label": STATUS_LABELS.get(receivable.status, receivable.status),
        "expected_on": receivable.expected_on or "",
        "submitted_on": receivable.submitted_on or "",
        "paid_on": receivable.paid_on or "",
        "note": receivable.note or "",
        "source": receivable.source,
        "attachment_count": len(attachments),
        "invoice_no": invoices[0].invoice_no if invoices else "",
        "requires_invoice": materials["requires_invoice"],
        "has_invoice": materials["has_invoice"],
        "missing_invoice": materials["missing_invoice"],
        "materials_ok": materials["materials_ok"],
        "days_overdue": days_overdue(receivable.expected_on) if receivable.status in ("submitted", "partial") else 0,
        "created_at": receivable.created_at.strftime("%Y-%m-%d %H:%M:%S") if receivable.created_at else "",
        "created_on": receivable.created_at.strftime("%Y-%m-%d") if receivable.created_at else "",
        "can_collect": collectable(receivable),
        "collect_reason": collectable_reason(receivable),
        "locked": is_locked(receivable),
    }
    if detail:
        data["attachments"] = [_serialize_attachment(a) for a in attachments]
        data["payments"] = [
            {
                "id": item.payment.id,
                "amount": cents_to_yuan(item.amount_cents or 0),
                "payment_amount": cents_to_yuan(item.payment.amount_cents),
                "paid_on": item.payment.paid_on or "",
                "note": item.payment.note or "",
            }
            for item in receivable.payment_items
            if item.payment and item.payment.deleted_at is None
        ]
    return data


def _receivable_query(db: Session, user: User):
    return db.query(Receivable).filter(
        Receivable.user_id == user.id,
        Receivable.deleted_at.is_(None),
    )


def _filter_receivables(
    db: Session,
    user: User,
    company_id: Optional[int] = None,
    status_value: Optional[str] = None,
    kind: Optional[str] = None,
    category_id: Optional[int] = None,
    period_from: Optional[str] = None,
    period_to: Optional[str] = None,
    query_text: Optional[str] = None,
    only_overdue: bool = False,
    only_missing_materials: bool = False,
):
    query = _receivable_query(db, user)
    if company_id:
        query = query.filter(Receivable.company_id == company_id)
    if status_value:
        query = query.filter(Receivable.status == status_value)
    if kind:
        query = query.filter(Receivable.kind == kind)
    if category_id:
        query = query.filter(Receivable.category_id == category_id)
    if period_from:
        query = query.filter(Receivable.period >= period_from)
    if period_to:
        query = query.filter(Receivable.period <= period_to)
    if query_text:
        like = f"%{query_text.strip()}%"
        query = query.filter(
            or_(
                Receivable.title.ilike(like),
                Receivable.note.ilike(like),
            )
        )
    if only_overdue:
        today = today_str()
        query = query.filter(
            Receivable.status.in_(("submitted", "partial")),
            Receivable.expected_on != "",
            Receivable.expected_on < today,
        )
    if only_missing_materials:
        query = query.filter(
            Receivable.status.in_(("pending", "submitted", "partial")),
            exists().where(
                Category.id == Receivable.category_id,
                Category.requires_invoice == True,  # noqa: E712
            ),
            ~exists().where(
                Attachment.receivable_id == Receivable.id,
                Attachment.deleted_at.is_(None),
                Attachment.kind == "invoice",
            ),
        )
    return query


def _current_month() -> str:
    return current_month()


def _resolve_export_receivables(
    db: Session,
    user: User,
    ids: list[int],
    batch_id: int | None,
    period: str | None = None,
    period_from: str | None = None,
    period_to: str | None = None,
    company_id: int | None = None,
) -> list[Receivable]:
    if batch_id:
        batch = _batch_or_404(db, user, batch_id)
        return [item for item in batch.receivables if item.deleted_at is None]
    if ids:
        return (
            db.query(Receivable)
            .filter(
                Receivable.user_id == user.id,
                Receivable.id.in_(ids),
                Receivable.deleted_at.is_(None),
            )
            .order_by(Receivable.occurred_on.asc(), Receivable.id.asc())
            .all()
        )
    start = normalize_period(period) or normalize_period(period_from or "")
    end = normalize_period(period) or normalize_period(period_to or "")
    if not start and not end and not company_id:
        return []
    query = _filter_receivables(
        db,
        user,
        company_id=company_id,
        period_from=start or None,
        period_to=end or None,
    )
    return query.order_by(Receivable.occurred_on.asc(), Receivable.id.asc()).all()


@app.on_event("startup")
async def startup_event():
    ensure_auth_schema(engine)
    ensure_receivable_schema(engine)
    ensure_agent_token_schema(engine)
    ensure_v33_schema(engine)
    db = next(get_db())
    try:
        init_default_admin(db)
        admin = db.query(User).filter(User.role == "admin").first()
        if admin:
            seed_user_defaults(db, admin.id)
        # 重启后把卡在识别中、排队中的草稿重新排队
        db.query(CaptureDraft).filter(CaptureDraft.status == "processing").update({"status": "pending"})
        db.commit()
        pending_ids = [row.id for row in db.query(CaptureDraft.id).filter(CaptureDraft.status == "pending").all()]
    finally:
        db.close()
    if pending_ids:
        app.state.capture_requeue = asyncio.create_task(process_drafts_async(pending_ids))


@app.on_event("shutdown")
async def shutdown_event():
    await close_http_clients()


# ---------------------------------------------------------------------------
# 系统初始化与认证
# ---------------------------------------------------------------------------
@app.get("/api/system/init-status")
def get_init_status(db: Session = Depends(get_db)):
    has_admin = db.query(User).filter(User.role == "admin").count() > 0
    return {"initialized": has_admin, "has_admin": has_admin, "total_users": db.query(User).count()}


@app.post("/api/system/init-admin")
@limiter.limit("10/minute")
def init_admin_user(request: Request, req: InitAdminRequest, response: Response, db: Session = Depends(get_db)):
    reject_if_agent_bearer(request.headers.get("Authorization"))
    if db.query(User).filter(User.role == "admin").count() > 0:
        raise HTTPException(status_code=400, detail="系统管理员已初始化，禁止重复设置")
    username = (req.username or "").strip()
    if len(username) < 3 or len(username) > 50:
        raise HTTPException(status_code=400, detail="管理员用户名长度必须在 3 到 50 位之间")
    if not req.password or len(req.password) < 6 or len(req.password) > 128:
        raise HTTPException(status_code=400, detail="管理员密码长度必须在 6 到 128 位之间")

    existing = db.query(User).filter(User.username == username).first()
    password_hash, salt = hash_password(req.password)
    if existing:
        existing.password_hash = password_hash
        existing.salt = salt
        existing.session_version = 0
        existing.role = "admin"
        user = existing
    else:
        user = User(
            username=username,
            password_hash=password_hash,
            salt=salt,
            session_version=0,
            role="admin",
        )
        db.add(user)
    db.commit()
    db.refresh(user)
    seed_user_defaults(db, user.id)
    record_audit(db, user.id, "系统初始化", "system", user.id, f"首次部署初始化管理员 [{username}]")

    token = create_access_token({"sub": user.username, "uid": user.id, "role": user.role, "sv": user.session_version})
    response.set_cookie("access_token", token, httponly=True, max_age=86400 * 7, samesite="lax")
    return {"success": True, "message": "管理员初始化成功", "token": token, "username": user.username, "role": "admin"}


@app.post("/api/auth/register")
@limiter.limit("10/minute")
def register_user(request: Request, req: RegisterRequest, db: Session = Depends(get_db)):
    reject_if_agent_bearer(request.headers.get("Authorization"))
    if os.environ.get("ALLOW_REGISTRATION", "").strip().lower() not in ("1", "true", "yes", "on"):
        raise HTTPException(status_code=403, detail="系统未开放自主注册，请联系管理员创建成员账号")
    client_ip = get_remote_address(request)
    check_auth_banned(client_ip)
    username = (req.username or "").strip()
    if len(username) < 3 or len(username) > 50:
        raise HTTPException(status_code=400, detail="用户名长度必须在 3 到 50 位之间")
    if not req.password or len(req.password) < 6 or len(req.password) > 128:
        raise HTTPException(status_code=400, detail="密码长度必须在 6 到 128 位之间")
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=400, detail="用户名已被使用，请更换")
    password_hash, salt = hash_password(req.password)
    user = User(username=username, password_hash=password_hash, salt=salt, session_version=0, role="user")
    db.add(user)
    db.commit()
    db.refresh(user)
    seed_user_defaults(db, user.id)
    record_audit(db, user.id, "注册账号", "user", user.id, f"新用户 [{username}] 注册")
    token = create_access_token({"sub": user.username, "uid": user.id, "role": user.role, "sv": user.session_version})
    return {"success": True, "message": "注册成功", "token": token, "username": user.username, "role": user.role}


@app.post("/api/auth/login")
@limiter.limit("10/minute")
def login(request: Request, req: LoginRequest, response: Response, db: Session = Depends(get_db)):
    client_ip = get_remote_address(request)
    check_auth_banned(client_ip)
    if not req.username or not req.username.strip() or not req.password:
        record_auth_failure(client_ip)
        raise HTTPException(status_code=400, detail="用户名或密码不能为空")
    user = db.query(User).filter(User.username == req.username.strip()).first()
    if not user:
        record_auth_failure(client_ip)
        raise HTTPException(status_code=400, detail="用户名或密码错误")
    is_valid, needs_rehash = verify_password_and_check_rehash(req.password, user.salt, user.password_hash)
    if not is_valid:
        record_auth_failure(client_ip)
        raise HTTPException(status_code=400, detail="用户名或密码错误")
    if user.deleted_at is not None or not bool(getattr(user, "is_active", True)):
        raise HTTPException(status_code=400, detail="账号已停用，请联系管理员")
    clear_auth_failures(client_ip)
    rehash_user_password_if_needed(db, user, req.password, needs_rehash)
    user.last_login = datetime.now()
    db.commit()
    seed_user_defaults(db, user.id)

    token = create_access_token({"sub": user.username, "uid": user.id, "role": user.role, "sv": user.session_version})
    response.set_cookie("access_token", token, httponly=True, max_age=86400 * 7, samesite="lax")
    record_audit(db, user.id, "登录系统", "user", user.id, f"用户 [{user.username}] 登录")
    return {"success": True, "message": "登录成功", "token": token, "username": user.username, "role": user.role}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    reject_if_agent_bearer(request.headers.get("Authorization"))
    response.delete_cookie("access_token")
    response.delete_cookie("expense_token")
    return {"success": True, "message": "已退出登录"}


@app.get("/api/auth/me")
def get_me(request: Request, user: User = Depends(get_current_user)):
    payload = {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "auth_method": getattr(request.state, "auth_method", "session"),
    }
    if payload["auth_method"] == "agent":
        payload["token_name"] = getattr(request.state, "agent_token_name", "")
        payload["scopes"] = list(getattr(request.state, "agent_scopes", []) or [])
    return payload


@app.post("/api/auth/change-password")
def change_password(req: PasswordChangeRequest, response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not req.new_password or len(req.new_password) < 6 or len(req.new_password) > 128:
        raise HTTPException(status_code=400, detail="新密码长度必须在 6 到 128 位之间")
    is_valid, _ = verify_password_and_check_rehash(req.old_password, user.salt, user.password_hash)
    if not is_valid:
        raise HTTPException(status_code=400, detail="原密码不正确")
    new_hash, new_salt = hash_password(req.new_password)
    user.password_hash = new_hash
    user.salt = new_salt
    user.session_version += 1
    db.commit()
    token = create_access_token({"sub": user.username, "uid": user.id, "role": user.role, "sv": user.session_version})
    response.set_cookie("access_token", token, httponly=True, max_age=86400 * 7, samesite="lax")
    record_audit(db, user.id, "修改密码", "user", user.id, "密码修改成功")
    return {"success": True, "message": "密码修改成功", "token": token}


@app.get("/api/admin/users")
def list_admin_users(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="权限不足")
    users = db.query(User).order_by(User.id.asc()).all()
    return [
        {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "is_active": bool(getattr(user, "is_active", True)) and user.deleted_at is None,
            "deleted": user.deleted_at is not None,
            "status": "已删除" if user.deleted_at is not None else ("已停用" if not bool(getattr(user, "is_active", True)) else "正常"),
            "created_at": user.created_at.strftime("%Y-%m-%d %H:%M") if user.created_at else "-",
            "last_login": user.last_login.strftime("%Y-%m-%d %H:%M") if user.last_login else "-",
            "receivable_count": db.query(Receivable).filter(Receivable.user_id == user.id, Receivable.deleted_at.is_(None)).count(),
            "company_count": db.query(Company).filter(Company.user_id == user.id).count(),
        }
        for user in users
    ]


@app.post("/api/admin/users")
def admin_create_user(req: AdminCreateUserRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="权限不足")
    username = (req.username or "").strip()
    if len(username) < 3 or len(username) > 50:
        raise HTTPException(status_code=400, detail="用户名长度必须在 3 到 50 位之间")
    if not req.password or len(req.password) < 6:
        raise HTTPException(status_code=400, detail="初始密码至少 6 位")
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=400, detail="该用户名已存在")
    role = req.role if req.role in ("admin", "user") else "user"
    if role == "admin" and not bool(req.confirm_admin):
        raise HTTPException(status_code=400, detail="创建管理员需要二次确认")
    password_hash, salt = hash_password(req.password)
    new_user = User(username=username, password_hash=password_hash, salt=salt, session_version=0, role=role)
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    seed_user_defaults(db, new_user.id)
    record_audit(db, current_user.id, "创建用户", "user", new_user.id, f"创建用户 [{username}]")
    return {"success": True, "message": f"用户 [{username}] 创建成功"}


@app.post("/api/admin/users/{user_id}/reset-password")
def admin_reset_user_password(user_id: int, req: AdminResetPasswordRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="权限不足")
    if not req.new_password or len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码至少 6 位")
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="目标用户不存在")
    password_hash, salt = hash_password(req.new_password)
    target.password_hash = password_hash
    target.salt = salt
    target.session_version += 1
    db.commit()
    record_audit(db, current_user.id, "重置密码", "user", target.id, f"重置用户 [{target.username}] 密码")
    return {"success": True, "message": "密码已重置"}


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="权限不足")
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="不能删除当前登录账号")
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="目标用户不存在")
    if target.deleted_at is not None:
        return {"success": True, "message": f"用户 [{target.username}] 已删除"}
    username = target.username
    target.is_active = False
    target.deleted_at = datetime.now()
    target.session_version = (target.session_version or 0) + 1
    db.commit()
    remove_user_uploads(target.id)
    record_audit(db, current_user.id, "删除用户", "user", user_id, f"软删除用户 [{username}]，账本数据保留，上传文件已清理")
    return {"success": True, "message": f"用户 [{username}] 已删除，账本数据保留"}


@app.post("/api/admin/users/{user_id}/disable")
def admin_disable_user(user_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="权限不足")
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="不能停用当前登录账号")
    target = db.query(User).filter(User.id == user_id).first()
    if not target or target.deleted_at is not None:
        raise HTTPException(status_code=404, detail="目标用户不存在")
    target.is_active = False
    target.session_version = (target.session_version or 0) + 1
    db.commit()
    record_audit(db, current_user.id, "停用用户", "user", target.id, f"停用用户 [{target.username}]")
    return {"success": True, "message": f"用户 [{target.username}] 已停用"}


@app.post("/api/admin/users/{user_id}/enable")
def admin_enable_user(user_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="权限不足")
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="目标用户不存在")
    if target.deleted_at is not None:
        raise HTTPException(status_code=400, detail="已删除的成员不能直接启用")
    target.is_active = True
    target.session_version = (target.session_version or 0) + 1
    db.commit()
    record_audit(db, current_user.id, "启用用户", "user", target.id, f"启用用户 [{target.username}]")
    return {"success": True, "message": f"用户 [{target.username}] 已启用"}


# ---------------------------------------------------------------------------
# 公司 / 类别
# ---------------------------------------------------------------------------
@app.get("/api/companies")
def list_companies(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    companies = (
        db.query(Company)
        .filter(Company.user_id == user.id)
        .order_by(Company.is_active.desc(), Company.id.asc())
        .all()
    )
    result = []
    today = today_str()
    for company in companies:
        active = _receivable_query(db, user).filter(
            Receivable.company_id == company.id,
            Receivable.status.in_(("pending", "submitted", "partial")),
        ).all()
        outstanding = sum(max(0, (item.amount_cents or 0) - (item.received_cents or 0)) for item in active)
        overdue_items = [
            item for item in active
            if item.status in ("submitted", "partial") and item.expected_on and item.expected_on < today
        ]
        last_paid = (
            db.query(func.max(Payment.paid_on))
            .filter(
                Payment.user_id == user.id,
                Payment.company_id == company.id,
                Payment.deleted_at.is_(None),
            )
            .scalar()
        )
        last_receivable = (
            db.query(func.max(Receivable.created_at))
            .filter(
                Receivable.user_id == user.id,
                Receivable.company_id == company.id,
                Receivable.deleted_at.is_(None),
            )
            .scalar()
        )
        last_payment = (
            db.query(func.max(Payment.created_at))
            .filter(
                Payment.user_id == user.id,
                Payment.company_id == company.id,
                Payment.deleted_at.is_(None),
            )
            .scalar()
        )
        last_confirm = (
            db.query(func.max(CaptureDraft.updated_at))
            .filter(
                CaptureDraft.user_id == user.id,
                CaptureDraft.company_id == company.id,
                CaptureDraft.status == "confirmed",
            )
            .scalar()
        )
        last_used_dt = max([item for item in (last_receivable, last_payment, last_confirm) if item], default=None)
        last_used = last_used_dt.strftime("%Y-%m-%d %H:%M:%S") if last_used_dt else ""
        stats = {
            "outstanding": cents_to_yuan(outstanding),
            "outstanding_cents": outstanding,
            "outstanding_count": len(active),
            "overdue": cents_to_yuan(sum(max(0, (item.amount_cents or 0) - (item.received_cents or 0)) for item in overdue_items)),
            "overdue_count": len(overdue_items),
            "oldest_overdue_days": max([days_overdue(item.expected_on) for item in overdue_items] or [0]),
            "last_paid_on": last_paid or "",
            "last_used_at": last_used,
        }
        result.append(_serialize_company(company, stats))
    active = [item for item in result if item.get("is_active")]
    inactive = [item for item in result if not item.get("is_active")]
    active.sort(key=lambda item: item.get("last_used_at") or "", reverse=True)
    return active + inactive


@app.post("/api/companies")
def create_company(req: CompanyCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="公司名称不能为空")
    if db.query(Company).filter(Company.user_id == user.id, Company.name == name).first():
        raise HTTPException(status_code=400, detail="公司名称已存在")
    company = Company(
        user_id=user.id,
        name=name,
        short_name=(req.short_name or "").strip(),
        invoice_title=(req.invoice_title or "").strip(),
        tax_id=(req.tax_id or "").strip(),
        payday=max(0, min(31, int(req.payday or 0))),
        payment_days=max(0, min(365, int(req.payment_days or 30))),
        color=req.color or "#2563eb",
        note=(req.note or "").strip(),
    )
    db.add(company)
    db.commit()
    db.refresh(company)
    record_audit(db, user.id, "新建公司", "company", company.id, f"新建公司 [{name}]")
    return _serialize_company(company)


@app.put("/api/companies/{company_id}")
def update_company(company_id: int, req: CompanyUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    company = _company_or_404(db, user, company_id)
    if req.name is not None:
        name = req.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="公司名称不能为空")
        duplicate = (
            db.query(Company)
            .filter(Company.user_id == user.id, Company.name == name, Company.id != company_id)
            .first()
        )
        if duplicate:
            raise HTTPException(status_code=400, detail="公司名称已存在")
        company.name = name
    if req.short_name is not None:
        company.short_name = req.short_name.strip()
    if req.invoice_title is not None:
        company.invoice_title = req.invoice_title.strip()
    if req.tax_id is not None:
        company.tax_id = req.tax_id.strip()
    if req.payday is not None:
        company.payday = max(0, min(31, int(req.payday)))
    if req.payment_days is not None:
        company.payment_days = max(0, min(365, int(req.payment_days)))
    if req.color is not None:
        company.color = req.color
    if req.note is not None:
        company.note = req.note.strip()
    if req.is_active is not None:
        company.is_active = req.is_active
    db.commit()
    record_audit(db, user.id, "修改公司", "company", company.id, f"修改公司 [{company.name}]")
    return _serialize_company(company)


@app.delete("/api/companies/{company_id}")
def delete_company(company_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    company = _company_or_404(db, user, company_id)
    used = db.query(Receivable).filter(Receivable.company_id == company.id, Receivable.deleted_at.is_(None)).count()
    if used:
        company.is_active = False
        db.commit()
        return {"success": True, "message": "该公司已有应收记录，已停用而不是删除"}
    db.delete(company)
    db.commit()
    record_audit(db, user.id, "删除公司", "company", company_id, f"删除公司 [{company.name}]")
    return {"success": True, "message": "公司已删除"}


@app.get("/api/categories")
def list_categories(include_inactive: bool = False, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    query = db.query(Category).filter(Category.user_id == user.id)
    if not include_inactive:
        query = query.filter(Category.is_active == True)
    categories = query.order_by(Category.sort_order.asc(), Category.id.asc()).all()
    return [_serialize_category(category) for category in categories]


@app.post("/api/categories")
def create_category(req: CategoryCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="类别名称不能为空")
    if db.query(Category).filter(Category.user_id == user.id, Category.name == name).first():
        raise HTTPException(status_code=400, detail="类别名称已存在")
    kind = req.kind if req.kind in ("reimbursement", "payroll", "other") else "reimbursement"
    category = Category(
        user_id=user.id,
        name=name,
        kind=kind,
        requires_invoice=bool(req.requires_invoice),
        sort_order=int(req.sort_order or 0),
    )
    db.add(category)
    db.commit()
    db.refresh(category)
    record_audit(db, user.id, "新建类别", "category", category.id, f"新建类别 [{name}]")
    return _serialize_category(category)


@app.put("/api/categories/{category_id}")
def update_category(category_id: int, req: CategoryUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    category = _category_or_404(db, user, category_id)
    if req.name is not None:
        name = req.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="类别名称不能为空")
        duplicate = (
            db.query(Category)
            .filter(Category.user_id == user.id, Category.name == name, Category.id != category_id)
            .first()
        )
        if duplicate:
            raise HTTPException(status_code=400, detail="类别名称已存在")
        category.name = name
    if req.kind is not None and req.kind in ("reimbursement", "payroll", "other"):
        category.kind = req.kind
    if req.requires_invoice is not None:
        category.requires_invoice = req.requires_invoice
    if req.sort_order is not None:
        category.sort_order = req.sort_order
    if req.is_active is not None:
        category.is_active = req.is_active
    db.commit()
    record_audit(db, user.id, "修改类别", "category", category.id, f"修改类别 [{category.name}]")
    return _serialize_category(category)


@app.delete("/api/categories/{category_id}")
def delete_category(category_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    category = _category_or_404(db, user, category_id)
    used = db.query(Receivable).filter(Receivable.category_id == category.id).count()
    if used:
        category.is_active = False
        db.commit()
        return {"success": True, "message": "该类别已有记录，已停用"}
    db.delete(category)
    db.commit()
    return {"success": True, "message": "类别已删除"}


# ---------------------------------------------------------------------------
# 采集收件箱
# ---------------------------------------------------------------------------
@app.post("/api/capture")
async def capture_files(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    company_id: Optional[int] = Form(None),
    category_id: Optional[int] = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if company_id:
        _company_or_404(db, user, company_id)
    if category_id:
        _category_or_404(db, user, category_id)
    if not files:
        raise HTTPException(status_code=400, detail="请选择要上传的文件")
    if len(files) > 30:
        raise HTTPException(status_code=400, detail="一次最多上传 30 个文件")

    drafts: list[CaptureDraft] = []
    skipped: list[dict] = []
    for upload in files:
        ext = file_ext(upload.filename)
        if ext not in (".pdf", ".ofd", ".zip") and ext not in IMAGE_EXTS:
            raise HTTPException(status_code=400, detail=f"不支持的文件类型: {upload.filename}")
        data = await upload.read()
        if len(data) > 25 * 1024 * 1024:
            raise HTTPException(status_code=400, detail=f"文件超过 25MB: {upload.filename}")
        if ext == ".zip":
            saved_zip = save_upload_bytes(user.id, upload.filename, data)
            try:
                zip_drafts, zip_skipped = create_drafts_from_zip(
                    db, user.id, saved_zip["stored_path"], company_id, category_id
                )
                drafts.extend(zip_drafts)
                skipped.extend(zip_skipped)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            finally:
                try:
                    os.remove(saved_zip["stored_path"])
                except OSError:
                    pass
        else:
            saved = save_upload_bytes(user.id, upload.filename, data)
            drafts.append(create_draft(db, user.id, saved, company_id, category_id))

    if not drafts:
        detail = "压缩包内没有可识别的文件"
        if skipped:
            raise HTTPException(
                status_code=400,
                detail={"message": detail, "skipped": skipped, "skipped_count": len(skipped)},
            )
        raise HTTPException(status_code=400, detail=detail)

    sync_mode = os.environ.get("CAPTURE_SYNC", "").strip().lower() in ("1", "true", "yes")
    if sync_mode:
        for draft in drafts:
            await _process_draft(db, draft.id)
    else:
        background_tasks.add_task(process_drafts_async, [draft.id for draft in drafts])
    for draft in drafts:
        db.refresh(draft)
    payloads = [draft_payload(item, db) for item in drafts]
    duplicates = []
    for item in payloads:
        for warning in item.get("duplicates") or []:
            warning = dict(warning)
            warning["draft_id"] = item["id"]
            warning["filename"] = item["original_filename"]
            duplicates.append(warning)
    parts = [f"已生成 {len(drafts)} 条草稿"]
    if skipped:
        parts.append(f"跳过 {len(skipped)} 个文件")
    if duplicates:
        parts.append(f"{len(duplicates)} 条疑似重复")
    return {
        "success": True,
        "count": len(drafts),
        "drafts": payloads,
        "skipped": skipped,
        "skipped_count": len(skipped),
        "duplicates": duplicates,
        "duplicate_count": len(duplicates),
        "message": "，".join(parts),
    }


@app.get("/api/capture/drafts")
def list_drafts(
    status_value: Optional[str] = None,
    limit: int = 60,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(CaptureDraft).filter(CaptureDraft.user_id == user.id)
    if status_value:
        query = query.filter(CaptureDraft.status == status_value)
    else:
        query = query.filter(CaptureDraft.status.in_(("pending", "processing", "ready", "failed")))
    drafts = query.order_by(CaptureDraft.id.desc()).limit(min(limit, 200)).all()
    return [draft_payload(item, db) for item in drafts]


@app.patch("/api/capture/drafts/{draft_id}")
def update_draft(draft_id: int, req: DraftUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    draft = db.query(CaptureDraft).filter(CaptureDraft.id == draft_id, CaptureDraft.user_id == user.id).first()
    if not draft:
        raise HTTPException(status_code=404, detail="草稿不存在")
    if draft.status == "confirmed":
        raise HTTPException(status_code=400, detail="草稿已入账，不能再修改")
    if req.company_id is not None:
        _company_or_404(db, user, req.company_id)
        draft.company_id = req.company_id
    if req.category_id is not None:
        _category_or_404(db, user, req.category_id)
        draft.category_id = req.category_id
    if req.amount is not None:
        amount_cents = yuan_to_cents(req.amount)
        if amount_cents <= 0:
            raise HTTPException(status_code=400, detail="金额必须大于 0")
        draft.amount_cents = amount_cents
    if req.occurred_on is not None:
        draft.occurred_on = normalize_date(req.occurred_on)
        if not (draft.period or "").strip():
            draft.period = period_from_date(draft.occurred_on)
    if req.period is not None:
        draft.period = normalize_period(req.period, fallback="")
    if req.title is not None:
        draft.title = req.title.strip()[:200]
    if req.merchant is not None:
        draft.merchant = req.merchant.strip()[:150]
    if req.doc_type is not None and req.doc_type in DOC_TYPES:
        draft.doc_type = req.doc_type
    if req.invoice_code is not None:
        draft.invoice_code = req.invoice_code.strip()[:50]
    if req.invoice_no is not None:
        draft.invoice_no = req.invoice_no.strip()[:100]
    if req.note is not None:
        draft.note = req.note.strip()[:300]
    if req.amount is not None or req.occurred_on is not None:
        # 用户核对过的字段不再提示低置信
        try:
            extracted = json.loads(draft.extracted_json or "{}")
        except Exception:
            extracted = {}
        if isinstance(extracted, dict):
            if req.amount is not None:
                extracted["amount_confirmed"] = True
            if req.occurred_on is not None:
                extracted["date_missing"] = False
            draft.extracted_json = json.dumps(extracted, ensure_ascii=False)
    if req.amount is not None and draft.amount_cents and draft.error:
        if any(key in draft.error for key in ("未配置 AI", "手动填写", "金额")):
            draft.error = None
    db.commit()
    return draft_payload(draft, db)


@app.post("/api/capture/drafts/{draft_id}/retry")
async def retry_draft(
    draft_id: int,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    draft = db.query(CaptureDraft).filter(CaptureDraft.id == draft_id, CaptureDraft.user_id == user.id).first()
    if not draft:
        raise HTTPException(status_code=404, detail="草稿不存在")
    if draft.status == "confirmed":
        raise HTTPException(status_code=400, detail="草稿已入账")
    rebuild_draft_from_file(db, draft)
    # 用户主动重试说明上次结果不对，不复用同文件的历史结果
    sync_mode = os.environ.get("CAPTURE_SYNC", "").strip().lower() in ("1", "true", "yes")
    if sync_mode:
        await _process_draft(db, draft.id, reuse=False)
    else:
        background_tasks.add_task(process_drafts_async, [draft.id], False)
    db.refresh(draft)
    return draft_payload(draft, db)


@app.delete("/api/capture/drafts/{draft_id}")
def delete_draft(draft_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    draft = db.query(CaptureDraft).filter(CaptureDraft.id == draft_id, CaptureDraft.user_id == user.id).first()
    if not draft:
        raise HTTPException(status_code=404, detail="草稿不存在")
    if draft.status == "confirmed":
        raise HTTPException(status_code=400, detail="已入账的草稿不能删除")
    for path in (draft.stored_path, draft.thumb_path):
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
    for extra in get_extra_files(draft):
        try:
            os.remove(extra.get("stored_path"))
        except OSError:
            pass
    db.delete(draft)
    db.commit()
    return {"success": True, "message": "草稿已丢弃"}


@app.get("/api/capture/drafts/{draft_id}/thumb")
def get_draft_thumb(draft_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    draft = db.query(CaptureDraft).filter(CaptureDraft.id == draft_id, CaptureDraft.user_id == user.id).first()
    if not draft:
        raise HTTPException(status_code=404, detail="草稿不存在")
    path = draft.thumb_path or draft.stored_path
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="缩略图不存在")
    return FileResponse(path, media_type="image/jpeg" if path.endswith(".jpg") else draft.content_type, content_disposition_type="inline")


@app.get("/api/capture/drafts/{draft_id}/file")
def get_draft_file(draft_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    draft = db.query(CaptureDraft).filter(CaptureDraft.id == draft_id, CaptureDraft.user_id == user.id).first()
    if not draft or not os.path.exists(draft.stored_path):
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(draft.stored_path, media_type=draft.content_type or "application/octet-stream", filename=draft.original_filename, content_disposition_type="inline")


def _preview_http_error(exc: PreviewUnsupported) -> HTTPException:
    code = 404 if exc.kind in ("missing",) else 400
    return HTTPException(status_code=code, detail=exc.message)


def _preview_response(info: dict) -> FileResponse:
    return FileResponse(
        info["path"],
        media_type=info["media_type"],
        content_disposition_type="inline",
        headers={
            "X-Preview-Page": str(info["page"]),
            "X-Preview-Pages": str(info["pages"]),
            "X-Preview-Kind": info["kind"],
            "Cache-Control": "private, max-age=3600",
        },
    )


@app.get("/api/capture/drafts/{draft_id}/preview")
def get_draft_preview(draft_id: int, page: int = 1, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    draft = db.query(CaptureDraft).filter(CaptureDraft.id == draft_id, CaptureDraft.user_id == user.id).first()
    if not draft:
        raise HTTPException(status_code=404, detail="草稿不存在")
    try:
        info = ensure_preview(user.id, draft.stored_path, page, draft.original_filename)
    except PreviewUnsupported as exc:
        raise _preview_http_error(exc) from exc
    return _preview_response(info)


@app.post("/api/capture/confirm")
def confirm_drafts(req: CaptureConfirm, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not req.draft_ids:
        raise HTTPException(status_code=400, detail="请选择要入账的草稿")
    drafts = (
        db.query(CaptureDraft)
        .filter(CaptureDraft.user_id == user.id, CaptureDraft.id.in_(req.draft_ids))
        .all()
    )
    by_id = {item.id: item for item in drafts}
    missing = [draft_id for draft_id in req.draft_ids if draft_id not in by_id]
    if missing:
        raise HTTPException(status_code=404, detail=f"草稿不存在: {missing}")

    active = [item for item in drafts if item.status != "confirmed"]
    already = [item for item in drafts if item.status == "confirmed"]
    if not active:
        return {"success": True, "created": [], "warnings": [], "already_confirmed": [item.receivable_id for item in already]}

    target = None
    if req.target_receivable_id:
        target = _receivable_or_404(db, user, req.target_receivable_id)
        if target.status == "paid":
            raise HTTPException(status_code=400, detail="已到账的应收不能再追加附件")
        for draft in active:
            if draft.company_id and draft.company_id != target.company_id:
                raise HTTPException(status_code=400, detail="草稿的归属公司与目标应收不一致")

    warnings: list[dict] = []
    if not req.force:
        seen_invoice = set()
        seen_hash = set()
        for draft in active:
            if draft.invoice_no:
                key = (draft.invoice_code or "", draft.invoice_no)
                if key in seen_invoice:
                    warnings.append({"type": "duplicate_in_batch", "message": f"本批里有重复发票号 {draft.invoice_no}"})
                seen_invoice.add(key)
            if draft.sha256:
                if draft.sha256 in seen_hash:
                    warnings.append({"type": "duplicate_in_batch", "message": f"本批里有重复文件 {draft.original_filename}"})
                seen_hash.add(draft.sha256)
            warnings.extend(find_duplicates(db, user.id, draft.sha256, draft.invoice_no, draft.invoice_code, exclude_draft_id=draft.id))
            warnings.extend(find_buyer_mismatch(db, user.id, draft, req.company_id or (target.company_id if target else None)))
        if warnings:
            has_duplicate = any(item.get("type") != "buyer_mismatch" for item in warnings)
            message = "检测到疑似重复或抬头不一致，请确认后再入账" if has_duplicate else "发票购买方与公司抬头不一致，请确认后再入账"
            raise HTTPException(status_code=409, detail={"message": message, "warnings": warnings})

    company_id = req.company_id or (target.company_id if target else None)
    category_id = req.category_id
    if not target:
        if not company_id:
            missing_company = [item.id for item in active if not item.company_id]
            if missing_company:
                raise HTTPException(
                    status_code=400,
                    detail={"message": "请为这些草稿选择公司", "draft_ids": missing_company},
                )
        companies = {req.company_id or item.company_id for item in active}
        if len(companies) > 1:
            raise HTTPException(status_code=400, detail="一次确认的草稿必须属于同一家公司")
        company_id = companies.pop()
        _company_or_404(db, user, company_id)

    created: list[dict] = []
    for draft in active:
        if not target and (draft.amount_cents is None or draft.amount_cents <= 0):
            raise HTTPException(status_code=400, detail=f"草稿「{draft.original_filename}」金额为空，请先填写金额")
        if not draft.occurred_on:
            draft.occurred_on = today_str()
        if not draft.title:
            draft.title = draft.merchant or "凭证"

        if target:
            receivable = target
        else:
            category = None
            chosen_category_id = category_id or draft.category_id
            if chosen_category_id:
                category = _category_or_404(db, user, chosen_category_id)
            else:
                default_category = (
                    db.query(Category)
                    .filter(Category.user_id == user.id, Category.kind == ("payroll" if draft.doc_type == "salary" else "reimbursement"), Category.is_active == True)
                    .order_by(Category.sort_order.asc())
                    .first()
                )
                category = default_category
            receivable = Receivable(
                user_id=user.id,
                company_id=company_id,
                category_id=category.id if category else None,
                kind=category.kind if category else ("payroll" if draft.doc_type == "salary" else "reimbursement"),
                title=draft.title or draft.original_filename,
                period=resolve_item_period(req.period or draft.period, draft.occurred_on),
                occurred_on=draft.occurred_on,
                amount_cents=draft.amount_cents,
                status="pending",
                source="capture",
                note=draft.note or "",
            )
            db.add(receivable)
            db.flush()

        attachment = Attachment(
            user_id=user.id,
            receivable_id=receivable.id,
            kind=ATTACHMENT_KIND_BY_DOC_TYPE.get(draft.doc_type, "other"),
            filename=draft.original_filename,
            stored_path=draft.stored_path,
            content_type=draft.content_type,
            file_size=draft.file_size,
            sha256=draft.sha256,
            invoice_code=draft.invoice_code or "",
            invoice_no=draft.invoice_no or "",
            amount_cents=draft.amount_cents,
            note=draft.note or "",
            sort_order=len(receivable.attachments),
            ocr_json=draft.extracted_json,
        )
        db.add(attachment)
        for extra_index, extra in enumerate(get_extra_files(draft), start=1):
            db.add(
                Attachment(
                    user_id=user.id,
                    receivable_id=receivable.id,
                    kind=extra.get("kind") or "other",
                    filename=extra.get("original_filename") or "附件",
                    stored_path=extra.get("stored_path"),
                    content_type=extra.get("content_type") or "",
                    file_size=extra.get("file_size") or 0,
                    sha256=extra.get("sha256") or "",
                    note=extra.get("note") or "",
                    sort_order=len(receivable.attachments) + extra_index,
                )
            )
        draft.status = "confirmed"
        draft.receivable_id = receivable.id
        db.flush()
        created.append(_serialize_receivable(receivable))

    db.commit()
    record_audit(
        db,
        user.id,
        "采集入账",
        "receivable",
        created[0]["id"] if created else None,
        f"确认 {len(active)} 份凭证入账",
    )
    return {"success": True, "created": created, "warnings": warnings, "already_confirmed": [item.receivable_id for item in already]}


@app.post("/api/capture/drafts/{draft_id}/apply-payment")
def apply_draft_payment(
    draft_id: int,
    req: CapturePaymentApply,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """把工资到账截图核销到已有工资应收。

    不传 allocations 时用推断结果（备注月份 → 最早欠发顺序）；分摊合计必须等于到账金额。
    """
    draft = db.query(CaptureDraft).filter(CaptureDraft.id == draft_id, CaptureDraft.user_id == user.id).first()
    if not draft:
        raise HTTPException(status_code=404, detail="草稿不存在")
    if draft.status == "confirmed":
        raise HTTPException(status_code=400, detail="草稿已入账")
    if draft.status == "processing":
        raise HTTPException(status_code=409, detail="草稿还在识别中，请稍后再试")
    if not draft.company_id:
        raise HTTPException(status_code=400, detail="请先选择公司")
    if not draft.amount_cents:
        raise HTTPException(status_code=400, detail="请先填写到账金额")
    _company_or_404(db, user, draft.company_id)
    try:
        extracted = json.loads(draft.extracted_json or "{}")
        if not isinstance(extracted, dict):
            extracted = {}
    except Exception:
        extracted = {}
    remark = str(extracted.get("remark") or "").strip()

    if req.allocations:
        allocations = req.allocations
        try:
            allocated = sum(yuan_to_cents(item.amount) for item in allocations)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="分摊金额格式不正确") from exc
    else:
        proposal = propose_allocation(
            db,
            user.id,
            draft.company_id,
            draft.amount_cents,
            draft.occurred_on or "",
            remark,
        )
        if not proposal["allocations"]:
            raise HTTPException(
                status_code=400,
                detail={"message": "没有可核销的工资应收，请先建好对应月份的应收", "proposal": proposal},
            )
        allocations = [
            PaymentAllocation(receivable_id=item["receivable_id"], amount=item["amount"])
            for item in proposal["allocations"]
        ]
        allocated = sum(yuan_to_cents(item.amount) for item in allocations)
        if allocated != draft.amount_cents:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "推断结果与到账金额不一致，请手动选择要核销的应收",
                    "proposal": proposal,
                },
            )

    payment = _record_payment(
        db,
        user,
        PaymentCreate(
            company_id=draft.company_id,
            amount=cents_to_yuan(draft.amount_cents),
            paid_on=normalize_date(draft.occurred_on or today_str()),
            allocations=allocations,
            note=(req.note or draft.note or remark).strip(),
            idempotency_key=f"capture-draft-{draft.id}",
        ),
    )

    receivable_ids = sorted({item.receivable_id for item in allocations})
    receivable = (
        db.query(Receivable)
        .filter(Receivable.user_id == user.id, Receivable.id == receivable_ids[0], Receivable.deleted_at.is_(None))
        .first()
    )
    if receivable:
        db.add(
            Attachment(
                user_id=user.id,
                receivable_id=receivable.id,
                kind=ATTACHMENT_KIND_BY_DOC_TYPE.get(draft.doc_type, "payment_proof"),
                filename=draft.original_filename,
                stored_path=draft.stored_path,
                content_type=draft.content_type,
                file_size=draft.file_size,
                sha256=draft.sha256,
                invoice_code=draft.invoice_code or "",
                invoice_no=draft.invoice_no or "",
                amount_cents=draft.amount_cents,
                note=draft.note or "",
                sort_order=len(receivable.attachments),
                ocr_json=draft.extracted_json,
            )
        )
        for extra_index, extra in enumerate(get_extra_files(draft), start=1):
            db.add(
                Attachment(
                    user_id=user.id,
                    receivable_id=receivable.id,
                    kind=extra.get("kind") or "other",
                    filename=extra.get("original_filename") or "附件",
                    stored_path=extra.get("stored_path"),
                    content_type=extra.get("content_type") or "",
                    file_size=extra.get("file_size") or 0,
                    sha256=extra.get("sha256") or "",
                    note=extra.get("note") or "",
                    sort_order=len(receivable.attachments) + extra_index,
                )
            )
    draft.status = "confirmed"
    draft.receivable_id = receivable_ids[0]
    draft.error = None
    db.commit()
    record_audit(
        db,
        user.id,
        "到账截图核销",
        "payment",
        payment.id,
        f"核销 {len(allocations)} 笔工资应收，共 ¥{cents_to_text(draft.amount_cents)}",
    )
    db.refresh(draft)
    return {
        "success": True,
        "payment_id": payment.id,
        "allocations": [
            {"receivable_id": item.receivable_id, "amount": cents_to_yuan(yuan_to_cents(item.amount))}
            for item in allocations
        ],
        "draft": draft_payload(draft, db),
    }


# ---------------------------------------------------------------------------
# 应收台账
# ---------------------------------------------------------------------------
@app.get("/api/receivables")
def list_receivables(
    company_id: Optional[int] = None,
    status_value: Optional[str] = None,
    kind: Optional[str] = None,
    category_id: Optional[int] = None,
    period_from: Optional[str] = None,
    period_to: Optional[str] = None,
    q: Optional[str] = None,
    overdue: bool = False,
    missing_materials: bool = False,
    page: int = 1,
    page_size: int = 50,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = _filter_receivables(
        db,
        user,
        company_id=company_id,
        status_value=status_value,
        kind=kind,
        category_id=category_id,
        period_from=period_from,
        period_to=period_to,
        query_text=q,
        only_overdue=overdue,
        only_missing_materials=missing_materials,
    )
    total = query.count()
    total_amount = sum(item.amount_cents or 0 for item in query.all())
    page = max(1, page)
    page_size = max(1, min(200, page_size))
    items = (
        query.order_by(Receivable.occurred_on.desc(), Receivable.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "items": [_serialize_receivable(item) for item in items],
        "total": total,
        "total_amount": cents_to_yuan(total_amount),
        "page": page,
        "page_size": page_size,
    }


@app.get("/api/receivables/{receivable_id}")
def get_receivable(receivable_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _serialize_receivable(_receivable_or_404(db, user, receivable_id), detail=True)


@app.post("/api/receivables")
def create_receivable(req: ReceivableCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    company = _company_or_404(db, user, req.company_id)
    if not req.title or not req.title.strip():
        raise HTTPException(status_code=400, detail="事项名称不能为空")
    amount_cents = yuan_to_cents(req.amount)
    if amount_cents <= 0:
        raise HTTPException(status_code=400, detail="金额必须大于 0")
    category = _category_or_404(db, user, req.category_id) if req.category_id else None
    occurred_on = normalize_date(req.occurred_on or "")
    receivable = Receivable(
        user_id=user.id,
        company_id=company.id,
        category_id=category.id if category else None,
        kind=category.kind if category else "reimbursement",
        title=req.title.strip()[:150],
        period=resolve_item_period(req.period, occurred_on),
        occurred_on=occurred_on,
        amount_cents=amount_cents,
        status="pending",
        expected_on=normalize_date(req.expected_on) if req.expected_on else "",
        note=(req.note or "").strip(),
        source="manual",
    )
    db.add(receivable)
    db.commit()
    db.refresh(receivable)
    record_audit(db, user.id, "新增应收", "receivable", receivable.id, f"新增应收 [{receivable.title}] ¥{cents_to_text(amount_cents)}")
    return _serialize_receivable(receivable)


@app.put("/api/receivables/{receivable_id}")
def update_receivable(receivable_id: int, req: ReceivableUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    receivable = _receivable_or_404(db, user, receivable_id)
    locked_touch = []
    if req.company_id is not None:
        locked_touch.append("company_id")
    if req.category_id is not None:
        locked_touch.append("category_id")
    if req.title is not None:
        locked_touch.append("title")
    if req.amount is not None:
        locked_touch.append("amount")
    if req.period is not None:
        locked_touch.append("period")
    if locked_touch:
        assert_unlocked_for_edit(receivable, locked_touch)
    if req.company_id is not None:
        receivable.company_id = _company_or_404(db, user, req.company_id).id
    if req.category_id is not None:
        category = _category_or_404(db, user, req.category_id)
        receivable.category_id = category.id
        receivable.kind = category.kind
    if req.title is not None:
        if not req.title.strip():
            raise HTTPException(status_code=400, detail="事项名称不能为空")
        receivable.title = req.title.strip()[:150]
    if req.occurred_on is not None:
        if receivable.status in ("abandoned",):
            raise HTTPException(status_code=400, detail="已放弃的应收不能再修改")
        receivable.occurred_on = normalize_date(req.occurred_on)
        # 发生日与归属月独立，改发生日不再覆盖已确认的归属月
        if not (receivable.period or "").strip():
            receivable.period = period_from_date(receivable.occurred_on)
    if req.period is not None:
        receivable.period = normalize_period(req.period) or period_from_date(receivable.occurred_on)
    if req.amount is not None:
        amount_cents = yuan_to_cents(req.amount)
        if amount_cents <= 0:
            raise HTTPException(status_code=400, detail="金额必须大于 0")
        if amount_cents < (receivable.received_cents or 0):
            raise HTTPException(status_code=400, detail="金额不能小于已到账金额")
        receivable.amount_cents = amount_cents
        if (receivable.received_cents or 0) >= amount_cents:
            receivable.status = "paid"
            if not receivable.paid_on:
                receivable.paid_on = today_str()
        elif receivable.status == "paid":
            receivable.status = "partial"
            receivable.paid_on = ""
    if req.expected_on is not None:
        if receivable.status == "abandoned":
            raise HTTPException(status_code=400, detail="已放弃的应收不能再修改")
        receivable.expected_on = normalize_date(req.expected_on) if req.expected_on else ""
    if req.note is not None:
        if receivable.status == "abandoned":
            raise HTTPException(status_code=400, detail="已放弃的应收不能再修改")
        receivable.note = req.note.strip()
    if req.status is not None:
        reason = (req.reason or "").strip()
        assert_status_change(receivable, req.status, reason)
        previous = receivable.status
        receivable.status = req.status
        if req.status == "pending":
            receivable.submitted_on = ""
            receivable.expected_on = ""
        if req.status in ("abandoned", "rejected") and receivable.batch_id:
            receivable.batch_id = None
        action = "放弃应收" if req.status == "abandoned" else ("驳回应收" if req.status == "rejected" else "修改应收")
        summary = f"{action} [{receivable.title}]"
        if reason:
            summary += f"，原因：{reason}"
        if previous != req.status:
            summary += f"（{previous} → {req.status}）"
        db.commit()
        record_audit(db, user.id, action, "receivable", receivable.id, summary)
        return _serialize_receivable(receivable, detail=True)
    db.commit()
    record_audit(db, user.id, "修改应收", "receivable", receivable.id, f"修改应收 [{receivable.title}]")
    return _serialize_receivable(receivable, detail=True)


@app.delete("/api/receivables/{receivable_id}")
def delete_receivable(receivable_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    receivable = _receivable_or_404(db, user, receivable_id)
    assert_can_delete(receivable)
    receivable.deleted_at = datetime.now()
    receivable.batch_id = None
    for attachment in receivable.attachments:
        attachment.deleted_at = datetime.now()
    db.commit()
    record_audit(db, user.id, "删除应收", "receivable", receivable.id, f"删除应收 [{receivable.title}]")
    return {"success": True, "message": "应收已移入回收状态"}


@app.post("/api/receivables/{receivable_id}/attachments")
async def upload_attachments(
    receivable_id: int,
    files: List[UploadFile] = File(...),
    kind: str = Form("screenshot"),
    invoice_no: str = Form(""),
    invoice_code: str = Form(""),
    note: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    receivable = _receivable_or_404(db, user, receivable_id)
    if kind not in ("invoice", "screenshot", "receipt", "chat", "payment_proof", "other"):
        kind = "other"
    created = []
    existing_count = len([a for a in receivable.attachments if a.deleted_at is None])
    for upload in files:
        ext = file_ext(upload.filename)
        if ext not in IMAGE_EXTS | {".pdf", ".ofd"}:
            raise HTTPException(status_code=400, detail=f"不支持的附件类型: {upload.filename}")
        data = await upload.read()
        if len(data) > 25 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="附件不能超过 25MB")
        saved = save_upload_bytes(user.id, upload.filename, data)
        target = user_dir(user.id, "attachments")
        new_path = os.path.join(target, os.path.basename(saved["stored_path"]))
        shutil.move(saved["stored_path"], new_path)
        attachment = Attachment(
            user_id=user.id,
            receivable_id=receivable.id,
            kind="invoice" if ext == ".pdf" and kind == "screenshot" else kind,
            filename=saved["original_filename"],
            stored_path=new_path,
            content_type=saved["content_type"],
            file_size=saved["file_size"],
            sha256=saved["sha256"],
            invoice_no=invoice_no.strip(),
            invoice_code=invoice_code.strip(),
            note=note.strip(),
            sort_order=existing_count + len(created),
        )
        db.add(attachment)
        created.append(attachment)
    db.commit()
    return {"success": True, "attachments": [_serialize_attachment(item) for item in created]}


@app.delete("/api/attachments/{attachment_id}")
def delete_attachment(attachment_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    attachment = (
        db.query(Attachment)
        .filter(Attachment.id == attachment_id, Attachment.user_id == user.id, Attachment.deleted_at.is_(None))
        .first()
    )
    if not attachment:
        raise HTTPException(status_code=404, detail="附件不存在")
    attachment.deleted_at = datetime.now()
    db.commit()
    return {"success": True, "message": "附件已移除"}


@app.get("/api/attachments/{attachment_id}/file")
def get_attachment_file(attachment_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    attachment = (
        db.query(Attachment)
        .filter(Attachment.id == attachment_id, Attachment.user_id == user.id, Attachment.deleted_at.is_(None))
        .first()
    )
    if not attachment or not os.path.exists(attachment.stored_path):
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(
        attachment.stored_path,
        media_type=attachment.content_type or "application/octet-stream",
        filename=attachment.filename,
        content_disposition_type="inline",
    )


@app.get("/api/attachments/{attachment_id}/thumb")
def get_attachment_thumb(attachment_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    attachment = (
        db.query(Attachment)
        .filter(Attachment.id == attachment_id, Attachment.user_id == user.id, Attachment.deleted_at.is_(None))
        .first()
    )
    if not attachment or not os.path.exists(attachment.stored_path):
        raise HTTPException(status_code=404, detail="文件不存在")
    ext = file_ext(attachment.stored_path or attachment.filename)
    thumb = create_thumbnail(attachment.stored_path, user.id, ext)
    if thumb and os.path.exists(thumb):
        return FileResponse(thumb, media_type="image/jpeg")
    raise HTTPException(status_code=404, detail="暂无缩略图")


@app.get("/api/attachments/{attachment_id}/preview")
def get_attachment_preview(attachment_id: int, page: int = 1, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    attachment = (
        db.query(Attachment)
        .filter(Attachment.id == attachment_id, Attachment.user_id == user.id, Attachment.deleted_at.is_(None))
        .first()
    )
    if not attachment:
        raise HTTPException(status_code=404, detail="附件不存在")
    try:
        info = ensure_preview(user.id, attachment.stored_path, page, attachment.filename)
    except PreviewUnsupported as exc:
        raise _preview_http_error(exc) from exc
    return _preview_response(info)


def fitz_available() -> bool:
    try:
        import pymupdf  # noqa: F401

        return True
    except Exception:
        try:
            import fitz  # noqa: F401

            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# 批次
# ---------------------------------------------------------------------------
@app.get("/api/batches")
def list_batches(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    batches = (
        db.query(Batch)
        .filter(Batch.user_id == user.id)
        .order_by(Batch.created_at.desc())
        .all()
    )
    result = []
    for batch in batches:
        items = [item for item in batch.receivables if item.deleted_at is None]
        unpaid = [item for item in items if (item.received_cents or 0) < (item.amount_cents or 0)]
        overdue = [
            item
            for item in unpaid
            if item.status in ("submitted", "partial") and item.expected_on and item.expected_on < today_str()
        ]
        result.append(
            {
                "id": batch.id,
                "company_id": batch.company_id,
                "company_name": batch.company.name if batch.company else "",
                "title": batch.title,
                "period": batch.period or "",
                "status": batch.status,
                "status_label": {"draft": "草稿", "submitted": "已提交", "paid": "已结清", "cancelled": "已取消"}.get(batch.status, batch.status),
                "submitted_on": batch.submitted_on or "",
                "expected_on": batch.expected_on or "",
                "paid_on": batch.paid_on or "",
                "count": len(items),
                "unpaid_count": len(unpaid),
                "amount": cents_to_yuan(sum(item.amount_cents or 0 for item in items)),
                "amount_cents": sum(item.amount_cents or 0 for item in items),
                "received": cents_to_yuan(sum(item.received_cents or 0 for item in items)),
                "remaining": cents_to_yuan(sum((item.amount_cents or 0) - (item.received_cents or 0) for item in items)),
                "overdue_count": len(overdue),
                "note": batch.note or "",
                "has_payments": sum(item.received_cents or 0 for item in items) > 0,
                "partial": (
                    sum(item.received_cents or 0 for item in items) > 0
                    and any((item.received_cents or 0) < (item.amount_cents or 0) for item in items)
                    and batch.status != "paid"
                ),
                "can_unsubmit": batch.status == "submitted" and all((item.received_cents or 0) == 0 for item in items),
                "can_cancel": batch.status in ("draft", "submitted") and all((item.received_cents or 0) == 0 for item in items),
                "can_edit_items": batch.status == "draft",
                "status_label": (
                    "部分到账"
                    if batch.status != "paid"
                    and sum(item.received_cents or 0 for item in items) > 0
                    and any((item.received_cents or 0) < (item.amount_cents or 0) for item in items)
                    else {"draft": "草稿", "submitted": "已提交", "paid": "已结清", "cancelled": "已取消"}.get(batch.status, batch.status)
                ),
            }
        )
    return result


@app.post("/api/batches")
def create_batch(req: BatchCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    company = _company_or_404(db, user, req.company_id)
    if not req.receivable_ids:
        raise HTTPException(status_code=400, detail="请选择要归集的应收事项")
    receivables = (
        db.query(Receivable)
        .filter(
            Receivable.user_id == user.id,
            Receivable.id.in_(req.receivable_ids),
            Receivable.deleted_at.is_(None),
        )
        .all()
    )
    if len(receivables) != len(set(req.receivable_ids)):
        raise HTTPException(status_code=404, detail="部分应收事项不存在")
    if any(item.company_id != company.id for item in receivables):
        raise HTTPException(status_code=400, detail="批次内的应收必须属于同一家公司")
    if any(item.batch_id for item in receivables):
        raise HTTPException(status_code=400, detail="部分应收已经属于其他批次")
    if any(item.status not in ("pending", "rejected") for item in receivables):
        raise HTTPException(status_code=400, detail="只有待提交或被驳回的应收可以归集")

    period = assert_same_period(receivables, req.period or "") or _current_month()
    title = (req.title or "").strip() or f"{company.name} {period} 报销"
    batch = Batch(user_id=user.id, company_id=company.id, title=title[:150], period=period, status="draft")
    db.add(batch)
    db.flush()
    for item in receivables:
        item.batch_id = batch.id
        if item.status == "rejected":
            item.status = "pending"
    db.commit()
    db.refresh(batch)
    record_audit(db, user.id, "创建批次", "batch", batch.id, f"创建批次 [{batch.title}]，共 {len(receivables)} 笔")
    return {"success": True, "batch_id": batch.id, "message": "批次已创建"}


def _batch_flags(batch: Batch, items: list[Receivable]) -> dict:
    received = sum(item.received_cents or 0 for item in items)
    remaining = sum(max(0, (item.amount_cents or 0) - (item.received_cents or 0)) for item in items)
    has_pay = received > 0
    return {
        "received": cents_to_yuan(received),
        "remaining": cents_to_yuan(remaining),
        "has_payments": has_pay,
        "partial": has_pay and remaining > 0,
        "can_unsubmit": batch.status == "submitted" and not has_pay,
        "can_cancel": batch.status in ("draft", "submitted") and not has_pay,
        "can_edit_items": batch.status == "draft" and not has_pay,
        "status_label": (
            "部分到账" if has_pay and remaining > 0 and batch.status != "paid"
            else {"draft": "草稿", "submitted": "已提交", "paid": "已结清", "cancelled": "已取消"}.get(batch.status, batch.status)
        ),
    }


@app.get("/api/batches/{batch_id}")
def get_batch(batch_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    batch = _batch_or_404(db, user, batch_id)
    items = [item for item in batch.receivables if item.deleted_at is None]
    amount = sum(item.amount_cents or 0 for item in items)
    data = {
        "id": batch.id,
        "company_id": batch.company_id,
        "company_name": batch.company.name if batch.company else "",
        "title": batch.title,
        "period": batch.period or "",
        "status": batch.status,
        "submitted_on": batch.submitted_on or "",
        "expected_on": batch.expected_on or "",
        "paid_on": batch.paid_on or "",
        "note": batch.note or "",
        "amount": cents_to_yuan(amount),
        "amount_cents": amount,
        "count": len(items),
        "items": [_serialize_receivable(item) for item in sorted(items, key=lambda x: (x.occurred_on, x.id))],
    }
    data.update(_batch_flags(batch, items))
    return data


@app.put("/api/batches/{batch_id}")
def update_batch(batch_id: int, req: BatchUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    batch = _batch_or_404(db, user, batch_id)
    if req.title is not None:
        batch.title = req.title.strip()[:150] or batch.title
    if req.period is not None:
        if batch.status != "draft":
            raise HTTPException(status_code=400, detail="已提交批次的归属月不能直接修改，请先撤回提交")
        items = [item for item in batch.receivables if item.deleted_at is None]
        if items:
            batch.period = assert_same_period(items, req.period)
        else:
            batch.period = normalize_period(req.period) or batch.period
    if req.expected_on is not None:
        batch.expected_on = normalize_date(req.expected_on) if req.expected_on else ""
        for item in batch.receivables:
            if item.deleted_at is None and item.status == "submitted":
                item.expected_on = batch.expected_on
    if req.note is not None:
        batch.note = req.note.strip()
    db.commit()
    return {"success": True, "message": "批次已更新"}


@app.post("/api/batches/{batch_id}/submit")
def submit_batch(batch_id: int, req: BatchSubmit, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    batch = _batch_or_404(db, user, batch_id)
    if batch.status in ("paid", "cancelled"):
        raise HTTPException(status_code=400, detail="该批次不能再次提交")
    items = [item for item in batch.receivables if item.deleted_at is None]
    if not items:
        raise HTTPException(status_code=400, detail="批次内没有应收事项")
    submitted_on = normalize_date(req.submitted_on or today_str())
    expected_on = normalize_date(req.expected_on) if req.expected_on else _next_pay_date(batch.company, submitted_on)
    for item in items:
        if item.status in ("pending", "rejected"):
            item.status = "submitted"
            item.submitted_on = submitted_on
            item.expected_on = expected_on
    batch.status = "submitted"
    batch.submitted_on = submitted_on
    batch.expected_on = expected_on
    db.commit()
    record_audit(db, user.id, "提交批次", "batch", batch.id, f"提交批次 [{batch.title}]，预计 {expected_on} 到账")
    return {"success": True, "message": "批次已提交", "expected_on": expected_on}


@app.post("/api/batches/{batch_id}/cancel")
def cancel_batch(batch_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    batch = _batch_or_404(db, user, batch_id)
    if batch.status == "cancelled":
        return {"success": True, "message": "批次已经取消"}
    if batch.status == "paid":
        raise HTTPException(status_code=400, detail="已结清的批次不能取消")
    assert_batch_adjustable(batch, "取消")
    items = [item for item in batch.receivables if item.deleted_at is None]
    amount = sum(item.amount_cents or 0 for item in items)
    for item in items:
        reset_receivable_to_pending(item, unbind=True)
    batch.status = "cancelled"
    db.commit()
    record_audit(
        db,
        user.id,
        "取消批次",
        "batch",
        batch.id,
        f"取消批次 [{batch.title}]，退回 {len(items)} 笔、合计 ¥{cents_to_text(amount)}",
    )
    return {
        "success": True,
        "message": "批次已取消，应收事项已退回待提交",
        "count": len(items),
        "amount": cents_to_yuan(amount),
    }


@app.post("/api/batches/{batch_id}/unsubmit")
def unsubmit_batch(batch_id: int, req: BatchUnsubmitRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    reason = (req.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="撤回提交必须填写原因")
    batch = _batch_or_404(db, user, batch_id)
    if batch.status != "submitted":
        raise HTTPException(status_code=400, detail="只有已提交且尚未到账的批次可以撤回提交")
    assert_batch_adjustable(batch, "撤回")
    items = [item for item in batch.receivables if item.deleted_at is None]
    amount = sum(item.amount_cents or 0 for item in items)
    original = batch.submitted_on or ""
    for item in items:
        reset_receivable_to_pending(item, unbind=False)
    batch.status = "draft"
    batch.submitted_on = ""
    batch.expected_on = ""
    db.commit()
    record_audit(
        db,
        user.id,
        "撤回提交",
        "batch",
        batch.id,
        f"撤回批次 [{batch.title}]，原提交日期 {original or '无'}，原因：{reason}，涉及 {len(items)} 笔、合计 ¥{cents_to_text(amount)}",
        details=json.dumps(
            {
                "reason": reason,
                "original_submitted_on": original,
                "receivable_ids": [item.id for item in items],
                "amount_cents": amount,
            },
            ensure_ascii=False,
        ),
    )
    return {
        "success": True,
        "message": "已撤回提交，批次回到草稿，可以继续调整",
        "count": len(items),
        "amount": cents_to_yuan(amount),
        "original_submitted_on": original,
    }


@app.post("/api/batches/{batch_id}/items")
def update_batch_items(batch_id: int, req: BatchItemsRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    batch = _batch_or_404(db, user, batch_id)
    if batch.status != "draft":
        raise HTTPException(status_code=400, detail="只有草稿批次可以增删应收；已提交的请先撤回提交")
    assert_batch_adjustable(batch, "调整")
    remove_ids = list(dict.fromkeys(req.remove_ids or []))
    add_ids = list(dict.fromkeys(req.add_ids or []))
    if not remove_ids and not add_ids:
        raise HTTPException(status_code=400, detail="请指定要移出或添加的应收")

    for rid in remove_ids:
        item = _receivable_or_404(db, user, rid)
        if item.batch_id != batch.id:
            raise HTTPException(status_code=400, detail=f"应收 #{rid} 不属于该批次")
        item.batch_id = None
        if item.status == "submitted" and (item.received_cents or 0) == 0:
            item.status = "pending"
            item.submitted_on = ""
            item.expected_on = ""

    for rid in add_ids:
        item = _receivable_or_404(db, user, rid)
        if item.company_id != batch.company_id:
            raise HTTPException(status_code=400, detail=f"应收「{item.title}」不属于同一家公司")
        if item.batch_id and item.batch_id != batch.id:
            raise HTTPException(status_code=400, detail=f"应收「{item.title}」已经属于其他批次")
        if item.status not in COLLECTABLE_STATUSES:
            raise HTTPException(status_code=400, detail=f"应收「{item.title}」当前不能归集：{collectable_reason(item)}")
        item.batch_id = batch.id
        if item.status == "rejected":
            item.status = "pending"

    remaining = [item for item in batch.receivables if item.deleted_at is None and item.batch_id == batch.id]
    # 刷新刚改动的对象
    remaining = (
        db.query(Receivable)
        .filter(Receivable.batch_id == batch.id, Receivable.deleted_at.is_(None), Receivable.user_id == user.id)
        .all()
    )
    if remaining:
        batch.period = assert_same_period(remaining, batch.period) or batch.period
    db.commit()
    record_audit(
        db,
        user.id,
        "调整批次",
        "batch",
        batch.id,
        f"调整批次 [{batch.title}]，添加 {len(add_ids)} 笔、移出 {len(remove_ids)} 笔",
    )
    return get_batch(batch_id, user, db)


# ---------------------------------------------------------------------------
# 到账记录
# ---------------------------------------------------------------------------
def _resolve_allocations(
    db: Session, user: User, req: PaymentCreate, amount_cents: int
) -> list[tuple[Receivable, int]]:
    """把一笔到账拆成「应收 -> 本次核销金额」列表。

    显式 allocations 优先；否则按 receivable_ids 顺序把到账金额自动分摊到各笔待收余额。
    """
    if req.allocations:
        ids = [alloc.receivable_id for alloc in req.allocations]
        if len(ids) != len(set(ids)):
            raise HTTPException(status_code=400, detail="同一笔应收不能重复分摊")
        receivables = (
            db.query(Receivable)
            .filter(
                Receivable.user_id == user.id,
                Receivable.id.in_(ids),
                Receivable.deleted_at.is_(None),
            )
            .all()
        )
        by_id = {item.id: item for item in receivables}
        if len(by_id) != len(set(ids)):
            raise HTTPException(status_code=404, detail="部分应收事项不存在")
        result = []
        for alloc in req.allocations:
            receivable = by_id[alloc.receivable_id]
            try:
                alloc_cents = yuan_to_cents(alloc.amount)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="分摊金额格式不正确") from exc
            result.append((receivable, alloc_cents))
        return result

    if not req.receivable_ids:
        raise HTTPException(status_code=400, detail="请选择这笔到账核销的应收事项")
    receivables = (
        db.query(Receivable)
        .filter(
            Receivable.user_id == user.id,
            Receivable.id.in_(req.receivable_ids),
            Receivable.deleted_at.is_(None),
        )
        .order_by(Receivable.id.asc())
        .all()
    )
    if len(receivables) != len(set(req.receivable_ids)):
        raise HTTPException(status_code=404, detail="部分应收事项不存在")

    remaining = amount_cents
    result = []
    for receivable in receivables:
        if remaining <= 0:
            break
        outstanding = (receivable.amount_cents or 0) - (receivable.received_cents or 0)
        alloc = min(remaining, max(0, outstanding))
        if alloc > 0:
            result.append((receivable, alloc))
            remaining -= alloc
    if remaining > 0:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "到账金额超过所选应收的待收合计",
                "unallocated": cents_to_yuan(remaining),
            },
        )
    return result


def _record_payment(db: Session, user: User, req: PaymentCreate) -> Payment:
    if req.idempotency_key:
        existing = (
            db.query(Payment)
            .filter(
                Payment.user_id == user.id,
                Payment.idempotency_key == req.idempotency_key,
                Payment.deleted_at.is_(None),
            )
            .first()
        )
        if existing:
            return existing

    company = _company_or_404(db, user, req.company_id)
    amount_cents = yuan_to_cents(req.amount)
    if amount_cents <= 0:
        raise HTTPException(status_code=400, detail="到账金额必须大于 0")

    allocations = _resolve_allocations(db, user, req, amount_cents)
    if not allocations:
        raise HTTPException(status_code=400, detail="请选择这笔到账核销的应收事项")

    for receivable, alloc_cents in allocations:
        if receivable.company_id != company.id:
            raise HTTPException(status_code=400, detail="到账只能核销同一家公司的应收")
        if receivable.status in ("paid", "abandoned", "rejected"):
            raise HTTPException(status_code=400, detail="选中的应收里有已结清或已关闭的记录")
        outstanding = (receivable.amount_cents or 0) - (receivable.received_cents or 0)
        if alloc_cents <= 0:
            raise HTTPException(status_code=400, detail="每笔核销金额必须大于 0")
        if alloc_cents > outstanding:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": f"「{receivable.title}」的核销金额超过待收余额",
                    "receivable_id": receivable.id,
                    "outstanding": cents_to_yuan(outstanding),
                },
            )

    total_allocated = sum(alloc for _, alloc in allocations)
    if total_allocated != amount_cents:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "分摊合计与到账金额不一致",
                "expected": cents_to_yuan(amount_cents),
                "actual": cents_to_yuan(total_allocated),
                "difference": cents_to_yuan(amount_cents - total_allocated),
            },
        )

    batch_ids = {item.batch_id for item, _ in allocations if item.batch_id}
    paid_on = normalize_date(req.paid_on or today_str())
    payment = Payment(
        user_id=user.id,
        company_id=company.id,
        batch_id=batch_ids.pop() if len(batch_ids) == 1 else None,
        amount_cents=amount_cents,
        paid_on=paid_on,
        note=(req.note or "").strip(),
        idempotency_key=req.idempotency_key,
    )
    db.add(payment)
    db.flush()

    for receivable, alloc_cents in allocations:
        receivable.received_cents = (receivable.received_cents or 0) + alloc_cents
        if receivable.received_cents >= (receivable.amount_cents or 0):
            receivable.status = "paid"
            receivable.paid_on = paid_on
        else:
            receivable.status = "partial"
            receivable.paid_on = ""
        db.add(PaymentItem(payment_id=payment.id, receivable_id=receivable.id, amount_cents=alloc_cents))
    db.flush()

    for batch_id in {item.batch_id for item, _ in allocations if item.batch_id}:
        batch = db.query(Batch).filter(Batch.id == batch_id).first()
        if batch:
            still_open = [
                item for item in batch.receivables
                if item.deleted_at is None and (item.received_cents or 0) < (item.amount_cents or 0)
            ]
            if not still_open:
                batch.status = "paid"
                batch.paid_on = paid_on
            elif batch.status == "paid":
                batch.status = "submitted"
                batch.paid_on = ""
    db.commit()
    db.refresh(payment)
    record_audit(
        db,
        user.id,
        "记录到账",
        "payment",
        payment.id,
        f"{company.name} 到账 ¥{cents_to_text(amount_cents)}，核销 {len(allocations)} 笔",
    )
    return payment


@app.get("/api/payments")
def list_payments(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    payments = (
        db.query(Payment)
        .filter(Payment.user_id == user.id, Payment.deleted_at.is_(None))
        .order_by(Payment.paid_on.desc(), Payment.id.desc())
        .all()
    )
    result = []
    for payment in payments:
        result.append(
            {
                "id": payment.id,
                "company_id": payment.company_id,
                "company_name": payment.company.name if payment.company else "",
                "batch_id": payment.batch_id,
                "batch_title": payment.batch.title if payment.batch else "",
                "amount": cents_to_yuan(payment.amount_cents),
                "paid_on": payment.paid_on or "",
                "note": payment.note or "",
                "item_count": len([item for item in payment.items if item.receivable and item.receivable.deleted_at is None]),
                "created_at": payment.created_at.strftime("%Y-%m-%d %H:%M") if payment.created_at else "",
            }
        )
    return result


@app.post("/api/payments")
def create_payment(req: PaymentCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    payment = _record_payment(db, user, req)
    return {
        "success": True,
        "payment_id": payment.id,
        "message": "到账记录已保存",
        "amount": cents_to_yuan(payment.amount_cents),
    }


@app.get("/api/payments/{payment_id}")
def get_payment(payment_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    payment = (
        db.query(Payment)
        .filter(Payment.id == payment_id, Payment.user_id == user.id, Payment.deleted_at.is_(None))
        .first()
    )
    if not payment:
        raise HTTPException(status_code=404, detail="到账记录不存在")
    return {
        "id": payment.id,
        "company_name": payment.company.name if payment.company else "",
        "amount": cents_to_yuan(payment.amount_cents),
        "paid_on": payment.paid_on or "",
        "note": payment.note or "",
        "items": [
            {
                **_serialize_receivable(item.receivable),
                "allocated": cents_to_yuan(item.amount_cents or 0),
            }
            for item in payment.items
            if item.receivable and item.receivable.deleted_at is None
        ],
    }


@app.delete("/api/payments/{payment_id}")
def delete_payment(payment_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    payment = (
        db.query(Payment)
        .filter(Payment.id == payment_id, Payment.user_id == user.id, Payment.deleted_at.is_(None))
        .first()
    )
    if not payment:
        raise HTTPException(status_code=404, detail="到账记录不存在")
    touched_batches = set()
    for item in payment.items:
        receivable = item.receivable
        if not receivable:
            continue
        receivable.received_cents = max(0, (receivable.received_cents or 0) - (item.amount_cents or 0))
        if receivable.received_cents <= 0:
            receivable.status = "submitted" if receivable.batch_id else "pending"
            receivable.paid_on = ""
        elif receivable.received_cents < (receivable.amount_cents or 0):
            receivable.status = "partial"
            receivable.paid_on = ""
        else:
            receivable.status = "paid"
        if receivable.batch_id:
            touched_batches.add(receivable.batch_id)
    for batch_id in touched_batches:
        batch = db.query(Batch).filter(Batch.id == batch_id).first()
        if batch:
            still_open = [
                item for item in batch.receivables
                if item.deleted_at is None and (item.received_cents or 0) < (item.amount_cents or 0)
            ]
            batch.status = "submitted" if still_open else "paid"
            batch.paid_on = "" if still_open else batch.paid_on
    payment.deleted_at = datetime.now()
    db.commit()
    record_audit(db, user.id, "删除到账", "payment", payment.id, f"删除到账记录 ¥{cents_to_text(payment.amount_cents)}")
    return {"success": True, "message": "到账记录已删除，相关应收已退回"}


@app.post("/api/batches/{batch_id}/payment")
def pay_batch(batch_id: int, req: BatchPaymentRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    batch = _batch_or_404(db, user, batch_id)
    items = [
        item for item in batch.receivables
        if item.deleted_at is None
        and item.status in ("submitted", "partial")
        and (item.received_cents or 0) < (item.amount_cents or 0)
    ]
    if not items:
        raise HTTPException(status_code=400, detail="该批次没有待核销的应收")
    payload = PaymentCreate(
        company_id=batch.company_id,
        amount=cents_to_yuan(sum((item.amount_cents or 0) - (item.received_cents or 0) for item in items)),
        paid_on=req.paid_on,
        allocations=[
            {"receivable_id": item.id, "amount": cents_to_yuan((item.amount_cents or 0) - (item.received_cents or 0))}
            for item in items
        ],
        note=req.note,
        idempotency_key=req.idempotency_key,
    )
    payment = _record_payment(db, user, payload)
    return {"success": True, "payment_id": payment.id, "message": "批次已结清"}


# ---------------------------------------------------------------------------
# 薪资计划
# ---------------------------------------------------------------------------
def _plan_or_404(db: Session, user: User, plan_id: int) -> PayrollPlan:
    plan = db.query(PayrollPlan).filter(PayrollPlan.id == plan_id, PayrollPlan.user_id == user.id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="薪资计划不存在")
    return plan


@app.get("/api/payroll-plans")
def list_payroll_plans(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    plans = (
        db.query(PayrollPlan)
        .filter(PayrollPlan.user_id == user.id)
        .order_by(PayrollPlan.is_active.desc(), PayrollPlan.id.desc())
        .all()
    )
    return [serialize_plan(plan) for plan in plans]


@app.post("/api/payroll-plans")
def create_payroll_plan(req: PayrollPlanCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    company = _company_or_404(db, user, req.company_id)
    start, end = parse_plan_dates(req.effective_from, req.effective_to)
    amount_cents = parse_salary(req.amount)
    payday = clamp_payday(req.payday)
    category = _payroll_category(db, user.id, req.category_id)
    assert_no_overlap(db, user.id, company.id, start, end)
    plan = PayrollPlan(
        user_id=user.id,
        company_id=company.id,
        category_id=category.id if category else None,
        amount_cents=amount_cents,
        payday=payday,
        effective_from=start,
        effective_to=end,
        note=(req.note or "").strip()[:300],
        is_active=True,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    record_audit(db, user.id, "新建薪资计划", "payroll_plan", plan.id, f"{company.name} 月薪 ¥{cents_to_text(amount_cents)}，自 {start} 起")
    return serialize_plan(plan)


@app.put("/api/payroll-plans/{plan_id}")
def update_payroll_plan(plan_id: int, req: PayrollPlanUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    plan = _plan_or_404(db, user, plan_id)
    if req.company_id is not None:
        plan.company_id = _company_or_404(db, user, req.company_id).id
    if req.category_id is not None:
        category = _payroll_category(db, user.id, req.category_id)
        plan.category_id = category.id if category else None
    if req.amount is not None:
        plan.amount_cents = parse_salary(req.amount)
    if req.payday is not None:
        plan.payday = clamp_payday(req.payday)
    start = plan.effective_from
    end = plan.effective_to or ""
    if req.effective_from is not None or req.effective_to is not None:
        start, end = parse_plan_dates(
            req.effective_from if req.effective_from is not None else plan.effective_from,
            req.effective_to if req.effective_to is not None else plan.effective_to,
        )
        plan.effective_from = start
        plan.effective_to = end
    if req.note is not None:
        plan.note = req.note.strip()[:300]
    if req.is_active is not None:
        plan.is_active = bool(req.is_active)
    if plan.is_active:
        assert_no_overlap(db, user.id, plan.company_id, plan.effective_from, plan.effective_to or "", exclude_id=plan.id)
    db.commit()
    db.refresh(plan)
    record_audit(db, user.id, "修改薪资计划", "payroll_plan", plan.id, f"修改薪资计划 #{plan.id}")
    return serialize_plan(plan)


@app.delete("/api/payroll-plans/{plan_id}")
def delete_payroll_plan(plan_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    plan = _plan_or_404(db, user, plan_id)
    # 停用即可停止生成；已生成的工资应收保留
    plan.is_active = False
    if not plan.effective_to:
        plan.effective_to = today_str()
    db.commit()
    record_audit(db, user.id, "停发薪资计划", "payroll_plan", plan.id, f"停发薪资计划 #{plan.id}")
    return {"success": True, "message": "计划已停发，已生成的工资应收仍保留"}


@app.post("/api/payroll-plans/sync")
def sync_payroll_plans(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    result = sync_user_plans(db, user.id)
    if result["created_count"]:
        record_audit(
            db,
            user.id,
            "补齐工资应收",
            "payroll_plan",
            None,
            f"补齐 {result['created_count']} 笔工资应收：{', '.join(result['created_periods'])}",
        )
    return result


# ---------------------------------------------------------------------------
# 看板
# ---------------------------------------------------------------------------

@app.get("/api/dashboard")
def dashboard(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    receivables = _receivable_query(db, user).all()
    payments = (
        db.query(Payment)
        .filter(Payment.user_id == user.id, Payment.deleted_at.is_(None))
        .all()
    )
    today = today_str()
    current_month = _current_month()

    def remaining_cents(item: Receivable) -> int:
        return max(0, (item.amount_cents or 0) - (item.received_cents or 0))

    outstanding = [item for item in receivables if item.status in ("pending", "submitted", "partial")]
    overdue = [
        item
        for item in outstanding
        if item.status in ("submitted", "partial") and item.expected_on and item.expected_on < today
    ]
    paid_this_month_cents = sum(
        p.amount_cents or 0 for p in payments if (p.paid_on or "").startswith(current_month)
    )

    durations = []
    for item in receivables:
        if item.status == "paid" and item.submitted_on and item.paid_on:
            start = parse_date(item.submitted_on)
            end = parse_date(item.paid_on)
            if start and end and end >= start:
                durations.append((end - start).days)
    avg_days = round(sum(durations) / len(durations), 1) if durations else 0

    companies = (
        db.query(Company)
        .filter(Company.user_id == user.id, Company.is_active == True)
        .order_by(Company.id.asc())
        .all()
    )
    company_stats = []
    for company in companies:
        items = [item for item in outstanding if item.company_id == company.id]
        late = [
            item for item in items
            if item.status in ("submitted", "partial") and item.expected_on and item.expected_on < today
        ]
        company_outstanding = sum(remaining_cents(item) for item in items)
        company_stats.append(
            {
                "id": company.id,
                "name": company.name,
                "short_name": company.short_name or company.name,
                "color": company.color or "#2563eb",
                "outstanding_cents": company_outstanding,
                "outstanding": cents_to_yuan(company_outstanding),
                "count": len(items),
                "overdue_cents": sum(remaining_cents(item) for item in late),
                "overdue_count": len(late),
                "oldest_overdue_days": max([days_overdue(item.expected_on) for item in late] or [0]),
            }
        )
    company_stats.sort(key=lambda item: item["outstanding_cents"], reverse=True)

    buckets = {"0-30": 0, "31-60": 0, "61-90": 0, "90+": 0}
    for item in outstanding:
        anchor = item.expected_on or item.submitted_on or item.occurred_on
        age = days_overdue(anchor)
        value = remaining_cents(item)
        if age <= 30:
            buckets["0-30"] += value
        elif age <= 60:
            buckets["31-60"] += value
        elif age <= 90:
            buckets["61-90"] += value
        else:
            buckets["90+"] += value

    months = month_range(6)
    trend = []
    for month in months:
        incurred = sum(item.amount_cents or 0 for item in receivables if (item.period or "") == month)
        paid = sum(p.amount_cents or 0 for p in payments if (p.paid_on or "").startswith(month))
        trend.append({"month": month, "incurred": cents_to_yuan(incurred), "paid": cents_to_yuan(paid)})

    by_category = {}
    for item in outstanding:
        name = item.category.name if item.category else "未分类"
        by_category.setdefault(name, 0)
        by_category[name] += remaining_cents(item)
    categories = [
        {"name": name, "amount": cents_to_yuan(amount), "amount_cents": amount}
        for name, amount in sorted(by_category.items(), key=lambda pair: pair[1], reverse=True)
    ]

    overdue_items = sorted(overdue, key=lambda item: days_overdue(item.expected_on), reverse=True)[:10]
    missing_attachments = [
        item for item in outstanding if not any(a.deleted_at is None for a in item.attachments)
    ]
    missing_materials = [item for item in outstanding if not materials_status(item)["materials_ok"]]
    salary_outstanding = [item for item in outstanding if item.kind == "payroll"]
    outstanding_cents = sum(remaining_cents(item) for item in outstanding)
    overdue_cents = sum(remaining_cents(item) for item in overdue)

    pending_this_month = [item for item in outstanding if item.status == "pending" and (item.period or "") == current_month]
    unconfirmed_drafts = (
        db.query(CaptureDraft)
        .filter(
            CaptureDraft.user_id == user.id,
            CaptureDraft.status.in_(("pending", "processing", "ready", "failed")),
        )
        .count()
    )
    payroll_open = [item for item in outstanding if item.kind == "payroll"]
    payroll_todo = missing_summary(db, user.id)
    todos = {
        "unconfirmed_drafts": unconfirmed_drafts,
        "pending_this_month_count": len(pending_this_month),
        "pending_this_month_cents": sum(remaining_cents(item) for item in pending_this_month),
        "pending_this_month": cents_to_yuan(sum(remaining_cents(item) for item in pending_this_month)),
        "pending_this_month_period": current_month,
        "overdue_count": len(overdue),
        "overdue_cents": overdue_cents,
        "overdue": cents_to_yuan(overdue_cents),
        "payroll_outstanding_count": len(payroll_open),
        "payroll_outstanding_cents": sum(remaining_cents(item) for item in payroll_open),
        "payroll_outstanding": cents_to_yuan(sum(remaining_cents(item) for item in payroll_open)),
        "payroll_missing_count": payroll_todo["count"],
        "payroll_missing_cents": payroll_todo["amount_cents"],
        "payroll_missing": payroll_todo,
    }

    return {
        "kpis": {
            "outstanding_cents": outstanding_cents,
            "outstanding": cents_to_yuan(outstanding_cents),
            "outstanding_count": len(outstanding),
            "overdue_cents": overdue_cents,
            "overdue": cents_to_yuan(overdue_cents),
            "overdue_count": len(overdue),
            "paid_this_month_cents": paid_this_month_cents,
            "paid_this_month": cents_to_yuan(paid_this_month_cents),
            "avg_days": avg_days,
            "pending_cents": sum(remaining_cents(item) for item in outstanding if item.status == "pending"),
            "pending_count": len([item for item in outstanding if item.status == "pending"]),
            "partial_count": len([item for item in outstanding if item.status == "partial"]),
            "missing_attachment_count": len(missing_attachments),
            "missing_materials_count": len(missing_materials),
            "salary_outstanding_cents": sum(remaining_cents(item) for item in salary_outstanding),
            "salary_outstanding": cents_to_yuan(sum(remaining_cents(item) for item in salary_outstanding)),
            "drafts_ready": db.query(CaptureDraft).filter(CaptureDraft.user_id == user.id, CaptureDraft.status == "ready").count(),
        },
        "companies": company_stats,
        "aging": [{"bucket": key, "amount": cents_to_yuan(value), "amount_cents": value} for key, value in buckets.items()],
        "trend": trend,
        "categories": categories[:8],
        "overdue": [_serialize_receivable(item) for item in overdue_items],
        "todos": todos,
    }


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
def _export_receivables_or_400(
    db: Session,
    user: User,
    ids: list[int],
    batch_id: int | None,
    period: str | None = None,
    period_from: str | None = None,
    period_to: str | None = None,
    company_id: int | None = None,
):
    receivables = _resolve_export_receivables(
        db, user, ids, batch_id, period=period, period_from=period_from, period_to=period_to, company_id=company_id
    )
    if not receivables:
        raise HTTPException(status_code=400, detail="没有可导出的应收事项")
    return receivables


@app.get("/api/export/excel")
def export_excel(
    company_id: Optional[int] = None,
    period_from: Optional[str] = None,
    period_to: Optional[str] = None,
    kind: Optional[str] = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = _filter_receivables(
        db,
        user,
        company_id=company_id,
        kind=kind,
        period_from=period_from,
        period_to=period_to,
    )
    receivables = query.order_by(Receivable.occurred_on.asc(), Receivable.id.asc()).all()
    if not receivables:
        raise HTTPException(status_code=400, detail="没有可导出的数据")
    stream = build_excel(receivables, "应收明细表")
    from urllib.parse import quote

    filename = f"应收明细_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=utf-8''{quote(filename)}"},
    )


@app.get("/api/export/pdf")
def export_pdf(
    ids: str = "",
    batch_id: Optional[int] = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    parsed_ids = [int(item) for item in ids.split(",") if item.strip().isdigit()]
    receivables = _export_receivables_or_400(db, user, parsed_ids, batch_id)
    content = build_merged_pdf(receivables, db, include_cover=True)
    if not content:
        raise HTTPException(status_code=400, detail="没有可合并的附件")
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=merged_receipts.pdf"},
    )


@app.post("/api/export/finance-pack")
def export_finance_pack(req: FinancePackRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    receivables = _export_receivables_or_400(
        db, user, req.receivable_ids, req.batch_id,
        period=req.period, period_from=req.period_from, period_to=req.period_to, company_id=req.company_id,
    )
    output_dir = user_dir(user.id, "exports")
    info = build_finance_zip(receivables, db, output_dir, "财务报销包")
    record = ExportRecord(
        user_id=user.id,
        kind="finance_pack",
        company_id=receivables[0].company_id if receivables else None,
        batch_id=req.batch_id,
        receivable_ids=json.dumps([item.id for item in receivables]),
        amount_cents=sum(item.amount_cents or 0 for item in receivables),
        filename=info["filename"],
        stored_path=info["path"],
        file_size=info["size"],
        file_hash=info["hash"],
    )
    db.add(record)
    db.commit()
    record_audit(db, user.id, "导出财务包", "export", record.id, f"导出 {len(receivables)} 笔，合计 ¥{cents_to_text(record.amount_cents)}")
    return FileResponse(
        info["path"],
        media_type="application/zip",
        filename=info["filename"],
    )


@app.post("/api/export/attachments")
def export_attachments(req: FinancePackRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    receivables = _export_receivables_or_400(
        db, user, req.receivable_ids, req.batch_id,
        period=req.period, period_from=req.period_from, period_to=req.period_to, company_id=req.company_id,
    )
    output_dir = user_dir(user.id, "exports")
    info = build_attachments_zip(receivables, output_dir, "原始附件")
    record = ExportRecord(
        user_id=user.id,
        kind="attachments",
        company_id=receivables[0].company_id if receivables else None,
        batch_id=req.batch_id,
        receivable_ids=json.dumps([item.id for item in receivables]),
        amount_cents=sum(item.amount_cents or 0 for item in receivables),
        filename=info["filename"],
        stored_path=info["path"],
        file_size=info["size"],
        file_hash=info["hash"],
    )
    db.add(record)
    db.commit()
    return FileResponse(info["path"], media_type="application/zip", filename=info["filename"])


@app.get("/api/export/records")
def list_export_records(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    records = (
        db.query(ExportRecord)
        .filter(ExportRecord.user_id == user.id)
        .order_by(ExportRecord.id.desc())
        .limit(100)
        .all()
    )
    return [
        {
            "id": record.id,
            "kind": record.kind,
            "filename": record.filename,
            "amount": cents_to_yuan(record.amount_cents),
            "file_size": record.file_size,
            "file_hash": record.file_hash,
            "created_at": record.created_at.strftime("%Y-%m-%d %H:%M:%S") if record.created_at else "",
        }
        for record in records
    ]


@app.get("/api/export/records/{record_id}/download")
def download_export_record(record_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    record = (
        db.query(ExportRecord)
        .filter(ExportRecord.id == record_id, ExportRecord.user_id == user.id)
        .first()
    )
    if not record or not os.path.exists(record.stored_path):
        raise HTTPException(status_code=404, detail="导出文件不存在")
    return FileResponse(record.stored_path, media_type="application/zip", filename=record.filename)


# ---------------------------------------------------------------------------
# Agent 令牌（仅会话 JWT 可管理，Agent 令牌不能签发或吊销令牌）
# ---------------------------------------------------------------------------
@app.get("/api/settings/agent-tokens")
def list_agent_tokens(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = (
        db.query(AgentToken)
        .filter(AgentToken.user_id == user.id)
        .order_by(AgentToken.id.desc())
        .all()
    )
    return [serialize_token(row) for row in rows]


@app.post("/api/settings/agent-tokens")
@limiter.limit("10/minute")
def create_agent_token(
    request: Request,
    req: AgentTokenCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    name = (req.name or "").strip()
    if not name or len(name) > 80:
        raise HTTPException(status_code=400, detail="令牌名称不能为空，且不超过 80 个字符")
    scopes = parse_scopes(req.scopes)
    expires_at = parse_expires_at(req.expires_at)
    active_count = (
        db.query(AgentToken)
        .filter(AgentToken.user_id == user.id, AgentToken.revoked_at.is_(None))
        .count()
    )
    if active_count >= MAX_TOKENS_PER_USER:
        raise HTTPException(status_code=400, detail=f"每位用户最多保留 {MAX_TOKENS_PER_USER} 个未吊销的 Agent 令牌")

    plaintext = generate_plaintext_token()
    row = AgentToken(
        user_id=user.id,
        name=name,
        token_prefix=display_prefix(plaintext),
        token_hash=hash_token(plaintext),
        scopes=store_scopes(scopes),
        enabled=True,
        expires_at=expires_at,
        created_at=datetime.now(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    record_audit(db, user.id, "签发 Agent 令牌", "agent_token", row.id, f"签发 Agent 令牌 [{name}]，范围 {','.join(scopes)}")
    payload = serialize_token(row)
    payload["token"] = plaintext
    payload["message"] = "请立即复制并妥善保存，关闭后将无法再次查看明文"
    return payload


@app.patch("/api/settings/agent-tokens/{token_id}")
def update_agent_token(
    token_id: int,
    req: AgentTokenUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = db.query(AgentToken).filter(AgentToken.id == token_id, AgentToken.user_id == user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="令牌不存在")
    if row.revoked_at is not None:
        raise HTTPException(status_code=400, detail="已吊销的令牌不能再修改或启用")
    if req.name is not None:
        name = req.name.strip()
        if not name or len(name) > 80:
            raise HTTPException(status_code=400, detail="令牌名称不能为空，且不超过 80 个字符")
        row.name = name
    if req.enabled is not None:
        row.enabled = bool(req.enabled)
        action = "启用 Agent 令牌" if row.enabled else "停用 Agent 令牌"
    else:
        action = None
    db.commit()
    db.refresh(row)
    if action:
        record_audit(db, user.id, action, "agent_token", row.id, f"{action} [{row.name}]")
    return serialize_token(row)


@app.post("/api/settings/agent-tokens/{token_id}/revoke")
def revoke_agent_token(token_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.query(AgentToken).filter(AgentToken.id == token_id, AgentToken.user_id == user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="令牌不存在")
    if row.revoked_at is None:
        row.revoked_at = datetime.now()
        row.enabled = False
        db.commit()
        record_audit(db, user.id, "吊销 Agent 令牌", "agent_token", row.id, f"吊销 Agent 令牌 [{row.name}]")
    return {"success": True, "message": "令牌已吊销"}


@app.post("/api/settings/agent-tokens/{token_id}/reissue")
@limiter.limit("10/minute")
def reissue_agent_token(request: Request, token_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.query(AgentToken).filter(AgentToken.id == token_id, AgentToken.user_id == user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="令牌不存在")
    if row.revoked_at is not None:
        raise HTTPException(status_code=400, detail="已吊销的令牌不能重新签发")
    plaintext = generate_plaintext_token()
    row.token_prefix = display_prefix(plaintext)
    row.token_hash = hash_token(plaintext)
    row.last_used_at = None
    db.commit()
    db.refresh(row)
    record_audit(db, user.id, "重新签发 Agent 令牌", "agent_token", row.id, f"重新签发 Agent 令牌 [{row.name}]，旧令牌已立即失效")
    payload = serialize_token(row)
    payload["token"] = plaintext
    payload["message"] = "旧令牌已立即失效，请立即复制并妥善保存，关闭后将无法再次查看明文"
    return payload


# ---------------------------------------------------------------------------
# 设置
# ---------------------------------------------------------------------------
@app.get("/api/settings/ai")
def get_ai_settings(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    config = get_ai_config(db, user.id)
    return {
        "enabled": config["enabled"],
        "base_url": config["base_url"],
        "model_name": config["model_name"],
        "has_key": bool(config["api_key"]),
        "system_prompt": config["system_prompt"] or DEFAULT_SYSTEM_PROMPT,
        "default_system_prompt": DEFAULT_SYSTEM_PROMPT,
        "can_call": config["can_call"],
    }


@app.get("/api/settings/ai/summary")
def get_ai_settings_summary(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """给 Agent 的只读摘要：是否启用和模型名称，不含密钥与接口地址。"""
    config = get_ai_config(db, user.id)
    return {
        "enabled": bool(config["enabled"]),
        "model_name": config["model_name"] if config["enabled"] else "",
        "can_call": bool(config["can_call"]),
    }


@app.post("/api/settings/ai")
def save_ai_settings(req: AISettingRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    set_user_setting(db, user.id, "ai_ocr_enabled", "true" if req.enabled else "false")
    set_user_setting(db, user.id, "ai_base_url", req.base_url.strip())
    if req.api_key and req.api_key.strip():
        set_user_setting(db, user.id, "ai_api_key", req.api_key.strip())
    set_user_setting(db, user.id, "ai_model_name", req.model_name.strip())
    prompt = (req.system_prompt or "").strip()
    set_user_setting(db, user.id, "ai_system_prompt", "" if is_default_prompt(prompt) else prompt)
    return {"success": True, "message": "AI 配置已保存"}


@app.post("/api/settings/ai/models")
async def get_models(req: AIModelFetchRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    api_key = req.api_key.strip()
    if not api_key:
        api_key = get_setting(db, user.id, "ai_api_key", "")
    return await fetch_available_models(req.base_url, api_key)


@app.post("/api/settings/ai/test")
async def test_ai(req: AIModelFetchRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    api_key = (req.api_key or "").strip()
    if not api_key:
        api_key = get_setting(db, user.id, "ai_api_key", "")
    # 优先使用当前表单中的模型，未传时才回退到用户已保存的值；不用内置默认模型冒充当前配置
    model_name = (req.model_name or "").strip()
    if not model_name:
        saved = (
            db.query(UserSetting)
            .filter(UserSetting.user_id == user.id, UserSetting.key == "ai_model_name")
            .first()
        )
        model_name = (saved.value or "").strip() if saved else ""
    return await test_ai_connection((req.base_url or "").strip(), api_key, model_name)


@app.get("/api/audit-logs")
def list_audit_logs(limit: int = 100, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    logs = (
        db.query(AuditLog)
        .filter(AuditLog.user_id == user.id)
        .order_by(AuditLog.id.desc())
        .limit(min(limit, 500))
        .all()
    )
    return [
        {
            "id": log.id,
            "action": log.action,
            "entity_type": log.entity_type,
            "summary": log.summary,
            "created_at": log.created_at.strftime("%Y-%m-%d %H:%M:%S") if log.created_at else "",
        }
        for log in logs
    ]


# ---------------------------------------------------------------------------
# 文件签名下载（兼容旧链接）
# ---------------------------------------------------------------------------
@app.get("/api/files/sign")
def sign_file(kind: str, obj_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if kind == "attachment":
        exists = db.query(Attachment).filter(Attachment.id == obj_id, Attachment.user_id == user.id).first()
    elif kind == "export":
        exists = db.query(ExportRecord).filter(ExportRecord.id == obj_id, ExportRecord.user_id == user.id).first()
    else:
        exists = None
    if not exists:
        raise HTTPException(status_code=404, detail="文件不存在")
    token = make_download_token(kind, obj_id, user.id)
    return {"url": f"/api/files/download/{token}", "expires_in": 300}


@app.get("/api/files/download/{token}")
def download_signed_file(token: str, db: Session = Depends(get_db)):
    payload = verify_download_token(token)
    if not payload:
        raise HTTPException(status_code=403, detail="下载链接已失效")
    kind = payload.get("k")
    obj_id = payload.get("i")
    user_id = payload.get("u")
    if kind == "attachment":
        row = db.query(Attachment).filter(Attachment.id == obj_id, Attachment.user_id == user_id).first()
        if row and os.path.exists(row.stored_path):
            return FileResponse(row.stored_path, media_type=row.content_type or "application/octet-stream", filename=row.filename)
    if kind == "export":
        row = db.query(ExportRecord).filter(ExportRecord.id == obj_id, ExportRecord.user_id == user_id).first()
        if row and os.path.exists(row.stored_path):
            return FileResponse(row.stored_path, media_type="application/zip", filename=row.filename)
    raise HTTPException(status_code=404, detail="文件不存在")


@app.get("/api/help/user-guide")
def get_user_guide(user: User = Depends(get_current_user)):
    path = os.path.join(BASE_DIR, "docs", "USER_GUIDE.md")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="使用指南暂未提供")
    with open(path, "r", encoding="utf-8") as handle:
        content = handle.read()
    return {"title": "使用指南", "markdown": content, "version": app.version}


# ---------------------------------------------------------------------------
# 前端静态资源
# ---------------------------------------------------------------------------
STATIC_DIR = os.path.join(BASE_DIR, "app", "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def serve_index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.now().isoformat()}


@app.get("/favicon.ico")
def favicon():
    return FileResponse(os.path.join(STATIC_DIR, "icon-192.png"), media_type="image/png")


@app.get("/favicon.svg")
def favicon_svg():
    return FileResponse(os.path.join(STATIC_DIR, "favicon.svg"), media_type="image/svg+xml")
