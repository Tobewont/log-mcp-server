# Log MCP Server

![python](https://img.shields.io/badge/python-3.12%2B-blue) ![license](https://img.shields.io/badge/license-MIT-green)

[中文](README.md) | English

A Log MCP Server based on the [Model Context Protocol](https://modelcontextprotocol.io) (built on [FastMCP](https://github.com/modelcontextprotocol/python-sdk)) that provides log query and analysis capabilities. The current implementation ships a **Grafana Loki** backend with **multi-Loki fan-out** queries and provides an extensible `LogBackend` interface so other log systems (Elasticsearch, CloudWatch, ClickHouse, …) can be plugged in.

## Highlights

- **Pluggable backends** via `LOG_BACKEND` (currently `loki`)
- **Multi-Loki fan-out**: set `LOKI_ADDR=url1|url2|url3` and the server queries them in parallel and merges results — Thanos-style aggregation for Loki
- **Multi-tenant concurrency** with per-`(cluster, tenant)` timeouts; partial failures are explicitly surfaced
- **All MCP transports**: `stdio` / `sse` / `streamable-http` (the latter being the **recommended** modern transport)
- **Lean tool surface**: 4 backend-agnostic tools — small enough for an AI to use immediately, full enough to cover real workflows
- **Timezone-consistent**: internal datetimes are always UTC tz-aware, output is rendered in the configured timezone (`Asia/Shanghai` by default)
- **Single long-lived HTTP connection pool**: avoids reconnecting on every tool invocation
- **Health caching**: unhealthy clusters are detected once and skipped automatically until the next refresh

## Quick Start

### Docker Compose (recommended for local dev)

This brings up `log-mcp-server`, Loki, and Grafana together:

```bash
git clone https://github.com/your-org/log-mcp-server.git
cd log-mcp-server

cp env.example .env       # adjust as needed
docker compose up -d

docker compose logs -f log-mcp-server
```

Endpoints after startup:

- `log-mcp-server` — streamable-http at [http://localhost:8000/mcp](http://localhost:8000/mcp)
- Loki — [http://localhost:3100](http://localhost:3100)
- Grafana — [http://localhost:3000](http://localhost:3000) (admin / admin)

### From source (with [uv](https://docs.astral.sh/uv/))

```bash
git clone https://github.com/your-org/log-mcp-server.git
cd log-mcp-server
uv sync          # installs all deps (incl. dev)
uv run log-mcp-server
```

## Available Tools

> Design rule: **don't add new tools unless strictly necessary**. The four tools below cover health, log querying, label discovery, and label-value enumeration — sufficient for the vast majority of AI-driven log workflows.

### Recommended workflow (multi-tenant)

Unhealthy clusters are **skipped automatically** (probed at startup and refreshed every 5 minutes by default). In multi-tenant deployments AI can perform a transparent two-step **discover → query** workflow so end users never need to specify a tenant:

1. `get_labels()` — discover which label names exist per tenant
2. `get_label_values(label="<relevant_label>")` — find which tenant owns the value you care about
3. `query_logs(tenant="<id>", query='{<label>="<value>"}|="keyword"')` — precise query against that tenant only

> Labels are entirely user-defined: `namespace`, `app`, `job`, `env`, etc. The AI selects the relevant label based on the user's intent — there is no hard-coded label name in the server.

> **Targeting a specific Loki instance**: when the user explicitly names a Loki (e.g. "show me logs from `loki.example.com`"), pass its cluster id via the optional `instance` parameter (available on all three query tools). The query then runs against that single cluster only, bypassing fan-out. Cluster ids are visible in the `health_check` output.

### `query_logs`

Query logs over a time range. When `tenant` is provided only that tenant is queried; otherwise all **client-allowed** tenants are queried in parallel (the intersection of `X-Allowed-Tenants` / `LOKI_CLIENT_TENANTS` and the server-side `LOKI_TENANTS` — see "Client-side tenant scope" below).

| Argument | Required | Description |
|---|---|---|
| `query` | yes | A LogQL log selector, e.g. `{job="nginx"} \|= "error"`. **Metric expressions are not supported** (`rate()`, `count_over_time()`, …) |
| `start` | no | RFC3339 / ISO 8601, e.g. `2025-01-01T00:00:00Z` or `2025-01-01T00:00:00+08:00`. Defaults to `end - LOG_DEFAULT_TIME_RANGE_MINUTES` |
| `end` | no | Same format. Defaults to *now* |
| `limit` | no | **Per-tenant** entry cap. With multi-Loki fan-out the entries are first merged across clusters and sorted by time within each tenant, then truncated to this limit. Defaults to `LOG_DEFAULT_LIMIT`; cannot exceed `LOG_MAX_LIMIT` |
| `direction` | no | `backward` (newest first, default) or `forward` |
| `tenant` | no | Tenant ID. **Strongly recommended** for multi-tenant deployments to avoid unnecessary fan-out |
| `instance` | no | Loki cluster id (e.g. `loki-bj:3100` or `loki.example.com`, as shown by `health_check`). When given, the query is restricted to **this single cluster only** — bypassing fan-out |

Returns a Markdown report. Each log entry carries `Tenant` and `Cluster` (when multiple Lokis are configured). Any partial failures (per-tenant or per-cluster) are listed at the bottom in an **Errors** section.

### `get_labels`

List the set of label names (de-duplicated). When `tenant` is provided only that tenant is queried.

| Argument | Required | Description |
|---|---|---|
| `start` | no | Optional time-range start. Narrows the search and reduces response size on large deployments |
| `end` | no | Optional time-range end |
| `tenant` | no | Tenant ID; omit to query all **client-allowed** tenants |
| `instance` | no | Loki cluster id; omit for default fan-out across healthy clusters |

### `get_label_values`

List all values of a given label (de-duplicated). When `tenant` is provided only that tenant is queried.

| Argument | Required | Description |
|---|---|---|
| `label` | yes | Label name |
| `start` | no | Optional time-range start |
| `end` | no | Optional time-range end |
| `tenant` | no | Tenant ID; omit to query all **client-allowed** tenants |
| `instance` | no | Loki cluster id; omit for default fan-out across healthy clusters |

### `health_check`

Returns backend health.  With multiple Lokis it shows per-cluster status (`healthy` / `unhealthy`) and the Loki version.  Also reports the **Allowed Tenants** for the current session and where the filter came from (see next section).  No arguments.

### `download_logs`

Run a LogQL query and write the results to a file the **user can pull onto their own machine** for offline analysis (grep / jq / Excel / ingestion) — without paying for the contents in LLM tokens. If the query succeeds but matches `0` log entries, no empty file or download link is created; the tool returns an empty-result message instead.

| Argument | Required | Description |
|---|---|---|
| `query` | yes | LogQL — same as `query_logs` |
| `start` / `end` | no | Strongly recommended to set explicitly so you don't pull GBs by accident |
| `limit` | no | Per-tenant cap; defaults to `LOG_MAX_LIMIT`, must not exceed it |
| `direction` | no | `backward` / `forward` |
| `tenant` / `instance` | no | Same semantics as `query_logs`; client must have declared `X-Allowed-Tenants` / `LOKI_CLIENT_TENANTS` |
| `fmt` | no | `jsonl` (default) / `csv` / `txt` |

**"Download to local" works differently per transport** — MCP itself has no API for the server to write a file on the client, so the implementation has to differ:

| Transport | Tool returns | How the user retrieves the file |
|---|---|---|
| `stdio` (server is launched by the MCP client on the user's machine) | An **absolute path** on the local filesystem | `cat` / `open` / editor |
| `streamable-http` / `sse` (server runs remote, e.g. on K8s) | A **download URL** like `https://logs-mcp.example.com/mcp/download/<token>` | Open in a browser or `curl -O <URL>` |

The download route is mounted under `<MCP path prefix>/download/<token>` (default `/mcp/download/<token>` for streamable-http; `/sse/download/<token>` for sse), i.e. **under the same prefix as the MCP endpoint itself**.  Any reverse-proxy / Ingress rule that already forwards `/mcp` to this backend automatically covers downloads — **no extra forwarding rule needed**.  The token is the only credential — `secrets.token_urlsafe(32)` (~256 bits of entropy).  Files live for `LOG_DOWNLOAD_TTL_SECONDS` (1 hour by default); a successful download consumes the token and deletes the file immediately, while unused expired links are cleaned up when accessed or when a new download is registered. The download registry is in-memory, so after a process restart it cannot identify files left by the previous process. The default K8s deployment writes to `/tmp` on an `emptyDir`, which is cleaned when the Pod is deleted; use an external cleanup policy if you switch to persistent storage.

**Configuration**:

| Env var | Default | Purpose |
|---|---|---|
| `LOG_DOWNLOAD_DIR` | `./logs/downloads` | Where the server writes files; in stdio this **is** the user's machine |
| `LOG_DOWNLOAD_TTL_SECONDS` | `3600` | TTL for tokens and files in HTTP mode |
| `LOG_DOWNLOAD_BASE_URL` | _unset_ | Public URL base used to render the download link; falls back to the incoming request's Host header |

**Output formats**:

- `jsonl` — one JSON object per line `{time, tenant, cluster, labels, line}`; ideal for `jq` / `grep` / pipelines.
- `csv` — `time, tenant, cluster, labels, line`; `labels` is a JSON string so commas inside don't break columns.
- `txt` — human-readable, `[time] tenant/cluster {k=v, ...} line`.

> The download `limit` is a **per-tenant** cap bounded by `LOG_MAX_LIMIT` (and Loki's own `max_entries_limit_per_query`). A multi-tenant download may contain more total entries than a single `limit`. Multi-Loki clusters may have different `max_entries_limit_per_query` values; when `instance` is omitted, low-cap clusters are retried with their own cap and do not lower the limit used for higher-cap clusters. If the target cluster is known, pass `instance` explicitly to avoid querying unrelated clusters. To download more, narrow the time window and call again.

## Client-side tenant scope (required)

`LOKI_TENANTS` defines the **full set** of tenants this server process is authorised to query.  On top of that, every MCP client **must** declare which subset it actually wants to see — otherwise the log-query/download tools (`query_logs` / `get_labels` / `get_label_values` / `download_logs`) **refuse to run** and tell the operator how to configure the scope.  `health_check` is a diagnostic and stays available so operators can inspect the active scope and the filter source.

> This is **defence in depth**, not authentication — anyone with write access to the MCP client config can change the header / env.  The point is to force every client to declare intent, eliminating accidental fan-outs across tenants the user does not actually care about.

| Transport | How a client declares its subset |
|---|---|
| `streamable-http` / `sse` | `X-Allowed-Tenants` HTTP header on every request, **comma-separated** |
| `stdio` | `env.LOKI_CLIENT_TENANTS` in the MCP client config (comma-separated) |

Precedence: `X-Allowed-Tenants` header > `LOKI_CLIENT_TENANTS` env.  The subset must be a subset of `LOKI_TENANTS`; otherwise startup fails (env mode) or the request is rejected (header mode).

Error model:

- **Unset** → `RuntimeError: No tenant scope is configured for this MCP client. ...` (with explicit guidance on how to set it).
- **Forbidden `tenant=`** → `RuntimeError: Forbidden tenant ...`
- **Empty intersection with server tenants** → `RuntimeError: No tenants are accessible. ...`

> **Keep the list small — list only the tenants you actually need.** When `query_logs` / `get_labels` / `get_label_values` are called without an explicit `tenant=`, the server fans out **in parallel to every client-allowed tenant**. The larger the list:
> - **The slower** — overall latency is bound by the slowest tenant; one timeout / 503 drags everyone down.
> - **The noisier and less precise** — different tenants reuse the same label names (e.g. `app=foo` everywhere) with different semantics, so the AI ends up mixing unrelated logs.
> - **The more verbose** — the `Errors` section grows with every flaky tenant, costing tokens for the AI to interpret.
>
> Best practice: scope each MCP client to **the 1–3 tenants the current session actually needs**.  Don't dump every tenant into `X-Allowed-Tenants` / `LOKI_CLIENT_TENANTS`.

#### Stdio client (`mcp.json`)

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

#### HTTP client (`streamable-http`)

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

`curl`:

```bash
curl -X POST http://localhost:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'X-Allowed-Tenants: team-a,team-b' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## Multi-Loki (Thanos-style fan-out)

When you operate multiple Loki instances (multi-region, multi-tenant, blue/green migrations, …) just pipe-separate them:

```bash
LOKI_ADDR="http://loki-bj:3100|http://loki-sh:3100|http://loki-sg:3100"
LOKI_TENANTS="team-a|team-b"
```

Behaviour:

- **Health cache**: every cluster is probed at startup (`GET /loki/api/v1/status/buildinfo`); the cache refreshes every `HEALTH_CHECK_INTERVAL` seconds (default 300). Unhealthy clusters are skipped for data queries to avoid timeout cascades
- Data queries only fan out to **healthy clusters**; `health_check` itself still probes every cluster (for diagnostics)
- Each `(cluster, tenant)` sub-query has an independent 30-second timeout
- Failure isolation: if one Loki is down, results from the others are returned regardless
- Global merge: entries are sorted by time → truncated to `limit`
- Each entry carries a `Cluster` tag (`host:port` by default)
- Auth, tenant list, and timeouts are **shared across clusters**

## Transports

The server supports three MCP transports:

| Transport | Endpoint | Use case |
|---|---|---|
| `stdio` | — | Local MCP clients such as Claude Desktop |
| `sse` | `/sse` | Legacy HTTP/SSE (kept for compatibility) |
| `streamable-http` | `MCP_PATH` (default `/mcp`) | **Recommended** modern HTTP transport |

### Selection priority

1. **CLI argument** wins: `log-mcp-server stdio | sse | streamable-http`
2. Otherwise the env / config value `MCP_TRANSPORT` (default `stdio`)

### Claude Desktop client config

#### Stdio (local)

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

#### HTTP (Streamable-HTTP / SSE)

Start the server first:

```bash
export MCP_TRANSPORT=streamable-http
export MCP_HOST=0.0.0.0
export MCP_PORT=8000
export LOKI_ADDR=https://loki.example.com
log-mcp-server
```

The client only needs the `url` — the protocol is inferred from the path:

```json
{
  "mcpServers": {
    "logs": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

> `/mcp` (or whatever `MCP_PATH` is set to) → streamable-http (recommended), `/sse` → SSE (legacy). `MCP_PATH` only affects the streamable-http transport; SSE is always `/sse`.

## Docker

### Build

```bash
docker build -t log-mcp-server:1.0.0 .

# push to your registry
docker tag log-mcp-server:1.0.0 your-registry/log-mcp-server:1.0.0
docker push your-registry/log-mcp-server:1.0.0
```

### Run

```bash
# single Loki + streamable-http
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

# multi-Loki fan-out
docker run -d --name log-mcp-server \
  -p 8000:8000 \
  -e MCP_TRANSPORT=streamable-http \
  -e MCP_HOST=0.0.0.0 -e MCP_PORT=8000 \
  -e LOKI_ADDR='http://loki-bj:3100|http://loki-sh:3100' \
  -e LOKI_TENANTS='team-a|team-b' \
  log-mcp-server:1.0.0

# stdio mode (typically via docker exec / no exposed port)
docker run --rm -i \
  -e LOKI_ADDR=http://your-loki:3100 \
  log-mcp-server:1.0.0 stdio
```

## Kubernetes

The `k8s/` directory ships a complete set of manifests (namespace / configmap / secret / deployment / service / ingress / hpa).

```bash
# 1. build & push image
docker build -t your-registry/log-mcp-server:1.0.0 .
docker push your-registry/log-mcp-server:1.0.0

# 2. edit k8s/configmap.yaml (LOKI_ADDR / LOKI_TENANTS / timezone, …)

# 3. create the Secret (CLI is preferred over committing secret.yaml)
kubectl create secret generic log-mcp-server-secrets \
  --namespace=log-mcp \
  --from-literal=LOKI_USERNAME=user \
  --from-literal=LOKI_PASSWORD='your-password'

# 4. apply
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/deployment.yaml

# or use Kustomize
kubectl apply -k k8s/

# 5. verify
kubectl -n log-mcp get pods
kubectl -n log-mcp logs -l app.kubernetes.io/name=log-mcp-server -f

# 6. local port-forward for testing
kubectl -n log-mcp port-forward svc/log-mcp-server 8000:8000
# point your MCP client at http://localhost:8000/mcp
```

See [`k8s/README.md`](k8s/README.md) for Ingress / HPA details and common issues.

## Configuration

Configuration sources (highest priority first):

1. Explicit `LogConfig(...)` constructor kwargs
2. Environment variables
3. `.env` file
4. YAML config file (`LOG_CONFIG_PATH` / `LOKI_CONFIG_PATH` / `./loki-config.yaml` / `./.loki-config.yaml` / `~/.loki-config.yaml`)
5. Built-in defaults

### Environment variables

| Variable | Description | Default |
|---|---|---|
| **Transport** | | |
| `MCP_TRANSPORT` | `stdio` / `sse` / `streamable-http` | `stdio` |
| `MCP_HOST` | Listen address (HTTP transports only) | `127.0.0.1` |
| `MCP_PORT` | Listen port (HTTP transports only) | `8000` |
| `MCP_PATH` | URL path prefix for the streamable-http transport (must start with `/`); ignored for SSE / stdio | `/mcp` |
| `LOG_LEVEL` | Server log level (`DEBUG` also enables FastMCP debug) | `INFO` |
| **Backend** | | |
| `LOG_BACKEND` | Active backend | `loki` |
| **Loki** | | |
| `LOKI_ADDR` | Loki URL(s) — `\|`-separated for multi-Loki | `http://localhost:3100` |
| `LOKI_TENANTS` | Tenant list (`\|`-separated) | `fake` |
| `LOKI_CLIENT_TENANTS` | Client-side tenant subset (comma-separated). Stdio only; HTTP transports use the `X-Allowed-Tenants` header instead | — |
| `LOKI_USERNAME` | Basic auth username | — |
| `LOKI_PASSWORD` | Basic auth password | — |
| `LOKI_BEARER_TOKEN` | Bearer token | — |
| `LOKI_BEARER_TOKEN_FILE` | Bearer token file path | — |
| `LOKI_CA_FILE` / `LOKI_CERT_FILE` / `LOKI_KEY_FILE` | TLS cert files | — |
| `LOKI_TLS_SKIP_VERIFY` | Skip TLS verification | `false` |
| `LOKI_CONNECT_TIMEOUT` | HTTP connect timeout (s) | `10.0` |
| `LOKI_READ_TIMEOUT` | HTTP read timeout (s) | `15.0` |
| `LOKI_WRITE_TIMEOUT` | HTTP write timeout (s) | `10.0` |
| `LOKI_POOL_TIMEOUT` | Pool acquire timeout (s) | `10.0` |
| **Health cache (multi-cluster)** | | |
| `HEALTH_CHECK_INTERVAL` | Background refresh interval (s) | `300.0` |
| `HEALTH_CHECK_TIMEOUT` | Per-cluster probe timeout (s) | `5.0` |
| **Generic query** | | |
| `LOG_DEFAULT_LIMIT` | Default entries per query | `100` |
| `LOG_MAX_LIMIT` | Hard cap on per-tenant `limit` | `5000` |
| `LOG_DEFAULT_TIME_RANGE_MINUTES` | Default time-window in minutes | `30` |
| `LOG_TIMEZONE` | Display timezone | `Asia/Shanghai` |

### YAML example

```yaml
# MCP transport
mcp_transport: "streamable-http"
mcp_host: "0.0.0.0"
mcp_port: 8000
log_level: "INFO"

# Backend
backend: "loki"

# Single Loki
addr: "https://loki.example.com"

# or multi-Loki fan-out
# addr: "https://loki-bj.example.com|https://loki-sh.example.com"

tenants: "team-a|team-b"
username: "your-username"
password: "your-password"

# Health cache (multi-cluster only)
health_check_interval: 300
health_check_timeout: 5

default_limit: 100
max_limit: 5000
default_time_range_minutes: 30
timezone: "Asia/Shanghai"
```

## Architecture

```
src/log_mcp_server/
├── main.py                       # entry point + lifespan wiring
├── config.py                     # LogConfig (pydantic-settings + YAML source)
├── backends/
│   ├── base.py                   # LogBackend / LogEntry abstractions
│   ├── factory.py                # create_backend(config) -> (backend, health_cache)
│   ├── fanout.py                 # FanoutBackend: parallel + merge + sort
│   ├── health_cache.py           # periodic health probe cache
│   └── loki/
│       ├── backend.py            # LokiBackend (single instance)
│       ├── http_client.py        # long-lived httpx.AsyncClient
│       └── auth.py               # one place to build auth headers
├── tools/
│   └── log_tools.py              # 4 backend-agnostic tools
└── utils/
    ├── errors.py
    ├── logging.py                # configure structlog once (stderr only)
    └── time_utils.py             # tz-aware UTC + parsing + formatting
```

### Invariants

- **HTTP client is a singleton** — opened once at startup, closed once at shutdown
- **All datetimes are UTC tz-aware internally** — only converted to `LOG_TIMEZONE` on output
- **Tools are backend-agnostic** — they only know about `LogBackend`
- **Multi-Loki is implemented as an upper-layer fan-out** — `LokiBackend` always represents a *single* Loki instance; multi-instance behaviour is supplied by `FanoutBackend`
- **Errors are typed**: `ValidationError` / `BackendQueryError` / `BackendHTTPError` / `BackendConnectionError`

### Adding a new backend

1. Implement the `LogBackend` interface under `src/log_mcp_server/backends/<name>/`
2. Register it in `factory.py`
3. Add the corresponding `<NAME>_*` env vars to `LogConfig`
4. The tools layer needs no changes; multi-instance support is automatic via `FanoutBackend`

## Development

```bash
uv sync                      # install all deps (incl. dev)

# run tests
uv run pytest                # 100 tests

# type-check / format / lint
uv run mypy src/
uv run black src/ tests/
uv run ruff check src/ tests/

# run (stdio)
uv run log-mcp-server stdio

# run (streamable-http)
MCP_TRANSPORT=streamable-http MCP_HOST=0.0.0.0 uv run log-mcp-server
```

### Debugging

```bash
export LOG_LEVEL=DEBUG
uv run log-mcp-server
```

Logs are emitted as structured JSON to **stderr** (so they never pollute the stdio MCP protocol stream).

## Troubleshooting

| Symptom | Where to look |
|---|---|
| Connection error | Verify that `LOKI_ADDR` is reachable and credentials are correct |
| Auth failure | Username/password or bearer token; multi-tenant deployments often require `X-Scope-OrgID` |
| "metric expression" error | LogQL metric expressions are intentionally rejected — use a stream selector (`{...}` with `\|=` / `\|~`) |
| One Loki dragging the whole response | Multi-Loki fan-out isolates failures, but the failing instance still costs a 60-second timeout per call. Remove it temporarily from `LOKI_ADDR` if it is known to be down |
| MCP client can't find the tools | The tool names are `query_logs` / `get_labels` / `get_label_values` / `health_check` |
| Ingress 504 / dropped long connection | streamable-http needs long-lived connections — set nginx `proxy-read-timeout` / `proxy-send-timeout` and disable `proxy-buffering`. See `k8s/ingress.example.yaml` |

## License

MIT License - see the [LICENSE](./LICENSE) file for details
