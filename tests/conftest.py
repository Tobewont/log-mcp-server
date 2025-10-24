"""Pytest configuration and fixtures."""
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.fixture
def mock_loki_config():
    """Mock Loki configuration for testing."""
    return {
        "addr": "http://localhost:3100",
        "username": None,
        "password": None,
        "bearer_token": None,
        "org_id": None,
        "tls_skip_verify": False,
    }


@pytest.fixture
def mock_httpx_client():
    """Mock httpx async client."""
    client = AsyncMock()
    return client


@pytest.fixture
def sample_loki_response():
    """Sample Loki API response for testing."""
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"job": "test", "level": "info"},
                    "values": [
                        ["1640995200000000000", "Test log message 1"],
                        ["1640995260000000000", "Test log message 2"],
                    ],
                }
            ],
        },
    }


@pytest.fixture
def sample_labels_response():
    """Sample labels API response for testing."""
    return {"status": "success", "data": ["job", "level", "instance"]}


@pytest.fixture
def sample_label_values_response():
    """Sample label values API response for testing."""
    return {"status": "success", "data": ["test", "production", "staging"]}
