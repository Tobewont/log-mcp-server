"""Log MCP Server.

A FastMCP-based Model Context Protocol server providing log query tools.

Currently supports Grafana Loki as the log backend, with an extensible
backend interface for adding other log systems (Elasticsearch, CloudWatch,
ClickHouse, ...) in the future.
"""

__version__ = "1.0.0"
__description__ = "FastMCP-based log MCP server with pluggable backends (Loki, ...)"

from .backends.base import LogBackend, LogEntry
from .config import LogConfig

__all__ = [
    "LogConfig",
    "LogBackend",
    "LogEntry",
]
