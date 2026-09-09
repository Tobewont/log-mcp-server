"""End-to-end tool tests against a stubbed backend."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
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
        self.partial_warn_for: set[str] = set()
        self.health_status = "healthy"
        # ``{标签名: {标签值: 条数}}`` —— 模拟后端可下推的分组计数。
        self.grouped_by_label: dict = {}

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
        cluster_warnings=None,
    ) -> List[LogEntry]:
        del instance
        if tenant in self.fail_for:
            raise RuntimeError(f"tenant {tenant} broken")
        if cluster_errors is not None and tenant in self.partial_fail_for:
            cluster_errors[f"sub-of-{tenant}"] = "simulated cluster failure"
        if cluster_warnings is not None and tenant in self.partial_warn_for:
            cluster_warnings[f"sub-of-{tenant}"] = "simulated cluster warning"
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

    async def count_logs(
        self,
        query,
        tenant,
        start,
        end,
        instance=None,
        cluster_errors=None,
    ):
        del instance
        if tenant in self.fail_for:
            raise RuntimeError(f"tenant {tenant} broken")
        if cluster_errors is not None and tenant in self.partial_fail_for:
            cluster_errors[f"sub-of-{tenant}"] = "simulated cluster failure"
        return len(self._entries.get(tenant, []))

    async def count_logs_grouped(
        self,
        query,
        tenant,
        start,
        end,
        by_label,
        instance=None,
        cluster_errors=None,
    ):
        """默认返回 None（无法下推），测试可通过 ``grouped_by_label`` 打开。"""
        del query, start, end, instance, cluster_errors
        if tenant in self.fail_for:
            raise RuntimeError(f"tenant {tenant} broken")
        if by_label not in self.grouped_by_label:
            return None
        return dict(self.grouped_by_label[by_label])


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

    out = await tools["query_logs"](
        query='{job="a"}', ctx=_ctx_stdio(), verbosity="full"
    )
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

    out = await tools["query_logs"](query='{a="b"}', ctx=_ctx_stdio(), verbosity="full")
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
        await tools["query_logs"](query='{a="b"}', direction="up", ctx=_ctx_stdio())


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
async def test_query_logs_cluster_warnings_surfaced():
    e1 = LogEntry(datetime(2025, 1, 1, tzinfo=timezone.utc), {"job": "a"}, "L1")
    backend = StubBackend(["tenant-a"], entries_by_tenant={"tenant-a": [e1]})
    backend.partial_warn_for = {"tenant-a"}
    tools, _ = _make_setup(backend)

    out = await tools["query_logs"](query='{a="b"}', ctx=_ctx_stdio())
    assert "L1" in out
    assert "Warnings:" in out
    assert "sub-of-tenant-a" in out
    assert "simulated cluster warning" in out


@pytest.mark.asyncio
async def test_query_logs_limit_above_max_rejected():
    backend = StubBackend(["tenant-a"])
    tools, _ = _make_setup(backend)
    with pytest.raises(RuntimeError, match="exceeds maximum"):
        await tools["query_logs"](query='{a="b"}', limit=999_999, ctx=_ctx_stdio())


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
        query='{job="a"}', tenant="tenant-a", ctx=_ctx_stdio(), verbosity="full"
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
        verbosity="full",
    )
    assert "**Instance:** `loki-sh:3100`" in out


@pytest.mark.asyncio
async def test_query_logs_default_instance_marker():
    backend = StubBackend(["tenant-a"])
    tools, _ = _make_setup(backend)
    out = await tools["query_logs"](
        query='{a="b"}', tenant="tenant-a", ctx=_ctx_stdio(), verbosity="full"
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
        query='{a="b"}', ctx=_ctx_http("tenant-a"), verbosity="full"
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
        await tools["query_logs"](query='{a="b"}', ctx=_ctx_http("tenant-c,tenant-d"))


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

    out = await tools["query_logs"](query='{a="b"}', ctx=_ctx_stdio(), verbosity="full")
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
        await tools["query_logs"](query='{a="b"}', tenant="tenant-a", ctx=_ctx_stdio())


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
    out = await tools["health_check"](ctx=_ctx_http("  tenant-a  ,  tenant-b ,, ,"))
    assert "Allowed Tenants (this session):** tenant-a, tenant-b" in out


# ---------------------------------------------------------------------------
# verbosity: compact / normal
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_query_logs_compact_is_default_and_slim():
    e1 = LogEntry(
        datetime(2025, 9, 1, tzinfo=timezone.utc), {"job": "a", "pod": "p1"}, "L1"
    )
    e2 = LogEntry(
        datetime(2025, 9, 1, tzinfo=timezone.utc), {"job": "a", "pod": "p2"}, "L2"
    )
    backend = StubBackend(["tenant-a"], entries_by_tenant={"tenant-a": [e1, e2]})
    tools, _ = _make_setup(backend)  # default verbosity == compact
    out = await tools["query_logs"](
        query='{job="a"}', tenant="tenant-a", ctx=_ctx_stdio()
    )
    assert "L1" in out and "L2" in out
    assert "## Entry" not in out
    assert "**Time:**" not in out
    assert "**Labels:**" not in out
    assert "**Total Entries:**" not in out


@pytest.mark.asyncio
async def test_query_logs_normal_lifts_common_labels():
    e1 = LogEntry(
        datetime(2025, 9, 1, 7, 47, 3, tzinfo=timezone.utc),
        {"job": "a", "pod": "p1"},
        "L1",
    )
    e2 = LogEntry(
        datetime(2025, 9, 1, 7, 48, 3, tzinfo=timezone.utc),
        {"job": "a", "pod": "p2"},
        "L2",
    )
    backend = StubBackend(["tenant-a"], entries_by_tenant={"tenant-a": [e1, e2]})
    tools, _ = _make_setup(backend)
    out = await tools["query_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        verbosity="normal",
    )
    assert "**Common Labels:**" in out
    header_line = next(ln for ln in out.split("\n") if "Common Labels" in ln)
    assert "job=a" in header_line
    assert "pod=p1" in out and "pod=p2" in out
    assert "09-01 07:47:03" in out
    assert "## Entry" not in out


@pytest.mark.asyncio
async def test_query_logs_invalid_verbosity_rejected():
    backend = StubBackend(["tenant-a"])
    tools, _ = _make_setup(backend)
    with pytest.raises(RuntimeError, match="Invalid verbosity"):
        await tools["query_logs"](
            query='{a="b"}',
            tenant="tenant-a",
            ctx=_ctx_stdio(),
            verbosity="loud",
        )


@pytest.mark.asyncio
async def test_query_logs_compact_folds_repeats():
    ts = datetime(2025, 9, 1, tzinfo=timezone.utc)
    entries = [LogEntry(ts, {"job": "a"}, f"request id=100{i} done") for i in range(5)]
    backend = StubBackend(["tenant-a"], entries_by_tenant={"tenant-a": entries})
    tools, _ = _make_setup(backend)
    out = await tools["query_logs"](
        query='{job="a"}', tenant="tenant-a", ctx=_ctx_stdio()
    )
    assert "×5" in out


@pytest.mark.asyncio
async def test_query_logs_compact_no_fold_when_disabled():
    cfg = LogConfig(
        addr="http://stub:3100",
        tenants="tenant-a",
        client_tenants="tenant-a",
        timezone="UTC",
        default_limit=50,
        default_time_range_minutes=15,
        fold_repeats=False,
    )
    ts = datetime(2025, 9, 1, tzinfo=timezone.utc)
    entries = [LogEntry(ts, {"job": "a"}, f"request id=100{i} done") for i in range(5)]
    backend = StubBackend(["tenant-a"], entries_by_tenant={"tenant-a": entries})
    tools, _ = _make_setup(backend, cfg)
    out = await tools["query_logs"](
        query='{job="a"}', tenant="tenant-a", ctx=_ctx_stdio()
    )
    assert "×5" not in out


@pytest.mark.asyncio
async def test_query_logs_compact_truncates_long_line():
    cfg = LogConfig(
        addr="http://stub:3100",
        tenants="tenant-a",
        client_tenants="tenant-a",
        timezone="UTC",
        default_limit=50,
        default_time_range_minutes=15,
        max_line_chars=20,
    )
    ts = datetime(2025, 9, 1, tzinfo=timezone.utc)
    long_line = "X" * 200
    backend = StubBackend(
        ["tenant-a"],
        entries_by_tenant={"tenant-a": [LogEntry(ts, {"job": "a"}, long_line)]},
    )
    tools, _ = _make_setup(backend, cfg)
    out = await tools["query_logs"](
        query='{job="a"}', tenant="tenant-a", ctx=_ctx_stdio()
    )
    assert "chars truncated, full line via download_logs" in out
    assert "X" * 200 not in out


@pytest.mark.asyncio
async def test_query_logs_full_multi_origin_keeps_entry_headers():
    e1 = LogEntry(
        datetime(2025, 1, 1, tzinfo=timezone.utc),
        {"job": "a"},
        "L1",
        tenant="tenant-a",
        cluster="c1",
    )
    e2 = LogEntry(
        datetime(2025, 1, 1, tzinfo=timezone.utc),
        {"job": "b"},
        "L2",
        tenant="tenant-b",
        cluster="c2",
    )
    backend = StubBackend(
        ["tenant-a", "tenant-b"],
        entries_by_tenant={"tenant-a": [e1], "tenant-b": [e2]},
    )
    tools, _ = _make_setup(backend)
    out = await tools["query_logs"](query='{a="b"}', ctx=_ctx_stdio(), verbosity="full")
    assert "## Entry 1" in out
    assert "**Labels:**" in out


@pytest.mark.asyncio
async def test_query_logs_truncation_note_when_limit_reached():
    cfg = LogConfig(
        addr="http://stub:3100",
        tenants="tenant-a",
        client_tenants="tenant-a",
        timezone="UTC",
        default_limit=2,
        default_time_range_minutes=15,
    )
    ts = datetime(2025, 9, 1, tzinfo=timezone.utc)
    entries = [LogEntry(ts, {"job": "a"}, f"L{i}") for i in range(2)]
    backend = StubBackend(["tenant-a"], entries_by_tenant={"tenant-a": entries})
    tools, _ = _make_setup(backend, cfg)
    out = await tools["query_logs"](
        query='{job="a"}', tenant="tenant-a", ctx=_ctx_stdio()
    )
    assert "may have been truncated" in out


# ---------------------------------------------------------------------------
# P0 二期：strip_ansi / min_level / fold_scope
# ---------------------------------------------------------------------------
def _loguru_line(level: str, message: str, *, colored: bool = True) -> str:
    """构造一条 loguru 风格日志行（可选带 ANSI 颜色码）。"""
    if colored:
        return (
            f"\x1b[32m2026-09-03 19:51:37.488\x1b[0m | "
            f"\x1b[1m{level:<8}\x1b[0m | "
            f"\x1b[36mapp.module\x1b[0m:\x1b[36m480\x1b[0m - "
            f"\x1b[1m{message}\x1b[0m"
        )
    return f"2026-09-03 19:51:37.488 | {level:<8} | app.module:480 - {message}"


def _cfg(**overrides) -> LogConfig:
    """构造单租户测试配置（可覆盖任意字段）。"""
    kwargs = dict(
        addr="http://stub:3100",
        tenants="tenant-a",
        client_tenants="tenant-a",
        timezone="UTC",
        default_limit=50,
        default_time_range_minutes=15,
    )
    kwargs.update(overrides)
    return LogConfig(**kwargs)


@pytest.mark.asyncio
async def test_query_logs_strips_ansi_by_default():
    ts = datetime(2025, 9, 1, tzinfo=timezone.utc)
    line = _loguru_line("INFO", "hello world")
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": [LogEntry(ts, {"job": "a"}, line)]}
    )
    tools, _ = _make_setup(backend)
    out = await tools["query_logs"](
        query='{job="a"}', tenant="tenant-a", ctx=_ctx_stdio()
    )
    assert "\x1b[" not in out
    assert "hello world" in out
    assert "| INFO" in out


@pytest.mark.asyncio
async def test_query_logs_strip_ansi_can_be_disabled_per_call():
    ts = datetime(2025, 9, 1, tzinfo=timezone.utc)
    line = _loguru_line("INFO", "hello world")
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": [LogEntry(ts, {"job": "a"}, line)]}
    )
    tools, _ = _make_setup(backend)
    out = await tools["query_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        strip_ansi=False,
    )
    assert "\x1b[32m" in out


@pytest.mark.asyncio
async def test_query_logs_full_never_strips_ansi():
    """full 是绝对逃生舱：即使配置开启 strip_ansi 也保持原始字节。"""
    ts = datetime(2025, 9, 1, tzinfo=timezone.utc)
    line = _loguru_line("INFO", "hello world")
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": [LogEntry(ts, {"job": "a"}, line)]}
    )
    tools, _ = _make_setup(backend, _cfg(strip_ansi=True))
    out = await tools["query_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        verbosity="full",
    )
    assert "\x1b[32m" in out


@pytest.mark.asyncio
async def test_query_logs_normal_strips_ansi():
    ts = datetime(2025, 9, 1, tzinfo=timezone.utc)
    line = _loguru_line("INFO", "hello world")
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": [LogEntry(ts, {"job": "a"}, line)]}
    )
    tools, _ = _make_setup(backend)
    out = await tools["query_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        verbosity="normal",
    )
    assert "\x1b[" not in out


def _mixed_level_entries() -> List[LogEntry]:
    ts = datetime(2025, 9, 1, tzinfo=timezone.utc)
    return [
        LogEntry(ts, {"job": "a"}, _loguru_line("DEBUG", "debug one")),
        LogEntry(ts, {"job": "a"}, _loguru_line("INFO", "info one")),
        LogEntry(ts, {"job": "a"}, _loguru_line("WARNING", "warn one")),
        LogEntry(ts, {"job": "a"}, _loguru_line("ERROR", "error one")),
    ]


@pytest.mark.asyncio
async def test_query_logs_min_level_defaults_to_no_filter():
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _mixed_level_entries()}
    )
    tools, _ = _make_setup(backend)
    out = await tools["query_logs"](
        query='{job="a"}', tenant="tenant-a", ctx=_ctx_stdio()
    )
    for msg in ("debug one", "info one", "warn one", "error one"):
        assert msg in out
    assert "已隐藏" not in out


@pytest.mark.asyncio
async def test_query_logs_min_level_warning_hides_and_reports_count():
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _mixed_level_entries()}
    )
    tools, _ = _make_setup(backend)
    out = await tools["query_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        min_level="warning",
    )
    assert "debug one" not in out
    assert "info one" not in out
    assert "warn one" in out and "error one" in out
    assert "min_level=WARNING 已隐藏 2 条" in out
    assert "DEBUG 1" in out and "INFO 1" in out


@pytest.mark.asyncio
async def test_query_logs_min_level_keeps_unparsable_lines():
    """解析不出级别的行必须保留——不确定就不丢。"""
    ts = datetime(2025, 9, 1, tzinfo=timezone.utc)
    entries = [
        LogEntry(ts, {"job": "a"}, _loguru_line("DEBUG", "debug one")),
        LogEntry(ts, {"job": "a"}, "plain line without any level marker"),
    ]
    backend = StubBackend(["tenant-a"], entries_by_tenant={"tenant-a": entries})
    tools, _ = _make_setup(backend)
    out = await tools["query_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        min_level="ERROR",
    )
    assert "plain line without any level marker" in out
    assert "debug one" not in out


@pytest.mark.asyncio
async def test_query_logs_min_level_invalid_rejected():
    backend = StubBackend(["tenant-a"])
    tools, _ = _make_setup(backend)
    with pytest.raises(RuntimeError, match="Invalid min_level"):
        await tools["query_logs"](
            query='{a="b"}',
            tenant="tenant-a",
            ctx=_ctx_stdio(),
            min_level="loud",
        )


@pytest.mark.asyncio
async def test_query_logs_min_level_warn_alias_accepted():
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _mixed_level_entries()}
    )
    tools, _ = _make_setup(backend)
    out = await tools["query_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        min_level="warn",
    )
    assert "min_level=WARNING" in out


@pytest.mark.asyncio
async def test_query_logs_min_level_ignored_in_full_mode():
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _mixed_level_entries()}
    )
    tools, _ = _make_setup(backend)
    out = await tools["query_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        verbosity="full",
        min_level="ERROR",
    )
    assert "debug one" in out and "info one" in out


def _interleaved_entries() -> List[LogEntry]:
    """交错的同模板行：相邻折叠无效，全局聚类才能压缩。"""
    base = datetime(2025, 9, 1, 11, 51, 0, tzinfo=timezone.utc)
    entries: List[LogEntry] = []
    for i in range(4):
        ts = base.replace(minute=51 + i)
        entries.append(LogEntry(ts, {"job": "a"}, f"GET /api/health id={i} 200"))
        entries.append(LogEntry(ts, {"job": "a"}, f"POST /api/login id={i} 200"))
    return entries


@pytest.mark.asyncio
async def test_query_logs_fold_keeps_distinct_ids_visible():
    """折叠不得吞掉组内不同的标识字段。

    真实排障场景：用户查 task_id 就是要看哪些任务在跑。
    若 90 条同模板行被压成 1 条，其余 task_id 将完全不可见。
    """
    base = datetime(2026, 9, 9, 9, 20, tzinfo=timezone.utc)
    ids = ["19049", "19075", "8359", "19049", "19075"]
    entries = [
        LogEntry(
            base + timedelta(seconds=i),
            {"job": "a"},
            f"2026-09-09 09:20:0{i}.000 | DEBUG | app.utils.task_queue:162 - "
            f"queue[poll] enqueue dedup task_id={tid} source=compensation",
            "tenant-a",
        )
        for i, tid in enumerate(ids)
    ]
    backend = StubBackend(["tenant-a"], entries_by_tenant={"tenant-a": entries})
    tools, _ = _make_setup(backend)
    out = await tools["query_logs"](
        query='{job="a"}', tenant="tenant-a", ctx=_ctx_stdio()
    )
    # 折叠发生了
    assert "×5" in out
    # 但三个不同的 task_id 必须都还能看到
    for tid in ("19049", "19075", "8359"):
        assert tid in out, f"task_id {tid} 被折叠吞掉了"


@pytest.mark.asyncio
async def test_query_logs_fold_scope_adjacent_is_default():
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _interleaved_entries()}
    )
    tools, _ = _make_setup(backend)
    out = await tools["query_logs"](
        query='{job="a"}', tenant="tenant-a", ctx=_ctx_stdio()
    )
    # 交错行没有 ≥3 的连续 run，adjacent 下应该逐条保留、不出现 ×N。
    assert "×" not in out
    assert "fold_scope=global" not in out
    assert out.count("GET /api/health") == 4


@pytest.mark.asyncio
async def test_query_logs_fold_scope_global_clusters_non_adjacent():
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _interleaved_entries()}
    )
    tools, _ = _make_setup(backend)
    out = await tools["query_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        fold_scope="global",
    )
    assert out.count("GET /api/health") == 1
    assert out.count("POST /api/login") == 1
    assert "×4" in out
    assert "11:51:00~11:54:00" in out
    assert "fold_scope=global 将 8 条聚合为 2 组" in out
    assert "fold_scope=adjacent 查看逐条时序" in out


@pytest.mark.asyncio
async def test_query_logs_fold_scope_global_keeps_http_error_codes_distinct():
    """4xx / 5xx 状态码不能被归一化进同一模板。

    否则 200 / 404 / 500 会折成一组，"发生过 500" 这个关键事实会被
    静默吞掉，直接损害排查效果。
    """
    base = datetime(2026, 9, 3, 20, 0, tzinfo=timezone.utc)
    codes = ["200", "200", "200", "500", "404", "200"]
    entries = [
        LogEntry(
            base + timedelta(seconds=i),
            {"job": "a"},
            f'2026-09-03 20:00:0{i}.000 | INFO | uvicorn:480 - "GET /x" {code}',
            "tenant-a",
        )
        for i, code in enumerate(codes)
    ]
    backend = StubBackend(["tenant-a"], entries_by_tenant={"tenant-a": entries})
    tools, _ = _make_setup(backend)
    out = await tools["query_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        fold_scope="global",
    )
    assert '"GET /x" 500' in out
    assert '"GET /x" 404' in out
    assert "聚合为 3 组" in out


@pytest.mark.asyncio
async def test_query_logs_fold_scope_global_never_folds_abnormal_levels():
    """WARNING / ERROR 行在 global 下逐条保留，不参与聚类。"""
    base = datetime(2025, 9, 1, 11, 51, 0, tzinfo=timezone.utc)
    entries: List[LogEntry] = []
    for i in range(3):
        entries.append(
            LogEntry(
                base.replace(minute=51 + i),
                {"job": "a"},
                _loguru_line("ERROR", f"db timeout attempt={i}", colored=False),
            )
        )
        entries.append(
            LogEntry(
                base.replace(minute=51 + i),
                {"job": "a"},
                _loguru_line("WARNING", f"retry attempt={i}", colored=False),
            )
        )
        entries.append(
            LogEntry(
                base.replace(minute=51 + i),
                {"job": "a"},
                _loguru_line("DEBUG", f"heartbeat seq={i}", colored=False),
            )
        )
    backend = StubBackend(["tenant-a"], entries_by_tenant={"tenant-a": entries})
    tools, _ = _make_setup(backend)
    out = await tools["query_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        fold_scope="global",
    )
    assert out.count("db timeout") == 3
    assert out.count("retry attempt") == 3
    assert out.count("heartbeat seq") == 1
    assert "6 条异常行未参与聚合" in out


@pytest.mark.asyncio
async def test_query_logs_fold_scope_global_disabled_by_fold_repeats():
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _interleaved_entries()}
    )
    tools, _ = _make_setup(backend, _cfg(fold_repeats=False))
    out = await tools["query_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        fold_scope="global",
    )
    assert out.count("GET /api/health") == 4
    assert "×" not in out


@pytest.mark.asyncio
async def test_query_logs_fold_scope_from_config_default():
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _interleaved_entries()}
    )
    tools, _ = _make_setup(backend, _cfg(fold_scope="global"))
    out = await tools["query_logs"](
        query='{job="a"}', tenant="tenant-a", ctx=_ctx_stdio()
    )
    assert "×4" in out


@pytest.mark.asyncio
async def test_query_logs_fold_scope_invalid_rejected():
    backend = StubBackend(["tenant-a"])
    tools, _ = _make_setup(backend)
    with pytest.raises(RuntimeError, match="Invalid fold_scope"):
        await tools["query_logs"](
            query='{a="b"}',
            tenant="tenant-a",
            ctx=_ctx_stdio(),
            fold_scope="everything",
        )


@pytest.mark.asyncio
async def test_query_logs_adjacent_never_folds_abnormal_levels():
    """adjacent 下连续的 ERROR 行也必须逐条保留。"""
    ts = datetime(2025, 9, 1, tzinfo=timezone.utc)
    entries = [
        LogEntry(
            ts,
            {"job": "a"},
            _loguru_line("ERROR", f"db timeout attempt={i}", colored=False),
        )
        for i in range(5)
    ]
    backend = StubBackend(["tenant-a"], entries_by_tenant={"tenant-a": entries})
    tools, _ = _make_setup(backend)
    # 关掉 A1 公共前缀上提，这里只验证"折叠"这一件事：否则 5 条同模板
    # ERROR 行的公共前缀会被提到头部，正文里自然不再重复出现。
    out = await tools["query_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        hoist_common_prefix=False,
    )
    assert "×5" not in out
    assert out.count("db timeout") == 5


# ---------------------------------------------------------------------------
# count_logs tool
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_count_logs_reports_per_tenant_and_total():
    ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
    a = [LogEntry(ts, {"job": "a"}, "L1"), LogEntry(ts, {"job": "a"}, "L2")]
    b = [LogEntry(ts, {"job": "b"}, "L3")]
    backend = StubBackend(
        ["tenant-a", "tenant-b"],
        entries_by_tenant={"tenant-a": a, "tenant-b": b},
    )
    tools, _ = _make_setup(backend)
    out = await tools["count_logs"](query='{job="a"}', ctx=_ctx_stdio())
    assert "`tenant-a`: 2" in out
    assert "`tenant-b`: 1" in out
    assert "**Total:** 3" in out


@pytest.mark.asyncio
async def test_count_logs_refuses_when_filter_unset():
    backend = StubBackend(["tenant-a", "tenant-b"])
    tools, _ = _make_setup(backend, with_client_filter=False)
    with pytest.raises(RuntimeError, match="No tenant scope is configured"):
        await tools["count_logs"](query='{a="b"}', ctx=_ctx_stdio())


@pytest.mark.asyncio
async def test_count_logs_single_tenant():
    ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
    a = [LogEntry(ts, {"job": "a"}, "L1")]
    backend = StubBackend(["tenant-a", "tenant-b"], entries_by_tenant={"tenant-a": a})
    tools, _ = _make_setup(backend)
    out = await tools["count_logs"](
        query='{job="a"}', tenant="tenant-a", ctx=_ctx_stdio()
    )
    assert "`tenant-a`: 1" in out
    assert "tenant-b" not in out
    assert "**Total:** 1" in out


# ---------------------------------------------------------------------------
# get_labels / get_label_values multi-tenant merge
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_label_values_merges_across_tenants():
    backend = StubBackend(["tenant-a", "tenant-b"])
    tools, _ = _make_setup(backend)
    out = await tools["get_label_values"](label="env", ctx=_ctx_stdio())
    assert out.count("`env-1`") == 1
    assert "tenants: tenant-a, tenant-b" in out


@pytest.mark.asyncio
async def test_get_labels_single_tenant_stays_sectioned():
    backend = StubBackend(["tenant-a", "tenant-b"])
    tools, _ = _make_setup(backend)
    out = await tools["get_labels"](tenant="tenant-a", ctx=_ctx_stdio())
    assert "## Tenant: `tenant-a`" in out
    assert "job" in out


# ---------------------------------------------------------------------------
# P1-4：exclude_loggers（噪音源黑名单）
# ---------------------------------------------------------------------------
def _named_line(level: str, logger_name: str, message: str) -> str:
    """构造一条带 logger 名的 loguru 风格日志行（不带 ANSI）。"""
    return f"2026-09-03 19:51:37.488 | {level:<8} | {logger_name}:480 - {message}"


def _noisy_entries() -> List[LogEntry]:
    """两条 httpcore 噪音 + 一条 httpcore ERROR + 一条业务日志 + 一条裸行。"""
    ts = datetime(2026, 9, 3, 19, 51, 37, 488000, tzinfo=timezone.utc)
    return [
        LogEntry(ts, {"job": "a"}, _named_line("DEBUG", "httpcore._trace", "conn a")),
        LogEntry(ts, {"job": "a"}, _named_line("DEBUG", "httpcore._trace", "conn b")),
        LogEntry(
            ts, {"job": "a"}, _named_line("ERROR", "httpcore._trace", "conn dead")
        ),
        LogEntry(ts, {"job": "a"}, _named_line("INFO", "app.services.queue", "job ok")),
        LogEntry(ts, {"job": "a"}, "bare line with no level and no logger"),
    ]


@pytest.mark.asyncio
async def test_query_logs_exclude_loggers_defaults_to_no_exclusion():
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _noisy_entries()}
    )
    tools, _ = _make_setup(backend, _cfg())
    out = await tools["query_logs"](
        query='{job="a"}', tenant="tenant-a", ctx=_ctx_stdio()
    )
    assert "conn a" in out
    assert "conn b" in out
    assert "job ok" in out
    assert "exclude_loggers" not in out


@pytest.mark.asyncio
async def test_query_logs_exclude_loggers_wildcard_hides_and_reports_count():
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _noisy_entries()}
    )
    tools, _ = _make_setup(backend, _cfg())
    out = await tools["query_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        exclude_loggers=["httpcore.*"],
    )
    assert "conn a" not in out
    assert "conn b" not in out
    assert "exclude_loggers 已隐藏 2 条（httpcore._trace 2）" in out
    assert "省略该参数可查看全部" in out


@pytest.mark.asyncio
async def test_query_logs_exclude_loggers_keeps_error_lines():
    """硬保护：ERROR 行即使 logger 命中黑名单也必须保留。"""
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _noisy_entries()}
    )
    tools, _ = _make_setup(backend, _cfg())
    out = await tools["query_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        exclude_loggers=["httpcore.*"],
    )
    assert "conn dead" in out
    assert "异常级别行不受影响" in out


@pytest.mark.asyncio
async def test_query_logs_exclude_loggers_keeps_unparsable_lines():
    """解析不出 logger 的行一律保留（不确定就不丢）。"""
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _noisy_entries()}
    )
    tools, _ = _make_setup(backend, _cfg())
    out = await tools["query_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        exclude_loggers=["*"],
    )
    assert "bare line with no level and no logger" in out


@pytest.mark.asyncio
async def test_query_logs_exclude_loggers_exact_name_match():
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _noisy_entries()}
    )
    tools, _ = _make_setup(backend, _cfg())
    out = await tools["query_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        exclude_loggers=["app.services.queue"],
    )
    assert "job ok" not in out
    assert "conn a" in out
    assert "app.services.queue 1" in out


@pytest.mark.asyncio
async def test_query_logs_exclude_loggers_from_config_default():
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _noisy_entries()}
    )
    tools, _ = _make_setup(backend, _cfg(default_exclude_loggers="httpcore.*"))
    out = await tools["query_logs"](
        query='{job="a"}', tenant="tenant-a", ctx=_ctx_stdio()
    )
    assert "conn a" not in out
    assert "exclude_loggers 已隐藏 2 条" in out


@pytest.mark.asyncio
async def test_query_logs_exclude_loggers_ignored_in_full_mode():
    """full 是绝对逃生舱：不参与任何新的过滤。"""
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _noisy_entries()}
    )
    tools, _ = _make_setup(backend, _cfg(default_exclude_loggers="httpcore.*"))
    out = await tools["query_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        verbosity="full",
        exclude_loggers=["httpcore.*"],
    )
    assert "conn a" in out
    assert "conn b" in out
    assert "exclude_loggers 已隐藏" not in out


# ---------------------------------------------------------------------------
# P1-5：dedup_timestamp（去正文内重复时间戳）
# ---------------------------------------------------------------------------
def _ts_entries() -> List[LogEntry]:
    """一条正文时间戳与 entry 时间吻合、一条明显不吻合。"""
    ts = datetime(2026, 9, 3, 19, 51, 37, 488000, tzinfo=timezone.utc)
    return [
        LogEntry(ts, {"job": "a"}, _named_line("INFO", "app.mod", "matching stamp")),
        LogEntry(
            ts,
            {"job": "a"},
            "2020-01-01 00:00:00.000 | INFO     | app.mod:480 - stale stamp",
        ),
    ]


@pytest.mark.asyncio
async def test_query_logs_dedup_timestamp_off_by_default_in_normal():
    """默认不去重：它会改写日志正文，而正文是排查依据。"""
    backend = StubBackend(["tenant-a"], entries_by_tenant={"tenant-a": _ts_entries()})
    tools, _ = _make_setup(backend, _cfg(timezone="UTC"))
    out = await tools["query_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        verbosity="normal",
    )
    assert "2026-09-03 19:51:37.488" in out
    assert "正文内与行首重复的时间戳已省略" not in out


@pytest.mark.asyncio
async def test_query_logs_dedup_timestamp_normal_removes_and_notes():
    backend = StubBackend(["tenant-a"], entries_by_tenant={"tenant-a": _ts_entries()})
    tools, _ = _make_setup(backend, _cfg(timezone="UTC", dedup_timestamp=True))
    out = await tools["query_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        verbosity="normal",
    )
    assert "2026-09-03 19:51:37.488" not in out
    assert "matching stamp" in out
    assert "正文内与行首重复的时间戳已省略" in out


@pytest.mark.asyncio
async def test_query_logs_dedup_timestamp_never_in_compact():
    """compact 行首没有时间，正文时间戳是唯一时间来源，绝不去重。"""
    backend = StubBackend(["tenant-a"], entries_by_tenant={"tenant-a": _ts_entries()})
    tools, _ = _make_setup(backend, _cfg(timezone="UTC", dedup_timestamp=True))
    out = await tools["query_logs"](
        query='{job="a"}', tenant="tenant-a", ctx=_ctx_stdio()
    )
    assert "2026-09-03 19:51:37.488" in out
    assert "正文内与行首重复的时间戳已省略" not in out


@pytest.mark.asyncio
async def test_query_logs_dedup_timestamp_keeps_mismatched_stamp():
    """时间不吻合时保持原样——宁可冗余也不能删错。"""
    backend = StubBackend(["tenant-a"], entries_by_tenant={"tenant-a": _ts_entries()})
    tools, _ = _make_setup(backend, _cfg(timezone="UTC"))
    out = await tools["query_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        verbosity="normal",
    )
    assert "2020-01-01 00:00:00.000" in out


@pytest.mark.asyncio
async def test_query_logs_dedup_timestamp_can_be_disabled():
    backend = StubBackend(["tenant-a"], entries_by_tenant={"tenant-a": _ts_entries()})
    tools, _ = _make_setup(backend, _cfg(timezone="UTC"))
    out = await tools["query_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        verbosity="normal",
        dedup_timestamp=False,
    )
    assert "2026-09-03 19:51:37.488" in out
    assert "正文内与行首重复的时间戳已省略" not in out


@pytest.mark.asyncio
async def test_query_logs_dedup_timestamp_ignored_in_full_mode():
    backend = StubBackend(["tenant-a"], entries_by_tenant={"tenant-a": _ts_entries()})
    tools, _ = _make_setup(backend, _cfg(timezone="UTC", dedup_timestamp=True))
    out = await tools["query_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        verbosity="full",
    )
    assert "2026-09-03 19:51:37.488" in out


# ---------------------------------------------------------------------------
# P2-7：sample_per_template（分层采样）
# ---------------------------------------------------------------------------
def _template_entries() -> List[LogEntry]:
    """3 条同模板 DEBUG（只有数字不同）+ 2 条 WARNING + 2 条 ERROR。"""
    base = datetime(2026, 9, 3, 19, 51, 37, 488000, tzinfo=timezone.utc)
    out: List[LogEntry] = []
    for i in range(3):
        out.append(
            LogEntry(
                base + timedelta(seconds=i),
                {"job": "a"},
                _named_line("DEBUG", "httpcore._trace", f"conn to host {i}"),
            )
        )
    for offset, level, msg in (
        (3, "WARNING", "slow query 1"),
        (4, "WARNING", "slow query 2"),
        (5, "ERROR", "db down 1"),
        (6, "ERROR", "db down 2"),
    ):
        out.append(
            LogEntry(
                base + timedelta(seconds=offset),
                {"job": "a"},
                _named_line(level, "app.mod", msg),
            )
        )
    return out


@pytest.mark.asyncio
async def test_query_logs_sample_per_template_defaults_to_no_sampling():
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _template_entries()}
    )
    tools, _ = _make_setup(backend, _cfg(fold_repeats=False))
    out = await tools["query_logs"](
        query='{job="a"}', tenant="tenant-a", ctx=_ctx_stdio()
    )
    for i in range(3):
        assert f"conn to host {i}" in out
    assert "sample_per_template" not in out


@pytest.mark.asyncio
async def test_query_logs_sample_per_template_keeps_one_per_template():
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _template_entries()}
    )
    tools, _ = _make_setup(backend, _cfg(fold_repeats=False))
    out = await tools["query_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        sample_per_template=1,
    )
    assert "conn to host 0" in out
    assert "conn to host 1" not in out
    assert "conn to host 2" not in out
    assert "sample_per_template=1 已隐藏 2 条（1 个模板，异常行全部保留）" in out
    assert "download_logs 查看全部" in out


@pytest.mark.asyncio
async def test_query_logs_sample_per_template_never_samples_abnormal_levels():
    """硬保护：WARNING / ERROR 行完全不参与采样，全部保留。"""
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _template_entries()}
    )
    tools, _ = _make_setup(backend, _cfg(fold_repeats=False))
    out = await tools["query_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        sample_per_template=1,
    )
    for msg in ("slow query 1", "slow query 2", "db down 1", "db down 2"):
        assert msg in out


@pytest.mark.asyncio
async def test_query_logs_sample_per_template_preserves_order():
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _template_entries()}
    )
    tools, _ = _make_setup(backend, _cfg(fold_repeats=False))
    out = await tools["query_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        sample_per_template=1,
    )
    order = [
        out.index("conn to host 0"),
        out.index("slow query 1"),
        out.index("slow query 2"),
        out.index("db down 1"),
        out.index("db down 2"),
    ]
    assert order == sorted(order)


@pytest.mark.asyncio
async def test_query_logs_sample_per_template_ignored_in_full_mode():
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _template_entries()}
    )
    tools, _ = _make_setup(backend, _cfg(fold_repeats=False))
    out = await tools["query_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        verbosity="full",
        sample_per_template=1,
    )
    for i in range(3):
        assert f"conn to host {i}" in out
    assert "sample_per_template=1 已隐藏" not in out


@pytest.mark.asyncio
async def test_query_logs_sample_per_template_rejects_non_positive():
    backend = StubBackend(["tenant-a"])
    tools, _ = _make_setup(backend, _cfg())
    with pytest.raises(RuntimeError, match="sample_per_template must be"):
        await tools["query_logs"](
            query='{job="a"}',
            tenant="tenant-a",
            ctx=_ctx_stdio(),
            sample_per_template=0,
        )


# ---------------------------------------------------------------------------
# P2-6：count_logs(group_by=...) 分布画像
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_count_logs_group_by_none_keeps_legacy_output():
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _noisy_entries()}
    )
    tools, _ = _make_setup(backend, _cfg())
    out = await tools["count_logs"](
        query='{job="a"}', tenant="tenant-a", ctx=_ctx_stdio()
    )
    assert out.startswith("# Log Count\n")
    assert "`tenant-a`: 5" in out
    assert "**Total:** 5" in out
    assert "Basis" not in out


@pytest.mark.asyncio
async def test_count_logs_group_by_level_pushes_down_when_supported():
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _noisy_entries()}
    )
    backend.grouped_by_label = {"detected_level": {"error": 3, "info": 7}}
    tools, _ = _make_setup(backend, _cfg())
    out = await tools["count_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        group_by="level",
    )
    assert "# Log Count by `level`" in out
    assert "exact push-down" in out
    assert "`info` | 7 | 70.0%" in out
    assert "`error` | 3 | 30.0%" in out
    assert "**Total:** 10" in out


@pytest.mark.asyncio
async def test_count_logs_group_by_level_falls_back_to_sampling():
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _noisy_entries()}
    )
    tools, _ = _make_setup(backend, _cfg())
    out = await tools["count_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        group_by="level",
    )
    assert "estimated from a 5-entry sample" in out
    assert "NOT exact totals" in out
    assert "`DEBUG` | 2" in out
    assert "`ERROR` | 1" in out
    assert "`(unparsed)` | 1" in out


@pytest.mark.asyncio
async def test_count_logs_group_by_logger_groups_by_module():
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _noisy_entries()}
    )
    tools, _ = _make_setup(backend, _cfg())
    out = await tools["count_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        group_by="logger",
    )
    assert "# Log Count by `logger`" in out
    assert "`httpcore._trace` | 3" in out
    assert "`app.services.queue` | 1" in out
    assert "estimated from a 5-entry sample" in out


@pytest.mark.asyncio
async def test_count_logs_group_by_template_always_samples():
    backend = StubBackend(
        ["tenant-a"], entries_by_tenant={"tenant-a": _template_entries()}
    )
    # 即使后端能下推标签，template 维度也只能走样本。
    backend.grouped_by_label = {"detected_level": {"error": 1}}
    tools, _ = _make_setup(backend, _cfg())
    out = await tools["count_logs"](
        query='{job="a"}',
        tenant="tenant-a",
        ctx=_ctx_stdio(),
        group_by="template",
    )
    assert "# Log Count by `template`" in out
    assert "template grouping cannot be pushed down" in out
    # 3 条 conn to host N 归一化后是同一个模板。
    assert "| 3 |" in out


@pytest.mark.asyncio
async def test_count_logs_group_by_invalid_rejected():
    backend = StubBackend(["tenant-a"])
    tools, _ = _make_setup(backend, _cfg())
    with pytest.raises(RuntimeError, match="Invalid group_by"):
        await tools["count_logs"](
            query='{job="a"}',
            tenant="tenant-a",
            ctx=_ctx_stdio(),
            group_by="severity",
        )


@pytest.mark.asyncio
async def test_count_logs_group_by_surfaces_tenant_errors():
    backend = StubBackend(
        ["tenant-a", "tenant-b"],
        entries_by_tenant={"tenant-a": _noisy_entries()},
    )
    backend.fail_for = {"tenant-b"}
    tools, _ = _make_setup(backend)
    out = await tools["count_logs"](
        query='{job="a"}', ctx=_ctx_stdio(), group_by="level"
    )
    assert "`tenant-b`: _error_" in out
    assert "Errors:" in out


# ---------------------------------------------------------------------------
# A 档「零信息损失」正文瘦身：A1 公共前缀 / A2 连续空白 / A3 正文内日期
# ---------------------------------------------------------------------------
_TS_A = datetime(2026, 9, 3, 19, 51, 37, 488000, tzinfo=timezone.utc)


def _slim_entries(lines, *, ts=None):
    """把若干原始正文包成 LogEntry 列表（标签统一，避免干扰断言）。"""
    return [LogEntry(ts or _TS_A, {"app": "demo"}, line) for line in lines]


async def _slim_query(entries, **kwargs):
    """跑一次 query_logs 并返回输出（单租户，默认三项瘦身全开）。"""
    backend = StubBackend(["tenant-a"], entries_by_tenant={"tenant-a": entries})
    tools, _ = _make_setup(backend)
    return await tools["query_logs"](
        query='{app="demo"}', tenant="tenant-a", ctx=_ctx_stdio(), **kwargs
    )


@pytest.mark.asyncio
async def test_a1_prefix_never_splits_a_date_or_word():
    """A1 只能在空白处切分，绝不能把日期/数字腰斩。

    反例：2026-09-03 与 2026-09-04 的字符级公共前缀是
    "2026-09-0"，若按字符切，行内只剩 "3 ..." / "4 ..."，
    既极易误读又彻底破坏 grep 可用性。
    """
    base = datetime(2026, 9, 3, 23, 59, tzinfo=timezone.utc)
    entries = [
        LogEntry(
            base,
            {"job": "a"},
            "2026-09-03 23:59:59.900 | INFO | svc:1 - before midnight",
            "tenant-a",
        ),
        LogEntry(
            base + timedelta(seconds=2),
            {"job": "a"},
            "2026-09-04 00:00:01.100 | INFO | svc:1 - after midnight",
            "tenant-a",
        ),
    ]
    backend = StubBackend(["tenant-a"], entries_by_tenant={"tenant-a": entries})
    tools, _ = _make_setup(backend)
    out = await tools["query_logs"](
        query='{job="a"}', tenant="tenant-a", ctx=_ctx_stdio()
    )
    # 跳天时两个完整日期必须都还在，不得出现被切半的日期
    assert "2026-09-03" in out
    assert "2026-09-04" in out
    assert "2026-09-0`" not in out


@pytest.mark.asyncio
async def test_a1_hoists_common_prefix_and_annotates_header():
    """≥8 字符公共前缀上提到头部，行内不再重复。"""
    entries = _slim_entries(
        [
            "service=payment | INFO | handler:10 - order created id=1",
            "service=payment | INFO | handler:11 - order paid id=2",
        ]
    )
    out = await _slim_query(entries, hoist_common_date=False)
    assert "**Common Prefix:** `service=payment | INFO | `" in out
    # 行内已去掉前缀，但排查信息（行号尾数 / 业务 ID）完好
    assert "handler:10 - order created id=1" in out
    assert "handler:11 - order paid id=2" in out
    assert out.count("service=payment") == 1


@pytest.mark.asyncio
async def test_a1_skips_short_common_prefix():
    """公共前缀 <8 字符时不上提（收益抵不上认知负担）。"""
    entries = _slim_entries(["abc first line", "abc second line"])
    out = await _slim_query(entries)
    assert "Common Prefix" not in out
    assert "abc first line" in out
    assert "abc second line" in out


@pytest.mark.asyncio
async def test_a1_skips_single_line():
    """只有 1 行时谈不上公共前缀，不上提。"""
    entries = _slim_entries(["service=payment | INFO | only one line"])
    out = await _slim_query(entries, hoist_common_date=False)
    assert "Common Prefix" not in out
    assert "service=payment | INFO | only one line" in out


@pytest.mark.asyncio
async def test_a1_skips_when_prefix_would_empty_a_line():
    """前缀不得把任何一行吃空；回退到空白边界后仍可安全上提。

    "service=payment start" 与 "service=payment start extra tail" 的
    字符级公共前缀正好等于第一行，直接切会产生空行；回退到
    最后一个空白后变成 "service=payment "，既省了字符又不会空行。
    """
    entries = _slim_entries(
        ["service=payment start", "service=payment start extra tail"]
    )
    out = await _slim_query(entries)
    assert "**Common Prefix:** `service=payment `" in out
    # 两行内容均未被吃空，且可与前缀拼回原文
    assert "start extra tail" in out
    assert out.count("service=payment") == 1


@pytest.mark.asyncio
async def test_a1_multi_origin_excludes_origin_tag():
    """多租户时 [tenant] 标记不参与前缀计算，且每行标记保留。"""
    e1 = LogEntry(
        _TS_A, {"app": "demo"}, "service=payment | INFO | a=1", tenant="tenant-a"
    )
    e2 = LogEntry(
        _TS_A, {"app": "demo"}, "service=payment | INFO | a=2", tenant="tenant-b"
    )
    backend = StubBackend(
        ["tenant-a", "tenant-b"],
        entries_by_tenant={"tenant-a": [e1], "tenant-b": [e2]},
    )
    tools, _ = _make_setup(backend)
    out = await tools["query_logs"](query='{app="demo"}', ctx=_ctx_stdio())
    assert "**Common Prefix:** `service=payment | INFO | `" in out
    assert "[tenant-a] a=1" in out
    assert "[tenant-b] a=2" in out


@pytest.mark.asyncio
async def test_a1_disabled_keeps_prefix_inline():
    """hoist_common_prefix=False 时前缀留在行内。"""
    entries = _slim_entries(
        [
            "service=payment | INFO | handler:10 - one",
            "service=payment | INFO | handler:11 - two",
        ]
    )
    out = await _slim_query(entries, hoist_common_prefix=False)
    assert "Common Prefix" not in out
    assert out.count("service=payment") == 2


@pytest.mark.asyncio
async def test_a1_applies_to_normal_verbosity():
    """A1 在 normal 模式同样生效（只作用于正文，不动行首时间）。"""
    entries = _slim_entries(
        [
            "service=payment | INFO | handler:10 - one",
            "service=payment | INFO | handler:11 - two",
        ]
    )
    out = await _slim_query(entries, verbosity="normal")
    assert "**Common Prefix:** `service=payment | INFO | `" in out


@pytest.mark.asyncio
async def test_a2_collapses_inner_spaces_but_keeps_indent():
    """连续空格压成 1 个；行首缩进（堆栈结构）原样保留。"""
    entries = _slim_entries(
        [
            "Traceback (most recent call last):",
            '    File "svc.py",  line 42,   in  handler',
            "ValueError:  boom",
        ]
    )
    out = await _slim_query(entries)
    assert '    File "svc.py", line 42, in handler' in out
    assert "ValueError: boom" in out
    assert "  line 42" not in out


@pytest.mark.asyncio
async def test_a2_disabled_keeps_original_spacing():
    """collapse_whitespace=False 时空格原样保留。"""
    entries = _slim_entries(
        [
            "Traceback (most recent call last):",
            '    File "svc.py",  line 42,   in  handler',
        ]
    )
    out = await _slim_query(entries, collapse_whitespace=False)
    assert '    File "svc.py",  line 42,   in  handler' in out


@pytest.mark.asyncio
async def test_a2_collapses_loguru_level_padding_in_normal():
    """loguru 的级别对齐填充在 normal 模式也被压掉。"""
    entries = _slim_entries([_loguru_line("INFO", "aa", colored=False)])
    out = await _slim_query(entries, verbosity="normal")
    assert "| INFO | app.module:480 - aa" in out


@pytest.mark.asyncio
async def test_a3_drops_common_date_but_keeps_clock():
    """同一天时日期被省略、时分秒完整保留，头部有 Date 标注。"""
    entries = _slim_entries(
        [
            "2026-09-03 19:51:37.488 | INFO | svc:1 - alpha",
            "2026-09-03 19:52:41.100 | INFO | svc:2 - beta",
        ]
    )
    out = await _slim_query(entries, hoist_common_prefix=False)
    assert "**Date:** `2026-09-03`" in out
    assert "正文内重复日期已省略" in out
    assert "19:51:37.488 | INFO | svc:1 - alpha" in out
    assert "19:52:41.100 | INFO | svc:2 - beta" in out
    assert "2026-09-03 19:51" not in out


@pytest.mark.asyncio
async def test_a3_disabled_across_midnight():
    """跨天时整批不省略——日期是判断顺序的关键信息。"""
    entries = _slim_entries(
        [
            "2026-09-03 23:59:59.900 | INFO | svc:1 - before",
            "2026-09-04 00:00:01.100 | INFO | svc:2 - after",
        ]
    )
    out = await _slim_query(entries, hoist_common_prefix=False)
    assert "**Date:**" not in out
    assert "2026-09-03 23:59:59.900" in out
    assert "2026-09-04 00:00:01.100" in out


@pytest.mark.asyncio
async def test_a3_disabled_by_flag():
    """hoist_common_date=False 时正文内日期保持原样。"""
    entries = _slim_entries(
        [
            "2026-09-03 19:51:37.488 | INFO | svc:1 - alpha",
            "2026-09-03 19:52:41.100 | INFO | svc:2 - beta",
        ]
    )
    out = await _slim_query(entries, hoist_common_date=False, hoist_common_prefix=False)
    assert "**Date:**" not in out
    assert "2026-09-03 19:51:37.488" in out


@pytest.mark.asyncio
async def test_a3_not_applied_in_normal_or_full():
    """A3 只作用于 compact：normal 交给 dedup_timestamp，full 不生效。"""
    entries = _slim_entries(
        [
            "2026-09-03 19:51:37.488 | INFO | svc:1 - alpha",
            "2026-09-03 19:52:41.100 | INFO | svc:2 - beta",
        ]
    )
    out_normal = await _slim_query(
        entries, verbosity="normal", hoist_common_prefix=False
    )
    assert "**Date:**" not in out_normal
    assert "2026-09-03 19:51:37.488" in out_normal

    out_full = await _slim_query(entries, verbosity="full")
    assert "**Date:**" not in out_full
    assert "2026-09-03 19:51:37.488" in out_full


@pytest.mark.asyncio
async def test_full_verbosity_immune_to_all_three_slim_steps():
    """full 是绝对逃生舱：三项均不生效，正文逐字节原样。"""
    lines = [
        "2026-09-03 19:51:37.488 | INFO     | svc:1 - alpha",
        "2026-09-03 19:52:41.100 | INFO     | svc:2 - beta",
    ]
    entries = _slim_entries(lines)
    out = await _slim_query(entries, verbosity="full")
    assert "Common Prefix" not in out
    assert "**Date:**" not in out
    for line in lines:
        assert line in out


@pytest.mark.asyncio
async def test_all_slim_flags_off_matches_legacy_compact_output():
    """三项开关全关时 compact 输出与改动前一致（无头部标注 / 无改写）。"""
    lines = [
        "2026-09-03 19:51:37.488 | INFO     | svc:1 - alpha",
        "2026-09-03 19:52:41.100 | INFO     | svc:2 - beta",
    ]
    entries = _slim_entries(lines)
    out = await _slim_query(
        entries,
        hoist_common_prefix=False,
        collapse_whitespace=False,
        hoist_common_date=False,
    )
    assert "Common Prefix" not in out
    assert "**Date:**" not in out
    for line in lines:
        assert line in out
