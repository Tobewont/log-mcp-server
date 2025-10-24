"""
Configuration management for Loki MCP Server.
"""
import os
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

import structlog

logger = structlog.get_logger(__name__)


class LokiConfig(BaseSettings):
    """Configuration for Loki MCP Server."""
    
    model_config = SettingsConfigDict(
        env_prefix="LOKI_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )
    
    # Server configuration
    server_mode: Literal["stdio", "sse"] = Field(
        default="stdio",
        description="Server mode: stdio for process communication, sse for HTTP/SSE"
    )
    server_host: str = Field(
        default="0.0.0.0",
        description="Host address to bind the SSE server (only used in sse mode)"
    )
    server_port: int = Field(
        default=8080,
        description="Port to bind the SSE server (only used in sse mode)"
    )
    
    # Loki server configuration
    addr: str = Field(
        default="http://localhost:3100",
        description="Loki server address (e.g., http://localhost:3100)"
    )
    
    # Authentication
    username: Optional[str] = Field(
        default=None,
        description="Username for basic authentication"
    )
    password: Optional[str] = Field(
        default=None,
        description="Password for basic authentication"
    )
    bearer_token: Optional[str] = Field(
        default=None,
        description="Bearer token for authentication"
    )
    bearer_token_file: Optional[str] = Field(
        default=None,
        description="Path to file containing bearer token"
    )
    
    # Organization/tenant configuration
    org_id: Optional[str] = Field(
        default=None,
        description="Organization ID header (X-Org-ID)"
    )
    
    # TLS configuration
    ca_file: Optional[str] = Field(
        default=None,
        description="Path to CA certificate file"
    )
    cert_file: Optional[str] = Field(
        default=None,
        description="Path to client certificate file"
    )
    key_file: Optional[str] = Field(
        default=None,
        description="Path to client private key file"
    )
    tls_skip_verify: bool = Field(
        default=False,
        description="Skip TLS certificate verification"
    )
    
    # HTTP client timeouts (in seconds)
    connect_timeout: float = Field(
        default=10.0,
        description="Connection timeout in seconds"
    )
    read_timeout: float = Field(
        default=30.0,
        description="Read timeout in seconds"
    )
    write_timeout: float = Field(
        default=10.0,
        description="Write timeout in seconds"
    )
    pool_timeout: float = Field(
        default=10.0,
        description="Connection pool timeout in seconds"
    )
    
    # Query limits
    default_limit: int = Field(
        default=1000,
        description="Default limit for query results"
    )
    max_limit: int = Field(
        default=5000,
        description="Maximum limit for query results"
    )
    
    @field_validator("addr")
    @classmethod
    def validate_addr(cls, v: str) -> str:
        """Validate Loki server address."""
        if not v:
            raise ValueError("Loki server address cannot be empty")
        if not v.startswith(("http://", "https://")):
            raise ValueError("Loki server address must start with http:// or https://")
        return v.rstrip("/")
    
    @field_validator("default_limit", "max_limit")
    @classmethod
    def validate_limits(cls, v: int) -> int:
        """Validate query limits."""
        if v <= 0:
            raise ValueError("Query limits must be positive integers")
        return v
    
    @field_validator("server_port")
    @classmethod
    def validate_server_port(cls, v: int) -> int:
        """Validate server port."""
        if not (1 <= v <= 65535):
            raise ValueError("Server port must be between 1 and 65535")
        return v
    
    @field_validator("server_host")
    @classmethod
    def validate_server_host(cls, v: str) -> str:
        """Validate server host."""
        if not v.strip():
            raise ValueError("Server host cannot be empty")
        return v.strip()
    
    def __init__(self, **kwargs):
        """Initialize configuration with environment variables and config files."""
        # Load from config files first (lower priority)
        config_data = self._load_config_files()
        
        # Load server configuration from MCP_ environment variables
        server_config = self._load_server_config_from_env()
        config_data.update(server_config)
        
        # Merge with provided kwargs (highest priority)
        config_data.update(kwargs)
        
        # Initialize with merged data
        super().__init__(**config_data)
        
        # Load bearer token from file if specified
        self._load_bearer_token_from_file()
        
        logger.info(
            "Configuration loaded",
            addr=self.addr,
            server_mode=self.server_mode,
            server_host=self.server_host,
            server_port=self.server_port,
            has_username=bool(self.username),
            has_password=bool(self.password),
            has_bearer_token=bool(self.bearer_token),
            org_id=self.org_id,
            tls_skip_verify=self.tls_skip_verify,
        )
    
    def _load_config_files(self) -> dict:
        """Load configuration from YAML files."""
        config_data = {}
        
        # Configuration file paths (in order of priority)
        config_paths = [
            os.environ.get("LOKI_CONFIG_PATH"),  # Custom path from env
            Path.cwd() / "loki-config.yaml",    # Current directory
            Path.cwd() / ".loki-config.yaml",   # Hidden file in current directory
            Path.home() / ".loki-config.yaml",  # Home directory
        ]
        
        for config_path in config_paths:
            if config_path and Path(config_path).exists():
                try:
                    with open(config_path, "r", encoding="utf-8") as f:
                        file_config = yaml.safe_load(f) or {}
                    
                    # Convert keys to match pydantic field names
                    normalized_config = {}
                    for key, value in file_config.items():
                        # Remove LOKI_ prefix if present and convert to lowercase
                        normalized_key = key.lower()
                        if normalized_key.startswith("loki_"):
                            normalized_key = normalized_key[5:]
                        normalized_config[normalized_key] = value
                    
                    config_data.update(normalized_config)
                    logger.info("Configuration file loaded", path=str(config_path))
                    break
                    
                except Exception as e:
                    logger.warning(
                        "Failed to load configuration file",
                        path=str(config_path),
                        error=str(e),
                    )
        
        return config_data
    
    def _load_server_config_from_env(self) -> dict:
        """Load server configuration from MCP_ environment variables."""
        server_config = {}
        
        # Map MCP environment variables to config fields
        env_mappings = {
            "MCP_SERVER_MODE": "server_mode",
            "MCP_SERVER_HOST": "server_host", 
            "MCP_SERVER_PORT": "server_port",
        }
        
        for env_var, config_key in env_mappings.items():
            value = os.environ.get(env_var)
            if value is not None:
                # Convert port to integer
                if config_key == "server_port":
                    try:
                        value = int(value)
                    except ValueError:
                        logger.warning(
                            "Invalid port value in environment variable",
                            env_var=env_var,
                            value=value
                        )
                        continue
                
                server_config[config_key] = value
                logger.debug("Server config loaded from environment", env_var=env_var, value=value)
        
        return server_config
    
    def _load_bearer_token_from_file(self) -> None:
        """Load bearer token from file if specified."""
        if self.bearer_token_file and not self.bearer_token:
            try:
                token_path = Path(self.bearer_token_file)
                if token_path.exists():
                    self.bearer_token = token_path.read_text(encoding="utf-8").strip()
                    logger.info("Bearer token loaded from file", path=str(token_path))
                else:
                    logger.warning("Bearer token file not found", path=str(token_path))
            except Exception as e:
                logger.warning(
                    "Failed to load bearer token from file",
                    path=self.bearer_token_file,
                    error=str(e),
                )
    
    def get_safe_config(self) -> dict:
        """Get configuration dict with sensitive data redacted."""
        config_dict = self.model_dump()
        
        # Redact sensitive fields
        sensitive_fields = ["password", "bearer_token"]
        for field in sensitive_fields:
            if config_dict.get(field):
                config_dict[field] = "[REDACTED]"
        
        return config_dict
