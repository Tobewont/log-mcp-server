"""
Base server interface for MCP server implementations.
"""

from abc import ABC, abstractmethod
from typing import List

import mcp.types as types
from mcp.server import Server

from ..client.loki_client import LokiClient
from ..config import LokiConfig
from ..tools.health_check import HealthCheckTool
from ..tools.tenants import TenantsTool
from ..tools.query import QueryTool
from ..tools.labels import LabelsTool, LabelValuesTool


class BaseServer(ABC):
    """Base class for MCP server implementations."""
    
    def __init__(self, config: LokiConfig):
        """Initialize the base server with configuration."""
        self.config = config
        self.server = Server("loki-mcp-server")
        self.loki_client = LokiClient(config)
        
        # Initialize tools
        self.health_check_tool = HealthCheckTool(self.loki_client)
        self.tenants_tool = TenantsTool(self.loki_client)
        self.query_tool = QueryTool(self.loki_client)
        self.labels_tool = LabelsTool(self.loki_client)
        self.label_values_tool = LabelValuesTool(self.loki_client)
        
        # Register tool handlers
        self._register_handlers()
    
    def _register_handlers(self) -> None:
        """Register MCP tool handlers."""
        
        @self.server.call_tool()
        async def handle_call_tool(
            name: str, 
            arguments: dict
        ) -> List[types.TextContent | types.ImageContent | types.EmbeddedResource]:
            """Handle tool calls."""
            try:
                if name == "health_check":
                    result = await self.health_check_tool.execute(arguments)
                elif name == "get_tenants":
                    result = await self.tenants_tool.execute(arguments)
                elif name == "query_loki":
                    result = await self.query_tool.execute(arguments)
                elif name == "get_labels":
                    result = await self.labels_tool.execute(arguments)
                elif name == "get_label_values":
                    result = await self.label_values_tool.execute(arguments)
                else:
                    raise ValueError(f"Unknown tool: {name}")
                
                return result.get("content", [])
                
            except Exception as e:
                from ..utils.errors import create_mcp_error_response
                from ..utils.logging import setup_logging
                
                logger = setup_logging(__name__)
                logger.error("Tool execution failed", tool=name, error=str(e))
                error_response = create_mcp_error_response(e, name)
                return error_response.get("content", [])
        
        @self.server.list_tools()
        async def handle_list_tools() -> List[types.Tool]:
            """List available tools."""
            return [
                self.health_check_tool.get_tool_definition(),
                self.tenants_tool.get_tool_definition(),
                self.query_tool.get_tool_definition(),
                self.labels_tool.get_tool_definition(),
                self.label_values_tool.get_tool_definition(),
            ]
    
    @abstractmethod
    async def start(self) -> None:
        """Start the server."""
        pass
    
    @abstractmethod
    async def stop(self) -> None:
        """Stop the server."""
        pass
