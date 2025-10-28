#!/usr/bin/env python3
"""
FastMCP tools for Loki operations.
"""
import json
from typing import Any, Dict, List, Optional

import structlog
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent, Tool

from ..client.loki_client import LokiClient
from ..config import LokiConfig
from ..utils.errors import create_mcp_error_response

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
                status_text = "Healthy"
            else:
                status_text = "Unhealthy"
            
            response_text = f"""# Loki Server Health Check

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
    async def query_loki(
        query: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: Optional[int] = None,
        direction: Optional[str] = None
    ) -> str:
        """Query Loki logs with LogQL.
        
        Args:
            query: LogQL query string
            start: Start time (RFC3339 format, optional)
            end: End time (RFC3339 format, optional)
            limit: Maximum number of entries to return (optional)
            direction: Query direction - 'forward' or 'backward' (optional)
        
        Returns:
            Formatted query results with log entries.
        """
        if not _loki_client or not _config:
            raise RuntimeError("Loki client or config not initialized")
        
        # Get configured tenants
        tenant_list = _config.get_tenant_list()
        
        try:
            logger.info("Executing Loki query", tenants=tenant_list, query=query)
            
            all_logs = []
            successful_tenants = []
            
            # Query each configured tenant
            for tenant in tenant_list:
                try:
                    async with _loki_client as client:
                        logs = await client.query_logs(
                            query=query,
                            tenant=tenant,
                            start=None,  # TODO: Parse start/end strings to datetime
                            end=None,
                            limit=limit,
                            direction=direction or "backward"
                        )
                    
                    if logs:
                        # Add tenant info to each log entry
                        for log_entry in logs:
                            log_entry["tenant"] = tenant
                        all_logs.extend(logs)
                        successful_tenants.append(tenant)
                        
                except Exception as e:
                    logger.warning("Query failed for tenant", tenant=tenant, error=str(e))
                    continue
            
            # Format response
            if not all_logs:
                return f"""# Loki Query Results

**Query:** `{query}`
**Tenants:** `{', '.join(tenant_list)}`

No results found in any configured tenant.
"""
            
            response_text = f"""# Loki Query Results

**Query:** `{query}`
**Tenants Queried:** `{', '.join(tenant_list)}`
**Successful Tenants:** `{', '.join(successful_tenants)}`
**Total Entries Found:** {len(all_logs)}

"""
            
            for i, log_entry in enumerate(all_logs, 1):
                time_str = log_entry.get("time", "N/A")
                labels = log_entry.get("labels", {})
                log_text = log_entry.get("log", "")
                tenant = log_entry.get("tenant", "unknown")
                
                # Format labels
                labels_str = ", ".join([f"{k}={v}" for k, v in labels.items()])
                
                response_text += f"""## Entry {i} (Tenant: {tenant})
**Time:** {time_str}
**Labels:** {{{labels_str}}}
**Log:** {log_text}

"""
            
            logger.info("Query completed", successful_tenants=successful_tenants, entries_count=len(all_logs))
            return response_text
            
        except Exception as e:
            logger.error("Query failed", tenants=tenant_list, query=query, error=str(e))
            raise RuntimeError(f"Query failed: {e}")

    @mcp.tool()
    async def get_labels() -> str:
        """Get all available labels from configured tenants.
        
        Returns:
            Formatted list of available labels from all configured tenants.
        """
        if not _loki_client or not _config:
            raise RuntimeError("Loki client or config not initialized")
        
        # Get configured tenants
        tenant_list = _config.get_tenant_list()
        
        try:
            logger.info("Executing get_labels", tenants=tenant_list)
            
            all_labels = {}  # tenant -> labels mapping
            successful_tenants = []
            
            # Get labels from each configured tenant
            for tenant in tenant_list:
                try:
                    async with _loki_client as client:
                        labels = await client.get_labels(tenant)
                    
                    all_labels[tenant] = labels
                    if labels:
                        successful_tenants.append(tenant)
                        
                except Exception as e:
                    logger.warning("Failed to get labels for tenant", tenant=tenant, error=str(e))
                    all_labels[tenant] = []
                    continue
            
            # Format response
            response_text = f"""# Available Labels

**Configured Tenants:** `{', '.join(tenant_list)}`
**Successful Tenants:** `{', '.join(successful_tenants)}`

"""
            
            for tenant, labels in all_labels.items():
                response_text += f"""## Tenant: `{tenant}`
"""
                if labels:
                    response_text += f"Found {len(labels)} labels:\n\n"
                    for i, label in enumerate(labels, 1):
                        response_text += f"{i}. `{label}`\n"
                else:
                    response_text += "No labels found for this tenant.\n"
                
                response_text += "\n"
            
            total_unique_labels = len(set(label for labels in all_labels.values() for label in labels))
            response_text += f"**Total Unique Labels:** {total_unique_labels}\n"
            
            logger.info("Labels retrieved", successful_tenants=successful_tenants, total_unique=total_unique_labels)
            return response_text
            
        except Exception as e:
            logger.error("Failed to get labels", tenants=tenant_list, error=str(e))
            raise RuntimeError(f"Failed to get labels: {e}")

    @mcp.tool()
    async def get_label_values(label: str) -> str:
        """Get all available values for a specific label from configured tenants.
        
        Args:
            label: Label name (required)
        
        Returns:
            Formatted list of available values for the specified label from all configured tenants.
        """
        if not _loki_client or not _config:
            raise RuntimeError("Loki client or config not initialized")
        
        # Get configured tenants
        tenant_list = _config.get_tenant_list()
        
        try:
            logger.info("Executing get_label_values", tenants=tenant_list, label=label)
            
            all_values = {}  # tenant -> values mapping
            successful_tenants = []
            
            # Get label values from each configured tenant
            for tenant in tenant_list:
                try:
                    async with _loki_client as client:
                        values = await client.get_label_values(tenant, label)
                    
                    all_values[tenant] = values
                    if values:
                        successful_tenants.append(tenant)
                        
                except Exception as e:
                    logger.warning("Failed to get label values for tenant", tenant=tenant, label=label, error=str(e))
                    all_values[tenant] = []
                    continue
            
            # Format response
            response_text = f"""# Values for Label `{label}`

**Configured Tenants:** `{', '.join(tenant_list)}`
**Successful Tenants:** `{', '.join(successful_tenants)}`

"""
            
            for tenant, values in all_values.items():
                response_text += f"""## Tenant: `{tenant}`
"""
                if values:
                    response_text += f"Found {len(values)} values:\n\n"
                    for i, value in enumerate(values, 1):
                        response_text += f"{i}. `{value}`\n"
                else:
                    response_text += f"No values found for label `{label}` in this tenant.\n"
                
                response_text += "\n"
            
            total_unique_values = len(set(value for values in all_values.values() for value in values))
            response_text += f"**Total Unique Values:** {total_unique_values}\n"
            
            logger.info("Label values retrieved", successful_tenants=successful_tenants, label=label, total_unique=total_unique_values)
            return response_text
            
        except Exception as e:
            logger.error("Failed to get label values", tenants=tenant_list, label=label, error=str(e))
            raise RuntimeError(f"Failed to get values for label {label}: {e}")

    logger.info("All FastMCP tools registered", tool_count=4)
