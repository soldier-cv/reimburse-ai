import os
import shutil
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from .database import engine, Base, get_db, BASE_DIR
from .models import User, Project, CategoryRule, Batch, ExpenseReport, Invoice, SystemSetting
from .schemas import (
    LoginRequest, PasswordChangeRequest, CategoryRuleCreate, CategoryRuleUpdate,
    StatusUpdateRequest, AISettingRequest, ExpenseReportUpdate, InvoiceUpdate,
    AIModelFetchRequest
)
from .services.auth import (
    get_current_user, create_access_token, verify_password, hash_password, init_default_admin
)
from .services.parser import process_zip_package, process_single_invoice_file
from .services.exporter import export_reports_to_excel
from .services.ai_service import test_ai_connection, fetch_available_models, DEFAULT_SYSTEM_PROMPT

# 初始化数据库结构
Base.metadata.create_all(bind=engine)

app = FastAPI(title="智能发票报销与多项目费用管理系统", version="1.0.0")

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

# -------------------------------------------------------------
# 初始化基础数据
# -------------------------------------------------------------
@app.on_event("startup")
def startup_event():
    db = next(get_db())
    try:
        # 1. 默认超级管理员
        init_default_admin(db)

        # 2. 默认业务主体
        if not db.query(Project).first():
            default_proj = Project(
                name="通用报销主体",
                plate_number="",
                tax_no="",
                is_default=True
            )
            db.add(default_proj)
            db.commit()

        # 3. 默认分类与文件夹映射规则
        if not db.query(CategoryRule).first():
            default_rules = [
                CategoryRule(
                    category_code="toll",
                    category_name="通行费",
                    folder_keywords="通行费,通行,etc,highway",
                    file_patterns="*票据*.pdf",
                    extractor_type="toll_summary",
                    description="ETC收费公路通行费官方汇总单(只提取带票据的单据以防金额翻倍)"
                ),
                CategoryRule(
                    category_code="fuel",
                    category_name="燃油费",
                    folder_keywords="燃油发票,燃油,加油,oil,gas",
                    file_patterns="*.pdf,*.jpg,*.jpeg,*.png",
                    extractor_type="invoice",
                    description="中国石化/中国石油等成品油电子普通发票"
                ),
                CategoryRule(
                    category_code="catering",
                    category_name="餐饮接待",
                    folder_keywords="餐饮,餐费,商务接待,招待,eat",
                    file_patterns="*.pdf,*.jpg,*.jpeg,*.png",
                    extractor_type="invoice",
                    description="业务用餐与接待发票"
                ),
                CategoryRule(
                    category_code="hotel",
                    category_name="差旅住宿",
                    folder_keywords="住宿,酒店,旅馆,hotel",
                    file_patterns="*.pdf,*.jpg,*.jpeg,*.png",
                    extractor_type="invoice",
                    description="出差酒店住宿发票"
                )
            ]
            db.add_all(default_rules)
            db.commit()

        # 4. 默认 AI 设置占位
        if not db.query(SystemSetting).filter(SystemSetting.key == "ai_ocr_enabled").first():
            db.add_all([
                SystemSetting(key="ai_ocr_enabled", value="true", description="是否启用AI OCR多模态识别"),
                SystemSetting(key="ai_base_url", value="https://api.openai.com/v1", description="OpenAI兼容API地址"),
                SystemSetting(key="ai_api_key", value="", description="API Key"),
                SystemSetting(key="ai_model_name", value="gpt-4o-mini", description="模型名称"),
                SystemSetting(key="ai_system_prompt", value=DEFAULT_SYSTEM_PROMPT, description="AI视觉发票识别系统提示词")
            ])
            db.commit()
    finally:
        db.close()

# -------------------------------------------------------------
# 1. 认证接口 (Auth)
# -------------------------------------------------------------
@app.post("/api/auth/login")
def login(req: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == req.username).first()
    if not user or not verify_password(req.password, user.salt, user.password_hash):
        raise HTTPException(status_code=400, detail="用户名或密码错误")

    token = create_access_token({"sub": user.username})
    user.last_login = datetime.now()
    db.commit()

    # 设置 HttpOnly Cookie（安全防护）
    response.set_cookie(
        key="expense_token",
        value=token,
        httponly=True,
        max_age=30 * 86400,
        samesite="lax"
    )

    return {
        "success": True,
        "token": token,
        "username": user.username
    }

@app.get("/api/auth/me")
def get_me(user: User = Depends(get_current_user)):
    return {
        "username": user.username,
        "last_login": user.last_login
    }

