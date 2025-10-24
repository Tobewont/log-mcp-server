#!/usr/bin/env python3
"""
Loki MCP Server - Main entry point.

A Model Context Protocol server for querying Grafana Loki logs via HTTP API.
"""
import asyncio
import signal
import sys
from typing import Any, Dict, List, Optional

import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server

from .config import LokiConfig
from .client.loki_client import LokiClient
from .tools.health_check import HealthCheckTool
from .tools.tenants import TenantsTool
from .tools.query import QueryTool
from .tools.labels import LabelsTool, LabelValuesTool
from .utils.logging import setup_logging
from .utils.errors import LokiMCPError, create_mcp_error_response

# Setup logging
logger = setup_logging(__name__)

# Create server instance
server = Server("loki-mcp-server")


async def main() -> None:
    """Main entry point for the Loki MCP server."""
    # Setup signal handlers for graceful shutdown
    shutdown_event = asyncio.Event()
    
    def signal_handler(signum, frame):
        """Handle shutdown signals gracefully."""
        logger.info("Received shutdown signal", signal=signum)
        shutdown_event.set()
    
    # Register signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        # Load configuration
        config = LokiConfig()
        logger.info("Configuration loaded", addr=config.addr)
        
        # Initialize Loki client
        loki_client = LokiClient(config)
        
        # Initialize tools
        health_check_tool = HealthCheckTool(loki_client)
        tenants_tool = TenantsTool(loki_client)
        query_tool = QueryTool(loki_client)
        labels_tool = LabelsTool(loki_client)
        label_values_tool = LabelValuesTool(loki_client)
        
        # Register tool handlers
        @server.call_tool()
        async def handle_call_tool(name: str, arguments: dict) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
            """Handle tool calls."""
            try:
                if name == "health_check":
                    result = await health_check_tool.execute(arguments)
                elif name == "get_tenants":
                    result = await tenants_tool.execute(arguments)
                elif name == "query_loki":
                    result = await query_tool.execute(arguments)
                elif name == "get_labels":
                    result = await labels_tool.execute(arguments)
                elif name == "get_label_values":
                    result = await label_values_tool.execute(arguments)
                else:
                    raise ValueError(f"Unknown tool: {name}")
                
                return result.get("content", [])
                
            except Exception as e:
                logger.error("Tool execution failed", tool=name, error=str(e))
                error_response = create_mcp_error_response(e, name)
                return error_response.get("content", [])
        
        @server.list_tools()
        async def handle_list_tools() -> list[types.Tool]:
            """List available tools."""
            return [
                health_check_tool.get_tool_definition(),
                tenants_tool.get_tool_definition(),
                query_tool.get_tool_definition(),
                labels_tool.get_tool_definition(),
                label_values_tool.get_tool_definition(),
            ]
            
        logger.info("MCP server initialized with tools", tool_count=5)
        
        # Start the server
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            # Create server task
            server_task = asyncio.create_task(
                server.run(
                    read_stream,
                    write_stream,
                    NotificationOptions(),
                )
            )
            
            # Wait for either server completion or shutdown signal
            done, pending = await asyncio.wait(
                [server_task, asyncio.create_task(shutdown_event.wait())],
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
            
    except Exception as e:
        logger.error("Failed to start MCP server", error=str(e), exc_info=True)
        sys.exit(1)


def cli_main() -> None:
    """CLI entry point."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error("Unexpected error", error=str(e), exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    cli_main()
