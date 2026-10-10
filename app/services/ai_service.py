import os
import json
import base64
import httpx
from typing import Optional

DEFAULT_SYSTEM_PROMPT = """你是一个专业的财税发票与费用单据识别助手。请识别并提取输入发票中的核心财务字段，必须仅输出标准 JSON 格式对象，不要包含任何 markdown 标记、解释或其他文字。
字段格式定义如下：
{
  "invoice_no": "发票号码或凭证汇总单号",
  "invoice_date": "开票日期(YYYY-MM-DD)",
  "amount": 123.45,
  "net_amount": 100.00,
  "tax_amount": 23.45,
  "seller": "销售方企业名称",
  "buyer": "购买方企业名称/发票抬头",
  "item_name": "主要商品、服务或劳务名称",
  "quantity": 0.0,
  "unit_price": 0.0
}"""

async def extract_invoice_with_ai(
    file_path: str,
    base_url: str,
    api_key: str,
    model_name: str = "gpt-4o-mini",
    system_prompt: Optional[str] = None
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

    effective_prompt = (system_prompt.strip() if system_prompt and system_prompt.strip() else DEFAULT_SYSTEM_PROMPT)

    # 规范化 endpoint
    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"

    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": effective_prompt},
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

async def fetch_available_models(base_url: str, api_key: str) -> dict:
    """
    通过 OpenAI 兼容协议从服务商获取可用模型列表。
    请求 GET /models 接口并解析返回的模型清单。
    """
    if not base_url or not api_key:
        return {"success": False, "message": "接口地址和密钥不能为空", "models": []}

    url = base_url.rstrip("/")
    if url.endswith("/chat/completions"):
        url = url[:-len("/chat/completions")]
    if not url.endswith("/models"):
        url += "/models"

    headers = {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json"
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                raw_models = data.get("data", [])
                model_ids = []
                for m in raw_models:
                    if isinstance(m, dict) and "id" in m:
                        model_ids.append(m["id"])
                    elif isinstance(m, str):
                        model_ids.append(m)

                # 将带有常用视觉与多模态能力的模型优先排在前列
                def priority_key(name):
                    n = name.lower()
                    if any(k in n for k in ["4o", "vl", "vision", "gemini-1.5", "claude-3"]):
                        return (0, n)
                    return (1, n)

                sorted_models = sorted(model_ids, key=priority_key)
                return {
                    "success": True,
                    "message": f"成功获取到 {len(sorted_models)} 个可用模型",
                    "models": sorted_models
                }
            else:
                return {
                    "success": False,
                    "message": f"服务商接口返回异常状态码: HTTP {resp.status_code}",
                    "models": []
                }
    except Exception as e:
        return {
            "success": False,
            "message": f"获取模型列表失败: {str(e)}",
            "models": []
        }
