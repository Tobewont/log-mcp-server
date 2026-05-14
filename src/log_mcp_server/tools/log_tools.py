"""Backend-agnostic FastMCP tools.

These tools delegate to the active ``LogBackend``. They are the only
externally-visible MCP surface, so keeping them lean and consistent is
important for AI ergonomics.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Dict, List, Optional

import structlog
from mcp.server.fastmcp import Context, FastMCP

from ..auth_context import parse_tenant_list
from ..backends.base import LogBackend, LogEntry, TenantQueryResult
from ..config import LogConfig
from ..utils.errors import LogMCPError, ValidationError
from ..utils.time_utils import format_in_tz, resolve_time_range

logger = structlog.get_logger(__name__)


_PER_TENANT_TIMEOUT_SECONDS = 60.0

_backend: Optional[LogBackend] = None
_config: Optional[LogConfig] = None


def initialize_tools(backend: LogBackend, config: LogConfig) -> None:
    """Inject the active backend / config (called once at startup)."""
    global _backend, _config
    _backend = backend
    _config = config
    logger.info(
        "Tools initialised",
        backend=backend.name,
        tenants=backend.tenants,
    )


def _require_state() -> tuple[LogBackend, LogConfig]:
    if _backend is None or _config is None:
        raise RuntimeError("Tools not initialised; call initialize_tools() first")
    return _backend, _config


# ---------------------------------------------------------------------------
# Multi-tenant fan-out helpers
# ---------------------------------------------------------------------------
def _client_filter_for(
    ctx: Optional[Context], config: LogConfig
) -> tuple[Optional[List[str]], str]:
    """Read the client-declared tenant subset for this tool call.

    Returns ``(subset, source)``:

    * In HTTP transports (streamable-http / SSE) the streamable_http
      server passes the underlying Starlette ``Request`` to every tool
      via ``ServerMessageMetadata.request_context``; we read the
      ``X-Allowed-Tenants`` header from that.  This works across the
      task boundary that an ASGI middleware + contextvar approach
      cannot reach.
    * In stdio there is no Starlette ``Request``; we fall back to the
      ``LOKI_CLIENT_TENANTS`` env var (parsed in :class:`LogConfig`).

    A ``None`` subset means "the client has not declared a scope" and
    the log-query tools refuse to run.
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
            # RFC 7230 §3.2.2: multiple headers with the same name are
            # equivalent to a single one with the values comma-joined.
            # Starlette's ``.get()`` only returns the first occurrence,
            # so use ``getlist`` and join ourselves to stay correct.
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
    """Return the tenant list visible to *this* request / process.

    The client-declared subset (HTTP header for HTTP transports;
    ``LOKI_CLIENT_TENANTS`` env for stdio) is intersected with
    ``backend.tenants`` so a misconfigured client cannot widen the scope
    beyond what the server permits.

    Returns ``None`` when **no** client filter has been declared — log-
    query tools must refuse in that case so the operator is forced to
    state which tenants they intend to look at.  ``health_check`` is
    intentionally exempt (it is a diagnostic, not a log query).
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
    """Return the list of tenants to query.

    Refuses (raises ``RuntimeError``) if the client has not declared a
    tenant subset — see :func:`_effective_tenants`.  Otherwise validates
    ``tenant`` (when provided) against the effective list and returns
    either a single-element list or the full effective list.
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
    """Run one coroutine per tenant in parallel with a per-tenant timeout.

    ``coro_factory(tenant, cluster_errors)`` is invoked for each tenant
    with a fresh ``cluster_errors`` dict, so multi-cluster fan-out
    backends can record per-cluster failures without cross-tenant
    contention.
    """

    async def _wrap(tenant: str) -> TenantQueryResult:
        cluster_errors: Dict[str, str] = {}
        try:
            data = await asyncio.wait_for(
                coro_factory(tenant, cluster_errors),
                timeout=_PER_TENANT_TIMEOUT_SECONDS,
            )
            return TenantQueryResult(
                tenant=tenant, data=data, cluster_errors=cluster_errors
            )
        except asyncio.TimeoutError:
            return TenantQueryResult(
                tenant=tenant,
                error=f"timeout after {_PER_TENANT_TIMEOUT_SECONDS:.0f}s",
                cluster_errors=cluster_errors,
            )
        except LogMCPError as e:
            return TenantQueryResult(
                tenant=tenant,
                error=f"{type(e).__name__}: {e}",
                cluster_errors=cluster_errors,
            )
        except Exception as e:  # noqa: BLE001
            return TenantQueryResult(
                tenant=tenant,
                error=f"{type(e).__name__}: {e}",
                cluster_errors=cluster_errors,
            )

    return await asyncio.gather(*[_wrap(t) for t in tenants])


