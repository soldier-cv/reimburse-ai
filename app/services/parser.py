import os
import re
import zipfile
import shutil
from datetime import datetime
from sqlalchemy.orm import Session
from ..models import Batch, ExpenseReport, Invoice, CategoryRule, SystemSetting, Project
from .extractor import extract_toll_summary, extract_standard_invoice, is_valid_file_by_pattern
from .ai_service import extract_invoice_with_ai

def normalize_month(folder_or_filename: str) -> str:
    """从文件夹名或文件名中解析出规范月份，例如 202604_华旭东 -> 2026年04月"""
    # 优先匹配纯6位年月，如 202604、202605
    m = re.search(r'(20\d{2})(0[1-9]|1[0-2])(?!\d)', folder_or_filename)
    if m:
        year, month = m.group(1), m.group(2)
        return f"{year}年{month}月"
    # 兼容 2026-04、2026_04
    m2 = re.search(r'(20\d{2})[-_](0[1-9]|1[0-2])', folder_or_filename)
    if m2:
        year, month = m2.group(1), m2.group(2)
        return f"{year}年{month}月"
    return "未知月份"

def match_category(folder_name: str, rules: list[CategoryRule]) -> tuple[CategoryRule, str]:
    """根据文件夹名称匹配预设的分类规则"""
    name_lower = folder_name.lower()
    for rule in rules:
        if not rule.is_active:
            continue
        keywords = [k.strip().lower() for k in rule.folder_keywords.split(",") if k.strip()]
        for kw in keywords:
            if kw in name_lower:
                return rule, rule.category_code
    return None, "other"

