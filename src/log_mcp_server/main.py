#!/usr/bin/env python3
"""Log MCP Server entry point.

The backend (and its underlying ``httpx.AsyncClient``) is opened and closed
inside FastMCP's own event loop via the ``lifespan`` context manager.  This
avoids the cross-event-loop hazard of binding the HTTP client to a loop
that has already been closed before tools run.
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from mcp.server.fastmcp import FastMCP

from .backends.factory import create_backend
from .config import LogConfig
from .tools import initialize_tools, register_tools
from .utils.logging import setup_logging

logger = setup_logging(__name__)


def _build_lifespan(config: LogConfig):
    """Return a FastMCP lifespan that opens/closes the active backend."""

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[None]:
        backend, health_cache = create_backend(config)
        logger.info(
            "Opening backend",
            backend=backend.name,
            tenants=backend.tenants,
            health_cache="enabled" if health_cache else "disabled",
        )
        async with backend:
            if health_cache is not None:
                await health_cache.start()
            initialize_tools(backend, config)
            logger.info("Backend ready", backend=backend.name)
            try:
                yield
            finally:
                if health_cache is not None:
                    await health_cache.stop()
                logger.info("Closing backend", backend=backend.name)

    return lifespan


def _build_server(config: LogConfig) -> FastMCP:
    is_debug = config.log_level == "DEBUG"
    mcp = FastMCP(
        name="log-mcp-server",
        instructions=(
            "A log MCP server with multi-tenant and multi-cluster support. "
            "Unhealthy clusters are automatically skipped. "
            "Recommended workflow for multi-tenant: "
            "1) Call get_labels() to discover available label names per tenant. "
            "2) Call get_label_values(label='<relevant label>') to find which "
            "tenant owns the target value. "
            "3) Call query_logs(tenant='<id>', query='...') to query only "
            "that tenant. "
            "Labels are user-defined (e.g. app, job, env, namespace, etc.). "
            "This avoids slow fan-out across all tenants and yields fast, "
            "precise results. "
            "If the user explicitly names a Loki instance (e.g. "
            "'loki.example.com' or 'loki:3100'), pass it as the optional "
            "'instance' argument so the query is restricted to that single "
            "cluster instead of fanning out. "
            "IMPORTANT: log-query tools (query_logs, get_labels, "
            "get_label_values) require the MCP client to declare an "
            "explicit tenant subset, either via the X-Allowed-Tenants "
            "request header (HTTP) or the LOKI_CLIENT_TENANTS env var "
            "(stdio). When this is unset the tools refuse to run with a "
            "clear error — call health_check first to inspect the active "
            "scope; if it shows '(unset)', tell the user to add a tenant "
            "list to their MCP client config and stop. Never try a tenant "
            "outside the allowed subset; it will be rejected as Forbidden. "
            "Performance: prefer querying a single tenant via 'tenant=' "
            "whenever possible; the more tenants in 'Allowed Tenants', "
            "the slower and noisier the default fan-out becomes — if you "
            "see 3+ tenants and notice unrelated results / errors, "
            "advise the user to narrow their client config."
        ),
        debug=is_debug,
        log_level=config.log_level,
        host=config.mcp_host,
        port=config.mcp_port,
        lifespan=_build_lifespan(config),
    )
    register_tools(mcp)
    return mcp


def _select_transport_from_argv(argv: list[str]) -> Optional[str]:
    """Optional CLI override for transport selection.

    Accepts ``stdio``, ``sse`` or ``streamable-http`` as a positional
    argument.  Returns ``None`` to fall back to config.
    """
    for arg in argv[1:]:
        if arg in ("stdio", "sse", "streamable-http"):
            return arg
    return None


def cli_main() -> None:
    """CLI entry point.

    Transport selection (highest priority first):

    1. Explicit CLI positional argument (``stdio`` / ``sse`` /
       ``streamable-http``).
    2. ``MCP_TRANSPORT`` env var / config field (default: ``stdio``).
    """
    try:
        config = LogConfig()
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to load configuration", error=str(e), exc_info=True)
        sys.exit(1)

    logger.info(
        "Configuration loaded",
        backend=config.backend,
        addrs=config.get_loki_addrs(),
        tenants=config.get_tenant_list(),
        mcp_transport=config.mcp_transport,
        mcp_host=config.mcp_host,
        mcp_port=config.mcp_port,
        log_level=config.log_level,
    )

    transport = _select_transport_from_argv(sys.argv) or config.mcp_transport
    mcp = _build_server(config)

    logger.info("Starting FastMCP", transport=transport)
    try:
        mcp.run(transport=transport)
    except KeyboardInterrupt:
        logger.info("Server interrupted by user")
    except Exception as e:  # noqa: BLE001
        logger.error("Server error", error=str(e), exc_info=True)
        raise


if __name__ == "__main__":
    cli_main()