# ---------------------------------------------------------------------------
# Formatters
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
# Tool registration
# ---------------------------------------------------------------------------
def register_tools(mcp: FastMCP) -> None:
    """Register all tools on the given FastMCP instance."""

    # ----- health_check -------------------------------------------------
    @mcp.tool()
    async def health_check(ctx: Context) -> str:
        """Check log backend health and report current time.

        Returns a Markdown-formatted health report containing the
        aggregated backend status, per-cluster status (when multiple
        clusters are configured), the current time formatted in the
        configured timezone (``LOG_TIMEZONE``), and the active client
        tenant filter for this session (Allowed Tenants / Filter Source).
        Always call this *first* — if Filter Source shows ``(unset ...)``
        the log-query tools will refuse and you must instruct the user
        to add ``X-Allowed-Tenants`` (HTTP) or ``LOKI_CLIENT_TENANTS``
        (stdio) to their MCP client config.
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
        """Query logs from the specified tenant (or all tenants if omitted).

        Unhealthy clusters are automatically skipped (health is cached).

        **Recommended workflow (multi-tenant):**
        1. ``get_labels()`` — discover available label names per tenant.
        2. ``get_label_values(label="<relevant_label>")`` — find which
           tenant owns the target value.
        3. ``query_logs(tenant="<id>", query='{<label>="<value>"}')``
           — query only that tenant for fast, precise results.

        Args:
            query: LogQL log selector, e.g. ``{job="nginx"} |= "error"``.
                Metric expressions (rate, count_over_time, ...) are not
                supported.
            start: Inclusive start (RFC3339 / ISO 8601).  Defaults to
                ``end - LOG_DEFAULT_TIME_RANGE_MINUTES``.
            end: Exclusive end (RFC3339 / ISO 8601).  Defaults to now.
            limit: Max entries **per tenant**.  Omit to use the server-side
                default (LOG_DEFAULT_LIMIT). Do NOT specify a value unless the
                user explicitly requests a specific number.  Must not exceed
                ``LOG_MAX_LIMIT``.
            direction: ``"backward"`` (newest first, default) or
                ``"forward"``.
            tenant: Tenant ID.  When specified only this tenant is
                queried.  When omitted, **all client-allowed tenants**
                are queried in parallel.  The client allowed-list **must
                be configured** by the MCP client (``X-Allowed-Tenants``
                header for HTTP transports, ``LOKI_CLIENT_TENANTS`` env
                for stdio); without it this tool refuses to run.  Run
                ``health_check`` to see the effective list.
            instance: Loki cluster id (e.g. ``host:port`` or hostname,
                as shown by ``health_check``).  When specified, the query
                runs against this single cluster only — bypassing fan-out
                even in multi-Loki deployments.  Use this when the user
                explicitly tells you which Loki to query.

        Returns:
            Markdown report with entries, plus any errors at the bottom.
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
            t: str, cluster_errors: Dict[str, str]
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
            return header + _format_failures(results) + "\nAll tenant queries failed.\n"
        if not all_entries:
            tail = _format_failures(results)
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
        return header + "\n" + body + _format_failures(results)

    # ----- get_labels ---------------------------------------------------
    @mcp.tool()
    async def get_labels(
        ctx: Context,
        start: Optional[str] = None,
        end: Optional[str] = None,
        tenant: Optional[str] = None,
        instance: Optional[str] = None,
    ) -> str:
        """List label names from the specified tenant (or all tenants).

        Unhealthy clusters are automatically skipped.
        Use this as the first step to discover which tenants have the
        labels you need before calling ``query_logs``.

        Args:
            start: Optional time-range start (RFC3339).  When provided
                together with ``end``, only labels appearing within that
                window are returned.  Reduces response size on large
                deployments.
            end: Optional time-range end (RFC3339).
            tenant: Tenant ID to query.  When omitted, all
                **client-allowed** tenants are queried.  The client
                allowed-list must be configured by the MCP client (see
                ``query_logs`` docstring for details); without it this
                tool refuses to run.

            instance: Loki cluster id to restrict to a single Loki when
                multiple are configured.  Omit for default fan-out.

        Returns:
            Markdown report grouped by tenant.
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
        """List values of a specific label from the specified tenant (or all).

        Unhealthy clusters are automatically skipped.
        Use this to confirm which tenant owns specific label values
        before calling ``query_logs``.

        Args:
            label: Label name (required).
            start: Optional time-range start (RFC3339).
            end: Optional time-range end (RFC3339).
            tenant: Tenant ID to query.  When omitted, all
                **client-allowed** tenants are queried.  The client
                allowed-list must be configured by the MCP client (see
                ``query_logs`` docstring for details); without it this
                tool refuses to run.

            instance: Loki cluster id to restrict to a single Loki when
                multiple are configured.  Omit for default fan-out.

        Returns:
            Markdown report grouped by tenant.
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

    logger.info("All tools registered", tool_count=4)


# ---------------------------------------------------------------------------
# Shared list helper used by ``get_labels`` and ``get_label_values``
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
        tenant: str, cluster_errors: Dict[str, str]
    ) -> List[str]:
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
        f"**Configured Tenants:** `{', '.join(tenants)}`",
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
