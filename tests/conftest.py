"""Shared fixtures for log_mcp_server tests."""
from __future__ import annotations

from typing import Any, Dict

import pytest

from log_mcp_server.config import LogConfig

# ----- env hygiene ---------------------------------------------------------
_ENV_VARS_TO_CLEAN = (
    "LOKI_ADDR",
    "LOKI_TENANTS",
    "LOKI_USERNAME",
    "LOKI_PASSWORD",
    "LOKI_BEARER_TOKEN",
    "LOKI_BEARER_TOKEN_FILE",
    "LOKI_TLS_SKIP_VERIFY",
    "LOKI_DEFAULT_LIMIT",
    "LOKI_MAX_LIMIT",
    "LOKI_DEFAULT_TIME_RANGE_MINUTES",
    "LOKI_TIMEZONE",
    "LOKI_CONFIG_PATH",
    "LOG_BACKEND",
    "LOG_DEFAULT_LIMIT",
    "LOG_MAX_LIMIT",
    "LOG_DEFAULT_TIME_RANGE_MINUTES",
    "LOG_TIMEZONE",
    "LOG_CONFIG_PATH",
    "LOG_LEVEL",
    "MCP_TRANSPORT",
    "MCP_HOST",
    "MCP_PORT",
    "HEALTH_CHECK_INTERVAL",
    "HEALTH_CHECK_TIMEOUT",
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Make sure the host environment never leaks into a unit test."""
    for var in _ENV_VARS_TO_CLEAN:
        monkeypatch.delenv(var, raising=False)
    # Point to a nonexistent YAML so no fallback paths are searched.
    monkeypatch.setenv("LOG_CONFIG_PATH", str(tmp_path / "nonexistent.yaml"))
    # Change cwd to tmp_path so pydantic-settings won't find a .env file.
    monkeypatch.chdir(tmp_path)
    yield


@pytest.fixture
def base_config() -> LogConfig:
    """A reasonable default config for unit tests."""
    return LogConfig(
        addr="http://loki.test:3100",
        tenants="tenant-a|tenant-b",
        default_limit=100,
        max_limit=5000,
        default_time_range_minutes=30,
        timezone="UTC",
    )


@pytest.fixture
def sample_streams_response() -> Dict[str, Any]:
    """A canonical Loki streams response."""
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"job": "app", "level": "info"},
                    "values": [
                        ["1700000000000000000", "first message"],
                        ["1700000060000000000", "second message"],
                    ],
                }
            ],
        },
    }


@pytest.fixture
def sample_labels_response() -> Dict[str, Any]:
    return {"status": "success", "data": ["job", "level", "instance"]}


@pytest.fixture
def sample_label_values_response() -> Dict[str, Any]:
    return {"status": "success", "data": ["dev", "prod", "staging"]}
