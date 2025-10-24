"""Tests for configuration management."""
import os
import tempfile
from pathlib import Path

import pytest
import yaml

from loki_mcp_server.config import LokiConfig


class TestLokiConfig:
    """Test configuration loading and validation."""
    
    def test_default_config(self):
        """Test default configuration values."""
        config = LokiConfig()
        
        assert config.addr == "http://localhost:3100"
        assert config.username is None
        assert config.password is None
        assert config.bearer_token is None
        assert config.org_id is None
        assert config.tls_skip_verify is False
        assert config.default_limit == 1000
        assert config.max_limit == 5000
    
    def test_env_var_config(self, monkeypatch):
        """Test configuration from environment variables."""
        monkeypatch.setenv("LOKI_ADDR", "https://loki.example.com")
        monkeypatch.setenv("LOKI_USERNAME", "testuser")
        monkeypatch.setenv("LOKI_PASSWORD", "testpass")
        monkeypatch.setenv("LOKI_ORG_ID", "test-org")
        monkeypatch.setenv("LOKI_TLS_SKIP_VERIFY", "true")
        
        config = LokiConfig()
        
        assert config.addr == "https://loki.example.com"
        assert config.username == "testuser"
        assert config.password == "testpass"
        assert config.org_id == "test-org"
        assert config.tls_skip_verify is True
    
    def test_config_file_loading(self, tmp_path):
        """Test configuration loading from YAML file."""
        config_file = tmp_path / "loki-config.yaml"
        config_data = {
            "addr": "https://loki.config.com",
            "username": "configuser",
            "default_limit": 2000,
        }
        
        with open(config_file, "w") as f:
            yaml.dump(config_data, f)
        
        # Change to temp directory so config file is found
        original_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            config = LokiConfig()
            
            assert config.addr == "https://loki.config.com"
            assert config.username == "configuser"
            assert config.default_limit == 2000
        finally:
            os.chdir(original_cwd)
    
    def test_env_overrides_config_file(self, tmp_path, monkeypatch):
        """Test that environment variables override config file."""
        config_file = tmp_path / "loki-config.yaml"
        config_data = {
            "addr": "https://loki.config.com",
            "username": "configuser",
        }
        
        with open(config_file, "w") as f:
            yaml.dump(config_data, f)
        
        # Set environment variable
        monkeypatch.setenv("LOKI_USERNAME", "envuser")
        
        original_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            config = LokiConfig()
            
            assert config.addr == "https://loki.config.com"  # From file
            assert config.username == "envuser"  # From env (overrides file)
        finally:
            os.chdir(original_cwd)
    
    def test_invalid_addr_validation(self):
        """Test address validation."""
        with pytest.raises(ValueError, match="must start with http"):
            LokiConfig(addr="invalid-url")
        
        with pytest.raises(ValueError, match="cannot be empty"):
            LokiConfig(addr="")
    
    def test_invalid_limit_validation(self):
        """Test limit validation."""
        with pytest.raises(ValueError, match="must be positive"):
            LokiConfig(default_limit=0)
        
        with pytest.raises(ValueError, match="must be positive"):
            LokiConfig(max_limit=-1)
    
    def test_bearer_token_from_file(self, tmp_path):
        """Test loading bearer token from file."""
        token_file = tmp_path / "token.txt"
        token_file.write_text("secret-token-123")
        
        config = LokiConfig(bearer_token_file=str(token_file))
        
        assert config.bearer_token == "secret-token-123"
    
    def test_get_safe_config(self):
        """Test safe configuration output (redacted sensitive data)."""
        config = LokiConfig(
            addr="https://loki.example.com",
            username="user",
            password="secret",
            bearer_token="token123",
        )
        
        safe_config = config.get_safe_config()
        
        assert safe_config["addr"] == "https://loki.example.com"
        assert safe_config["username"] == "user"
        assert safe_config["password"] == "[REDACTED]"
        assert safe_config["bearer_token"] == "[REDACTED]"
