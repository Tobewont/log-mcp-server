"""Tests for LogConfig."""
from __future__ import annotations

import pytest
import yaml

from log_mcp_server.config import LogConfig


class TestDefaults:
    def test_default_values(self):
        cfg = LogConfig()
        assert cfg.backend == "loki"
        assert cfg.addr == "http://localhost:3100"
        assert cfg.tenants == "fake"
        assert cfg.get_tenant_list() == ["fake"]
        assert cfg.username is None
        assert cfg.get_password() is None
        assert cfg.get_bearer_token() is None
        assert cfg.tls_skip_verify is False
        assert cfg.default_limit == 100
        assert cfg.max_limit == 5000
        assert cfg.default_time_range_minutes == 30
        assert cfg.timezone == "Asia/Shanghai"
        assert cfg.mcp_host == "127.0.0.1"
        assert cfg.mcp_port == 8000
        assert cfg.mcp_transport == "stdio"
        assert cfg.log_level == "INFO"


class TestEnv:
    def test_loki_env(self, monkeypatch):
        monkeypatch.setenv("LOKI_ADDR", "https://loki.example.com")
        monkeypatch.setenv("LOKI_TENANTS", "t1|t2|t3")
        monkeypatch.setenv("LOKI_USERNAME", "user")
        monkeypatch.setenv("LOKI_PASSWORD", "pwd")
        monkeypatch.setenv("LOKI_TLS_SKIP_VERIFY", "true")

        cfg = LogConfig()
        assert cfg.addr == "https://loki.example.com"
        assert cfg.get_tenant_list() == ["t1", "t2", "t3"]
        assert cfg.username == "user"
        assert cfg.get_password() == "pwd"
        assert cfg.tls_skip_verify is True

    def test_log_env(self, monkeypatch):
        monkeypatch.setenv("LOG_DEFAULT_LIMIT", "200")
        monkeypatch.setenv("LOG_MAX_LIMIT", "9999")
        monkeypatch.setenv("LOG_TIMEZONE", "UTC")
        cfg = LogConfig()
        assert cfg.default_limit == 200
        assert cfg.max_limit == 9999
        assert cfg.timezone == "UTC"

    def test_mcp_env(self, monkeypatch):
        monkeypatch.setenv("MCP_HOST", "0.0.0.0")
        monkeypatch.setenv("MCP_PORT", "9000")
        monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        cfg = LogConfig()
        assert cfg.mcp_host == "0.0.0.0"
        assert cfg.mcp_port == 9000
        assert cfg.mcp_transport == "streamable-http"
        assert cfg.log_level == "DEBUG"


class TestYamlConfig:
    def test_yaml_load(self, tmp_path, monkeypatch):
        f = tmp_path / "loki-config.yaml"
        yaml.dump(
            {
                "addr": "https://loki.from-yaml",
                "username": "yaml-user",
                "default_limit": 250,
            },
            f.open("w"),
        )
        monkeypatch.setenv("LOG_CONFIG_PATH", str(f))
        cfg = LogConfig()
        assert cfg.addr == "https://loki.from-yaml"
        assert cfg.username == "yaml-user"
        assert cfg.default_limit == 250

    def test_env_overrides_yaml(self, tmp_path, monkeypatch):
        f = tmp_path / "loki-config.yaml"
        yaml.dump({"addr": "https://yaml", "username": "u-yaml"}, f.open("w"))
        monkeypatch.setenv("LOG_CONFIG_PATH", str(f))
        monkeypatch.setenv("LOKI_USERNAME", "u-env")
        cfg = LogConfig()
        assert cfg.addr == "https://yaml"
        assert cfg.username == "u-env"


class TestValidation:
    def test_addr_invalid(self):
        with pytest.raises(ValueError, match="must start with http"):
            LogConfig(addr="invalid")
        with pytest.raises(ValueError):
            LogConfig(addr="")

    def test_limits_must_be_positive(self):
        with pytest.raises(ValueError):
            LogConfig(default_limit=0)
        with pytest.raises(ValueError):
            LogConfig(max_limit=-1)

    def test_default_le_max(self):
        with pytest.raises(ValueError, match="cannot exceed"):
            LogConfig(default_limit=10000, max_limit=100)

    def test_invalid_timezone(self):
        with pytest.raises(Exception):
            LogConfig(timezone="Made/Up")

    def test_invalid_backend(self):
        with pytest.raises(ValueError, match="Unsupported backend"):
            LogConfig(backend="splunk")

    def test_invalid_port(self):
        with pytest.raises(ValueError):
            LogConfig(mcp_port=0)
        with pytest.raises(ValueError):
            LogConfig(mcp_port=70000)

    def test_cert_without_key_rejected(self, tmp_path):
        cert = tmp_path / "client.crt"
        cert.write_text("dummy")
        with pytest.raises(ValueError, match="must be set together"):
            LogConfig(cert_file=str(cert))

    def test_key_without_cert_rejected(self, tmp_path):
        key = tmp_path / "client.key"
        key.write_text("dummy")
        with pytest.raises(ValueError, match="must be set together"):
            LogConfig(key_file=str(key))


class TestBearerTokenFile:
    def test_loaded_from_file(self, tmp_path):
        f = tmp_path / "token.txt"
        f.write_text("the-secret\n")
        cfg = LogConfig(bearer_token_file=str(f))
        assert cfg.get_bearer_token() == "the-secret"

    def test_missing_file_does_not_crash(self, tmp_path):
        cfg = LogConfig(bearer_token_file=str(tmp_path / "missing"))
        assert cfg.get_bearer_token() is None


class TestSafeConfig:
    def test_redacts_secrets(self):
        cfg = LogConfig(
            addr="https://loki.example.com",
            username="u",
            password="secret",
            bearer_token="token123",
        )
        safe = cfg.get_safe_config()
        assert safe["addr"] == "https://loki.example.com"
        assert safe["username"] == "u"
        assert safe["password"] == "[REDACTED]"
        assert safe["bearer_token"] == "[REDACTED]"


class TestTenantList:
    def test_pipe_separated(self):
        cfg = LogConfig(tenants="a|b|c")
        assert cfg.get_tenant_list() == ["a", "b", "c"]

    def test_whitespace_stripped(self):
        cfg = LogConfig(tenants="  a |b|  c  ")
        assert cfg.get_tenant_list() == ["a", "b", "c"]

    def test_empty_falls_back_to_fake(self):
        cfg = LogConfig(tenants="|||")
        assert cfg.get_tenant_list() == ["fake"]
