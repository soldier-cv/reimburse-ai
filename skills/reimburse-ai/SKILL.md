---
name: reimburse-ai
description: Use when 采集报销票据、维护个人对公应收台账、提交批次、核销到账或导出财务包。
version: 1.0.0
author: Hermes coding agent
license: MIT
metadata:
  hermes:
    tags: [reimburse, receivables, invoices, capture, ledger, export, agent-api]
    related_skills: []
---

# reimburse-ai：个人对公应收台账

这是一份发给外部 Agent 的 HTTP 接口说明。没有 MCP，没有 tools/list，没有会话：用 `ragt_` Bearer 令牌直接调 REST 接口，作用范围仅限签发该令牌的用户账本。

> 技能真源在仓库 `skills/reimburse-ai/SKILL.md`，随代码版本化。下列更新地址要等宿主建好符号链接后才可用；在此之前不要假定线上已有这份技能。

## When to Use

在「公司欠我的钱」这条链路上用它，而不是拿它做个人消费记账：

- 采集发票、客票、小票、支付截图、工资条等凭证，核对后入账
- 维护公司、类别、应收事项，把待提交的应收归集为批次并提交
- 按分摊记录到账（含部分到账），或把工资到账截图核销到已有工资应收
- 导出 Excel、合并票据 PDF、财务包 ZIP，并重新下载历史导出

**不要用**：个人消费、预算、账户余额；改密码、管账号、签发或吊销令牌、读写 AI 密钥；发票税务验真、退款红冲、多币种汇率。这些要么被接口拒绝，要么本服务根本不做。

## 更新与版本

| 项 | 值 |
|---|---|
| 当前版本 | 见 frontmatter 的 `version` |
| 对齐应用 | `3.2.0` 及以上 |
| 更新源 | `http://100.64.0.88:8088/.well-known/skills/index.json` |
| 安装根 | `http://100.64.0.88:8088/.well-known/skills/reimburse-ai/` |

技能版本与应用版本相互独立：`patch` 修措辞不改结论；`minor` 新增章节或接口；`major` 推翻既有结论（需重读全文）。改 Agent 可见的接口或规则时，仓库里的本文件必须与代码同一提交更新。

```bash
# 查版本（宿主完成分发后）
curl -s http://100.64.0.88:8088/.well-known/skills/index.json | python3 -c "
import json,sys
s=[x for x in json.load(sys.stdin)['skills'] if x['name']=='reimburse-ai'][0]
print(s['version'], s['updated_at'])"

# 升级（单文件）
curl -fsSL http://100.64.0.88:8088/.well-known/skills/reimburse-ai/SKILL.md -o <你的技能目录>/reimburse-ai/SKILL.md
```

**1.0.0 对齐应用 3.2.0**：引入用户级 `ragt_` Agent 令牌与 `capture` / `ledger` / `export` 三个范围；令牌管理只在 Web「设置 -> Agent 令牌」，Agent 不能签发或吊销令牌。

---

## 1. 服务是什么

这是个人对公应收台账，不是报销审批系统，也不是个人记账。

- **应收事项 `Receivable` 是唯一账本事实**；发票、截图、聊天记录、回单只是附件。
- 一笔应收只属于一个批次；批次是向同一家公司的一次提交。
- 工资与报销共用应收模型，用 `kind` 区分：`reimbursement` / `payroll` / `other`。
- 单币种人民币。全库金额以整数分（`*_cents`）存储。

### 金额单位（务必分清）

| 位置 | 单位 | 说明 |
|---|---|---|
| 请求 JSON / 表单里的 `amount` | **元** | 数字或字符串均可，例如 `128`、`"128.00"`、`"¥128.00"` |
| 响应里的 `amount`、`received`、`remaining`、`outstanding`、`allocated` 等 | **元** | 浮点数，保留两位 |
| 响应里的 `amount_cents`、`received_cents`、`remaining_cents` 及看板 `*_cents` | **分** | 整数；计算与对账优先用这些字段 |

**不要把「分」传进请求的 `amount`。** 整数 `128` 会被当成 128 元（12800 分）。要对账或累加，读响应里的 `*_cents`。

---

## 2. 地址

