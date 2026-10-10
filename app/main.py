import os
import json
import shutil
from datetime import datetime
from typing import Optional, List
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from .database import engine, Base, get_db, BASE_DIR
from .models import User, CategoryRule, Batch, ExpenseReport, Invoice, UserSetting, SystemSetting, AuditLog
from .schemas import (
    LoginRequest, RegisterRequest, PasswordChangeRequest, CategoryRuleCreate, CategoryRuleUpdate,
    StatusUpdateRequest, AISettingRequest, ExpenseReportUpdate, InvoiceUpdate,
    AIModelFetchRequest
)
from .services.auth import (
    get_current_user, create_access_token, verify_password, hash_password, init_default_admin
)
from .services.parser import (
    process_zip_package, process_single_invoice_file, process_voucher_file, record_audit
)
from .services.exporter import export_reports_to_excel
from .services.ai_service import test_ai_connection, fetch_available_models, DEFAULT_SYSTEM_PROMPT

# 初始化数据库结构
Base.metadata.create_all(bind=engine)

app = FastAPI(title="个人发票与报销助手", version="2.0.0")

# CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

def init_user_default_rules(db: Session, user_id: int):
    """为新用户初始化系统默认的分类规则模板"""
    if db.query(CategoryRule).filter(CategoryRule.user_id == user_id).first():
        return
    
    default_rules = [
        CategoryRule(
            user_id=user_id,
            category_code="toll",
            category_name="通行费",
            folder_keywords=json.dumps(["通行费", "通行", "etc", "高速", "路桥"]),
            file_patterns=json.dumps(["*(票据)*.pdf", "*汇总*.pdf"]),
            extractor_type="toll_summary",
            description="ETC收费公路通行费官方汇总单(整月汇总直填)"
        ),
        CategoryRule(
            user_id=user_id,
            category_code="fuel",
            category_name="燃油费",
            folder_keywords=json.dumps(["燃油", "油费", "加油", "汽油", "柴油"]),
            file_patterns=json.dumps([]),
            extractor_type="invoice",
            description="中石化/中石油等成品油电子普通发票(单张明细录入)"
        ),
        CategoryRule(
            user_id=user_id,
            category_code="catering",
            category_name="餐饮招待",
            folder_keywords=json.dumps(["餐饮", "餐费", "聚餐", "招待"]),
            file_patterns=json.dumps([]),
            extractor_type="invoice",
            description="日常工作就餐与商务招待发票"
        ),
        CategoryRule(
            user_id=user_id,
            category_code="hotel",
            category_name="差旅住宿",
            folder_keywords=json.dumps(["住宿", "酒店", "机票", "火车", "车票"]),
            file_patterns=json.dumps([]),
            extractor_type="invoice",
            description="差旅酒店住宿与交通客运发票"
        ),
        CategoryRule(
            user_id=user_id,
            category_code="voucher",
            category_name="通用凭证附件",
            folder_keywords=json.dumps(["凭证", "收据", "小票", "水单", "截图"]),
            file_patterns=json.dumps([]),
            extractor_type="voucher",
            description="纯凭证文件存档(无需OCR识别)"
        )
    ]
    db.add_all(default_rules)
    db.commit()

@app.on_event("startup")
def startup_event():
    db = next(get_db())
    try:
        init_default_admin(db)
        admin = db.query(User).filter(User.username == "admin").first()
        if admin:
            init_user_default_rules(db, admin.id)
    finally:
        db.close()

