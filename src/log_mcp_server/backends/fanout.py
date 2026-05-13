"""Fan-out backend.

Wraps multiple homogeneous ``LogBackend`` instances and presents them as
one logical backend. Used when multiple Loki instances (or other clusters
of the same backend type) need to be queried in parallel and aggregated.

When a ``HealthCache`` is provided, only clusters marked as healthy are
dispatched to for data queries (``query_logs``, ``get_labels``,
``get_label_values``).  The ``health_check`` method always probes every
cluster regardless.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import structlog

from ..utils.errors import ValidationError
from .base import LogBackend, LogEntry

if TYPE_CHECKING:
    from .health_cache import HealthCache

logger = structlog.get_logger(__name__)

_PER_CLUSTER_TIMEOUT = 30.0


class FanoutBackend(LogBackend):
    """Run the same query against several backends in parallel.

    All sub-backends are expected to share the same ``tenants`` list and
    backend ``name`` (e.g. several Loki instances of the same kind).
    """

    def __init__(
        self,
        backends: List[LogBackend],
        health_cache: Optional["HealthCache"] = None,
    ):
        if not backends:
            raise ValueError("FanoutBackend requires at least one sub-backend")
        self._backends = backends
        self._health_cache = health_cache
        self.name = backends[0].name

    @property
    def tenants(self) -> List[str]:
        return self._backends[0].tenants

    async def __aenter__(self) -> "FanoutBackend":
        await asyncio.gather(*(b.__aenter__() for b in self._backends))
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await asyncio.gather(
            *(b.__aexit__(exc_type, exc_val, exc_tb) for b in self._backends),
            return_exceptions=True,
        )

    # ---- helpers ----------------------------------------------------------
    @staticmethod
    def _cluster_id(backend: LogBackend) -> str:
        return getattr(backend, "cluster_id", None) or backend.name

    def _active_backends(self) -> List[LogBackend]:
        """Return backends to dispatch data queries to.

        Consults the ``HealthCache`` if available; otherwise returns all.
        """
        if self._health_cache is not None:
            return self._health_cache.healthy_backends()
        return self._backends

    @property
    def cluster_ids(self) -> List[str]:
        """All known cluster ids (regardless of health)."""
        return [self._cluster_id(b) for b in self._backends]

    def _resolve_instance(
        self, active: List[LogBackend], instance: Optional[str]
    ) -> List[LogBackend]:
        """Filter ``active`` to a single backend matching ``instance``.

        Returns ``active`` unchanged when ``instance`` is None.  Raises
        ``ValidationError`` when ``instance`` does not match any
        configured cluster.  Logs a warning (and still returns the match)
        when the requested instance is currently unhealthy.
        """
        if not instance:
            return active
        match = next(
            (b for b in self._backends if self._cluster_id(b) == instance),
            None,
        )
        if match is None:
            raise ValidationError(
                f"Unknown instance {instance!r}. "
                f"Configured: {', '.join(self.cluster_ids)}"
            )
        if match not in active:
            logger.warning(
                "Querying explicitly requested instance that is not "
                "currently marked healthy",
                instance=instance,
            )
        return [match]

    async def _run(self, backend: LogBackend, coro_factory):
        try:
            return await asyncio.wait_for(
                coro_factory(backend), timeout=_PER_CLUSTER_TIMEOUT
            )
        except asyncio.TimeoutError as e:
            raise TimeoutError(f"timeout after {_PER_CLUSTER_TIMEOUT:.0f}s") from e

    def _record_failures(
        self,
        active: List[LogBackend],
        results: List[Any],
        cluster_errors: Optional[Dict[str, str]],
        op: str,
        **log_extras: Any,
    ) -> None:
        """Log per-cluster failures and record them into ``cluster_errors``."""
        for backend, res in zip(active, results):
            if not isinstance(res, Exception):
                continue
            cid = self._cluster_id(backend)
            err = f"{type(res).__name__}: {res}"
            logger.warning(
                "Cluster operation failed",
                op=op,
                cluster=cid,
                error=err,
                **log_extras,
            )
            if cluster_errors is not None:
                cluster_errors[cid] = err

    # ---- health -----------------------------------------------------------
    async def health_check(self) -> Dict[str, Any]:
        results = await asyncio.gather(
            *(b.health_check() for b in self._backends),
            return_exceptions=True,
        )

        clusters: List[Dict[str, Any]] = []
        any_healthy = False
        any_unhealthy = False
        timezone: Optional[str] = None
        current_time: Optional[str] = None

        for backend, info in zip(self._backends, results):
            cid = self._cluster_id(backend)
            if isinstance(info, Exception):
                clusters.append(
                    {"id": cid, "status": "unhealthy", "error": str(info)}
                )
                any_unhealthy = True
                continue
            timezone = timezone or info.get("timezone")
            current_time = current_time or info.get("current_time")
            sub = info.get("clusters") or []
            if sub:
                clusters.extend(sub)
            else:
                clusters.append(
                    {"id": cid, "status": info.get("status", "unknown")}
                )
            sub_status = info.get("status")
            if sub_status == "healthy":
                any_healthy = True
            elif sub_status == "degraded":
                # A degraded sub-backend has at least one healthy cluster
                # under it: count both signals so the aggregate is also
                # degraded rather than collapsing to unhealthy.
                any_healthy = True
                any_unhealthy = True
            else:
                any_unhealthy = True

        if any_healthy and not any_unhealthy:
            status = "healthy"
        elif any_healthy and any_unhealthy:
            status = "degraded"
        else:
            status = "unhealthy"

        return {
            "backend": self.name,
            "status": status,
            "current_time": current_time,
            "timezone": timezone,
            "clusters": clusters,
        }

    # ---- queries ----------------------------------------------------------
    async def query_logs(
        self,
        query: str,
        tenant: str,
        start: datetime,
        end: datetime,
        limit: int,
        direction: str,
        instance: Optional[str] = None,
        cluster_errors: Optional[Dict[str, str]] = None,
    ) -> List[LogEntry]:
        active = self._resolve_instance(self._active_backends(), instance)

        async def call(backend: LogBackend) -> List[LogEntry]:
            return await backend.query_logs(
                query=query,
                tenant=tenant,
                start=start,
                end=end,
                limit=limit,
                direction=direction,
            )

        results = await asyncio.gather(
            *(self._run(b, call) for b in active),
            return_exceptions=True,
        )
        self._record_failures(
            active, results, cluster_errors, op="query_logs", tenant=tenant
        )

        merged: List[LogEntry] = []
        for backend, res in zip(active, results):
            if isinstance(res, Exception):
                continue
            cid = self._cluster_id(backend)
            for entry in res:
                if entry.cluster is None:
                    entry.cluster = cid
                merged.append(entry)

        merged.sort(
            key=lambda e: e.timestamp,
            reverse=(direction == "backward"),
        )
        return merged[:limit]

    async def get_labels(
        self,
        tenant: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        instance: Optional[str] = None,
        cluster_errors: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        active = self._resolve_instance(self._active_backends(), instance)

        async def call(backend: LogBackend):
            return await backend.get_labels(tenant, start=start, end=end)

        results = await asyncio.gather(
            *(self._run(b, call) for b in active),
            return_exceptions=True,
        )
        self._record_failures(
            active, results, cluster_errors, op="get_labels", tenant=tenant
        )

        seen: set[str] = set()
        for res in results:
            if isinstance(res, Exception):
                continue
            seen.update(res or [])
        return sorted(seen)

    async def get_label_values(
        self,
        tenant: str,
        label: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        instance: Optional[str] = None,
        cluster_errors: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        active = self._resolve_instance(self._active_backends(), instance)

        async def call(backend: LogBackend):
            return await backend.get_label_values(
                tenant, label, start=start, end=end
            )

        results = await asyncio.gather(
            *(self._run(b, call) for b in active),
            return_exceptions=True,
        )
        self._record_failures(
            active,
            results,
            cluster_errors,
            op="get_label_values",
            tenant=tenant,
            label=label,
        )

        seen: set[str] = set()
        for res in results:
            if isinstance(res, Exception):
                continue
            seen.update(res or [])
        return sorted(seen)