生产由 Woodpecker 按 `v*` 标签部署到与技能注册同一台宿主的 **8092** 端口。内网基址：

```text
http://100.64.0.88:8092
```

如果用户给了别的基址（本机、其它端口、反代域名），用用户给的，不要写死上面的地址。先验证服务是否可用：

```bash
BASE="${REIMBURSE_AI_BASE:-http://100.64.0.88:8092}"
curl -sS "$BASE/health"
# → {"status":"ok","time":"..."}
```

`GET /health` 与首页 `GET /` 不需要令牌。业务接口一律走 `/api/...`。默认关闭 CORS 与 `/docs`，从浏览器跨域调用不会成功，请用 HTTP 客户端直连。

---

## 3. 鉴权

用户在 Web「设置 -> Agent 令牌」为自己签发令牌。明文前缀为 `ragt_`，**只在签发时显示一次**，库中只存 SHA-256 哈希与可见前缀。每个 Agent 使用自己的令牌，不要共用；令牌始终以签发人身份访问，看不到其他用户的账本。建议为故事测试或自动化单独建一个成员账号，由该成员自己签发令牌，避免和好账本混在一起。系统不会向生产库自动写入测试用户。

```http
Authorization: Bearer ragt_...
```

- 令牌只放在 `Authorization` 头，**禁止放进 query**。
- 先调用 `GET /api/auth/me` 确认身份与范围：

```bash
H="Authorization: Bearer $RAGT_TOKEN"
curl -sS "$BASE/api/auth/me" -H "$H"
# → {"id":..., "username":"...", "role":"user"|"admin",
#    "auth_method":"agent", "token_name":"...", "scopes":["capture","ledger","export"]}
```

`auth_method` 为 `agent` 时才会带 `token_name` 和 `scopes`。会话 JWT 登录时 `auth_method` 为 `session`，没有这两个字段。

签发时未传 `scopes`（或传空）则默认三个范围全部开通。每位用户最多保留 20 个未吊销令牌；可设 `expires_at`（`YYYY-MM-DD` 或 ISO 8601，日期会被视为当天 23:59:59）。吊销、停用或过期的令牌返回 **401**（文案与无效令牌相同，不区分原因）。停用后再启用，原明文仍有效；**吊销不可恢复**，只能重新签发。

Agent 的 POST / PUT / PATCH / DELETE 按令牌 ID 限流：每分钟 60 次，超出返回 **429**「请求过于频繁，请稍后再试」。

令牌管理接口（签发、列举、改名、启用/停用、吊销）只接受浏览器会话 JWT，Agent 调用一律 403。不要在自动化里用 Agent 令牌自己给自己发新令牌。

---

## 4. 权限范围

缺少所需范围返回 **403**，文案为「当前 Agent 令牌缺少「采集/台账/导出」权限」。路径按前缀匹配。

| 范围 | 覆盖路径 | 能做什么 |
|---|---|---|
| `capture` | `/api/capture` 及其子路径 | 上传采集、列/改/删草稿、重试识别、看原件与缩略图、确认入账、工资到账截图核销 |
| `ledger` | `/api/companies`、`/api/categories`、`/api/receivables`、`/api/batches`、`/api/payments`、`/api/dashboard`、`/api/attachments`、`/api/audit-logs`；以及 `GET /api/files/sign?kind=attachment`（或未传 / 非 `export` 的 `kind`） | 公司与类别、应收台账、附件、批次、到账、看板、本用户审计日志、为附件签发下载链接 |
| `export` | `/api/export` 及其子路径；以及 `GET /api/files/sign?kind=export` | Excel、合并 PDF、财务包、附件压缩包、导出记录与下载、为导出文件签发下载链接 |

