# 个人应收与报销管理台 (Reimburse AI)

把「公司欠我的钱」管清楚：报销、工资、奖金、补贴都可以记；每笔钱知道属于哪家公司、走到哪一步、逾期多久、材料够不够，并能一键打包交给财务。

## 核心能力

1. **极简采集**
   - 截图后按 `Ctrl+V` 直接粘贴，或拖入图片、PDF、ZIP；
   - 上传即完成，文件先进入收件箱，AI 在后台识别；
   - 自动提取金额、日期、商户、类别、发票号，确认时只需要选公司。

2. **对公应收台账**
   - 公司、类别、归属月、发生日、申报金额、已收金额、状态、预计到账日；
   - 归属月独立于发生日期，确认入账时可改，已提交后锁定；
   - 状态：待提交、已提交、部分到账、已到账、被驳回、已放弃；只有待提交和被驳回可归集；
   - 逾期按预计到账日实时计算，不需要手工维护。

3. **提交批次**
   - 按公司勾选同一归属月的应收归集为批次；
   - 草稿批次可增删应收；已提交尚未到账的可以撤回提交后再调整；
   - 提交时按公司「每月打款日 + 账期」自动推算预计到账日；
   - 记录批次到账时可选择到账日期，不选则默认为当天；到账后整批核销。

4. **到账核销**
   - 一笔银行到账可以同时核销多笔应收；
   - 支持部分到账：1000 的应收可以先到 500，剩余继续挂账；
   - 每笔到账明确分摊到具体应收，分摊合计必须等于到账金额；
   - 应收详情可直接「记录到账」，并查看到账明细；
   - 删除到账记录会把应收退回对应状态。

5. **统计看板**
   - 待收总额、逾期金额、本月到账、平均回款天数；
   - 待办：未确认草稿、本月待提交、已逾期、待核销或待生成的工资；
   - 各公司待收、账龄分布、近 6 个月应收与到账、类别占比、逾期清单；
   - 薪资计划按月自动补齐待提交工资应收，同一公司同一归属月不重复。

6. **财务交付**
   - 财务报销包 ZIP：封面 PDF、明细 Excel、合并票据 PDF、票据原件、截图凭证；
   - 也可按应收、批次或归属月只打包原始文件；
   - 合并票据 PDF 可直接微信发给财务；
   - 导出记录留快照，可追溯、可重新下载；
   - 应用内预览图片和 PDF，不必先下载再离开页面。

7. **多用户与安全**
   - JWT + argon2id、登录限流与失败封禁、一次性签名下载；
   - 所有查询强制带 `user_id`；每个成员是独立账本，管理员只能管理账号，不能读取其他成员账本；
   - 默认只创建普通成员，创建管理员需二次确认；支持停用（会话立即失效）和软删除；
   - 成员账号默认由管理员创建，仅 `ALLOW_REGISTRATION=1` 时开放自主注册。

8. **Agent 令牌**
   - 用户可在「设置 -> Agent 令牌」为自己签发访问令牌，前缀为 `ragt_`，明文只在签发或重新签发时显示一次；重新签发后旧令牌立即失效；
   - 在请求头使用 `Authorization: Bearer ragt_...`，令牌只作用于该用户账本；
   - 权限范围：`capture`（采集与确认）、`ledger`（公司、类别、台账、批次、到账、看板、薪资计划）、`export`（Excel、PDF、财务包与按月份打包原件）；
   - 不能用 Agent 令牌管理账号、改密码、签发或吊销令牌，也不能读取或修改 AI 密钥；可以读取是否启用 AI 及模型名称。

## 环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `PORT` | 容器内监听端口 | `8090` |
| `ALLOW_REGISTRATION` | 是否开放自主注册 | 关闭 |
| `CORS_ORIGINS` | 允许的跨域来源，逗号分隔 | 空（同源） |
| `ENABLE_API_DOCS` | 是否开放 `/docs` | 关闭 |
| `CJK_FONT_PATH` | 指定导出 PDF 使用的中文字体 | 自动查找 |
| `CAPTURE_SYNC` | 仅测试用，同步识别 | 关闭 |
| `CAPTURE_CONCURRENCY` | 后台同时识别的草稿数 | `4` |

## 本地测试部署

使用本地源码构建，数据落在当前目录的 `data/` 与 `uploads/`：

```bash
docker compose -f docker-compose.local.yml up -d --build
```

