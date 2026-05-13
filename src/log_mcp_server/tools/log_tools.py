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
from mcp.server.fastmcp import FastMCP

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
def _resolve_tenants(backend: LogBackend, tenant: Optional[str]) -> List[str]:
    """Return the list of tenants to query.

    When ``tenant`` is provided, validate it against configured tenants
    and return a single-element list. Otherwise return all tenants.
    """
    if tenant is None:
        return backend.tenants
    tenant = tenant.strip()
    if not tenant:
        raise RuntimeError("tenant cannot be empty")
    if tenant not in backend.tenants:
        raise RuntimeError(
            f"Unknown tenant {tenant!r}. "
            f"Configured tenants: {', '.join(backend.tenants)}"
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
    async def health_check() -> str:
        """Check log backend health and report current time.

        Returns a Markdown-formatted health report containing the
        aggregated backend status, per-cluster status (when multiple
        clusters are configured), and the current time formatted in the
        configured timezone (``LOG_TIMEZONE``).
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

        report = (
            f"# Log Backend Health Check\n\n"
            f"**Backend:** `{info.get('backend', backend.name)}`\n"
            f"**Status:** {status_text}\n"
            f"**Configured Clusters:** {len(clusters)}\n"
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
                queried.  When omitted all configured tenants are queried
                in parallel.
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

        tenants = _resolve_tenants(backend, tenant)

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
            tenant: Tenant ID to query. When omitted all configured
                tenants are queried in parallel.
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
        )

    # ----- get_label_values ---------------------------------------------
    @mcp.tool()
    async def get_label_values(
        label: str,
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
            tenant: Tenant ID to query. When omitted all configured
                tenants are queried in parallel.
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

    tenants = _resolve_tenants(backend, tenant)
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
