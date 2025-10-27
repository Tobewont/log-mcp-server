#!/usr/bin/env python3
"""
Loki MCP Server - FastMCP-based implementation.

A Model Context Protocol server for querying Grafana Loki logs via HTTP API.
Uses FastMCP for automatic mode detection and simplified tool management.
"""
import asyncio
import sys
from typing import Optional

from mcp.server import FastMCP

from .config import LokiConfig
from .client.loki_client import LokiClient
from .tools.fastmcp_tools import initialize_tools, register_tools
from .utils.logging import setup_logging

logger = setup_logging(__name__)


class LokiMCPServer:
    """Loki MCP Server using FastMCP framework."""
    
    def __init__(self):
        """Initialize the server."""
        self.config: Optional[LokiConfig] = None
        self.loki_client: Optional[LokiClient] = None
        self.mcp: Optional[FastMCP] = None
    
    async def initialize(self) -> None:
        """Initialize configuration and clients."""
        try:
            # Load configuration
            self.config = LokiConfig()
            logger.info(
                "Configuration loaded",
                loki_addr=self.config.addr,
                fastmcp_debug=self.config.fastmcp_debug,
                fastmcp_host=self.config.fastmcp_host,
                fastmcp_port=self.config.fastmcp_port,
            )
            
            # Initialize Loki client
            self.loki_client = LokiClient(self.config)
            logger.info("Loki client initialized")
            
            # Create FastMCP instance
            self.mcp = FastMCP(
                name="loki-mcp-server",
                instructions="A Model Context Protocol server for querying Grafana Loki logs. "
                           "Provides tools to check health, discover tenants, query logs, and explore labels.",
                debug=self.config.fastmcp_debug,
                host=self.config.fastmcp_host,
                port=self.config.fastmcp_port,
            )
            
            # Initialize and register tools
            initialize_tools(self.loki_client, self.config)
            register_tools(self.mcp)
            
            logger.info(
                "FastMCP server initialized",
                name="loki-mcp-server",
                debug=self.config.fastmcp_debug,
                host=self.config.fastmcp_host,
                port=self.config.fastmcp_port,
            )
            
        except Exception as e:
            logger.error("Failed to initialize server", error=str(e), exc_info=True)
            raise
    
    def run(self) -> None:
        """Run the FastMCP server.
        
        FastMCP automatically detects the running mode:
        - If run with a port argument or HTTP environment, uses HTTP/SSE mode
        - Otherwise, uses stdio mode for process communication
        """
        if not self.mcp:
            raise RuntimeError("Server not initialized. Call initialize() first.")
        
        logger.info("Starting Loki MCP Server with FastMCP")
        
        try:
            # FastMCP handles mode detection and server lifecycle
            self.mcp.run()
        except KeyboardInterrupt:
            logger.info("Server interrupted by user")
        except Exception as e:
            logger.error("Server error", error=str(e), exc_info=True)
            sys.exit(1)


async def main() -> None:
    """Main entry point for the Loki MCP server."""
    server = LokiMCPServer()
    
    try:
        await server.initialize()
        server.run()
    except Exception as e:
        logger.error("Failed to start server", error=str(e), exc_info=True)
        sys.exit(1)


def cli_main() -> None:
    """CLI entry point."""
    try:
        # Check if we're in an async context already
        try:
            loop = asyncio.get_running_loop()
            # If we're already in an async context, create a task
            task = loop.create_task(main())
            # This won't work in most cases, but FastMCP.run() is synchronous anyway
            logger.warning("Already in async context, running synchronously")
            server = LokiMCPServer()
            asyncio.run(server.initialize())
            server.run()
        except RuntimeError:
            # No running loop, we can use asyncio.run()
            asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server interrupted")
    except Exception as e:
        logger.error("Server startup failed", error=str(e), exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    cli_main()