不要求业务范围、已鉴权即可访问：

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/auth/me` | 校验令牌，返回 `auth_method`、令牌名称与范围 |

`GET /api/files/download/{token}` 不走 Bearer，只用签名链接（见第 5.6 节）。

### Agent 禁止访问（一律 403）

文案：「Agent 令牌不能用于账号管理、令牌管理或 AI 密钥设置」。即使令牌属于管理员也一样。

| 前缀或路径 | 原因 |
|---|---|
| `/api/admin` | 成员账号管理 |
| `/api/system/init-admin` | 初始化管理员 |
| `/api/auth/register` | 自主注册 |
| `/api/auth/change-password` | 改密码 |
| `/api/auth/logout` | 退出登录 |
| `/api/settings/ai` | 读写 AI 配置与密钥、拉模型、测连 |
| `/api/settings/agent-tokens` | 签发、列举、改名、启用/停用、吊销令牌 |

未列入范围表、也不在开放名单里的已鉴权路径（例如 `/api/system/init-status`）同样 403。不要尝试用 Cookie 里的会话 JWT 冒充 Agent，也不要把 `ragt_` 令牌写进 Cookie。

---

## 5. 接口速查

JSON 接口使用 `Content-Type: application/json`。上传接口使用 `multipart/form-data`，字段名必须与下表一致。下列接口均需对应范围的 Agent 令牌。

### 5.1 采集（`capture`）

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/api/capture` | 上传文件，立即返回草稿，后台识别 |
| GET | `/api/capture/drafts` | 列草稿。查询参数：`status_value`、`limit`（默认 60，最大 200）。不传 `status_value` 时返回 `pending` / `processing` / `ready` / `failed`，不含已入账 |
| PATCH | `/api/capture/drafts/{draft_id}` | 核对并改草稿字段 |
| POST | `/api/capture/drafts/{draft_id}/retry` | 重新识别（不复用同文件历史结果） |
| DELETE | `/api/capture/drafts/{draft_id}` | 丢弃未入账草稿 |
| GET | `/api/capture/drafts/{draft_id}/thumb` | 缩略图 |
| GET | `/api/capture/drafts/{draft_id}/file` | 原件（inline） |
| POST | `/api/capture/confirm` | 确认入账，生成应收与附件；可追加到已有应收 |
| POST | `/api/capture/drafts/{draft_id}/apply-payment` | 把到账截图核销到已有应收（主要为工资到账） |

### 5.2 台账（`ledger`）

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/companies` | 列公司（含待收、逾期统计） |
| POST | `/api/companies` | 新建公司 |
| PUT | `/api/companies/{company_id}` | 改公司（含 `is_active`） |
| DELETE | `/api/companies/{company_id}` | 删除；若已有应收则改为停用 |
| GET | `/api/categories` | 列类别。`include_inactive=true` 可含停用项；默认只返回启用中的 |
| POST | `/api/categories` | 新建类别 |
| PUT | `/api/categories/{category_id}` | 改类别 |
| DELETE | `/api/categories/{category_id}` | 删除；若已有记录则改为停用 |
| GET | `/api/receivables` | 筛选列应收，见下方查询参数 |
| GET | `/api/receivables/{receivable_id}` | 详情（含附件与到账明细） |
| POST | `/api/receivables` | 手工新建应收 |
| PUT | `/api/receivables/{receivable_id}` | 修改应收 |
| DELETE | `/api/receivables/{receivable_id}` | 软删（已有到账则拒绝） |
| POST | `/api/receivables/{receivable_id}/attachments` | 给已有应收补传附件 |
| DELETE | `/api/attachments/{attachment_id}` | 软删附件 |
| GET | `/api/attachments/{attachment_id}/file` | 附件原件 |
| GET | `/api/attachments/{attachment_id}/thumb` | 附件缩略图 |
| GET | `/api/batches` | 列批次 |
| POST | `/api/batches` | 按公司归集应收为草稿批次 |
| GET | `/api/batches/{batch_id}` | 批次详情 |
| PUT | `/api/batches/{batch_id}` | 改标题、期间、预计到账日、备注 |
| POST | `/api/batches/{batch_id}/submit` | 提交批次 |
| POST | `/api/batches/{batch_id}/cancel` | 取消批次，应收退回待提交 |
| POST | `/api/batches/{batch_id}/payment` | 按待收余额一次性结清该批 |
| GET | `/api/payments` | 列到账 |
| POST | `/api/payments` | 记录到账并分摊 |
| GET | `/api/payments/{payment_id}` | 到账详情（含分摊到的应收） |
| DELETE | `/api/payments/{payment_id}` | 删除到账并回退应收状态 |
| GET | `/api/dashboard` | 看板 KPI、公司待收、账龄、近 6 个月趋势、类别占比、逾期清单 |
| GET | `/api/audit-logs` | 本用户审计。`limit` 默认 100，最大 500 |
| GET | `/api/files/sign` | 签发限时下载链接。查询参数：`kind`、`obj_id` |

`GET /api/receivables` 查询参数：`company_id`、`status_value`（不是 `status`）、`kind`、`category_id`、`period_from`、`period_to`（归属月 `YYYY-MM`）、`q`（标题或备注模糊）、`overdue`（`true` 时仅已提交/部分到账且已过预计到账日）、`missing_materials`（`true` 时仅类别需要发票且尚无发票附件、且状态为待提交/已提交/部分到账）、`page`（默认 1）、`page_size`（默认 50，最大 200）。响应：`{items, total, total_amount, page, page_size}`，其中 `total_amount` 是筛选结果金额合计（元）。

没有「按 id 获取单个公司 / 类别」的接口，也没有 `GET /api/companies/{id}`。

### 5.3 导出（`export`）

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/export/excel` | 按筛选导出明细 Excel。查询参数：`company_id`、`period_from`、`period_to`、`kind` |
| GET | `/api/export/pdf` | 合并票据 PDF。查询参数：`ids`（逗号分隔的应收 id）、`batch_id` |
| POST | `/api/export/finance-pack` | 财务包 ZIP，并写入导出记录 |
| POST | `/api/export/attachments` | 仅打包原始附件 ZIP，并写入导出记录 |
| GET | `/api/export/records` | 最近 100 条导出记录（含文件哈希） |
| GET | `/api/export/records/{record_id}/download` | 重新下载历史导出 |
| GET | `/api/files/sign?kind=export` | 为导出记录签发限时下载链接 |

