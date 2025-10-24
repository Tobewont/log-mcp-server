## Context

当前项目是一个基于 TypeScript 的 MCP 服务器，用于与 Grafana Loki 日志系统集成。现有实现依赖 logcli 命令行工具，这在部署和维护方面带来了额外的复杂性。用户希望将项目重写为 Python，并移除 logcli 依赖，仅使用 HTTP API 进行交互。

## Goals / Non-Goals

### Goals
- 将项目完全重写为 Python，遵循项目约定中的 Python 代码规范
- 移除 logcli 依赖，仅使用 Loki HTTP API
- 添加健康检查和租户发现功能
- 为现有工具添加租户参数支持
- 保持 MCP 协议兼容性
- 提供更好的异步性能和错误处理

### Non-Goals
- 保持与 TypeScript 版本的代码结构一致性
- 支持 logcli 作为备选方案
- 向后兼容旧的环境变量配置

## Decisions

### 技术栈选择
- **Python 3.8+**: 利用现代 Python 特性和类型注解
- **MCP SDK for Python**: 使用官方 Python MCP SDK
- **aiohttp/httpx**: 异步 HTTP 客户端，提供更好的性能
- **pydantic**: 数据验证和设置管理
- **pytest**: 测试框架
- **black + flake8 + mypy**: 代码质量工具

### 架构决策

#### 1. 模块化设计
```
src/
├── loki_mcp_server/
│   ├── __init__.py
│   ├── main.py              # MCP 服务器入口
│   ├── config.py            # 配置管理
│   ├── client/
│   │   ├── __init__.py
│   │   ├── loki_client.py   # Loki HTTP 客户端
│   │   └── auth.py          # 认证处理
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── health_check.py
│   │   ├── tenants.py
│   │   ├── query.py
│   │   └── labels.py
│   └── utils/
│       ├── __init__.py
│       ├── errors.py
│       └── logging.py
```

#### 2. 配置管理
- 使用 pydantic Settings 进行配置管理
- 支持环境变量和配置文件
- 移除 `LOKI_TENANT_ID`，改为工具参数

#### 3. 异步架构
- 全面使用 async/await 模式
- 异步 HTTP 客户端处理 Loki API 调用
- 连接池和请求重试机制

#### 4. 租户支持
- 每个工具调用都需要指定租户参数
- 通过 `X-Scope-OrgID` 头部传递租户信息
- 新增租户发现功能

### Alternatives Considered

#### 1. 保留 logcli 支持
**决定**: 完全移除 logcli 依赖
**原因**: 
- 简化部署和依赖管理
- HTTP API 提供更好的错误处理
- 减少外部工具依赖的维护负担

#### 2. 渐进式迁移
**决定**: 完全重写
**原因**:
- TypeScript 和 Python 的架构差异较大
- 重写可以更好地利用 Python 生态系统
- 避免维护两套代码的复杂性

#### 3. 保持环境变量兼容性
**决定**: 移除 `LOKI_TENANT_ID` 环境变量
**原因**:
- 多租户支持需要动态指定租户
- 工具参数提供更好的灵活性
- 符合 MCP 工具设计原则

## Risks / Trade-offs

### 风险
1. **兼容性风险**: 现有用户需要更新配置和使用方式
   - **缓解**: 提供详细的迁移指南和示例配置
   
2. **功能回归风险**: 重写可能遗漏现有功能
   - **缓解**: 全面的测试覆盖和功能对比验证

3. **性能风险**: Python 可能比 TypeScript 性能略低
   - **缓解**: 使用异步编程和连接池优化

### 权衡
1. **开发效率 vs 运行性能**: 选择 Python 提高开发效率，可能牺牲一些运行性能
2. **灵活性 vs 复杂性**: 租户参数化增加了灵活性，但也增加了使用复杂性

## Migration Plan

### 阶段 1: 准备和设置 (1-2 天)
1. 创建 Python 项目结构
2. 设置开发环境和工具链
3. 实现基础配置管理

### 阶段 2: 核心功能实现 (3-5 天)
1. 实现 Loki HTTP 客户端
2. 实现 MCP 服务器框架
3. 实现基础工具功能

### 阶段 3: 高级功能和优化 (2-3 天)
1. 实现新增工具 (health_check, get_tenants)
2. 添加租户参数支持
3. 性能优化和错误处理

### 阶段 4: 测试和文档 (2-3 天)
1. 编写全面的测试套件
2. 更新文档和示例
3. 验证功能完整性

### 回滚计划
- 保留 TypeScript 代码的 git 标签
- 如果发现重大问题，可以快速回滚到 TypeScript 版本
- 提供并行部署选项，允许用户选择版本

## Open Questions

1. **MCP Python SDK 兼容性**: 需要验证 Python MCP SDK 的功能完整性和稳定性
2. **性能基准**: 需要建立性能基准测试，确保 Python 版本性能可接受
3. **包发布策略**: 是否需要更改包名或版本号来反映重大变更
4. **向后兼容性**: 是否需要提供配置迁移工具
