"""
FastMCP tools for Loki MCP Server.
All tools are implemented as FastMCP decorated functions.
"""
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import structlog
from mcp.server import FastMCP

from ..client.loki_client import LokiClient
from ..config import LokiConfig

logger = structlog.get_logger(__name__)

# Global variables to be set by main.py
_loki_client: Optional[LokiClient] = None
_config: Optional[LokiConfig] = None


def initialize_tools(loki_client: LokiClient, config: LokiConfig) -> None:
    """Initialize tools with Loki client and configuration."""
    global _loki_client, _config
    _loki_client = loki_client
    _config = config
    logger.info("FastMCP tools initialized")


def register_tools(mcp: FastMCP) -> None:
    """Register all tools with the FastMCP instance."""
    
    @mcp.tool()
    async def health_check() -> str:
        """Check Loki server health status and get current time.
        
        Returns:
            Formatted health status report with server information and current time.
        """
        if not _loki_client:
            raise RuntimeError("Loki client not initialized")
        
        try:
            logger.info("Executing health check")
            
            async with _loki_client as client:
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
            return response_text
            
        except Exception as e:
            logger.error("Health check failed", error=str(e))
            raise RuntimeError(f"Health check failed: {e}")

    @mcp.tool()
    async def get_tenants() -> str:
        """Get all available tenants (tenant label values) from Loki.
        
        Returns:
            Formatted list of available tenants.
        """
        if not _loki_client:
            raise RuntimeError("Loki client not initialized")
        
        try:
            logger.info("Getting tenants list")
            
            async with _loki_client as client:
                tenants = await client.get_tenants()
            
            if not tenants:
                return "# Available Tenants\n\nNo tenants found in Loki."
            
            tenant_list = "\n".join([f"- `{tenant}`" for tenant in sorted(tenants)])
            
            response_text = f"""# Available Tenants

Found **{len(tenants)}** tenant(s):

{tenant_list}

## Usage
Use these tenant names with other tools like `query_loki`, `get_labels`, and `get_label_values`.
"""
            
            logger.info("Tenants list retrieved", count=len(tenants))
            return response_text
            
        except Exception as e:
            logger.error("Failed to get tenants", error=str(e))
            raise RuntimeError(f"Failed to get tenants: {e}")

    @mcp.tool()
    async def query_loki(
        tenant: str,
        query: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: Optional[int] = None,
        direction: Optional[str] = None
    ) -> str:
        """Query Loki logs with LogQL.
        
        Args:
            tenant: Tenant name (required)
            query: LogQL query string
            start: Start time (RFC3339 format, optional)
            end: End time (RFC3339 format, optional)
            limit: Maximum number of entries to return (optional)
            direction: Query direction - 'forward' or 'backward' (optional)
        
        Returns:
            Formatted query results with log entries.
        """
        if not _loki_client:
            raise RuntimeError("Loki client not initialized")
        
        try:
            logger.info("Executing Loki query", tenant=tenant, query=query)
            
            # Prepare query parameters
            params = {"query": query}
            if start:
                params["start"] = start
            if end:
                params["end"] = end
            if limit:
                params["limit"] = limit
            if direction:
                params["direction"] = direction
            
            async with _loki_client as client:
                result = await client.query_range(tenant, **params)
            
            # Format response
            if not result.get("data", {}).get("result"):
                return f"""# Loki Query Results

**Query:** `{query}`
**Tenant:** `{tenant}`

No results found.
"""
            
            entries_count = 0
            formatted_entries = []
            
            for stream in result["data"]["result"]:
                labels = stream.get("stream", {})
                entries = stream.get("values", [])
                
                # Format stream labels
                labels_str = ", ".join([f"{k}={v}" for k, v in labels.items()])
                formatted_entries.append(f"## Stream: {{{labels_str}}}")
                
                for entry in entries:
                    timestamp, message = entry
                    # Convert nanosecond timestamp to readable format
                    try:
                        ts = int(timestamp) / 1_000_000_000
                        dt = datetime.fromtimestamp(ts)
                        time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                    except (ValueError, OSError):
                        time_str = timestamp
                    
                    formatted_entries.append(f"**{time_str}:** {message}")
                    entries_count += 1
            
            entries_text = "\n\n".join(formatted_entries)
            
            response_text = f"""# Loki Query Results

**Query:** `{query}`
**Tenant:** `{tenant}`
**Entries Found:** {entries_count}

{entries_text}
"""
            
            logger.info("Query completed", tenant=tenant, entries_count=entries_count)
            return response_text
            
        except Exception as e:
            logger.error("Query failed", tenant=tenant, query=query, error=str(e))
            raise RuntimeError(f"Query failed: {e}")

    @mcp.tool()
    async def get_labels(tenant: str) -> str:
        """Get all available labels for a specific tenant.
        
        Args:
            tenant: Tenant name (required)
        
        Returns:
            Formatted list of available labels for the tenant.
        """
        if not _loki_client:
            raise RuntimeError("Loki client not initialized")
        
        try:
            logger.info("Getting labels for tenant", tenant=tenant)
            
            async with _loki_client as client:
                labels = await client.get_labels(tenant)
            
            if not labels:
                return f"""# Available Labels for Tenant `{tenant}`

No labels found for this tenant.
"""
            
            labels_list = "\n".join([f"- `{label}`" for label in sorted(labels)])
            
            response_text = f"""# Available Labels for Tenant `{tenant}`

Found **{len(labels)}** label(s):

{labels_list}

## Usage
Use these label names with `get_label_values` to see available values for each label.
"""
            
            logger.info("Labels retrieved", tenant=tenant, count=len(labels))
            return response_text
            
        except Exception as e:
            logger.error("Failed to get labels", tenant=tenant, error=str(e))
            raise RuntimeError(f"Failed to get labels for tenant {tenant}: {e}")

    @mcp.tool()
    async def get_label_values(tenant: str, label: str) -> str:
        """Get all available values for a specific label in a tenant.
        
        Args:
            tenant: Tenant name (required)
            label: Label name (required)
        
        Returns:
            Formatted list of available values for the specified label.
        """
        if not _loki_client:
            raise RuntimeError("Loki client not initialized")
        
        try:
            logger.info("Getting label values", tenant=tenant, label=label)
            
            async with _loki_client as client:
                values = await client.get_label_values(tenant, label)
            
            if not values:
                return f"""# Available Values for Label `{label}` in Tenant `{tenant}`

No values found for this label.
"""
            
            values_list = "\n".join([f"- `{value}`" for value in sorted(values)])
            
            response_text = f"""# Available Values for Label `{label}` in Tenant `{tenant}`

Found **{len(values)}** value(s):

{values_list}

## Usage
Use these values in LogQL queries to filter logs by this label.
"""
            
            logger.info("Label values retrieved", tenant=tenant, label=label, count=len(values))
            return response_text
            
        except Exception as e:
            logger.error("Failed to get label values", tenant=tenant, label=label, error=str(e))
            raise RuntimeError(f"Failed to get values for label {label} in tenant {tenant}: {e}")

    logger.info("All FastMCP tools registered", tool_count=5)