财务包与附件包的请求体：

```json
{"receivable_ids": [1, 2, 3], "batch_id": null, "include_originals": true}
```

`batch_id` 与 `receivable_ids` 二选一即可；给了 `batch_id` 时导出该批全部未删除应收。请求模型里有 `include_originals`（默认 `true`），但当前实现**不会读取该字段**，财务包始终包含封面 PDF、明细 Excel、合并票据 PDF、票据原件与截图凭证。

---

## 6. 常用工作流

推荐顺序：**查公司与类别 → 上传采集 → 轮询草稿直到识别结束 → 核对并补全金额 → 确认入账 →（可选）追加附件 → 组批次并提交 → 按分摊记到账 → 导出财务包**。

```bash
BASE="${REIMBURSE_AI_BASE:-http://100.64.0.88:8092}"
H="Authorization: Bearer $RAGT_TOKEN"
```

### 6.1 查公司与类别

新用户创建时会写入默认类别：差旅住宿、城市交通、餐饮招待、办公采购、通讯补贴、工资、奖金、其他。公司需要先建好（含发票抬头、税号，便于识别时匹配购买方）。

```bash
curl -sS "$BASE/api/companies" -H "$H"
curl -sS "$BASE/api/categories" -H "$H"

curl -sS -X POST "$BASE/api/companies" -H "$H" -H 'Content-Type: application/json' \
  -d '{
    "name":"杭州识微科技有限公司",
    "short_name":"识微",
    "invoice_title":"杭州识微科技有限公司",
    "tax_id":"91330100MA2XXXXX1X",
    "payday":15,
    "payment_days":30
  }'
# 其它字段：color（默认 #2563eb）、note
# payday：1–31 表示每月打款日；0 表示不用打款日，改用 payment_days（0–365，默认 30）作为提交后的账期天数
```

`kind` 只允许 `reimbursement` / `payroll` / `other`（新建类别时非法值会回落到 `reimbursement`）。`requires_invoice` 为 true 时，没有发票附件会被标成材料不全。

### 6.2 上传采集文件

multipart 字段名必须是 **`files`**（可重复多次），可选表单字段 **`company_id`**、**`category_id`**。不要用 `file` 或 `upload`。

```bash
curl -sS -X POST "$BASE/api/capture" -H "$H" \
  -F "files=@taxi.png;type=image/png" \
  -F "company_id=1" \
  -F "category_id=2"
# → {"success":true,"count":1,"drafts":[{id,status,amount,amount_cents,...}]}
```

