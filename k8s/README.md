# Kubernetes 部署

本目录提供 log-mcp-server 在 Kubernetes 上的部署清单。默认使用 **streamable-http** transport，端点 `/mcp`。

## 文件清单

| 文件 | 说明 |
|---|---|
| `namespace.yaml` | 命名空间 `log-mcp` |
| `configmap.yaml` | 非敏感配置（Loki 地址、租户、超时、时区等） |
| `secret.example.yaml` | 敏感信息模板（用户名/密码/token），**生产请勿提交** |
| `deployment.yaml` | 主应用 Deployment（非 root、只读根文件系统、TCP 探针） |
| `service.yaml` | ClusterIP Service |
| `ingress.example.yaml` | 可选 Ingress 模板（含长连接所需的 nginx 注解） |
| `hpa.example.yaml` | 可选水平自动扩缩 |
| `kustomization.yaml` | Kustomize 入口 |

## 准备镜像

先在本地或 CI 构建镜像并推送到集群可访问的仓库：

```bash
# 本地构建
docker build -t your-registry/log-mcp-server:1.0.0 .
docker push your-registry/log-mcp-server:1.0.0

# 然后在 kustomization.yaml 中把 newName / newTag 改为你的镜像
```

## 配置

### 1. ConfigMap（非敏感）

编辑 `configmap.yaml`：

```yaml
LOG_BACKEND: "loki"

# 单 Loki
LOKI_ADDR: "http://loki.observability.svc.cluster.local:3100"

# 多 Loki（Thanos 风格扇出查询，| 分隔）
# LOKI_ADDR: "http://loki-bj:3100|http://loki-sh:3100|http://loki-sg:3100"

LOKI_TENANTS: "team-a|team-b"
LOG_TIMEZONE: "Asia/Shanghai"
```

### 2. Secret（敏感）

**不要**直接 apply `secret.example.yaml`。建议两种方式之一：

```bash
# 方式 A：命令行创建（推荐）
kubectl create secret generic log-mcp-server-secrets \
  --namespace=log-mcp \
  --from-literal=LOKI_USERNAME=user \
  --from-literal=LOKI_PASSWORD='your-password'

# 方式 B：复制示例文件并改名（不要提交到 git）
cp k8s/secret.example.yaml k8s/secret.yaml
$EDITOR k8s/secret.yaml
kubectl apply -f k8s/secret.yaml
```

## 部署

### 使用 kubectl 直接部署

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/configmap.yaml
# 用上面"方式 A"创建 Secret，或 apply 你自己的 secret.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/deployment.yaml

# 可选
kubectl apply -f k8s/ingress.example.yaml   # 改好 host/TLS 后再 apply
kubectl apply -f k8s/hpa.example.yaml
```

### 使用 Kustomize

```bash
kubectl apply -k k8s/
```

要包含可选资源，编辑 `k8s/kustomization.yaml` 取消相应行的注释。

## 验证

```bash
# Pod 是否就绪
kubectl -n log-mcp get pods -l app.kubernetes.io/name=log-mcp-server

# 看启动日志
kubectl -n log-mcp logs -l app.kubernetes.io/name=log-mcp-server -f

# 端口转发本地测试
kubectl -n log-mcp port-forward svc/log-mcp-server 8000:8000
# 然后在 MCP 客户端连 http://localhost:8000/mcp
```

## 客户端配置

### Claude Desktop（streamable-http）

```json
{
  "mcpServers": {
    "logs": {
      "url": "https://logs-mcp.example.com/mcp"
    }
  }
}
```

客户端从路径自动识别协议（`/mcp` → streamable-http，`/sse` → SSE）。如果使用 Ingress，把 URL 改成你的对外地址；如果只在集群内部使用，直接用 `http://log-mcp-server.log-mcp:8000/mcp`。

#### 客户端必须声明可见的租户子集（强制）

服务端 `LOKI_TENANTS` 是上限。每个 MCP 客户端**必须**在请求头里声明可见租户子集，否则三个查询工具会直接拒绝（`No tenant scope is configured ...`）。`health_check` 不受影响，可用于诊断。

```json
{
  "mcpServers": {
    "logs": {
      "url": "https://logs-mcp.example.com/mcp",
      "headers": { "X-Allowed-Tenants": "team-a,team-b" }
    }
  }
}
```

服务端将把这次请求的可见租户**强制限制**为 `team-a, team-b`（且必须是 `LOKI_TENANTS` 的子集）。这是纵深防御而非鉴权——能改 `mcp.json` 的人也能改 header；目的是迫使每个客户端显式声明意图，避免漫无目的的全租户扇出。Stdio 模式下用 `env.LOKI_CLIENT_TENANTS` 达到同样效果。

> **建议只列当前业务真正需要的租户。** 不传 `tenant=` 时服务端会并行扇出到列表里所有租户：列得越多越慢、结果越混乱、`Errors` 越长。最佳实践是按业务拆多个客户端配置，每个客户端只列 1–3 个租户。

## 常见问题

**Pod 反复 CrashLoopBackOff**：通常是配置缺失。先用 `kubectl logs` 看错误，最常见的是 `LOKI_ADDR` 不可达或认证失败。

**Ingress 504 / 连接断开**：streamable-http 需要长连接。`ingress.example.yaml` 已经配置了必要的 nginx 注解；其他 Ingress Controller（如 Traefik）请按各自文档配置长连接和取消缓冲。

**多 Loki 一个挂掉影响响应**：扇出后单个 Loki 失败不会阻塞其他 Loki，错误会**显式列在工具响应底部的 Errors 区**（标记为 `cluster, tenant=...`），同时也会输出到 stderr 日志。如果某个 Loki 已知不可用，建议从 `LOKI_ADDR` 中暂时移除以避免每次查询的 60s 超时等待。
