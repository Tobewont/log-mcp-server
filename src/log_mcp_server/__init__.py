"""log-mcp-server。

基于 FastMCP 的 Model Context Protocol 日志服务，提供日志查询、标签
浏览和日志下载等工具。

目前支持 Grafana Loki 作为日志后端，并预留可扩展的后端接口，便于后续
接入 Elasticsearch、CloudWatch、ClickHouse 等其他日志系统。
"""

__version__ = "1.0.0"
__description__ = "基于 FastMCP 的日志 MCP 服务，支持可插拔后端（Loki 等）"

from .backends.base import LogBackend, LogEntry
from .config import LogConfig

__all__ = [
    "LogConfig",
    "LogBackend",
    "LogEntry",
]
