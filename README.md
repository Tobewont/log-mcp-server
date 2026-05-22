# Log MCP Server

![python](https://img.shields.io/badge/python-3.12%2B-blue) ![license](https://img.shields.io/badge/license-MIT-green)

中文 | [English](README_EN.md)

基于 [FastMCP](https://github.com/modelcontextprotocol/python-sdk) 的 Model Context Protocol 的 Log MCP Server，提供统一的日志查询工具。当前内置 **Grafana Loki** 后端（含**多 Loki 扇出查询**），并提供可扩展的 `LogBackend` 接口以便接入其他日志系统（Elasticsearch、CloudWatch、ClickHouse 等）。

## 主要特性

- **后端可插拔**：通过 `LOG_BACKEND` 选择，目前实现 `loki`
- **多 Loki 扇出查询**：`LOKI_ADDR=url1|url2|url3` 即可像 Thanos 之于 Prometheus 一样，把多个 Loki 实例聚合查询
- **多租户并发**：每个 (cluster, tenant) 独立超时，部分失败也会显式上报
- **传输模式齐全**：`stdio` / `sse` / `streamable-http`（**推荐**最新模式）
- **简洁的工具**：4 个 backend-agnostic 工具，AI 直接可用
- **时区一致**：内部全部 tz-aware UTC，输出统一按配置时区（默认 `Asia/Shanghai`）
- **单连接池长生命周期**，避免每次调用重建 HTTP 连接

## 快速开始

### Docker Compose（推荐用于本地开发）

会同时启动 log-mcp-server、Loki 和 Grafana：

```bash
git clone https://github.com/your-org/log-mcp-server.git
cd log-mcp-server

cp env.example .env       # 按需修改
docker compose up -d

docker compose logs -f log-mcp-server
```

启动后：

- log-mcp-server：streamable-http 模式 [http://localhost:8000/mcp](http://localhost:8000/mcp)
- Loki：[http://localhost:3100](http://localhost:3100)
- Grafana：[http://localhost:3000](http://localhost:3000)（admin / admin）

### 从源码安装

```bash
git clone https://github.com/your-org/log-mcp-server.git
cd log-mcp-server
uv sync          # 安装所有依赖（含 dev）
uv run log-mcp-server
```

> 如果没有安装 uv，参见 [uv 安装指南](https://docs.astral.sh/uv/getting-started/installation/)。

## 可用工具

> 设计原则：**非必要不增加**。下列 5 个工具足以覆盖 AI 助手的日志查询下载、标签发现和健康检查全部主流场景。

### 推荐工作流（多租户场景）

不健康的 Loki cluster 会被**自动跳过**（启动时 + 每 5 分钟后台探测缓存）。多租户时 AI 自动执行"发现→查询"两步，租户对人类用户**完全透明**：

1. `get_labels()` — 发现各租户下有哪些标签名
2. `get_label_values(label="<用户关心的标签>")` — 查看各租户该标签的具体值，定位目标租户
3. `query_logs(tenant="<目标租户>", query='{<label>="<value>"}|="keyword"')` — 精准查询

> 标签完全由用户的日志数据决定，可以是 `namespace`、`app`、`job`、`env` 等任意自定义标签。AI 会根据用户的查询意图选择合适的标签进行发现和过滤。

> **指定 Loki 实例**：当用户明确指出某个 Loki 实例时（例如"查 `loki.example.com` 上的日志"），所有 3 个查询工具都接受可选的 `instance` 参数，传入对应的 cluster id 即可绕过多实例扇出，**只查该实例**。Cluster id 可在 `health_check` 输出中查看。

### 🔍 `query_logs`

按时间范围查询日志。指定 `tenant` 时只查该租户；省略则并发查询**所有客户端可见的租户**（即 `X-Allowed-Tenants` / `LOKI_CLIENT_TENANTS` 与 `LOKI_TENANTS` 的交集，详见下文「客户端租户范围」）。

| 参数 | 必填 | 说明 |
|---|---|---|
| `query` | ✅ | LogQL 日志选择器，例如 `{job="nginx"} \|= "error"`。**不支持** 指标表达式（`rate()` 等） |
| `start` | ❌ | RFC3339 / ISO 8601 时间，例如 `2025-01-01T00:00:00Z` 或 `2025-01-01T00:00:00+08:00`。默认 `end - LOG_DEFAULT_TIME_RANGE_MINUTES` |
| `end` | ❌ | 同上格式。默认当前时间 |
| `limit` | ❌ | **每个 tenant 返回条数上限**。多 Loki 扇出时，fanout 内部已先在每 tenant 内跨 cluster 合并、按时间排序、再截断到此上限。默认 `LOG_DEFAULT_LIMIT`，不得超过 `LOG_MAX_LIMIT` |
| `direction` | ❌ | `backward`（默认，最新在前）或 `forward` |
| `tenant` | ❌ | 指定租户 ID。**推荐在多租户场景下使用**，避免不必要的全租户扇出 |
| `instance` | ❌ | 指定 Loki 实例（cluster id，例如 `loki-bj:3100` 或 `loki.example.com`，从 `health_check` 输出可见）。指定后**只查该实例**，绕过多 Loki 扇出 |

返回 Markdown 报告：每条日志带 `Tenant` 和 `Cluster`（多 Loki 时）；文末 `Errors` 区列出每个失败的 tenant 和 cluster 错误（多 Loki 部分失败时）。

### 🏷️ `get_labels`

列出标签名集合（去重）。指定 `tenant` 时只查该租户。

| 参数 | 必填 | 说明 |
|---|---|---|
| `start` | ❌ | 可选时间范围起点，缩小查询面以避免大集群慢查询 |
| `end` | ❌ | 可选时间范围终点 |
| `tenant` | ❌ | 指定租户 ID，省略则查询**所有客户端可见的租户** |
| `instance` | ❌ | 指定 Loki 实例（cluster id），多 Loki 时只查该实例 |

### 🔖 `get_label_values`

列出某个标签的所有值（去重）。指定 `tenant` 时只查该租户。

| 参数 | 必填 | 说明 |
|---|---|---|
| `label` | ✅ | 标签名 |
| `start` | ❌ | 可选时间范围起点 |
| `end` | ❌ | 可选时间范围终点 |
| `tenant` | ❌ | 指定租户 ID，省略则查询**所有客户端可见的租户** |
| `instance` | ❌ | 指定 Loki 实例（cluster id），多 Loki 时只查该实例 |

### ❤️ `health_check`

检查后端健康状态。多 Loki 时按 cluster 列出每个实例的状态（healthy / unhealthy）和 Loki 版本。同时输出当前会话生效的 `Allowed Tenants` 与过滤来源（见下节）。无参数。

### 📥 `download_logs`

把查询结果**离线写到一个文件**，让用户能直接下载到本地分析（grep / jq / Excel / 入库），而**不**走 LLM 上下文。若查询成功但命中 `0` 条日志，不会生成空文件或下载链接，只返回空结果提示。

| 参数 | 必填 | 说明 |
|---|---|---|
| `query` | ✅ | LogQL，与 `query_logs` 一致 |
| `start` / `end` | ❌ | 强烈建议明确指定，避免一次拉到 GB 级数据 |
| `limit` | ❌ | **每个 tenant 上限**，默认 `LOG_MAX_LIMIT`；不得超过它 |
| `direction` | ❌ | `backward` / `forward` |
| `tenant` / `instance` | ❌ | 与 `query_logs` 同；同样要求客户端先声明 `X-Allowed-Tenants` / `LOKI_CLIENT_TENANTS` |
| `fmt` | ❌ | `jsonl`（默认）/ `csv` / `txt` |

**两种部署模式下的"下载到本地"是不同的实现路径**：

| 部署 | 工具返回 | 用户怎么拿到文件 |
|---|---|---|
| `stdio`（server 由 MCP 客户端在用户本机启动） | 服务器端的**绝对路径**（= 本机路径） | `cat` / `open` / 编辑器直接打开 |
| `streamable-http` / `sse`（server 在远端，比如 K8s） | **下载 URL**：`https://logs-mcp.example.com/mcp/download/<token>` | 浏览器点开或 `curl -O <URL>` |

下载路由挂在 `<MCP 路径前缀>/download/<token>`（streamable-http 默认 `/mcp/download/<token>`，sse 默认 `/sse/download/<token>`），与 MCP 自身的端点**同前缀**。这样反向代理 / Ingress 只要已经把 `/mcp` 转发到后端，下载链接就**自动可用**，无需新增任何转发规则。**仅靠不可猜测的 token 鉴权**（`secrets.token_urlsafe(32)`，约 256 bits 熵）。文件 TTL 默认 1 小时（`LOG_DOWNLOAD_TTL_SECONDS`）；链接成功下载一次后立即失效并删除文件，未下载则到期清理。

**配置项**：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `LOG_DOWNLOAD_DIR` | `./logs/downloads` | 服务器端文件落地目录；stdio 模式下就是用户本机路径 |
| `LOG_DOWNLOAD_TTL_SECONDS` | `3600` | HTTP 模式下 token + 文件存活秒数 |
| `LOG_DOWNLOAD_BASE_URL` | _未设_ | 反代下用于渲染 URL 的 base，例如 `https://logs-mcp.example.com`。未设时从请求 Host header 推断 |

**输出格式说明**：

- `jsonl`：每行一个 JSON `{time, tenant, cluster, labels, line}`，最忠实于 Loki 数据，适合 `jq` / `grep` / 入仓
- `csv`：列 `time, tenant, cluster, labels, line`（labels 是 JSON 字符串，逗号不会破列）
- `txt`：人读 `[time] tenant/cluster {k=v, ...} line`

> **注意**：下载的 `limit` 是**每个租户**的上限，受 `LOG_MAX_LIMIT` 限制（Loki 服务端 `max_entries_limit_per_query` 也要配套）。多租户下载的总条数可能超过单个 `limit`。要下载更多，请缩小时间窗多次调用。

## 客户端租户范围（必填）

服务端 `LOKI_TENANTS` 决定整个进程**可访问**的租户全集，每个 MCP 客户端还**必须**显式声明一个子集 —— 否则日志查询/下载工具（`query_logs` / `get_labels` / `get_label_values` / `download_logs`）会**直接拒绝**，错误中明确告诉你怎么配。`health_check` 是诊断工具，不受此限制，仍可用来观察当前会话的 Allowed Tenants 与 Filter Source。

> 这是**纵深防御**，不是鉴权 —— 能改 MCP 客户端配置的人也能改 header / env，请勿用作安全边界。它的目的是迫使每个客户端 / 用户**显式声明意图**，避免漫无目的的全租户扇出与误查。

| 传输 | 客户端如何指定子集 |
|---|---|
| `streamable-http` / `sse` | 每次 HTTP 请求带 `X-Allowed-Tenants` 头部，**逗号分隔** |
| `stdio` | MCP 客户端配置里 `env.LOKI_CLIENT_TENANTS`（逗号分隔） |

优先级：`X-Allowed-Tenants` 头 > `LOKI_CLIENT_TENANTS` env。子集必须是 `LOKI_TENANTS` 的子集，否则启动报错（env 模式）或本次请求被拒（header 模式）。

错误处理：
- **未声明** → `RuntimeError: No tenant scope is configured for this MCP client. ...`（指引用户配置 header / env）
- **越权 tenant=** → `RuntimeError: Forbidden tenant ...`
- **子集与服务端交集为空** → `RuntimeError: No tenants are accessible. ...`

> **建议：只配置当前业务实际需要的租户。** 调用 `query_logs` / `get_labels` / `get_label_values` 而不传 `tenant=` 时，服务端会**并行扇出**到所有"客户端可见"的租户。租户越多：
> - **越慢** —— 扇出的并发数越大，整体响应时间被最慢的那个拖累；任何一个租户超时 / 503 都会拖慢整体。
> - **越不精准** —— 多个租户标签命名空间常有重叠（例如都有 `app=foo` 但语义不同），结果会把不相关的日志也混进来，AI 容易看错。
> - **越嘈杂** —— 部分失败的 `Errors` 区会变长，AI 必须解读，token 成本上升。
>
> 推荐的做法是按业务领域分组，每个 MCP 客户端只列**这次会话**实际需要的 1–3 个租户；不要一股脑把所有租户都填进 `X-Allowed-Tenants` / `LOKI_CLIENT_TENANTS`。

#### Stdio 客户端（mcp.json）示例

```json
{
  "mcpServers": {
    "logs": {
      "command": "log-mcp-server",
      "args": ["stdio"],
      "env": {
        "LOKI_ADDR": "https://loki.example.com",
        "LOKI_TENANTS": "team-a|team-b|team-c",
        "LOKI_CLIENT_TENANTS": "team-a,team-b"
      }
    }
  }
}
```

#### HTTP 客户端（streamable-http）示例

```json
{
  "mcpServers": {
    "logs": {
      "url": "http://log-mcp.example.com:8000/mcp",
      "headers": { "X-Allowed-Tenants": "team-a,team-b" }
    }
  }
}
```

`curl` 直连：

```bash
curl -X POST http://localhost:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'X-Allowed-Tenants: team-a,team-b' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## 多 Loki（Thanos 风格扇出）

当你拥有多个 Loki 实例（多机房、多业务、新旧迁移并存）时，`LOKI_ADDR` 用 `|` 分隔多个地址即可：

```bash
LOKI_ADDR="http://loki-bj:3100|http://loki-sh:3100|http://loki-sg:3100"
LOKI_TENANTS="team-a|team-b"
```

行为：

- **健康缓存**：启动时探测所有 cluster（`GET /loki/api/v1/status/buildinfo`），之后每 `HEALTH_CHECK_INTERVAL` 秒（默认 300）后台刷新。不健康 cluster 自动跳过，避免超时拖累
- 数据查询只发往**健康 cluster**；`health_check` 工具仍探测所有 cluster（用于诊断）
- 每个 (cluster, tenant) 子查询独立超时（30 秒）
- 失败隔离：某个 Loki 整个挂掉，其它结果照常返回
- 全局合并：日志按时间排序 → 按 `limit` 截断
- 结果中每条日志带 `Cluster` 标识（默认是 `host:port`）
- 认证、租户、超时等参数对所有 Loki **共享**

## 传输模式（Transport）

服务器支持 3 种 MCP 传输模式：

| Transport | 端点 | 适用场景 |
|---|---|---|
| `stdio` | — | Claude Desktop 等本地 MCP 客户端 |
| `sse` | `/sse` | 旧版 HTTP/SSE 模式（兼容性保留） |
| `streamable-http` | `/mcp` | **当前推荐**的 HTTP 模式 |

### 选择优先级

1. **CLI 参数** 最优先：`log-mcp-server stdio | sse | streamable-http`
2. **环境变量 / 配置** `MCP_TRANSPORT`（默认 `stdio`）

### Claude Desktop 客户端配置

#### Stdio 模式（本地）

```json
{
  "mcpServers": {
    "logs": {
      "command": "log-mcp-server",
      "args": ["stdio"],
      "env": {
        "LOG_BACKEND": "loki",
        "LOKI_ADDR": "https://loki.example.com",
        "LOKI_TENANTS": "fake"
      }
    }
  }
}
```

#### HTTP 模式（Streamable-HTTP / SSE）

先启动服务器：

```bash
export MCP_TRANSPORT=streamable-http
export MCP_HOST=0.0.0.0
export MCP_PORT=8000
export LOKI_ADDR=https://loki.example.com
log-mcp-server
```

客户端只需指定 `url`，MCP 客户端从路径即可推断协议类型：

```json
{
  "mcpServers": {
    "logs": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

> `/mcp` → streamable-http（推荐），`/sse` → SSE（旧版兼容）

## Docker 构建与启动

### 构建镜像

```bash
# 本地构建（用项目根目录的 Dockerfile）
docker build -t log-mcp-server:1.0.0 .

# 推送到你的镜像仓库
docker tag log-mcp-server:1.0.0 your-registry/log-mcp-server:1.0.0
docker push your-registry/log-mcp-server:1.0.0
```

### 直接运行容器

```bash
# 单 Loki + streamable-http
docker run -d --name log-mcp-server \
  -p 8000:8000 \
  -e MCP_TRANSPORT=streamable-http \
  -e MCP_HOST=0.0.0.0 \
  -e MCP_PORT=8000 \
  -e LOG_BACKEND=loki \
  -e LOKI_ADDR=http://your-loki:3100 \
  -e LOKI_TENANTS=fake \
  -e LOG_TIMEZONE=Asia/Shanghai \
  log-mcp-server:1.0.0

# 多 Loki 扇出
docker run -d --name log-mcp-server \
  -p 8000:8000 \
  -e MCP_TRANSPORT=streamable-http \
  -e MCP_HOST=0.0.0.0 -e MCP_PORT=8000 \
  -e LOKI_ADDR='http://loki-bj:3100|http://loki-sh:3100' \
  -e LOKI_TENANTS='team-a|team-b' \
  log-mcp-server:1.0.0

# stdio 模式（一般用 docker exec / 不暴露端口）
docker run --rm -i \
  -e LOKI_ADDR=http://your-loki:3100 \
  log-mcp-server:1.0.0 stdio

# 看日志
docker logs -f log-mcp-server

# 停止 / 删除
docker stop log-mcp-server && docker rm log-mcp-server
```

### Docker Compose 一键启动（含 Loki + Grafana）

```bash
cp env.example .env       # 按需修改
docker compose up -d
docker compose logs -f log-mcp-server

# 关闭
docker compose down
```

## Kubernetes 部署

`k8s/` 目录已经包含完整的部署清单（namespace / configmap / secret / deployment / service / ingress / hpa）。

```bash
# 1. 准备镜像
docker build -t your-registry/log-mcp-server:1.0.0 .
docker push your-registry/log-mcp-server:1.0.0

# 2. 编辑 k8s/configmap.yaml，设置 LOKI_ADDR / LOKI_TENANTS / 时区等
# 3. 创建 Secret（推荐命令行而非 commit secret.yaml）
kubectl create secret generic log-mcp-server-secrets \
  --namespace=log-mcp \
  --from-literal=LOKI_USERNAME=user \
  --from-literal=LOKI_PASSWORD='your-password'

# 4. 部署（kubectl 直接 apply）
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/deployment.yaml

# 或使用 Kustomize
kubectl apply -k k8s/

# 5. 验证
kubectl -n log-mcp get pods
kubectl -n log-mcp logs -l app.kubernetes.io/name=log-mcp-server -f

# 6. 端口转发本地测试
kubectl -n log-mcp port-forward svc/log-mcp-server 8000:8000
# 客户端连 http://localhost:8000/mcp
```

详细说明（含 Ingress 长连接配置、HPA、常见问题）见 [`k8s/README.md`](k8s/README.md)。

## 配置

支持的配置来源（**优先级从高到低**）：

1. 显式 `LogConfig(...)` 构造参数
2. 环境变量
3. `.env` 文件
4. YAML 配置文件（`LOG_CONFIG_PATH` / `LOKI_CONFIG_PATH` / `./loki-config.yaml` / `./.loki-config.yaml` / `~/.loki-config.yaml`）
5. 内置默认值

### 环境变量

| 变量 | 说明 | 默认值 |
|---|---|---|
| **传输模式** | | |
| `MCP_TRANSPORT` | `stdio` / `sse` / `streamable-http` | `stdio` |
| `MCP_HOST` | HTTP 模式监听地址 | `127.0.0.1` |
| `MCP_PORT` | HTTP 模式端口 | `8000` |
| `LOG_LEVEL` | 服务日志级别（`DEBUG` 时同时开启 FastMCP debug） | `INFO` |
| **后端选择** | | |
| `LOG_BACKEND` | 当前激活的后端 | `loki` |
| **Loki 后端** | | |
| `LOKI_ADDR` | Loki 服务器地址（`\|` 分隔多个） | `http://localhost:3100` |
| `LOKI_TENANTS` | 租户列表（`\|` 分隔） | `fake` |
| `LOKI_CLIENT_TENANTS` | 客户端租户子集（逗号分隔），仅 stdio；HTTP 模式用 `X-Allowed-Tenants` 头 | — |
| `LOKI_USERNAME` | Basic 认证用户名 | — |
| `LOKI_PASSWORD` | Basic 认证密码 | — |
| `LOKI_BEARER_TOKEN` | Bearer token | — |
| `LOKI_BEARER_TOKEN_FILE` | Bearer token 文件路径 | — |
| `LOKI_CA_FILE` / `LOKI_CERT_FILE` / `LOKI_KEY_FILE` | TLS 证书 | — |
| `LOKI_TLS_SKIP_VERIFY` | 跳过 TLS 校验 | `false` |
| `LOKI_CONNECT_TIMEOUT` | 连接超时（秒） | `10.0` |
| `LOKI_READ_TIMEOUT` | 读取超时（秒） | `15.0` |
| `LOKI_WRITE_TIMEOUT` | 写入超时（秒） | `10.0` |
| `LOKI_POOL_TIMEOUT` | 连接池超时（秒） | `10.0` |
| **健康缓存（多 cluster）** | | |
| `HEALTH_CHECK_INTERVAL` | 后台健康刷新间隔（秒） | `300.0` |
| `HEALTH_CHECK_TIMEOUT` | 单 cluster 健康探测超时（秒） | `5.0` |
| **通用查询设置** | | |
| `LOG_DEFAULT_LIMIT` | 默认结果条数 | `100` |
| `LOG_MAX_LIMIT` | 单租户 `limit` 最大值 | `5000` |
| `LOG_DEFAULT_TIME_RANGE_MINUTES` | 默认时间范围（分钟） | `30` |
| `LOG_TIMEZONE` | 显示时区 | `Asia/Shanghai` |

### YAML 配置示例

```yaml
# MCP 传输
mcp_transport: "streamable-http"
mcp_host: "0.0.0.0"
mcp_port: 8000
log_level: "INFO"

# 后端
backend: "loki"

# 单 Loki
addr: "https://loki.example.com"

# 或多 Loki 扇出
# addr: "https://loki-bj.example.com|https://loki-sh.example.com"

tenants: "team-a|team-b"
username: "your-username"
password: "your-password"

# 健康缓存（多 cluster 时生效）
health_check_interval: 300   # 后台刷新间隔（秒）
health_check_timeout: 5      # 单 cluster 探测超时（秒）

default_limit: 100
max_limit: 5000
default_time_range_minutes: 30
timezone: "Asia/Shanghai"
```

## 架构

```
src/log_mcp_server/
├── main.py                       # 进程入口
├── config.py                     # LogConfig（pydantic-settings + YAML source）
├── backends/
│   ├── base.py                   # LogBackend / LogEntry 抽象
│   ├── factory.py                # create_backend(config)（多 addr 时返回 FanoutBackend）
│   ├── fanout.py                 # FanoutBackend：多 cluster 并发 + 合并 + 排序
│   └── loki/
│       ├── backend.py            # LokiBackend（单 Loki 实例）
│       ├── http_client.py        # 长生命周期 httpx.AsyncClient
│       └── auth.py               # 唯一的认证头构造
├── tools/
│   └── log_tools.py              # 4 个 backend-agnostic 工具
└── utils/
    ├── errors.py
    ├── logging.py                # 单次配置 structlog（输出到 stderr）
    └── time_utils.py             # tz-aware UTC + 解析 + 格式化
```

### 关键不变量

- **HTTP 客户端是单例**：进程启动时打开一次，关闭一次
- **时间在内部一律是 UTC tz-aware**；只有在向用户输出时才转换为 `LOG_TIMEZONE`
- **Tools 层不感知后端**：通过 `LogBackend` 接口与具体实现解耦
- **多 Loki 是上层 fanout**：`LokiBackend` 永远代表单个 Loki，多实例由 `FanoutBackend` 包装
- **错误明确分类**：`ValidationError` / `BackendQueryError` / `BackendHTTPError` / `BackendConnectionError`

### 添加新后端

1. 在 `src/log_mcp_server/backends/<name>/` 实现 `LogBackend` 接口
2. 在 `factory.py` 中注册
3. 添加新的 `<NAME>_*` 环境变量到 `LogConfig`
4. 工具层无需修改；多实例直接复用 `FanoutBackend`

## 开发

```bash
uv sync                      # 安装所有依赖（含 dev）

# 运行测试
uv run pytest                # 100 个测试

# 类型检查 / 格式化 / lint
uv run mypy src/
uv run black src/ tests/
uv run ruff check src/ tests/

# 启动（stdio）
uv run log-mcp-server stdio

# 启动（streamable-http）
MCP_TRANSPORT=streamable-http MCP_HOST=0.0.0.0 uv run log-mcp-server
```

### 调试

```bash
export LOG_LEVEL=DEBUG
uv run log-mcp-server
```

日志使用结构化 JSON 格式输出到 **stderr**（避免污染 stdio MCP 协议流）。

## 故障排查

| 问题 | 排查方向 |
|---|---|
| 连接错误 | 验证 `LOKI_ADDR` 可达，认证凭据正确 |
| 认证错误 | 用户名/密码或 token；多租户部署可能要求 `X-Scope-OrgID` 头部 |
| 查询返回 metric 类错误 | LogQL 指标表达式不被支持，使用日志选择器（`{...}` 或带 `\|=`/`\|~`） |
| 一个 Loki 挂导致整体慢 | 多 Loki 扇出时单实例失败不影响其他，但会等其超时（默认 60s）。建议从 `LOKI_ADDR` 中暂时移除已知不可用的实例 |
| MCP 客户端找不到工具 | 工具名是 `query_logs` / `get_labels` / `get_label_values` / `health_check` |
| Ingress 504 / 长连接断开 | streamable-http 需要长连接，配置 nginx 的 `proxy-read-timeout` / `proxy-send-timeout` 和关闭 `proxy-buffering`，见 `k8s/ingress.example.yaml` |

## License

MIT License - 详见 [LICENSE](./LICENSE) 文件
