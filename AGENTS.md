# AGENTS.md - 个人应收与报销管理台项目规范

本项目是个人对公应收台账：记录公司应付给我的报销、工资、奖金、补贴，跟踪提交、逾期与到账，并导出财务报销材料。不是个人记账，不记录个人消费、预算、账户余额。

---

## 核心安全准则与交互规范

1. **禁止破坏生产数据**：不得对 `/data/reimburse-ai` 执行删除、覆盖或破坏性 DDL。测试必须使用 `tests/` 下的隔离临时库与临时上传目录。
2. **环境感知**：生产为 Linux + Docker（宿主 8092 → 容器 8090，数据 `/data/reimburse-ai`）；本地开发可能是 Windows。命令与路径按当前环境处理。
3. **语言规范**：始终使用中文回复和解释。
4. **禁止 Emoji**：界面、提示词、注释、日志一律不使用 emoji；图标统一使用 EttsDohikct 或内联 SVG。
5. **认证鉴权纪律**：口令哈希用 `argon2-cffi`，会话令牌用 `PyJWT`，限流用 `slowapi`/`limits`，签名链接用 `itsdangerous`；禁止手写密码哈希、令牌签名、随机数与常量时间比较。Agent 令牌使用 `hashlib.sha256` 存储、`hmac.compare_digest` 比对，明文前缀为 `ragt_`，只在签发时返回一次。凭证只走 Header/Cookie，禁止 query 传凭证。
6. **数据隔离**：所有业务查询必须带 `user_id`。管理员只能管理账号，不能读取其他成员账本。Agent 令牌属于某个用户，并始终以该用户身份访问，不能查看其他账本，也不能管理任何用户的令牌。
7. **注册默认关闭**：成员账号由管理员在设置中创建；仅当显式设置 `ALLOW_REGISTRATION=1` 时开放自主注册。
8. **CORS 默认关闭**：前后端同源部署；跨域来源必须通过 `CORS_ORIGINS` 显式白名单。
9. **提交准则**：本地验证通过后按逻辑分组 `git commit`；`git push` 由派发方验收后执行。版本号与标签规则见「版本与发布」。

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
| `AgentToken` | 用户签发的 Agent 访问令牌，只存哈希与前缀 |
| `AuditLog` / `UserSetting` / `SystemSetting` | 审计与配置 |

---

## 采集与 AI 识别

- 入口统一为 `POST /api/capture`：图片、PDF、OFD、ZIP；单文件 25MB，单次最多 30 个。
- 上传立即返回草稿，后台并发识别（`CAPTURE_CONCURRENCY`，默认 4）；`CAPTURE_SYNC=1` 仅用于测试；服务重启后未完成的草稿自动重新排队。
- 凭证类型 `doc_type`：`invoice` 发票 / `ticket` 客票行程单 / `receipt` 小票收据 / `payment` 支付截图 / `order` 订单截图 / `bank` 银行回单 / `salary` 工资条 / `chat` 聊天记录 / `other`。
- 识别顺序：同用户同 SHA256 复用已有结果（用户点「重新识别」时不复用）→ PDF 文本层套固定模板（增值税/全电发票、铁路电子客票、通行费汇总单，见 `app/services/extractor.py`）→ 其他有文本层的 PDF 只发文本给模型 → 扫描件和图片走视觉模型。模板没命中就交给模型，不用宽松正则猜金额。
- 文件名只作为低置信候选（金额、20 位发票号），不再从文件名里取 8 位数字当发票号。
- 公司自动匹配：先按购买方税号匹配 `Company.tax_id`，再按购买方或付款方名称匹配；销售方和商户是收款人，不参与匹配。
- 工资到账截图（`direction=in`）：不建应收，按「备注月份 → 最早欠发顺序 → 金额对月薪」推断归属月，给出核销建议；点「核销到账」才写入到账记录，规则见 `app/services/payroll_match.py`。
- AI 输出必须是严格 JSON，字段见 `app/services/ai_service.py`；金额取支付实付、发票价税合计、工资实发。
- AI 置信度不可信，金额必须可编辑、可看原图；金额为空不允许确认；模型没给出金额时不拿候选值顶替，候选只作可点选的建议。
- 去重：文件哈希 + 发票代码/号码 + 同批重复；命中只提示，不自动删除。
- 确认时生成 `Receivable + Attachment`，草稿状态置为 `confirmed`；支持把草稿追加到已有应收。

