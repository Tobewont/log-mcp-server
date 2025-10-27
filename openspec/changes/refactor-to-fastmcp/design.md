# FastMCP 重构设计文档

## Context

当前的 Loki MCP Server 使用自定义的服务器架构，包含复杂的抽象层和多种服务器实现。这导致了代码复杂性高、维护困难，特别是 SSE 模式的实现存在问题，无法正确处理 MCP 协议。

FastMCP 是 MCP SDK 提供的现代化服务器框架，提供了开箱即用的功能和最佳实践。

## Goals / Non-Goals

### Goals
- 简化项目架构，减少代码复杂度
- 修复 SSE 模式的 MCP 协议处理问题
- 提升开发效率和代码可维护性
- 保持所有现有功能和 API 兼容性
- 改善性能和资源使用

### Non-Goals
- 不改变 Loki 客户端实现
- 不修改工具的业务逻辑
- 不改变 Docker 部署方式
- 不影响现有的配置文件格式

## Technical Decisions

### 1. FastMCP vs 自定义实现
- **选择**: 使用 FastMCP 框架
- **原因**: 
  - 官方支持，标准兼容
  - 内置 stdio 和 HTTP/SSE 支持
  - 自动协议处理
  - 更好的性能和稳定性
- **替代方案**: 修复当前实现（工作量大，维护困难）

### 2. 工具实现模式
- **选择**: 使用 FastMCP 装饰器模式
- **原因**: 
  - 简化工具注册
  - 自动类型验证
  - 更好的错误处理
- **替代方案**: 保持当前类模式（与 FastMCP 不兼容）

### 3. 配置管理
- **选择**: 保持现有配置系统，移除服务器模式配置
- **原因**: 
  - FastMCP 自动检测运行模式
  - 减少配置复杂度
  - 保持向后兼容
- **替代方案**: 完全重写配置系统（破坏性太大）

### 4. 项目结构
- **选择**: 扁平化结构，移除 server/ 目录
- **原因**: 
  - FastMCP 处理服务器逻辑
  - 减少抽象层
  - 更直观的代码组织
- **替代方案**: 保持现有结构（增加复杂度）

## Architecture

### 新架构概览
```
src/loki_mcp_server/
├── __init__.py
├── main.py              # FastMCP 应用入口
├── config.py            # 简化的配置管理
├── client/              # Loki 客户端（保持不变）
│   ├── loki_client.py
│   ├── http_client.py
│   └── auth.py
├── tools/               # FastMCP 工具实现
│   ├── __init__.py
│   ├── health_check.py  # @mcp.tool 装饰器
│   ├── tenants.py       # @mcp.tool 装饰器
│   ├── query.py         # @mcp.tool 装饰器
│   └── labels.py        # @mcp.tool 装饰器
└── utils/               # 工具函数（保持不变）
    ├── errors.py
    └── logging.py
```

### FastMCP 集成模式
```python
from mcp.server import FastMCP

# 创建 FastMCP 实例
mcp = FastMCP(
    name="loki-mcp-server",
    host="0.0.0.0",
    port=8080,
    debug=True
)

# 使用装饰器注册工具
@mcp.tool()
async def health_check() -> dict:
    """检查 Loki 服务器健康状态"""
    # 实现逻辑
    pass

# 启动服务器
if __name__ == "__main__":
    mcp.run()
```

### 运行模式检测
FastMCP 自动检测运行环境：
- **Stdio 模式**: 当从 stdin/stdout 运行时自动启用
- **HTTP 模式**: 当指定端口时启用 HTTP/SSE 服务器
- **自动切换**: 无需手动配置

### 工具注册流程
```python
# 旧方式（复杂）
class HealthCheckTool:
    def get_tool_definition(self): ...
    async def execute(self, args): ...

server.register_tool(HealthCheckTool())

# 新方式（简单）
@mcp.tool()
async def health_check() -> dict:
    """检查健康状态"""
    return await loki_client.health_check()
```

