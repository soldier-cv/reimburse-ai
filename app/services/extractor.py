import re
import fnmatch
from pypdf import PdfReader

def is_valid_file_by_pattern(filename: str, patterns_str: str) -> bool:
    """
    检查文件名是否符合该分类的有效文件名模式。
    如果未设置模式（为空），默认所有文件均有效；
    如果设置了多个模式（逗号分隔，如 '*票据*.pdf, *.png'），只要匹配其中任意一个即可。
    """
    if not patterns_str or not patterns_str.strip():
        return True
    
    filename_lower = filename.lower()
    patterns = [p.strip().lower() for p in patterns_str.split(",") if p.strip()]
    for p in patterns:
        if fnmatch.fnmatch(filename_lower, p):
            return True
    return False

def extract_pdf_text(pdf_path: str) -> str:
    """提取 PDF 文件的全部纯文本"""
    try:
        reader = PdfReader(pdf_path)
        all_text = []
        for page in reader.pages:
            t = page.extract_text()
            if t:
                all_text.append(t)
        return "\n".join(all_text)
    except Exception as e:
        return ""

def extract_toll_summary(pdf_path: str, filename: str) -> dict:
    """
    提取收费公路通行费电子票据汇总单
    汇总单第一行或抬头即有：苏U88M32 交易金额 ¥818.63
    """
    txt = extract_pdf_text(pdf_path)
    res = {
        "invoice_no": "",
        "invoice_date": "",
        "amount": 0.0,
        "net_amount": 0.0,
        "tax_amount": 0.0,
        "seller": "全国收费公路通行费",
        "buyer": "",
        "item_name": "通行费电子票据汇总",
        "quantity": 0.0,
        "unit_price": 0.0,
        "method": "native_pdf"
    }

    if not txt:
        return res

    # 交易金额
    amt_m = re.search(r'交易金额\s*[¥￥]?\s*([\d\.]+)', txt)
    if amt_m:
        res["amount"] = float(amt_m.group(1))
    else:
        # fallback 查找 （小写）¥818.63
        tot_m = re.search(r'（小写）[¥￥]?\s*([\d\.]+)', txt)
        if tot_m:
            res["amount"] = float(tot_m.group(1))

    # 开票申请日期
    d_m = re.search(r'开票申请日期[：:\s]*([\d\-]+)', txt)
    if d_m:
        res["invoice_date"] = d_m.group(1)

    # 购买方
    b_m = re.search(r'购买方名称\s*([^\n\r\s]+)', txt)
    if b_m:
        res["buyer"] = b_m.group(1).strip()

    # 税额
    tax_m = re.search(r'税额[^\n\r]*?[¥￥]?\s*([\d\.]+)', txt)
    if tax_m:
        res["tax_amount"] = float(tax_m.group(1))

    # 行程数量
    trip_m = re.search(r'共(\d+)段行程', txt)
    if trip_m:
        res["quantity"] = float(trip_m.group(1))

    # 车牌
    plate_m = re.search(r'车牌号码\s*([^\s]+)', txt)
    plate = plate_m.group(1) if plate_m else ""
    res["item_name"] = f"通行费 (车牌:{plate}, 行程:{int(res['quantity'])}段)"

    return res

def extract_standard_invoice(pdf_path: str, filename: str) -> dict:
    """
    提取增值税普通发票 / 电子发票 / 成品油发票
    """
    txt = extract_pdf_text(pdf_path)
    res = {
        "invoice_no": "",
        "invoice_date": "",
        "amount": 0.0,
        "net_amount": 0.0,
        "tax_amount": 0.0,
        "seller": "",
        "buyer": "",
        "item_name": "燃油/商品服务",
        "quantity": 0.0,
        "unit_price": 0.0,
        "method": "native_pdf"
    }

    # 1. 尝试从文件名直接提取发票号与金额（很多发票文件名自带）
    # 如: 发票_26327000000801686170_328.61元.pdf 或 dzfp_26327000001041284306_...
    fn_no_m = re.search(r'(\d{20}|\d{18}|\d{12}|\d{8})', filename)
    if fn_no_m:
        res["invoice_no"] = fn_no_m.group(1)
    
    fn_amt_m = re.search(r'_([\d\.]+)元', filename)
    if fn_amt_m:
        res["amount"] = float(fn_amt_m.group(1))

    if not txt:
        return res

    # 2. 从文本提取发票号码
    fp_m = re.search(r'发票号码[：:\s]*(\d+)', txt)
    if fp_m:
        res["invoice_no"] = fp_m.group(1)

    # 3. 开票日期
    date_m = re.search(r'开票日期[：:\s]*(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}-\d{2}-\d{2})', txt)
    if date_m:
        res["invoice_date"] = date_m.group(1).replace(" ", "")

    # 4. 价税合计（小写）
    total_m = re.search(r'（小写）[¥￥]?\s*([\d\.]+)', txt)
    if not total_m:
        total_m = re.search(r'\(小写\)[¥￥]?\s*([\d\.]+)', txt)
    if not total_m:
        total_m = re.search(r'小写[）\)]\s*[¥￥]?\s*([\d\.]+)', txt)
    if total_m:
        res["amount"] = float(total_m.group(1))

    # 5. 不含税金额与税额
    sum_m = re.search(r'合\s*计\s*[¥￥]?\s*([\d\.]+)\s*[¥￥]?\s*([\d\.]+)', txt)
    if sum_m:
        res["net_amount"] = float(sum_m.group(1))
        res["tax_amount"] = float(sum_m.group(2))

    # 6. 购买方与销售方
    buyer_m = re.search(r'购\s*买\s*方[\s\S]*?名\s*称[：:\s]*([^\n\r]+)', txt)
    if buyer_m:
        res["buyer"] = buyer_m.group(1).strip()

    seller_m = re.search(r'销\s*售\s*方[\s\S]*?名\s*称[：:\s]*([^\n\r]+)', txt)
    if seller_m:
        res["seller"] = seller_m.group(1).strip()

    # 7. 货物名称与规格 (如 *汽油*92号车用汽油)
    lines = [l.strip() for l in txt.split("\n") if l.strip()]
    for l in lines:
        if "*" in l:
            res["item_name"] = l
            nums = re.findall(r'(\d+\.\d+)', l)
            if len(nums) >= 2:
                res["quantity"] = float(nums[0])
                res["unit_price"] = float(nums[1])
            break

    return res
