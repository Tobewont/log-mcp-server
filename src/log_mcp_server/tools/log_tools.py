"""与后端无关的 FastMCP 工具集。

这些工具把请求委托给当前启用的 ``LogBackend``。它们是 MCP 对外暴露
的唯一接口，因此尽量保持精简和一致，对 AI 客户端使用更友好。
"""
from __future__ import annotations

import asyncio
import json
import secrets
from datetime import datetime
from datetime import timezone as _tz
from typing import Dict, List, Optional

import structlog
from mcp.server.fastmcp import Context, FastMCP

from ..auth_context import parse_tenant_list
from ..backends.base import LogBackend, LogEntry, TenantQueryResult
from ..config import LogConfig
from ..downloads import (
    SUPPORTED_FORMATS,
    DownloadRegistry,
    write_download,
)
from ..downloads.writer import build_filename
from ..utils.errors import LogMCPError, ValidationError
from ..utils.time_utils import format_in_tz, resolve_time_range

logger = structlog.get_logger(__name__)


_PER_TENANT_TIMEOUT_SECONDS = 60.0

_backend: Optional[LogBackend] = None
_config: Optional[LogConfig] = None
_download_registry: Optional[DownloadRegistry] = None
_download_url_path: str = "/mcp/download"


def initialize_tools(
    backend: LogBackend,
    config: LogConfig,
    download_registry: Optional[DownloadRegistry] = None,
    download_url_path: str = "/mcp/download",
) -> None:
    """注入当前启用的后端 / 配置 / 注册表（启动时调用一次）。

    Args:
        backend: 当前启用的日志后端。
        config: 生效的 :class:`LogConfig`。
        download_registry: 仅对 HTTP 传输（streamable-http / sse）
            有意义——此时注册表持有"令牌 → 文件"映射，供下载路由
            消费。stdio 模式下应当传 ``None``（工具会直接返回
            文件路径）。
        download_url_path: 下载路由挂载的 URL 路径前缀，**不**包含
            末尾的 token 段。必须与 ``main.py`` 中注册的路由保持一致。
            默认为 ``/mcp/download``，与 MCP 主路径共享前缀，便于
            任何已经覆盖 ``/mcp`` 的反向代理规则自动覆盖下载链接。
    """
    global _backend, _config, _download_registry, _download_url_path
    _backend = backend
    _config = config
    _download_registry = download_registry
    _download_url_path = download_url_path or "/mcp/download"
    logger.info(
        "Tools initialised",
        backend=backend.name,
        tenants=backend.tenants,
        download_registry_enabled=download_registry is not None,
        download_url_path=_download_url_path,
    )


def _require_state() -> tuple[LogBackend, LogConfig]:
    if _backend is None or _config is None:
        raise RuntimeError("Tools not initialised; call initialize_tools() first")
    return _backend, _config


def _get_download_registry() -> Optional[DownloadRegistry]:
    return _download_registry


# ---------------------------------------------------------------------------
# 多租户扇出相关辅助函数
# ---------------------------------------------------------------------------
def _client_filter_for(
    ctx: Optional[Context], config: LogConfig
) -> tuple[Optional[List[str]], str]:
    """读取客户端为本次调用声明的租户子集。

    返回 ``(subset, source)``：

    * 在 HTTP 传输（streamable-http / SSE）下，streamable_http server
      会通过 ``ServerMessageMetadata.request_context`` 把底层 Starlette
      ``Request`` 透传给每次工具调用，我们直接从中读取
      ``X-Allowed-Tenants`` 请求头。这条路径能穿过 ASGI middleware +
      contextvar 跨不过去的任务边界。
    * stdio 模式下没有 Starlette ``Request``，回退到
      ``LOKI_CLIENT_TENANTS`` 环境变量（在 :class:`LogConfig` 内解析）。

    返回 ``None`` 表示"客户端未声明范围"，此时日志查询/下载工具会
    直接拒绝执行。
    """
    request = None
    if ctx is not None:
        try:
            request = ctx.request_context.request
        except Exception:
            request = None

    if request is not None:
        header: Optional[str] = None
        try:
            # RFC 7230 §3.2.2：同名 header 出现多次时，等价于把它们的
            # 值用逗号拼成一个 header。Starlette 的 ``.get()`` 只会
            # 返回第一次出现的值，所以这里用 ``getlist`` 后自己拼接
            # 以保持语义正确。
            values = request.headers.getlist("x-allowed-tenants")
            if values:
                header = ",".join(values)
        except Exception:
            header = None
        return parse_tenant_list(header), "header"

    return config.get_client_tenant_list(), "env"


