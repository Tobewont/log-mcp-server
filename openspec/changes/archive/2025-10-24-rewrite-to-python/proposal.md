## Why

当前的 TypeScript 实现依赖 logcli 命令行工具，这增加了部署复杂性和依赖管理的负担。将项目重写为 Python 可以更好地与 MCP 生态系统集成，并且 Python 在数据处理和 API 集成方面具有更丰富的生态系统。同时，移除 logcli 依赖，仅使用 HTTP API 可以简化架构并提高可靠性。

## What Changes

- **BREAKING**: 将整个项目从 TypeScript 重写为 Python
- **BREAKING**: 移除 logcli 命令行工具依赖，仅使用 Loki HTTP API
- **BREAKING**: 移除 `LOKI_TENANT_ID` 环境变量，改为通过工具参数传递租户信息
- 添加新工具：`health_check` - 获取 Loki 健康状态和当前时间
- 添加新工具：`get_tenants` - 获取所有可用租户列表
- 优化现有工具：为 `get_labels`、`get_label_values`、`query_loki` 添加必需的 `tenant` 参数
- 保持与现有 MCP 接口的兼容性
- 使用 Python 异步编程模式优化性能

## Impact

- Affected specs: mcp-server (新建规范)
- Affected code: 
  - 删除所有 TypeScript 源文件 (`src/`, `package.json`, `tsconfig.json` 等)
  - 新建 Python 项目结构 (`src/`, `requirements.txt`, `pyproject.toml` 等)
  - 重写 MCP 服务器实现
  - 重写 Loki 客户端实现
  - 更新配置管理和认证机制
  - 更新文档和部署脚本
