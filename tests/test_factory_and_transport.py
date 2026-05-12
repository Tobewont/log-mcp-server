"""Tests for backend factory + transport selection."""
from __future__ import annotations

import pytest

from log_mcp_server.backends.factory import create_backend
from log_mcp_server.backends.fanout import FanoutBackend
from log_mcp_server.backends.health_cache import HealthCache
from log_mcp_server.backends.loki.backend import LokiBackend
from log_mcp_server.config import LogConfig


class TestFactory:
    def test_single_addr_returns_loki_backend(self):
        cfg = LogConfig(addr="http://loki:3100")
        b, cache = create_backend(cfg)
        assert isinstance(b, LokiBackend)
        assert cache is None
        assert b.addr == "http://loki:3100"
        assert b.cluster_id == "loki:3100"

    def test_multi_addr_returns_fanout_with_health_cache(self):
        cfg = LogConfig(addr="http://loki-bj:3100|http://loki-sh:3100|http://loki-sg:3100")
        b, cache = create_backend(cfg)
        assert isinstance(b, FanoutBackend)
        assert isinstance(cache, HealthCache)
        addrs = [sub.addr for sub in b._backends]  # type: ignore[attr-defined]
        assert addrs == [
            "http://loki-bj:3100",
            "http://loki-sh:3100",
            "http://loki-sg:3100",
        ]
        cluster_ids = [sub.cluster_id for sub in b._backends]  # type: ignore[attr-defined]
        assert cluster_ids == ["loki-bj:3100", "loki-sh:3100", "loki-sg:3100"]

    def test_addr_validates_each_part(self):
        with pytest.raises(ValueError):
            LogConfig(addr="http://loki:3100|invalid-no-scheme")


class TestTransportResolution:
    def test_default_is_stdio(self):
        cfg = LogConfig(addr="http://loki:3100")
        assert cfg.mcp_transport == "stdio"

    def test_explicit_streamable_http(self):
        cfg = LogConfig(addr="http://loki:3100", mcp_transport="streamable-http")
        assert cfg.mcp_transport == "streamable-http"

    def test_explicit_sse(self):
        cfg = LogConfig(addr="http://loki:3100", mcp_transport="sse")
        assert cfg.mcp_transport == "sse"

    def test_explicit_stdio(self):
        cfg = LogConfig(addr="http://loki:3100", mcp_transport="stdio")
        assert cfg.mcp_transport == "stdio"

    def test_invalid_rejected(self):
        with pytest.raises(ValueError):
            LogConfig(mcp_transport="websocket")

    def test_env_mcp_transport(self, monkeypatch):
        monkeypatch.setenv("MCP_TRANSPORT", "sse")
        cfg = LogConfig(addr="http://loki:3100")
        assert cfg.mcp_transport == "sse"


class TestLokiAddrs:
    def test_get_loki_addrs_single(self):
        cfg = LogConfig(addr="http://loki:3100")
        assert cfg.get_loki_addrs() == ["http://loki:3100"]

    def test_get_loki_addrs_multiple(self):
        cfg = LogConfig(addr="http://a:3100|http://b:3100")
        assert cfg.get_loki_addrs() == ["http://a:3100", "http://b:3100"]

    def test_trailing_slash_stripped_each(self):
        cfg = LogConfig(addr="http://a:3100/|http://b:3100/")
        assert cfg.get_loki_addrs() == ["http://a:3100", "http://b:3100"]
