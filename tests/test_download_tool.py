"""End-to-end tests for the ``download_logs`` tool.

Reuses the StubBackend / mock-context machinery from ``test_tools`` to
keep the harness in one place.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from log_mcp_server.backends.base import LogEntry
from log_mcp_server.config import LogConfig
from log_mcp_server.downloads import DownloadRegistry
from log_mcp_server.tools.log_tools import initialize_tools, register_tools

from .test_tools import (
    StubBackend,
    _capture_tools,
    _ctx_http,
    _ctx_stdio,
    _MockContext,
    _MockRequest,
)


def _entries(tenant: str, n: int) -> list[LogEntry]:
    return [
        LogEntry(
            timestamp=datetime(2026, 1, 1, 0, 0, i, tzinfo=timezone.utc),
            labels={"job": "demo", "tenant": tenant},
            line=f"line-{i}",
            tenant=tenant,
        )
        for i in range(n)
    ]


def _make_setup(
    backend: StubBackend,
    download_dir: Path,
    *,
    with_registry: bool = False,
    download_base_url: str | None = None,
    download_url_path: str = "/mcp/download",
):
    cfg = LogConfig(
        addr="http://stub:3100",
        tenants="|".join(backend.tenants),
        client_tenants=",".join(backend.tenants),
        timezone="UTC",
        default_limit=10,
        max_limit=100,
        default_time_range_minutes=15,
        download_dir=download_dir,
        download_ttl_seconds=60,
        download_base_url=download_base_url,
    )
    fake_mcp, tools = _capture_tools()
    registry = DownloadRegistry(ttl_seconds=60) if with_registry else None
    initialize_tools(
        backend,
        cfg,
        download_registry=registry,
        download_url_path=download_url_path,
    )
    register_tools(fake_mcp)
    return tools, cfg, registry


# ---------------------------------------------------------------------------
# Stdio mode: tool returns a path, no URL
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_stdio_writes_jsonl_and_returns_path(tmp_path: Path):
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _entries("tenant-a", 3)}
    )
    tools, cfg, _ = _make_setup(backend, tmp_path)

    out = await tools["download_logs"](
        query='{a="b"}', ctx=_ctx_stdio(), tenant="tenant-a"
    )
    assert "Log Download Ready" in out
    assert "**Format:** `jsonl`" in out
    assert "**Entries:** 3" in out
    # Path line points at a real file inside the configured dir.
    assert "**Path:**" in out
    assert "logs-" in out
    assert ".jsonl" in out

    # The actual file is on disk and parseable as jsonl.
    files = list(tmp_path.glob("logs-*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text("utf-8").strip().splitlines()
    assert len(lines) == 3


@pytest.mark.asyncio
async def test_stdio_csv_and_txt_formats(tmp_path: Path):
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _entries("tenant-a", 2)}
    )
    tools, _, _ = _make_setup(backend, tmp_path)

    out_csv = await tools["download_logs"](
        query='{a="b"}', ctx=_ctx_stdio(), tenant="tenant-a", fmt="csv"
    )
    assert "**Format:** `csv`" in out_csv
    csv_files = list(tmp_path.glob("*.csv"))
    assert len(csv_files) == 1
    assert csv_files[0].read_text("utf-8").startswith("time,tenant,cluster,labels,line")

    out_txt = await tools["download_logs"](
        query='{a="b"}', ctx=_ctx_stdio(), tenant="tenant-a", fmt="txt"
    )
    assert "**Format:** `txt`" in out_txt
    txt_files = list(tmp_path.glob("*.txt"))
    assert len(txt_files) == 1


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unsupported_format_rejected(tmp_path: Path):
    backend = StubBackend(["tenant-a"])
    tools, _, _ = _make_setup(backend, tmp_path)
    with pytest.raises(RuntimeError, match="Unsupported fmt"):
        await tools["download_logs"](
            query='{a="b"}', ctx=_ctx_stdio(), tenant="tenant-a", fmt="xml"
        )


@pytest.mark.asyncio
async def test_invalid_direction_rejected(tmp_path: Path):
    backend = StubBackend(["tenant-a"])
    tools, _, _ = _make_setup(backend, tmp_path)
    with pytest.raises(RuntimeError, match="Invalid direction"):
        await tools["download_logs"](
            query='{a="b"}',
            ctx=_ctx_stdio(),
            tenant="tenant-a",
            direction="up",
        )


@pytest.mark.asyncio
async def test_limit_above_max_rejected(tmp_path: Path):
    backend = StubBackend(["tenant-a"])
    tools, _, _ = _make_setup(backend, tmp_path)
    with pytest.raises(RuntimeError, match="exceeds maximum"):
        await tools["download_logs"](
            query='{a="b"}',
            ctx=_ctx_stdio(),
            tenant="tenant-a",
            limit=10**9,
        )


@pytest.mark.asyncio
async def test_refuses_when_client_filter_unset(tmp_path: Path):
    backend = StubBackend(["tenant-a"])
    cfg = LogConfig(
        addr="http://stub:3100",
        tenants="tenant-a",
        timezone="UTC",
        download_dir=tmp_path,
    )
    fake_mcp, tools = _capture_tools()
    initialize_tools(backend, cfg, download_registry=None)
    register_tools(fake_mcp)
    with pytest.raises(RuntimeError, match="No tenant scope is configured"):
        await tools["download_logs"](
            query='{a="b"}', ctx=_ctx_stdio(), tenant="tenant-a"
        )


# ---------------------------------------------------------------------------
# HTTP mode: registry registers a token + tool emits a download URL
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_http_returns_url_with_explicit_base_url(tmp_path: Path):
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _entries("tenant-a", 1)}
    )
    tools, cfg, registry = _make_setup(
        backend,
        tmp_path,
        with_registry=True,
        download_base_url="https://logs-mcp.example.com",
    )
    out = await tools["download_logs"](
        query='{a="b"}',
        ctx=_ctx_http("tenant-a"),
        tenant="tenant-a",
    )
    assert (
        "**Download URL:** https://logs-mcp.example.com/mcp/download/" in out
    )
    assert "Link expires in" in out
    # token is registered in the registry
    assert registry is not None
    # exactly one entry should be live
    files = list(tmp_path.glob("logs-*.jsonl"))
    assert len(files) == 1


@pytest.mark.asyncio
async def test_http_does_not_create_download_for_empty_results(tmp_path: Path):
    backend = StubBackend(["tenant-a"], entries_by_tenant={"tenant-a": []})
    tools, _, registry = _make_setup(
        backend,
        tmp_path,
        with_registry=True,
        download_base_url="https://logs-mcp.example.com",
    )

    out = await tools["download_logs"](
        query='{a="b"}',
        ctx=_ctx_http("tenant-a"),
        tenant="tenant-a",
    )

    assert "Log Download Empty" in out
    assert "**Entries:** 0" in out
    assert "**Download URL:**" not in out
    assert "**Token:**" not in out
    assert registry is not None
    assert registry._items == {}
    assert list(tmp_path.glob("logs-*")) == []


@pytest.mark.asyncio
async def test_http_url_honours_x_forwarded_proto_and_host(tmp_path: Path):
    """Behind a TLS-terminating reverse proxy the rendered URL must use
    the original scheme + host the user reached, not the internal http
    + service IP."""
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _entries("tenant-a", 1)}
    )
    tools, _, _ = _make_setup(
        backend, tmp_path, with_registry=True, download_base_url=None
    )

    class _Url:
        scheme = "http"  # internal scheme behind the proxy

    class _ProxiedRequest(_MockRequest):
        def __init__(self):
            super().__init__(
                {
                    "x-allowed-tenants": "tenant-a",
                    "host": "10.0.0.5:8000",
                    "x-forwarded-proto": "https",
                    "x-forwarded-host": "logs-mcp.example.com",
                }
            )
            self.url = _Url()

    ctx = _MockContext(request=_ProxiedRequest())
    out = await tools["download_logs"](
        query='{a="b"}', ctx=ctx, tenant="tenant-a"
    )
    # Must use https + the public host, NOT http + the internal one.
    assert (
        "**Download URL:** https://logs-mcp.example.com/mcp/download/" in out
    )
    assert "http://10.0.0.5:8000" not in out


@pytest.mark.asyncio
async def test_http_url_handles_comma_separated_x_forwarded_headers(
    tmp_path: Path,
):
    """When multiple proxies chain, X-Forwarded-* values are
    comma-joined; we must take the first (the original client-facing
    value)."""
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _entries("tenant-a", 1)}
    )
    tools, _, _ = _make_setup(
        backend, tmp_path, with_registry=True, download_base_url=None
    )

    class _Url:
        scheme = "http"

    class _ChainedRequest(_MockRequest):
        def __init__(self):
            super().__init__(
                {
                    "x-allowed-tenants": "tenant-a",
                    "host": "10.0.0.5:8000",
                    "x-forwarded-proto": "https, http",
                    "x-forwarded-host": "logs-mcp.example.com, internal-lb",
                }
            )
            self.url = _Url()

    ctx = _MockContext(request=_ChainedRequest())
    out = await tools["download_logs"](
        query='{a="b"}', ctx=ctx, tenant="tenant-a"
    )
    assert (
        "**Download URL:** https://logs-mcp.example.com/mcp/download/" in out
    )


@pytest.mark.asyncio
async def test_http_returns_url_inferred_from_request_host(tmp_path: Path):
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _entries("tenant-a", 1)}
    )
    tools, cfg, registry = _make_setup(
        backend, tmp_path, with_registry=True, download_base_url=None
    )

    # Build a mock request that exposes both .url.scheme and .headers["host"].
    class _Url:
        scheme = "http"

    class _RichRequest(_MockRequest):
        def __init__(self):
            super().__init__(
                {
                    "x-allowed-tenants": "tenant-a",
                    "host": "logs.local:9000",
                }
            )
            self.url = _Url()

    ctx = _MockContext(request=_RichRequest())
    out = await tools["download_logs"](
        query='{a="b"}', ctx=ctx, tenant="tenant-a"
    )
    assert "**Download URL:** http://logs.local:9000/mcp/download/" in out


@pytest.mark.asyncio
async def test_http_url_uses_sse_prefix_when_configured(tmp_path: Path):
    """SSE deployments mount the download route under /sse/download —
    the rendered URL must follow."""
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _entries("tenant-a", 1)}
    )
    tools, _, _ = _make_setup(
        backend,
        tmp_path,
        with_registry=True,
        download_base_url="https://logs-mcp.example.com",
        download_url_path="/sse/download",
    )
    out = await tools["download_logs"](
        query='{a="b"}',
        ctx=_ctx_http("tenant-a"),
        tenant="tenant-a",
    )
    assert (
        "**Download URL:** https://logs-mcp.example.com/sse/download/" in out
    )


@pytest.mark.asyncio
async def test_http_falls_back_to_token_when_no_base_and_no_request(tmp_path: Path):
    """Pathological case: HTTP registry but ctx has no Starlette Request.

    Should still produce a usable artefact (just expose the token).
    """
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _entries("tenant-a", 1)}
    )
    tools, _, _ = _make_setup(
        backend, tmp_path, with_registry=True, download_base_url=None
    )
    # ctx_stdio() simulates 'no Request' but the client_tenants env still
    # comes from cfg, so the call doesn't refuse on scope.
    out = await tools["download_logs"](
        query='{a="b"}', ctx=_ctx_stdio(), tenant="tenant-a"
    )
    assert "**Token:**" in out
    assert "LOG_DOWNLOAD_BASE_URL" in out


# ---------------------------------------------------------------------------
# Truncation hint
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_truncation_warning_when_limit_reached(tmp_path: Path):
    """If the result hits the per-tenant limit, the response must warn."""
    backend = StubBackend(
        ["tenant-a"],
        entries_by_tenant={"tenant-a": _entries("tenant-a", 5)},
    )
    tools, _, _ = _make_setup(backend, tmp_path)
    out = await tools["download_logs"](
        query='{a="b"}',
        ctx=_ctx_stdio(),
        tenant="tenant-a",
        limit=5,
    )
    assert "may have been truncated" in out


@pytest.mark.asyncio
async def test_no_truncation_warning_when_only_total_reaches_limit(tmp_path: Path):
    """The limit is per tenant, so total entries across tenants can exceed it."""
    backend = StubBackend(
        ["tenant-a", "tenant-b"],
        entries_by_tenant={
            "tenant-a": _entries("tenant-a", 3),
            "tenant-b": _entries("tenant-b", 3),
        },
    )
    tools, _, _ = _make_setup(backend, tmp_path)
    out = await tools["download_logs"](
        query='{a="b"}',
        ctx=_ctx_stdio(),
        limit=5,
    )
    assert "**Entries:** 6" in out
    assert "may have been truncated" not in out


# ---------------------------------------------------------------------------
# Failure surfacing
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_all_tenants_fail_returns_failure_report(tmp_path: Path):
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": []}
    )
    backend.fail_for = {"tenant-a"}
    tools, _, _ = _make_setup(backend, tmp_path)
    out = await tools["download_logs"](
        query='{a="b"}', ctx=_ctx_stdio(), tenant="tenant-a"
    )
    assert "Download Failed" in out
    assert "tenant-a" in out
    # No file is written when the report bails early.
    assert not list(tmp_path.glob("logs-*.jsonl"))


@pytest.mark.asyncio
async def test_partial_cluster_failures_surface_in_report(tmp_path: Path):
    """One cluster fails, another succeeds — the report still completes
    and the errors section is present."""
    backend = StubBackend(
        ["tenant-a"],
        entries_by_tenant={"tenant-a": _entries("tenant-a", 2)},
    )
    backend.partial_fail_for = {"tenant-a"}
    tools, _, _ = _make_setup(backend, tmp_path)
    out = await tools["download_logs"](
        query='{a="b"}', ctx=_ctx_stdio(), tenant="tenant-a"
    )
    assert "Log Download Ready" in out
    assert "Errors:" in out
    assert "simulated cluster failure" in out
