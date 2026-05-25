"""Tests for the FanoutBackend."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from log_mcp_server.backends.base import LogBackend, LogEntry
from log_mcp_server.backends.fanout import FanoutBackend
from log_mcp_server.backends.health_cache import HealthCache
from log_mcp_server.utils.errors import BackendHTTPError


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
        self,
        query,
        tenant,
        start,
        end,
        limit,
        direction,
        instance=None,
        cluster_errors=None,
        cluster_warnings=None,
    ):
        del instance
        del cluster_warnings
        if self.fail:
            raise RuntimeError(f"{self.cluster_id} query failure")
        return list(self._entries)

    async def get_labels(
        self, tenant, start=None, end=None, instance=None, cluster_errors=None
    ):
        del instance
        if self.fail:
            raise RuntimeError(f"{self.cluster_id} labels failure")
        return list(self._labels)

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
        if self.fail:
            raise RuntimeError(f"{self.cluster_id} label values failure")
        return list(self._values)


class LimitCappedBackend(StubBackend):
    """Stub that mimics Loki's per-cluster max_entries_limit_per_query."""

    def __init__(
        self,
        cluster_id: str,
        max_entries_limit: int,
        entries: List[LogEntry] | None = None,
    ) -> None:
        super().__init__(cluster_id, entries=entries)
        self.max_entries_limit = max_entries_limit
        self.requested_limits: list[int] = []

    async def query_logs(
        self,
        query,
        tenant,
        start,
        end,
        limit,
        direction,
        instance=None,
        cluster_errors=None,
    ):
        self.requested_limits.append(limit)
        if limit > self.max_entries_limit:
            raise BackendHTTPError(
                "HTTP 400: max entries limit per query exceeded, "
                f"limit > max_entries_limit ({limit} > {self.max_entries_limit})",
                status_code=400,
            )
        return await super().query_logs(
            query=query,
            tenant=tenant,
            start=start,
            end=end,
            limit=limit,
            direction=direction,
            instance=instance,
            cluster_errors=cluster_errors,
        )


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
async def test_query_retries_loki_limit_without_lowering_global_limit():
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    high_limit = LimitCappedBackend(
        "high",
        max_entries_limit=50_000,
        entries=[_entry(t0 + timedelta(seconds=i), f"high-{i}") for i in range(5)],
    )
    low_limit = LimitCappedBackend(
        "low",
        max_entries_limit=4_000,
        entries=[_entry(t0, "low")],
    )
    fan = FanoutBackend([high_limit, low_limit])
    errors: dict[str, str] = {}
    warnings: dict[str, str] = {}

    out = await fan.query_logs(
        query="{app=\"demo\"}",
        tenant="t1",
        start=t0 - timedelta(hours=1),
        end=t0 + timedelta(hours=1),
        limit=50_000,
        direction="backward",
        cluster_errors=errors,
        cluster_warnings=warnings,
    )

    assert high_limit.requested_limits == [50_000]
    assert low_limit.requested_limits == [50_000, 4_000]
    assert any(entry.line == "high-4" for entry in out)
    assert any(entry.line == "low" for entry in out)
    assert errors == {}
    assert "low" in warnings
    assert "max_entries_limit_per_query (4000)" in warnings["low"]


@pytest.mark.asyncio
async def test_query_does_not_retry_loki_limit_when_instance_is_explicit():
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    low_limit = LimitCappedBackend("low", max_entries_limit=4_000)
    fan = FanoutBackend([low_limit])
    errors: dict[str, str] = {}

    out = await fan.query_logs(
        query="{app=\"demo\"}",
        tenant="t1",
        start=t0 - timedelta(hours=1),
        end=t0 + timedelta(hours=1),
        limit=50_000,
        direction="backward",
        instance="low",
        cluster_errors=errors,
    )

    assert out == []
    assert low_limit.requested_limits == [50_000]
    assert "low" in errors
    assert "max entries limit per query exceeded" in errors["low"]


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
async def test_fanout_instance_restricts_to_single_cluster():
    """When instance is given, only that cluster's results are returned."""
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    a = StubBackend("a", entries=[_entry(t0, "a-line")])
    b = StubBackend("b", entries=[_entry(t0, "b-line")])
    fan = FanoutBackend([a, b])
    out = await fan.query_logs(
        query="{x=\"y\"}",
        tenant="t1",
        start=t0 - timedelta(hours=1),
        end=t0 + timedelta(hours=1),
        limit=10,
        direction="backward",
        instance="b",
    )
    assert [e.line for e in out] == ["b-line"]


@pytest.mark.asyncio
async def test_fanout_instance_unknown_rejected():
    a = StubBackend("a")
    fan = FanoutBackend([a])
    from log_mcp_server.utils.errors import ValidationError

    with pytest.raises(ValidationError, match="Unknown instance"):
        await fan.get_labels("t1", instance="nope")


@pytest.mark.asyncio
async def test_fanout_instance_overrides_health_filter():
    """Explicit instance is honoured even if it's marked unhealthy."""
    a = StubBackend("a", labels=["job"])
    b = StubBackend("b", labels=["host"])
    a.fail = True  # health probe fails
    cache = HealthCache([a, b], interval=9999, probe_timeout=2)
    await cache.start()
    try:
        # 'a' is unhealthy, but a successful query is still possible
        # because StubBackend.fail only affects health_check / queries
        # while .fail is True. To make this case sensible, flip .fail
        # off for the actual data path:
        a.fail = False
        fan = FanoutBackend([a, b], health_cache=cache)
        out = await fan.get_labels("t1", instance="a")
        assert out == ["job"]
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
