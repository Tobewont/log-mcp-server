#!/usr/bin/env python3
"""
Loki MCP Server - Main entry point.

A Model Context Protocol server for querying Grafana Loki logs via HTTP API.
"""
import asyncio
import signal
import sys
from typing import Optional, Union

from .config import LokiConfig
from .server.factory import ServerFactory
from .server.base_server import BaseServer
from .server.http_server import HTTPServer
from .utils.logging import setup_logging

logger = setup_logging(__name__)


class MCPServerManager:
    """Manager for MCP server lifecycle."""
    
    def __init__(self):
        """Initialize server manager."""
        self.config: Optional[LokiConfig] = None
        self.server: Optional[Union[BaseServer, HTTPServer]] = None
        self.shutdown_event = asyncio.Event()
    
    def setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown."""
        def signal_handler(signum, frame):
            """Handle shutdown signals gracefully."""
            logger.info("Received shutdown signal", signal=signum)
            self.shutdown_event.set()
        
        # Register signal handlers
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
    
    async def start(self) -> None:
        """Start the MCP server."""
        try:
            # Load configuration
            self.config = LokiConfig()
            logger.info(
                "Configuration loaded",
                server_mode=self.config.server_mode,
                loki_addr=self.config.addr
            )
            
            # Create server instance
            self.server = ServerFactory.create_server(self.config)
            
            # Setup signal handlers
            self.setup_signal_handlers()
            
            # Start server based on mode
            if self.config.server_mode == "sse":
                await self._start_sse_server()
            else:
                await self._start_stdio_server()
                
        except Exception as e:
            logger.error("Failed to start MCP server", error=str(e), exc_info=True)
            sys.exit(1)
    
    async def _start_stdio_server(self) -> None:
        """Start stdio server."""
        logger.info("Starting MCP server in stdio mode")
        
        # Create server task
        server_task = asyncio.create_task(self.server.start())
        
        # Wait for either server completion or shutdown signal
        done, pending = await asyncio.wait(
            [server_task, asyncio.create_task(self.shutdown_event.wait())],
            return_when=asyncio.FIRST_COMPLETED
        )
        
        # Cancel pending tasks
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        
        # Check if server task completed with an exception
        if server_task in done:
            try:
                await server_task
            except Exception as e:
                logger.error("Server task failed", error=str(e))
                raise
        
        logger.info("Server shutdown complete")
    
    async def _start_sse_server(self) -> None:
        """Start SSE server with HTTP wrapper."""
        logger.info(
            "Starting MCP server in SSE mode",
            host=self.config.server_host,
            port=self.config.server_port
        )
        
        try:
            # Start the HTTP server
            await self.server.start()
            
            # Wait for shutdown signal
            await self.shutdown_event.wait()
            
        finally:
            # Stop the server
            if self.server:
                await self.server.stop()
            
            logger.info("SSE server shutdown complete")
    
    async def stop(self) -> None:
        """Stop the server."""
        if self.server:
            await self.server.stop()


async def main() -> None:
    """Main entry point for the Loki MCP server."""
    server_manager = MCPServerManager()
    
    try:
        await server_manager.start()
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.error("Server failed", error=str(e), exc_info=True)
        sys.exit(1)
    finally:
        await server_manager.stop()


def cli_main() -> None:
    """CLI entry point."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application interrupted")
        sys.exit(0)
    except Exception as e:
        logger.error("Application failed", error=str(e), exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    cli_main()