一次最多 30 个文件，单文件不超过 25MB。允许的扩展名：`.jpg` `.jpeg` `.png` `.webp` `.bmp` `.gif` `.heic` `.pdf` `.ofd` `.zip`。ZIP 会在服务端解开后再为其中的可识别文件建草稿。默认异步识别；上传响应里的草稿可能仍是 `pending` / `processing`。

凭证类型 `doc_type`：`invoice` 发票 / `ticket` 客票行程单 / `receipt` 小票收据 / `payment` 支付截图 / `order` 订单截图 / `bank` 银行回单 / `salary` 工资条 / `chat` 聊天记录 / `other`。OFD 只保存原件，不做自动识别，金额需手工填写。

### 6.3 轮询草稿并核对

```bash
# 默认不含已入账草稿。查询参数名是 status_value，不是 status
curl -sS "$BASE/api/capture/drafts?status_value=ready" -H "$H"
curl -sS "$BASE/api/capture/drafts?limit=60" -H "$H"
```

草稿状态：`pending` 排队、`processing` 识别中、`ready` 可核对、`failed` 失败、`confirmed` 已入账。请轮询直到目标草稿变为 `ready` 或 `failed`，不要在 `processing` 时确认或核销到账。

识别结果仅供参考：**不要信任 AI 置信度，也不要用候选金额擅自填空。** 金额为空时必须对照原件（`GET /api/capture/drafts/{id}/file` 或 `/thumb`）由人确认后再写入。模型没给出金额时，不要拿 `amount_candidates_cents` 顶替；候选只是可点选的建议。`hints` 里会出现「金额把握不高」等复核提示。若 `is_inflow` 为 true，响应可能带 `payroll_match` 核销建议，那也只是建议。

```bash
curl -sS -X PATCH "$BASE/api/capture/drafts/12" -H "$H" -H 'Content-Type: application/json' \
  -d '{
    "company_id":1,
    "category_id":2,
    "amount":"128.00",
    "occurred_on":"2026-11-08",
    "title":"市内交通费",
    "merchant":"杭州出租汽车",
    "doc_type":"invoice",
    "invoice_code":"",
    "invoice_no":"25112000000000000001",
    "note":""
  }'
```

已入账草稿不能再改（400）。识别失败时：

```bash
curl -sS -X POST "$BASE/api/capture/drafts/12/retry" -H "$H"
```

主动重试不会复用同 SHA256 的历史识别结果。

### 6.4 确认入账

新建应收（金额必须已填写且大于 0）：

```bash
curl -sS -X POST "$BASE/api/capture/confirm" -H "$H" -H 'Content-Type: application/json' \
  -d '{"draft_ids":[12],"company_id":1,"category_id":2}'
# → {"success":true,"created":[{id,amount,amount_cents,status:"pending",...}],"warnings":[],"already_confirmed":[]}
```

规则：

- 一次确认的草稿必须属于同一家公司；未指定 `company_id` 时用草稿上的公司，仍缺则 400，并在 `detail.draft_ids` 指出哪些草稿缺公司。
- 未指定 `category_id` 时用草稿上的类别；再缺则回落到该用户第一个启用中的同类默认类别（工资条走 `payroll`，其它走 `reimbursement`）。
- 发生日为空时填今天；标题为空时用商户名或「凭证」。
- 已确认过的草稿不会重复建账，id 出现在 `already_confirmed`。
- **去重只警告、不自动删除**：同批重复发票号/文件、或库中已有相同发票号/文件哈希时，若未传 `"force": true`，返回 **409**，`detail` 为 `{message, warnings}`，`warnings[].type` 可能是 `duplicate_in_batch`、`invoice_no`、`file_hash`。
- **购买方与公司抬头不一致**：仅当草稿 `doc_type` 为 `invoice` 且识别到了购买方时检查；不匹配则 409，`warnings[].type` 为 `buyer_mismatch`（含 `buyer`、`invoice_title`、`draft_id`）。确认无误后加 `"force": true` 再提交：

```bash
curl -sS -X POST "$BASE/api/capture/confirm" -H "$H" -H 'Content-Type: application/json' \
  -d '{"draft_ids":[15],"company_id":1,"category_id":2,"force":true}'
```

