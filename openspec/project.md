# Project Context

## Purpose
Loki MCP Server 是一个基于 Model Context Protocol (MCP) 的服务器实现，用于与 Grafana Loki 日志聚合系统进行集成。该项目旨在提供一个标准化的接口，使 AI 助手能够查询、分析和处理存储在 Loki 中的日志数据。

## Tech Stack
- **Python** - 主要编程语言
- **MCP (Model Context Protocol)** - 核心协议框架
- **Grafana Loki** - 日志聚合和存储系统
- **LogQL** - Loki 查询语言
- **OpenSpec** - 规范驱动开发框架

## Project Conventions

### Code Style
- 使用 Python 标准代码风格 (PEP 8)
- 函数和变量使用 snake_case 命名
- 类名使用 PascalCase 命名
- 常量使用 UPPER_SNAKE_CASE 命名
- 使用类型注解提高代码可读性
- 文档字符串使用 Google 风格

### Architecture Patterns
- **MCP 服务器模式**: 实现标准的 MCP 服务器接口
- **插件化架构**: 支持不同类型的 Loki 查询和操作
- **异步处理**: 使用异步编程模式处理 I/O 密集型操作
- **错误处理**: 统一的错误处理和日志记录机制
- **配置管理**: 基于环境变量和配置文件的设置管理

### Testing Strategy
- **单元测试**: 使用 pytest 框架进行单元测试
- **集成测试**: 测试与 Loki 系统的集成
- **模拟测试**: 使用 mock 对象测试外部依赖
- **覆盖率要求**: 维持 80% 以上的代码覆盖率
- **持续集成**: 自动化测试流水线

### Git Workflow
- **分支策略**: 使用 Git Flow 工作流
- **提交规范**: 遵循 Conventional Commits 格式
- **代码审查**: 所有变更必须经过 PR 审查
- **版本标签**: 使用语义化版本控制 (SemVer)

## Domain Context

### MCP (Model Context Protocol)
- MCP 是一个标准化协议，用于 AI 助手与外部系统的交互
- 支持工具调用、资源访问和提示管理
- 提供类型安全的接口定义

### Grafana Loki
- 水平可扩展的日志聚合系统
- 使用 LogQL 查询语言进行日志检索
- 支持标签索引和时间序列数据
- 与 Prometheus 生态系统集成

### LogQL 查询语言
- 类似于 PromQL 的日志查询语言
- 支持过滤器、聚合和数学运算
- 时间范围查询和实时流式查询

## Important Constraints

### 技术约束
- 必须兼容 MCP 协议规范
- 支持 Loki API v1 和 v2
- 内存使用优化，避免大量日志数据缓存
- 网络超时和重试机制

### 业务约束
- 日志数据安全性和隐私保护
- 查询性能和响应时间要求
- 多租户支持和权限控制

### 合规要求
- 遵循数据保护法规
- 审计日志记录
- 安全认证和授权

## External Dependencies

### 核心依赖
- **Grafana Loki API**: 日志查询和检索接口
- **MCP SDK**: Model Context Protocol 实现库
- **HTTP 客户端**: 用于 API 调用的 HTTP 库

### 可选依赖
- **认证服务**: OAuth2/OIDC 认证提供者
- **监控系统**: Prometheus/Grafana 监控集成
- **配置中心**: 集中化配置管理服务

### 开发依赖
- **测试框架**: pytest, pytest-asyncio
- **代码质量**: black, flake8, mypy
- **文档生成**: sphinx, mkdocs
