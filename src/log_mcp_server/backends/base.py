"""Abstract log backend interface.

All concrete backends (Loki, Elasticsearch, ...) must implement this
interface. The MCP tools layer talks only to ``LogBackend`` and is
backend-agnostic.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Generic, List, Optional, TypeVar


@dataclass
class LogEntry:
    """Unified log entry across all backends.

    ``cluster`` is set when the entry came from a multi-cluster fan-out
    backend (e.g. multiple Loki instances).  ``None`` for single-cluster
    deployments.
    """

    timestamp: datetime  # UTC, tz-aware
    labels: Dict[str, str]
    line: str
    tenant: Optional[str] = None
    cluster: Optional[str] = None


T = TypeVar("T")


@dataclass
class TenantQueryResult(Generic[T]):
    """Result of a per-tenant operation that may individually fail.

    Used by multi-tenant fan-out so the caller can distinguish "no data"
    from "tenant errored".  ``cluster_errors`` carries per-cluster
    failures from a fan-out backend (e.g. one of several Loki instances
    failed but others succeeded).
    """

    tenant: str
    data: Optional[T] = None
    error: Optional[str] = None
    cluster_errors: Dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.cluster_errors is None:
            self.cluster_errors = {}

    @property
    def ok(self) -> bool:
        return self.error is None


class LogBackend(ABC):
    """Abstract log backend.

    Concrete backends MUST be safe to share across concurrent requests
    after ``__aenter__``/``setup`` has been called once. Implementations
    typically wrap a single long-lived ``httpx.AsyncClient``.

    Multi-cluster fan-out implementations (e.g. ``FanoutBackend``) report
    per-cluster partial failures by populating the optional
    ``cluster_errors`` dict argument that the caller may pass in.
    Single-cluster backends ignore the argument.
    """

    name: str = "base"

    @property
    @abstractmethod
    def tenants(self) -> List[str]:
        """Configured tenant identifiers (at least one)."""

    @abstractmethod
    async def __aenter__(self) -> "LogBackend":
        """Open underlying resources (HTTP client, etc.)."""

    @abstractmethod
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Close underlying resources."""

    @abstractmethod
    async def health_check(self) -> Dict[str, Any]:
        """Return health information about the backend.

        The dict must include at least ``status`` (``healthy`` /
        ``degraded`` / ``unhealthy``) and ``backend`` (backend name).
        Additional backend-specific fields are allowed.
        """

    @abstractmethod
    async def query_logs(
        self,
        query: str,
        tenant: str,
        start: datetime,
        end: datetime,
        limit: int,
        direction: str,
        cluster_errors: Optional[Dict[str, str]] = None,
    ) -> List[LogEntry]:
        """Query logs for a single tenant.

        Implementations should raise ``BackendQueryError`` /
        ``ValidationError`` / ``BackendHTTPError`` rather than returning
        empty results on failure.  When ``cluster_errors`` is provided,
        multi-cluster backends should populate it with
        ``{cluster_id: error_message}`` for partial failures that did
        not abort the whole call.
        """

    @abstractmethod
    async def get_labels(
        self,
        tenant: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        cluster_errors: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        """List label names for a tenant in the (optional) time range."""

    @abstractmethod
    async def get_label_values(
        self,
        tenant: str,
        label: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        cluster_errors: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        """List values of ``label`` for a tenant in the (optional) range."""
