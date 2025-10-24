# SSE 服务器模式设计文档

## Context

当前 Loki MCP Server 只支持 stdio 模式，限制了其在网络环境和容器化部署中的使用。需要添加 SSE (Server-Sent Events) 模式支持，使其能够作为 HTTP 服务运行。

## Goals / Non-Goals

### Goals
- 添加 SSE 服务器模式支持 MCP over HTTP
- 保持 stdio 模式的向后兼容性
- 支持容器化部署和网络访问
- 提供健康检查端点
- 简化客户端连接配置

### Non-Goals
- 不移除现有的 stdio 模式
- 不改变现有的工具接口
- 不添加认证授权（后续功能）
- 不支持 WebSocket 模式（MCP 标准未定义）

## Technical Decisions

### 1. MCP 协议实现
- **选择**: 使用 MCP SDK 的 SSE 服务器实现
- **原因**: 官方支持，标准兼容
- **替代方案**: 自实现 HTTP 协议处理（复杂度高）

### 2. HTTP 框架选择
- **选择**: 使用 `mcp.server.sse` 内置实现
- **原因**: 与 MCP SDK 集成，减少依赖
- **替代方案**: FastAPI/Starlette（增加依赖复杂度）

### 3. 服务器模式配置
- **选择**: 通过 `server_mode` 配置项选择模式
- **原因**: 明确的模式切换，易于理解
- **替代方案**: 自动检测（可能导致意外行为）

### 4. 端口配置
- **选择**: 默认端口 8080
- **原因**: 常用的非特权端口，容器友好
- **替代方案**: 3000/8000（可能与其他服务冲突）

### 5. Docker 默认模式
- **选择**: 容器环境默认使用 SSE 模式
- **原因**: 容器通常作为网络服务运行
- **替代方案**: 保持 stdio 默认（不符合容器使用场景）

## Architecture

### 服务器架构
```
┌─────────────────┐    ┌──────────────────┐
│   main.py       │    │  ServerFactory   │
│                 │    │                  │
│ ┌─────────────┐ │    │ ┌──────────────┐ │
│ │Config Loader│ │────┤ │ Mode Selector│ │
│ └─────────────┘ │    │ └──────────────┘ │
└─────────────────┘    └──────────────────┘
         │                       │
         ▼                       ▼
┌─────────────────┐    ┌──────────────────┐
│  StdioServer    │    │   SSEServer      │
│                 │    │                  │
│ ┌─────────────┐ │    │ ┌──────────────┐ │
│ │   stdio     │ │    │ │ HTTP Handler │ │
│ │ transport   │ │    │ │              │ │
│ └─────────────┘ │    │ │ /sse         │ │
└─────────────────┘    │ │ /health      │ │
                       │ └──────────────┘ │
                       └──────────────────┘
```

### HTTP 端点设计
```
GET  /sse          # MCP over SSE 主端点
GET  /health       # 健康检查端点
GET  /             # 服务信息页面（可选）
```

### 配置结构
```python
@dataclass
class ServerConfig:
    mode: Literal["stdio", "sse"] = "stdio"
    host: str = "0.0.0.0"
    port: int = 8080
    cors_origins: List[str] = field(default_factory=list)
```

## Implementation Plan

### Phase 1: 核心 SSE 服务器
1. 创建 `SSEServer` 类
2. 实现 MCP over HTTP 协议
3. 添加健康检查端点
4. 基本错误处理

### Phase 2: 配置集成
1. 扩展配置系统
2. 添加环境变量支持
3. 更新配置验证
4. 添加配置示例

### Phase 3: 主程序重构
1. 创建服务器工厂
2. 重构 main.py 支持模式选择
3. 统一信号处理
4. 优化启动流程

### Phase 4: Docker 集成
1. 更新 Dockerfile
2. 配置端口暴露
3. 更新 docker-compose.yml
4. 添加健康检查

## Configuration Examples

### 环境变量
```bash
# 服务器模式
MCP_SERVER_MODE=sse
MCP_SERVER_HOST=0.0.0.0
MCP_SERVER_PORT=8080

# Loki 配置保持不变
LOKI_ADDR=http://loki:3100
```

### 配置文件
```yaml
# loki-config.yaml
server:
  mode: sse
  host: 0.0.0.0
  port: 8080
  cors_origins: []

loki:
  addr: "http://loki:3100"
  # ... 其他配置
```

## Client Connection Examples

### HTTP 客户端连接
```javascript
// JavaScript 客户端示例
const client = new MCPClient({
  transport: 'sse',
  url: 'http://localhost:8080/sse'
});

await client.connect();
```

### Claude Desktop 配置
```json
{
  "mcpServers": {
    "loki": {
      "transport": "sse",
      "url": "http://localhost:8080/sse"
    }
  }
}
```

### Docker 网络访问
```bash
# 容器内访问
curl http://loki-mcp-server:8080/health

# 宿主机访问
curl http://localhost:8080/health
```

## Security Considerations

### 当前阶段
- 无认证授权（开发环境）
- CORS 配置支持
- 容器网络隔离

### 未来考虑
- API 密钥认证
- TLS/HTTPS 支持
- 请求限流
- 访问日志

## Performance Considerations

### SSE 连接管理
- 连接池大小限制
- 超时配置
- 内存使用监控

### 并发处理
- 异步请求处理
- 工具调用队列
- 资源限制

## Risks and Mitigations

### 风险 1: 向后兼容性
- **风险**: stdio 模式功能回归
- **缓解**: 保持现有测试，添加模式切换测试

### 风险 2: 端口冲突
- **风险**: 默认端口被占用
- **缓解**: 可配置端口，提供端口检测

### 风险 3: 网络安全
- **风险**: 无认证的网络暴露
- **缓解**: 文档说明，后续添加认证

### 风险 4: 性能影响
- **风险**: HTTP 开销影响性能
- **缓解**: 性能测试，优化配置

## Migration Guide

### 从 stdio 到 SSE
1. 更新配置文件添加服务器配置
2. 设置环境变量 `MCP_SERVER_MODE=sse`
3. 更新客户端连接配置
4. 测试网络连接

### Docker 部署更新
1. 重新构建镜像
2. 更新 docker-compose.yml 端口映射
3. 验证健康检查
4. 更新客户端连接地址

## Testing Strategy

### 单元测试
- SSE 服务器基本功能
- 配置加载和验证
- 端点响应测试

### 集成测试
- stdio 模式兼容性
- SSE 模式完整流程
- Docker 容器测试

### 性能测试
- 并发连接测试
- 内存使用监控
- 响应时间测试
