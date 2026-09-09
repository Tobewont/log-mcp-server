"""``LogBackend`` 的 Loki 实现。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse

import structlog

from ...config import LogConfig
from ...utils.errors import (
    BackendQueryError,
    ValidationError,
)
from ...utils.time_utils import from_unix_ns, now_utc, to_unix_ns
from ..base import LogBackend, LogEntry
from .auth import validate_tenant
from .http_client import LokiHTTPClient

logger = structlog.get_logger(__name__)


_VALID_DIRECTIONS = ("forward", "backward")

# Prometheus / Loki 标签名的合法字符集。分组计数会把标签名直接拼进
# LogQL 表达式，所以必须先校验，避免表达式注入。
_LABEL_NAME_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


def _derive_cluster_id(addr: str) -> str:
    """Best-effort cluster id from a Loki URL (host:port)."""
    try:
        u = urlparse(addr)
        host = u.hostname or addr
        if u.port:
            return f"{host}:{u.port}"
        return host
    except Exception:
        return addr


class LokiBackend(LogBackend):
    """Grafana Loki backend (single Loki instance).

    Owns a long-lived HTTP client; safe to share between concurrent calls
    once entered as a context manager. For multiple Loki instances, wrap
    several ``LokiBackend`` instances in a ``FanoutBackend``.
    """

    name = "loki"

    def __init__(self, config: LogConfig, addr: Optional[str] = None):
        self.addr = addr or config.get_loki_addrs()[0]
        self.cluster_id = _derive_cluster_id(self.addr)
        self.config = config
        self.http = LokiHTTPClient(config, addr_override=self.addr)

    @property
    def tenants(self) -> List[str]:
        return self.config.get_tenant_list()

    async def __aenter__(self) -> "LokiBackend":
        await self.http.open()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.http.close()

    # ---- health -----------------------------------------------------------
    async def health_check(self) -> Dict[str, Any]:
        cluster: Dict[str, Any] = {
            "id": self.cluster_id,
            "server_addr": self.addr,
        }
        try:
            info = await self.http.get("/loki/api/v1/status/buildinfo", retries=1)
            cluster["status"] = "healthy"
            cluster["version"] = info.get("version", "unknown")
        except Exception as e:
            logger.error(
                "Loki health check failed", cluster=self.cluster_id, error=str(e)
            )
            cluster["status"] = "unhealthy"
            cluster["error"] = str(e)
        return {
            "backend": self.name,
            "status": cluster["status"],
            "current_time": now_utc().isoformat(),
            "timezone": self.config.timezone,
            "clusters": [cluster],
        }

    # ---- queries ----------------------------------------------------------
    def _validate_limit(self, limit: Optional[int]) -> int:
        if limit is None:
            return self.config.default_limit
        if limit <= 0:
            raise ValidationError("Limit must be a positive integer")
        if limit > self.config.max_limit:
            raise ValidationError(
                f"Limit {limit} exceeds maximum {self.config.max_limit}"
            )
        return limit

    @staticmethod
    def _validate_direction(direction: str) -> str:
        d = (direction or "backward").lower()
        if d not in _VALID_DIRECTIONS:
            raise ValidationError(
                f"Invalid direction {direction!r}; must be one of "
                f"{_VALID_DIRECTIONS}"
            )
        return d

    def _check_instance(self, instance: Optional[str]) -> None:
        """Reject queries targeting a different cluster id.

        Single-cluster backend: when the caller pinned the query to a
        specific instance, it must match this backend's cluster id.
        """
        if instance and instance != self.cluster_id:
            raise ValidationError(
                f"Unknown instance {instance!r}. "
                f"This backend serves cluster {self.cluster_id!r}."
            )

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
        cluster_warnings: Optional[Dict[str, str]] = None,
    ) -> List[LogEntry]:
        # Single-cluster backend: ``cluster_errors`` / ``cluster_warnings``
        # are ignored. Errors are surfaced by raising instead.
        del cluster_errors
        del cluster_warnings
        self._check_instance(instance)
        validate_tenant(tenant)
        if not query or not query.strip():
            raise ValidationError("Query cannot be empty")

        params = {
            "query": query,
            "limit": str(self._validate_limit(limit)),
            "direction": self._validate_direction(direction),
            "start": str(to_unix_ns(start)),
            "end": str(to_unix_ns(end)),
        }
        response = await self.http.get(
            "/loki/api/v1/query_range",
            params=params,
            tenant=tenant,
        )

        if response.get("status") != "success":
            raise BackendQueryError(
                f"Loki query failed: {response.get('error') or response}"
            )

        data = response.get("data", {})
        result_type = data.get("resultType", "")
        if result_type and result_type != "streams":
            # LogQL metric queries (vector/matrix) are not part of this
            # log-focused server.  Surface a clear error instead of
            # silently returning empty results.
            raise ValidationError(
                f"Query produced a {result_type!r} result. This server only "
                "supports log-stream queries; metric expressions like rate() "
                "are not allowed."
            )

        entries: List[LogEntry] = []
        for stream in data.get("result", []):
            stream_labels = stream.get("stream", {}) or {}
            for ts_ns, line in stream.get("values", []) or []:
                try:
                    ts = from_unix_ns(ts_ns)
                except (ValueError, OSError) as e:
                    logger.warning(
                        "Failed to parse Loki timestamp",
                        ts=ts_ns,
                        error=str(e),
                    )
                    continue
                entries.append(
                    LogEntry(
                        timestamp=ts,
                        labels=stream_labels,
                        line=line,
                        tenant=tenant,
                        cluster=self.cluster_id,
                    )
                )
        return entries

    async def get_labels(
        self,
        tenant: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        instance: Optional[str] = None,
        cluster_errors: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        del cluster_errors
        self._check_instance(instance)
        validate_tenant(tenant)
        params: Dict[str, str] = {}
        if start:
            params["start"] = str(to_unix_ns(start))
        if end:
            params["end"] = str(to_unix_ns(end))

        response = await self.http.get(
            "/loki/api/v1/labels",
            params=params or None,
            tenant=tenant,
        )
        if response.get("status") != "success":
            raise BackendQueryError(
                f"Failed to list labels: {response.get('error') or response}"
            )
        return list(response.get("data") or [])

    async def get_label_values(
        self,
        tenant: str,
        label: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        instance: Optional[str] = None,
        cluster_errors: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        del cluster_errors
        self._check_instance(instance)
        validate_tenant(tenant)
        if not label or not label.strip():
            raise ValidationError("Label name cannot be empty")

        params: Dict[str, str] = {}
        if start:
            params["start"] = str(to_unix_ns(start))
        if end:
            params["end"] = str(to_unix_ns(end))

        response = await self.http.get(
            f"/loki/api/v1/label/{quote(label, safe='')}/values",
            params=params or None,
            tenant=tenant,
        )
        if response.get("status") != "success":
            raise BackendQueryError(
                f"Failed to list label values: {response.get('error') or response}"
            )
        return list(response.get("data") or [])

    async def count_logs(
        self,
        query: str,
        tenant: str,
        start: datetime,
        end: datetime,
        instance: Optional[str] = None,
        cluster_errors: Optional[Dict[str, str]] = None,
    ) -> int:
        # 单集群后端：忽略 cluster_errors，出错直接抛出。
        del cluster_errors
        self._check_instance(instance)
        validate_tenant(tenant)
        if not query or not query.strip():
            raise ValidationError("Query cannot be empty")

        # 用户传入的是日志选择器（不含聚合），这里包一层
        # sum(count_over_time(<selector>[<range>])) 做即时计数查询。
        # range 用 start~end 的秒数（至少 1s），保证覆盖整个窗口。
        span_seconds = max(1, int((end - start).total_seconds()))
        count_expr = f"sum(count_over_time({query.strip()}[{span_seconds}s]))"
        params = {
            "query": count_expr,
            "time": str(to_unix_ns(end)),
        }
        response = await self.http.get(
            "/loki/api/v1/query",
            params=params,
            tenant=tenant,
        )
        if response.get("status") != "success":
            raise BackendQueryError(
                f"Loki count query failed: {response.get('error') or response}"
            )

        data = response.get("data", {})
        result_type = data.get("resultType", "")
        if result_type != "vector":
            raise BackendQueryError(
                f"Loki count query returned unexpected resultType "
                f"{result_type!r}; expected 'vector'."
            )
        results = data.get("result") or []
        if not results:
            return 0
        total = 0
        for sample in results:
            value = sample.get("value") or []
            if len(value) >= 2:
                try:
                    total += int(float(value[1]))
                except (TypeError, ValueError):
                    continue
        return total

    async def count_logs_grouped(
        self,
        query: str,
        tenant: str,
        start: datetime,
        end: datetime,
        by_label: str,
        instance: Optional[str] = None,
        cluster_errors: Optional[Dict[str, str]] = None,
    ) -> Optional[Dict[str, int]]:
        """把分组计数下推到 Loki：``sum by (<label>) (count_over_time(...))``。

        ``by_label`` 必须是 Loki 侧真实存在的标签（例如 Loki 3.x 自动
        附加的 ``detected_level``）。查询成功但没有任何 series 时返回
        空字典；Loki 报错（多为"标签不存在"或表达式不合法）时返回
        ``None``，交由工具层退化为抽样估算。
        """
        del cluster_errors
        self._check_instance(instance)
        validate_tenant(tenant)
        if not query or not query.strip():
            raise ValidationError("Query cannot be empty")
        if not by_label or not by_label.strip():
            raise ValidationError("by_label cannot be empty")
        label = by_label.strip()
        if not _LABEL_NAME_RE.fullmatch(label):
            raise ValidationError(
                f"Invalid label name {by_label!r} for grouped count "
                "(expected a Prometheus/Loki label identifier)"
            )

        span_seconds = max(1, int((end - start).total_seconds()))
        count_expr = (
            f"sum by ({label}) (count_over_time({query.strip()}[{span_seconds}s]))"
        )
        try:
            response = await self.http.get(
                "/loki/api/v1/query",
                params={"query": count_expr, "time": str(to_unix_ns(end))},
                tenant=tenant,
            )
        except Exception as e:  # noqa: BLE001 — 无法下推时退化为抽样
            logger.info(
                "Grouped count push-down failed; caller should fall back to sampling",
                label=label,
                error=str(e),
            )
            return None
        if response.get("status") != "success":
            return None

        data = response.get("data", {})
        if data.get("resultType") != "vector":
            return None
        grouped: Dict[str, int] = {}
        for sample in data.get("result") or []:
            metric = sample.get("metric") or {}
            key = metric.get(label) or "(none)"
            value = sample.get("value") or []
            if len(value) < 2:
                continue
            try:
                grouped[key] = grouped.get(key, 0) + int(float(value[1]))
            except (TypeError, ValueError):
                continue
        return grouped