async def process_zip_package(zip_path: str, db: Session, target_project_id: int = 1) -> dict:
    """
    处理上传的 ZIP 发票压缩包：
    1. 解压缩并处理编码
    2. 递归扫描识别各月份文件夹
    3. 根据分类规则表路由、过滤有效文件
    4. 执行本地提取或 AI OCR
    5. 结构化入库并更新月度台账
    """
    batch_name = os.path.basename(zip_path)
    extract_dir = os.path.splitext(zip_path)[0] + "_extracted"
    os.makedirs(extract_dir, exist_ok=True)

    batch = Batch(
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

    # 解压处理中文文件名
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for member in zf.infolist():
                # 尝试修复 Windows zip 常见 GBK 编码文件名乱码
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
        logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] 解压缩完成，开始扫描目录结构...")
    except Exception as e:
        batch.status = "error"
        batch.log_info = f"解压失败: {str(e)}"
        db.commit()
        return {"success": False, "message": str(e), "logs": logs}

    # 加载系统配置与分类规则
    rules = db.query(CategoryRule).filter(CategoryRule.is_active == True).all()
    ai_setting_enabled = db.query(SystemSetting).filter(SystemSetting.key == "ai_ocr_enabled").first()
    ai_base_url = db.query(SystemSetting).filter(SystemSetting.key == "ai_base_url").first()
    ai_api_key = db.query(SystemSetting).filter(SystemSetting.key == "ai_api_key").first()
    ai_model = db.query(SystemSetting).filter(SystemSetting.key == "ai_model_name").first()

    use_ai = ai_setting_enabled and ai_setting_enabled.value == "true" and ai_api_key and ai_api_key.value

    # 递归遍历目录
    parsed_files_count = 0
    total_batch_amount = 0.0
    affected_months = set()

    for root, dirs, files in os.walk(extract_dir):
        if not files:
            continue

        folder_name = os.path.basename(root)
        # 匹配分类规则
        matched_rule, cat_code = match_category(folder_name, rules)
        if not matched_rule:
            # 尝试上一级目录匹配
            parent_name = os.path.basename(os.path.dirname(root))
            matched_rule, cat_code = match_category(parent_name, rules)

        if not matched_rule:
            continue

        # 确定月份（从当前路径或父目录中识别）
        current_month = normalize_month(root)
        if current_month == "未知月份":
            current_month = normalize_month(batch_name)

        affected_months.add(current_month)

        for f in files:
            file_path = os.path.join(root, f)
            # 1. 有效文件名过滤检查
            if not is_valid_file_by_pattern(f, matched_rule.file_patterns):
                logs.append(f"  [跳过] 文件不符合分类过滤规则: {f}")
                continue

            # 只处理支持的文件格式
            ext = os.path.splitext(f)[1].lower()
            if ext not in [".pdf", ".jpg", ".jpeg", ".png"]:
                continue

            logs.append(f"  [解析中] 分类: {matched_rule.category_name} | 文件: {f}")

            # 2. 执行数据抽取
            extracted_data = {}
            if matched_rule.extractor_type == "toll_summary" and ext == ".pdf":
                extracted_data = extract_toll_summary(file_path, f)
            elif ext == ".pdf":
                extracted_data = extract_standard_invoice(file_path, f)

            # 3. 如果未能识别出有效金额且启用了 AI，则触发 AI OCR
            if (not extracted_data or extracted_data.get("amount", 0) <= 0) and use_ai:
                logs.append(f"    -> 本地提取金额为0，正在调用 AI OCR 视觉模型解析...")
                ai_res = await extract_invoice_with_ai(
                    file_path=file_path,
                    base_url=ai_base_url.value,
                    api_key=ai_api_key.value,
                    model_name=ai_model.value if ai_model else "gpt-4o-mini"
                )
                if ai_res and ai_res.get("amount", 0) > 0:
                    extracted_data = ai_res
                    logs.append(f"    -> AI OCR 成功解析金额: ¥{extracted_data['amount']:.2f}")

            amt = extracted_data.get("amount", 0.0)
            inv_no = extracted_data.get("invoice_no", "")

            # 4. 发票防重校验
            is_dup = False
            if inv_no:
                existing = db.query(Invoice).filter(Invoice.invoice_no == inv_no).first()
                if existing:
                    is_dup = True
                    logs.append(f"    [警告] 检测到发票号码 {inv_no} 已存在于库中（防重复报销标记）")

            # 5. 持久化单张发票记录
            invoice_record = Invoice(
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
                buyer=extracted_data.get("buyer", ""),
                item_name=extracted_data.get("item_name", ""),
                quantity=extracted_data.get("quantity", 0.0),
                unit_price=extracted_data.get("unit_price", 0.0),
                raw_filename=f,
                stored_path=file_path,
                recognition_method=extracted_data.get("method", "native_pdf"),
                is_duplicate=is_dup
            )
            db.add(invoice_record)
            parsed_files_count += 1
            total_batch_amount += amt
            logs.append(f"    -> 成功入库: 金额 ¥{amt:.2f}")

    db.commit()

    # 6. 重新聚合受影响月份的 ExpenseReport 月度台账
    for m in affected_months:
        if m == "未知月份":
            continue
        report = db.query(ExpenseReport).filter(
            ExpenseReport.month == m,
            ExpenseReport.project_id == target_project_id
        ).first()

        if not report:
            report = ExpenseReport(
                project_id=target_project_id,
                month=m,
                status="待报销"
            )
            db.add(report)
            db.commit()
            db.refresh(report)

        # 关联发票到报销单
        invoices_m = db.query(Invoice).filter(Invoice.month == m).all()
        toll_sum = sum(inv.amount for inv in invoices_m if inv.category_code == "toll")
        fuel_sum = sum(inv.amount for inv in invoices_m if inv.category_code == "fuel")
        other_sum = sum(inv.amount for inv in invoices_m if inv.category_code not in ["toll", "fuel"])

        report.toll_amount = toll_sum
        report.fuel_amount = fuel_sum
        report.other_amount = other_sum
        report.total_amount = toll_sum + fuel_sum + other_sum

        for inv in invoices_m:
            inv.report_id = report.id

    batch.file_count = parsed_files_count
    batch.total_amount = total_batch_amount
    batch.status = "completed"
    logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] 批次处理完毕！共提取 {parsed_files_count} 份有效票据，总金额 ¥{total_batch_amount:.2f}")
    batch.log_info = "\n".join(logs)

    db.commit()

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
    specified_month: str = None,
    specified_amount: float = None,
    specified_invoice_no: str = None,
    specified_invoice_date: str = None,
    specified_seller: str = None,
    specified_item_name: str = None,
    specified_memo: str = None,
    target_project_id: int = 1,
    db: Session = None
) -> dict:
    """
    处理单文件发票上传与录入：
    1. 根据用户指定的分类规则或文件格式执行数据抽取
    2. 支持用户手动覆盖或补充关键信息
    3. 自动推导归属月份并核算月度台账
    """
    rule = db.query(CategoryRule).filter(CategoryRule.category_code == category_code).first()
    category_name = rule.category_name if rule else "普通发票"
    extractor_type = rule.extractor_type if rule else "invoice"

    ext = os.path.splitext(original_filename)[1].lower()
    extracted_data = {}

    ai_setting_enabled = db.query(SystemSetting).filter(SystemSetting.key == "ai_ocr_enabled").first()
    ai_base_url = db.query(SystemSetting).filter(SystemSetting.key == "ai_base_url").first()
    ai_api_key = db.query(SystemSetting).filter(SystemSetting.key == "ai_api_key").first()
    ai_model = db.query(SystemSetting).filter(SystemSetting.key == "ai_model_name").first()
    use_ai = ai_setting_enabled and ai_setting_enabled.value == "true" and ai_api_key and ai_api_key.value

    # 1. 本地抽取
    if extractor_type == "toll_summary" and ext == ".pdf":
        extracted_data = extract_toll_summary(file_path, original_filename)
    elif ext == ".pdf":
        extracted_data = extract_standard_invoice(file_path, original_filename)

    # 2. AI OCR 兜底
    if (extracted_data.get("amount", 0.0) <= 0 or ext in [".jpg", ".jpeg", ".png"]) and use_ai:
        ai_res = await extract_invoice_with_ai(
            file_path,
            ai_base_url.value if ai_base_url else "https://api.openai.com/v1",
            ai_api_key.value,
            ai_model.value if ai_model else "gpt-4o-mini"
        )
        if ai_res.get("amount", 0.0) > 0:
            extracted_data = ai_res

    # 3. 字段合并与优先权判定（用户手动设置优先）
    final_amount = float(specified_amount) if (specified_amount is not None and specified_amount > 0) else extracted_data.get("amount", 0.0)
    final_no = specified_invoice_no.strip() if specified_invoice_no else extracted_data.get("invoice_no", "")
    final_date = specified_invoice_date.strip() if specified_invoice_date else extracted_data.get("invoice_date", "")
    final_seller = specified_seller.strip() if specified_seller else extracted_data.get("seller", "")
    final_item = specified_item_name.strip() if specified_item_name else extracted_data.get("item_name", "")
    final_memo = specified_memo.strip() if specified_memo else ""
    method = "manual_override" if specified_amount is not None else extracted_data.get("method", "manual")

    # 4. 确定归属月份
    month = None
    if specified_month and specified_month.strip():
        month = specified_month.strip()
    else:
        # 从文件名尝试解析
        month = normalize_month(original_filename)
        if month == "未知月份":
            # 尝试从开票日期提取 (如 2026-08-15)
            if final_date and len(final_date) >= 7:
                m_date = re.search(r'(20\d{2})[-/年.](0[1-9]|1[0-2])', final_date)
                if m_date:
                    month = f"{m_date.group(1)}年{m_date.group(2)}月"
        if not month or month == "未知月份":
            month = datetime.now().strftime("%Y年%m月")

    # 5. 防重校验
    is_dup = False
    if final_no:
        existing = db.query(Invoice).filter(Invoice.invoice_no == final_no).first()
        if existing:
            is_dup = True

    # 6. 保存发票记录
    invoice = Invoice(
        raw_filename=original_filename,
        stored_path=file_path,
        month=month,
        category_code=category_code,
        category_name=category_name,
        invoice_no=final_no or f"SINGLE_{datetime.now().strftime('%Y%m%d%H%M%S')}",
        invoice_date=final_date,
        amount=final_amount,
        seller=final_seller,
        item_name=final_item,
        is_duplicate=is_dup,
        recognition_method=method,
        memo=final_memo
    )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)

    # 7. 更新或创建对应的月度费用台账
    report = db.query(ExpenseReport).filter(
        ExpenseReport.project_id == target_project_id,
        ExpenseReport.month == month
    ).first()

    if not report:
        report = ExpenseReport(
            project_id=target_project_id,
            month=month,
            toll_amount=0.0,
            fuel_amount=0.0,
            other_amount=0.0,
            total_amount=0.0,
            status="待报销"
        )
        db.add(report)
        db.commit()
        db.refresh(report)

    invoice.report_id = report.id

    # 重新汇总计算该月金额
    invoices_m = db.query(Invoice).filter(Invoice.report_id == report.id).all()
    report.toll_amount = sum(i.amount for i in invoices_m if i.category_code == "toll")
    report.fuel_amount = sum(i.amount for i in invoices_m if i.category_code == "fuel")
    report.other_amount = sum(i.amount for i in invoices_m if i.category_code not in ["toll", "fuel"])
    report.total_amount = report.toll_amount + report.fuel_amount + report.other_amount
    db.commit()

    return {
        "success": True,
        "message": "单文件发票处理并入库成功",
        "invoice_id": invoice.id,
        "month": month,
        "category_name": category_name,
        "amount": final_amount,
        "invoice_no": final_no,
        "is_duplicate": is_dup,
        "recognition_method": method
    }
