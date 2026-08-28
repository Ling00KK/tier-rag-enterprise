# Tier 企业 RAG 知识库助手

> **企业版（v2.0.0）** — 支持 ClickHouse 向量检索、部门权限和可切换模型服务的企业知识库网站。

将公司的 PDF、Word、Excel、文本和图片资料放入统一目录，员工即可通过网页提问。正式服务器可使用 ClickHouse HNSW 向量召回，并与 BM25、RRF、Reranker 组成混合检索；本地开发环境仍可降级使用 FAISS。回答模型支持公司 vLLM、OpenRouter 和其他 OpenAI-compatible 服务。

## 第一版能力

- 独立网页与 FastAPI 后端，不依赖 Streamlit
- 用户名、密码哈希验证及登录失败限流
- 支持 PDF、DOCX、XLSX、XLS、TXT、Markdown 与常见图片 OCR
- 问题改写、企业同义词扩展、多路 BM25 + BGE/FAISS/ClickHouse 召回、RRF 融合及 `bge-reranker-base` Top 3 重排
- 段落边界切分、上下文重叠和 Excel 短行保留
- SQLite 增量向量缓存与低相关度拒答
- 接入 OpenAI-compatible API，可对接公司内部 vLLM
- 回答强制逐句引用证据，并经过独立事实一致性复核；资料不足、引用无效或核验失败时明确拒答
- 免费回答模型未稳定生成合格引用时自动重试；仍失败则安全展示最相关资料原文及页码，避免把“生成失败”误报成“资料不存在”
- 同系列制度默认选择最新版本，问题指定年份时可查询历史版本
- 资料发生变化后，在下一次提问时自动检查并更新索引
- WPS/金山文档与腾讯文档在线来源连接器，可定时或手动同步
- Docker、WSL 或普通 Linux 服务器均可部署
- 管理员运行看板、提问趋势、未命中记录、答案有用率和审计 CSV 导出
- 标准评测题可新增、编辑、启停、删除并批量回归测试
- 员工账号可编辑、启用或停用，资料权限在检索前由服务端强制过滤
- 资料库支持选择两个文档版本并显示逐行差异

## 工作流程

```text
企业资料 → 多格式读取与结构化切分 → ClickHouse HNSW + BM25 混合召回
         → RRF 融合 → bge-reranker-base 精排 Top 3
         → 公司 vLLM / Qwen → 中文答案 + 来源定位
```

## 快速启动（Docker）

### 1. 准备配置

复制 `.env.example` 为 `.env`，然后生成密码哈希：

```bash
python scripts/hash_password.py
```

将生成结果填入 `.env`，并设置至少 32 位的随机 `SESSION_SECRET`。不要把真实 `.env` 提交到 Git。

### 2. 放入资料

把企业资料放入 `source/`。该目录中的实际资料已被 `.gitignore` 排除，不会上传到 GitHub。

### 3. 启动网站

```bash
docker compose up -d --build
```

浏览器访问 `http://服务器IP:8501`。

## ClickHouse 正式部署

使用项目自带的 ClickHouse 组合配置启动：

```bash
docker compose -f docker-compose.yml -f docker-compose.clickhouse.yml up -d --build
```

在 `.env` 中设置：

```text
CLICKHOUSE_ENABLED=true
CLICKHOUSE_REQUIRED=true
CLICKHOUSE_HOST=clickhouse
CLICKHOUSE_PORT=8123
CLICKHOUSE_DATABASE=tier_rag
CLICKHOUSE_USER=tier_rag_app
CLICKHOUSE_PASSWORD=使用强随机密码
EMBEDDING_DIMENSION=512
```

首次迁移已有资料：

```bash
docker compose -f docker-compose.yml -f docker-compose.clickhouse.yml exec web \
  python scripts/migrate_to_clickhouse.py
```

同步采用不可变快照：新向量全部写入成功后才切换检索快照，失败时保留旧索引。ClickHouse 中的向量查询包含部门权限条件；应用层还会执行第二次权限校验。旧快照默认保留 30 天后由 TTL 清理。

`CLICKHOUSE_REQUIRED=true` 用于正式环境：ClickHouse 不可用时请求明确失败，不会静默切回本地索引。开发环境可设为 `false`，保留 FAISS 降级能力。

