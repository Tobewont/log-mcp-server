## Context

当前的 Loki MCP Server 是一个 Python 应用程序，通过 pip 安装和运行。为了提高部署的一致性、可移植性和简化运维，需要添加 Docker 容器化支持。这将使应用能够在任何支持 Docker 的环境中运行，包括本地开发、测试和生产环境。

## Goals / Non-Goals

### Goals
- 提供标准化的 Docker 镜像构建
- 支持 Docker Compose 本地开发环境
- 优化镜像大小和安全性
- 保持与现有配置方式的兼容性
- 提供完整的部署文档和示例

### Non-Goals
- 不改变现有的应用程序架构
- 不强制要求使用 Docker 部署
- 不包含 Kubernetes 部署配置（可作为后续扩展）
- 不改变现有的配置管理方式

## Decisions

### 1. Docker 镜像策略

#### 基础镜像选择
- **选择**: `python:3.11-slim` 作为基础镜像
- **原因**: 
  - 官方维护，安全更新及时
  - slim 版本减少镜像大小
  - Python 3.11 提供良好的性能和兼容性

#### 多阶段构建
- **选择**: 使用多阶段构建优化镜像大小
- **阶段设计**:
  1. **builder** 阶段：安装构建依赖，构建应用
  2. **runtime** 阶段：仅包含运行时依赖和应用代码

### 2. 安全配置

#### 用户权限
- **选择**: 创建非 root 用户运行应用
- **用户**: `loki-mcp` (UID: 1000, GID: 1000)
- **原因**: 遵循容器安全最佳实践

#### 文件权限
- 应用代码：只读权限
- 配置文件：根据需要设置适当权限
- 日志目录：写权限

### 3. 配置管理

#### 环境变量
- 继续支持现有的环境变量配置
- 通过 Docker 环境变量传递配置
- 支持 `.env` 文件加载

#### 配置文件
- 支持通过卷挂载配置文件
- 默认配置文件路径：`/app/config/`
- 支持多种配置文件格式（YAML）

### 4. 网络和端口

#### 端口暴露
- **选择**: 不暴露 HTTP 端口（MCP 使用 stdio）
- **原因**: MCP 协议基于标准输入输出，不需要网络端口

#### 健康检查
- **选择**: 实现简单的健康检查机制
- **方法**: 检查进程状态和配置有效性
- **间隔**: 30秒检查间隔，3次失败后标记不健康

### 5. Docker Compose 设计

#### 服务组合
```yaml
services:
  loki-mcp-server:
    # MCP 服务器
  loki:
    # 本地 Loki 实例（开发/测试用）
```

#### 网络配置
- 创建专用网络 `loki-mcp-network`
- 服务间通过服务名通信

#### 卷管理
- 配置文件卷：`./config:/app/config:ro`
- 日志卷：`./logs:/app/logs`
- Loki 数据卷：`loki-data:/loki`

### 6. 构建优化

#### 层缓存优化
1. 先复制依赖文件（requirements.txt）
2. 安装依赖（利用 Docker 层缓存）
3. 复制应用代码
4. 设置入口点

#### .dockerignore 配置
- 排除开发文件：`.git`, `__pycache__`, `.pytest_cache`
- 排除文档：`docs/`, `*.md`（除 README.md）
- 排除测试文件：`tests/`

## Alternatives Considered

### 1. 基础镜像选择
**考虑的选项**:
- `python:3.11-alpine`: 更小的镜像
- `python:3.11`: 完整版本
- `ubuntu:22.04` + Python: 自定义安装

**选择 slim 的原因**:
- Alpine 可能有 glibc 兼容性问题
- 完整版本过大
- slim 版本在大小和兼容性间平衡

### 2. 健康检查方式
**考虑的选项**:
- HTTP 健康检查端点
- 进程检查
- 配置文件验证

**选择进程检查的原因**:
- MCP 不需要 HTTP 端点
- 简单有效
- 不增加应用复杂性

### 3. 配置管理方式
**考虑的选项**:
- 仅环境变量
- 仅配置文件
- 混合方式

**选择混合方式的原因**:
- 保持现有兼容性
- 提供灵活性
- 适应不同部署场景

## Risks / Trade-offs

### 风险
1. **镜像大小**: Python 镜像相对较大
   - **缓解**: 使用 slim 基础镜像和多阶段构建

2. **安全漏洞**: 基础镜像可能包含漏洞
   - **缓解**: 定期更新基础镜像，使用官方镜像

3. **配置复杂性**: Docker 配置增加学习成本
   - **缓解**: 提供详细文档和示例

### 权衡
1. **便利性 vs 复杂性**: Docker 增加了部署便利性，但也增加了配置复杂性
2. **标准化 vs 灵活性**: 容器化提供标准化环境，但可能限制某些自定义需求

## Migration Plan

### 阶段 1: 基础容器化 (1-2 天)
1. 创建基础 Dockerfile
2. 实现多阶段构建
3. 配置安全设置

### 阶段 2: Docker Compose 集成 (1 天)
1. 创建 docker-compose.yml
2. 配置服务编排
3. 测试本地环境

### 阶段 3: 优化和文档 (1 天)
1. 优化镜像大小
2. 完善健康检查
3. 更新文档

### 回滚计划
- 保持现有的 pip 安装方式
- Docker 作为可选部署方式
- 不影响现有用户的使用方式

## Implementation Notes

### Dockerfile 结构
```dockerfile
# Builder stage
FROM python:3.11-slim as builder
# Install build dependencies and build app

# Runtime stage  
FROM python:3.11-slim as runtime
# Copy built app and set runtime config
```

### 健康检查实现
```dockerfile
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python -c "import sys; sys.exit(0)" || exit 1
```

### 入口点设计
- 使用 `ENTRYPOINT` 指定主命令
- 使用 `CMD` 提供默认参数
- 支持命令行参数覆盖

## Open Questions

1. **镜像仓库**: 是否需要推送到公共镜像仓库？
2. **版本标签**: 如何管理镜像版本标签策略？
3. **CI/CD 集成**: 是否需要在 CI/CD 中自动构建镜像？
4. **监控集成**: 是否需要集成 Prometheus 监控？
