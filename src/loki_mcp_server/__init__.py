"""
Loki MCP Server - A FastMCP-based server for Grafana Loki integration.

This package provides a modern Python-based MCP server using the FastMCP framework
that enables AI assistants to query, analyze, and process log data stored in 
Grafana Loki. Features automatic mode detection (stdio/HTTP), simplified tool 
management, and enhanced debugging capabilities.
"""

__version__ = "1.0.0"
__author__ = "Loki MCP Server Team"
__description__ = "FastMCP-based server for querying Loki logs with stdio and HTTP/SSE support"

# Main exports
from .config import LokiConfig
from .client.loki_client import LokiClient

# Lazy import to avoid circular import issues when running as module
def _get_server_classes():
    from .main import LokiMCPServer, cli_main
    return LokiMCPServer, cli_main

__all__ = [
    "LokiConfig",
    "LokiClient",
    "_get_server_classes",
]
