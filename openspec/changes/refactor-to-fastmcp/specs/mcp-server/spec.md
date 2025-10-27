# MCP Server 规范增量

## MODIFIED Requirements

### Requirement: MCP 服务器实现
系统 SHALL 使用 FastMCP 框架实现 MCP 服务器，提供标准化的协议处理和自动模式检测功能。

#### Scenario: FastMCP 服务器启动
- **WHEN** 启动 MCP 服务器
- **THEN** 应当使用 FastMCP 框架初始化
- **AND** 自动检测运行环境（stdio vs HTTP）
- **AND** 注册所有可用工具
- **AND** 记录启动日志包含服务器信息

#### Scenario: 自动模式检测
- **WHEN** 从命令行直接运行服务器
- **THEN** FastMCP 应当自动启用 stdio 模式
- **WHEN** 指定端口参数运行服务器
- **THEN** FastMCP 应当自动启用 HTTP/SSE 模式
- **AND** 监听指定的端口

### Requirement: 工具注册和管理
系统 SHALL 使用 FastMCP 的装饰器模式注册和管理所有 MCP 工具，简化工具实现和维护。

#### Scenario: 装饰器工具注册
- **WHEN** 使用 @mcp.tool() 装饰器定义工具
- **THEN** 工具应当自动注册到 FastMCP 实例
- **AND** 自动生成工具定义和输入验证
- **AND** 支持异步工具执行

#### Scenario: 工具参数验证
- **WHEN** 客户端调用工具时提供参数
- **THEN** FastMCP 应当自动验证参数类型和格式
- **AND** 对无效参数返回标准错误响应
- **AND** 记录参数验证错误

### Requirement: HTTP 服务器集成
系统 SHALL 在 HTTP 模式下提供完整的 MCP over HTTP/SSE 协议支持和 Web 调试界面。

#### Scenario: HTTP/SSE 协议处理
- **WHEN** 客户端通过 HTTP/SSE 连接到服务器
- **THEN** FastMCP 应当正确处理 MCP 协议消息
- **AND** 支持工具调用和响应
- **AND** 维护持久的 SSE 连接

#### Scenario: Web 调试界面
- **WHEN** 通过浏览器访问服务器根路径
- **THEN** 应当显示 FastMCP 内置的调试界面
- **AND** 列出所有可用工具
- **AND** 提供工具测试功能

### Requirement: 配置管理简化
系统 SHALL 简化配置管理，移除复杂的服务器模式配置，依赖 FastMCP 的自动检测功能。

#### Scenario: 简化配置加载
- **WHEN** 加载服务器配置
- **THEN** 应当只加载 Loki 连接相关配置
- **AND** 不再需要服务器模式配置
- **AND** 支持 FastMCP 特定的配置选项

#### Scenario: 环境变量支持
- **WHEN** 通过环境变量配置服务器
- **THEN** 应当支持 FASTMCP_* 前缀的环境变量
- **AND** 保持对现有 LOKI_* 环境变量的支持
- **AND** 移除 MCP_SERVER_* 环境变量支持

## ADDED Requirements

### Requirement: 性能监控和指标
系统 SHALL 利用 FastMCP 的内置功能提供性能监控和指标收集。

#### Scenario: 请求指标收集
- **WHEN** 处理 MCP 工具调用请求
- **THEN** 应当收集请求处理时间指标
- **AND** 记录成功和失败的请求数量
- **AND** 提供内存使用情况监控

#### Scenario: 健康检查增强
- **WHEN** 访问健康检查端点
- **THEN** 应当返回 FastMCP 服务器状态
- **AND** 包含工具注册状态
- **AND** 显示当前连接数和请求统计

### Requirement: 错误处理标准化
系统 SHALL 使用 FastMCP 的标准化错误处理机制，提供一致的错误响应格式。

#### Scenario: 工具执行错误处理
- **WHEN** 工具执行过程中发生异常
- **THEN** FastMCP 应当自动捕获异常
- **AND** 转换为标准 MCP 错误响应
- **AND** 记录详细的错误日志

#### Scenario: 协议错误处理
- **WHEN** 接收到无效的 MCP 协议消息
- **THEN** FastMCP 应当返回标准协议错误
- **AND** 不影响其他正常连接
- **AND** 记录协议错误详情

### Requirement: 开发和调试支持
系统 SHALL 提供增强的开发和调试功能，利用 FastMCP 的内置调试工具。

#### Scenario: 调试模式启用
- **WHEN** 在调试模式下启动服务器
- **THEN** 应当启用详细的请求日志记录
- **AND** 提供交互式工具测试界面
- **AND** 显示实时的协议消息流

#### Scenario: 工具文档生成
- **WHEN** 访问工具文档端点
- **THEN** 应当自动生成所有工具的文档
- **AND** 包含参数说明和使用示例
- **AND** 提供 OpenAPI 格式的规范

## REMOVED Requirements

### Requirement: 自定义服务器实现
**原因**: 使用 FastMCP 框架替代自定义实现
**迁移**: 所有功能通过 FastMCP 提供

### Requirement: 手动模式配置
**原因**: FastMCP 提供自动模式检测
**迁移**: 移除 server_mode 配置项，依赖自动检测

### Requirement: 复杂服务器抽象
**原因**: FastMCP 简化了服务器架构
**迁移**: 移除 BaseServer、ServerFactory 等抽象类
