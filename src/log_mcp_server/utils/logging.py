"""Logging configuration for the log MCP server.

Initialised exactly once on first import. Subsequent calls are no-ops, so
multiple modules calling ``setup_logging`` is safe.
"""
from __future__ import annotations

import logging
import os
import sys

import structlog

_INITIALISED = False


def setup_logging(name: str = "log_mcp_server") -> structlog.BoundLogger:
    """Configure structured logging once and return a bound logger.

    Log level is taken from the ``LOG_LEVEL`` environment variable
    (defaulting to ``INFO``). Subsequent invocations only return a logger.
    """
    global _INITIALISED
    if not _INITIALISED:
        level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
        level = getattr(logging, level_name, logging.INFO)

        # IMPORTANT: log to stderr. stdio MCP transport uses stdout for
        # protocol messages — writing logs there would corrupt the stream.
        logging.basicConfig(
            format="%(message)s",
            stream=sys.stderr,
            level=level,
            force=True,
        )

        structlog.configure(
            processors=[
                structlog.stdlib.filter_by_level,
                structlog.stdlib.add_logger_name,
                structlog.stdlib.add_log_level,
                structlog.stdlib.PositionalArgumentsFormatter(),
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.processors.JSONRenderer(),
            ],
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )
        _INITIALISED = True

    return structlog.get_logger(name)
