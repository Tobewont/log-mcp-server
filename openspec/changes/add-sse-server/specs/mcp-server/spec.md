# MCP Server 规范增量

## ADDED Requirements

### Requirement: SSE 服务器模式支持
系统应当支持通过 Server-Sent Events (SSE) 协议提供 MCP 服务，使客户端能够通过 HTTP 连接访问 MCP 工具。

#### Scenario: SSE 模式启动
- **WHEN** 配置 `server_mode=sse` 并启动服务器
- **THEN** 服务器应当监听指定的 HTTP 端口
- **AND** 提供 `/sse` 端点用于 MCP 通信
- **AND** 记录启动日志包含监听地址和端口

#### Scenario: MCP over HTTP 通信
- **WHEN** 客户端连接到 `/sse` 端点
- **THEN** 服务器应当建立 SSE 连接
- **AND** 支持完整的 MCP 协议交互
- **AND** 正确处理工具调用请求和响应

### Requirement: 健康检查端点
系统应当提供 HTTP 健康检查端点，用于容器编排和负载均衡器检查服务状态。

#### Scenario: 健康检查成功
- **WHEN** 向 `/health` 端点发送 GET 请求
- **THEN** 返回 HTTP 200 状态码
- **AND** 响应体包含服务状态信息
- **AND** 包含 Loki 连接状态检查

#### Scenario: 健康检查失败
- **WHEN** Loki 服务不可用时访问 `/health` 端点
- **THEN** 返回 HTTP 503 状态码
- **AND** 响应体包含错误信息

### Requirement: 双模式配置支持
系统应当同时支持 stdio 和 SSE 两种服务器模式，通过配置进行选择。

#### Scenario: stdio 模式兼容性
- **WHEN** 配置 `server_mode=stdio` 或未配置模式
- **THEN** 服务器应当使用 stdio 模式启动
- **AND** 保持现有的 stdio 通信功能
- **AND** 不监听任何网络端口

#### Scenario: SSE 模式配置
- **WHEN** 配置 `server_mode=sse`
- **THEN** 服务器应当使用 SSE 模式启动
- **AND** 监听配置的主机地址和端口
- **AND** 提供 HTTP 端点服务

### Requirement: 服务器配置管理
系统应当支持通过环境变量和配置文件管理服务器模式相关配置。

#### Scenario: 环境变量配置
- **WHEN** 设置环境变量 `MCP_SERVER_MODE=sse`
- **THEN** 服务器应当使用 SSE 模式
- **AND** 支持 `MCP_SERVER_HOST` 和 `MCP_SERVER_PORT` 配置

#### Scenario: 配置文件支持
- **WHEN** 在配置文件中设置 `server.mode=sse`
- **THEN** 服务器应当读取并应用服务器配置
- **AND** 支持主机地址和端口配置

### Requirement: 容器端口暴露
在容器环境中，系统应当默认使用 SSE 模式并暴露 HTTP 端口。

#### Scenario: Docker 容器端口暴露
- **WHEN** 在 Docker 容器中运行
- **THEN** 应当暴露配置的 HTTP 端口
- **AND** 支持容器间网络通信
- **AND** 支持端口映射到宿主机

#### Scenario: Docker Compose 网络访问
- **WHEN** 通过 Docker Compose 部署
- **THEN** 其他服务应当能够通过服务名访问 MCP 服务器
- **AND** 宿主机应当能够通过映射端口访问服务

### Requirement: 错误处理和日志
系统应当提供完善的错误处理和日志记录，支持 SSE 模式的调试和监控。

#### Scenario: 端口占用处理
- **WHEN** 配置的端口已被占用
- **THEN** 服务器应当记录错误日志并退出
- **AND** 提供清晰的错误信息和解决建议

#### Scenario: 连接错误处理
- **WHEN** SSE 连接过程中发生错误
- **THEN** 服务器应当记录详细的错误信息
- **AND** 优雅地关闭连接
- **AND** 不影响其他活跃连接
