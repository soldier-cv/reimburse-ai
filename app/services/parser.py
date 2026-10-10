import os
import re
import json
import zipfile
import shutil
from datetime import datetime
from sqlalchemy.orm import Session
from ..models import Batch, ExpenseReport, Invoice, CategoryRule, UserSetting, SystemSetting, AuditLog
from .extractor import extract_toll_summary, extract_standard_invoice, is_valid_file_by_pattern
from .ai_service import extract_invoice_with_ai

def normalize_month(folder_or_filename: str) -> str:
    """从文件夹名或文件名中解析出规范月份，例如 202604_华旭东 -> 2026年04月"""
    m = re.search(r'(20\d{2})(0[1-9]|1[0-2])(?!\d)', folder_or_filename)
    if m:
        year, month = m.group(1), m.group(2)
        return f"{year}年{month}月"
    m2 = re.search(r'(20\d{2})[-_](0[1-9]|1[0-2])', folder_or_filename)
    if m2:
        year, month = m2.group(1), m2.group(2)
        return f"{year}年{month}月"
    return "未知月份"

def parse_keywords(kw_raw) -> list[str]:
    """安全解析目录关键词列表（支持 JSON 数组或逗号隔开）"""
    if not kw_raw:
        return []
    text = str(kw_raw).strip()
    if text.startswith("[") and text.endswith("]"):
        try:
            arr = json.loads(text)
            return [str(k).strip().lower() for k in arr if str(k).strip()]
        except Exception:
            return [k.strip().lower() for k in text.strip("[]").split(",") if k.strip()]
    return [k.strip().lower() for k in text.split(",") if k.strip()]

def match_category(folder_name: str, rules: list[CategoryRule]) -> tuple[CategoryRule, str]:
    """根据文件夹名称匹配用户的分类规则"""
    name_lower = folder_name.lower()
    for rule in rules:
        if not rule.is_active:
            continue
        keywords = parse_keywords(rule.folder_keywords)
        for kw in keywords:
            if kw in name_lower:
                return rule, rule.category_code
    return None, "other"

def get_user_ai_config(db: Session, user_id: int) -> dict:
    """获取用户的 AI 视觉接口个性化配置（优先从用户配置读取）"""
    def get_val(key, default=""):
        us = db.query(UserSetting).filter(UserSetting.user_id == user_id, UserSetting.key == key).first()
        if us and us.value:
            return us.value
        ss = db.query(SystemSetting).filter(SystemSetting.key == key).first()
        return ss.value if ss and ss.value else default

    enabled = get_val("ai_ocr_enabled", "true") == "true"
    base_url = get_val("ai_base_url", "https://api.openai.com/v1")
    api_key = get_val("ai_api_key", "")
    model_name = get_val("ai_model_name", "gpt-4o-mini")
    sys_prompt = get_val("ai_system_prompt", None)

    return {
        "enabled": enabled,
        "base_url": base_url,
        "api_key": api_key,
        "model_name": model_name,
        "system_prompt": sys_prompt,
        "can_call": enabled and bool(api_key.strip())
    }

def record_audit(db: Session, user_id: int, action: str, entity_type: str, entity_id: int, summary: str, details: str = None):
    """记录操作审计日志"""
    try:
        log = AuditLog(
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            summary=summary,
            details=details,
            created_at=datetime.now()
        )
        db.add(log)
        db.commit()
    except Exception:
        db.rollback()

