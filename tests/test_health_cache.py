"""Tests for the HealthCache."""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from log_mcp_server.backends.base import LogBackend, LogEntry
from log_mcp_server.backends.health_cache import HealthCache


class StubBackend(LogBackend):
    """Minimal backend stub for health-cache tests."""

    name = "loki"

    def __init__(self, cluster_id: str, health: str = "healthy"):
        self.cluster_id = cluster_id
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
            raise RuntimeError(f"{self.cluster_id} is down")
        return {
            "backend": self.name,
            "status": self._health,
            "current_time": "2025-01-01T00:00:00+00:00",
            "timezone": "UTC",
        }

    async def query_logs(self, *a, **kw) -> List[LogEntry]:
        return []

    async def get_labels(self, *a, **kw) -> List[str]:
        return []

    async def get_label_values(self, *a, **kw) -> List[str]:
        return []


@pytest.mark.asyncio
async def test_healthy_backends_returns_healthy_only():
    a = StubBackend("a", health="healthy")
    b = StubBackend("b", health="healthy")
    b.fail = True
    cache = HealthCache([a, b], interval=9999, probe_timeout=2)
    await cache.start()
    try:
        healthy = cache.healthy_backends()
        assert len(healthy) == 1
        assert healthy[0].cluster_id == "a"
    finally:
        await cache.stop()


@pytest.mark.asyncio
async def test_all_unhealthy_falls_back_to_all():
    a = StubBackend("a")
    b = StubBackend("b")
    a.fail = True
    b.fail = True
    cache = HealthCache([a, b], interval=9999, probe_timeout=2)
    await cache.start()
    try:
        healthy = cache.healthy_backends()
        assert len(healthy) == 2
    finally:
        await cache.stop()


@pytest.mark.asyncio
async def test_degraded_counts_as_healthy():
    a = StubBackend("a", health="degraded")
    b = StubBackend("b")
    b.fail = True
    cache = HealthCache([a, b], interval=9999, probe_timeout=2)
    await cache.start()
    try:
        healthy = cache.healthy_backends()
        ids = [b.cluster_id for b in healthy]
        assert "a" in ids
        assert "b" not in ids
    finally:
        await cache.stop()


@pytest.mark.asyncio
async def test_get_status_returns_all_clusters():
    a = StubBackend("a", health="healthy")
    b = StubBackend("b")
    b.fail = True
    cache = HealthCache([a, b], interval=9999, probe_timeout=2)
    await cache.start()
    try:
        status = cache.get_status()
        assert "a" in status and "b" in status
        assert status["a"].healthy is True
        assert status["b"].healthy is False
    finally:
        await cache.stop()


@pytest.mark.asyncio
async def test_start_stop_idempotent():
    a = StubBackend("a")
    cache = HealthCache([a], interval=9999, probe_timeout=2)
    await cache.start()
    await cache.stop()
    await cache.stop()


@pytest.mark.asyncio
async def test_refresh_updates_after_recovery():
    a = StubBackend("a")
    a.fail = True
    cache = HealthCache([a], interval=9999, probe_timeout=2)
    await cache.start()
    try:
        assert cache.healthy_backends()[0].cluster_id == "a"  # fallback
        assert cache.get_status()["a"].healthy is False

        a.fail = False
        await cache._refresh()
        assert cache.get_status()["a"].healthy is True
        healthy = cache.healthy_backends()
        assert len(healthy) == 1
        assert healthy[0].cluster_id == "a"
    finally:
        await cache.stop()