## WSL / Linux 直接运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
set -a
source .env
set +a
uvicorn app.main:app --host 0.0.0.0 --port 8501
```

CPU 服务器建议安装 PyTorch CPU 版，以避免下载不需要的 CUDA 组件。生产环境建议通过 systemd 托管进程。

## 部门权限演示

项目提供可重复运行的虚构数据脚本，用于创建人事部、财务部两个演示员工，以及公共、人事专属、财务专属三份演示手册：

```bash
python scripts/seed_permission_demo.py \
  --hr-password '自行设置的人事测试密码' \
  --finance-password '自行设置的财务测试密码'
```

同步知识库后，可使用真实 HTTP 问答接口执行交叉权限测试：

```bash
python scripts/test_permission_demo_live.py \
  --hr-password '自行设置的人事测试密码' \
  --finance-password '自行设置的财务测试密码'
```

测试会确认两个账号都能看到公共手册，只能检索本部门专属手册，并且无法从问答接口获得另一个部门的演示识别词。脚本中的手册内容完全虚构，不代表公司真实制度。

## 主要配置

| 配置项 | 用途 |
|---|---|
| `APP_USERNAME` | 网站登录用户名 |
| `APP_PASSWORD_SALT` | 密码哈希盐值 |
| `APP_PASSWORD_HASH` | PBKDF2 密码哈希 |
| `SESSION_SECRET` | 登录会话签名密钥 |
| `COOKIE_HTTPS_ONLY` | 启用 HTTPS 后应设为 `true` |
| `SOURCE_DIR` | 企业资料目录 |
| `VLLM_BASE_URL` | OpenAI-compatible API 地址 |
| `VLLM_MODEL` | 大模型名称 |
| `VLLM_API_KEY` | API 密钥；无鉴权服务可使用 `EMPTY` |
| `INTEGRATIONS_CONFIG` | 加密后的在线连接配置保存位置 |
| `MODEL_CONFIG_PATH` | 管理员模型配置的加密保存位置 |
| `MODEL_ALLOWED_HOSTS` | 允许管理员连接的模型主机白名单 |
| `QUERY_REWRITE_ENABLED` | 是否调用当前模型生成多种制度检索表达 |
| `QUERY_SYNONYMS_PATH` | 企业同义词 JSON 文件位置，可由 IT 持续维护 |
| `EVIDENCE_MIN_SCORE` | 允许生成答案的最低 Reranker 证据分数 |
| `ANSWER_VERIFICATION_ENABLED` | 是否在答案生成后执行独立证据一致性核验 |
| `VERIFICATION_FAIL_CLOSED` | 核验服务异常时是否安全拒答 |

`openrouter/free` 会在不同免费模型之间动态路由，个别模型可能返回空的核验结果，建议保持 `VERIFICATION_FAIL_CLOSED=false`：明确核验不通过时仍会拒答，核验器不可用时则依靠相关度门槛和强制有效引用继续回答。切换到稳定的公司模型后，可改为 `true` 获得最严格的故障拒答策略。
| `CLICKHOUSE_ENABLED` | 是否启用 ClickHouse 向量库 |
| `CLICKHOUSE_REQUIRED` | ClickHouse 故障时是否禁止降级 |
| `CLICKHOUSE_HOST/PORT` | ClickHouse 服务地址和 HTTP 端口 |
| `CLICKHOUSE_DATABASE` | ClickHouse 数据库名 |

## 支持的文件

| 类型 | 扩展名 | 来源定位 |
|---|---|---|
| PDF | `.pdf` | PDF 页码 |
| Word | `.docx` | 段落、表格 |
| Excel | `.xlsx`、`.xls` | 工作表与行号 |
| 文本 | `.txt`、`.md` | 文本内容 |
| 图片 | `.png`、`.jpg`、`.jpeg`、`.bmp`、`.tif`、`.tiff` | OCR 识别结果 |

扫描件和图片 OCR 依赖 Tesseract；中文环境请安装 `chi_sim` 语言包。

## 文档版本规则

推荐统一命名：

```text
员工守则_2024版.pdf
员工守则_2025版.pdf
员工守则_2026版.pdf
报销制度_V2.1.docx
```

未指定年份时，系统优先检索同系列最新版本；问题中明确包含 `2024` 等年份时，则选择对应历史版本。版本判断来自文件名，因此公司应制定统一命名规范。

系统会区分“完整版本”和“增量修订”：多个完整版本只使用最新一份；文件名包含“修订通知、修改决定、补充规定、补充通知、勘误”等字样时，视为增量修订。当前答案按“最新完整版本 + 其后的有效修订”组成，后续修订覆盖冲突事项，未被修改的内容继续继承。查询历史年份时，则重建截至该年份的有效版本链。

增量文件建议命名为 `员工守则_2025修订通知.pdf` 或 `员工守则_2026补充规定.pdf`。

## 在线文档接入

复制 `online_sources.example.json` 为资料目录下的 `online_sources.json`，按需启用来源。密钥字段填写的是环境变量名称，真实凭证只能保存在 `.env`。

### WPS / 金山文档

金山文档与 WPS 统一使用 WPS 365 开放平台。需要在开放平台创建企业应用，申请文件读取权限，并取得 `drive_id`、`file_id` 和 access token。若应用开启了接口签名，还需配置 APPID 和 APPKEY，程序会自动生成 KSO-1 签名。

### 腾讯文档

腾讯文档企业 API 需要在开放合作平台申请应用并通过审核。审核后，将官方提供的内容或导出接口填写到 `endpoint`，并配置 access token、Client ID 和 Open ID。由于不同合作应用开放的地址及响应字段可能不同，可通过 `TENCENT_DOCS_API_BASE_URL` 和 `content_field` 适配。

默认每 300 秒在下一次提问时检查同步一次，也可以点击网页侧栏的“立即同步知识库”。通过 `ONLINE_REFRESH_SECONDS` 可修改周期。

### 网页知识库管理

登录后点击侧栏“添加资料”，可直接选择：

- 文件上传：保存到服务器资料库并立即建立索引。
- WPS / 金山：填写 Drive ID、File ID 和开放平台凭证。
- 腾讯文档：填写审核后获得的开放接口与 OAuth 凭证。
- 云端资料库：配置 S3 兼容存储，可用于 AWS S3、腾讯 COS、阿里 OSS 或 MinIO。

Client Secret、Token、Access Key 等字段使用服务器会话密钥加密后保存到 `INTEGRATIONS_CONFIG`，接口和资料列表不会返回密钥原文。云端上传成功后，服务器会保留检索缓存，因此文档可立即进入 RAG。

## 项目结构

```text
app/
  main.py              FastAPI、登录与问答接口
  rag_engine.py        检索、重排、版本选择与大模型调用
  clickhouse_store.py  ClickHouse表、快照、权限过滤与HNSW检索
  model_store.py       回答模型加密配置、校验和连接测试
  document_loader.py   多格式文件读取
  online_sources.py    WPS/金山、腾讯文档与通用 HTTPS 连接器
  static/              网页前端
