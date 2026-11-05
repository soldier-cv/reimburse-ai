# AGENTS.md - 个人应收与报销管理台项目规范

本项目是个人对公应收台账：记录公司应付给我的报销、工资、奖金、补贴，跟踪提交、逾期与到账，并导出财务报销材料。不是个人记账，不记录个人消费、预算、账户余额。

---

## 核心安全准则与交互规范

1. **禁止破坏生产数据**：不得对 `/data/reimburse-ai` 执行删除、覆盖或破坏性 DDL。测试必须使用 `tests/` 下的隔离临时库与临时上传目录。
2. **环境感知**：生产为 Linux + Docker（宿主 8092 → 容器 8090，数据 `/data/reimburse-ai`）；本地开发可能是 Windows。命令与路径按当前环境处理。
3. **语言规范**：始终使用中文回复和解释。
4. **禁止 Emoji**：界面、提示词、注释、日志一律不使用 emoji；图标统一使用 EttsDohikct 或内联 SVG。
5. **认证鉴权纪律**：口令哈希用 `argon2-cffi`，令牌用 `PyJWT`，限流用 `slowapi`/`limits`，签名链接用 `itsdangerous`；禁止手写密码哈希、令牌签名、随机数与常量时间比较。凭证只走 Header/Cookie，禁止 query 传凭证。
6. **数据隔离**：所有业务查询必须带 `user_id`。管理员只能管理账号，不能读取其他成员账本。
7. **注册默认关闭**：成员账号由管理员在设置中创建；仅当显式设置 `ALLOW_REGISTRATION=1` 时开放自主注册。
8. **CORS 默认关闭**：前后端同源部署；跨域来源必须通过 `CORS_ORIGINS` 显式白名单。
9. **提交准则**：本地验证通过后按逻辑分组 `git commit`；`git push` 由派发方验收后执行。

---

## 业务定位与核心口径

- **应收事项 `Receivable` 是唯一账本事实**；发票、截图、聊天记录只是附件。
- 一笔应收只属于一个批次；批次是向公司的一次提交。
- **支持部分到账**：一笔应收可以被多笔到账分次核销；每笔到账必须分摊到具体应收，分摊合计必须等于到账金额，单笔分摊不得超过该应收待收余额。
- 状态：`待提交 / 已提交 / 部分到账 / 已到账 / 被驳回 / 已放弃`。
- 逾期不单独存状态，由「今天 > 预计到账日 且 未结清」实时计算。
- 待收 = 应收金额 - 已收金额；草稿、被驳回、已放弃不计入。
- 工资与报销共用应收模型，用 `kind` 区分 `reimbursement / payroll / other`。
- 金额全库使用整数分 `*_cents`，禁止 Float 存金额；展示与导出时转元。

---

## 数据模型

| 表 | 用途 |
|---|---|
| `Company` | 欠款主体，含发票抬头、税号、每月打款日、账期天数 |
| `Category` | 应收类别，含 kind、是否需要发票 |
| `Receivable` | 应收事项：公司、类别、批次、归属月、发生日、金额、已收金额、状态、预计到账日 |
| `Batch` | 提交批次：公司、期间、提交日、预计到账日、状态 |
| `Payment` | 一笔实际到账 |
| `PaymentItem` | 该笔到账分摊到各应收的金额，支持一笔应收多次到账 |
| `Attachment` | 发票、截图、聊天记录、回单 |
| `CaptureDraft` | 已上传未确认的采集草稿，承载 AI 识别结果 |
| `ExportRecord` | 导出快照：清单、金额、文件哈希、路径 |
| `AuditLog` / `UserSetting` / `SystemSetting` | 审计与配置 |

---

## 采集与 AI 识别

- 入口统一为 `POST /api/capture`：图片、PDF、OFD、ZIP；单文件 25MB，单次最多 30 个。
- 上传立即返回草稿，后台识别；`CAPTURE_SYNC=1` 仅用于测试。
- 识别顺序：PDF 文本层本地抽取 → 视觉模型兜底；同一文件按 SHA256 去重。
- AI 输出必须是严格 JSON，字段见 `app/services/ai_service.py`；金额取支付实付、发票价税合计、工资实发。
- AI 置信度不可信，金额必须可编辑、可看原图；金额为空不允许确认。
- 去重：文件哈希 + 发票代码/号码 + 同批重复；命中只提示，不自动删除。
- 确认时生成 `Receivable + Attachment`，草稿状态置为 `confirmed`；支持把草稿追加到已有应收。

## 导出

- `GET /api/export/excel`：筛选条件导出明细 Excel。
- `GET /api/export/pdf`：合并票据 PDF，按清单编号。
- `POST /api/export/finance-pack`：财务包 ZIP，包含封面、Excel、合并 PDF、原件、截图。
- 每次财务包写入 `ExportRecord`，保留文件哈希与路径。
- PDF 合成使用 Pillow + PyMuPDF；容器必须安装中文字体（Dockerfile 已安装 `fonts-noto-cjk`）。

---

## 部署与 CI

- `docker-compose.yml`：服务/容器/镜像名 `reimburse-ai`，宿主 8092 → 容器 8090。
- 挂载：`/data/reimburse-ai/data:/app/data`、`/data/reimburse-ai/uploads:/app/uploads`。
- `.woodpecker.yaml`：仅 `tag` 与 `manual` 触发；`resolve-tag → build-and-push-image → deploy-and-verify`。
- 健康检查：`GET /health` 或首页 `GET /`。

## 本地命令

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python run.py
python -m pytest -q
```

测试使用临时数据库与上传目录，禁止指向生产路径。

## 已知边界

- 单币种人民币，不做汇率。
- 不做退款/红冲、预算、账户余额、邮件微信提醒、分享链接。
- 不做发票税务验真。
- OFD 只保存原件，不做自动识别。
- AI 需要用户自行配置 OpenAI 兼容接口；未配置时手动填金额。
