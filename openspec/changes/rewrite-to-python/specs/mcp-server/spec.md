## ADDED Requirements

### Requirement: Python MCP Server Implementation
系统 SHALL 提供基于 Python 的 MCP 服务器实现，用于与 Grafana Loki 日志系统集成。

#### Scenario: 服务器启动成功
- **WHEN** 启动 Python MCP 服务器
- **THEN** 服务器应成功初始化并监听 MCP 协议连接
- **AND** 服务器应记录启动日志信息

#### Scenario: 配置加载
- **WHEN** 服务器启动时
- **THEN** 应从环境变量和配置文件加载 Loki 连接配置
- **AND** 应验证必需的配置参数

### Requirement: HTTP-Only Loki Integration
系统 SHALL 仅使用 Loki HTTP API 进行所有日志查询和管理操作，不依赖 logcli 命令行工具。

#### Scenario: HTTP API 查询
- **WHEN** 执行 Loki 查询时
- **THEN** 应使用 Loki HTTP API 端点
- **AND** 应处理 HTTP 响应和错误状态

#### Scenario: 认证处理
- **WHEN** 访问 Loki API 时
- **THEN** 应支持 Basic Auth 和 Bearer Token 认证
- **AND** 应在请求头中正确传递认证信息

### Requirement: Health Check Tool
系统 SHALL 提供健康检查工具，用于验证 Loki 服务器状态和获取当前时间。

#### Scenario: 健康检查成功
- **WHEN** 调用 health_check 工具时
- **THEN** 应返回 Loki 服务器的健康状态
- **AND** 应包含当前服务器时间信息
- **AND** 不需要任何输入参数

#### Scenario: 健康检查失败
- **WHEN** Loki 服务器不可访问时
- **THEN** 应返回明确的错误信息
- **AND** 应包含连接失败的详细原因

### Requirement: Tenant Discovery Tool
系统 SHALL 提供租户发现工具，用于获取所有可用租户的列表。

#### Scenario: 获取租户列表
- **WHEN** 调用 get_tenants 工具时
- **THEN** 应返回所有可用租户的列表
- **AND** 租户列表应基于 tenant 标签的值
- **AND** 不需要任何输入参数

#### Scenario: 无租户情况
- **WHEN** 系统中没有配置租户时
- **THEN** 应返回空的租户列表
- **AND** 应提供相应的说明信息

### Requirement: Tenant-Aware Query Tool
系统 SHALL 提供支持租户参数的日志查询工具，允许用户查询特定租户的日志数据。

#### Scenario: 租户查询成功
- **WHEN** 使用 query_loki 工具并指定 tenant 参数时
- **THEN** 应在指定租户范围内执行 LogQL 查询
- **AND** 应在请求头中设置 X-Scope-OrgID
- **AND** 应返回格式化的查询结果

#### Scenario: 缺少租户参数
- **WHEN** 调用 query_loki 工具但未指定 tenant 参数时
- **THEN** 应返回参数验证错误
- **AND** 应提示需要提供 tenant 参数

#### Scenario: 查询参数验证
- **WHEN** 提供查询参数时
- **THEN** 应验证 query、from、to、limit 等参数的有效性
- **AND** 应支持 ISO 8601 时间格式
- **AND** 应限制最大查询结果数量

### Requirement: Tenant-Aware Label Management
系统 SHALL 提供支持租户参数的标签管理工具，允许用户查询特定租户的标签信息。

#### Scenario: 获取租户标签
- **WHEN** 使用 get_labels 工具并指定 tenant 参数时
- **THEN** 应返回指定租户下所有可用的标签列表
- **AND** 应在请求头中设置 X-Scope-OrgID

#### Scenario: 获取标签值
- **WHEN** 使用 get_label_values 工具并指定 tenant 和 label 参数时
- **THEN** 应返回指定租户下指定标签的所有可能值
- **AND** 应验证 label 参数的有效性

#### Scenario: 租户权限验证
- **WHEN** 访问不存在或无权限的租户时
- **THEN** 应返回适当的权限错误信息
- **AND** 应记录访问尝试的审计日志

### Requirement: Asynchronous Processing
系统 SHALL 使用异步编程模式处理所有 I/O 密集型操作，以提高性能和响应能力。

#### Scenario: 并发请求处理
- **WHEN** 同时接收多个 MCP 工具调用时
- **THEN** 应能够并发处理这些请求
- **AND** 不应阻塞其他请求的处理

#### Scenario: 长时间查询处理
- **WHEN** 执行耗时较长的 Loki 查询时
- **THEN** 应使用异步方式处理
- **AND** 应设置合理的超时时间
- **AND** 应支持查询取消机制

### Requirement: Configuration Management
系统 SHALL 支持基于环境变量和配置文件的设置管理，不再使用 LOKI_TENANT_ID 环境变量。

#### Scenario: 环境变量配置
- **WHEN** 通过环境变量配置 Loki 连接时
- **THEN** 应支持 LOKI_ADDR、LOKI_USERNAME、LOKI_PASSWORD 等变量
- **AND** 不应再支持 LOKI_TENANT_ID 变量

#### Scenario: 配置文件加载
- **WHEN** 存在配置文件时
- **THEN** 应从配置文件加载设置
- **AND** 环境变量应具有更高的优先级

#### Scenario: 配置验证
- **WHEN** 加载配置时
- **THEN** 应验证必需配置项的存在和有效性
- **AND** 应提供清晰的配置错误信息

### Requirement: Error Handling and Logging
系统 SHALL 提供统一的错误处理和日志记录机制，确保问题的可追踪性和调试便利性。

#### Scenario: 错误信息格式化
- **WHEN** 发生错误时
- **THEN** 应返回标准化的 MCP 错误响应
- **AND** 应包含错误代码、消息和详细信息

#### Scenario: 日志记录
- **WHEN** 执行操作时
- **THEN** 应记录适当级别的日志信息
- **AND** 应包含请求上下文和执行时间
- **AND** 敏感信息应被适当脱敏