scripts/
  hash_password.py     登录密码哈希生成工具
  migrate_to_clickhouse.py  已有资料迁移和验证
source/                企业资料目录（内容不提交）
Dockerfile
docker-compose.yml
requirements.txt
```

## 安全与生产部署

- 管理中心的“系统日志”仅对管理员开放，记录请求路径、状态码、耗时和异常，不记录请求正文、密码、API Key 或文档内容；日志默认自动轮转。

- 不要提交 `.env`、真实密码、API Key、企业文档或索引缓存。
- 公网或正式内网域名应使用 Nginx/Caddy 反向代理并启用 HTTPS。
- 启用 HTTPS 后将 `COOKIE_HTTPS_ONLY=true`。
- 建议限制 vLLM、网站端口和资料目录的网络及文件权限。
- 支持管理员和员工独立账号、部门归属及资料可见范围。权限在检索前由服务器强制执行。
- 登录后可从“资料库”按权限查阅和搜索资料摘要；管理员可直接调整每份资料的授权范围。
- 管理员可在二次确认后删除资料，普通员工无删除按钮且删除接口会拒绝非管理员请求。
- 管理数据中心提供运行指标、操作审计和标准问题召回评测；评测不会调用生成模型。
- 上传或连接资料时可设为全公司可见、指定部门可见或仅管理员可见；既有资料升级后默认全公司可见。
- 员工与权限配置保存在 `ACCESS_CONTROL_CONFIG`，密码仅保存 PBKDF2 哈希，不保存明文。
- 管理员可以测试并切换回答模型；API Key 使用 Fernet 加密保存且接口不回显。
- `MODEL_ALLOWED_HOSTS` 应由 IT 固定，防止管理员配置未经批准的外部模型地址。
- `/api/health` 提供不含敏感错误的健康状态；详细状态仅管理员可见。

## 版本

当前版本：**v2.0.0（ClickHouse 企业版）**。详细内容见 [CHANGELOG.md](CHANGELOG.md)。

## License

本项目暂未声明开源许可证。公开仓库允许查看代码，但在公司外复制、分发或商用前，请由项目所有者补充并确认许可证。