把草稿**追加**到已有应收（只挂附件，**不改原应收金额**；追加时不要求草稿已有金额）：

```bash
curl -sS -X POST "$BASE/api/capture/confirm" -H "$H" -H 'Content-Type: application/json' \
  -d '{"draft_ids":[16],"company_id":1,"target_receivable_id":40}'
```

已到账（`paid`）的应收不能再追加（400）。草稿上的 `company_id` 若存在，必须与目标应收一致。

### 6.5 列应收与材料不全

```bash
curl -sS "$BASE/api/receivables?company_id=1&page=1&page_size=50" -H "$H"
curl -sS "$BASE/api/receivables?missing_materials=true" -H "$H"
curl -sS "$BASE/api/receivables/40" -H "$H"
```

详情和列表项都带 `requires_invoice`、`has_invoice`、`missing_invoice`、`materials_ok`。类别勾了「需要发票」且还没有 `kind=invoice` 的附件时，`materials_ok` 为 false。看板 `kpis.missing_materials_count` 统计这类待收事项。

手工新建（`amount` 为元）：

```bash
curl -sS -X POST "$BASE/api/receivables" -H "$H" -H 'Content-Type: application/json' \
  -d '{"company_id":1,"category_id":1,"title":"待补发票的住宿费","amount":"320.00","occurred_on":"2026-11-08"}'
```

给已有应收补传附件，字段名是 **`files`**，另有表单字段 `kind`（默认 `screenshot`；允许 `invoice` / `screenshot` / `receipt` / `chat` / `payment_proof` / `other`）、`invoice_no`、`invoice_code`、`note`。附件允许图片、PDF、OFD，单文件 25MB；PDF 且 `kind=screenshot` 时会记成 `invoice`。

```bash
curl -sS -X POST "$BASE/api/receivables/40/attachments" -H "$H" \
  -F "files=@invoice.pdf;type=application/pdf" \
  -F "kind=invoice" \
  -F "invoice_no=25112000000000000001"
```

### 6.6 组批次并提交

只有状态为 `pending` 或 `rejected` 的应收可以归集；必须属于同一家公司，且尚未加入其它批次。被驳回的条目加入批次后会回到 `pending`。

```bash
curl -sS -X POST "$BASE/api/batches" -H "$H" -H 'Content-Type: application/json' \
  -d '{"company_id":1,"period":"2026-11","title":"2026年11月报销","receivable_ids":[40,41,42]}'
# → {"success":true,"batch_id":7,"message":"批次已创建"}

curl -sS -X POST "$BASE/api/batches/7/submit" -H "$H" -H 'Content-Type: application/json' \
  -d '{"submitted_on":"2026-11-10"}'
# → {"success":true,"message":"批次已提交","expected_on":"2026-11-15"}
```

未传 `expected_on` 时，按公司规则推算预计到账日：`payday` 在 1–31 则取提交日当天或之后最近一个打款日（跨月时进入下月，月份天数不足则取月末）；否则为提交日加上 `payment_days`。批次内应收同步改为 `submitted`，并写入相同的提交日与预计到账日。已结清或已取消的批次不能再提交。有到账记录的批次不能取消。

### 6.7 记录到账（含部分到账）

优先显式 `allocations`；分摊合计必须等于到账金额（都是元），单笔分摊不得超过该应收的待收余额，且不能重复分摊同一笔应收。只能核销同一家公司、尚未结清或关闭的应收（不能是 `paid` / `abandoned` / `rejected`）。

```bash
# 先到 100 元，该笔 128 元应收变为 partial，received_cents=10000，remaining_cents=2800
curl -sS -X POST "$BASE/api/payments" -H "$H" -H 'Content-Type: application/json' \
  -d '{
    "company_id":1,
    "amount":"100.00",
    "paid_on":"2026-11-16",
    "allocations":[{"receivable_id":40,"amount":"100.00"}],
    "note":"",
    "idempotency_key":null
  }'
# → {"success":true,"payment_id":...,"message":"到账记录已保存","amount":100.0}

# 补足剩余 28 元后状态变为 paid
curl -sS -X POST "$BASE/api/payments" -H "$H" -H 'Content-Type: application/json' \
  -d '{
    "company_id":1,
    "amount":"28.00",
    "paid_on":"2026-11-20",
    "allocations":[{"receivable_id":40,"amount":"28.00"}]
  }'
```