async def process_zip_package(
    zip_path: str,
    user_id: int,
    db: Session,
    specified_month: str = None,
    specified_buyer: str = None,
    specified_memo: str = None,
    dup_strategy: str = "skip" # skip (跳过), overwrite (覆盖更新), record_only (仅记录不计金额)
) -> dict:
    """
    处理上传的 ZIP 发票压缩包（按用户隔离）
    """
    batch_name = os.path.basename(zip_path)
    extract_dir = os.path.splitext(zip_path)[0] + "_extracted"
    os.makedirs(extract_dir, exist_ok=True)

    batch = Batch(
        user_id=user_id,
        filename=batch_name,
        upload_time=datetime.now(),
        status="processing",
        log_info=""
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)

    logs = []
    logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] 开始解压缩: {batch_name}")
    if specified_month:
        logs.append(f"  [批次参数] 预设默认月份: {specified_month}")
    if specified_buyer:
        logs.append(f"  [批次参数] 预设默认抬头: {specified_buyer}")
    if dup_strategy:
        strategy_desc = {"skip": "跳过重复", "overwrite": "覆盖已有", "record_only": "保留记录但防重"}.get(dup_strategy, dup_strategy)
        logs.append(f"  [批次参数] 重复发票处理策略: {strategy_desc}")

    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for member in zf.infolist():
                try:
                    filename = member.filename.encode('cp437').decode('gbk')
                except Exception:
                    filename = member.filename
                
                target_path = os.path.join(extract_dir, filename)
                if member.is_dir():
                    os.makedirs(target_path, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(target_path), exist_ok=True)
                    with zf.open(member) as source, open(target_path, "wb") as target:
                        shutil.copyfileobj(source, target)
        logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] 解压完成，开始按用户规则分类扫描...")
    except Exception as e:
        batch.status = "error"
        batch.log_info = f"解压失败: {str(e)}"
        db.commit()
        return {"success": False, "message": str(e), "logs": logs}

    rules = db.query(CategoryRule).filter(CategoryRule.user_id == user_id, CategoryRule.is_active == True).all()
    ai_cfg = get_user_ai_config(db, user_id)

    parsed_files_count = 0
    total_batch_amount = 0.0
    affected_months = set()

    for root, dirs, files in os.walk(extract_dir):
        if not files:
            continue

        folder_name = os.path.basename(root)
        matched_rule, cat_code = match_category(folder_name, rules)
        if not matched_rule:
            parent_name = os.path.basename(os.path.dirname(root))
            matched_rule, cat_code = match_category(parent_name, rules)

        if not matched_rule:
            continue

        # 确定月份：用户指定优先，其次路径推导，最后以压缩包名推导
        current_month = specified_month.strip() if (specified_month and specified_month.strip()) else normalize_month(root)
        if current_month == "未知月份":
            current_month = normalize_month(batch_name)

        affected_months.add(current_month)

        for f in files:
            file_path = os.path.join(root, f)
            if not is_valid_file_by_pattern(f, matched_rule.file_patterns):
                logs.append(f"  [跳过] 文件不符合分类过滤模式: {f}")
                continue

            ext = os.path.splitext(f)[1].lower()
            if ext not in [".pdf", ".jpg", ".jpeg", ".png"]:
                continue

            logs.append(f"  [解析中] 分类: {matched_rule.category_name} | 文件: {f}")

            extracted_data = {}
            if matched_rule.extractor_type == "toll_summary" and ext == ".pdf":
                extracted_data = extract_toll_summary(file_path, f)
            elif ext == ".pdf":
                extracted_data = extract_standard_invoice(file_path, f)

            if (not extracted_data or extracted_data.get("amount", 0) <= 0) and ai_cfg["can_call"]:
                logs.append(f"    -> 本地提取金额为0，正在调用 AI 视觉多模态接口解析...")
                ai_res = await extract_invoice_with_ai(
                    file_path=file_path,
                    base_url=ai_cfg["base_url"],
                    api_key=ai_cfg["api_key"],
                    model_name=ai_cfg["model_name"],
                    system_prompt=ai_cfg["system_prompt"]
                )
                if ai_res and ai_res.get("amount", 0) > 0:
                    extracted_data = ai_res
                    logs.append(f"    -> AI OCR 解析成功，金额: ¥{extracted_data['amount']:.2f}")

            amt = extracted_data.get("amount", 0.0)
            inv_no = extracted_data.get("invoice_no", "")
            final_buyer = specified_buyer.strip() if specified_buyer else extracted_data.get("buyer", "")
            final_memo = specified_memo.strip() if specified_memo else ""

            # 防重处理
            existing_inv = None
            if inv_no:
                existing_inv = db.query(Invoice).filter(Invoice.user_id == user_id, Invoice.invoice_no == inv_no).first()

            if existing_inv:
                if dup_strategy == "skip":
                    logs.append(f"    [跳过重复] 发票号 {inv_no} 已存在，策略为跳过重复")
                    continue
                elif dup_strategy == "overwrite":
                    existing_inv.amount = amt
                    existing_inv.invoice_date = extracted_data.get("invoice_date", "")
                    existing_inv.month = current_month
                    existing_inv.category_code = cat_code
                    existing_inv.category_name = matched_rule.category_name
                    existing_inv.seller = extracted_data.get("seller", "")
                    if final_buyer:
                        existing_inv.buyer = final_buyer
                    if final_memo:
                        existing_inv.memo = final_memo
                    existing_inv.raw_filename = f
                    existing_inv.stored_path = file_path
                    existing_inv.recognition_method = extracted_data.get("method", "native_pdf")
                    logs.append(f"    [覆盖已有] 发票号 {inv_no} 已存在，已覆盖更新最新金额与信息: ¥{amt:.2f}")
                    parsed_files_count += 1
                    total_batch_amount += amt
                    continue

            is_dup = bool(existing_inv)
            invoice_record = Invoice(
                user_id=user_id,
                batch_id=batch.id,
                category_code=cat_code,
                category_name=matched_rule.category_name,
                month=current_month,
                invoice_no=inv_no or f"TEMP_{batch.id}_{parsed_files_count}",
                invoice_date=extracted_data.get("invoice_date", ""),
                amount=amt,
                net_amount=extracted_data.get("net_amount", 0.0),
                tax_amount=extracted_data.get("tax_amount", 0.0),
                seller=extracted_data.get("seller", ""),
                buyer=final_buyer,
                item_name=extracted_data.get("item_name", ""),
                quantity=extracted_data.get("quantity", 0.0),
                unit_price=extracted_data.get("unit_price", 0.0),
                raw_filename=f,
                stored_path=file_path,
                recognition_method=extracted_data.get("method", "native_pdf"),
                is_duplicate=is_dup,
                memo=final_memo
            )
            db.add(invoice_record)
            parsed_files_count += 1
            total_batch_amount += amt
            logs.append(f"    -> 录入成功: 金额 ¥{amt:.2f}")

    db.commit()

    # 重新聚合受影响月份的 ExpenseReport
    today_str = datetime.now().strftime("%Y-%m-%d")
    for m in affected_months:
        if m == "未知月份":
            continue
        report = db.query(ExpenseReport).filter(
            ExpenseReport.user_id == user_id,
            ExpenseReport.month == m
        ).first()

        if not report:
            report = ExpenseReport(
                user_id=user_id,
                month=m,
                submit_date=today_str,
                status="待报销"
            )
            db.add(report)
            db.commit()
            db.refresh(report)
        elif not report.submit_date:
            report.submit_date = today_str

        invoices_m = db.query(Invoice).filter(Invoice.user_id == user_id, Invoice.month == m).all()
        report.toll_amount = sum(inv.amount for inv in invoices_m if inv.category_code == "toll")
        report.fuel_amount = sum(inv.amount for inv in invoices_m if inv.category_code == "fuel")
        report.other_amount = sum(inv.amount for inv in invoices_m if inv.category_code not in ["toll", "fuel"])
        report.total_amount = report.toll_amount + report.fuel_amount + report.other_amount

        for inv in invoices_m:
            inv.report_id = report.id

    batch.file_count = parsed_files_count
    batch.total_amount = total_batch_amount
    batch.status = "completed"
    logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] 批次处理完毕！共入库 {parsed_files_count} 份有效票据，总金额 ¥{total_batch_amount:.2f}")
    batch.log_info = "\n".join(logs)
    db.commit()

    record_audit(db, user_id, "批量导入", "batch", batch.id, f"上传 ZIP 压缩包 [{batch_name}]，成功处理 {parsed_files_count} 张发票，总额 ¥{total_batch_amount:.2f}")

    return {
        "success": True,
        "batch_id": batch.id,
        "parsed_count": parsed_files_count,
        "total_amount": total_batch_amount,
        "logs": logs
    }

