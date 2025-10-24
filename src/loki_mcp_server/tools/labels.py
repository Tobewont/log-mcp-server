"""
Labels management tools for Loki MCP Server.
"""
import json
from typing import Any, Dict

from mcp.types import Tool, TextContent

import structlog

from ..client.loki_client import LokiClient
from ..utils.errors import create_mcp_error_response, LokiValidationError

logger = structlog.get_logger(__name__)


class LabelsTool:
    """MCP tool for getting available labels."""
    
    def __init__(self, loki_client: LokiClient):
        """Initialize labels tool."""
        self.loki_client = loki_client
    
    def get_tool_definition(self) -> Tool:
        """Get MCP tool definition."""
        return Tool(
            name="get_labels",
            description="Get all available labels for a specific tenant",
            inputSchema={
                "type": "object",
                "properties": {
                    "tenant": {
                        "type": "string",
                        "description": "Tenant name (required)",
                    },
                },
                "required": ["tenant"],
            },
        )
    
    async def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute labels retrieval."""
        try:
            tenant = arguments.get("tenant")
            
            if not tenant:
                raise LokiValidationError("Tenant parameter is required")
            
            logger.info("Retrieving labels", tenant=tenant)
            
            async with self.loki_client as client:
                labels = await client.get_labels(tenant)
            
            # Format response
            if labels:
                label_list = "\n".join(f"- `{label}`" for label in sorted(labels))
                response_text = f"""# Available Labels 🏷️

**Tenant:** `{tenant}`
**Label Count:** {len(labels)}

## Labels
{label_list}

## Usage
Use these label names in LogQL queries:
- Filter by label: `{{job="app"}}`
- Get label values: Use `get_label_values` tool

## Raw Data
```json
{json.dumps(sorted(labels), indent=2)}
```
"""
            else:
                response_text = f"""# Available Labels 🏷️

**Tenant:** `{tenant}`

No labels found for this tenant. This could mean:
- No data has been ingested for this tenant
- The tenant doesn't exist
- Access permissions may be restricting label visibility
"""
            
            logger.info("Labels retrieved successfully", tenant=tenant, label_count=len(labels))
            
            return {
                "content": [
                    TextContent(
                        type="text",
                        text=response_text,
                    )
                ]
            }
            
        except Exception as e:
            logger.error("Labels retrieval failed", error=str(e))
            return create_mcp_error_response(e, "get_labels")


class LabelValuesTool:
    """MCP tool for getting label values."""
    
    def __init__(self, loki_client: LokiClient):
        """Initialize label values tool."""
        self.loki_client = loki_client
    
    def get_tool_definition(self) -> Tool:
        """Get MCP tool definition."""
        return Tool(
            name="get_label_values",
            description="Get all values for a specific label in a tenant",
            inputSchema={
                "type": "object",
                "properties": {
                    "tenant": {
                        "type": "string",
                        "description": "Tenant name (required)",
                    },
                    "label": {
                        "type": "string",
                        "description": "Label name to get values for (required)",
                    },
                },
                "required": ["tenant", "label"],
            },
        )
    
    async def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute label values retrieval."""
        try:
            tenant = arguments.get("tenant")
            label = arguments.get("label")
            
            if not tenant:
                raise LokiValidationError("Tenant parameter is required")
            
            if not label:
                raise LokiValidationError("Label parameter is required")
            
            logger.info("Retrieving label values", tenant=tenant, label=label)
            
            async with self.loki_client as client:
                values = await client.get_label_values(tenant, label)
            
            # Format response
            if values:
                # Limit display to first 50 values for readability
                display_values = values[:50]
                value_list = "\n".join(f"- `{value}`" for value in sorted(display_values))
                
                response_text = f"""# Label Values 🔖

**Tenant:** `{tenant}`
**Label:** `{label}`
**Total Values:** {len(values)}

## Values
{value_list}
"""
                
                if len(values) > 50:
                    response_text += f"\n*Showing first 50 of {len(values)} values*\n"
                
                response_text += f"""
## Usage Examples
Use these values in LogQL queries:
- `{{{label}="{values[0] if values else 'value'}"}}`
- `{{{label}=~"pattern.*"}}`

## Raw Data
```json
{json.dumps(sorted(values), indent=2)}
```
"""
            else:
                response_text = f"""# Label Values 🔖

**Tenant:** `{tenant}`
**Label:** `{label}`

No values found for this label. This could mean:
- The label doesn't exist in this tenant
- No data with this label has been ingested
- The label name might be misspelled

Try using `get_labels` to see available labels first.
"""
            
            logger.info(
                "Label values retrieved successfully",
                tenant=tenant,
                label=label,
                value_count=len(values),
            )
            
            return {
                "content": [
                    TextContent(
                        type="text",
                        text=response_text,
                    )
                ]
            }
            
        except Exception as e:
            logger.error("Label values retrieval failed", error=str(e))
            return create_mcp_error_response(e, "get_label_values")
