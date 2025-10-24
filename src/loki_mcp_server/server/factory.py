"""
Server factory for creating MCP server instances based on configuration.
"""

from typing import Union

from ..config import LokiConfig
from ..utils.logging import setup_logging
from .base_server import BaseServer
from .http_server import HTTPServer
from .stdio_server import StdioServer

logger = setup_logging(__name__)


class ServerFactory:
    """Factory for creating MCP server instances."""
    
    @staticmethod
    def create_server(config: LokiConfig) -> Union[BaseServer, HTTPServer]:
        """Create a server instance based on configuration."""
        
        if config.server_mode == "sse":
            logger.info("Creating SSE server with HTTP wrapper")
            return HTTPServer(config)
        elif config.server_mode == "stdio":
            logger.info("Creating stdio server")
            return StdioServer(config)
        else:
            raise ValueError(f"Unknown server mode: {config.server_mode}")
    
    @staticmethod
    def get_supported_modes() -> list[str]:
        """Get list of supported server modes."""
        return ["stdio", "sse"]