def _effective_tenants(
    backend: LogBackend, config: LogConfig, ctx: Optional[Context]
) -> Optional[List[str]]:
    """返回 *本次请求 / 本进程* 实际可见的租户列表。

    客户端声明的子集（HTTP 模式下来自请求头，stdio 来自
    ``LOKI_CLIENT_TENANTS`` 环境变量）会与 ``backend.tenants`` 取交集，
    从而确保畸形或恶意的客户端配置无法把可见范围扩大到服务端允许
    范围之外。

    若客户端 **未声明任何** 范围，返回 ``None``——此时日志查询/下载
    工具必须拒绝执行，迫使用户先声明要查的租户。``health_check`` 不
    走这条路径（它是诊断工具，不是日志查询）。
    """
    client, _source = _client_filter_for(ctx, config)
    if client is None:
        return None

    server_set = set(backend.tenants)
    return [t for t in client if t in server_set]


def _client_filter_required_error(backend: LogBackend) -> RuntimeError:
    return RuntimeError(
        "No tenant scope is configured for this MCP client. Log-query "
        "tools require an explicit tenant list before they can run. "
        "HTTP transports (streamable-http / sse): send the "
        "'X-Allowed-Tenants: <tenant-a>,<tenant-b>' request header. "
        "Stdio transport: set 'LOKI_CLIENT_TENANTS=<tenant-a>,<tenant-b>' "
        "in the env block of your MCP client config (e.g. mcp.json). "
        f"Tenants configured on this server: {', '.join(backend.tenants)}."
    )


def _resolve_tenants(
    backend: LogBackend,
    config: LogConfig,
    tenant: Optional[str],
    ctx: Optional[Context],
) -> List[str]:
    """返回本次实际要查询的租户列表。

    若客户端未声明租户子集，抛 ``RuntimeError``——参见
    :func:`_effective_tenants`。否则在显式给出 ``tenant`` 时校验它必须
    在生效集合内，并返回单元素列表；未给 ``tenant`` 时返回全部生效
    租户。
    """
    effective = _effective_tenants(backend, config, ctx)
    if effective is None:
        raise _client_filter_required_error(backend)
    if not effective:
        raise RuntimeError(
            "No tenants are accessible. The client filter "
            "(X-Allowed-Tenants header or LOKI_CLIENT_TENANTS env) "
            "intersected with the server tenants produced an empty "
            f"set. Server tenants: {', '.join(backend.tenants)}."
        )
    if tenant is None:
        return effective
    tenant = tenant.strip()
    if not tenant:
        raise RuntimeError("tenant cannot be empty")
    if tenant not in effective:
        if tenant in backend.tenants:
            raise RuntimeError(
                f"Forbidden tenant {tenant!r}: not in the client-allowed "
                f"set {effective}. Server tenants: "
                f"{', '.join(backend.tenants)}."
            )
        raise RuntimeError(
            f"Unknown tenant {tenant!r}. "
            f"Allowed tenants: {', '.join(effective)}"
        )
    return [tenant]


async def _fan_out(
    tenants: List[str],
    coro_factory,
) -> List[TenantQueryResult]:
    """对每个租户并发执行一次协程，并对单租户设置超时。

    ``coro_factory(tenant, cluster_errors, cluster_warnings)`` 在每个
    租户上都会被调一次，且每次都会得到一份新的字典，这样多集群扇出
    后端可以在不同租户之间互不干扰地记录"部分集群失败"和"成功但
    需要用户注意"的信息。
    """

    async def _wrap(tenant: str) -> TenantQueryResult:
        cluster_errors: Dict[str, str] = {}
        cluster_warnings: Dict[str, str] = {}
        try:
            data = await asyncio.wait_for(
                coro_factory(tenant, cluster_errors, cluster_warnings),
                timeout=_PER_TENANT_TIMEOUT_SECONDS,
            )
            return TenantQueryResult(
                tenant=tenant,
                data=data,
                cluster_errors=cluster_errors,
                cluster_warnings=cluster_warnings,
            )
        except asyncio.TimeoutError:
            return TenantQueryResult(
                tenant=tenant,
                error=f"timeout after {_PER_TENANT_TIMEOUT_SECONDS:.0f}s",
                cluster_errors=cluster_errors,
                cluster_warnings=cluster_warnings,
            )
        except LogMCPError as e:
            return TenantQueryResult(
                tenant=tenant,
                error=f"{type(e).__name__}: {e}",
                cluster_errors=cluster_errors,
                cluster_warnings=cluster_warnings,
            )
        except Exception as e:  # noqa: BLE001
            return TenantQueryResult(
                tenant=tenant,
                error=f"{type(e).__name__}: {e}",
                cluster_errors=cluster_errors,
                cluster_warnings=cluster_warnings,
            )

    return await asyncio.gather(*[_wrap(t) for t in tenants])


