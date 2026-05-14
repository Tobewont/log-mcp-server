"""End-to-end tool tests against a stubbed backend."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import List, Optional
from unittest.mock import MagicMock

import pytest

from log_mcp_server.backends.base import LogBackend, LogEntry
from log_mcp_server.config import LogConfig
from log_mcp_server.tools.log_tools import initialize_tools, register_tools


# ---------------------------------------------------------------------------
# Mock FastMCP Context
# ---------------------------------------------------------------------------
class _MockHeaders:
    """Tiny stand-in for starlette.datastructures.Headers.

    Supports both single-value (``dict``) construction and multi-value
    (``list[tuple]``) construction so we can exercise the RFC 7230 case
    where the same header name appears more than once.
    """

    def __init__(self, headers):
        if headers is None:
            self._items: list[tuple[str, str]] = []
        elif isinstance(headers, dict):
            self._items = [(k.lower(), v) for k, v in headers.items()]
        else:
            self._items = [(k.lower(), v) for k, v in headers]

    def get(self, k, default=None):
        kl = k.lower()
        for name, value in self._items:
            if name == kl:
                return value
        return default

    def getlist(self, k):
        kl = k.lower()
        return [v for name, v in self._items if name == kl]


class _MockRequest:
    def __init__(self, headers=None):
        self.headers = _MockHeaders(headers)


class _MockRequestContext:
    def __init__(self, request):
        self.request = request


class _MockContext:
    """Lightweight stand-in for ``mcp.server.fastmcp.Context``."""

    def __init__(self, request=None):
        self.request_context = _MockRequestContext(request)


def _ctx_http(header_tenants: Optional[str] = None) -> _MockContext:
    """Simulate an HTTP request.

    ``header_tenants=None`` means "HTTP request, but no X-Allowed-Tenants
    header was sent" — log-query tools must refuse.
    """
    headers = {}
    if header_tenants is not None:
        headers["x-allowed-tenants"] = header_tenants
    return _MockContext(request=_MockRequest(headers))


def _ctx_stdio() -> _MockContext:
    """Simulate stdio mode: there is no Starlette Request, so the
    server falls back to the LOKI_CLIENT_TENANTS env / config field."""
    return _MockContext(request=None)


class StubBackend(LogBackend):
    """In-memory backend used to drive the tools layer."""

    name = "stub"

    def __init__(self, tenants: List[str], entries_by_tenant: dict | None = None):
        self._tenants = tenants
        self._entries = entries_by_tenant or {}
        self.fail_for: set[str] = set()
        self.partial_fail_for: set[str] = set()
        self.health_status = "healthy"

    @property
    def tenants(self) -> List[str]:
        return self._tenants

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def health_check(self):
        return {
            "backend": self.name,
            "status": self.health_status,
            "server_addr": "stub://nowhere",
            "current_time": datetime(2025, 1, 1, tzinfo=timezone.utc).isoformat(),
            "timezone": "UTC",
        }

    async def query_logs(
        self,
        query: str,
        tenant: str,
        start: datetime,
        end: datetime,
        limit: int,
        direction: str,
        instance=None,
        cluster_errors=None,
    ) -> List[LogEntry]:
        del instance
        if tenant in self.fail_for:
            raise RuntimeError(f"tenant {tenant} broken")
        if cluster_errors is not None and tenant in self.partial_fail_for:
            cluster_errors[f"sub-of-{tenant}"] = "simulated cluster failure"
        return list(self._entries.get(tenant, []))

    async def get_labels(
        self, tenant, start=None, end=None, instance=None, cluster_errors=None
    ):
        del instance
        if tenant in self.fail_for:
            raise RuntimeError(f"tenant {tenant} broken")
        return ["job", "level"] if tenant == "tenant-a" else ["host"]

    async def get_label_values(
        self,
        tenant,
        label,
        start=None,
        end=None,
        instance=None,
        cluster_errors=None,
    ):
        del instance
        if tenant in self.fail_for:
            raise RuntimeError(f"tenant {tenant} broken")
        return [f"{label}-1", f"{label}-2"]


def _capture_tools():
    """Capture FastMCP-registered tools into a dict for direct invocation."""
    tools: dict = {}

    fake_mcp = MagicMock()

    def fake_tool_decorator(*dargs, **dkwargs):
        def wrap(fn):
            tools[fn.__name__] = fn
            return fn

        return wrap

    fake_mcp.tool = MagicMock(side_effect=fake_tool_decorator)
    return fake_mcp, tools


def _make_setup(
    backend: StubBackend,
    config: Optional[LogConfig] = None,
    *,
    with_client_filter: bool = True,
):
    """Build a tools-under-test harness.

    Log-query tools now require the client to declare a tenant subset.
    The legacy tests assume "no filter == query everything", so by
    default this fixture seeds ``client_tenants`` with the full backend
    tenant list, restoring the old behaviour.  Pass
    ``with_client_filter=False`` for tests that intentionally exercise
    the unset-filter refusal path.
    """
    if config is None:
        kwargs = dict(
            addr="http://stub:3100",
            tenants="|".join(backend.tenants) or "tenant-a|tenant-b",
            timezone="UTC",
            default_limit=50,
            default_time_range_minutes=15,
        )
        if with_client_filter:
            kwargs["client_tenants"] = ",".join(backend.tenants)
        cfg = LogConfig(**kwargs)
    else:
        cfg = config
    fake_mcp, tools = _capture_tools()
    initialize_tools(backend, cfg)
    register_tools(fake_mcp)
    return tools, cfg


@pytest.mark.asyncio
async def test_health_check_tool():
    backend = StubBackend(["tenant-a"])
    tools, _ = _make_setup(backend)
    out = await tools["health_check"](ctx=_ctx_stdio())
    assert "Healthy" in out
    assert "stub" in out


@pytest.mark.asyncio
async def test_query_logs_aggregates_tenants():
    e1 = LogEntry(datetime(2025, 1, 1, tzinfo=timezone.utc), {"job": "a"}, "L1")
    e2 = LogEntry(datetime(2025, 1, 1, tzinfo=timezone.utc), {"job": "b"}, "L2")
    backend = StubBackend(
        ["tenant-a", "tenant-b"],
        entries_by_tenant={"tenant-a": [e1], "tenant-b": [e2]},
    )
    tools, _ = _make_setup(backend)

    out = await tools["query_logs"](query='{job="a"}', ctx=_ctx_stdio())
    assert "Total Entries:** 2" in out
    assert "L1" in out and "L2" in out
    # No invalid time format like "+00:00Z"
    assert "+00:00Z" not in out
    assert not re.search(r"\+\d{2}:\d{2}Z", out)


@pytest.mark.asyncio
async def test_query_logs_partial_failure_surfaces_error():
    e1 = LogEntry(datetime(2025, 1, 1, tzinfo=timezone.utc), {"job": "a"}, "L1")
    backend = StubBackend(
        ["tenant-a", "tenant-b"], entries_by_tenant={"tenant-a": [e1]}
    )
    backend.fail_for = {"tenant-b"}
    tools, _ = _make_setup(backend)

    out = await tools["query_logs"](query='{a="b"}', ctx=_ctx_stdio())
    assert "Total Entries:** 1" in out
    assert "Errors:" in out
    assert "tenant-b" in out


@pytest.mark.asyncio
async def test_query_logs_all_tenants_fail_distinguished_from_no_data():
    backend = StubBackend(["tenant-a"], entries_by_tenant={})
    backend.fail_for = {"tenant-a"}
    tools, _ = _make_setup(backend)

    out = await tools["query_logs"](query='{a="b"}', ctx=_ctx_stdio())
    assert "All tenant queries failed" in out


@pytest.mark.asyncio
async def test_query_logs_invalid_direction_rejected():
    backend = StubBackend(["tenant-a"])
    tools, _ = _make_setup(backend)
    with pytest.raises(RuntimeError, match="Invalid direction"):
        await tools["query_logs"](
            query='{a="b"}', direction="up", ctx=_ctx_stdio()
        )


@pytest.mark.asyncio
async def test_query_logs_cluster_errors_surfaced():
    e1 = LogEntry(datetime(2025, 1, 1, tzinfo=timezone.utc), {"job": "a"}, "L1")
    backend = StubBackend(["tenant-a"], entries_by_tenant={"tenant-a": [e1]})
    backend.partial_fail_for = {"tenant-a"}
    tools, _ = _make_setup(backend)

    out = await tools["query_logs"](query='{a="b"}', ctx=_ctx_stdio())
    assert "L1" in out
    assert "Errors:" in out
    assert "sub-of-tenant-a" in out
    assert "simulated cluster failure" in out


@pytest.mark.asyncio
async def test_query_logs_limit_above_max_rejected():
    backend = StubBackend(["tenant-a"])
    tools, _ = _make_setup(backend)
    with pytest.raises(RuntimeError, match="exceeds maximum"):
        await tools["query_logs"](
            query='{a="b"}', limit=999_999, ctx=_ctx_stdio()
        )


@pytest.mark.asyncio
async def test_query_logs_limit_zero_rejected():
    backend = StubBackend(["tenant-a"])
    tools, _ = _make_setup(backend)
    with pytest.raises(RuntimeError, match="positive"):
        await tools["query_logs"](query='{a="b"}', limit=0, ctx=_ctx_stdio())


@pytest.mark.asyncio
async def test_get_labels_groups_by_tenant():
    backend = StubBackend(["tenant-a", "tenant-b"])
    tools, _ = _make_setup(backend)
    out = await tools["get_labels"](ctx=_ctx_stdio())
    assert "tenant-a" in out and "tenant-b" in out
    assert "job" in out and "host" in out


@pytest.mark.asyncio
async def test_get_label_values_requires_label():
    backend = StubBackend(["tenant-a"])
    tools, _ = _make_setup(backend)
    with pytest.raises(RuntimeError):
        await tools["get_label_values"](label="", ctx=_ctx_stdio())


@pytest.mark.asyncio
async def test_get_label_values_with_time():
    backend = StubBackend(["tenant-a"])
    tools, _ = _make_setup(backend)
    out = await tools["get_label_values"](
        label="env",
        start="2025-01-01T00:00:00Z",
        end="2025-01-01T01:00:00Z",
        ctx=_ctx_stdio(),
    )
    assert "env-1" in out
    assert "Time Range:" in out


# ---------------------------------------------------------------------------
# tenant parameter tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_query_logs_single_tenant():
    """When tenant is specified, only that tenant is queried."""
    e1 = LogEntry(datetime(2025, 1, 1, tzinfo=timezone.utc), {"job": "a"}, "L1")
    e2 = LogEntry(datetime(2025, 1, 1, tzinfo=timezone.utc), {"job": "b"}, "L2")
    backend = StubBackend(
        ["tenant-a", "tenant-b"],
        entries_by_tenant={"tenant-a": [e1], "tenant-b": [e2]},
    )
    tools, _ = _make_setup(backend)

    out = await tools["query_logs"](
        query='{job="a"}', tenant="tenant-a", ctx=_ctx_stdio()
    )
    assert "Total Entries:** 1" in out
    assert "L1" in out
    assert "L2" not in out
    assert "Tenants Queried:** `tenant-a`" in out


@pytest.mark.asyncio
async def test_query_logs_unknown_tenant_rejected():
    backend = StubBackend(["tenant-a", "tenant-b"])
    tools, _ = _make_setup(backend)
    with pytest.raises(RuntimeError, match="Unknown tenant"):
        await tools["query_logs"](
            query='{a="b"}', tenant="no-such-tenant", ctx=_ctx_stdio()
        )


@pytest.mark.asyncio
async def test_get_labels_single_tenant():
    backend = StubBackend(["tenant-a", "tenant-b"])
    tools, _ = _make_setup(backend)
    out = await tools["get_labels"](tenant="tenant-a", ctx=_ctx_stdio())
    assert "tenant-a" in out
    assert "tenant-b" not in out
    assert "job" in out


@pytest.mark.asyncio
async def test_get_label_values_single_tenant():
    backend = StubBackend(["tenant-a", "tenant-b"])
    tools, _ = _make_setup(backend)
    out = await tools["get_label_values"](
        label="env", tenant="tenant-a", ctx=_ctx_stdio()
    )
    assert "tenant-a" in out
    assert "tenant-b" not in out
    assert "env-1" in out


# ---------------------------------------------------------------------------
# instance parameter tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_query_logs_instance_passed_through_to_backend():
    """The tool should pass instance to the backend verbatim."""
    captured: dict = {}

    class CapturingBackend(StubBackend):
        async def query_logs(self, **kw):  # type: ignore[override]
            captured.update(kw)
            return []

        async def get_labels(self, *a, **kw):  # type: ignore[override]
            return []

        async def get_label_values(self, *a, **kw):  # type: ignore[override]
            return []

    backend = CapturingBackend(["tenant-a"])
    tools, _ = _make_setup(backend)
    await tools["query_logs"](
        query='{a="b"}',
        tenant="tenant-a",
        instance="loki-bj:3100",
        ctx=_ctx_stdio(),
    )
    assert captured["instance"] == "loki-bj:3100"


@pytest.mark.asyncio
async def test_query_logs_instance_appears_in_header():
    backend = StubBackend(["tenant-a"])
    tools, _ = _make_setup(backend)
    out = await tools["query_logs"](
        query='{a="b"}',
        tenant="tenant-a",
        instance="loki-sh:3100",
        ctx=_ctx_stdio(),
    )
    assert "**Instance:** `loki-sh:3100`" in out


@pytest.mark.asyncio
async def test_query_logs_default_instance_marker():
    backend = StubBackend(["tenant-a"])
    tools, _ = _make_setup(backend)
    out = await tools["query_logs"](
        query='{a="b"}', tenant="tenant-a", ctx=_ctx_stdio()
    )
    assert "**Instance:** `*all healthy*`" in out


# ---------------------------------------------------------------------------
# Client-side tenant filter
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_http_header_restricts_fanout():
    """X-Allowed-Tenants narrows the default fan-out scope (HTTP)."""
    e1 = LogEntry(datetime(2025, 1, 1, tzinfo=timezone.utc), {"job": "a"}, "L1")
    e2 = LogEntry(datetime(2025, 1, 1, tzinfo=timezone.utc), {"job": "b"}, "L2")
    backend = StubBackend(
        ["tenant-a", "tenant-b"],
        entries_by_tenant={"tenant-a": [e1], "tenant-b": [e2]},
    )
    tools, _ = _make_setup(backend, with_client_filter=False)

    out = await tools["query_logs"](
        query='{a="b"}', ctx=_ctx_http("tenant-a")
    )
    assert "L1" in out
    assert "L2" not in out
    assert "Tenants Queried:** `tenant-a`" in out


@pytest.mark.asyncio
async def test_http_header_rejects_explicit_forbidden_tenant():
    """An explicit ``tenant=`` outside the header subset is rejected."""
    backend = StubBackend(["tenant-a", "tenant-b"])
    tools, _ = _make_setup(backend, with_client_filter=False)
    with pytest.raises(RuntimeError, match="Forbidden tenant"):
        await tools["query_logs"](
            query='{a="b"}', tenant="tenant-b", ctx=_ctx_http("tenant-a")
        )


@pytest.mark.asyncio
async def test_http_header_empty_intersection_errors_clearly():
    """Allowed list disjoint from server tenants yields a clear error."""
    backend = StubBackend(["tenant-a", "tenant-b"])
    tools, _ = _make_setup(backend, with_client_filter=False)
    with pytest.raises(RuntimeError, match="No tenants are accessible"):
        await tools["query_logs"](
            query='{a="b"}', ctx=_ctx_http("tenant-c,tenant-d")
        )


@pytest.mark.asyncio
async def test_stdio_env_restricts_fanout(monkeypatch):
    """Stdio mode: LOKI_CLIENT_TENANTS env restricts visible tenants."""
    monkeypatch.setenv("LOKI_CLIENT_TENANTS", "tenant-a")
    cfg = LogConfig(
        addr="http://stub:3100",
        tenants="tenant-a|tenant-b",
        timezone="UTC",
        default_limit=50,
        default_time_range_minutes=15,
    )
    e1 = LogEntry(datetime(2025, 1, 1, tzinfo=timezone.utc), {"job": "a"}, "L1")
    e2 = LogEntry(datetime(2025, 1, 1, tzinfo=timezone.utc), {"job": "b"}, "L2")
    backend = StubBackend(
        ["tenant-a", "tenant-b"],
        entries_by_tenant={"tenant-a": [e1], "tenant-b": [e2]},
    )
    tools, _ = _make_setup(backend, cfg)

    out = await tools["query_logs"](query='{a="b"}', ctx=_ctx_stdio())
    assert "L1" in out and "L2" not in out
    assert "Tenants Queried:** `tenant-a`" in out


@pytest.mark.asyncio
async def test_http_header_does_not_fall_back_to_env(monkeypatch):
    """In HTTP mode the env var must NOT be consulted; only the header."""
    monkeypatch.setenv("LOKI_CLIENT_TENANTS", "tenant-a")
    cfg = LogConfig(
        addr="http://stub:3100",
        tenants="tenant-a|tenant-b",
        timezone="UTC",
        default_limit=50,
        default_time_range_minutes=15,
    )
    backend = StubBackend(["tenant-a", "tenant-b"])
    tools, _ = _make_setup(backend, cfg)

    # HTTP request with NO header — must refuse even though env is set.
    with pytest.raises(RuntimeError, match="No tenant scope is configured"):
        await tools["query_logs"](query='{a="b"}', ctx=_ctx_http(None))


@pytest.mark.asyncio
async def test_health_check_reports_client_filter_via_header():
    backend = StubBackend(["tenant-a", "tenant-b"])
    tools, _ = _make_setup(backend, with_client_filter=False)
    out = await tools["health_check"](ctx=_ctx_http("tenant-a"))
    assert "Allowed Tenants (this session):** tenant-a" in out
    assert "Filter Source:** request header X-Allowed-Tenants" in out
    assert "Server Tenants:** tenant-a, tenant-b" in out


@pytest.mark.asyncio
async def test_health_check_reports_client_filter_via_env(monkeypatch):
    monkeypatch.setenv("LOKI_CLIENT_TENANTS", "tenant-a")
    cfg = LogConfig(
        addr="http://stub:3100",
        tenants="tenant-a|tenant-b",
        timezone="UTC",
        default_limit=50,
        default_time_range_minutes=15,
    )
    backend = StubBackend(["tenant-a", "tenant-b"])
    tools, _ = _make_setup(backend, cfg)
    out = await tools["health_check"](ctx=_ctx_stdio())
    assert "Allowed Tenants (this session):** tenant-a" in out
    assert "Filter Source:** env LOKI_CLIENT_TENANTS" in out


@pytest.mark.asyncio
async def test_health_check_reports_unset_filter_stdio():
    backend = StubBackend(["tenant-a", "tenant-b"])
    tools, _ = _make_setup(backend, with_client_filter=False)
    out = await tools["health_check"](ctx=_ctx_stdio())
    assert "Filter Source:** (unset" in out
    assert "LOKI_CLIENT_TENANTS" in out
    assert "Allowed Tenants (this session):** (unset" in out


@pytest.mark.asyncio
async def test_health_check_reports_unset_filter_http():
    """HTTP request without the header — must say so explicitly."""
    backend = StubBackend(["tenant-a", "tenant-b"])
    tools, _ = _make_setup(backend, with_client_filter=False)
    out = await tools["health_check"](ctx=_ctx_http(None))
    assert "Filter Source:** (unset" in out
    assert "X-Allowed-Tenants" in out


# ---------------------------------------------------------------------------
# Unset-filter refusal
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_query_logs_refuses_when_filter_unset_stdio():
    backend = StubBackend(["tenant-a", "tenant-b"])
    tools, _ = _make_setup(backend, with_client_filter=False)
    with pytest.raises(RuntimeError, match="No tenant scope is configured"):
        await tools["query_logs"](query='{a="b"}', ctx=_ctx_stdio())


@pytest.mark.asyncio
async def test_query_logs_refuses_when_filter_unset_http():
    backend = StubBackend(["tenant-a", "tenant-b"])
    tools, _ = _make_setup(backend, with_client_filter=False)
    with pytest.raises(RuntimeError, match="No tenant scope is configured"):
        await tools["query_logs"](query='{a="b"}', ctx=_ctx_http(None))


@pytest.mark.asyncio
async def test_query_logs_refuses_even_with_explicit_tenant_when_unset():
    """Even providing tenant= must not bypass the client-filter requirement."""
    backend = StubBackend(["tenant-a", "tenant-b"])
    tools, _ = _make_setup(backend, with_client_filter=False)
    with pytest.raises(RuntimeError, match="No tenant scope is configured"):
        await tools["query_logs"](
            query='{a="b"}', tenant="tenant-a", ctx=_ctx_stdio()
        )


@pytest.mark.asyncio
async def test_get_labels_refuses_when_filter_unset():
    backend = StubBackend(["tenant-a", "tenant-b"])
    tools, _ = _make_setup(backend, with_client_filter=False)
    with pytest.raises(RuntimeError, match="No tenant scope is configured"):
        await tools["get_labels"](ctx=_ctx_stdio())


@pytest.mark.asyncio
async def test_get_label_values_refuses_when_filter_unset():
    backend = StubBackend(["tenant-a", "tenant-b"])
    tools, _ = _make_setup(backend, with_client_filter=False)
    with pytest.raises(RuntimeError, match="No tenant scope is configured"):
        await tools["get_label_values"](label="env", ctx=_ctx_stdio())


@pytest.mark.asyncio
async def test_health_check_does_not_refuse_when_filter_unset():
    """health_check is a diagnostic and must still run when filter is unset."""
    backend = StubBackend(["tenant-a", "tenant-b"])
    tools, _ = _make_setup(backend, with_client_filter=False)
    out = await tools["health_check"](ctx=_ctx_stdio())
    assert "Healthy" in out
    assert "(unset" in out


# ---------------------------------------------------------------------------
# Header parsing edge cases (HTTP path)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw", ["", "   ", ",,,", " , ,,, "])
@pytest.mark.asyncio
async def test_http_blank_or_punctuation_only_header_is_unset(raw):
    """Empty / whitespace / punctuation-only header == 'no scope'."""
    backend = StubBackend(["tenant-a", "tenant-b"])
    tools, _ = _make_setup(backend, with_client_filter=False)
    with pytest.raises(RuntimeError, match="No tenant scope is configured"):
        await tools["query_logs"](query='{a="b"}', ctx=_ctx_http(raw))


@pytest.mark.asyncio
async def test_http_multiple_headers_are_merged_per_rfc7230():
    """RFC 7230 §3.2.2: same-name headers merge by comma-joining."""
    backend = StubBackend(["tenant-a", "tenant-b"])
    tools, _ = _make_setup(backend, with_client_filter=False)
    multi = _MockContext(
        request=_MockRequest(
            [("x-allowed-tenants", "tenant-a"), ("x-allowed-tenants", "tenant-b")]
        )
    )
    out = await tools["health_check"](ctx=multi)
    assert "Allowed Tenants (this session):** tenant-a, tenant-b" in out


@pytest.mark.asyncio
async def test_http_header_case_insensitive():
    backend = StubBackend(["tenant-a", "tenant-b"])
    tools, _ = _make_setup(backend, with_client_filter=False)
    upper = _MockContext(request=_MockRequest({"X-Allowed-Tenants": "tenant-a"}))
    out = await tools["health_check"](ctx=upper)
    assert "Allowed Tenants (this session):** tenant-a" in out


@pytest.mark.asyncio
async def test_http_header_with_extra_whitespace_is_normalised():
    """`'  tenant-a  ,  tenant-b '` should parse to two tenants."""
    backend = StubBackend(["tenant-a", "tenant-b"])
    tools, _ = _make_setup(backend, with_client_filter=False)
    out = await tools["health_check"](
        ctx=_ctx_http("  tenant-a  ,  tenant-b ,, ,")
    )
    assert "Allowed Tenants (this session):** tenant-a, tenant-b" in out