async def process_single_invoice_file(
    file_path: str,
    original_filename: str,
    category_code: str,
    user_id: int,
    db: Session,
    specified_month: str = None,
    specified_amount: float = None,
    specified_invoice_no: str = None,
    specified_invoice_date: str = None,
    specified_seller: str = None,
    specified_buyer: str = None,
    specified_item_name: str = None,
    specified_memo: str = None,
    dup_strategy: str = "skip"
) -> dict:
    """
    处理单文件发票上传与录入（按用户隔离）
    """
    rule = db.query(CategoryRule).filter(CategoryRule.user_id == user_id, CategoryRule.category_code == category_code).first()
    category_name = rule.category_name if rule else "普通发票"
    extractor_type = rule.extractor_type if rule else "invoice"

    ext = os.path.splitext(original_filename)[1].lower()
    extracted_data = {}
    ai_cfg = get_user_ai_config(db, user_id)

    if extractor_type == "toll_summary" and ext == ".pdf":
        extracted_data = extract_toll_summary(file_path, original_filename)
    elif ext == ".pdf":
        extracted_data = extract_standard_invoice(file_path, original_filename)

    if (extracted_data.get("amount", 0.0) <= 0 or ext in [".jpg", ".jpeg", ".png"]) and ai_cfg["can_call"]:
        ai_res = await extract_invoice_with_ai(
            file_path,
            ai_cfg["base_url"],
            ai_cfg["api_key"],
            ai_cfg["model_name"],
            system_prompt=ai_cfg["system_prompt"]
        )
        if ai_res.get("amount", 0.0) > 0:
            extracted_data = ai_res

    final_amount = float(specified_amount) if (specified_amount is not None and specified_amount > 0) else extracted_data.get("amount", 0.0)
    final_no = specified_invoice_no.strip() if specified_invoice_no else extracted_data.get("invoice_no", "")
    final_date = specified_invoice_date.strip() if specified_invoice_date else extracted_data.get("invoice_date", "")
    final_seller = specified_seller.strip() if specified_seller else extracted_data.get("seller", "")
    final_buyer = specified_buyer.strip() if specified_buyer else extracted_data.get("buyer", "")
    final_item = specified_item_name.strip() if specified_item_name else extracted_data.get("item_name", "")
    final_memo = specified_memo.strip() if specified_memo else ""
    method = "manual_override" if specified_amount is not None else extracted_data.get("method", "manual")

    month = None
    if specified_month and specified_month.strip():
        month = specified_month.strip()
    else:
        month = normalize_month(original_filename)
        if month == "未知月份" and final_date and len(final_date) >= 7:
            m_date = re.search(r'(20\d{2})[-/年.](0[1-9]|1[0-2])', final_date)
            if m_date:
                month = f"{m_date.group(1)}年{m_date.group(2)}月"
        if not month or month == "未知月份":
            month = datetime.now().strftime("%Y年%m月")

    existing_inv = None
    if final_no:
        existing_inv = db.query(Invoice).filter(Invoice.user_id == user_id, Invoice.invoice_no == final_no).first()

    if existing_inv and dup_strategy == "overwrite":
        existing_inv.amount = final_amount
        existing_inv.invoice_date = final_date
        existing_inv.month = month
        existing_inv.category_code = category_code
        existing_inv.category_name = category_name
        existing_inv.seller = final_seller
        existing_inv.buyer = final_buyer
        existing_inv.item_name = final_item
        existing_inv.memo = final_memo
        existing_inv.raw_filename = original_filename
        existing_inv.stored_path = file_path
        existing_inv.recognition_method = method
        db.commit()
        invoice = existing_inv
    else:
        is_dup = bool(existing_inv)
        invoice = Invoice(
            user_id=user_id,
            raw_filename=original_filename,
            stored_path=file_path,
            month=month,
            category_code=category_code,
            category_name=category_name,
            invoice_no=final_no or f"SINGLE_{datetime.now().strftime('%Y%m%d%H%M%S')}",
            invoice_date=final_date,
            amount=final_amount,
            seller=final_seller,
            buyer=final_buyer,
            item_name=final_item,
            is_duplicate=is_dup,
            recognition_method=method,
            memo=final_memo
        )
        db.add(invoice)
        db.commit()
        db.refresh(invoice)

    today_str = datetime.now().strftime("%Y-%m-%d")
    report = db.query(ExpenseReport).filter(
        ExpenseReport.user_id == user_id,
        ExpenseReport.month == month
    ).first()

    if not report:
        report = ExpenseReport(
            user_id=user_id,
            month=month,
            toll_amount=0.0,
            fuel_amount=0.0,
            other_amount=0.0,
            total_amount=0.0,
            submit_date=today_str,
            status="待报销"
        )
        db.add(report)
        db.commit()
        db.refresh(report)
    elif not report.submit_date:
        report.submit_date = today_str

    invoice.report_id = report.id
    db.commit()

    invoices_m = db.query(Invoice).filter(Invoice.user_id == user_id, Invoice.month == month).all()
    report.toll_amount = sum(i.amount for i in invoices_m if i.category_code == "toll")
    report.fuel_amount = sum(i.amount for i in invoices_m if i.category_code == "fuel")
    report.other_amount = sum(i.amount for i in invoices_m if i.category_code not in ["toll", "fuel"])
    report.total_amount = report.toll_amount + report.fuel_amount + report.other_amount
    for inv in invoices_m:
        inv.report_id = report.id
    db.commit()

    record_audit(db, user_id, "录入发票", "invoice", invoice.id, f"单文件录入发票 [{original_filename}]，金额 ¥{final_amount:.2f}，归属 {month}")

    return {
        "success": True,
        "message": "发票已成功录入并生成台账记录",
        "invoice_id": invoice.id,
        "month": month,
        "category_name": category_name,
        "amount": final_amount,
        "invoice_no": final_no,
        "buyer": final_buyer,
        "is_duplicate": invoice.is_duplicate,
        "recognition_method": method
    }

