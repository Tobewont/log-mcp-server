"""Tests for the FanoutBackend."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from log_mcp_server.backends.base import LogBackend, LogEntry
from log_mcp_server.backends.fanout import FanoutBackend
from log_mcp_server.backends.health_cache import HealthCache


class StubBackend(LogBackend):
    """Single-cluster stub used to drive the FanoutBackend."""

    name = "loki"

    def __init__(
        self,
        cluster_id: str,
        entries: List[LogEntry] | None = None,
        labels: List[str] | None = None,
        values: List[str] | None = None,
        health: str = "healthy",
    ) -> None:
        self.cluster_id = cluster_id
        self._entries = entries or []
        self._labels = labels or []
        self._values = values or []
        self._health = health
        self.fail = False

    @property
    def tenants(self) -> List[str]:
        return ["t1"]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def health_check(self) -> Dict[str, Any]:
        if self.fail:
            raise RuntimeError(f"{self.cluster_id} health failure")
        return {
            "backend": self.name,
            "status": self._health,
            "current_time": "2025-01-01T00:00:00+00:00",
            "timezone": "UTC",
            "clusters": [
                {
                    "id": self.cluster_id,
                    "server_addr": f"http://{self.cluster_id}:3100",
                    "status": self._health,
                }
            ],
        }

    async def query_logs(
        self, query, tenant, start, end, limit, direction, cluster_errors=None
    ):
        if self.fail:
            raise RuntimeError(f"{self.cluster_id} query failure")
        return list(self._entries)

    async def get_labels(self, tenant, start=None, end=None, cluster_errors=None):
        if self.fail:
            raise RuntimeError(f"{self.cluster_id} labels failure")
        return list(self._labels)

    async def get_label_values(
        self, tenant, label, start=None, end=None, cluster_errors=None
    ):
        if self.fail:
            raise RuntimeError(f"{self.cluster_id} label values failure")
        return list(self._values)


def _entry(ts: datetime, line: str, cluster: Optional[str] = None):
    return LogEntry(
        timestamp=ts,
        labels={"job": "x"},
        line=line,
        tenant="t1",
        cluster=cluster,
    )


@pytest.mark.asyncio
async def test_health_aggregation_healthy():
    a = StubBackend("a", health="healthy")
    b = StubBackend("b", health="healthy")
    fan = FanoutBackend([a, b])
    info = await fan.health_check()
    assert info["status"] == "healthy"
    assert len(info["clusters"]) == 2


@pytest.mark.asyncio
async def test_health_aggregation_degraded():
    a = StubBackend("a", health="healthy")
    b = StubBackend("b", health="unhealthy")
    fan = FanoutBackend([a, b])
    info = await fan.health_check()
    assert info["status"] == "degraded"
    assert {c["id"] for c in info["clusters"]} == {"a", "b"}


@pytest.mark.asyncio
async def test_health_aggregation_unhealthy_when_all_fail():
    a = StubBackend("a")
    b = StubBackend("b")
    a.fail = True
    b.fail = True
    fan = FanoutBackend([a, b])
    info = await fan.health_check()
    assert info["status"] == "unhealthy"


@pytest.mark.asyncio
async def test_query_merge_sort_backward():
    t0 = datetime(2025, 1, 1, 10, 0, tzinfo=timezone.utc)
    a = StubBackend(
        "a",
        entries=[_entry(t0, "old-a"), _entry(t0 + timedelta(minutes=2), "new-a")],
    )
    b = StubBackend(
        "b",
        entries=[_entry(t0 + timedelta(minutes=1), "mid-b")],
    )
    fan = FanoutBackend([a, b])
    out = await fan.query_logs(
        query="{a=\"b\"}",
        tenant="t1",
        start=t0 - timedelta(hours=1),
        end=t0 + timedelta(hours=1),
        limit=10,
        direction="backward",
    )
    assert [e.line for e in out] == ["new-a", "mid-b", "old-a"]
    # Cluster info preserved/stamped
    clusters = [e.cluster for e in out]
    assert "a" in clusters and "b" in clusters


@pytest.mark.asyncio
async def test_query_merge_sort_forward():
    t0 = datetime(2025, 1, 1, 10, 0, tzinfo=timezone.utc)
    a = StubBackend("a", entries=[_entry(t0, "old-a")])
    b = StubBackend("b", entries=[_entry(t0 + timedelta(minutes=1), "new-b")])
    fan = FanoutBackend([a, b])
    out = await fan.query_logs(
        query="{x=\"y\"}",
        tenant="t1",
        start=t0 - timedelta(hours=1),
        end=t0 + timedelta(hours=1),
        limit=10,
        direction="forward",
    )
    assert [e.line for e in out] == ["old-a", "new-b"]


@pytest.mark.asyncio
async def test_query_global_limit_truncates():
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    a = StubBackend(
        "a",
        entries=[_entry(t0 + timedelta(seconds=i), f"a{i}") for i in range(5)],
    )
    b = StubBackend(
        "b",
        entries=[_entry(t0 + timedelta(seconds=i + 100), f"b{i}") for i in range(5)],
    )
    fan = FanoutBackend([a, b])
    out = await fan.query_logs(
        query="{a=\"b\"}",
        tenant="t1",
        start=t0,
        end=t0 + timedelta(hours=1),
        limit=3,
        direction="backward",
    )
    assert len(out) == 3
    # Backward: newest first => from b (later timestamps)
    assert all(e.cluster == "b" for e in out)


@pytest.mark.asyncio
async def test_query_partial_failure_continues():
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    a = StubBackend("a", entries=[_entry(t0, "a-line")])
    b = StubBackend("b")
    b.fail = True
    fan = FanoutBackend([a, b])
    out = await fan.query_logs(
        query="{a=\"b\"}",
        tenant="t1",
        start=t0 - timedelta(hours=1),
        end=t0 + timedelta(hours=1),
        limit=10,
        direction="backward",
    )
    assert [e.line for e in out] == ["a-line"]


@pytest.mark.asyncio
async def test_get_labels_unioned_and_sorted():
    a = StubBackend("a", labels=["job", "level"])
    b = StubBackend("b", labels=["host", "job"])
    fan = FanoutBackend([a, b])
    labels = await fan.get_labels("t1")
    assert labels == ["host", "job", "level"]


@pytest.mark.asyncio
async def test_get_label_values_unioned_and_sorted():
    a = StubBackend("a", values=["dev", "prod"])
    b = StubBackend("b", values=["prod", "stage"])
    fan = FanoutBackend([a, b])
    values = await fan.get_label_values("t1", "env")
    assert values == ["dev", "prod", "stage"]


@pytest.mark.asyncio
async def test_empty_backends_rejected():
    with pytest.raises(ValueError):
        FanoutBackend([])


@pytest.mark.asyncio
async def test_query_records_cluster_errors():
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    a = StubBackend("a", entries=[_entry(t0, "a-line")])
    b = StubBackend("b")
    b.fail = True
    fan = FanoutBackend([a, b])
    errors: dict[str, str] = {}
    out = await fan.query_logs(
        query="{a=\"b\"}",
        tenant="t1",
        start=t0 - timedelta(hours=1),
        end=t0 + timedelta(hours=1),
        limit=10,
        direction="backward",
        cluster_errors=errors,
    )
    assert [e.line for e in out] == ["a-line"]
    assert "b" in errors
    assert "query failure" in errors["b"]


@pytest.mark.asyncio
async def test_labels_records_cluster_errors():
    a = StubBackend("a", labels=["job"])
    b = StubBackend("b")
    b.fail = True
    fan = FanoutBackend([a, b])
    errors: dict[str, str] = {}
    out = await fan.get_labels("t1", cluster_errors=errors)
    assert out == ["job"]
    assert "b" in errors


@pytest.mark.asyncio
async def test_label_values_records_cluster_errors():
    a = StubBackend("a", values=["v1"])
    b = StubBackend("b")
    b.fail = True
    fan = FanoutBackend([a, b])
    errors: dict[str, str] = {}
    out = await fan.get_label_values("t1", "env", cluster_errors=errors)
    assert out == ["v1"]
    assert "b" in errors


@pytest.mark.asyncio
async def test_health_aggregation_when_sub_is_degraded():
    """A sub-backend reporting 'degraded' should not collapse the
    aggregate to 'unhealthy'."""
    a = StubBackend("a", health="degraded")
    fan = FanoutBackend([a])
    info = await fan.health_check()
    assert info["status"] == "degraded"


# ---------------------------------------------------------------------------
# HealthCache-aware FanoutBackend tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fanout_skips_unhealthy_clusters_for_labels():
    a = StubBackend("a", labels=["job", "level"])
    b = StubBackend("b", labels=["host"])
    b.fail = True
    cache = HealthCache([a, b], interval=9999, probe_timeout=2)
    await cache.start()
    try:
        fan = FanoutBackend([a, b], health_cache=cache)
        labels = await fan.get_labels("t1")
        assert "host" not in labels
        assert "job" in labels
    finally:
        await cache.stop()


@pytest.mark.asyncio
async def test_fanout_skips_unhealthy_clusters_for_query():
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    a = StubBackend("a", entries=[_entry(t0, "a-line")])
    b = StubBackend("b", entries=[_entry(t0, "b-line")])
    b.fail = True
    cache = HealthCache([a, b], interval=9999, probe_timeout=2)
    await cache.start()
    try:
        fan = FanoutBackend([a, b], health_cache=cache)
        out = await fan.query_logs(
            query="{a=\"b\"}",
            tenant="t1",
            start=t0 - timedelta(hours=1),
            end=t0 + timedelta(hours=1),
            limit=10,
            direction="backward",
        )
        assert [e.line for e in out] == ["a-line"]
    finally:
        await cache.stop()


@pytest.mark.asyncio
async def test_fanout_health_check_probes_all_clusters():
    """health_check always probes all clusters, even unhealthy ones."""
    a = StubBackend("a", health="healthy")
    b = StubBackend("b", health="unhealthy")
    cache = HealthCache([a, b], interval=9999, probe_timeout=2)
    await cache.start()
    try:
        fan = FanoutBackend([a, b], health_cache=cache)
        info = await fan.health_check()
        assert info["status"] == "degraded"
        assert len(info["clusters"]) == 2
    finally:
        await cache.stop()
