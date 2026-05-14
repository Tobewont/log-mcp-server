---
name: log-query
description: "通过 log-mcp-server（Loki 后端）查询应用/服务/Kubernetes 日志。当用户提到 '查日志'、'看错误日志'、'看报错'、'check logs'、'show me errors'、'why did service X fail'、'什么时候报错了'，或者任何需要日志数据才能回答的问题时，使用此 skill。"
---

# 日志查询 Skill

使用 [`log-mcp-server`](../../README.md) 回答日志相关问题的工作流指南。服务器暴露 4 个工具（`query_logs`、`get_labels`、`get_label_values`、`health_check`），支持多 Loki 实例 + 多租户透明查询。

## 何时使用

以下场景应激活此 skill：

- 用户询问日志、错误、异常、堆栈跟踪、请求链路
- 用户给出服务名 / Pod / App / Namespace 并问"发生了什么"
- 用户给出时间窗口并问"那段时间怎么了"
- 用户想跨服务关联事件
- 有告警或故障需要根因定位

## 核心原则

1. **任何查询前先 `health_check`。** 检查 `Filter Source`：若是 `(unset ...)`，**立即停止**并指导用户配置 `X-Allowed-Tenants` / `LOKI_CLIENT_TENANTS`，不要尝试绕过；只有 `Allowed Tenants` 已设定后才继续。
2. **调用工具，不要模拟。** 如果 `log-mcp-server` 可达，直接调用工具。不要凭空编造日志行，不要转述工具输出。
3. **先发现，再查询。** 多租户环境下，先用 `get_labels` / `get_label_values` 定位目标租户，然后只查该租户。全租户扇出既慢又嘈杂。
4. **生产环境下每次 `query_logs` 只查一个租户。** 全租户扇出仅用于"我完全不知道数据在哪"的探索阶段。
5. **LogQL 要精确。** 始终使用流选择器 `{...}`。尽早加上 `|= "keyword"` / `|~ "regex"` 过滤——返回少意味着快和便宜。
6. **不要随意指定 `limit`。** 省略 `limit` 以使用服务端的 `LOG_DEFAULT_LIMIT`。只有用户明确要求"只看前 N 条"或"我要更多"时才传值。
7. **暴露部分失败。** 如果返回中有 `Errors` 区（每租户或每集群的错误），必须告知用户——不能静默吞掉。

## 工具速查

| 工具 | 用途 | 必填参数 |
|---|---|---|
| `query_logs` | 按时间范围和 LogQL 选择器查询日志 | `query` |
| `get_labels` | 列出各租户可用的标签名 | — |
| `get_label_values` | 列出某标签在各租户的值 | `label` |
| `health_check` | 查看后端/集群健康状态 | — |

前三个工具的可选参数：`start`、`end`（RFC3339）、`tenant`、`instance`。`query_logs` 还支持 `limit` 和 `direction`（`backward` / `forward`）。

> **`instance` 参数**：用户明确指定 Loki 实例时（例如"查 `loki.example.com` 上的日志"），传入对应的 cluster id（从 `health_check` 输出中获取）。指定后只查该实例，绕过多实例扇出。未指定时按原工作流并发查询所有健康实例。

### 客户端必须声明租户范围（强制）

三个日志查询工具（`query_logs` / `get_labels` / `get_label_values`）**要求 MCP 客户端先声明可见租户子集**。没声明就拒绝，错误会指引用户怎么配。`health_check` 不受影响，可以随时调用看现状。

**第一步永远是 `health_check`**，看：

- `Server Tenants` — 服务端配置的全集（你能选的最大范围）
- `Allowed Tenants (this session)` — 当前会话实际生效的子集
- `Filter Source` — `request header X-Allowed-Tenants` / `env LOKI_CLIENT_TENANTS` / `(unset — log-query tools are disabled until ...)`

#### 当 `Filter Source` 是 `(unset ...)`

**立即停止调用查询工具**。任何查询都会失败并报：

```
No tenant scope is configured for this MCP client. Log-query tools require an explicit tenant list before they can run. ...
```

按下面的方法引导用户更新自己的 MCP 客户端配置（不要尝试绕过）：

- HTTP（streamable-http / sse）：在 `mcp.json` 的 `mcpServers.<name>.headers` 中加 `X-Allowed-Tenants: tenant-a,tenant-b`。
- Stdio：在 `mcp.json` 的 `mcpServers.<name>.env` 中加 `LOKI_CLIENT_TENANTS=tenant-a,tenant-b`。
- 用 `Server Tenants` 列出的值挑一个或多个填进去。

#### 当 `Filter Source` 已设置

按 `Allowed Tenants` 工作。**不要尝试** `tenant=<不在 Allowed Tenants 列表内的值>`，会被服务端直接拒绝（`Forbidden tenant ...`）。如果用户明确想查某个被限制掉的租户，让用户先更新自己的 `mcp.json`，不要绕过。

