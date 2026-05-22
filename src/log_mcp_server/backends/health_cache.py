"""为多集群后端提供的周期性健康检查缓存。

按可配置间隔通过 ``backend.health_check()``（即 Loki 的
``/loki/api/v1/status/buildinfo``）探测每个集群，并把结果缓存下来。
``FanoutBackend`` 在派发查询前会先查这个缓存，从而立即跳过不健康
的集群，避免每次查询都被超时拖慢。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

import structlog

from .base import LogBackend

logger = structlog.get_logger(__name__)


@dataclass
class ClusterHealth:
    cluster_id: str
    healthy: bool
    checked_at: datetime
    detail: str = ""


class HealthCache:
    """Periodically probes cluster health and caches results.

    Usage::

        cache = HealthCache(backends, interval=300, probe_timeout=5)
        await cache.start()   # initial probe + background loop
        ...
        active = cache.healthy_backends()
        ...
        await cache.stop()
    """

    def __init__(
        self,
        backends: List[LogBackend],
        interval: float = 300.0,
        probe_timeout: float = 5.0,
    ):
        self._backends = backends
        self._interval = interval
        self._probe_timeout = probe_timeout
        self._status: Dict[str, ClusterHealth] = {}
        self._task: Optional[asyncio.Task] = None

    @staticmethod
    def _cluster_id(backend: LogBackend) -> str:
        return getattr(backend, "cluster_id", None) or backend.name

    # -- public API ---------------------------------------------------------

    async def start(self) -> None:
        """Run the initial probe and start the background refresh loop."""
        await self._refresh()
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "Health cache started",
            interval=self._interval,
            clusters=len(self._backends),
        )

    async def stop(self) -> None:
        """Cancel the background refresh task."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("Health cache stopped")

    def _is_healthy(self, backend: LogBackend) -> bool:
        """Return True if the backend has no recorded entry yet (optimistic
        before the first probe completes) or its latest probe was healthy.
        """
        status = self._status.get(self._cluster_id(backend))
        return status is None or status.healthy

    def healthy_backends(self) -> List[LogBackend]:
        """Return backends currently marked as healthy.

        If **all** backends are unhealthy the full list is returned as a
        fallback (better to try and fail visibly than return nothing).
        """
        healthy = [b for b in self._backends if self._is_healthy(b)]
        if not healthy:
            logger.warning(
                "No healthy clusters in cache, falling back to all",
                total=len(self._backends),
            )
            return list(self._backends)
        return healthy

    def get_status(self) -> Dict[str, ClusterHealth]:
        """Snapshot of the current health state (for diagnostics)."""
        return dict(self._status)

    # -- internals ----------------------------------------------------------

    async def _probe_one(self, backend: LogBackend) -> ClusterHealth:
        cid = self._cluster_id(backend)
        try:
            info = await asyncio.wait_for(
                backend.health_check(), timeout=self._probe_timeout
            )
            status = info.get("status", "unknown")
            healthy = status in ("healthy", "degraded")
            return ClusterHealth(
                cluster_id=cid,
                healthy=healthy,
                checked_at=datetime.now(timezone.utc),
                detail=status,
            )
        except Exception as exc:
            return ClusterHealth(
                cluster_id=cid,
                healthy=False,
                checked_at=datetime.now(timezone.utc),
                detail=f"{type(exc).__name__}: {exc}",
            )

    async def _refresh(self) -> None:
        results = await asyncio.gather(
            *(self._probe_one(b) for b in self._backends),
            return_exceptions=True,
        )
        for b, result in zip(self._backends, results):
            cid = self._cluster_id(b)
            if isinstance(result, Exception):
                self._status[cid] = ClusterHealth(
                    cluster_id=cid,
                    healthy=False,
                    checked_at=datetime.now(timezone.utc),
                    detail=f"{type(result).__name__}: {result}",
                )
            else:
                self._status[cid] = result

        healthy_ids = [cid for cid, s in self._status.items() if s.healthy]
        unhealthy_ids = [cid for cid, s in self._status.items() if not s.healthy]
        logger.info(
            "Health cache refreshed",
            healthy=healthy_ids,
            unhealthy=unhealthy_ids,
        )

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._interval)
                await self._refresh()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Health cache refresh failed")
