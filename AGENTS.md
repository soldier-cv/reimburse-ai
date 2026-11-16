# reimburse-ai — Agent 工作规约

## 0. 项目定位

个人对公应收与报销管理台：记录公司应付给个人的报销、工资、奖金、补贴，支持发票凭证批量导入、AI 识别归一化、提交批次、到账分摊核销与财务材料包导出（非个人日常记账，不记录个人消费、预算与账户余额）。

- 仓库地址：`https://github.com/soldier-cv/reimburse-ai.git`
- 默认主分支：`main`

## 1. 运行环境事实

- **部署入口与端口**：生产环境为 Linux + Docker Compose，宿主机监听 `8092` 端口并由 1Panel OpenResty 反向代理对外，容器内监听 `8090`；本地开发通过 `run.py` 监听 `8090`。
- **容器与镜像**：容器名 `reimburse-ai`，生产镜像由 Woodpecker CI 构建并推送至内网 Registry：`100.64.0.88:5000/reimburse-ai:<tag>`（同时打 `latest` 别名）。
- **部署与源码路径**：宿主机部署目录 `/opt/code/reimburse-ai` 即 Git 仓库目录。
- **持久化数据**：
  - SQLite 数据库与持久系统配置：`/data/reimburse-ai/data/expense.db`（容器内挂载至 `/app/data`）。
  - 上传发票、原件截图与导出附件：`/data/reimburse-ai/uploads`（容器内挂载至 `/app/uploads`）。
- **数据库版本管理**：从 v3.4.0 起采用 Alembic 管理数据库迁移，修订文件位于 `alembic/versions/`，配置文件为 `alembic.ini`；应用启动时自动执行 `alembic upgrade head` 并将版本记录在 `alembic_version` 表；`app/services/migrations.py` 为 v3.4.0 前的冻结历史兼容层，禁止继续扩充。
- **外部 Agent 技能真源**：`skills/reimburse-ai/SKILL.md` 为唯一真源，随本仓库代码同一提交维护；通过宿主机符号链接 `/opt/code/static-deploy/skills/reimburse-ai` 对外分发至 `http://100.64.0.88:8088/.well-known/skills/`。
- **CI/CD 流水线**：唯一 CI 为 `.woodpecker.yaml`（Woodpecker v3），触发事件严格限定为 `tag` (`refs/tags/v*`) 与 `manual` 页面手动触发，日常 push 不触发构建与发布。
- **AI 与渲染依赖**：多模态识别支持任意 OpenAI 兼容接口，密钥由用户在 Web 设置中配置；PDF 合成与导出依赖 `Pillow`、`PyMuPDF` 以及系统中文字体 `fonts-noto-cjk`。

## 2. 硬规则（违反必有检测手段）

1. **禁止破坏生产持久化数据**：不得向生产目录 `/data/reimburse-ai` 写入测试数据、执行删除文件或破坏性 DDL；本地与自动化测试必须走临时隔离路径。
   - **验证方式**：检查代码与配置改动中无指向生产绝对路径的硬编码写操作：`git diff | grep -E '/data/reimburse-ai'` 应为 0 命中。
2. **界面文本纪律（§0.2）**：页面 UI 严禁堆砌「本功能实现」「为了满足需求」「设计思路」等面向开发者的说明文字；需求说明只写在源码注释或开发文档中。
   - **验证方式**：`grep -inE "本功能|为了满足|设计思路|实现逻辑|根据需求" app/static/index.html` 结果必须为 0 命中。
3. **用户账本数据绝对隔离**：所有业务查询必须显式过滤 `user_id`；管理员仅限管理账号状态，禁止越权读取普通成员账本；Agent 令牌（`ragt_` 前缀）仅能以签发用户身份访问。
   - **验证方式**：运行多用户与权限故事测试：`pytest tests/test_agent_stories.py tests/test_members.py -q`，必须全部通过且无跨用户数据泄漏。
4. **全库金额严格使用整数分**：数据库中所有涉及金额的列必须以 `*_cents` 命名并使用整数存储，严禁使用 Float；仅在前端展示与导出报表时除以 100 换算为元。
   - **验证方式**：`grep -n "Float" app/models.py` 必须无任何金额属性使用 Float 类型。