## Agent 接口

- 用户在「设置 -> Agent 令牌」签发、停用、启用或吊销自己的令牌；管理员不能管理其他用户的令牌。
- 请求头：`Authorization: Bearer ragt_...`。`GET /api/auth/me` 对 Agent 调用返回 `auth_method`、令牌名称和范围。
- 默认签发全部三个范围：`capture`（`/api/capture*`，含确认与核销到账）、`ledger`（公司、类别、应收、批次、到账、看板、附件）、`export`（Excel、PDF、财务包、附件压缩包、导出记录与下载）。缺少范围返回 403。
- Agent 令牌禁止用于：管理员账号管理、初始化管理员、注册、改密、退出登录、AI 设置（含密钥与测连）、以及令牌本身的签发/列举/吊销。
- 吊销、停用或过期的令牌返回 401。已有数据库启动时自动创建 `agent_tokens` 表，无需破坏性迁移。
- 不要在生产库自动写入测试用户。如需给 Agent 或故事测试准备专用账本，由管理员创建独立成员，再由该成员自己签发令牌。

## 技能文档（skills/）

`skills/reimburse-ai/SKILL.md` 是发给外部 Agent 的用法说明，**真源就在本仓库**，随代码一起版本化：

- 改 Agent 可见的接口、权限范围或业务规则时，**同一个提交里**把技能改掉，别让它落后于代码。
- 技能版本号独立于应用版本，按 patch（措辞）/ minor（新增章节或接口）/ major（推翻既有结论）递增。当前技能 1.0.0 对齐应用 3.2.0+。
- 本仓库不带用于同步技能目录的 git hooks。要挂到公共技能注册，只需在宿主上建立符号链接：`/opt/code/static-deploy/skills/reimburse-ai` → 本仓库的 `skills/reimburse-ai`。之后由宿主已有的 index 同步机制纳入 `http://100.64.0.88:8088/.well-known/skills/`。在完成这一步之前，不要假定线上已提供这份技能。
- 判断线上是否最新：`curl -s http://100.64.0.88:8088/.well-known/skills/reimburse-ai/SKILL.md | sha256sum` 与本地 `sha256sum skills/reimburse-ai/SKILL.md` 一致。

## 导出

- `GET /api/export/excel`：筛选条件导出明细 Excel。
- `GET /api/export/pdf`：合并票据 PDF，按清单编号。
- `POST /api/export/finance-pack`：财务包 ZIP，包含封面、Excel、合并 PDF、原件、截图。
- 每次财务包写入 `ExportRecord`，保留文件哈希与路径。
- PDF 合成使用 Pillow + PyMuPDF；容器必须安装中文字体（Dockerfile 已安装 `fonts-noto-cjk`）。

---

## 版本与发布

- 版本号遵循语义化版本 `主.次.补丁`，并保持克制，不要每次改动都升次版本号。
- **默认只升补丁号**（如 `3.2.0 → 3.2.1`）：缺陷修复、界面调整、文档、测试，以及不改变使用方式的小功能。
- **升次版本号**（如 `3.2.x → 3.3.0`）：只在攒够一批明显的新能力，或用户明确要求时。
- **升主版本号**：只在不兼容的数据或接口变更时，且需事先得到用户确认。
- 版本号需同步修改 `app/main.py` 中的应用版本与 `CHANGELOG.md`；日常改动先写入「未发布」，发布时再归入对应版本。
- 推送 `v*` 标签会触发生产部署；推送 `main` 不会部署。发布前确认测试通过，标签由派发方确认后再打。

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

测试使用临时数据库与上传目录，禁止指向生产路径。故事测试使用 `tests/fixtures/invoices/` 中的发票截图，并在隔离用户 `story-tester` 的账本中执行；不依赖在线 AI。重新生成夹具：`python tests/fixtures/invoices/render.py`。

## 已知边界

- 单币种人民币，不做汇率。
- 不做退款/红冲、预算、账户余额、邮件微信提醒、分享链接。
- 不做发票税务验真。
- OFD 只保存原件，不做自动识别。
- AI 需要用户自行配置 OpenAI 兼容接口；未配置时手动填金额。