#### 主动建议用户收窄租户范围

如果发现 `Allowed Tenants` 列得很长（>3 个），并且实际查询时大量租户报 `Errors` / 数据混乱，应该**主动提醒用户**：

> 你当前 `LOKI_CLIENT_TENANTS` / `X-Allowed-Tenants` 列了 N 个租户，但本次只用到 M 个；把无关的租户从配置里去掉会让查询更快、结果更精准、Errors 更少。

调用查询工具时**优先 `tenant=<id>` 显式单租户**，再退到默认扇出。扇出仅在"完全不知道数据在哪"的探索阶段使用。

## 推荐工作流（多租户）

```
1. get_labels()                              -> 看各租户有哪些标签名
2. get_label_values(label="<相关标签>")       -> 找到拥有目标值的租户
3. query_logs(tenant="<id>", query='{...}')  -> 精准查询，单租户
```

标签选择**完全由用户问题决定**——没有硬编码：

- "查 **drama** 服务的日志" → `app` / `service` / `component`
- "查 pod xyz 的日志" → `pod` / `instance`
- "查 **prod** 的错误" → `env` / `environment`
- "查 namespace **operation-devops-prod** 的日志" → `namespace` / `k8s_namespace`

如果只配了单租户，跳过步骤 1–2，直接调用 `query_logs`。

### 用户指定 Loki 实例

如果用户明确给出 Loki 域名/地址（例如"查 `loki.example.com` 的日志"、"用 `loki-bj` 那个实例"），先用 `health_check` 看实际的 cluster id，再带上 `instance` 参数：

```
query_logs(
  tenant="<id>",
  instance="loki.example.com",        # 来自 health_check 输出的 cluster id
  query='{...}'
)
```

这种场景下**不要扇出**——既快又精确。

## 时间窗口

- 省略 `start` 和 `end` 时使用服务端默认窗口：`[now − LOG_DEFAULT_TIME_RANGE_MINUTES, now]`（通常 30 分钟）。
- 用户说"10:29 左右"/"昨晚 22:30 的故障"时，给一个窄窗口（±5 ~ ±15 分钟）——窗口越窄返回越快越干净。
- 始终传带时区的 RFC3339 格式：`2026-05-12T10:29:00+08:00` 或 `2026-05-12T02:29:00Z`。不要传不带时区的时间。

## LogQL 速查表

| 需求 | LogQL |
|---|---|
| 某个 app 的日志 | `{app="drama"}` |
| 多标签组合 | `{namespace="prod", app="drama"}` |
| 子串匹配 | `{app="drama"} \|= "error"` |
| 正则匹配 | `{app="drama"} \|~ "(?i)timeout\|panic"` |
| 排除子串 | `{app="drama"} != "healthcheck"` |
| JSON 字段过滤 | `{app="drama"} \| json \| level="error"` |

> 指标表达式（`rate(...)`、`count_over_time(...)` 等）**不受支持**。只能使用日志流选择器。

## 接入方式——三种途径访问 MCP

根据当前环境选择合适的传输方式。无论哪种方式，skill 工作流完全一致。

### 方式 A：IDE 内置 MCP（Cursor / Claude Desktop / VS Code MCP）

直接按工具名调用（`query_logs`、`get_labels` 等）。IDE 自动处理底层协议。在 Cursor 中配好 `mcp.json` 即为此模式。

Streamable-HTTP 客户端配置示例：

```json
{
  "mcpServers": {
    "logs": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

如果想限制本客户端只能查指定租户子集：

```json
{
  "mcpServers": {
    "logs": {
      "url": "http://localhost:8000/mcp",
      "headers": { "X-Allowed-Tenants": "team-a,team-b" }
    }
  }
}
```

Stdio 客户端配置示例：

```json
{
  "mcpServers": {
    "logs": {
      "command": "log-mcp-server",
      "args": ["stdio"],
      "env": {
        "LOKI_ADDR": "https://loki.example.com",
        "LOKI_TENANTS": "tenant-a|tenant-b",
        "LOKI_CLIENT_TENANTS": "tenant-a"
      }
    }
  }
}
```

### 方式 B：`mcporter` CLI

`mcporter`（或 `npx @modelcontextprotocol/inspector`）可在命令行调用 MCP 工具：

```bash
# 列出工具
mcporter list http://localhost:8000/mcp

# 查某租户的标签
mcporter call http://localhost:8000/mcp get_labels '{"tenant":"tenant-a"}'

