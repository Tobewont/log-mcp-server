## Why

当前的 Loki MCP Server 缺乏标准化的容器化部署方案，这限制了项目的可移植性和部署灵活性。添加 Docker 支持将使项目能够在任何支持容器的环境中一致运行，简化部署流程，并为开发、测试和生产环境提供统一的运行环境。

## What Changes

- 添加 `Dockerfile` 用于构建 Loki MCP Server 容器镜像
- 添加 `docker-compose.yml` 用于本地开发和测试环境的快速部署
- 添加 `.dockerignore` 文件优化构建上下文
- 更新 README.md 文档，包含 Docker 部署说明
- 添加多阶段构建支持，优化镜像大小
- 支持健康检查和优雅关闭
- 提供环境变量配置示例

## Impact

- Affected specs: mcp-server (添加容器化部署需求)
- Affected code:
  - 新增 `Dockerfile` - 多阶段构建配置
  - 新增 `docker-compose.yml` - 编排配置
  - 新增 `.dockerignore` - 构建优化
  - 更新 `README.md` - 部署文档
  - 可能需要调整 `src/loki_mcp_server/main.py` - 添加健康检查端点
