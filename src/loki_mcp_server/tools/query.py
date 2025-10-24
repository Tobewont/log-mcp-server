"""
Query tool for Loki MCP Server.
"""
import json
from datetime import datetime
from typing import Any, Dict, Optional

from mcp.types import Tool, TextContent

import structlog

from ..client.loki_client import LokiClient
from ..utils.errors import create_mcp_error_response, LokiValidationError

logger = structlog.get_logger(__name__)


class QueryTool:
    """MCP tool for querying Loki logs."""
    
    def __init__(self, loki_client: LokiClient):
        """Initialize query tool."""
        self.loki_client = loki_client
    
    def get_tool_definition(self) -> Tool:
        """Get MCP tool definition."""
        return Tool(
            name="query_loki",
            description="Query logs from Loki with LogQL syntax",
            inputSchema={
                "type": "object",
                "properties": {
                    "tenant": {
                        "type": "string",
                        "description": "Tenant name (required for multi-tenant setups)",
                    },
                    "query": {
                        "type": "string",
                        "description": "LogQL query string (e.g., '{job=\"app\"} |= \"error\"')",
                    },
                    "start": {
                        "type": "string",
                        "description": "Start time in ISO 8601 format (e.g., '2023-01-01T12:00:00Z')",
                    },
                    "end": {
                        "type": "string",
                        "description": "End time in ISO 8601 format (e.g., '2023-01-01T13:00:00Z')",
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"Maximum number of log entries to return (max: {self.loki_client.config.max_limit})",
                        "minimum": 1,
                        "maximum": self.loki_client.config.max_limit,
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["forward", "backward"],
                        "description": "Query direction (forward=oldest first, backward=newest first)",
                        "default": "backward",
                    },
                },
                "required": ["tenant", "query"],
            },
        )
    
    async def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute log query."""
        try:
            # Extract and validate arguments
            tenant = arguments.get("tenant")
            query = arguments.get("query")
            start_str = arguments.get("start")
            end_str = arguments.get("end")
            limit = arguments.get("limit")
            direction = arguments.get("direction", "backward")
            
            if not tenant:
                raise LokiValidationError("Tenant parameter is required")
            
            if not query:
                raise LokiValidationError("Query parameter is required")
            
            # Parse timestamps
            start_time = None
            end_time = None
            
            if start_str:
                try:
                    start_time = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                except ValueError as e:
                    raise LokiValidationError(f"Invalid start time format: {e}")
            
            if end_str:
                try:
                    end_time = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                except ValueError as e:
                    raise LokiValidationError(f"Invalid end time format: {e}")
            
            # Validate time range
            if start_time and end_time and start_time >= end_time:
                raise LokiValidationError("Start time must be before end time")
            
            logger.info(
                "Executing Loki query",
                tenant=tenant,
                query=query,
                start=start_str,
                end=end_str,
                limit=limit,
                direction=direction,
            )
            
            # Execute query
            async with self.loki_client as client:
                result = await client.query_logs(
                    query=query,
                    tenant=tenant,
                    start=start_time,
                    end=end_time,
                    limit=limit,
                    direction=direction,
                )
            
            # Format response
            response_text = self._format_query_result(result, query, tenant)
            
            logger.info(
                "Query completed successfully",
                tenant=tenant,
                result_type=result.get("result_type"),
                entry_count=result.get("total_entries", 0),
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
            logger.error("Query execution failed", error=str(e))
            return create_mcp_error_response(e, "query_loki")
    
    def _format_query_result(
        self,
        result: Dict[str, Any],
        query: str,
        tenant: str,
    ) -> str:
        """Format query result for display."""
        result_type = result.get("result_type", "unknown")
        
        if result_type == "streams":
            return self._format_log_streams(result, query, tenant)
        elif result_type in ["vector", "matrix"]:
            return self._format_metrics(result, query, tenant)
        else:
            return self._format_raw_result(result, query, tenant)
    
    def _format_log_streams(
        self,
        result: Dict[str, Any],
        query: str,
        tenant: str,
    ) -> str:
        """Format log stream results."""
        logs = result.get("logs", [])
        total_entries = result.get("total_entries", 0)
        stats = result.get("stats", {})
        
        # Header
        response = f"""# Loki Query Results 📋

**Tenant:** `{tenant}`
**Query:** `{query}`
**Total Entries:** {total_entries}

"""
        
        # Add stats if available
        if stats:
            response += f"""## Query Statistics
```json
{json.dumps(stats, indent=2)}
```

"""
        
        # Format log entries
        if logs:
            response += "## Log Entries\n\n"
            
            for i, log_entry in enumerate(logs, 1):
                timestamp = log_entry.get("timestamp", "")
                labels = log_entry.get("labels", {})
                line = log_entry.get("line", "")
                
                # Format labels
                label_str = " ".join(f"{k}={v}" for k, v in labels.items())
                
                response += f"""### Entry {i}
**Time:** `{timestamp}`
**Labels:** `{{{label_str}}}`
**Log:** 
```
{line}
```

"""
        else:
            response += "## No log entries found\n\nTry adjusting your query or time range.\n"
        
        return response
    
    def _format_metrics(
        self,
        result: Dict[str, Any],
        query: str,
        tenant: str,
    ) -> str:
        """Format metric results."""
        metrics = result.get("metrics", [])
        result_type = result.get("result_type", "")
        stats = result.get("stats", {})
        
        response = f"""# Loki Metric Query Results 📊

**Tenant:** `{tenant}`
**Query:** `{query}`
**Result Type:** {result_type}
**Metric Count:** {len(metrics)}

"""
        
        # Add stats if available
        if stats:
            response += f"""## Query Statistics
```json
{json.dumps(stats, indent=2)}
```

"""
        
        # Format metrics
        if metrics:
            response += "## Metrics\n\n"
            
            for i, metric in enumerate(metrics, 1):
                labels = metric.get("labels", {})
                label_str = " ".join(f"{k}={v}" for k, v in labels.items())
                
                response += f"""### Metric {i}
**Labels:** `{{{label_str}}}`

"""
                
                if result_type == "vector":
                    timestamp = metric.get("timestamp")
                    value = metric.get("value")
                    response += f"**Value:** {value} (at {timestamp})\n\n"
                else:  # matrix
                    values = metric.get("values", [])
                    response += "**Values:**\n"
                    for val in values[:10]:  # Limit to first 10 values
                        response += f"- {val['timestamp']}: {val['value']}\n"
                    if len(values) > 10:
                        response += f"... and {len(values) - 10} more values\n"
                    response += "\n"
        else:
            response += "## No metrics found\n\nTry adjusting your query.\n"
        
        return response
    
    def _format_raw_result(
        self,
        result: Dict[str, Any],
        query: str,
        tenant: str,
    ) -> str:
        """Format raw/unknown result types."""
        return f"""# Loki Query Results (Raw) 📄

**Tenant:** `{tenant}`
**Query:** `{query}`

## Raw Response
```json
{json.dumps(result, indent=2)}
```
"""