async def process_voucher_file(
    file_path: str,
    original_filename: str,
    category_code: str,
    month: str,
    user_id: int,
    db: Session,
    amount: float = 0.0,
    buyer: str = "",
    item_name: str = "",
    memo: str = ""
) -> dict:
    """
    处理纯凭证/附件上传（无需 OCR，直接归档入库）
    """
    rule = db.query(CategoryRule).filter(CategoryRule.user_id == user_id, CategoryRule.category_code == category_code).first()
    category_name = rule.category_name if rule else "凭证附件"

    final_month = month.strip() if month and month.strip() else datetime.now().strftime("%Y年%m月")
    final_amount = float(amount) if amount is not None and float(amount) > 0 else 0.0

    voucher_no = f"VOUCHER_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    invoice = Invoice(
        user_id=user_id,
        raw_filename=original_filename,
        stored_path=file_path,
        month=final_month,
        category_code=category_code,
        category_name=category_name,
        invoice_no=voucher_no,
        invoice_date=datetime.now().strftime("%Y-%m-%d"),
        amount=final_amount,
        seller="凭证上传",
        buyer=buyer.strip() if buyer else "",
        item_name=item_name.strip() if item_name else "日常费用凭证",
        is_duplicate=False,
        recognition_method="voucher",
        memo=memo.strip() if memo else "纯凭证附件归档"
    )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)

    # 关联并更新对应月份台账
    today_str = datetime.now().strftime("%Y-%m-%d")
    report = db.query(ExpenseReport).filter(
        ExpenseReport.user_id == user_id,
        ExpenseReport.month == final_month
    ).first()

    if not report:
        report = ExpenseReport(
            user_id=user_id,
            month=final_month,
            toll_amount=0.0,
            fuel_amount=0.0,
            other_amount=0.0,
            total_amount=0.0,
            submit_date=today_str,
            status="待报销"
        )
        db.add(report)
        db.commit()
        db.refresh(report)

    invoice.report_id = report.id
    db.commit()

    invoices_m = db.query(Invoice).filter(Invoice.user_id == user_id, Invoice.month == final_month).all()
    report.toll_amount = sum(i.amount for i in invoices_m if i.category_code == "toll")
    report.fuel_amount = sum(i.amount for i in invoices_m if i.category_code == "fuel")
    report.other_amount = sum(i.amount for i in invoices_m if i.category_code not in ["toll", "fuel"])
    report.total_amount = report.toll_amount + report.fuel_amount + report.other_amount
    for inv in invoices_m:
        inv.report_id = report.id
    db.commit()

    record_audit(db, user_id, "凭证上传", "voucher", invoice.id, f"上传凭证附件 [{original_filename}]，金额 ¥{final_amount:.2f}，归属 {final_month}")

    return {
        "success": True,
        "message": "凭证已成功归档入库",
        "invoice_id": invoice.id,
        "month": final_month,
        "category_name": category_name,
        "amount": final_amount,
        "recognition_method": "voucher"
    }