# 查询日志
mcporter call http://localhost:8000/mcp query_logs '{
  "query": "{app=\"drama\"} |= \"error\"",
  "tenant": "tenant-a",
  "start": "2026-05-12T02:00:00Z",
  "end": "2026-05-12T03:00:00Z"
}'
```

### 方式 C：直接 `curl` 调用 streamable-http

在没有 MCP 客户端的场景（CI 脚本、临时调试）可直接通过 JSON-RPC 对话。需要先执行一次 `initialize` 握手。

```bash
ENDPOINT="http://localhost:8000/mcp"

# 1) 初始化会话（拿到 Mcp-Session-Id 响应头）
SESSION=$(curl -s -D - -o /dev/null \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
        "protocolVersion":"2025-06-18",
        "capabilities":{},
        "clientInfo":{"name":"curl","version":"0.1"}}}' \
  "$ENDPOINT" | awk -F': ' '/Mcp-Session-Id/{print $2}' | tr -d '\r')

# 2) 确认初始化完成
curl -s -H "Content-Type: application/json" \
     -H "Accept: application/json, text/event-stream" \
     -H "Mcp-Session-Id: $SESSION" \
     -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
     "$ENDPOINT" >/dev/null

# 3) 调用工具（这里以 get_labels 为例）
curl -s -H "Content-Type: application/json" \
     -H "Accept: application/json, text/event-stream" \
     -H "Mcp-Session-Id: $SESSION" \
     -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{
           "name":"get_labels","arguments":{"tenant":"tenant-a"}}}' \
     "$ENDPOINT"
```

> 协议版本（`protocolVersion`）随 MCP 版本更新而变化。如果返回 `unsupported protocol`，检查 `mcp` 库版本并相应调整。

> 如需把本次会话限制在特定租户子集，给上述每一次 `curl` 加上 `-H 'X-Allowed-Tenants: tenant-a,tenant-b'` 即可。

SSE 传输（`/sse`）流程类似，但使用 `EventSource` 风格的流式响应。本地开发推荐直接用 Stdio 或上述高层方式。

## 完整示例

> 用户："查一下昨天晚上 22:30 左右 drama 服务的报错日志"

1. **确认数据归属**：如果是多租户，先 `get_label_values(label="app")` 找到 `app="drama"` 在哪个 tenant。
2. **选择窄时间窗口**：
   - `start = "2026-05-11T14:25:00Z"`（UTC），`end = "2026-05-11T14:35:00Z"` — 即东八区 `22:25–22:35`。
3. **执行查询**（带 error 倾向的过滤）：
   ```
   query_logs(
     tenant="<目标租户>",
     query='{app="drama"} |~ "(?i)error|exception|panic"',
     start="2026-05-11T14:25:00Z",
     end="2026-05-11T14:35:00Z"
   )
   ```
4. **总结输出** — 按标签（如 `pod`、`level`）分组，高亮堆栈跟踪，给出计数，**引用**代表性日志行。提及服务端返回的 `Errors` 区。

## 反模式

- ❌ **用户给了明确时间却不传 `start`/`end`。** 应传入精确窗口——默认窗口比需要的宽，会引入噪音。
- ❌ **硬编码 `limit=100` 或其他小值。** 要么省略（使用服务端默认值），要么设成足够大。
- ❌ **每次都扇出所有租户。** 先用 `get_label_values` 缩小范围。租户是实现细节，用户不应感知。
- ❌ **生成 `rate(...)` / `count_over_time(...)` 等指标表达式。** 本服务只支持日志流查询，指标表达式会返回校验错误。
- ❌ **原样粘贴返回的 Markdown 报告。** 应阅读后因果总结、引用最少量代表性行，再提供深入选项。
- ❌ **忽略 `Errors` 区。** 如果有租户或集群失败，必须告知用户——部分成功不等于完全成功。

## 查无结果时的排查

如果 `query_logs` 返回 "No log entries found"：

1. 用**更宽**的时间窗口（扩大 3 倍）重新查，或去掉 `|= "..."` 子串过滤。
2. 用 `get_label_values(label="<X>")` 验证标签/值组合——可能拼写错误或者数据在其他租户。
3. 跑 `health_check`——可能有集群不健康。服务器会自动跳过不健康集群，但结果集相应减少。
4. 如果用户关心的是**最早**的事件，尝试 `direction="forward"`。

## 输出风格

每次输出应做到：

1. 先用一行总结发现（如："在 22:29:17 ~ 22:29:30 期间发现 23 条 error，全部来自 `drama-84f4c7fcc6-d2lnd`"）。
2. 展示实际运行的 LogQL（方便用户自行复查）。
3. 引用 1–3 条代表性日志行（不是全量转储）。过长的行用 `…` 截断。
4. 列出服务端返回的 `Errors`（如有）。
5. 给出具体的后续建议（"需要扩大时间窗口吗？" / "要不要再加 level=warning 过滤？"）。

绝不在响应有错误、无条目或仅部分集群覆盖时声称"成功"。对已检查和未检查的范围保持诚实。
