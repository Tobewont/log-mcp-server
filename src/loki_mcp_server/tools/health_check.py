"""
Health check tool for Loki MCP Server.
"""
import json
from typing import Any, Dict

from mcp.types import Tool, TextContent

import structlog

from ..client.loki_client import LokiClient
from ..utils.errors import create_mcp_error_response

logger = structlog.get_logger(__name__)


class HealthCheckTool:
    """MCP tool for checking Loki server health."""
    
    def __init__(self, loki_client: LokiClient):
        """Initialize health check tool."""
        self.loki_client = loki_client
    
    def get_tool_definition(self) -> Tool:
        """Get MCP tool definition."""
        return Tool(
            name="health_check",
            description="Check Loki server health status and get current time",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )
    
    async def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute health check."""
        try:
            logger.info("Executing health check")
            
            async with self.loki_client as client:
                health_data = await client.health_check()
            
            # Format response
            if health_data["status"] == "healthy":
                status_emoji = "✅"
                status_text = "Healthy"
            else:
                status_emoji = "❌"
                status_text = "Unhealthy"
            
            response_text = f"""# Loki Server Health Check {status_emoji}

**Status:** {status_text}
**Server Address:** {health_data['server_addr']}
**Current Time:** {health_data['current_time']}

## Details
```json
{json.dumps(health_data, indent=2)}
```
"""
            
            logger.info("Health check completed", status=health_data["status"])
            
            return {
                "content": [
                    TextContent(
                        type="text",
                        text=response_text,
                    )
                ]
            }
            
        except Exception as e:
            logger.error("Health check failed", error=str(e))
            return create_mcp_error_response(e, "health_check")
