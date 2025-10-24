"""
Async HTTP client for Loki API interactions.
"""
import asyncio
from typing import Any, Dict, Optional, Union
from urllib.parse import urljoin

import httpx
import structlog

from ..config import LokiConfig
from ..utils.errors import LokiHTTPError, LokiConnectionError

logger = structlog.get_logger(__name__)


class AsyncHTTPClient:
    """Async HTTP client for Loki API calls with retry and timeout support."""
    
    def __init__(self, config: LokiConfig):
        """Initialize the HTTP client with configuration."""
        self.config = config
        self._client: Optional[httpx.AsyncClient] = None
        
    async def __aenter__(self) -> "AsyncHTTPClient":
        """Async context manager entry."""
        await self._ensure_client()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.close()
        
    async def _ensure_client(self) -> None:
        """Ensure HTTP client is initialized."""
        if self._client is None:
            timeout = httpx.Timeout(
                connect=self.config.connect_timeout,
                read=self.config.read_timeout,
                write=self.config.write_timeout,
                pool=self.config.pool_timeout,
            )
            
            self._client = httpx.AsyncClient(
                timeout=timeout,
                verify=not self.config.tls_skip_verify,
                limits=httpx.Limits(
                    max_keepalive_connections=10,
                    max_connections=20,
                ),
            )
            
    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
            
    def _get_headers(self, tenant: Optional[str] = None) -> Dict[str, str]:
        """Get HTTP headers for requests."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        
        # Add authentication headers
        if self.config.username and self.config.password:
            import base64
            auth_string = f"{self.config.username}:{self.config.password}"
            auth_bytes = auth_string.encode("utf-8")
            auth_b64 = base64.b64encode(auth_bytes).decode("ascii")
            headers["Authorization"] = f"Basic {auth_b64}"
        elif self.config.bearer_token:
            headers["Authorization"] = f"Bearer {self.config.bearer_token}"
            
        # Add tenant header if specified
        if tenant:
            headers["X-Scope-OrgID"] = tenant
        elif self.config.org_id:
            headers["X-Org-ID"] = self.config.org_id
            
        return headers
        
    async def get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        tenant: Optional[str] = None,
        retries: int = 3,
    ) -> Dict[str, Any]:
        """Make a GET request to Loki API."""
        await self._ensure_client()
        
        url = urljoin(self.config.addr, path)
        headers = self._get_headers(tenant)
        
        last_exception = None
        
        for attempt in range(retries + 1):
            try:
                logger.debug(
                    "Making HTTP GET request",
                    url=url,
                    params=params,
                    attempt=attempt + 1,
                    max_attempts=retries + 1,
                )
                
                response = await self._client.get(
                    url,
                    params=params,
                    headers=headers,
                )
                
                # Check for HTTP errors
                if response.status_code >= 400:
                    error_msg = f"HTTP {response.status_code}: {response.text}"
                    raise LokiHTTPError(
                        f"Request failed: {error_msg}",
                        status_code=response.status_code,
                        response_text=response.text,
                    )
                    
                # Parse JSON response
                try:
                    data = response.json()
                    logger.debug("HTTP request successful", status_code=response.status_code)
                    return data
                except Exception as e:
                    raise LokiHTTPError(f"Failed to parse JSON response: {e}")
                    
            except httpx.ConnectError as e:
                last_exception = LokiConnectionError(f"Connection failed: {e}")
                logger.warning(
                    "Connection failed, retrying",
                    error=str(e),
                    attempt=attempt + 1,
                    max_attempts=retries + 1,
                )
                
            except httpx.TimeoutException as e:
                last_exception = LokiConnectionError(f"Request timeout: {e}")
                logger.warning(
                    "Request timeout, retrying",
                    error=str(e),
                    attempt=attempt + 1,
                    max_attempts=retries + 1,
                )
                
            except LokiHTTPError:
                # Don't retry HTTP errors (4xx, 5xx)
                raise
                
            except Exception as e:
                last_exception = LokiHTTPError(f"Unexpected error: {e}")
                logger.warning(
                    "Unexpected error, retrying",
                    error=str(e),
                    attempt=attempt + 1,
                    max_attempts=retries + 1,
                )
                
            # Wait before retry (exponential backoff)
            if attempt < retries:
                wait_time = 2 ** attempt
                logger.debug("Waiting before retry", wait_seconds=wait_time)
                await asyncio.sleep(wait_time)
                
        # All retries exhausted
        if last_exception:
            raise last_exception
        else:
            raise LokiConnectionError("All retry attempts failed")
            
    async def post(
        self,
        path: str,
        data: Optional[Dict[str, Any]] = None,
        tenant: Optional[str] = None,
        retries: int = 3,
    ) -> Dict[str, Any]:
        """Make a POST request to Loki API."""
        await self._ensure_client()
        
        url = urljoin(self.config.addr, path)
        headers = self._get_headers(tenant)
        
        last_exception = None
        
        for attempt in range(retries + 1):
            try:
                logger.debug(
                    "Making HTTP POST request",
                    url=url,
                    attempt=attempt + 1,
                    max_attempts=retries + 1,
                )
                
                response = await self._client.post(
                    url,
                    json=data,
                    headers=headers,
                )
                
                # Check for HTTP errors
                if response.status_code >= 400:
                    error_msg = f"HTTP {response.status_code}: {response.text}"
                    raise LokiHTTPError(
                        f"Request failed: {error_msg}",
                        status_code=response.status_code,
                        response_text=response.text,
                    )
                    
                # Parse JSON response
                try:
                    data = response.json()
                    logger.debug("HTTP request successful", status_code=response.status_code)
                    return data
                except Exception as e:
                    raise LokiHTTPError(f"Failed to parse JSON response: {e}")
                    
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                last_exception = LokiConnectionError(f"Connection/timeout error: {e}")
                logger.warning(
                    "Connection/timeout error, retrying",
                    error=str(e),
                    attempt=attempt + 1,
                    max_attempts=retries + 1,
                )
                
            except LokiHTTPError:
                # Don't retry HTTP errors
                raise
                
            except Exception as e:
                last_exception = LokiHTTPError(f"Unexpected error: {e}")
                logger.warning(
                    "Unexpected error, retrying",
                    error=str(e),
                    attempt=attempt + 1,
                    max_attempts=retries + 1,
                )
                
            # Wait before retry
            if attempt < retries:
                wait_time = 2 ** attempt
                await asyncio.sleep(wait_time)
                
        # All retries exhausted
        if last_exception:
            raise last_exception
        else:
            raise LokiConnectionError("All retry attempts failed")