# ---------------------------------------------------------------------------
# 输出格式化辅助函数
# ---------------------------------------------------------------------------
def _format_failures(results: List[TenantQueryResult]) -> str:
    failure_lines: List[str] = []
    for r in results:
        if not r.ok:
            failure_lines.append(f"- `{r.tenant}` (tenant): {r.error}")
        for cluster_id, err in sorted(r.cluster_errors.items()):
            failure_lines.append(
                f"- `{cluster_id}` (cluster, tenant=`{r.tenant}`): {err}"
            )
    if not failure_lines:
        return ""
    return "\n".join(["", "**Errors:**", *failure_lines]) + "\n"


def _format_warnings(results: List[TenantQueryResult]) -> str:
    warning_lines: List[str] = []
    for r in results:
        for cluster_id, warning in sorted(r.cluster_warnings.items()):
            warning_lines.append(
                f"- `{cluster_id}` (cluster, tenant=`{r.tenant}`): {warning}"
            )
    if not warning_lines:
        return ""
    return "\n".join(["", "**Warnings:**", *warning_lines]) + "\n"


def _format_log_entries(entries: List[LogEntry], tz: str) -> str:
    out: List[str] = []
    for i, e in enumerate(entries, 1):
        labels_str = ", ".join(f"{k}={v}" for k, v in sorted(e.labels.items()))
        meta_parts = [f"Tenant: {e.tenant or '-'}"]
        if e.cluster:
            meta_parts.append(f"Cluster: {e.cluster}")
        out.append(
            f"## Entry {i} ({', '.join(meta_parts)})\n"
            f"**Time:** {format_in_tz(e.timestamp, tz)}\n"
            f"**Labels:** {{{labels_str}}}\n"
            f"**Log:** {e.line}\n"
        )
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 工具注册
# ---------------------------------------------------------------------------
def register_tools(mcp: FastMCP) -> None:
    """把全部工具注册到给定的 FastMCP 实例上。"""

    # ----- health_check -------------------------------------------------
    @mcp.tool()
    async def health_check(ctx: Context) -> str:
        """检查日志后端健康状态。

        返回后端聚合状态、各 Loki 实例状态、当前时间，以及本会话生效
        的客户端租户范围（Allowed Tenants）和过滤来源。

        建议作为查询前的第一步调用。如果 Filter Source 显示 unset，
        说明客户端未声明可见租户，其它查询/下载工具都会拒绝执行；这时
        应让用户在 MCP 客户端配置中添加 X-Allowed-Tenants（HTTP 模式）
        或 LOKI_CLIENT_TENANTS（stdio 模式）。
        """
        backend, config = _require_state()
        logger.info("Tool: health_check")
        info = await backend.health_check()
        status = info.get("status", "unknown")
        status_text = {
            "healthy": "Healthy",
            "degraded": "Degraded (some clusters unhealthy)",
            "unhealthy": "Unhealthy",
        }.get(status, status)

        clusters = info.get("clusters") or []
        clusters_md = ""
        if clusters:
            lines = [
                "",
                "## Clusters",
                "",
                "| Cluster | Address | Status | Detail |",
                "|---|---|---|---|",
            ]
            for c in clusters:
                detail = c.get("error") or c.get("version") or ""
                lines.append(
                    f"| `{c.get('id', '-')}` | `{c.get('server_addr', '-')}` "
                    f"| {c.get('status', '-')} | {detail} |"
                )
            clusters_md = "\n".join(lines) + "\n"

        client, source = _client_filter_for(ctx, config)
        if client is None:
            effective: Optional[List[str]] = None
        else:
            server_set = set(backend.tenants)
            effective = [t for t in client if t in server_set]

        if client is not None and source == "header":
            filter_source = "request header X-Allowed-Tenants"
        elif client is not None and source == "env":
            filter_source = "env LOKI_CLIENT_TENANTS"
        elif source == "header":
            filter_source = (
                "(unset — log-query tools are disabled until the MCP "
                "client sends an 'X-Allowed-Tenants' request header)"
            )
        else:
            filter_source = (
                "(unset — log-query tools are disabled until "
                "'LOKI_CLIENT_TENANTS' is set in the MCP client env "
                "block, e.g. mcp.json)"
            )

        if effective is None:
            allowed_display = "(unset — log-query tools disabled)"
        elif not effective:
            allowed_display = "(empty — intersection with server tenants is empty)"
        else:
            allowed_display = ", ".join(effective)

        report = (
            f"# Log Backend Health Check\n\n"
            f"**Backend:** `{info.get('backend', backend.name)}`\n"
            f"**Status:** {status_text}\n"
            f"**Configured Clusters:** {len(clusters)}\n"
            f"**Server Tenants:** {', '.join(backend.tenants) or '-'}\n"
            f"**Allowed Tenants (this session):** {allowed_display}\n"
            f"**Filter Source:** {filter_source}\n"
            f"**Timezone:** {info.get('timezone', config.timezone)}\n"
            f"**Current Time:** {info.get('current_time', '-')}\n"
            f"{clusters_md}\n## Details\n"
            f"```json\n{json.dumps(info, indent=2, default=str)}\n```\n"
        )
        return report

    # ----- query_logs ---------------------------------------------------
    @mcp.tool()
    async def query_logs(
        query: str,
        ctx: Context,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: Optional[int] = None,
        direction: str = "backward",
        tenant: Optional[str] = None,
        instance: Optional[str] = None,
    ) -> str:
        """按 LogQL 查询指定租户（或全部可见的租户）的日志。

        推荐工作流（多租户场景）：
        1. get_labels()：发现各租户下有哪些标签名。
        2. get_label_values(label="<标签>")：查看各租户下该标签的值，
           定位目标值归属的租户。
        3. query_logs(tenant="<id>", query='{<label>="<value>"}')：
           查询指定租户的日志。

        参数：
          query     必填。LogQL 日志选择器，如 {job="nginx"} |= "error"。
                    不支持指标表达式（rate、count_over_time 等）。
          start     起始时间（RFC3339 / ISO 8601）。省略时默认为30分钟前。
          end       结束时间（RFC3339 / ISO 8601）。省略时默认为当前时间。
          limit     每个租户的返回条数上限。省略时使用 LOG_DEFAULT_LIMIT；
                    除非用户明确要求"只看前 N 条"或"我要更多"，否则不要显式传值。
          direction backward（默认，最新在前）或 forward（最早在前）。
          tenant    指定租户 ID。省略时查询所有可见租户。
          instance  指定 Loki 实例 ID（可从 health_check 输出查到）。
                    省略时按所有健康实例并发查询。

        客户端必须先在 MCP 配置中声明可见租户（X-Allowed-Tenants 或
        LOKI_CLIENT_TENANTS），否则本工具直接拒绝。
        """
        backend, config = _require_state()

        try:
            start_dt, end_dt = resolve_time_range(
                start, end, config.default_time_range_minutes
            )
        except ValidationError as e:
            raise RuntimeError(str(e)) from e

        if direction not in ("forward", "backward"):
            raise RuntimeError(
                f"Invalid direction {direction!r}; must be 'forward' or 'backward'."
            )

        if limit is not None:
            if not isinstance(limit, int) or limit <= 0:
                raise RuntimeError("limit must be a positive integer")
            if limit > config.max_limit:
                raise RuntimeError(
                    f"limit {limit} exceeds maximum {config.max_limit} "
                    f"(LOG_MAX_LIMIT)"
                )
        effective_limit = limit if limit is not None else config.default_limit

        tenants = _resolve_tenants(backend, config, tenant, ctx)

        logger.info(
            "Tool: query_logs",
            tenants=tenants,
            query=query,
            start=start_dt.isoformat(),
            end=end_dt.isoformat(),
            limit=effective_limit,
            direction=direction,
        )

        async def run_for_tenant(
            t: str,
            cluster_errors: Dict[str, str],
            cluster_warnings: Dict[str, str],
        ) -> List[LogEntry]:
            return await backend.query_logs(
                query=query,
                tenant=t,
                start=start_dt,
                end=end_dt,
                limit=effective_limit,
                direction=direction,
                instance=instance,
                cluster_errors=cluster_errors,
                cluster_warnings=cluster_warnings,
            )

        results = await _fan_out(tenants, run_for_tenant)
        all_entries: List[LogEntry] = []
        for r in results:
            if r.ok and r.data:
                all_entries.extend(r.data)

        successful = [r.tenant for r in results if r.ok]
        failed_tenants = [r for r in results if not r.ok]
        any_cluster_failure = any(r.cluster_errors for r in results)

        header = (
            f"# Log Query Results\n\n"
            f"**Backend:** `{backend.name}`\n"
            f"**Query:** `{query}`\n"
            f"**Time Range:** "
            f"`{format_in_tz(start_dt, config.timezone)}` to "
            f"`{format_in_tz(end_dt, config.timezone)}`\n"
            f"**Limit:** {effective_limit} per tenant\n"
            f"**Direction:** {direction}\n"
            f"**Tenants Queried:** `{', '.join(tenants)}`\n"
            f"**Successful Tenants:** `{', '.join(successful) or '-'}`\n"
            f"**Instance:** `{instance or '*all healthy*'}`\n"
            f"**Total Entries:** {len(all_entries)}\n"
        )

        if not all_entries and failed_tenants and not successful:
            return (
                header
                + _format_failures(results)
                + _format_warnings(results)
                + "\nAll tenant queries failed.\n"
            )
        if not all_entries:
            tail = _format_failures(results) + _format_warnings(results)
            note = ""
            if not failed_tenants and not any_cluster_failure:
                note = "\nNo log entries found.\n"
            elif not failed_tenants and any_cluster_failure:
                note = (
                    "\nNo log entries found in the surviving clusters; see "
                    "errors above.\n"
                )
            return header + tail + note

        body = _format_log_entries(all_entries, config.timezone)
        return (
            header
            + "\n"
            + body
            + _format_failures(results)
            + _format_warnings(results)
        )

    # ----- get_labels ---------------------------------------------------
    @mcp.tool()
    async def get_labels(
        ctx: Context,
        start: Optional[str] = None,
        end: Optional[str] = None,
        tenant: Optional[str] = None,
        instance: Optional[str] = None,
    ) -> str:
        """列出某租户（或全部可见租户）下的标签名。

        通常作为日志查询的第一步使用：先看有哪些标签可用，再用
        get_label_values 定位目标值归属的租户。

        参数：
          start    可选时间范围起点（RFC3339）。与 end 同时给出时，
                   只返回该时间窗内出现过的标签。省略时默认为30分钟前。
          end      可选时间范围终点（RFC3339），省略时默认为当前时间。
          tenant   指定租户 ID。省略时查询全部可见的租户。
          instance 指定 Loki 实例 ID。省略时按所有健康实例并发查询。
        """
        return await _list_keys(
            label=None,
            start=start,
            end=end,
            heading="Available Labels",
            tenant=tenant,
            instance=instance,
            ctx=ctx,
        )

    # ----- get_label_values ---------------------------------------------
    @mcp.tool()
    async def get_label_values(
        label: str,
        ctx: Context,
        start: Optional[str] = None,
        end: Optional[str] = None,
        tenant: Optional[str] = None,
        instance: Optional[str] = None,
    ) -> str:
        """列出某个标签的所有取值。

        用于在 query_logs 之前确认目标值归属于哪个租户。

        参数：
          label    必填。标签名。
          start    可选时间范围起点（RFC3339）。与 end 同时给出时，
                   只返回该时间窗内出现过的标签。省略时默认为30分钟前。
          end      可选时间范围终点（RFC3339），省略时默认为当前时间。
          tenant   指定租户 ID。省略时查询全部可见的租户。
          instance 指定 Loki 实例 ID。省略时按所有健康实例并发查询。
        """
        if not label or not label.strip():
            raise RuntimeError("Label name cannot be empty")
        return await _list_keys(
            label=label,
            start=start,
            end=end,
            heading=f"Values for Label `{label}`",
            tenant=tenant,
            instance=instance,
            ctx=ctx,
        )

    # ----- download_logs ------------------------------------------------
    @mcp.tool()
    async def download_logs(
        query: str,
        ctx: Context,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: Optional[int] = None,
        direction: str = "backward",
        tenant: Optional[str] = None,
        instance: Optional[str] = None,
        fmt: str = "jsonl",
    ) -> str:
        """按 LogQL 查询日志并写到文件，让用户离线下载到本地分析。

        适用场景：用户希望把日志拉到本地用 grep / jq / Excel 等方式
        处理，不需要 AI 在对话里复述日志内容。

        参数：
          query     必填。LogQL 日志选择器，与 query_logs 一致。
          start     起始时间（RFC3339 / ISO 8601）。强烈建议显式给出，
                    避免一次拉过多数据。
          end       结束时间（RFC3339 / ISO 8601）。
          limit     每个租户的返回条数上限。省略时使用 LOG_MAX_LIMIT。
          direction backward（默认，最新在前）或 forward（最早在前）。
          tenant    指定租户 ID。省略时查询所有客户端可见的租户。
          instance  指定 Loki 实例 ID。
          fmt       输出格式，可选 jsonl（默认）/ csv / txt。

        返回：
          命中 0 条时不生成文件，只返回空结果提示。
          HTTP 模式（streamable-http / sse）：有日志时返回完整下载
          URL，用户在本机用浏览器或 curl -O 拉取；链接默认 1 小时
          过期，成功下载后立即失效。
          stdio 模式：有日志时返回服务端绝对路径（即用户本机路径），
          直接打开即可。
        """
        backend, config = _require_state()
        registry = _get_download_registry()

        if fmt not in SUPPORTED_FORMATS:
            raise RuntimeError(
                f"Unsupported fmt {fmt!r}. Choose one of: "
                f"{', '.join(SUPPORTED_FORMATS)}."
            )

        try:
            start_dt, end_dt = resolve_time_range(
                start, end, config.default_time_range_minutes
            )
        except ValidationError as e:
            raise RuntimeError(str(e)) from e

        if direction not in ("forward", "backward"):
            raise RuntimeError(
                f"Invalid direction {direction!r}; must be 'forward' or 'backward'."
            )

        if limit is not None:
            if not isinstance(limit, int) or limit <= 0:
                raise RuntimeError("limit must be a positive integer")
            if limit > config.max_limit:
                raise RuntimeError(
                    f"limit {limit} exceeds maximum {config.max_limit} "
                    f"(LOG_MAX_LIMIT)"
                )
        # 下载默认 limit == max_limit，单次尽可能多拉；如果用户要更少
        # 数据，再显式传 limit。
        effective_limit = limit if limit is not None else config.max_limit

        tenants = _resolve_tenants(backend, config, tenant, ctx)

        logger.info(
            "Tool: download_logs",
            tenants=tenants,
            query=query,
            start=start_dt.isoformat(),
            end=end_dt.isoformat(),
            limit=effective_limit,
            direction=direction,
            fmt=fmt,
        )

        async def run_for_tenant(
            t: str,
            cluster_errors: Dict[str, str],
            cluster_warnings: Dict[str, str],
        ) -> List[LogEntry]:
            return await backend.query_logs(
                query=query,
                tenant=t,
                start=start_dt,
                end=end_dt,
                limit=effective_limit,
                direction=direction,
                instance=instance,
                cluster_errors=cluster_errors,
                cluster_warnings=cluster_warnings,
            )

        results = await _fan_out(tenants, run_for_tenant)
        all_entries: List[LogEntry] = []
        for r in results:
            if r.ok and r.data:
                all_entries.extend(r.data)

        successful = [r.tenant for r in results if r.ok]
        failed_tenants = [r for r in results if not r.ok]
        limit_reached = any(
            r.ok and r.data is not None and len(r.data) >= effective_limit
            for r in results
        )

        if not all_entries and failed_tenants and not successful:
            return (
                "# Download Failed\n\n"
                "All tenant queries failed; nothing was written.\n"
                + _format_failures(results)
            )

        if not all_entries:
            return (
                f"# Log Download Empty\n\n"
                f"**Backend:** `{backend.name}`\n"
                f"**Query:** `{query}`\n"
                f"**Time Range:** "
                f"`{format_in_tz(start_dt, config.timezone)}` to "
                f"`{format_in_tz(end_dt, config.timezone)}`\n"
                f"**Tenants Queried:** `{', '.join(tenants)}`\n"
                f"**Successful Tenants:** `{', '.join(successful) or '-'}`\n"
                f"**Instance:** `{instance or '*all healthy*'}`\n"
                f"**Format:** `{fmt}`\n"
                f"**Entries:** 0\n\n"
                "Query succeeded, but no log entries matched. "
                "No download file was created.\n"
                f"{_format_failures(results)}"
                f"{_format_warnings(results)}"
            )

        # 文件名用 tenant 名；多租户时用 "all"。
        tenant_label = tenant or (
            tenants[0] if len(tenants) == 1 else "all"
        )
        try:
            config.download_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise RuntimeError(
                f"Cannot create LOG_DOWNLOAD_DIR "
                f"{str(config.download_dir)!r}: {e}"
            ) from e

        now_utc = datetime.now(tz=_tz.utc)
        # 两个文件名：
        # * ``filename`` 是用户侧看到的名字（HTTP Content-Disposition）
        # * ``on_disk_name`` 多加一段随机 hex，避免同一秒内的并发下载
        #   在磁盘上互相覆盖；用户看不到这一段，因为 registry 用前者作
        #   为下载文件名。
        filename = build_filename(
            tenant_label=tenant_label, fmt=fmt, now=now_utc
        )
        on_disk_name = build_filename(
            tenant_label=tenant_label,
            fmt=fmt,
            now=now_utc,
            suffix=secrets.token_hex(4),
        )
        target_path = (config.download_dir / on_disk_name).resolve()

        # 纵深防御：目标路径必须在 download_dir 之内（防 symlink 逃逸）。
        download_root = config.download_dir.resolve()
        try:
            target_path.relative_to(download_root)
        except ValueError as e:  # pragma: no cover — symlink 异常时才触发
            raise RuntimeError(
                f"Refusing to write outside LOG_DOWNLOAD_DIR: {target_path}"
            ) from e

        result = write_download(
            all_entries,
            target_path=target_path,
            fmt=fmt,
            timezone=config.timezone,
        )

        # 拼用户侧可见的"取件方式"：HTTP 模式用 URL，stdio 用绝对路径。
        delivery: str
        if registry is not None:
            entry = await registry.register(
                path=result.path,
                fmt=fmt,
                download_filename=filename,
            )
            url = _make_download_url(ctx, config, entry.token)
            if url is None:
                # 走到这里说明 HTTP 模式但无法推断 base URL，正常配置下
                # 几乎不会发生。退化方案：把 token 显式给出来，便于
                # 运维侧排查。
                delivery = (
                    f"**Token:** `{entry.token}` — set "
                    "`LOG_DOWNLOAD_BASE_URL` so the server can render "
                    "a complete URL."
                )
            else:
                delivery = (
                    f"**Download URL:** {url}\n"
                    f"_Link expires in {registry.ttl_seconds // 60} "
                    "minutes._"
                )
        else:
            delivery = f"**Path:** `{str(result.path)}`"

        size_kb = result.byte_size / 1024
        truncated_note = ""
        if limit_reached:
            truncated_note = (
                "\n> ⚠️ Result reached the per-tenant limit "
                f"({effective_limit}); some entries may have been "
                "truncated. Narrow the time range and download again "
                "to capture everything.\n"
            )

        report = (
            f"# Log Download Ready\n\n"
            f"**Backend:** `{backend.name}`\n"
            f"**Query:** `{query}`\n"
            f"**Time Range:** "
            f"`{format_in_tz(start_dt, config.timezone)}` to "
            f"`{format_in_tz(end_dt, config.timezone)}`\n"
            f"**Tenants Queried:** `{', '.join(tenants)}`\n"
            f"**Successful Tenants:** `{', '.join(successful) or '-'}`\n"
            f"**Instance:** `{instance or '*all healthy*'}`\n"
            f"**Format:** `{fmt}`\n"
            f"**Entries:** {result.entry_count}\n"
            f"**Size:** {size_kb:.1f} KB\n"
            f"{delivery}\n"
            f"{truncated_note}"
            f"{_format_failures(results)}"
            f"{_format_warnings(results)}"
        )
        return report

    logger.info("All tools registered", tool_count=5)


