import io
import pytest
from fastapi.testclient import TestClient

# 最小合法 1x1 透明 PNG 字节流
MINIMAL_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc`\x00\x00\x00"
    b"\x02\x00\x01H\xaf\xa4q\x00\x00\x00\x00IEND\xaeB`\x82"
)

def test_full_regression_smoke_flow(client: TestClient):
    """
    验收项 6 全链路回归冒烟测试：
    1. 注册/登录用户
    2. 新建分类规则
    3. 上传纯凭证文件
    4. 查询发票明细确认包含该文件且 has_file 为 True
    5. 通过签名短链安全下载发票文件原件
    6. 更新关联的月度台账金额与数据
    7. 流转月度台账审核状态
    8. 导出月度台账 Excel 电子表格并验证 content-type
    9. 验证个人规则列表非空
    10. 删除该笔发票明细并确认清理
    """
    # 1. 注册与登录
    username = "regression_user"
    password = "RegressionPassword123!"

    reg_resp = client.post("/api/auth/register", json={"username": username, "password": password})
    assert reg_resp.status_code == 200, f"注册失败: {reg_resp.text}"

    login_resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert login_resp.status_code == 200, f"登录失败: {login_resp.text}"
    token = login_resp.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 2. 新建分类规则
    rule_payload = {
        "category_code": "smoke_cat",
        "category_name": "冒烟测试分类",
        "folder_keywords": ["冒烟", "smoke"],
        "file_patterns": ["*.png"],
        "extractor_type": "voucher",
        "description": "自动化烟雾测试专用规则"
    }
    rule_create_resp = client.post("/api/rules", json=rule_payload, headers=headers)
    assert rule_create_resp.status_code == 200, f"创建规则失败: {rule_create_resp.text}"

    # 3. 上传纯凭证文件
    voucher_files = {
        "file": ("smoke_voucher.png", MINIMAL_PNG_BYTES, "image/png")
    }
    voucher_data = {
        "category_code": "voucher",
        "month": "2026-10",
        "amount": "99.5",
        "buyer": "自动化测试报销抬头",
        "item_name": "冒烟测试物料凭证",
        "memo": "烟雾测试纯凭证归档"
    }
    upload_resp = client.post("/api/upload/voucher", files=voucher_files, data=voucher_data, headers=headers)
    assert upload_resp.status_code == 200, f"凭证上传失败: {upload_resp.text}"

    # 4. GET /api/invoices 验证明细存在且 has_file 为 True
    invoices_resp = client.get("/api/invoices?month=2026-10", headers=headers)
    assert invoices_resp.status_code == 200
    inv_list = invoices_resp.json()
    assert len(inv_list) >= 1, "未查询到上传的凭证明细记录"
    target_inv = inv_list[0]
    assert target_inv["has_file"] is True, f"has_file 标记异常: {target_inv}"
    invoice_id = target_inv["id"]
    report_id = target_inv["report_id"]

    # 5. 走签名链接安全取到该发票原件 (200)
    sign_resp = client.get(f"/api/files/sign?kind=invoice&id={invoice_id}", headers=headers)
    assert sign_resp.status_code == 200
    download_url = sign_resp.json()["url"]

    # 使用无鉴权客户端直接请求签名 URL
    client.cookies.clear()
    download_file_resp = client.get(download_url)
    assert download_file_resp.status_code == 200
    assert len(download_file_resp.content) == len(MINIMAL_PNG_BYTES)

    # 6. PUT /api/reports/{id} 修改台账金额
    update_rep_payload = {
        "toll_amount": 100.0,
        "fuel_amount": 200.0,
        "other_amount": 99.5
    }
    rep_update_resp = client.put(f"/api/reports/{report_id}", json=update_rep_payload, headers=headers)
    assert rep_update_resp.status_code == 200

    # 7. PUT /api/reports/{id}/status 修改台账状态与提交时间
    status_payload = {
        "status": "待付款",
        "submit_date": "2026-10-16",
        "memo": "冒烟流程状态流转完毕"
    }
    rep_status_resp = client.put(f"/api/reports/{report_id}/status", json=status_payload, headers=headers)
    assert rep_status_resp.status_code == 200

    # 8. GET /api/reports/export/excel 导出 Excel (200 且 xlsx 格式)
    export_resp = client.get("/api/reports/export/excel", headers=headers)
    assert export_resp.status_code == 200
    assert "spreadsheetml" in export_resp.headers.get("content-type", "")
    assert len(export_resp.content) > 0

    # 9. GET /api/rules 验证规则非空
    rules_resp = client.get("/api/rules", headers=headers)
    assert rules_resp.status_code == 200
    rules_data = rules_resp.json()
    assert len(rules_data) > 0

    # 10. DELETE /api/invoices/{id} 删除明细
    del_resp = client.delete(f"/api/invoices/{invoice_id}", headers=headers)
    assert del_resp.status_code == 200
    assert del_resp.json()["success"] is True

    # 再次查询发票列表验证已删除
    after_del_resp = client.get("/api/invoices?month=2026-10", headers=headers)
    assert len(after_del_resp.json()) == 0