5. **凭据安全与禁止入库**：真实密钥、真实密码与私钥证书绝不上库；`.env.example` 仅允许键名与中文注释；`.gitignore` 必须忽略 `.env`、`*.key`、`*.pem`、`*.token`。
   - **验证方式**：`git ls-files | grep -E '\.env$|\.pem$|\.key$|\.token$'` 必须为空；`git diff | grep -iE 'ghp_|sk-[a-zA-Z0-9]{20,}'` 必须为 0 命中。
6. **数据库变更必须走 Alembic 修订（§0.1）**：禁止在应用业务热路径随意执行散落 DDL，禁止向已冻结的 `migrations.py` 添加新逻辑；所有表结构与列变更必须在 `alembic/versions/` 下创建新的迁移脚本。
   - **验证方式**：`git diff origin/main -- app/services/migrations.py` 必须为空；若修改了 `app/models.py`，必须有对应的 `alembic/versions/*.py` 提交。

## 3. 开发与验证流程

1. **开工第一步**：`git fetch && git pull --ff-only origin main` 拉齐远端分支，多环境开发严禁在落后基线上动工。
2. **本地环境准备**：
   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   python run.py  # 默认监听 8090 端口
   ```
3. **改完必跑验证清单**：
   - 语法编译自检：`python3 -m py_compile app/main.py app/models.py app/database.py`
   - 凭据泄漏排查：`git ls-files | grep -E '\.env$|\.pem$|\.key$'`
   - 界面纪律排查：`grep -inE "本功能|为了满足|设计思路" app/static/index.html`
   - 自动化测试回归：`python3 -m pytest -q`（测试使用隔离临时库与临时上传目录，绝不污染生产）

## 4. 已知坑（踩过的，别再踩）

1. **ETC 票据与行程单重复计费与千分位截断**：票根网发票包同时含 `(票据).pdf` 与 `(行程).pdf`，金额常含千分位逗号（如 `¥1,005.60`）；必须经 `parse_clean_amount` 剔除逗号，且仅以 `(票据).pdf` 作为主发票入账，行程单作为附件挂载，单号走确定性规则 `TOLL_{车牌}_{日期}_{金额(分)}`。
2. **Woodpecker 镜像同步与宿主机文件遗留**：CI 构建镜像前会同步文件到宿主机 `/opt/code/reimburse-ai/`，但 `cp -rf` 不会删除历史已废弃的文件（如旧的 `app/services/parser.py`）；清理文件时需显式在 Git 历史中跟踪。
3. **Alembic 启动与 script_location 路径**：FastAPI 在 `startup` 中调用 `command.upgrade(alembic_cfg, "head")` 时，`alembic_cfg` 必须能正确解析 `alembic.ini` 与 `script_location`（相对应应用根目录），否则会导致容器重启失败。
4. **工资税前应发 vs 实发到账**：工资 `amount` 记税前，实发通过到账登记，差额记入 `waived_cents` 代扣；批次到账必须跳过 `kind=payroll`，避免误核销。
5. **Favicon 响应格式与离线加载**：Web UI 必须提供 32x32 `favicon.ico` 与 SVG，且在 `app/main.py` 正确返回 `image/x-icon`，离线内网环境不能依赖外链 CDN 图标。

## 5. 需求台账 / 决策日志

- 2026-10-10：核心解析规范落地：ETC 票据去重与防千分位截断；首次部署管理员向导与多用户管理；对齐 Woodpecker CI 部署决策。
- 2026-11-04：重构对公应收台账、采集流水线与财务导出包（移除废弃 parser.py，拆分 capture/ledger/extractor 等服务）。
- 2026-11-12：引入独立归属月概念；严格应收状态机（待提交/已提交/部分到账/已到账/被驳回/已放弃）。
- 2026-11-16：工资区分税前应发与实发代扣，到账跳过工资；引入 Alembic 进行数据库版本化迁移；补齐项目仓库规范（.env.example、favicon.ico、.gitignore 凭据防线与标准化 AGENTS.md）。