def _make_download_url(
    ctx: Optional[Context], config: LogConfig, token: str
) -> Optional[str]:
    """渲染下载路由的绝对 URL。

    下载路由挂在 **与 MCP 主端点相同的路径前缀** 下（默认
    ``/mcp/download/<token>``），任何把 ``/mcp`` 转发到本服务的反向
    代理规则都会自动覆盖下载链接。

    选择 base URL 的优先级：

    1. 配置项 ``LOG_DOWNLOAD_BASE_URL``（推荐在反向代理改写 Host 的
       场景下显式配置）；下载路径会自动拼接到末尾。
    2. 当前请求的 scheme + Host 请求头（仅 HTTP 传输有该信息）。
    3. ``None``——交由调用方退化为直接展示 token / 路径。
    """
    path = _download_url_path.rstrip("/")
    if config.download_base_url:
        return f"{config.download_base_url}{path}/{token}"

    request = None
    if ctx is not None:
        try:
            request = ctx.request_context.request
        except Exception:
            request = None
    if request is None:
        return None

    try:
        # 优先读 X-Forwarded-Proto / X-Forwarded-Host：在 TLS 终止
        # 反向代理后面，这样生成的链接才会是 ``https://...`` 而不是
        # ``http://...``（混合内容 / 重定向问题的高发场景）。这两个
        # 请求头不存在时，退化到直接读请求自身的 scheme + Host。
        fwd_proto = request.headers.get("x-forwarded-proto")
        scheme = (
            fwd_proto.split(",")[0].strip()
            if fwd_proto
            else request.url.scheme  # type: ignore[attr-defined]
        )
        fwd_host = request.headers.get("x-forwarded-host")
        host = (
            fwd_host.split(",")[0].strip()
            if fwd_host
            else request.headers.get("host")
        )
    except Exception:
        return None
    if not host:
        return None
    return f"{scheme}://{host}{path}/{token}"


