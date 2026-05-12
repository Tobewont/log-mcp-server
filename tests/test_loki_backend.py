"""Tests for the Loki backend."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from log_mcp_server.backends.loki.backend import LokiBackend
from log_mcp_server.config import LogConfig
from log_mcp_server.utils.errors import (
    BackendQueryError,
    ValidationError,
)


@pytest.fixture
def backend(base_config: LogConfig) -> LokiBackend:
    b = LokiBackend(base_config)
    # Force the http client to be considered "open" so we can stub it.
    b.http._client = object()  # type: ignore[attr-defined]
    return b


class TestQueryLogs:
    @pytest.mark.asyncio
    async def test_streams_response_decoded(
        self, backend: LokiBackend, sample_streams_response: Any, monkeypatch
    ):
        backend.http.get = AsyncMock(return_value=sample_streams_response)  # type: ignore[method-assign]

        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2025, 1, 1, tzinfo=timezone.utc)
        entries = await backend.query_logs(
            query='{job="app"}',
            tenant="tenant-a",
            start=start,
            end=end,
            limit=100,
            direction="backward",
        )

        assert len(entries) == 2
        assert entries[0].tenant == "tenant-a"
        assert entries[0].timestamp.tzinfo is not None
        assert entries[0].labels == {"job": "app", "level": "info"}
        assert entries[0].line == "first message"

        # Verify call shape
        call = backend.http.get.await_args
        assert call.args[0] == "/loki/api/v1/query_range"
        assert call.kwargs["tenant"] == "tenant-a"
        params = call.kwargs["params"]
        assert params["query"] == '{job="app"}'
        assert params["limit"] == "100"
        assert params["direction"] == "backward"
        # Time should be ns ints (as strings)
        assert int(params["start"]) > 0
        assert int(params["end"]) > 0

    @pytest.mark.asyncio
    async def test_empty_query_rejected(self, backend: LokiBackend):
        with pytest.raises(ValidationError):
            await backend.query_logs(
                query="",
                tenant="tenant-a",
                start=datetime(2024, 1, 1, tzinfo=timezone.utc),
                end=datetime(2025, 1, 1, tzinfo=timezone.utc),
                limit=100,
                direction="backward",
            )

    @pytest.mark.asyncio
    async def test_invalid_direction_rejected(self, backend: LokiBackend):
        with pytest.raises(ValidationError):
            await backend.query_logs(
                query="{a=\"b\"}",
                tenant="tenant-a",
                start=datetime(2024, 1, 1, tzinfo=timezone.utc),
                end=datetime(2025, 1, 1, tzinfo=timezone.utc),
                limit=100,
                direction="sideways",
            )

    @pytest.mark.asyncio
    async def test_limit_exceeds_max_rejected(self, backend: LokiBackend):
        with pytest.raises(ValidationError, match="exceeds maximum"):
            await backend.query_logs(
                query="{a=\"b\"}",
                tenant="tenant-a",
                start=datetime(2024, 1, 1, tzinfo=timezone.utc),
                end=datetime(2025, 1, 1, tzinfo=timezone.utc),
                limit=99999,
                direction="backward",
            )

    @pytest.mark.asyncio
    async def test_metric_query_rejected(self, backend: LokiBackend):
        backend.http.get = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "status": "success",
                "data": {"resultType": "vector", "result": []},
            }
        )
        with pytest.raises(ValidationError, match="metric"):
            await backend.query_logs(
                query='rate({job="app"}[5m])',
                tenant="tenant-a",
                start=datetime(2024, 1, 1, tzinfo=timezone.utc),
                end=datetime(2025, 1, 1, tzinfo=timezone.utc),
                limit=100,
                direction="backward",
            )

    @pytest.mark.asyncio
    async def test_failure_status_raises(self, backend: LokiBackend):
        backend.http.get = AsyncMock(  # type: ignore[method-assign]
            return_value={"status": "error", "error": "bad query"}
        )
        with pytest.raises(BackendQueryError):
            await backend.query_logs(
                query='{a="b"}',
                tenant="tenant-a",
                start=datetime(2024, 1, 1, tzinfo=timezone.utc),
                end=datetime(2025, 1, 1, tzinfo=timezone.utc),
                limit=100,
                direction="backward",
            )


class TestLabels:
    @pytest.mark.asyncio
    async def test_get_labels(
        self, backend: LokiBackend, sample_labels_response: Any
    ):
        backend.http.get = AsyncMock(return_value=sample_labels_response)  # type: ignore[method-assign]
        labels = await backend.get_labels("tenant-a")
        assert labels == ["job", "level", "instance"]
        call = backend.http.get.await_args
        assert call.args[0] == "/loki/api/v1/labels"
        assert call.kwargs["tenant"] == "tenant-a"

    @pytest.mark.asyncio
    async def test_get_label_values(
        self, backend: LokiBackend, sample_label_values_response: Any
    ):
        backend.http.get = AsyncMock(return_value=sample_label_values_response)  # type: ignore[method-assign]
        values = await backend.get_label_values("tenant-a", "env")
        assert values == ["dev", "prod", "staging"]
        call = backend.http.get.await_args
        assert call.args[0] == "/loki/api/v1/label/env/values"
        assert call.kwargs["tenant"] == "tenant-a"

    @pytest.mark.asyncio
    async def test_get_label_values_with_time(
        self, backend: LokiBackend, sample_label_values_response: Any
    ):
        backend.http.get = AsyncMock(return_value=sample_label_values_response)  # type: ignore[method-assign]
        await backend.get_label_values(
            "tenant-a",
            "env",
            start=datetime(2024, 1, 1, tzinfo=timezone.utc),
            end=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        params = backend.http.get.await_args.kwargs["params"]
        assert "start" in params and "end" in params

    @pytest.mark.asyncio
    async def test_label_url_encoded(
        self, backend: LokiBackend, sample_label_values_response: Any
    ):
        backend.http.get = AsyncMock(return_value=sample_label_values_response)  # type: ignore[method-assign]
        await backend.get_label_values("tenant-a", "weird/label name")
        url = backend.http.get.await_args.args[0]
        assert "weird%2Flabel%20name" in url

    @pytest.mark.asyncio
    async def test_empty_label_rejected(self, backend: LokiBackend):
        with pytest.raises(ValidationError):
            await backend.get_label_values("tenant-a", "")


class TestValidation:
    @pytest.mark.asyncio
    async def test_empty_tenant_rejected(self, backend: LokiBackend):
        # Empty tenants must raise ValidationError (not bare ValueError)
        # so the tools layer's typed error handling sees them.
        with pytest.raises(ValidationError):
            await backend.query_logs(
                query='{a="b"}',
                tenant="",
                start=datetime(2024, 1, 1, tzinfo=timezone.utc),
                end=datetime(2025, 1, 1, tzinfo=timezone.utc),
                limit=100,
                direction="backward",
            )


class TestHealth:
    @pytest.mark.asyncio
    async def test_healthy(self, backend: LokiBackend):
        backend.http.get = AsyncMock(return_value={"version": "2.9.0"})  # type: ignore[method-assign]
        info = await backend.health_check()
        assert info["status"] == "healthy"
        assert info["backend"] == "loki"
        assert "current_time" in info
        clusters = info["clusters"]
        assert len(clusters) == 1
        assert clusters[0]["server_addr"] == backend.addr
        assert clusters[0]["status"] == "healthy"
        assert clusters[0]["version"] == "2.9.0"

    @pytest.mark.asyncio
    async def test_unhealthy(self, backend: LokiBackend):
        backend.http.get = AsyncMock(side_effect=Exception("boom"))  # type: ignore[method-assign]
        info = await backend.health_check()
        assert info["status"] == "unhealthy"
        clusters = info["clusters"]
        assert len(clusters) == 1
        assert "boom" in clusters[0]["error"]