## Implementation Plan

### Phase 1: 基础重构
1. 更新依赖项，添加 FastMCP
2. 创建新的 main.py 使用 FastMCP
3. 重构一个工具作为示例
4. 验证基本功能

### Phase 2: 工具迁移
1. 逐个重构所有工具
2. 保持相同的输入输出接口
3. 测试每个工具的功能
4. 验证错误处理

### Phase 3: 清理和优化
1. 移除旧的服务器实现
2. 清理不需要的代码
3. 优化配置管理
4. 更新文档

### Phase 4: 部署和测试
1. 更新 Docker 配置
2. 测试两种运行模式
3. 性能基准测试
4. 集成测试

## Configuration Changes

### 移除的配置
```yaml
# 不再需要
server_mode: "sse"
server_host: "0.0.0.0"
server_port: 8080
```

### 新增的配置
```yaml
# FastMCP 配置
fastmcp:
  debug: false
  host: "0.0.0.0"
  port: 8080
  mount_path: "/"
```

### 环境变量映射
```bash
# 旧变量（移除）
MCP_SERVER_MODE=sse
MCP_SERVER_HOST=0.0.0.0
MCP_SERVER_PORT=8080

# 新变量（可选）
FASTMCP_DEBUG=false
FASTMCP_HOST=0.0.0.0
FASTMCP_PORT=8080
```

## Error Handling Strategy

### FastMCP 错误处理
- 自动捕获工具执行异常
- 标准化 MCP 错误响应格式
- 内置请求验证和类型检查
- 详细的错误日志记录

### 自定义错误处理
```python
@mcp.tool()
async def query_loki(tenant: str, query: str) -> dict:
    try:
        result = await loki_client.query(tenant, query)
        return {"status": "success", "data": result}
    except LokiError as e:
        # FastMCP 自动处理异常转换
        raise MCPError(f"Loki query failed: {e}")
```

## Performance Considerations

### 内存优化
- FastMCP 使用更高效的请求处理
- 减少抽象层开销
- 更好的连接池管理

### 并发处理
- FastMCP 内置异步处理
- 自动请求队列管理
- 更好的资源利用

### 启动时间
- 减少初始化代码
- 更快的工具注册
- 优化的依赖加载

## Migration Strategy

### 渐进式迁移
1. 保持旧代码作为备份
2. 逐步替换组件
3. 并行测试新旧实现
4. 确认功能完整后删除旧代码

### 回滚计划
- 保留 git 分支用于回滚
- 文档化所有变更
- 准备快速恢复方案

### 用户影响最小化
- 保持 API 兼容性
- 相同的配置文件格式
- 相同的 Docker 使用方式

## Testing Strategy

### 单元测试
- 测试每个工具的功能
- 模拟 Loki 客户端响应
- 验证错误处理

### 集成测试
- 测试完整的 MCP 协议流程
- 验证 stdio 和 HTTP 模式
- 测试与真实 Loki 的集成

### 性能测试
- 并发请求处理
- 内存使用监控
- 响应时间基准

## Risks and Mitigations

### 风险 1: FastMCP 学习曲线
- **风险**: 团队需要学习新框架
- **缓解**: 提供详细文档和示例

### 风险 2: 功能回归
- **风险**: 重构可能引入 bug
- **缓解**: 全面测试，保持 API 兼容

### 风险 3: 性能影响
- **风险**: 新框架可能影响性能
- **缓解**: 性能基准测试，持续监控

### 风险 4: 依赖风险
- **风险**: 增加对 FastMCP 的依赖
- **缓解**: FastMCP 是官方框架，稳定可靠

## Success Metrics

### 代码质量
- 减少 50% 的代码行数
- 提高测试覆盖率到 90%
- 减少复杂度指标

### 性能指标
- 启动时间减少 30%
- 内存使用减少 20%
- 响应时间保持或改善

### 开发效率
- 新工具开发时间减少 40%
- Bug 修复时间减少
- 文档维护工作量减少