# ---------------------------------------------------------------------------
# get_labels / get_label_values 共用的列表辅助函数
# ---------------------------------------------------------------------------
async def _list_keys(
    *,
    label: Optional[str],
    start: Optional[str],
    end: Optional[str],
    heading: str,
    tenant: Optional[str] = None,
    instance: Optional[str] = None,
    ctx: Optional[Context] = None,
) -> str:
    backend, config = _require_state()

    start_dt: Optional[datetime] = None
    end_dt: Optional[datetime] = None
    if start or end:
        try:
            start_dt, end_dt = resolve_time_range(
                start, end, config.default_time_range_minutes
            )
        except ValidationError as e:
            raise RuntimeError(str(e)) from e

    tenants = _resolve_tenants(backend, config, tenant, ctx)
    logger.info(
        "Tool: list keys",
        kind="label_values" if label else "labels",
        tenants=tenants,
        label=label,
        start=start_dt.isoformat() if start_dt else None,
        end=end_dt.isoformat() if end_dt else None,
    )

    async def run_for_tenant(
        tenant: str,
        cluster_errors: Dict[str, str],
        cluster_warnings: Dict[str, str],
    ) -> List[str]:
        del cluster_warnings
        if label is None:
            return await backend.get_labels(
                tenant,
                start=start_dt,
                end=end_dt,
                instance=instance,
                cluster_errors=cluster_errors,
            )
        return await backend.get_label_values(
            tenant,
            label,
            start=start_dt,
            end=end_dt,
            instance=instance,
            cluster_errors=cluster_errors,
        )

    results = await _fan_out(tenants, run_for_tenant)
    successful = [r.tenant for r in results if r.ok]

    parts = [
        f"# {heading}\n",
        f"**Backend:** `{backend.name}`",
        f"**Tenants Queried:** `{', '.join(tenants)}`",
        f"**Successful Tenants:** `{', '.join(successful) or '-'}`",
        f"**Instance:** `{instance or '*all healthy*'}`",
    ]
    if start_dt and end_dt:
        parts.append(
            f"**Time Range:** `{format_in_tz(start_dt, config.timezone)}` to "
            f"`{format_in_tz(end_dt, config.timezone)}`"
        )
    parts.append("")

    unique: set[str] = set()
    for r in results:
        parts.append(f"## Tenant: `{r.tenant}`")
        if not r.ok:
            parts.append(f"_Error: {r.error}_\n")
            continue
        items = r.data or []
        unique.update(items)
        if not items:
            parts.append(
                "_No values found_\n" if label else "_No labels found_\n"
            )
        else:
            parts.append(f"Found {len(items)} item(s):\n")
            for i, name in enumerate(items, 1):
                parts.append(f"{i}. `{name}`")
            parts.append("")

    parts.append(f"**Total Unique:** {len(unique)}")
    cluster_errors_md = _format_failures(results)
    return "\n".join(parts) + "\n" + cluster_errors_md
