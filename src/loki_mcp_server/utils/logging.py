"""
Logging configuration for Loki MCP Server.
"""
import logging
import sys
from typing import Any, Dict

import structlog


def setup_logging(name: str = "loki_mcp_server") -> structlog.BoundLogger:
    """Setup structured logging for the application."""
    
    # Configure standard library logging
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.INFO,
    )
    
    # Configure structlog
    structlog.configure(
        processors=[
            # Add log level and timestamp
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # Use JSON formatter for structured output
            structlog.processors.JSONRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    
    return structlog.get_logger(name)


def log_request(
    logger: structlog.BoundLogger,
    method: str,
    url: str,
    params: Dict[str, Any] = None,
    tenant: str = None,
) -> None:
    """Log HTTP request details."""
    logger.info(
        "HTTP request",
        method=method,
        url=url,
        params=params,
        tenant=tenant,
    )


def log_response(
    logger: structlog.BoundLogger,
    status_code: int,
    response_size: int = None,
    duration_ms: float = None,
) -> None:
    """Log HTTP response details."""
    logger.info(
        "HTTP response",
        status_code=status_code,
        response_size=response_size,
        duration_ms=duration_ms,
    )


def log_error(
    logger: structlog.BoundLogger,
    error: Exception,
    context: str = None,
    **kwargs,
) -> None:
    """Log error with context."""
    logger.error(
        "Error occurred",
        error=str(error),
        error_type=type(error).__name__,
        context=context,
        **kwargs,
        exc_info=True,
    )
