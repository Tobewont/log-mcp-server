# 添加 SSE 服务器模式支持

## Why

当前的 Loki MCP Server 只支持 stdio 模式，通过标准输入/输出与客户端通信。这种模式适用于本地进程间通信（如 Claude Desktop），但不适用于网络服务场景。

需要添加 SSE (Server-Sent Events) 模式支持，使 MCP 服务器能够：
1. 监听网络端口提供 HTTP 服务
2. 支持远程客户端连接
3. 在容器环境中作为网络服务运行
4. 支持负载均衡和集群部署

## What Changes

### 新增功能
- **SSE 服务器模式**: 添加基于 HTTP/SSE 的 MCP 服务器实现
- **双模式支持**: 同时支持 stdio 和 SSE 模式，通过配置选择
- **HTTP 端点**: 提供标准的 MCP over HTTP 端点
- **健康检查端点**: 添加 `/health` 端点用于容器健康检查

### 配置变更
- 添加 `server_mode` 配置项（stdio/sse）
- 添加 `listen_host` 和 `listen_port` 配置项
- 更新环境变量支持

### Docker 配置更新
- **BREAKING**: Docker 容器默认使用 SSE 模式
- 暴露 HTTP 端口（默认 8080）
- 更新 docker-compose.yml 配置端口映射

### 文档更新
- 更新 README.md 说明两种模式的使用
- 添加客户端连接示例
- 更新 Docker 部署文档

## Impact

### 受影响的规范
- `specs/mcp-server/spec.md` - 添加 SSE 模式要求

### 受影响的代码
- `src/loki_mcp_server/main.py` - 添加模式选择逻辑
- `src/loki_mcp_server/config.py` - 添加服务器配置
- `src/loki_mcp_server/server/` - 新增 SSE 服务器实现
- `Dockerfile` - 暴露端口
- `docker-compose.yml` - 添加端口映射
- `README.md` - 更新文档

### 兼容性
- **向后兼容**: stdio 模式保持不变
- **Docker 环境**: 默认使用 SSE 模式，需要更新客户端连接方式