打开 `http://localhost:8092`，首次访问会要求设置管理员用户名和密码。本地 compose 只绑定 `127.0.0.1`，不会暴露到局域网。

端口冲突时：

```bash
# Windows PowerShell
$env:PORT=8095; docker compose -f docker-compose.local.yml up -d --build
# Linux / macOS
PORT=8095 docker compose -f docker-compose.local.yml up -d --build
```

常用命令：

```bash
docker compose -f docker-compose.local.yml logs -f
docker compose -f docker-compose.local.yml down
docker exec reimburse-ai-local python scripts/backup.py
```

测试数据只影响当前目录的 `data/` 与 `uploads/`，不会触碰服务器上的 `/data/reimburse-ai`。

## 快速启动

### Docker Compose

```bash
docker compose up -d
```

访问 `http://localhost:8092`，首次打开会引导设置管理员账号。

数据持久化：

- `data/expense.db`：SQLite 数据库
- `uploads/`：凭证、截图、发票与导出文件

### 本地运行

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
python run.py
```

默认端口 `8090`。

## AI 识别配置

在「设置 -> AI 识别」里填写任意 OpenAI 兼容的多模态接口：

- 接口地址，例如 `https://api.openai.com/v1`
- API Key
- 模型名称，例如 `gpt-4o-mini`、`qwen-vl-max`
- 可选自定义提示词

不配置 AI 也能使用：截图会保留原图，只需手动填写金额。图片会发送到你配置的模型服务商，请自行评估隐私边界。

## 业务口径

- 金额全库使用整数分存储，展示时转元；
- 一笔应收只属于一个批次；
- 支持部分到账，待收 = 应收金额 - 已收金额；
- 一笔到账必须全部分摊到应收，分摊合计等于到账金额；
- 逾期 = 已提交或部分到账，且今天超过预计到账日；
- 草稿、被驳回、已放弃不计入待收；
- 平均回款天数只统计已结清且已提交过的应收。

## 备份与恢复

```bash
python scripts/backup.py
```

脚本使用 SQLite backup API 备份数据库，并把 `uploads/` 与密钥一起打包到 `data/backups/`，默认保留最近 14 份。容器内可执行：

```bash
docker exec reimburse-ai python scripts/backup.py
```

恢复时停止容器，解压备份包覆盖 `data/` 与 `uploads/`，再启动即可。

## 项目结构

```
app/
  main.py                 API 路由与业务编排
  models.py               公司、类别、应收、批次、到账、附件、草稿、导出记录、Agent 令牌
  schemas.py              请求模型
  services/
    ai_service.py         多模态识别与 JSON 归一化
    capture.py            文件落盘、缩略图、草稿识别、去重
    exporter.py           Excel、合并 PDF、财务包 ZIP
    money.py              整数分与日期工具
    auth.py               认证、会话 JWT 与 Agent 令牌校验
    auth_guard.py         登录限流与封禁
    agent_tokens.py       Agent 令牌签发、哈希与范围
    download_tokens.py    一次性签名下载
  static/index.html       Vue 3 单页界面
tests/                    接口、导出与 Agent 故事测试
tests/fixtures/invoices/  可复用发票 HTML 与 PNG 夹具
```

## Agent 调用

其它 Agent 的完整用法（鉴权、权限范围、采集到导出的步骤与边界）见仓库内技能文档 [`skills/reimburse-ai/SKILL.md`](skills/reimburse-ai/SKILL.md)。签发令牌后，用该用户的身份访问业务接口：

```http
Authorization: Bearer ragt_...
GET /api/auth/me
```

`/api/auth/me` 会返回 `auth_method`、令牌名称和权限范围。已有数据库在启动时会自动创建 `agent_tokens` 表，无需手工迁移，也不影响旧数据。

建议为 Agent 或故事测试单独准备一个成员账号，只在该账本中跑测试，避免和个人生产数据混在一起。成员由管理员在「设置 -> 成员」中创建，再由该成员自己签发 Agent 令牌。系统不会向生产库自动写入测试用户。

## 测试

```bash
python -m pytest -q
```

测试使用独立临时数据库与上传目录，不会触碰 `data/` 与 `uploads/`。故事测试会创建名为 `story-tester` 的隔离用户，并用 `tests/fixtures/invoices/` 中的发票截图跑采集、入账、批次、到账和导出流程；识别结果为空时测试会补全草稿字段，不依赖在线 AI。重新生成夹具：

```bash
python tests/fixtures/invoices/render.py
```

## 许可证

MIT