如果不传 `allocations`，必须传 `receivable_ids`：系统按 id 升序把到账金额依次摊到各笔待收；若金额超过所选应收待收合计，返回 400，`detail.unallocated` 为多出来的元。可传 `idempotency_key`，同一用户同一键会直接返回已有到账，不重复记账。

一批次剩余全部结清：

```bash
curl -sS -X POST "$BASE/api/batches/7/payment" -H "$H" -H 'Content-Type: application/json' \
  -d '{"paid_on":"2026-11-20","note":""}'
```

删除到账会按分摊回退已收金额，并把应收退回 `partial` / `submitted`（仍在批次中）或 `pending`。

### 6.8 工资到账截图核销

采集到账类截图且识别 `direction=in` 时，草稿会带 `is_inflow` 与 `payroll_match`。确认前若这是工资入账而不是一笔新的应收，应走核销接口，而不是 `POST /api/capture/confirm`。

```bash
# 不传 allocations 时使用推断（备注月份 → 最早欠发顺序 → 金额约等于月薪倍数）
curl -sS -X POST "$BASE/api/capture/drafts/20/apply-payment" -H "$H" \
  -H 'Content-Type: application/json' -d '{"note":""}'

# 或手工指定分摊（元），合计必须等于草稿到账金额
curl -sS -X POST "$BASE/api/capture/drafts/20/apply-payment" -H "$H" \
  -H 'Content-Type: application/json' \
  -d '{"allocations":[{"receivable_id":55,"amount":"8000.00"}],"note":"11月工资"}'
```

前置条件：草稿未入账、不在识别中、已选公司、已有到账金额。识别中返回 409。推断失败或推断合计与到账金额不一致时 400，`detail` 可能含 `proposal`。成功后写入到账记录，把截图挂到被核销的第一笔应收，草稿标为 `confirmed`。幂等键固定为 `capture-draft-{草稿id}`，重复调用不会再记一笔到账。该接口本身不强制 `doc_type=salary` 或 `direction=in`，但业务上只应用于到账截图。

### 6.9 看板

```bash
curl -sS "$BASE/api/dashboard" -H "$H"
# kpis：outstanding / overdue / paid_this_month / avg_days / pending_* / partial_count /
#        missing_attachment_count / missing_materials_count / salary_outstanding / drafts_ready
# 另有 companies、aging、trend、categories、overdue（最多 10 笔）
```

待收只统计状态为 `pending` / `submitted` / `partial` 的应收；`rejected`、`abandoned` 以及已到账不计入。平均回款天数只统计已结清且同时有提交日与到账日的应收。

### 6.10 导出与签名下载

```bash
# Excel：响应体就是 xlsx
curl -sS "$BASE/api/export/excel" -H "$H" -o receivables.xlsx

# 合并 PDF：ids 为逗号分隔，或改用 batch_id
curl -sS "$BASE/api/export/pdf?ids=40,41,42" -H "$H" -o receipts.pdf

# 财务包：响应体就是 zip，同时写入导出记录
curl -sS -X POST "$BASE/api/export/finance-pack" -H "$H" -H 'Content-Type: application/json' \
  -d '{"receivable_ids":[40,41,42]}' -o finance-pack.zip

curl -sS "$BASE/api/export/records" -H "$H"
curl -sS "$BASE/api/export/records/3/download" -H "$H" -o finance-pack-again.zip
```

没有可导出的数据、或合并 PDF 找不到可合并附件时返回 400。

也可用签名链接（兼容旧流程）。`kind=export` 需要 `export` 范围，其它 `kind`（实际有效的是 `attachment`）需要 `ledger` 范围。链接 300 秒有效，下载时不必再带 Bearer：

```bash
curl -sS "$BASE/api/files/sign?kind=export&obj_id=3" -H "$H"
# → {"url":"/api/files/download/<token>","expires_in":300}

curl -sS "$BASE/api/files/download/<token>" -o finance-pack.zip
```