@app.post("/api/auth/change-password")
def change_password(req: PasswordChangeRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not verify_password(req.old_password, user.salt, user.password_hash):
        raise HTTPException(status_code=400, detail="旧密码不正确")
    
    new_hash, new_salt = hash_password(req.new_password)
    user.password_hash = new_hash
    user.salt = new_salt
    db.commit()
    return {"success": True, "message": "密码修改成功，请牢记新密码"}

@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie("expense_token")
    return {"success": True}

# -------------------------------------------------------------
# 2. 压缩包上传与解析
# -------------------------------------------------------------
@app.post("/api/upload/zip")
async def upload_zip(
    file: UploadFile = File(...),
    project_id: int = Form(1),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="请上传 .zip 格式的压缩文件")

    # 保存上传文件
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_filename = f"{timestamp}_{file.filename}"
    save_path = os.path.join(UPLOAD_DIR, save_filename)

    with open(save_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # 异步处理压缩包
    result = await process_zip_package(save_path, db, target_project_id=project_id)
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
    project_id: int = Form(1),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in [".pdf", ".jpg", ".jpeg", ".png"]:
        raise HTTPException(status_code=400, detail="仅支持上传 PDF、JPG、JPEG、PNG 格式的发票文件")

    # 单文件保存目录
    single_dir = os.path.join(UPLOAD_DIR, "single")
    os.makedirs(single_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_filename = f"{timestamp}_{file.filename}"
    save_path = os.path.join(single_dir, save_filename)

    with open(save_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    result = await process_single_invoice_file(
        file_path=save_path,
        original_filename=file.filename,
        category_code=category_code,
        specified_month=month,
        specified_amount=amount,
        specified_invoice_no=invoice_no,
        specified_invoice_date=invoice_date,
        specified_seller=seller,
        specified_buyer=buyer,
        specified_item_name=item_name,
        specified_memo=memo,
        target_project_id=project_id,
        db=db
    )
    return result

# -------------------------------------------------------------
# 3. 月度报销台账
# -------------------------------------------------------------
@app.get("/api/reports")
def get_reports(project_id: int = 1, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    reports = db.query(ExpenseReport).filter(ExpenseReport.project_id == project_id).order_by(ExpenseReport.month.asc()).all()
    res = []
    for r in reports:
        # 获取该月的发票列表
        invs = db.query(Invoice).filter(Invoice.report_id == r.id).order_by(Invoice.category_code.asc(), Invoice.id.asc()).all()
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
            "memo": r.memo,
            "fuel_tickets": [{"id": i.id, "amount": i.amount, "invoice_no": i.invoice_no, "date": i.invoice_date} for i in fuel_invs],
            "toll_tickets": [{"id": i.id, "amount": i.amount, "item_name": i.item_name} for i in toll_invs],
            "other_tickets": [{"id": i.id, "amount": i.amount, "category": i.category_name, "seller": i.seller} for i in other_invs]
        })
    return res

@app.put("/api/reports/{report_id}")
def update_report(report_id: int, req: ExpenseReportUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    report = db.query(ExpenseReport).filter(ExpenseReport.id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="未找到该月份台账")

    old_month = report.month
    if req.month is not None and req.month.strip():
        new_month = req.month.strip()
        report.month = new_month
        if old_month != new_month:
            db.query(Invoice).filter(Invoice.report_id == report_id).update({Invoice.month: new_month})

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
    return {"success": True, "message": "台账记录已更新"}

@app.delete("/api/reports/{report_id}")
def delete_report(report_id: int, delete_invoices: bool = True, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    report = db.query(ExpenseReport).filter(ExpenseReport.id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="未找到该月份台账")

    if delete_invoices:
        db.query(Invoice).filter(Invoice.report_id == report_id).delete()
    else:
        db.query(Invoice).filter(Invoice.report_id == report_id).update({Invoice.report_id: None})

    db.delete(report)
    db.commit()
    return {"success": True, "message": "台账已删除"}

@app.post("/api/reports/clear-all")
def clear_all_reports(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    db.query(Invoice).delete()
    db.query(ExpenseReport).delete()
    db.query(Batch).delete()
    db.commit()
    return {"success": True, "message": "所有台账与发票明细已清空"}

@app.put("/api/reports/{report_id}/status")
def update_report_status(report_id: int, req: StatusUpdateRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    report = db.query(ExpenseReport).filter(ExpenseReport.id == report_id).first()
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
    return {"success": True}

@app.get("/api/reports/export/excel")
def export_excel(project_id: int = 1, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from urllib.parse import quote
    excel_stream = export_reports_to_excel(db, project_id=project_id)
    raw_name = "费用报销汇总表.xlsx"
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
    query = db.query(Invoice)
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
    """获取所有已录入发票中的不重复购买方/抬头列表"""
    rows = db.query(Invoice.buyer).filter(Invoice.buyer != "").distinct().all()
    buyer_list = sorted(list(set(r[0].strip() for r in rows if r[0] and r[0].strip())))
    return buyer_list

@app.delete("/api/invoices/{invoice_id}")
def delete_invoice(invoice_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    inv = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="未找到发票")
    report_id = inv.report_id
    db.delete(inv)
    db.commit()

    # 重新核算报销单金额
    if report_id:
        report = db.query(ExpenseReport).filter(ExpenseReport.id == report_id).first()
        if report:
            invoices_m = db.query(Invoice).filter(Invoice.report_id == report_id).all()
            report.toll_amount = sum(i.amount for i in invoices_m if i.category_code == "toll")
            report.fuel_amount = sum(i.amount for i in invoices_m if i.category_code == "fuel")
            report.other_amount = sum(i.amount for i in invoices_m if i.category_code not in ["toll", "fuel"])
            report.total_amount = report.toll_amount + report.fuel_amount + report.other_amount
            db.commit()

    return {"success": True}

@app.put("/api/invoices/{invoice_id}")
def update_invoice(invoice_id: int, req: InvoiceUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    inv = db.query(Invoice).filter(Invoice.id == invoice_id).first()
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
        report = db.query(ExpenseReport).filter(ExpenseReport.id == inv.report_id).first()
        if report:
            invoices_m = db.query(Invoice).filter(Invoice.report_id == inv.report_id).all()
            report.toll_amount = sum(i.amount for i in invoices_m if i.category_code == "toll")
            report.fuel_amount = sum(i.amount for i in invoices_m if i.category_code == "fuel")
            report.other_amount = sum(i.amount for i in invoices_m if i.category_code not in ["toll", "fuel"])
            report.total_amount = report.toll_amount + report.fuel_amount + report.other_amount
            db.commit()

    return {"success": True, "message": "发票已更新"}

# -------------------------------------------------------------
# 5. 分类映射规则管理 (Category Rules)
# -------------------------------------------------------------
@app.get("/api/rules")
def get_rules(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.query(CategoryRule).all()

@app.post("/api/rules")
def create_rule(req: CategoryRuleCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    existing = db.query(CategoryRule).filter(CategoryRule.category_code == req.category_code).first()
    if existing:
        raise HTTPException(status_code=400, detail="分类代码已存在")
    
    rule = CategoryRule(
        category_code=req.category_code,
        category_name=req.category_name,
        folder_keywords=req.folder_keywords,
        file_patterns=req.file_patterns or "",
        extractor_type=req.extractor_type or "invoice",
        description=req.description or ""
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule

@app.put("/api/rules/{rule_id}")
def update_rule(rule_id: int, req: CategoryRuleUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rule = db.query(CategoryRule).filter(CategoryRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="未找到该规则")
    
    if req.category_name is not None:
        rule.category_name = req.category_name
    if req.folder_keywords is not None:
        rule.folder_keywords = req.folder_keywords
    if req.file_patterns is not None:
        rule.file_patterns = req.file_patterns
    if req.extractor_type is not None:
        rule.extractor_type = req.extractor_type
    if req.is_active is not None:
        rule.is_active = req.is_active

    db.commit()
    return rule

@app.delete("/api/rules/{rule_id}")
def delete_rule(rule_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rule = db.query(CategoryRule).filter(CategoryRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="未找到该规则")
    db.delete(rule)
    db.commit()
    return {"success": True}

# -------------------------------------------------------------
# 6. AI OCR API 设置
# -------------------------------------------------------------
@app.get("/api/settings/ai")
def get_ai_settings(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    def get_val(k, default=""):
        s = db.query(SystemSetting).filter(SystemSetting.key == k).first()
        return s.value if s else default

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
        s = db.query(SystemSetting).filter(SystemSetting.key == k).first()
        if not s:
            s = SystemSetting(key=k, value=str(v))
            db.add(s)
        else:
            s.value = str(v)

    set_val("ai_ocr_enabled", "true" if req.enabled else "false")
    set_val("ai_base_url", req.base_url.strip())
    set_val("ai_api_key", req.api_key.strip())
    set_val("ai_model_name", req.model_name.strip())
    if req.system_prompt is not None:
        set_val("ai_system_prompt", req.system_prompt.strip())
    db.commit()
    return {"success": True, "message": "AI 设置已保存"}

@app.post("/api/settings/ai/models")
async def fetch_models(req: AIModelFetchRequest, user: User = Depends(get_current_user)):
    res = await fetch_available_models(req.base_url, req.api_key)
    return res

@app.post("/api/settings/ai/test")
async def test_ai_settings(req: AISettingRequest, user: User = Depends(get_current_user)):
    res = await test_ai_connection(req.base_url, req.api_key, req.model_name)
    return res

# -------------------------------------------------------------
# 7. 静态资源与前端页面托管
# -------------------------------------------------------------
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/")
def read_root():
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return {"message": "Service is running, index.html not found"}
