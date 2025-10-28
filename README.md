# Loki MCP 服务器

基于 [FastMCP](https://github.com/modelcontextprotocol/mcp) 框架的现代化 Model Context Protocol 服务器，用于通过 HTTP API 查询 Grafana Loki 日志。该服务器使 AI 助手能够通过标准化的 MCP 工具直接访问、查询和分析 Loki 中的日志数据。

## 🚀 FastMCP 架构优势

- **自动模式检测** - 无需手动配置，自动检测 stdio 或 HTTP/SSE 模式
- **简化的工具管理** - 使用装饰器模式，代码更简洁
- **内置 Web UI** - 提供调试和测试界面
- **标准化错误处理** - 一致的 MCP 错误响应格式
- **性能优化** - 更高效的请求处理和资源利用

## 功能特性

- 🔍 **查询 Loki 日志** - 完整的 LogQL 语法支持
- 🏷️ **浏览标签和值** - 用于数据探索
- 🏢 **多租户支持** - 配置时指定多个租户，自动查询所有租户
- ❤️ **健康监控** - Loki 服务器状态监控
- 🔐 **认证支持** - 基本认证和 Bearer Token
- ⚡ **异步 HTTP 客户端** - 优化性能
- 🛡️ **全面错误处理** - 完善的错误处理和日志记录

## 快速开始

### 安装

#### 选项 1: Docker（推荐）

```bash
# 克隆仓库
git clone https://github.com/your-org/loki-mcp-server.git
cd loki-mcp-server

# 使用 Docker Compose 启动（包含 Loki 和 Grafana）
# FastMCP 自动检测模式，默认启用 HTTP 模式在端口 8000
docker-compose up -d

# 或者只构建和运行 MCP 服务器
docker build -t loki-mcp-server .
docker run -e LOKI_ADDR=http://your-loki-server:3100 -e FASTMCP_HOST=0.0.0.0 -e FASTMCP_PORT=8000 -p 8000:8000 loki-mcp-server
```

#### 选项 2: Python 包

```bash
# 从 PyPI 安装（发布后）
pip install loki-mcp-server

# 或从源码安装
git clone https://github.com/your-org/loki-mcp-server.git
cd loki-mcp-server
pip install -e .
```

### 配置

#### FastMCP 配置

服务器使用 FastMCP 框架，自动检测运行模式（stdio 或 HTTP/SSE）。可选配置：

```bash
# FastMCP 配置（可选）
export FASTMCP_DEBUG="false"     # 调试模式，默认: false
export FASTMCP_HOST="0.0.0.0"    # HTTP 模式监听地址，默认: 0.0.0.0
export FASTMCP_PORT="8000"       # HTTP 模式端口，默认: 8000
```

#### Loki 配置

使用环境变量配置 Loki 连接：

```bash
export LOKI_ADDR="https://your-loki-server.com"
export LOKI_USERNAME="your-username"  # 可选
export LOKI_PASSWORD="your-password"  # 可选
# 或者
export LOKI_BEARER_TOKEN="your-token"  # 可选
```

或者创建 `loki-config.yaml` 文件：

```yaml
# FastMCP 配置（可选）
fastmcp_debug: false
fastmcp_host: "0.0.0.0"
fastmcp_port: 8000

# Loki 配置
addr: "https://your-loki-server.com"
tenants: "fake"  # 或 "tenant1|tenant2|tenant3"
username: "your-username"
password: "your-password"
# 或者
bearer_token: "your-token"
```

### 与 Claude Desktop 配合使用

#### Stdio 模式（本地）
添加到 Claude Desktop 配置：

```json
{
  "mcpServers": {
    "loki": {
      "command": "loki-mcp-server",
      "env": {
        "LOKI_ADDR": "https://your-loki-server.com",
        "LOKI_TENANTS": "fake"
      }
    }
  }
}
```

#### HTTP/SSE 模式（网络）
首先启动服务器：

```bash
# 启动服务器（FastMCP 自动检测 HTTP 模式）
export FASTMCP_HOST=0.0.0.0
export FASTMCP_PORT=8000
export LOKI_ADDR=https://your-loki-server.com
loki-mcp-server
```

然后配置 Claude Desktop：

```json
{
  "mcpServers": {
    "loki": {
      "transport": "sse",
      "url": "http://localhost:8000/sse"
    }
  }
}
```

## Docker 部署

### 🐳 Docker Compose（推荐用于开发）

使用 Docker Compose 是最简单的开始方式，它包含了 Loki、Grafana 和 MCP 服务器：

```bash
# 克隆仓库
git clone https://github.com/your-org/loki-mcp-server.git
cd loki-mcp-server

# 复制环境变量文件并自定义
cp env.example .env
# 编辑 .env 文件配置

# 启动所有服务
docker-compose up -d

# 查看日志
docker-compose logs -f loki-mcp-server

# 停止服务
docker-compose down
```

这将启动：
- **Loki MCP 服务器** - 主要的 MCP 服务器，FastMCP 自动模式检测 (http://localhost:8000)
- **Loki** - 日志聚合系统 (http://localhost:3100)
- **Grafana** - 日志可视化 (http://localhost:3000, admin/admin)

### 🏗️ Docker 构建

仅构建和运行 MCP 服务器容器：

```bash
# 构建镜像
docker build -t loki-mcp-server:latest .

# 使用环境变量运行（HTTP 模式）
docker run -d \
  --name loki-mcp-server \
  -p 8000:8000 \
  -e FASTMCP_HOST=0.0.0.0 \
  -e FASTMCP_PORT=8000 \
  -e LOKI_ADDR=http://your-loki-server:3100 \
  -e LOKI_TENANTS=fake \
  -e LOKI_USERNAME=your-username \
  -e LOKI_PASSWORD=your-password \
  loki-mcp-server:latest

# 使用配置文件运行
docker run -d \
  --name loki-mcp-server \
  -v $(pwd)/config:/app/config:ro \
  -v $(pwd)/logs:/app/logs \
  loki-mcp-server:latest

# 查看日志
docker logs -f loki-mcp-server
```

### 🔧 Docker 配置

#### 环境变量
所有配置都可以通过环境变量传递：

```bash
# FastMCP 配置（可选 - 自动检测模式）
FASTMCP_DEBUG=false      # 调试模式
FASTMCP_HOST=0.0.0.0     # HTTP 模式监听地址
FASTMCP_PORT=8000        # HTTP 模式端口

# 必需配置
LOKI_ADDR=http://loki:3100

# 认证（选择一种）
LOKI_USERNAME=your-username
LOKI_PASSWORD=your-password
# 或者
LOKI_BEARER_TOKEN=your-token

# 多租户配置
LOKI_TENANTS=fake  # 或 tenant1|tenant2|tenant3

# 可选设置
LOKI_TLS_SKIP_VERIFY=false
LOKI_CONNECT_TIMEOUT=10.0
LOKI_READ_TIMEOUT=30.0
LOKI_DEFAULT_LIMIT=1000
LOKI_MAX_LIMIT=5000
```

#### 卷挂载
- **配置文件**: `-v /path/to/config:/app/config:ro`
- **日志**: `-v /path/to/logs:/app/logs`
- **环境变量文件**: `-v /path/to/.env:/app/.env:ro`

#### 健康检查
容器包含内置健康检查：

```bash
# 检查容器健康状态
docker inspect --format='{{.State.Health.Status}}' loki-mcp-server

# 手动健康检查
docker exec loki-mcp-server python -c "from loki_mcp_server.config import LokiConfig; print('OK' if LokiConfig().addr else 'FAIL')"
```

### 🚀 生产环境部署

生产环境配置：

```yaml
# docker-compose.prod.yml
version: '3.8'
services:
  loki-mcp-server:
    image: loki-mcp-server:latest
    restart: unless-stopped
    environment:
      - LOKI_ADDR=https://your-production-loki.com
      - LOKI_BEARER_TOKEN_FILE=/run/secrets/loki_token
    secrets:
      - loki_token
    volumes:
      - ./config:/app/config:ro
      - logs:/app/logs
    deploy:
      resources:
        limits:
          memory: 512M
          cpus: '0.5'
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"

secrets:
  loki_token:
    file: ./secrets/loki_token.txt

volumes:
  logs:
```

### 🐛 Docker 故障排除

#### 常见问题

**容器无法启动：**
```bash
# 检查日志
docker logs loki-mcp-server

# 检查配置
docker exec loki-mcp-server env | grep LOKI_
```

**连接问题：**
```bash
# 从容器测试 Loki 连接
docker exec loki-mcp-server curl -f http://loki:3100/ready

# 检查网络连接
docker network ls
docker network inspect loki-mcp-network
```

**权限问题：**
```bash
# 检查文件权限
docker exec loki-mcp-server ls -la /app/config /app/logs

# 修复权限
sudo chown -R 1000:1000 ./config ./logs
```

#### 调试模式

在调试模式下运行容器：

```bash
# 交互式 shell
docker run -it --rm \
  -e LOKI_ADDR=http://loki:3100 \
  loki-mcp-server:latest \
  /bin/bash

# Python 调试
docker run -it --rm \
  -e LOKI_ADDR=http://loki:3100 \
  loki-mcp-server:latest \
  python -c "from loki_mcp_server.config import LokiConfig; print(LokiConfig().get_safe_config())"
```

## 可用工具

### 🔍 `query_loki`
使用 LogQL 语法查询配置的所有租户的日志。

**参数：**
- `query`（必需）：LogQL 查询字符串（例如：`{job="app"} |= "error"`）
- `start`（可选）：ISO 8601 格式的开始时间（默认：当前时间前1小时）
- `end`（可选）：ISO 8601 格式的结束时间（默认：当前时间）
- `limit`（可选）：最大条目数（默认：100，最大：5000）
- `direction`（可选）：查询方向（默认：`backward`）

**示例：**
```
查询: {job="nginx"} |= "error"
# 不指定时间范围时，自动查询最近1小时的日志
# 默认返回100条日志
```

**注意：** 
- 工具会自动查询所有配置的租户（通过 `LOKI_TENANTS` 环境变量配置）
- 如果不指定时间范围，默认查询最近1小时的日志
- 默认限制返回100条日志以提高性能

### 🏷️ `get_labels`
获取所有配置租户的可用标签。

**参数：** 无

### 🔖 `get_label_values`
获取特定标签在所有配置租户中的值。

**参数：**
- `label`（必需）：标签名称

### ❤️ `health_check`
检查 Loki 服务器健康状态并获取当前时间。

**参数：** 无

## 配置选项

| 环境变量 | 描述 | 默认值 |
|---------|------|--------|
| **FastMCP 配置** | | |
| `FASTMCP_DEBUG` | 启用调试模式 | `false` |
| `FASTMCP_HOST` | HTTP 模式监听地址 | `0.0.0.0` |
| `FASTMCP_PORT` | HTTP 模式端口 | `8000` |
| **Loki 配置** | | |
| `LOKI_ADDR` | Loki 服务器地址 | `http://localhost:3100` |
| `LOKI_TENANTS` | 租户列表（用\|分隔） | `fake` |
| `LOKI_USERNAME` | 基本认证用户名 | 无 |
| `LOKI_PASSWORD` | 基本认证密码 | 无 |
| `LOKI_BEARER_TOKEN` | Bearer token 认证 | 无 |
| `LOKI_BEARER_TOKEN_FILE` | Bearer token 文件路径 | 无 |
| `LOKI_TLS_SKIP_VERIFY` | 跳过 TLS 验证 | `false` |
| `LOKI_CONNECT_TIMEOUT` | 连接超时（秒） | `10.0` |
| `LOKI_READ_TIMEOUT` | 读取超时（秒） | `30.0` |
| `LOKI_DEFAULT_LIMIT` | 默认查询结果限制 | `100` |
| `LOKI_MAX_LIMIT` | 最大查询结果限制 | `5000` |

### 多租户配置

配置多个租户以同时查询多个 Loki 租户：

```bash
# 单租户（默认）
export LOKI_TENANTS="fake"

# 多租户
export LOKI_TENANTS="tenant1|tenant2|tenant3"

# 配合认证使用
export LOKI_TENANTS="company-logs|app-logs|system-logs"
export LOKI_USERNAME="your-username"
export LOKI_PASSWORD="your-password"
```

**注意**：
- 工具会自动查询所有配置的租户
- 每个租户通过 `X-Scope-OrgID` 头部指定
- 如果某个租户查询失败，会继续查询其他租户
- 结果会标明来自哪个租户

## 开发

### 设置开发环境

```bash
# 克隆仓库
git clone https://github.com/your-org/loki-mcp-server.git
cd loki-mcp-server

# 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 安装依赖
pip install -e .[dev]
```

### 运行测试

```bash
# 运行所有测试
pytest

# 运行覆盖率测试
pytest --cov=loki_mcp_server

# 运行特定测试文件
pytest tests/test_config.py
```

### 代码质量

```bash
# 格式化代码
black src/ tests/

# 代码检查
flake8 src/ tests/

# 类型检查
mypy src/
```

### 运行服务器

```bash
# 直接运行
python -m loki_mcp_server.main

# 或使用安装的命令
loki-mcp-server
```

## 架构

服务器构建基于：

- **Python 3.8+** 使用现代 async/await 模式
- **httpx** 异步 HTTP 客户端功能
- **pydantic** 配置管理和验证
- **structlog** 结构化日志
- **MCP SDK** Model Context Protocol 实现

### 项目结构

```
src/loki_mcp_server/
├── __init__.py
├── main.py              # MCP 服务器入口点
├── config.py            # 配置管理
├── client/
│   ├── __init__.py
│   ├── loki_client.py   # Loki HTTP API 客户端
│   ├── http_client.py   # 通用 HTTP 客户端
│   └── auth.py          # 认证处理
├── tools/               # MCP 工具实现
│   ├── __init__.py
│   └── loki_tools.py    # FastMCP 工具定义
└── utils/
    ├── __init__.py
    ├── errors.py        # 错误处理
    └── logging.py       # 日志设置
```

## 故障排除

### 常见问题

**连接错误**
- 验证 `LOKI_ADDR` 正确且可访问
- 检查认证凭据
- 确保 Loki 服务器运行正常

**认证错误**
- 验证用户名/密码或 bearer token
- 检查多租户模式是否需要特定头部

**查询错误**
- 验证 LogQL 语法
- 确保租户存在且可访问
- 检查时间范围参数

**工具未找到错误**
- 验证 MCP 服务器正确注册
- 检查工具名称完全匹配
- 确保提供所有必需参数

### 调试模式

启用调试日志：

```bash
export LOG_LEVEL=DEBUG
loki-mcp-server
```

