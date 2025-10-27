# 重构为 FastMCP 架构

## Why

当前的 Loki MCP Server 实现存在以下问题：

1. **复杂的服务器架构**：手动实现了多种服务器模式（stdio、SSE、HTTP），代码复杂且维护困难
2. **SSE 实现不完整**：当前的 SSE 服务器实现无法正确处理 MCP 协议，工具无法正常加载
3. **重复代码**：多个服务器实现之间存在大量重复的工具注册和处理逻辑
4. **缺乏标准化**：没有使用 MCP SDK 提供的标准化解决方案

FastMCP 提供了一个现代化的、功能完整的 MCP 服务器框架，具有以下优势：
- 内置支持 stdio 和 SSE 模式
- 自动处理 MCP 协议细节
- 简化的工具注册和配置
- 更好的错误处理和日志记录
- 内置的健康检查和调试功能

## What Changes

### 架构重构
- **BREAKING**: 使用 FastMCP 替换当前的自定义服务器实现
- **BREAKING**: 简化项目结构，移除复杂的服务器抽象层
- **BREAKING**: 统一工具实现，使用 FastMCP 的装饰器模式

### 新增功能
- **自动模式检测**: FastMCP 自动检测运行环境（stdio vs HTTP）
- **内置 Web UI**: FastMCP 提供调试和测试界面
- **改进的错误处理**: 标准化的 MCP 错误响应
- **性能优化**: 使用 FastMCP 的优化实现

### 配置简化
- 简化服务器配置，移除复杂的模式选择逻辑
- 保持 Loki 连接配置不变
- 添加 FastMCP 特定的配置选项

### 文件结构优化
- 移除 `server/` 目录的复杂实现
- 简化 `main.py` 为单一入口点
- 重构工具实现使用 FastMCP 装饰器
- 保持 `client/` 和 `utils/` 目录不变

## Impact

### 受影响的规范
- `specs/mcp-server/spec.md` - 更新服务器实现要求

### 受影响的代码
- `src/loki_mcp_server/main.py` - 完全重写使用 FastMCP
- `src/loki_mcp_server/server/` - 整个目录将被移除
- `src/loki_mcp_server/tools/` - 重构为 FastMCP 装饰器模式
- `src/loki_mcp_server/config.py` - 简化配置管理
- `requirements.txt` - 更新依赖项
- `docker-compose.yml` - 更新端口和环境变量配置
- `README.md` - 更新使用说明

### 兼容性
- **向后兼容**: Loki 连接和认证配置保持不变
- **工具兼容**: 所有现有工具功能保持不变
- **Docker 兼容**: 容器化部署方式保持不变
- **BREAKING**: 服务器模式配置方式改变（FastMCP 自动检测）

### 性能影响
- **正面**: 使用 FastMCP 的优化实现，性能提升
- **正面**: 减少代码复杂度，降低内存占用
- **正面**: 更好的并发处理能力
