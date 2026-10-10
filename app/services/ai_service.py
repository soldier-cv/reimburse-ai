import os
import json
import base64
import httpx
from typing import Optional

async def extract_invoice_with_ai(
    file_path: str,
    base_url: str,
    api_key: str,
    model_name: str = "gpt-4o-mini"
) -> dict:
    """
    通过 OpenAI 兼容的多模态视觉 API 进行发票识别与结构化提取。
    支持 Qwen-VL, GPT-4o-mini, Gemini, GLM-4V 等任意兼容接口。
    """
    if not api_key or not base_url:
        return {}

    # 读取文件转为 Base64
    ext = os.path.splitext(file_path)[1].lower()
    mime = "application/pdf" if ext == ".pdf" else "image/jpeg"
    if ext == ".png":
        mime = "image/png"

    try:
        with open(file_path, "rb") as f:
            b64_data = base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        return {}

    system_prompt = (
        "你是一个专业的财税发票识别助手。请识别并提取输入发票中的核心字段，"
        "必须仅输出标准的 JSON 格式对象，不要包含任何 markdown 标记、解释或其他文字。\n"
        "字段定义如下：\n"
        "{\n"
        '  "invoice_no": "发票号码字符串",\n'
        '  "invoice_date": "开票日期(YYYY-MM-DD)",\n'
        '  "amount": 123.45,  // 价税合计总金额(浮点数)\n'
        '  "net_amount": 100.00,  // 不含税金额\n'
        '  "tax_amount": 23.45,  // 税额\n'
        '  "seller": "销售方公司名称",\n'
        '  "buyer": "购买方公司名称",\n'
        '  "item_name": "主要商品或服务劳务名称",\n'
        '  "quantity": 0.0, // 数量(如加油升数)\n'
        '  "unit_price": 0.0 // 单价\n'
        "}"
    )

    # 规范化 endpoint
    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"

    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "请识别并提取这张发票的详细信息，以 JSON 格式输出："},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime};base64,{b64_data}"
                        }
                    }
                ]
            }
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"} if "gpt" in model_name.lower() else None
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                # 清洗可能的 markdown 包裹
                content = content.replace("```json", "").replace("```", "").strip()
                res = json.loads(content)
                res["method"] = "ai_ocr"
                return res
    except Exception as e:
        pass

    return {}

async def test_ai_connection(base_url: str, api_key: str, model_name: str) -> dict:
    """测试 AI API 接口连通性"""
    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"

    payload = {
        "model": model_name,
        "messages": [
            {"role": "user", "content": "Hello, please reply with 'pong'"}
        ],
        "max_tokens": 10
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 200:
                return {"success": True, "message": "连接成功！AI 接口正常响应。"}
            else:
                return {"success": False, "message": f"连接失败，状态码: {resp.status_code}, 内容: {resp.text[:200]}"}
    except Exception as e:
        return {"success": False, "message": f"连接发生异常: {str(e)}"}
