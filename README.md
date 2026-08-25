# Tier 企业 RAG 知识库助手

> **第一版（v1.0.0）** — 面向企业内网的多格式知识库问答网站。

将公司的 PDF、Word、Excel、文本和图片资料放入统一目录，员工即可通过网页提问。系统使用 BM25 关键词检索与 BGE/FAISS 语义检索进行混合召回，再经 Reranker 精排，最后调用 OpenAI-compatible vLLM 生成简洁中文答案，并返回文件名、页码或工作表等来源信息。

## 第一版能力

- 独立网页与 FastAPI 后端，不依赖 Streamlit
- 用户名、密码哈希验证及登录失败限流
- 支持 PDF、DOCX、XLSX、XLS、TXT、Markdown 与常见图片 OCR
- BM25 + BGE/FAISS 混合召回、RRF 融合及 `bge-reranker-base` Top 3 重排
- 段落边界切分、上下文重叠和 Excel 短行保留
- SQLite 增量向量缓存与低相关度拒答
- 接入 OpenAI-compatible API，可对接公司内部 vLLM
- 回答附带来源；资料不足时明确说明未找到，避免编造
- 同系列制度默认选择最新版本，问题指定年份时可查询历史版本
- 资料发生变化后，在下一次提问时自动检查并更新索引
- WPS/金山文档与腾讯文档在线来源连接器，可定时或手动同步
- Docker、WSL 或普通 Linux 服务器均可部署

## 工作流程

```text
企业资料 → 多格式读取与结构化切分 → BM25 + BGE/FAISS 混合召回
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
  document_loader.py   多格式文件读取
  online_sources.py    WPS/金山、腾讯文档与通用 HTTPS 连接器
  static/              网页前端
scripts/
  hash_password.py     登录密码哈希生成工具
source/                企业资料目录（内容不提交）
Dockerfile
docker-compose.yml
requirements.txt
```

## 安全与生产部署

- 不要提交 `.env`、真实密码、API Key、企业文档或索引缓存。
- 公网或正式内网域名应使用 Nginx/Caddy 反向代理并启用 HTTPS。
- 启用 HTTPS 后将 `COOKIE_HTTPS_ONLY=true`。
- 建议限制 vLLM、网站端口和资料目录的网络及文件权限。
- 支持管理员和员工独立账号、部门归属及资料可见范围。权限在检索前由服务器强制执行。
- 登录后可从“资料库”按权限查阅和搜索资料摘要；管理员可直接调整每份资料的授权范围。
- 管理员可在二次确认后删除资料，普通员工无删除按钮且删除接口会拒绝非管理员请求。
- 上传或连接资料时可设为全公司可见、指定部门可见或仅管理员可见；既有资料升级后默认全公司可见。
- 员工与权限配置保存在 `ACCESS_CONTROL_CONFIG`，密码仅保存 PBKDF2 哈希，不保存明文。

## 版本

当前版本：**v1.0.0（第一版）**。详细内容见 [CHANGELOG.md](CHANGELOG.md)。

## License

本项目暂未声明开源许可证。公开仓库允许查看代码，但在公司外复制、分发或商用前，请由项目所有者补充并确认许可证。
