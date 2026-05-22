"""Loki 的异步 HTTP client。

该 client 在后端启动时 **只创建一次**，并在并发的工具调用之间共享，
保留连接池和 keep-alive，避免每次请求都重连。
"""
from __future__ import annotations

import asyncio
import ssl
from typing import Any, Dict, Optional

import httpx
import structlog

from ...config import LogConfig
from ...utils.errors import BackendConnectionError, BackendHTTPError
from .auth import build_headers

logger = structlog.get_logger(__name__)


def _build_ssl_context(config: LogConfig) -> ssl.SSLContext | bool:
    """Build the verify argument for httpx based on TLS settings."""
    if config.tls_skip_verify:
        return False
    if config.ca_file or config.cert_file or config.key_file:
        ctx = ssl.create_default_context(cafile=config.ca_file)
        if config.cert_file:
            ctx.load_cert_chain(certfile=config.cert_file, keyfile=config.key_file)
        return ctx
    return True


class LokiHTTPClient:
    """Long-lived async HTTP client for Loki.

    Safe to share across concurrent coroutines: ``httpx.AsyncClient`` is
    itself concurrent-safe.
    """

    def __init__(self, config: LogConfig, addr_override: Optional[str] = None):
        self.config = config
        # Pick the effective address: explicit override wins, otherwise the
        # first configured Loki addr (single-cluster path).
        self.addr = (addr_override or config.get_loki_addrs()[0]).rstrip("/")
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        """Initialise the underlying ``httpx.AsyncClient``."""
        async with self._lock:
            if self._client is not None:
                return
            timeout = httpx.Timeout(
                connect=self.config.connect_timeout,
                read=self.config.read_timeout,
                write=self.config.write_timeout,
                pool=self.config.pool_timeout,
            )
            self._client = httpx.AsyncClient(
                base_url=self.addr,
                timeout=timeout,
                verify=_build_ssl_context(self.config),
                limits=httpx.Limits(
                    max_keepalive_connections=10,
                    max_connections=20,
                ),
            )
            logger.debug("Loki HTTP client opened", base_url=self.addr)

    async def close(self) -> None:
        async with self._lock:
            if self._client is not None:
                await self._client.aclose()
                self._client = None
                logger.debug("Loki HTTP client closed")

    def _ensure_open(self) -> httpx.AsyncClient:
        if self._client is None:
            raise BackendConnectionError(
                "HTTP client not open. Did you forget to enter the backend "
                "as an async context manager?"
            )
        return self._client

    async def get(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        tenant: Optional[str] = None,
        retries: int = 2,
        accept_text: bool = False,
    ) -> Any:
        """Perform a GET. Returns parsed JSON, or raw text if ``accept_text``."""
        client = self._ensure_open()
        headers = build_headers(self.config, tenant=tenant)
        # httpx joins ``base_url`` (already set on the client) with the
        # relative path automatically.
        relative = path if path.startswith("/") else f"/{path}"

        last_error: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                logger.debug(
                    "Loki HTTP GET",
                    base_url=self.addr,
                    path=relative,
                    params=params,
                    attempt=attempt + 1,
                    retries=retries,
                )
                response = await client.get(relative, params=params, headers=headers)

                if response.status_code >= 400:
                    raise BackendHTTPError(
                        f"HTTP {response.status_code}: {response.text}",
                        status_code=response.status_code,
                        response_text=response.text,
                    )

                if accept_text:
                    return response.text
                try:
                    return response.json()
                except Exception as e:
                    raise BackendHTTPError(
                        f"Failed to parse JSON response: {e}",
                        status_code=response.status_code,
                        response_text=response.text,
                    ) from e

            except BackendHTTPError:
                raise
            except asyncio.CancelledError:
                raise
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                err_msg = str(e) or type(e).__name__
                last_error = BackendConnectionError(
                    f"Connection/timeout error: {err_msg}"
                )
                if attempt < retries:
                    logger.warning(
                        "Loki HTTP transient error, retrying",
                        error=err_msg,
                        attempt=attempt + 1,
                        retries=retries,
                        base_url=self.addr,
                        path=relative,
                    )
                else:
                    logger.warning(
                        "Loki HTTP transient error, no more retries",
                        error=err_msg,
                        attempt=attempt + 1,
                        retries=retries,
                        base_url=self.addr,
                        path=relative,
                    )
            except Exception as e:
                err_msg = str(e) or type(e).__name__
                last_error = BackendConnectionError(
                    f"Unexpected HTTP error: {err_msg}"
                )
                logger.warning(
                    "Loki HTTP unexpected error",
                    error=err_msg,
                    attempt=attempt + 1,
                    retries=retries,
                )

            if attempt < retries:
                await asyncio.sleep(min(2**attempt, 5))

        assert last_error is not None
        raise last_error