# -------------------------------------------------------------
# 1. 认证与用户管理 (Auth & Multi-user)
# -------------------------------------------------------------
@app.post("/api/auth/register")
def register_user(req: RegisterRequest, db: Session = Depends(get_db)):
    """用户自主注册新账户（独立隔离数据空间）"""
    username = req.username.strip()
    if not username or len(username) < 3:
        raise HTTPException(status_code=400, detail="用户名长度不能少于 3 位")
    if not req.password or len(req.password) < 6:
        raise HTTPException(status_code=400, detail="密码长度不能少于 6 位")

    existing = db.query(User).filter(User.username == username).first()
    if existing:
        raise HTTPException(status_code=400, detail="用户名已被使用，请更换")

    pwd_hash, salt = hash_password(req.password)
    user = User(
        username=username,
        password_hash=pwd_hash,
        salt=salt,
        role="user",
        created_at=datetime.now()
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # 自动克隆一份系统默认分类规则
    init_user_default_rules(db, user.id)
    record_audit(db, user.id, "注册账号", "user", user.id, f"新用户 [{username}] 成功注册")

    token = create_access_token({"sub": user.username, "uid": user.id})
    return {
        "success": True,
        "message": "注册成功，已为您初始化专属报销规则空间",
        "token": token,
        "username": user.username
    }

@app.post("/api/auth/login")
def login(req: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == req.username).first()
    if not user:
        raise HTTPException(status_code=400, detail="用户名或密码错误")

    if not verify_password(req.password, user.salt, user.password_hash):
        raise HTTPException(status_code=400, detail="用户名或密码错误")

    user.last_login = datetime.now()
    db.commit()

    # 兜底确保该用户拥有默认规则
    init_user_default_rules(db, user.id)

    token = create_access_token({"sub": user.username, "uid": user.id})
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        max_age=86400 * 7,
        samesite="lax"
    )

    record_audit(db, user.id, "登录系统", "user", user.id, f"用户 [{user.username}] 登录成功")

    return {
        "success": True,
        "message": "登录成功",
        "token": token,
        "username": user.username
    }

@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie(key="access_token")
    return {"success": True, "message": "已退出登录"}

@app.get("/api/auth/me")
def get_me(user: User = Depends(get_current_user)):
    return {
        "username": user.username,
        "id": user.id,
        "role": user.role
    }

