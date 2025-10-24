"""
Tenants management tool for Loki MCP Server.
"""
import json
from typing import Any, Dict

from mcp.types import Tool, TextContent

import structlog

from ..client.loki_client import LokiClient
from ..utils.errors import create_mcp_error_response

logger = structlog.get_logger(__name__)


class TenantsTool:
    """MCP tool for getting available tenants."""
    
    def __init__(self, loki_client: LokiClient):
        """Initialize tenants tool."""
        self.loki_client = loki_client
    
    def get_tool_definition(self) -> Tool:
        """Get MCP tool definition."""
        return Tool(
            name="get_tenants",
            description="Get list of all available tenants from Loki",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )
    
    async def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute tenant discovery."""
        try:
            logger.info("Discovering available tenants")
            
            async with self.loki_client as client:
                tenants = await client.get_tenants()
            
            # Format response
            if tenants:
                tenant_list = "\n".join(f"- `{tenant}`" for tenant in tenants)
                response_text = f"""# Available Tenants 🏢

Found **{len(tenants)}** tenant(s):

{tenant_list}

## Usage
Use any of these tenant names as the `tenant` parameter in other Loki tools.

## Raw Data
```json
{json.dumps(tenants, indent=2)}
```
"""
            else:
                response_text = """# Available Tenants 🏢

No tenants found. This could mean:
- No multi-tenant configuration is active
- The 'tenant' label is not used in your Loki setup
- You may need to check your Loki configuration

You can still use other tools without specifying a tenant, or try using a known tenant name.
"""
            
            logger.info("Tenant discovery completed", tenant_count=len(tenants))
            
            return {
                "content": [
                    TextContent(
                        type="text",
                        text=response_text,
                    )
                ]
            }
            
        except Exception as e:
            logger.error("Tenant discovery failed", error=str(e))
            return create_mcp_error_response(e, "get_tenants")
