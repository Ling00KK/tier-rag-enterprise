# IT 源码与运行数据交接清单

## 源码

从 GitHub 克隆仓库，记录交接 commit。依赖见 requirements.txt，配置模板见 .env.example，部署入口见 README.md。最新工作流功能尚待验收，应在隔离环境部署；源码上传不代表生产服务器已升级。

## 不在公开仓库中的内容

- SOURCE_DIR 中的真实公司资料。
- ClickHouse 数据及备份。
- 员工、部门、资料授权、在线连接、模型配置、审计记录和向量缓存。
- .env、SESSION_SECRET、API Key、数据库密码与其他凭证。
- 本地 BGE / Reranker 模型权重、虚拟环境和临时部署包。

以上内容从现有服务器备份，经公司批准的内部渠道交接。加密配置应与原解密密钥配套保管；不要把备份或凭证提交到公开 GitHub。索引可重新生成，但不能代替原始文档与权限配置备份。

## 新功能运行目录

- DOCUMENT_WORKFLOW_DIR：草稿、回收站和 workflow.sqlite3；未设置时位于 SOURCE_DIR 父目录下的 .document-workflow。必须放在资料扫描目录之外，并加入持久化卷及备份。
- TASK_DB_PATH：后台任务记录；默认位于 INTEGRATIONS_CONFIG 同目录的 tasks.sqlite3。
- BACKGROUND_WORKERS：单进程后台线程数，默认 2；不是多实例任务协调方案。

## 上线前验收

1. 备份代码、原始资料、权限配置、密钥和数据库，并验证恢复方法。
2. 先在测试环境验证依赖、模型和 ClickHouse 连接。
3. 验证不同部门权限、员工停用、草稿不可检索、发布与失败回滚。
4. 验证回收、恢复、同名文件冲突、云端副本处理和服务重启场景。
5. 测试完整问答评测；使用外部模型会发送问题与文档片段，需先获批准。
6. 配置 HTTPS、强密码、日志告警和备份后再上线。

详细要求见 production-checklist.md。代码中的实验功能与测试通过数量不构成生产安全保证。