@app.post("/api/auth/change-password")
def change_password(req: PasswordChangeRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not verify_password(req.old_password, user.salt, user.password_hash):
        raise HTTPException(status_code=400, detail="原密码不正确")

    new_hash, new_salt = hash_password(req.new_password)
    user.password_hash = new_hash
    user.salt = new_salt
    db.commit()
    record_audit(db, user.id, "修改密码", "user", user.id, "管理员密码已成功重置")
    return {"success": True, "message": "密码修改成功"}

# -------------------------------------------------------------
# 2. 上传与解析 (ZIP 批量、单发票、纯凭证附件)
# -------------------------------------------------------------
@app.post("/api/upload/zip")
async def upload_zip(
    file: UploadFile = File(...),
    specified_month: Optional[str] = Form(None),
    specified_buyer: Optional[str] = Form(None),
    specified_memo: Optional[str] = Form(None),
    dup_strategy: str = Form("skip"), # skip, overwrite
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="只允许上传 .zip 格式的压缩文件")

    user_upload_dir = os.path.join(UPLOAD_DIR, f"user_{user.id}")
    os.makedirs(user_upload_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_filename = f"{timestamp}_{file.filename}"
    save_path = os.path.join(user_upload_dir, save_filename)

    with open(save_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    result = await process_zip_package(
        zip_path=save_path,
        user_id=user.id,
        db=db,
        specified_month=specified_month,
        specified_buyer=specified_buyer,
        specified_memo=specified_memo,
        dup_strategy=dup_strategy
    )
    return result

@app.post("/api/upload/single")
async def upload_single_invoice(
    file: UploadFile = File(...),
    category_code: str = Form(...),
    month: Optional[str] = Form(None),
    amount: Optional[float] = Form(None),
    invoice_no: Optional[str] = Form(None),
    invoice_date: Optional[str] = Form(None),
    seller: Optional[str] = Form(None),
    buyer: Optional[str] = Form(None),
    item_name: Optional[str] = Form(None),
    memo: Optional[str] = Form(None),
    dup_strategy: str = Form("skip"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in [".pdf", ".jpg", ".jpeg", ".png"]:
        raise HTTPException(status_code=400, detail="仅支持上传 PDF、JPG、JPEG、PNG 格式的发票文件")

    user_single_dir = os.path.join(UPLOAD_DIR, f"user_{user.id}", "single")
    os.makedirs(user_single_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_filename = f"{timestamp}_{file.filename}"
    save_path = os.path.join(user_single_dir, save_filename)

    with open(save_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    result = await process_single_invoice_file(
        file_path=save_path,
        original_filename=file.filename,
        category_code=category_code,
        user_id=user.id,
        db=db,
        specified_month=month,
        specified_amount=amount,
        specified_invoice_no=invoice_no,
        specified_invoice_date=invoice_date,
        specified_seller=seller,
        specified_buyer=buyer,
        specified_item_name=item_name,
        specified_memo=memo,
        dup_strategy=dup_strategy
    )
    return result

@app.post("/api/upload/voucher")
async def upload_voucher(
    file: UploadFile = File(...),
    category_code: str = Form("voucher"),
    month: Optional[str] = Form(None),
    amount: Optional[float] = Form(0.0),
    buyer: Optional[str] = Form(None),
    item_name: Optional[str] = Form(None),
    memo: Optional[str] = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """纯凭证/附件上传（无需 OCR 识别，文件限制 15MB 内）"""
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in [".pdf", ".jpg", ".jpeg", ".png", ".webp"]:
        raise HTTPException(status_code=400, detail="仅支持上传 PDF、JPG、JPEG、PNG、WEBP 格式的凭证文件")

    user_voucher_dir = os.path.join(UPLOAD_DIR, f"user_{user.id}", "vouchers")
    os.makedirs(user_voucher_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_filename = f"{timestamp}_{file.filename}"
    save_path = os.path.join(user_voucher_dir, save_filename)

    file_size = 0
    with open(save_path, "wb") as buffer:
        while chunk := await file.read(1024 * 1024):
            file_size += len(chunk)
            if file_size > 15 * 1024 * 1024:
                os.remove(save_path)
                raise HTTPException(status_code=400, detail="凭证文件大小超过 15MB 限制")
            buffer.write(chunk)

    result = await process_voucher_file(
        file_path=save_path,
        original_filename=file.filename,
        category_code=category_code,
        month=month,
        user_id=user.id,
        db=db,
        amount=amount,
        buyer=buyer or "",
        item_name=item_name or "",
        memo=memo or ""
    )
    return result

# -------------------------------------------------------------
# 3. 月度报销台账
# -------------------------------------------------------------
@app.get("/api/reports")
def get_reports(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    reports = db.query(ExpenseReport).filter(ExpenseReport.user_id == user.id).order_by(ExpenseReport.month.asc()).all()
    res = []
    for r in reports:
        invs = db.query(Invoice).filter(Invoice.user_id == user.id, Invoice.report_id == r.id).order_by(Invoice.category_code.asc(), Invoice.id.asc()).all()
        fuel_invs = [i for i in invs if i.category_code == "fuel"]
        toll_invs = [i for i in invs if i.category_code == "toll"]
        other_invs = [i for i in invs if i.category_code not in ["fuel", "toll"]]
        res.append({
            "id": r.id,
            "month": r.month,
            "toll_amount": r.toll_amount,
            "fuel_amount": r.fuel_amount,
            "other_amount": r.other_amount,
            "total_amount": r.total_amount,
            "submit_date": r.submit_date,
            "status": r.status,
            "memo": r.memo or "",
            "fuel_count": len(fuel_invs),
            "toll_count": len(toll_invs),
            "other_count": len(other_invs),
            "fuel_tickets": [{"id": i.id, "amount": i.amount, "invoice_no": i.invoice_no} for i in fuel_invs]
        })
    return res

@app.put("/api/reports/{report_id}")
def update_report(report_id: int, req: ExpenseReportUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    report = db.query(ExpenseReport).filter(ExpenseReport.id == report_id, ExpenseReport.user_id == user.id).first()
    if not report:
        raise HTTPException(status_code=404, detail="未找到该月份台账")

    old_month = report.month
    if req.month is not None and req.month.strip():
        new_month = req.month.strip()
        report.month = new_month
        if old_month != new_month:
            db.query(Invoice).filter(Invoice.user_id == user.id, Invoice.report_id == report_id).update({Invoice.month: new_month})

    if req.toll_amount is not None:
        report.toll_amount = float(req.toll_amount)
    if req.fuel_amount is not None:
        report.fuel_amount = float(req.fuel_amount)
    if req.other_amount is not None:
        report.other_amount = float(req.other_amount)

    report.total_amount = report.toll_amount + report.fuel_amount + report.other_amount

    if req.submit_date is not None:
        report.submit_date = req.submit_date
    if req.status is not None:
        if req.status not in ["待报销", "待付款", "已完成"]:
            raise HTTPException(status_code=400, detail="无效的状态，必须为：待报销、待付款、已完成")
        report.status = req.status
    if req.memo is not None:
        report.memo = req.memo

    db.commit()
    record_audit(db, user.id, "修改台账", "report", report.id, f"修改台账 [{report.month}]，金额 ¥{report.total_amount:.2f}，状态 {report.status}")
    return {"success": True, "message": "台账记录已更新"}

@app.delete("/api/reports/{report_id}")
def delete_report(report_id: int, delete_invoices: bool = True, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    report = db.query(ExpenseReport).filter(ExpenseReport.id == report_id, ExpenseReport.user_id == user.id).first()
    if not report:
        raise HTTPException(status_code=404, detail="未找到该月份台账")

    month_name = report.month
    if delete_invoices:
        db.query(Invoice).filter(Invoice.user_id == user.id, Invoice.report_id == report_id).delete()
    else:
        db.query(Invoice).filter(Invoice.user_id == user.id, Invoice.report_id == report_id).update({Invoice.report_id: None})

    db.delete(report)
    db.commit()
    record_audit(db, user.id, "删除台账", "report", report_id, f"删除月度台账 [{month_name}] 及下属发票凭证")
    return {"success": True, "message": "台账已删除"}

@app.post("/api/reports/clear-all")
def clear_all_reports(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    db.query(Invoice).filter(Invoice.user_id == user.id).delete()
    db.query(ExpenseReport).filter(ExpenseReport.user_id == user.id).delete()
    db.query(Batch).filter(Batch.user_id == user.id).delete()
    db.commit()
    record_audit(db, user.id, "清空数据", "report", 0, "一键清空个人所有月度台账与发票凭证明细")
    return {"success": True, "message": "个人名下所有台账与发票明细已清空"}

@app.put("/api/reports/{report_id}/status")
def update_report_status(report_id: int, req: StatusUpdateRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    report = db.query(ExpenseReport).filter(ExpenseReport.id == report_id, ExpenseReport.user_id == user.id).first()
    if not report:
        raise HTTPException(status_code=404, detail="未找到该月份台账")
    
    if req.status not in ["待报销", "待付款", "已完成"]:
        raise HTTPException(status_code=400, detail="无效的状态，必须为：待报销、待付款、已完成")

    report.status = req.status
    if req.submit_date is not None:
        report.submit_date = req.submit_date
    if req.memo is not None:
        report.memo = req.memo
    db.commit()
    record_audit(db, user.id, "更新状态", "report", report.id, f"更新台账 [{report.month}] 状态为 [{req.status}]，提交时间 [{req.submit_date}]")
    return {"success": True}

@app.get("/api/reports/export/excel")
def export_excel(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from urllib.parse import quote
    excel_stream = export_reports_to_excel(db, user_id=user.id)
    raw_name = f"费用报销汇总表_{user.username}.xlsx"
    encoded_name = quote(raw_name)
    headers = {
        "Content-Disposition": f"attachment; filename*=utf-8''{encoded_name}"
    }
    return StreamingResponse(
        excel_stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers
    )

# -------------------------------------------------------------
# 4. 发票凭证明细
# -------------------------------------------------------------
@app.get("/api/invoices")
def get_invoices(
    month: str = None,
    category: str = None,
    buyer: str = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    query = db.query(Invoice).filter(Invoice.user_id == user.id)
    if month:
        query = query.filter(Invoice.month == month)
    if category:
        query = query.filter(Invoice.category_code == category)
    if buyer and buyer.strip():
        query = query.filter(Invoice.buyer.contains(buyer.strip()))
    
    items = query.order_by(Invoice.month.desc(), Invoice.id.desc()).all()
    return items

@app.get("/api/invoices/buyers")
def get_invoice_buyers(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """获取该用户所有已录入发票中的不重复购买方/抬头列表"""
    rows = db.query(Invoice.buyer).filter(Invoice.user_id == user.id, Invoice.buyer != "").distinct().all()
    buyer_list = sorted(list(set(r[0].strip() for r in rows if r[0] and r[0].strip())))
    return buyer_list

@app.delete("/api/invoices/{invoice_id}")
def delete_invoice(invoice_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    inv = db.query(Invoice).filter(Invoice.id == invoice_id, Invoice.user_id == user.id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="未找到发票")
    report_id = inv.report_id
    inv_no = inv.invoice_no
    amt = inv.amount
    db.delete(inv)
    db.commit()

    if report_id:
        report = db.query(ExpenseReport).filter(ExpenseReport.id == report_id, ExpenseReport.user_id == user.id).first()
        if report:
            invoices_m = db.query(Invoice).filter(Invoice.user_id == user.id, Invoice.report_id == report_id).all()
            report.toll_amount = sum(i.amount for i in invoices_m if i.category_code == "toll")
            report.fuel_amount = sum(i.amount for i in invoices_m if i.category_code == "fuel")
            report.other_amount = sum(i.amount for i in invoices_m if i.category_code not in ["toll", "fuel"])
            report.total_amount = report.toll_amount + report.fuel_amount + report.other_amount
            db.commit()

    record_audit(db, user.id, "删除发票", "invoice", invoice_id, f"删除发票 [{inv_no}]，金额 ¥{amt:.2f}")
    return {"success": True}

@app.put("/api/invoices/{invoice_id}")
def update_invoice(invoice_id: int, req: InvoiceUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    inv = db.query(Invoice).filter(Invoice.id == invoice_id, Invoice.user_id == user.id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="未找到发票凭证")

    if req.amount is not None:
        inv.amount = float(req.amount)
    if req.invoice_no is not None:
        inv.invoice_no = req.invoice_no
    if req.invoice_date is not None:
        inv.invoice_date = req.invoice_date
    if req.seller is not None:
        inv.seller = req.seller
    if req.buyer is not None:
        inv.buyer = req.buyer
    if req.item_name is not None:
        inv.item_name = req.item_name
    if req.memo is not None:
        inv.memo = req.memo

    db.commit()

    if inv.report_id:
        report = db.query(ExpenseReport).filter(ExpenseReport.id == inv.report_id, ExpenseReport.user_id == user.id).first()
        if report:
            invoices_m = db.query(Invoice).filter(Invoice.user_id == user.id, Invoice.report_id == inv.report_id).all()
            report.toll_amount = sum(i.amount for i in invoices_m if i.category_code == "toll")
            report.fuel_amount = sum(i.amount for i in invoices_m if i.category_code == "fuel")
            report.other_amount = sum(i.amount for i in invoices_m if i.category_code not in ["toll", "fuel"])
            report.total_amount = report.toll_amount + report.fuel_amount + report.other_amount
            db.commit()

    record_audit(db, user.id, "修改发票", "invoice", invoice_id, f"修改发票 [{inv.invoice_no}]，最新金额 ¥{inv.amount:.2f}")
    return {"success": True, "message": "发票已更新"}

# -------------------------------------------------------------
# 5. 分类映射规则管理 (Category Rules) - 按用户隔离
# -------------------------------------------------------------
def format_rule_output(r: CategoryRule) -> dict:
    # 格式化多 input 数组输出
    def to_list(raw):
        if not raw:
            return []
        text = str(raw).strip()
        if text.startswith("[") and text.endswith("]"):
            try:
                return json.loads(text)
            except Exception:
                pass
        return [k.strip() for k in text.split(",") if k.strip()]

    # 解析策略中文说明字典
    extractor_names = {
        "invoice": "标准增值税发票识别（单票明细提取）",
        "toll_summary": "通行费汇总单识别（月度整笔提取）",
        "general_ocr": "通用票据 / AI视觉多模态识别",
        "voucher": "纯凭证文件存档（无需识别）"
    }

    kw_list = to_list(r.folder_keywords)
    pat_list = to_list(r.file_patterns)

    return {
        "id": r.id,
        "category_code": r.category_code,
        "category_name": r.category_name,
        "folder_keywords": ", ".join(kw_list),
        "folder_keywords_list": kw_list,
        "file_patterns": ", ".join(pat_list),
        "file_patterns_list": pat_list,
        "extractor_type": r.extractor_type,
        "extractor_type_cn": extractor_names.get(r.extractor_type, r.extractor_type),
        "description": r.description or "",
        "is_active": r.is_active
    }

@app.get("/api/rules")
def get_rules(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rules = db.query(CategoryRule).filter(CategoryRule.user_id == user.id).all()
    return [format_rule_output(r) for r in rules]

@app.post("/api/rules")
def create_rule(req: CategoryRuleCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    existing = db.query(CategoryRule).filter(
        CategoryRule.user_id == user.id,
        CategoryRule.category_code == req.category_code
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="该分类代码已存在，请勿重复定义")
    
    # 规范化转为 JSON 数组
    if isinstance(req.folder_keywords, list):
        kw_json = json.dumps([str(k).strip() for k in req.folder_keywords if str(k).strip()], ensure_ascii=False)
    else:
        kw_json = json.dumps([k.strip() for k in req.folder_keywords.split(",") if k.strip()], ensure_ascii=False)

    if isinstance(req.file_patterns, list):
        pat_json = json.dumps([str(p).strip() for p in req.file_patterns if str(p).strip()], ensure_ascii=False)
    elif req.file_patterns:
        pat_json = json.dumps([p.strip() for p in req.file_patterns.split(",") if p.strip()], ensure_ascii=False)
    else:
        pat_json = json.dumps([])

    rule = CategoryRule(
        user_id=user.id,
        category_code=req.category_code.strip(),
        category_name=req.category_name.strip(),
        folder_keywords=kw_json,
        file_patterns=pat_json,
        extractor_type=req.extractor_type or "invoice",
        description=req.description or ""
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    record_audit(db, user.id, "新增规则", "rule", rule.id, f"新增分类规则 [{rule.category_name} ({rule.category_code})]")
    return format_rule_output(rule)

@app.put("/api/rules/{rule_id}")
def update_rule(rule_id: int, req: CategoryRuleUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rule = db.query(CategoryRule).filter(CategoryRule.id == rule_id, CategoryRule.user_id == user.id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="未找到该规则")
    
    if req.category_name is not None:
        rule.category_name = req.category_name.strip()
    if req.folder_keywords is not None:
        if isinstance(req.folder_keywords, list):
            rule.folder_keywords = json.dumps([str(k).strip() for k in req.folder_keywords if str(k).strip()], ensure_ascii=False)
        else:
            rule.folder_keywords = json.dumps([k.strip() for k in req.folder_keywords.split(",") if k.strip()], ensure_ascii=False)
    if req.file_patterns is not None:
        if isinstance(req.file_patterns, list):
            rule.file_patterns = json.dumps([str(p).strip() for p in req.file_patterns if str(p).strip()], ensure_ascii=False)
        elif req.file_patterns:
            rule.file_patterns = json.dumps([p.strip() for p in req.file_patterns.split(",") if p.strip()], ensure_ascii=False)
        else:
            rule.file_patterns = json.dumps([])
    if req.extractor_type is not None:
        rule.extractor_type = req.extractor_type
    if req.is_active is not None:
        rule.is_active = req.is_active

    db.commit()
    record_audit(db, user.id, "修改规则", "rule", rule.id, f"修改分类规则 [{rule.category_name}]")
    return format_rule_output(rule)

@app.delete("/api/rules/{rule_id}")
def delete_rule(rule_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rule = db.query(CategoryRule).filter(CategoryRule.id == rule_id, CategoryRule.user_id == user.id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="未找到该规则")
    rule_name = rule.category_name
    db.delete(rule)
    db.commit()
    record_audit(db, user.id, "删除规则", "rule", rule_id, f"删除分类规则 [{rule_name}]")
    return {"success": True}

# -------------------------------------------------------------
# 6. AI 视觉多模态配置 (按用户级独立持久化)
# -------------------------------------------------------------
@app.get("/api/settings/ai")
def get_ai_settings(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    def get_val(k, default=""):
        us = db.query(UserSetting).filter(UserSetting.user_id == user.id, UserSetting.key == k).first()
        if us and us.value is not None:
            return us.value
        ss = db.query(SystemSetting).filter(SystemSetting.key == k).first()
        return ss.value if ss and ss.value is not None else default

    return {
        "enabled": get_val("ai_ocr_enabled", "true") == "true",
        "base_url": get_val("ai_base_url", "https://api.openai.com/v1"),
        "api_key": get_val("ai_api_key", ""),
        "model_name": get_val("ai_model_name", "gpt-4o-mini"),
        "system_prompt": get_val("ai_system_prompt", DEFAULT_SYSTEM_PROMPT),
        "default_system_prompt": DEFAULT_SYSTEM_PROMPT
    }

@app.post("/api/settings/ai")
def save_ai_settings(req: AISettingRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    def set_val(k, v):
        us = db.query(UserSetting).filter(UserSetting.user_id == user.id, UserSetting.key == k).first()
        if not us:
            us = UserSetting(user_id=user.id, key=k, value=str(v))
            db.add(us)
        else:
            us.value = str(v)

    set_val("ai_ocr_enabled", "true" if req.enabled else "false")
    set_val("ai_base_url", req.base_url.strip())
    set_val("ai_api_key", req.api_key.strip())
    set_val("ai_model_name", req.model_name.strip())
    if req.system_prompt is not None:
        set_val("ai_system_prompt", req.system_prompt.strip())
    db.commit()

    record_audit(db, user.id, "修改配置", "setting", 0, f"更新个人 AI 视觉识别配置 (模型: {req.model_name.strip()})")
    return {"success": True, "message": "个人 AI 设置已保存"}

@app.post("/api/settings/ai/models")
async def fetch_models(req: AIModelFetchRequest, user: User = Depends(get_current_user)):
    res = await fetch_available_models(req.base_url, req.api_key)
    return res

@app.post("/api/settings/ai/test")
async def test_ai_settings(req: AISettingRequest, user: User = Depends(get_current_user)):
    res = await test_ai_connection(req.base_url, req.api_key, req.model_name)
    return res

# -------------------------------------------------------------
# 7. 审计日志查询
# -------------------------------------------------------------
@app.get("/api/audit-logs")
def get_audit_logs(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    logs = db.query(AuditLog).filter(AuditLog.user_id == user.id).order_by(AuditLog.id.desc()).limit(50).all()
    return [
        {
            "id": l.id,
            "action": l.action,
            "entity_type": l.entity_type,
            "summary": l.summary,
            "created_at": l.created_at.strftime("%Y-%m-%d %H:%M:%S") if l.created_at else ""
        } for l in logs
    ]

# -------------------------------------------------------------
# 8. 静态资源与前端页面托管
# -------------------------------------------------------------
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/")
def read_root():
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return {"message": "Service is running, index.html not found"}
