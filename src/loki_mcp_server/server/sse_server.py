"""
SSE (Server-Sent Events) server implementation for MCP.
"""

import asyncio
from typing import Any, Dict, Optional

from mcp.server import NotificationOptions

from ..config import LokiConfig
from ..utils.logging import setup_logging
from .base_server import BaseServer

logger = setup_logging(__name__)


class SSEServer(BaseServer):
    """SSE server implementation for MCP over HTTP."""
    
    def __init__(self, config: LokiConfig):
        """Initialize SSE server."""
        super().__init__(config)
        self.host = getattr(config, 'server_host', '0.0.0.0')
        self.port = getattr(config, 'server_port', 8080)
        self._server_task: Optional[asyncio.Task] = None
    
    async def start(self) -> None:
        """Start the SSE server."""
        # Note: This is a placeholder implementation
        # In a real implementation, you would need to integrate with a proper HTTP server
        # that can handle SSE connections and route MCP messages
        logger.info(
            "SSE server mode initialized", 
            host=self.host, 
            port=self.port,
            loki_addr=self.config.addr
        )
        
        # For now, we'll run indefinitely
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            logger.info("SSE server task cancelled")
            raise
    
    async def stop(self) -> None:
        """Stop the SSE server."""
        logger.info("Stopping SSE server")
        
        if self._server_task:
            self._server_task.cancel()
            try:
                await self._server_task
            except asyncio.CancelledError:
                pass
        
        logger.info("SSE server stopped")
    
    async def health_check(self) -> Dict[str, Any]:
        """Perform health check."""
        try:
            # Test Loki connection
            await self.loki_client.health_check()
            
            return {
                "status": "healthy",
                "server_mode": "sse",
                "loki_addr": self.config.addr,
                "listen_address": f"{self.host}:{self.port}"
            }
        except Exception as e:
            logger.error("Health check failed", error=str(e))
            return {
                "status": "unhealthy",
                "server_mode": "sse",
                "error": str(e),
                "loki_addr": self.config.addr,
                "listen_address": f"{self.host}:{self.port}"
            }
    
    def get_server_info(self) -> Dict[str, Any]:
        """Get server information."""
        return {
            "name": "Loki MCP Server",
            "version": "1.0.0",
            "mode": "sse",
            "endpoints": {
                "sse": f"http://{self.host}:{self.port}/sse",
                "health": f"http://{self.host}:{self.port}/health"
            },
            "loki_config": {
                "addr": self.config.addr,
                "org_id": getattr(self.config, 'org_id', None)
            }
        }