过期或签名无效返回 403「下载链接已失效」。`kind` 只能是 `attachment` 或 `export`。

---

## 7. 业务规则

Agent 必须遵守，不要靠猜测绕过：

- **应收是账本事实**，凭证只是附件；追加附件不改原金额。
- **确认新建应收前金额必须大于 0**；金额为空就停止，让人对照原件填写，不要用 AI 置信度或候选值顶替。
- 去重（文件哈希、发票代码/号码、同批重复）只警告；要继续必须 `force=true`。购买方抬头不符同样先 409，确认后再强制。
- 状态：应收为 `pending` 待提交 / `submitted` 已提交 / `partial` 部分到账 / `paid` 已到账 / `rejected` 被驳回 / `abandoned` 已放弃。批次为 `draft` / `submitted` / `paid` / `cancelled`。
- 手工改应收状态只允许切到 `pending`、`submitted`、`rejected`、`abandoned`，且已有到账金额时不能改。到账状态由核销自动维护，不要手工改成 `partial` 或 `paid`。
- **逾期不单独存状态**：已提交或部分到账，且今天晚于预计到账日，即为逾期；`days_overdue` 按此实时计算。
- 待收 = 应收金额 - 已收金额；草稿、被驳回、已放弃不计入。
- 一笔应收只属于一个批次；只有待提交或被驳回的可以归集。
- **分摊合计必须等于到账金额**；单笔分摊不得超过该应收待收余额；不能重复分摊同一笔；只能核销同一家公司的未关闭应收。
- 删除到账会回退已收与状态；删除已有到账的应收会被拒绝，须先删到账。
- 所有业务查询都隔离在令牌所属用户；访问他人资源返回 404 或空列表，不会串账。

---

## 8. 错误与状态码

| 状态码 | 常见原因 |
|---|---|
| 200 / 201 | 成功。本服务多数写接口成功时返回 200 |
| 400 | 参数或业务校验失败：缺公司、金额为空或格式不对、分摊合计不一致、批次状态不允许、文件类型/数量/大小不合规、已入账草稿不可改等。`detail` 可能是字符串，也可能是带 `message`、`warnings`、`draft_ids`、`expected` / `actual`、`proposal` 的对象 |
| 401 | 未带令牌、令牌无效、已吊销、已停用或已过期。文案一般为「认证令牌已失效或无效，请重新登录」或「请先登录系统」 |
| 403 | 缺少所需范围；或命中禁止路径（账号 / 令牌 / AI 密钥）。签名下载过期也是 403 |
| 404 | 资源不存在，或不属于当前用户 |
| 409 | 确认入账时存在重复或购买方抬头不符（可 `force`）；草稿仍在识别中（`apply-payment`） |
| 429 | Agent 写操作超过每分钟 60 次 |
| 500 | 服务端异常，不要重放具有副作用且无幂等键的写请求 |

响应错误体一般为 `{"detail": ...}`。

---

## 9. 能力边界

| 能力 | 支持情况 |
|---|---|
| 用户级 Agent 令牌，仅访问本人账本 | 支持 |
| 采集图片 / PDF / ZIP，后台识别后确认入账 | 支持 |
| 追加附件到已有应收、部分到账、批次提交与核销 | 支持 |
| 工资到账截图推断并核销 | 支持（建议，不自动入账） |
| 导出 Excel、合并 PDF、财务包，导出记录可追溯 | 支持 |
| 单币种以外的货币与汇率 | 不支持，仅人民币 |
| 退款、红冲、预算、账户余额 | 不支持 |
| 发票税务验真 | 不支持 |
| OFD 自动识别 | 不支持，只保存原件 |
| 邮件 / 即时消息提醒、分享链接 | 不支持 |
| MCP / tools/list / 独立 Agent 网关 | 不支持，统一用本技能描述的 HTTP 接口 |
| 用 Agent 令牌管理账号、令牌或 AI 密钥 | 不支持，一律 403 |
| 在生产库自动创建测试用户 | 不支持；测试请用独立成员账号 |

未配置 AI 时仍可采集：草稿会保留原件，金额必须手工填写。识别请求会发到用户自己配置的模型服务商，本技能不负责配置密钥。
