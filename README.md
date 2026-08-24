# Tier 企业 RAG 知识库助手

> **第一版（v1.0.0）** — 面向企业内网的多格式知识库问答网站。

将公司的 PDF、Word、Excel、文本和图片资料放入统一目录，员工即可通过网页提问。系统先使用 BGE、FAISS 和 Reranker 检索最相关的原文，再调用 OpenAI-compatible vLLM 生成简洁中文答案，并返回文件名、页码或工作表等来源信息。

## 第一版能力

- 独立网页与 FastAPI 后端，不依赖 Streamlit
- 用户名、密码哈希验证及登录失败限流
- 支持 PDF、DOCX、XLSX、XLS、TXT、Markdown 与常见图片 OCR
- BGE Embedding + FAISS 召回 + `bge-reranker-base` Top 3 重排
- 接入 OpenAI-compatible API，可对接公司内部 vLLM
- 回答附带来源；资料不足时明确说明未找到，避免编造
- 同系列制度默认选择最新版本，问题指定年份时可查询历史版本
- 资料发生变化后，在下一次提问时自动检查并更新索引
- Docker、WSL 或普通 Linux 服务器均可部署

## 工作流程

```text
企业资料 → 多格式读取与切分 → BGE + FAISS 召回
         → bge-reranker-base 精排 Top 3
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

## 项目结构

```text
app/
  main.py              FastAPI、登录与问答接口
  rag_engine.py        检索、重排、版本选择与大模型调用
  document_loader.py   多格式文件读取
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
- 第一版为单账号登录；多用户、角色权限和操作审计可在后续版本增加。

## 版本

当前版本：**v1.0.0（第一版）**。详细内容见 [CHANGELOG.md](CHANGELOG.md)。

## License

本项目暂未声明开源许可证。公开仓库允许查看代码，但在公司外复制、分发或商用前，请由项目所有者补充并确认许可证。
