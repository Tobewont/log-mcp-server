"""Tests for Loki client."""
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from loki_mcp_server.client.loki_client import LokiClient
from loki_mcp_server.config import LokiConfig
from loki_mcp_server.utils.errors import LokiValidationError, LokiQueryError


class TestLokiClient:
    """Test Loki client functionality."""
    
    @pytest.fixture
    def config(self):
        """Create test configuration."""
        return LokiConfig(
            addr="http://localhost:3100",
            username="testuser",
            password="testpass",
        )
    
    @pytest.fixture
    def mock_http_client(self):
        """Create mock HTTP client."""
        return AsyncMock()
    
    @pytest.fixture
    def loki_client(self, config):
        """Create Loki client instance."""
        return LokiClient(config)
    
    @pytest.mark.asyncio
    async def test_health_check_success(self, loki_client, mock_http_client, monkeypatch):
        """Test successful health check."""
        # Mock HTTP response
        mock_response = {"status": "ready"}
        mock_http_client.get.return_value = mock_response
        
        # Patch the HTTP client
        monkeypatch.setattr(loki_client, "_http_client", mock_http_client)
        
        result = await loki_client.health_check()
        
        assert result["status"] == "healthy"
        assert result["loki_status"] == "ready"
        assert "current_time" in result
        assert "server_addr" in result
        
        mock_http_client.get.assert_called_once_with("/ready")
    
    @pytest.mark.asyncio
    async def test_health_check_failure(self, loki_client, mock_http_client, monkeypatch):
        """Test health check failure."""
        # Mock HTTP client to raise exception
        mock_http_client.get.side_effect = Exception("Connection failed")
        
        # Patch the HTTP client
        monkeypatch.setattr(loki_client, "_http_client", mock_http_client)
        
        result = await loki_client.health_check()
        
        assert result["status"] == "unhealthy"
        assert "error" in result
        assert "Connection failed" in result["error"]
    
    @pytest.mark.asyncio
    async def test_get_tenants_success(self, loki_client, mock_http_client, monkeypatch):
        """Test successful tenant retrieval."""
        mock_response = {
            "status": "success",
            "data": ["tenant1", "tenant2", "tenant3"]
        }
        mock_http_client.get.return_value = mock_response
        
        monkeypatch.setattr(loki_client, "_http_client", mock_http_client)
        
        tenants = await loki_client.get_tenants()
        
        assert tenants == ["tenant1", "tenant2", "tenant3"]
        mock_http_client.get.assert_called_once_with("/loki/api/v1/label/tenant/values")
    
    @pytest.mark.asyncio
    async def test_get_tenants_no_data(self, loki_client, mock_http_client, monkeypatch):
        """Test tenant retrieval with no data."""
        mock_response = {"status": "success"}  # No data field
        mock_http_client.get.return_value = mock_response
        
        monkeypatch.setattr(loki_client, "_http_client", mock_http_client)
        
        tenants = await loki_client.get_tenants()
        
        assert tenants == []
    
    @pytest.mark.asyncio
    async def test_query_logs_success(self, loki_client, mock_http_client, monkeypatch, sample_loki_response):
        """Test successful log query."""
        mock_http_client.get.return_value = sample_loki_response
        
        monkeypatch.setattr(loki_client, "_http_client", mock_http_client)
        
        result = await loki_client.query_logs(
            query='{job="test"}',
            tenant="test-tenant",
            limit=100,
        )
        
        assert result["result_type"] == "streams"
        assert result["total_entries"] == 2
        assert len(result["logs"]) == 2
        
        # Check formatted log entry
        log_entry = result["logs"][0]
        assert "timestamp" in log_entry
        assert "labels" in log_entry
        assert "line" in log_entry
        
        mock_http_client.get.assert_called_once()
        call_args = mock_http_client.get.call_args
        assert call_args[0][0] == "/loki/api/v1/query_range"
        assert call_args[1]["tenant"] == "test-tenant"
    
    @pytest.mark.asyncio
    async def test_query_logs_validation_errors(self, loki_client):
        """Test query validation errors."""
        # Empty query
        with pytest.raises(LokiValidationError, match="Query cannot be empty"):
            await loki_client.query_logs("", "tenant")
        
        # Invalid limit
        with pytest.raises(LokiValidationError, match="Limit must be positive"):
            await loki_client.query_logs("query", "tenant", limit=0)
        
        # Limit exceeds maximum
        with pytest.raises(LokiValidationError, match="exceeds maximum"):
            await loki_client.query_logs("query", "tenant", limit=10000)
    
    @pytest.mark.asyncio
    async def test_get_labels_success(self, loki_client, mock_http_client, monkeypatch, sample_labels_response):
        """Test successful labels retrieval."""
        mock_http_client.get.return_value = sample_labels_response
        
        monkeypatch.setattr(loki_client, "_http_client", mock_http_client)
        
        labels = await loki_client.get_labels("test-tenant")
        
        assert labels == ["job", "level", "instance"]
        
        mock_http_client.get.assert_called_once_with("/loki/api/v1/labels", tenant="test-tenant")
    
    @pytest.mark.asyncio
    async def test_get_label_values_success(self, loki_client, mock_http_client, monkeypatch, sample_label_values_response):
        """Test successful label values retrieval."""
        mock_http_client.get.return_value = sample_label_values_response
        
        monkeypatch.setattr(loki_client, "_http_client", mock_http_client)
        
        values = await loki_client.get_label_values("test-tenant", "job")
        
        assert values == ["test", "production", "staging"]
        
        mock_http_client.get.assert_called_once_with("/loki/api/v1/label/job/values", tenant="test-tenant")
    
    @pytest.mark.asyncio
    async def test_get_label_values_validation(self, loki_client):
        """Test label values validation."""
        with pytest.raises(LokiValidationError, match="Label name cannot be empty"):
            await loki_client.get_label_values("tenant", "")
    
    def test_format_query_response_streams(self, loki_client, sample_loki_response):
        """Test formatting of stream query response."""
        result = loki_client._format_query_response(sample_loki_response["data"])
        
        assert result["result_type"] == "streams"
        assert result["total_entries"] == 2
        assert len(result["logs"]) == 2
        
        # Check timestamp formatting
        log_entry = result["logs"][0]
        assert log_entry["timestamp"].endswith("Z")
        assert log_entry["labels"] == {"job": "test", "level": "info"}
        assert log_entry["line"] == "Test log message 1"
    
    def test_format_query_response_vector(self, loki_client):
        """Test formatting of vector query response."""
        vector_data = {
            "resultType": "vector",
            "result": [
                {
                    "metric": {"job": "test"},
                    "value": [1640995200, "42"]
                }
            ]
        }
        
        result = loki_client._format_query_response(vector_data)
        
        assert result["result_type"] == "vector"
        assert len(result["metrics"]) == 1
        
        metric = result["metrics"][0]
        assert metric["labels"] == {"job": "test"}
        assert metric["timestamp"] == 1640995200
        assert metric["value"] == "42"
