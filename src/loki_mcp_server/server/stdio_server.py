"""
Stdio server implementation for MCP.
"""

import mcp.server.stdio
from mcp.server import NotificationOptions

from ..config import LokiConfig
from ..utils.logging import setup_logging
from .base_server import BaseServer

logger = setup_logging(__name__)


class StdioServer(BaseServer):
    """Stdio server implementation for MCP."""
    
    def __init__(self, config: LokiConfig):
        """Initialize stdio server."""
        super().__init__(config)
    
    async def start(self) -> None:
        """Start the stdio server."""
        logger.info(
            "Starting stdio server", 
            loki_addr=self.config.addr
        )
        
        try:
            async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
                logger.info("Stdio server started successfully")
                
                await self.server.run(
                    read_stream,
                    write_stream,
                    NotificationOptions(),
                )
                
        except Exception as e:
            logger.error("Stdio server failed", error=str(e), exc_info=True)
            raise
    
    async def stop(self) -> None:
        """Stop the stdio server."""
        logger.info("Stdio server stopping")
        # Stdio server stops automatically when the context exits
