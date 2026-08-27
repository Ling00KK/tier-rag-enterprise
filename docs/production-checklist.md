# 正式部署检查清单

## 必须完成

- ClickHouse 版本不低于 25.8，推荐 26.3 LTS 或公司批准版本。
- 为应用创建独立 ClickHouse 用户和强随机密码，不使用 `default` 用户。
- 设置 `CLICKHOUSE_ENABLED=true` 与 `CLICKHOUSE_REQUIRED=true`。
- 将 ClickHouse 仅开放给应用服务器，不直接暴露公网。
- 将 `MODEL_ALLOWED_HOSTS` 固定为获批准的公司模型或 OpenRouter 主机。
- 使用不少于 32 位的随机 `SESSION_SECRET`，启用 HTTPS 后设置 `COOKIE_HTTPS_ONLY=true`。
- `.env`、企业资料、模型密钥和 ClickHouse 密码不得提交到 Git。
- 配置 ClickHouse 数据卷备份、恢复演练、磁盘空间和查询延迟告警。
- 先用虚构资料完成权限交叉测试，再导入真实资料。

## 上线验收

- `/api/health` 返回 `healthy`，且向量存储显示 `clickhouse`。
- 管理员详细状态中 ClickHouse 版本、延迟、资料数和切片数正确。
- 人事、财务等测试账号无法获取其他部门的资料或答案。
- 模型连接测试通过，API Key 保存后不会在页面或接口回显。
- 同步失败时旧快照仍可查询；修复后重新同步可切换到新快照。
- 完成标准问题评测，并记录通过率和平均延迟基线。

## 仍需公司基础设施提供

- 域名、HTTPS证书、反向代理和防火墙策略。
- ClickHouse高可用、备份、监控和容量规划。
- 公司统一身份认证（AD/LDAP/OIDC）及账号离职联动。
- 对外部模型的数据安全审批、调用额度和供应商协议。
