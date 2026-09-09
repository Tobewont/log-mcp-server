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

    def test_mcp_path_default(self):
        cfg = LogConfig()
        assert cfg.mcp_path == "/mcp"

    def test_mcp_path_env(self, monkeypatch):
        monkeypatch.setenv("MCP_PATH", "/logs-mcp")
        cfg = LogConfig()
        assert cfg.mcp_path == "/logs-mcp"

    def test_mcp_path_strips_trailing_slash(self, monkeypatch):
        monkeypatch.setenv("MCP_PATH", "/logs-mcp/")
        cfg = LogConfig()
        assert cfg.mcp_path == "/logs-mcp"

    def test_mcp_path_rejects_missing_leading_slash(self, monkeypatch):
        monkeypatch.setenv("MCP_PATH", "logs-mcp")
        with pytest.raises(Exception):
            LogConfig()

    def test_mcp_path_rejects_empty(self, monkeypatch):
        monkeypatch.setenv("MCP_PATH", "   ")
        with pytest.raises(Exception):
            LogConfig()


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


class TestClientTenants:
    def test_unset_means_no_filter(self):
        cfg = LogConfig(tenants="t1|t2|t3")
        assert cfg.get_client_tenant_list() is None

    def test_subset_accepted(self):
        cfg = LogConfig(tenants="t1|t2|t3", client_tenants="t1,t3")
        assert cfg.get_client_tenant_list() == ["t1", "t3"]

    def test_via_env(self, monkeypatch):
        monkeypatch.setenv("LOKI_TENANTS", "alpha|beta|gamma")
        monkeypatch.setenv("LOKI_CLIENT_TENANTS", "beta, gamma")
        cfg = LogConfig()
        assert cfg.get_client_tenant_list() == ["beta", "gamma"]

    def test_non_subset_rejected(self):
        with pytest.raises(ValueError, match="not a subset"):
            LogConfig(tenants="t1|t2", client_tenants="t1,t9")

    def test_blank_string_treated_as_unset(self):
        cfg = LogConfig(tenants="t1|t2", client_tenants="   ,  , ")
        assert cfg.get_client_tenant_list() is None

    def test_whitespace_trimmed(self):
        cfg = LogConfig(tenants="t1|t2", client_tenants="  t1  ,  t2  ")
        assert cfg.get_client_tenant_list() == ["t1", "t2"]


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


def test_default_verbosity_defaults_to_compact():
    from log_mcp_server.config import LogConfig

    cfg = LogConfig(addr="http://loki.test:3100", tenants="a")
    assert cfg.default_verbosity == "compact"
    assert cfg.max_line_chars == 2000
    assert cfg.fold_repeats is True


def test_default_verbosity_normalised_and_validated():
    cfg = LogConfig(addr="http://loki.test:3100", tenants="a", default_verbosity="FULL")
    assert cfg.default_verbosity == "full"
    with pytest.raises(Exception):
        LogConfig(addr="http://loki.test:3100", tenants="a", default_verbosity="loud")


def test_max_line_chars_must_be_positive():
    with pytest.raises(Exception):
        LogConfig(addr="http://loki.test:3100", tenants="a", max_line_chars=0)


def test_strip_ansi_and_fold_scope_defaults():
    cfg = LogConfig(addr="http://loki.test:3100", tenants="a")
    assert cfg.strip_ansi is True
    assert cfg.fold_scope == "adjacent"


def test_fold_scope_normalised_and_validated():
    cfg = LogConfig(addr="http://loki.test:3100", tenants="a", fold_scope="  GLOBAL  ")
    assert cfg.fold_scope == "global"
    with pytest.raises(Exception):
        LogConfig(addr="http://loki.test:3100", tenants="a", fold_scope="everything")


def test_strip_ansi_can_be_disabled():
    cfg = LogConfig(addr="http://loki.test:3100", tenants="a", strip_ansi=False)
    assert cfg.strip_ansi is False


def test_default_exclude_loggers_defaults_to_empty():
    """开箱不排除任何 logger——"隐藏内容"必须是用户主动选择的。"""
    cfg = LogConfig(addr="http://loki.test:3100", tenants="a")
    assert cfg.default_exclude_loggers == []


def test_default_exclude_loggers_parses_comma_separated_string():
    cfg = LogConfig(
        addr="http://loki.test:3100",
        tenants="a",
        default_exclude_loggers=" httpcore.*, uvicorn.protocols.http.h11_impl , ",
    )
    assert cfg.default_exclude_loggers == [
        "httpcore.*",
        "uvicorn.protocols.http.h11_impl",
    ]


def test_default_exclude_loggers_accepts_list():
    cfg = LogConfig(
        addr="http://loki.test:3100",
        tenants="a",
        default_exclude_loggers=["httpcore.*", " ", "uvicorn.*"],
    )
    assert cfg.default_exclude_loggers == ["httpcore.*", "uvicorn.*"]


def test_default_exclude_loggers_from_env(monkeypatch):
    monkeypatch.setenv("LOG_DEFAULT_EXCLUDE_LOGGERS", "httpcore.*,asyncio")
    cfg = LogConfig(addr="http://loki.test:3100", tenants="a")
    assert cfg.default_exclude_loggers == ["httpcore.*", "asyncio"]


def test_default_exclude_loggers_rejects_bad_type():
    with pytest.raises(Exception):
        LogConfig(
            addr="http://loki.test:3100",
            tenants="a",
            default_exclude_loggers=123,
        )


def test_dedup_timestamp_defaults_to_false():
    cfg = LogConfig(addr="http://loki.test:3100", tenants="a")
    assert cfg.dedup_timestamp is False


def test_dedup_timestamp_can_be_enabled():
    cfg = LogConfig(addr="http://loki.test:3100", tenants="a", dedup_timestamp=True)
    assert cfg.dedup_timestamp is True


def test_dedup_timestamp_from_env(monkeypatch):
    monkeypatch.setenv("LOG_DEDUP_TIMESTAMP", "false")
    cfg = LogConfig(addr="http://loki.test:3100", tenants="a")
    assert cfg.dedup_timestamp is False


# ---------------------------------------------------------------------------
# A 档零信息损失瘦身：三项配置默认全开，可被构造参数 / 环境变量覆盖
# ---------------------------------------------------------------------------
def test_slim_flags_default_to_true():
    cfg = LogConfig(addr="http://loki.test:3100", tenants="a")
    assert cfg.hoist_common_prefix is True
    assert cfg.collapse_whitespace is True
    assert cfg.hoist_common_date is True


def test_slim_flags_can_be_disabled_by_kwargs():
    cfg = LogConfig(
        addr="http://loki.test:3100",
        tenants="a",
        hoist_common_prefix=False,
        collapse_whitespace=False,
        hoist_common_date=False,
    )
    assert cfg.hoist_common_prefix is False
    assert cfg.collapse_whitespace is False
    assert cfg.hoist_common_date is False


def test_slim_flags_from_env(monkeypatch):
    monkeypatch.setenv("LOG_HOIST_COMMON_PREFIX", "false")
    monkeypatch.setenv("LOG_COLLAPSE_WHITESPACE", "false")
    monkeypatch.setenv("LOG_HOIST_COMMON_DATE", "false")
    cfg = LogConfig(addr="http://loki.test:3100", tenants="a")
    assert cfg.hoist_common_prefix is False
    assert cfg.collapse_whitespace is False
    assert cfg.hoist_common_date is False
