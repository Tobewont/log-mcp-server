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


def _make_setup(backend: StubBackend, config: Optional[LogConfig] = None):
    cfg = config or LogConfig(
        addr="http://stub:3100",
        tenants="tenant-a|tenant-b",
        timezone="UTC",
        default_limit=50,
        default_time_range_minutes=15,
    )
    fake_mcp, tools = _capture_tools()
    initialize_tools(backend, cfg)
    register_tools(fake_mcp)
    return tools, cfg


@pytest.mark.asyncio
async def test_health_check_tool():
    backend = StubBackend(["tenant-a"])
    tools, _ = _make_setup(backend)
    out = await tools["health_check"]()
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

    out = await tools["query_logs"](query='{job="a"}')
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

    out = await tools["query_logs"](query='{a="b"}')
    assert "Total Entries:** 1" in out
    assert "Errors:" in out
    assert "tenant-b" in out


@pytest.mark.asyncio
async def test_query_logs_all_tenants_fail_distinguished_from_no_data():
    backend = StubBackend(["tenant-a"], entries_by_tenant={})
    backend.fail_for = {"tenant-a"}
    tools, _ = _make_setup(backend)

    out = await tools["query_logs"](query='{a="b"}')
    assert "All tenant queries failed" in out


@pytest.mark.asyncio
async def test_query_logs_invalid_direction_rejected():
    backend = StubBackend(["tenant-a"])
    tools, _ = _make_setup(backend)
    with pytest.raises(RuntimeError, match="Invalid direction"):
        await tools["query_logs"](query='{a="b"}', direction="up")


@pytest.mark.asyncio
async def test_query_logs_cluster_errors_surfaced():
    e1 = LogEntry(datetime(2025, 1, 1, tzinfo=timezone.utc), {"job": "a"}, "L1")
    backend = StubBackend(["tenant-a"], entries_by_tenant={"tenant-a": [e1]})
    backend.partial_fail_for = {"tenant-a"}
    tools, _ = _make_setup(backend)

    out = await tools["query_logs"](query='{a="b"}')
    assert "L1" in out
    assert "Errors:" in out
    assert "sub-of-tenant-a" in out
    assert "simulated cluster failure" in out


@pytest.mark.asyncio
async def test_query_logs_limit_above_max_rejected():
    backend = StubBackend(["tenant-a"])
    tools, _ = _make_setup(backend)
    with pytest.raises(RuntimeError, match="exceeds maximum"):
        await tools["query_logs"](query='{a="b"}', limit=999_999)


@pytest.mark.asyncio
async def test_query_logs_limit_zero_rejected():
    backend = StubBackend(["tenant-a"])
    tools, _ = _make_setup(backend)
    with pytest.raises(RuntimeError, match="positive"):
        await tools["query_logs"](query='{a="b"}', limit=0)


@pytest.mark.asyncio
async def test_get_labels_groups_by_tenant():
    backend = StubBackend(["tenant-a", "tenant-b"])
    tools, _ = _make_setup(backend)
    out = await tools["get_labels"]()
    assert "tenant-a" in out and "tenant-b" in out
    assert "job" in out and "host" in out


@pytest.mark.asyncio
async def test_get_label_values_requires_label():
    backend = StubBackend(["tenant-a"])
    tools, _ = _make_setup(backend)
    with pytest.raises(RuntimeError):
        await tools["get_label_values"](label="")


@pytest.mark.asyncio
async def test_get_label_values_with_time():
    backend = StubBackend(["tenant-a"])
    tools, _ = _make_setup(backend)
    out = await tools["get_label_values"](
        label="env",
        start="2025-01-01T00:00:00Z",
        end="2025-01-01T01:00:00Z",
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

    out = await tools["query_logs"](query='{job="a"}', tenant="tenant-a")
    assert "Total Entries:** 1" in out
    assert "L1" in out
    assert "L2" not in out
    assert "Tenants Queried:** `tenant-a`" in out


@pytest.mark.asyncio
async def test_query_logs_unknown_tenant_rejected():
    backend = StubBackend(["tenant-a", "tenant-b"])
    tools, _ = _make_setup(backend)
    with pytest.raises(RuntimeError, match="Unknown tenant"):
        await tools["query_logs"](query='{a="b"}', tenant="no-such-tenant")


@pytest.mark.asyncio
async def test_get_labels_single_tenant():
    backend = StubBackend(["tenant-a", "tenant-b"])
    tools, _ = _make_setup(backend)
    out = await tools["get_labels"](tenant="tenant-a")
    assert "tenant-a" in out
    assert "tenant-b" not in out
    assert "job" in out


@pytest.mark.asyncio
async def test_get_label_values_single_tenant():
    backend = StubBackend(["tenant-a", "tenant-b"])
    tools, _ = _make_setup(backend)
    out = await tools["get_label_values"](label="env", tenant="tenant-a")
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
        query='{a="b"}', tenant="tenant-a", instance="loki-bj:3100"
    )
    assert captured["instance"] == "loki-bj:3100"


@pytest.mark.asyncio
async def test_query_logs_instance_appears_in_header():
    backend = StubBackend(["tenant-a"])
    tools, _ = _make_setup(backend)
    out = await tools["query_logs"](
        query='{a="b"}', tenant="tenant-a", instance="loki-sh:3100"
    )
    assert "**Instance:** `loki-sh:3100`" in out


@pytest.mark.asyncio
async def test_query_logs_default_instance_marker():
    backend = StubBackend(["tenant-a"])
    tools, _ = _make_setup(backend)
    out = await tools["query_logs"](query='{a="b"}', tenant="tenant-a")
    assert "**Instance:** `*all healthy*`" in out
