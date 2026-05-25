"""日志后端的抽象接口。

所有具体的后端（Loki、Elasticsearch 等）都必须实现这个接口。MCP
工具层只与 ``LogBackend`` 交互，对具体后端无感知。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Generic, List, Optional, TypeVar


@dataclass
class LogEntry:
    """跨后端的统一日志条目模型。

    ``cluster`` 字段仅在数据来自多集群扇出后端（例如多个 Loki 实例）
    时设置；单集群部署下保持为 ``None``。
    """

    timestamp: datetime  # UTC，带时区
    labels: Dict[str, str]
    line: str
    tenant: Optional[str] = None
    cluster: Optional[str] = None


T = TypeVar("T")


@dataclass
class TenantQueryResult(Generic[T]):
    """单租户操作的结果（每个租户可能各自失败）。

    在多租户扇出场景下使用，让调用方能够区分"没有数据"和"该租户出错了"。
    ``cluster_errors`` 用于携带扇出后端中的"部分集群失败"信息——例如
    多 Loki 中某个实例挂了但其他还成功了。``cluster_warnings`` 用于
    携带"查询成功但需要用户注意"的信息，例如某个 Loki 集群因为自身
    ``max_entries_limit_per_query`` 较低而降级重试。
    """

    tenant: str
    data: Optional[T] = None
    error: Optional[str] = None
    cluster_errors: Dict[str, str] = None  # type: ignore[assignment]
    cluster_warnings: Dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.cluster_errors is None:
            self.cluster_errors = {}
        if self.cluster_warnings is None:
            self.cluster_warnings = {}

    @property
    def ok(self) -> bool:
        return self.error is None


class LogBackend(ABC):
    """日志后端的抽象基类。

    具体后端在 ``__aenter__`` / ``setup`` 调用一次之后，必须能在并发
    请求间安全共享。实现通常会包一个长生命周期的
    ``httpx.AsyncClient``。

    多集群扇出实现（例如 ``FanoutBackend``）通过填充调用方传入的
    可选 ``cluster_errors`` 字典来上报"部分集群失败"信息；单集群
    后端忽略该参数。
    """

    name: str = "base"

    @property
    @abstractmethod
    def tenants(self) -> List[str]:
        """已配置的租户列表（至少一个）。"""

    @abstractmethod
    async def __aenter__(self) -> "LogBackend":
        """打开底层资源（HTTP client 等）。"""

    @abstractmethod
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """关闭底层资源。"""

    @abstractmethod
    async def health_check(self) -> Dict[str, Any]:
        """返回后端健康信息。

        字典中至少包含 ``status``（``healthy`` / ``degraded`` /
        ``unhealthy``）和 ``backend``（后端名）两个字段。允许带上各
        后端自定义的额外字段。
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
        instance: Optional[str] = None,
        cluster_errors: Optional[Dict[str, str]] = None,
        cluster_warnings: Optional[Dict[str, str]] = None,
    ) -> List[LogEntry]:
        """查询单个租户的日志。

        失败时实现应抛出 ``BackendQueryError`` / ``ValidationError`` /
        ``BackendHTTPError``，而不是返回空结果。``cluster_errors`` 不为
        ``None`` 时，多集群后端应当把"未导致整体失败的部分集群错误"
        以 ``{cluster_id: error_message}`` 形式填进去。
        ``cluster_warnings`` 不为 ``None`` 时，多集群后端可以把"查询
        最终成功但有精度 / 完整性提示"的信息填进去。

        ``instance``：可选的集群标识（``host:port`` 或主机名）。多集群
        后端在该参数提供时只会查询对应集群；单集群后端会校验它是否
        与自身集群 id 一致（不一致则抛 ``ValidationError``），匹配时
        忽略。
        """

    @abstractmethod
    async def get_labels(
        self,
        tenant: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        instance: Optional[str] = None,
        cluster_errors: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        """列出某租户在（可选）时间窗内出现过的所有标签名。"""

    @abstractmethod
    async def get_label_values(
        self,
        tenant: str,
        label: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        instance: Optional[str] = None,
        cluster_errors: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        """列出某租户在（可选）时间窗内 ``label`` 的所有取值。"""
