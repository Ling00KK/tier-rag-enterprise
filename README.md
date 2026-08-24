# 企业 RAG 知识库助手

面向公司内网部署的多格式企业知识库网站。支持 PDF、Word、Excel、文本和图片 OCR，使用 BGE Embedding、FAISS、bge-reranker 与 OpenAI-compatible vLLM 完成检索增强问答。

## 特性

- 正式网页与后端 API，不依赖 Streamlit
- 登录会话、密码哈希、失败限流
- 多格式资料统一读取与来源定位
- 同系列制度默认使用最新版本，明确年份时查询历史版本
- Docker 部署，适合交给 IT 绑定内网域名和 HTTPS
- 企业资料、密码和缓存默认不进入 Git

## 快速部署

1. 复制 `.env.example` 为 `.env`。
2. 运行 `python scripts/hash_password.py`，把输出填入 `.env`。
3. 为 `SESSION_SECRET` 设置至少 32 位随机字符串。
4. 把企业资料放入 `source/`（该目录内容不会提交到 Git）。
5. 执行 `docker compose up -d --build`。
6. 打开 `http://服务器IP:8501`。

生产环境应由 IT 使用 Nginx/Caddy 反向代理并启用 HTTPS，然后把 `COOKIE_HTTPS_ONLY` 改为 `true`。服务器必须能够访问配置的 vLLM 内网地址。

## 配置

所有运行配置均由 `.env` 提供，仓库只保留无机密的 `.env.example`。请勿提交真实密码、API 密钥或企业资料。

## 文档版本命名

推荐：`员工守则_2026版.pdf`、`报销制度_V2.1.docx`。默认问题只检索同系列最新版本；问题明确包含年份时，使用对应历史版本。
