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
    
    async def _check_ready_endpoint(self) -> None:
        """Check the ready endpoint which returns plain text."""
        import httpx
        from urllib.parse import urljoin
        
        # Create a simple HTTP client for plain text response
        timeout = httpx.Timeout(
            connect=self.config.connect_timeout,
            read=self.config.read_timeout,
            write=self.config.write_timeout,
            pool=self.config.pool_timeout,
        )
        
        async with httpx.AsyncClient(timeout=timeout, verify=not self.config.tls_skip_verify) as client:
            url = urljoin(self.config.addr, "/ready")
            headers = {}
            
            # Add authentication if configured
            if self.config.username and self.config.password:
                import base64
                auth_string = f"{self.config.username}:{self.config.password}"
                auth_bytes = auth_string.encode("utf-8")
                auth_b64 = base64.b64encode(auth_bytes).decode("ascii")
                headers["Authorization"] = f"Basic {auth_b64}"
            elif self.config.bearer_token:
                headers["Authorization"] = f"Bearer {self.config.bearer_token}"
            
            response = await client.get(url, headers=headers)
            
            # Check if the response is successful (200 OK)
            if response.status_code != 200:
                raise Exception(f"Ready endpoint returned {response.status_code}: {response.text}")
    
    async def health_check(self) -> Dict[str, Any]:
        """Check Loki server health and get current time."""
        try:
            client = self._get_http_client()
            
            # Use a simple endpoint that returns JSON - try metrics endpoint first
            try:
                # Try the metrics endpoint which should return JSON
                response = await client.get("/loki/api/v1/labels")
                loki_status = "healthy" if response.get("status") == "success" else "unknown"
            except Exception:
                # Fallback: just check if we can connect to the ready endpoint
                # The ready endpoint returns plain text, so we'll handle it specially
                await self._check_ready_endpoint()
                loki_status = "healthy"
            
            # Get current time
            current_time = datetime.utcnow().isoformat() + "Z"
            
            return {
                "status": "healthy",
                "loki_status": loki_status,
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
    
    async def query_logs(
        self,
        query: str,
        tenant: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: Optional[int] = None,
        direction: str = "backward",
    ) -> List[Dict[str, Any]]:
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
                # Extract logs from the response
                logs = []
                data = response.get("data", {})
                results = data.get("result", [])
                
                for stream in results:
                    stream_labels = stream.get("stream", {})
                    values = stream.get("values", [])
                    
                    for timestamp_ns, log_line in values:
                        # Convert nanosecond timestamp to datetime
                        try:
                            timestamp_s = int(timestamp_ns) / 1_000_000_000
                            dt = datetime.fromtimestamp(timestamp_s)
                            time_str = dt.isoformat() + "Z"
                        except (ValueError, OSError):
                            time_str = timestamp_ns
                        
                        logs.append({
                            "time": time_str,
                            "labels": stream_labels,
                            "log": log_line,
                        })
                
                return logs
            else:
                raise LokiQueryError(f"Query failed: {response}")
                
        except Exception as e:
            logger.error("Query execution failed", query=query, tenant=tenant, error=str(e))
            raise
    
    async def query_range(
        self,
        tenant: str,
        query: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: Optional[int] = None,
        direction: str = "backward",
        **kwargs
    ) -> Dict[str, Any]:
        """Query range from Loki (raw response for compatibility)."""
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
            
            # Add time range if provided
            if start:
                params["start"] = start
            if end:
                params["end"] = end
            
            client = self._get_http_client()
            response = await client.get(
                "/loki/api/v1/query_range",
                params=params,
                tenant=tenant,
            )
            
            return response
                
        except Exception as e:
            logger.error("Query range failed", query=query, tenant=tenant, error=str(e))
            raise
    
    async def get_labels(self, tenant: str) -> List[str]:
        """Get all available labels for a tenant."""
        try:
            self.auth.validate_tenant_access(tenant)
            
            client = self._get_http_client()
            response = await client.get("/loki/api/v1/labels", tenant=tenant)
            
            if response.get("status") == "success":
                labels = response.get("data", [])
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
            
            if response.get("status") == "success":
                values = response.get("data", [])
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
