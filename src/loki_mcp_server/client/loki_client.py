"""
Loki HTTP API client implementation.
"""
import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional, Union
from urllib.parse import quote

import structlog

from ..config import LokiConfig
from ..utils.errors import LokiQueryError, LokiValidationError
from .http_client import AsyncHTTPClient
from .auth import LokiAuth

logger = structlog.get_logger(__name__)


class LokiClient:
    """Async Loki HTTP API client."""
    
    def __init__(self, config: LokiConfig):
        """Initialize Loki client with configuration."""
        self.config = config
        self.auth = LokiAuth(config)
        self._http_client: Optional[AsyncHTTPClient] = None
    
    async def __aenter__(self) -> "LokiClient":
        """Async context manager entry."""
        self._http_client = AsyncHTTPClient(self.config)
        await self._http_client.__aenter__()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        if self._http_client:
            await self._http_client.__aexit__(exc_type, exc_val, exc_tb)
    
    def _get_http_client(self) -> AsyncHTTPClient:
        """Get HTTP client instance."""
        if not self._http_client:
            raise RuntimeError("LokiClient must be used as async context manager")
        return self._http_client
    
    async def health_check(self) -> Dict[str, Any]:
        """Check Loki server health and get current time."""
        try:
            client = self._get_http_client()
            
            # Use the ready endpoint for health check
            response = await client.get("/ready")
            
            # Also get current time from Loki
            current_time = datetime.utcnow().isoformat() + "Z"
            
            return {
                "status": "healthy",
                "loki_status": response.get("status", "unknown"),
                "current_time": current_time,
                "server_addr": self.config.addr,
            }
            
        except Exception as e:
            logger.error("Health check failed", error=str(e))
            return {
                "status": "unhealthy",
                "error": str(e),
                "current_time": datetime.utcnow().isoformat() + "Z",
                "server_addr": self.config.addr,
            }
    
    async def get_tenants(self) -> List[str]:
        """Get list of all available tenants from tenant label values."""
        try:
            client = self._get_http_client()
            
            # Get all values for the 'tenant' label
            # Note: This assumes tenants are identified by a 'tenant' label
            response = await client.get("/loki/api/v1/label/tenant/values")
            
            if response.get("status") == "success" and "data" in response:
                tenants = response["data"]
                logger.info("Retrieved tenants", tenant_count=len(tenants))
                return tenants
            else:
                logger.warning("No tenants found or unexpected response format")
                return []
                
        except Exception as e:
            logger.error("Failed to get tenants", error=str(e))
            # If tenant label doesn't exist, return empty list
            return []
    
    async def query_logs(
        self,
        query: str,
        tenant: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: Optional[int] = None,
        direction: str = "backward",
    ) -> Dict[str, Any]:
        """Query logs from Loki."""
        try:
            # Validate tenant
            self.auth.validate_tenant_access(tenant)
            
            # Validate query
            if not query.strip():
                raise LokiValidationError("Query cannot be empty")
            
            # Validate limit
            if limit is not None:
                if limit <= 0:
                    raise LokiValidationError("Limit must be positive")
                if limit > self.config.max_limit:
                    raise LokiValidationError(
                        f"Limit {limit} exceeds maximum {self.config.max_limit}"
                    )
            else:
                limit = self.config.default_limit
            
            # Build query parameters
            params = {
                "query": query,
                "limit": str(limit),
                "direction": direction,
            }
            
            if start:
                params["start"] = str(int(start.timestamp() * 1_000_000_000))
            if end:
                params["end"] = str(int(end.timestamp() * 1_000_000_000))
            
            client = self._get_http_client()
            response = await client.get(
                "/loki/api/v1/query_range",
                params=params,
                tenant=tenant,
            )
            
            if response.get("status") == "success":
                return self._format_query_response(response["data"])
            else:
                raise LokiQueryError(f"Query failed: {response}")
                
        except Exception as e:
            logger.error("Query execution failed", query=query, tenant=tenant, error=str(e))
            raise
    
    async def get_labels(self, tenant: str) -> List[str]:
        """Get all available labels for a tenant."""
        try:
            self.auth.validate_tenant_access(tenant)
            
            client = self._get_http_client()
            response = await client.get("/loki/api/v1/labels", tenant=tenant)
            
            if response.get("status") == "success" and "data" in response:
                labels = response["data"]
                logger.info("Retrieved labels", tenant=tenant, label_count=len(labels))
                return labels
            else:
                raise LokiQueryError(f"Failed to get labels: {response}")
                
        except Exception as e:
            logger.error("Failed to get labels", tenant=tenant, error=str(e))
            raise
    
    async def get_label_values(self, tenant: str, label: str) -> List[str]:
        """Get all values for a specific label in a tenant."""
        try:
            self.auth.validate_tenant_access(tenant)
            
            if not label.strip():
                raise LokiValidationError("Label name cannot be empty")
            
            # URL encode the label name
            encoded_label = quote(label)
            
            client = self._get_http_client()
            response = await client.get(
                f"/loki/api/v1/label/{encoded_label}/values",
                tenant=tenant,
            )
            
            if response.get("status") == "success" and "data" in response:
                values = response["data"]
                logger.info(
                    "Retrieved label values",
                    tenant=tenant,
                    label=label,
                    value_count=len(values),
                )
                return values
            else:
                raise LokiQueryError(f"Failed to get label values: {response}")
                
        except Exception as e:
            logger.error(
                "Failed to get label values",
                tenant=tenant,
                label=label,
                error=str(e),
            )
            raise
    
    def _format_query_response(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Format Loki query response for better readability."""
        result_type = data.get("resultType", "")
        results = data.get("result", [])
        
        if result_type == "streams":
            # Format log streams
            formatted_logs = []
            total_entries = 0
            
            for stream in results:
                stream_labels = stream.get("stream", {})
                values = stream.get("values", [])
                total_entries += len(values)
                
                for timestamp_ns, log_line in values:
                    # Convert nanosecond timestamp to datetime
                    timestamp_s = int(timestamp_ns) / 1_000_000_000
                    dt = datetime.fromtimestamp(timestamp_s)
                    
                    formatted_logs.append({
                        "timestamp": dt.isoformat() + "Z",
                        "labels": stream_labels,
                        "line": log_line,
                    })
            
            return {
                "result_type": result_type,
                "total_entries": total_entries,
                "logs": formatted_logs,
                "stats": data.get("stats", {}),
            }
        
        elif result_type in ["vector", "matrix"]:
            # Format metric results
            formatted_metrics = []
            
            for item in results:
                metric_labels = item.get("metric", {})
                
                if result_type == "vector":
                    timestamp, value = item.get("value", [None, None])
                    formatted_metrics.append({
                        "labels": metric_labels,
                        "timestamp": timestamp,
                        "value": value,
                    })
                else:  # matrix
                    values = item.get("values", [])
                    formatted_values = [
                        {"timestamp": ts, "value": val} for ts, val in values
                    ]
                    formatted_metrics.append({
                        "labels": metric_labels,
                        "values": formatted_values,
                    })
            
            return {
                "result_type": result_type,
                "metrics": formatted_metrics,
                "stats": data.get("stats", {}),
            }
        
        else:
            # Return raw data for unknown result types
            return data
