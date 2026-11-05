# 个人应收与报销管理台 (Reimburse AI)

把「公司欠我的钱」管清楚：报销、工资、奖金、补贴都可以记；每笔钱知道属于哪家公司、走到哪一步、逾期多久、材料够不够，并能一键打包交给财务。

## 核心能力

1. **极简采集**
   - 截图后按 `Ctrl+V` 直接粘贴，或拖入图片、PDF、ZIP；
   - 上传即完成，文件先进入收件箱，AI 在后台识别；
   - 自动提取金额、日期、商户、类别、发票号，确认时只需要选公司。

2. **对公应收台账**
   - 公司、类别、归属月、发生日、申报金额、已收金额、状态、预计到账日；
   - 状态：待提交、已提交、部分到账、已到账、被驳回、已放弃；
   - 逾期按预计到账日实时计算，不需要手工维护。

3. **提交批次**
   - 按公司勾选应收归集为批次；
   - 提交时按公司「每月打款日 + 账期」自动推算预计到账日；
   - 到账后整批核销，批次自动结清。

4. **到账核销**
   - 一笔银行到账可以同时核销多笔应收；
   - 支持部分到账：1000 的应收可以先到 500，剩余继续挂账；
   - 每笔到账明确分摊到具体应收，分摊合计必须等于到账金额；
   - 应收详情可直接「记录到账」，并查看到账明细；
   - 删除到账记录会把应收退回对应状态。

5. **统计看板**
   - 待收总额、逾期金额、本月到账、平均回款天数；
   - 各公司待收、账龄分布、近 6 个月应收与到账、类别占比、逾期清单。

6. **财务交付**
   - 财务报销包 ZIP：封面 PDF、明细 Excel、合并票据 PDF、票据原件、截图凭证；
   - 合并票据 PDF 可直接微信发给财务；
   - 导出记录留快照，可追溯、可重新下载。

7. **多用户与安全**
   - JWT + argon2id、登录限流与失败封禁、一次性签名下载；
   - 所有查询强制带 `user_id`，管理员只能管理账号，不能读取其他成员账本；
   - 成员账号默认由管理员创建，仅 `ALLOW_REGISTRATION=1` 时开放自主注册。

## 环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `PORT` | 容器内监听端口 | `8090` |
| `ALLOW_REGISTRATION` | 是否开放自主注册 | 关闭 |
| `CORS_ORIGINS` | 允许的跨域来源，逗号分隔 | 空（同源） |
| `ENABLE_API_DOCS` | 是否开放 `/docs` | 关闭 |
| `CJK_FONT_PATH` | 指定导出 PDF 使用的中文字体 | 自动查找 |
| `CAPTURE_SYNC` | 仅测试用，同步识别 | 关闭 |

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
  models.py               公司、类别、应收、批次、到账、附件、草稿、导出记录
  schemas.py              请求模型
  services/
    ai_service.py         多模态识别与 JSON 归一化
    capture.py            文件落盘、缩略图、草稿识别、去重
    exporter.py           Excel、合并 PDF、财务包 ZIP
    money.py              整数分与日期工具
    auth.py               认证与口令哈希
    auth_guard.py         登录限流与封禁
    download_tokens.py    一次性签名下载
  static/index.html       Vue 3 单页界面
tests/                    接口与导出回归测试
```

## 测试

```bash
python -m pytest -q
```

测试使用独立临时数据库与上传目录，不会触碰 `data/` 与 `uploads/`。

## 许可证

MIT
